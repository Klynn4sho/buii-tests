"""
Invite tracking & growth analytics: join/leave logging, risk scoring for
new accounts, the live auto-refreshing dashboard panel, growth graphs,
inviter leaderboard, and the guild config commands (log channel, alert
role, mod role, prefix).

Every response here is a Components V2 layout inside a Container (see
core/components.py). Containers carry NO accent by default; an accent is
set only for invite content: the join alert and /testjoin use the risk
tier color (red/yellow/green, matching the 🚨/⚠️/🟢 status and the
🟥/🟨/🟩 maturity bar), and /leaderboard, /invites, the dashboard and the
graph picker use the brand color. Config confirmations and errors are
plain, un-accented containers.
"""

import asyncio
import io
import random
import re
from datetime import datetime, timezone

import discord
from discord import ui
from discord.ext import commands, tasks

from core import database
from core.checks import has_mod_permission
from core.components import SimpleLayout, Layout, footer_line, notice
from core.config import COLOR_BRAND, COLOR_DANGER, COLOR_WARNING, COLOR_SUCCESS
from core.helpers import make_bar, account_maturity_bar, build_joins_graph_async, create_join_card, create_invite_stats_card
from views.growth_views import DashboardView, GraphView, JoinAlertView, JoinValueButton, InviteStatsView


def _risk_status(account_age_days: int) -> str:
    if account_age_days < 7:
        return "🚨 EXTREME RISK (New Account)"
    elif account_age_days < 30:
        return "⚠️ HIGH RISK (Under 30 Days)"
    return "🟢 LOW RISK (Established User)"


def _risk_color(account_age_days: int) -> discord.Color:
    if account_age_days < 7:
        return COLOR_DANGER
    elif account_age_days < 30:
        return COLOR_WARNING
    return COLOR_SUCCESS


async def userinfo_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    query = current.strip().lower()
    choices = []
    seen = set()
    members = sorted(interaction.guild.members, key=lambda m: (not m.display_name.lower().startswith(query), m.display_name.lower()))
    for member in members:
        if query and query not in member.display_name.lower() and query not in member.name.lower() and query not in str(member.id):
            continue
        choices.append(discord.app_commands.Choice(name=f'{member.display_name} (@{member.name})'[:100], value=str(member.id)))
        seen.add(member.id)
        if len(choices) >= 25:
            return choices
    saved = await database.async_search_member_snapshots(interaction.guild.id, query, limit=25)
    for row in saved:
        user_id = int(row['user_id'])
        if user_id in seen:
            continue
        label = row['display_name'] or row['username'] or str(user_id)
        status = 'in server' if row['is_member'] else 'saved record'
        choices.append(discord.app_commands.Choice(name=f'{label} · {status}'[:100], value=str(user_id)))
        if len(choices) >= 25:
            break
    return choices


class GrowthCog(commands.Cog, name="GrowthCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invites_cache: dict[int, dict[str, int]] = {}
        self.invite_locks: dict[int, asyncio.Lock] = {}
        self._ready_once = False
        self.member_snapshot_task: asyncio.Task | None = None

    async def cog_load(self):
        # Persistent-view registration is purely local dispatch-table
        # bookkeeping — discord.py never transmits these objects anywhere,
        # so DashboardView()'s placeholder content and GraphView's dummy
        # placeholder file (never actually uploaded) are both safe to use
        # here; only the buttons'/select's custom_ids need to match.
        self.bot.add_dynamic_items(JoinValueButton)
        self.bot.add_view(DashboardView())
        self.bot.add_view(GraphView(days=30, graph_file=discord.File(
            fp=io.BytesIO(b""), filename="joins_graph.png"
        )))

    def cog_unload(self):
        self.panel_refresh_loop.cancel()
        if self.member_snapshot_task:
            self.member_snapshot_task.cancel()

    async def snapshot_member(self, member: discord.Member, is_member: bool = True):
        roles = [role.name for role in member.roles if not role.is_default()]
        badges = []
        try:
            for item in member.public_flags.all():
                if isinstance(item, tuple):
                    name, enabled = item
                    if enabled:
                        badges.append(str(name).replace("_", " ").title())
                else:
                    name = getattr(item, "name", str(item))
                    badges.append(str(name).replace("_", " ").title())
        except Exception:
            pass

        await database.async_upsert_member_snapshot(
            member.guild.id,
            member.id,
            member.name,
            member.display_name,
            member.display_avatar.url,
            member.created_at,
            member.joined_at,
            member.premium_since is not None,
            roles,
            badges,
            is_member=is_member,
        )

    async def member_snapshot_loop(self):
        while True:
            try:
                for guild in self.bot.guilds:
                    try:
                        members = [member async for member in guild.fetch_members(limit=None)]
                    except (discord.Forbidden, discord.HTTPException):
                        members = guild.members
                    for member in members:
                        await self.snapshot_member(member)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"[growth] member snapshot refresh failed: {error!r}")
            await asyncio.sleep(random.uniform(13 * 3600, 24 * 3600))

    # ------------------------------------------------------------------
    # Background loop: refresh every live dashboard panel once an hour
    # ------------------------------------------------------------------
    @tasks.loop(seconds=3600)
    async def panel_refresh_loop(self):
        for guild in self.bot.guilds:
            config = await database.async_get_panel_config(guild.id)
            if config:
                channel_id, msg_id = int(config[0]), int(config[1])
                channel = guild.get_channel(channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(msg_id)
                        new_view = await DashboardView.build(guild)
                        await message.edit(view=new_view, attachments=[new_view.file])
                    except Exception:
                        pass

    @panel_refresh_loop.before_loop
    async def before_panel_refresh_loop(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready can fire again after a reconnect — only warm the invite
        # cache and start the loop once per process.
        for guild in self.bot.guilds:
            try:
                self.invites_cache[guild.id] = {inv.code: inv.uses for inv in await guild.invites()}
            except discord.Forbidden:
                pass

        if not self._ready_once:
            self._ready_once = True
            self.panel_refresh_loop.start()
            self.member_snapshot_task = asyncio.create_task(self.member_snapshot_loop())

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        try:
            self.invites_cache[guild.id] = {inv.code: inv.uses for inv in await guild.invites()}
        except discord.Forbidden:
            pass

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild_cache = self.invites_cache.setdefault(invite.guild.id, {})
        guild_cache[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild_cache = self.invites_cache.get(invite.guild.id)
        if guild_cache is not None:
            guild_cache.pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        await self.snapshot_member(member)

        log_channel_id = await database.async_get_guild_log_channel(guild.id)
        if log_channel_id:
            welcome_channel = guild.get_channel(log_channel_id)
        else:
            welcome_channel = discord.utils.get(guild.text_channels, name="welcome")

        if not welcome_channel:
            return

        now = datetime.now(timezone.utc)
        account_age_days = (now - member.created_at).days
        alert_status = _risk_status(account_age_days)

        await asyncio.sleep(1)

        i_mention = "N/A"
        inviter = "Unknown / Custom Link"
        inviter_id = None
        used_code = "Unknown"

        # Serialize invite diff+cache-update per guild so two members joining
        # within the same second can't both match against the same stale
        # cache entry (which previously could misattribute the second join).
        lock = self.invite_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            try:
                current_invites = await guild.invites()
                old_invites = self.invites_cache.get(guild.id, {})

                for inv in current_invites:
                    if inv.code in old_invites and inv.uses > old_invites[inv.code]:
                        if inv.inviter is not None:
                            i_mention = inv.inviter.mention
                            inviter = inv.inviter.name
                            inviter_id = inv.inviter.id
                        used_code = inv.code
                        break

                self.invites_cache[guild.id] = {inv.code: inv.uses for inv in current_invites}
            except (discord.Forbidden, AttributeError):
                pass

        await database.async_save_join(guild.id, member.id, member.name, inviter, used_code, account_age_days)

        maturity_bar = account_maturity_bar(account_age_days)

        alert_role_mention = ""
        if account_age_days < 7:
            alert_role_id = await database.async_get_alert_role(guild.id)
            if alert_role_id:
                alert_role = guild.get_role(alert_role_id)
                if alert_role:
                    alert_role_mention = alert_role.mention + "\n"

        card_buf = await create_join_card(
            self.bot.http_session, member.display_avatar.url, member.display_name, member.name,
            member.id, inviter, inviter_id, used_code, account_age_days, alert_status,
            guild.member_count, _risk_color(account_age_days).to_rgb(),
        )
        ping_text = " ".join(part for part in (alert_role_mention.strip(), member.mention) if part)
        view = JoinAlertView(
            discord.File(fp=card_buf, filename=f"join-{member.id}.png"),
            ping_text, member.id, inviter_id, used_code,
            _risk_color(account_age_days), member.id,
        )
        try:
            await welcome_channel.send(
                view=view, file=view.file,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
        except discord.HTTPException as e:
            print(f"[growth] couldn't post join alert in guild {guild.id}: {e!r}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self.snapshot_member(member, is_member=False)
        await database.async_save_leave(member.guild.id, member.id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="testjoin", description="Simulate a member join event to test and preview the join alert layout.")
    @has_mod_permission()
    async def testjoin(self, ctx: commands.Context, account_age_days: int = 2):
        if ctx.interaction:
            await ctx.defer()

        alert_status = _risk_status(account_age_days)
        maturity_bar = account_maturity_bar(account_age_days)

        alert_role_mention = ""
        if account_age_days < 7:
            alert_role_id = await database.async_get_alert_role(ctx.guild.id)
            if alert_role_id:
                alert_role = ctx.guild.get_role(alert_role_id)
                if alert_role:
                    alert_role_mention = alert_role.mention + "\n"

        card_buf = await create_join_card(
            self.bot.http_session, ctx.author.display_avatar.url,
            f"{ctx.author.display_name} (TEST PREVIEW)", ctx.author.name, ctx.author.id,
            ctx.author.name, ctx.author.id, "TESTCODE", account_age_days, alert_status,
            ctx.guild.member_count, _risk_color(account_age_days).to_rgb(),
        )
        ping_text = " ".join(part for part in (alert_role_mention.strip(), ctx.author.mention) if part)
        view = JoinAlertView(
            discord.File(fp=card_buf, filename=f"test-join-{ctx.author.id}.png"),
            ping_text, ctx.author.id, ctx.author.id, "TESTCODE",
            _risk_color(account_age_days), ctx.author.id,
        )
        await ctx.send(
            view=view, file=view.file,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )

    @commands.hybrid_command(name="leaderboard", aliases=["lb"], description="Displays top inviters based on recorded join history.")
    async def leaderboard(self, ctx: commands.Context):
        results = await database.async_get_leaderboard(ctx.guild.id, limit=10)

        lines = [
            "## INVITER LEADERBOARD",
            "-# Ranked by recorded server joins.",
            "",
        ]
        if results:
            for index, (name, count) in enumerate(results, start=1):
                lines.append(f"**{index:02d}.** **{name}**  ·  **{count}** join{'s' if count != 1 else ''}")
        else:
            lines.append("*No tracked join data available yet.*")
        lines.extend(["", footer_line("Server Growth")])

        await ctx.send(view=SimpleLayout("\n".join(lines)))

    @commands.hybrid_command(
        name='userinfo',
        aliases=['user'],
        description='View saved profile and membership information for a user.',
    )
    @discord.app_commands.describe(user='Search a member or paste a user ID')
    @discord.app_commands.autocomplete(user=userinfo_autocomplete)
    async def userinfo(self, ctx: commands.Context, user: str = None):
        raw_user = str(user or ctx.author.id)
        id_match = re.search(r'(\d{15,22})', raw_user)
        if not id_match:
            await ctx.send(view=notice('❌ Enter a member search result, mention, or numeric user ID.'), ephemeral=bool(ctx.interaction))
            return

        user_id = int(id_match.group(1))
        live_member = ctx.guild.get_member(user_id) if ctx.guild else None
        if live_member:
            await self.snapshot_member(live_member)
        snapshot = await database.async_get_member_snapshot(ctx.guild.id, user_id) if ctx.guild else None
        try:
            target = live_member or await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            target = None

        target_name = getattr(target, 'name', None) or (snapshot or {}).get('username') or str(user_id)
        display_name = getattr(target, 'display_name', None) or (snapshot or {}).get('display_name') or target_name
        names = [target_name, display_name]
        if snapshot:
            names.extend([snapshot.get('username'), snapshot.get('display_name')])
        latest_join, invite_count = await database.async_get_member_invite_summary(ctx.guild.id, user_id, names)

        created_at = getattr(target, 'created_at', None) or (snapshot or {}).get('created_at')
        joined_at = ((live_member.joined_at if live_member else None) or (snapshot or {}).get('joined_at') or (latest_join or {}).get('join_date'))
        avatar_url = (live_member.display_avatar.url if live_member else (snapshot or {}).get('avatar_url') or (getattr(target, 'display_avatar', None).url if target else None))
        avatar_url = avatar_url or 'https://cdn.discordapp.com/embed/avatars/0.png'

        def stamp(value):
            if isinstance(value, str):
                try:
                    value = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                except ValueError:
                    return value
            if hasattr(value, 'timestamp'):
                return f'<t:{int(value.timestamp())}:R>'
            return 'Unknown'

        roles = (snapshot or {}).get('roles') or []
        badges = (snapshot or {}).get('badges') or []
        role_text = ', '.join(roles) if roles else 'None recorded'
        badge_text = ', '.join(badges) if badges else 'None recorded'
        if len(role_text) > 850:
            role_text = role_text[:847] + '...'
        if len(badge_text) > 350:
            badge_text = badge_text[:347] + '...'
        status = '✅ In this server' if live_member else '⚪ Not currently in this server'
        boosting = live_member.premium_since is not None if live_member else bool((snapshot or {}).get('boosting'))
        inviter = (latest_join or {}).get('inviter_name') or 'Unknown'
        invite_code = (latest_join or {}).get('invite_code') or 'Unknown'
        join_age = (latest_join or {}).get('account_age_days')
        join_detail = f'{join_age} days old at join' if join_age is not None else 'No join record'

        header = ui.Section(
            ui.TextDisplay(f'## **{display_name}**\n-# <@{user_id}> · @{target_name}'),
            accessory=ui.Thumbnail(media=avatar_url),
        )
        summary = ui.TextDisplay(
            f'**Membership**  ·  {status}\n'
            f'**Created**  ·  {stamp(created_at)}\n'
            f'**Joined**  ·  {stamp(joined_at)}\n'
            f'**Boosting**  ·  {"✅ Yes" if boosting else "No"}'
        )
        invites = ui.TextDisplay(
            f'**Invited by**  ·  {inviter}\n'
            f'**Invite code**  ·  {invite_code}\n'
            f'**Invites**  ·  {invite_count}\n'
            f'-# {join_detail}'
        )
        details = ui.TextDisplay(
            f'**Badges**\n{badge_text}\n\n'
            f'**Roles**\n{role_text}\n\n'
            f'**User ID**  ·  `{user_id}`'
        )
        items = [
            header,
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            summary,
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            invites,
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            details,
            ui.TextDisplay(footer_line(f'Last saved {stamp((snapshot or {}).get("last_seen_at"))}')),
            ui.ActionRow(ui.Button(label='View Profile', style=discord.ButtonStyle.link, url=f'https://discord.com/users/{user_id}')),
        ]
        await ctx.send(view=Layout(*items), ephemeral=bool(ctx.interaction))
    @commands.hybrid_command(name="invites", description="View a member's invite history and stats.")
    async def invites(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        totals, left_count, recent = await database.async_get_inviter_stats(ctx.guild.id, member.name)

        total_joins = totals["total_joins"] if totals else 0
        if not total_joins:
            await ctx.send(view=notice(f"❌ No recorded joins are credited to **{member.display_name}** yet."), ephemeral=True)
            return

        flagged_alts = totals["flagged_alts"] or 0
        code_rows = await database.async_get_invite_code_stats(ctx.guild.id, member.name)
        card_buf = await create_invite_stats_card(
            self.bot.http_session,
            member.display_avatar.url,
            member.display_name,
            member.name,
            total_joins,
            total_joins - left_count,
            left_count,
            flagged_alts,
            recent,
        )
        view = InviteStatsView(
            discord.File(fp=card_buf, filename=f"invite-stats-{member.id}.png"),
            ctx.guild.id,
            member.name,
            code_rows,
        )
        await ctx.send(view=view, file=view.file)

    @commands.hybrid_command(name="statspanel", aliases=["sp"], description="Deploys an auto-refreshing live server growth dashboard.")
    @has_mod_permission()
    async def statspanel(self, ctx: commands.Context):
        if ctx.interaction:
            await ctx.defer()
        config = await database.async_get_panel_config(ctx.guild.id)
        if config:
            old_channel_id, old_msg_id = int(config[0]), int(config[1])
            old_channel = ctx.guild.get_channel(old_channel_id)
            if old_channel:
                try:
                    old_msg = await old_channel.fetch_message(old_msg_id)
                    await old_msg.delete()
                except Exception:
                    pass

        view = await DashboardView.build(ctx.guild)
        panel_message = await ctx.send(view=view, file=view.file)
        await database.async_save_panel_config(ctx.guild.id, ctx.channel.id, panel_message.id)

    @commands.hybrid_command(name="graph", aliases=["g"], description="Displays daily growth trend charts.")
    @has_mod_permission()
    async def graph(self, ctx: commands.Context, days: int = 30):
        if ctx.interaction:
            await ctx.defer()
        view = await GraphView.build(ctx.guild.id, days=days)
        await ctx.send(view=view, file=view.file)

    @commands.hybrid_command(name="setlog", aliases=["sl"], description="Lock incoming join alert notifications to this channel.")
    @has_mod_permission()
    async def setlog(self, ctx: commands.Context):
        await database.async_set_guild_log_channel(ctx.guild.id, ctx.channel.id)
        await ctx.send(view=SimpleLayout(f"🎯 **LOG CHANNEL LOCKED**\nJoin notification alerts successfully locked to {ctx.channel.mention}."))

    @commands.hybrid_command(name="setalertrole", aliases=["sar"], description="Set the role pinged when a high-risk (new account) join is detected.")
    @has_mod_permission()
    async def setalertrole(self, ctx: commands.Context, role: discord.Role = None):
        if role is None:
            await database.async_set_alert_role(ctx.guild.id, None)
            text = "🔕 **ALERT ROLE CLEARED**\nExtreme-risk join alerts will no longer ping a role."
        else:
            await database.async_set_alert_role(ctx.guild.id, role.id)
            text = f"🚨 **ALERT ROLE SET**\nWill now ping {role.mention} when an extreme-risk (new account, <7d old) join is detected."
        await ctx.send(view=SimpleLayout(text))

    @commands.hybrid_command(name="setmodrole", description="Set a role that can manage bot config commands without full Manage Server permission.")
    @commands.has_permissions(administrator=True)
    async def setmodrole(self, ctx: commands.Context, role: discord.Role = None):
        # Deliberately administrator-gated, not @has_mod_permission() — a
        # mod role shouldn't be able to grant/change/revoke itself.
        if role is None:
            await database.async_set_mod_role(ctx.guild.id, None)
            text = "🔕 **MOD ROLE CLEARED**\nOnly members with Manage Server can use bot config commands now."
        else:
            await database.async_set_mod_role(ctx.guild.id, role.id)
            text = f"🛠️ **MOD ROLE SET**\n{role.mention} can now use bot config commands (setlog, setprefix, music settings, etc.) without needing Manage Server."
        await ctx.send(view=SimpleLayout(text))

    @commands.hybrid_command(name="setprefix", aliases=["pfx"], description="Modify server command prefix.")
    @has_mod_permission()
    async def setprefix(self, ctx: commands.Context, new_prefix: str):
        if len(new_prefix) > 5:
            await ctx.send(view=notice("⚠️ Prefix length must be 5 characters or fewer."))
            return
        await database.async_set_prefix(ctx.guild.id, new_prefix)
        await ctx.send(view=SimpleLayout(f"🔧 **PREFIX UPDATED**\nServer prefix successfully updated to `{new_prefix}`"))


async def setup(bot: commands.Bot):
    await bot.add_cog(GrowthCog(bot))
