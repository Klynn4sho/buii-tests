"""
Invite tracking & growth analytics: join/leave logging, risk scoring for
new accounts, the live auto-refreshing dashboard panel, growth graphs,
inviter leaderboard, and the guild config commands (log channel, alert
role, mod role, prefix).

Every response here is a Components V2 layout, not an embed. Per the
project's rule (core/components.py), a bordered Container is used only
where interactive buttons/selects are attached (the dashboard panel, the
graph range picker); everything else — including the risk-tier join
alerts — is borderless TextDisplay content. The risk color signal isn't
lost: it survives via the 🚨/⚠️/🟢 status emoji and the 🟥/🟨/🟩 maturity
bar, which already carried that meaning even inside the old embed.
"""

import asyncio
import io
from datetime import datetime, timezone

import discord
from discord import ui
from discord.ext import commands, tasks

from core import database
from core.checks import has_mod_permission
from core.components import SimpleLayout, Layout, footer_line
from core.helpers import make_bar, account_maturity_bar, build_joins_graph_async
from views.growth_views import DashboardView, GraphView


def _risk_status(account_age_days: int) -> str:
    if account_age_days < 7:
        return "🚨 EXTREME RISK (New Account)"
    elif account_age_days < 30:
        return "⚠️ HIGH RISK (Under 30 Days)"
    return "🟢 LOW RISK (Established User)"


class GrowthCog(commands.Cog, name="GrowthCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invites_cache: dict[int, dict[str, int]] = {}
        self.invite_locks: dict[int, asyncio.Lock] = {}
        self._ready_once = False

    async def cog_load(self):
        # Persistent-view registration is purely local dispatch-table
        # bookkeeping — discord.py never transmits these objects anywhere,
        # so DashboardView()'s placeholder content and GraphView's dummy
        # placeholder file (never actually uploaded) are both safe to use
        # here; only the buttons'/select's custom_ids need to match.
        self.bot.add_view(DashboardView())
        self.bot.add_view(GraphView(days=30, graph_file=discord.File(
            fp=io.BytesIO(b""), filename="joins_graph.png"
        )))

    def cog_unload(self):
        self.panel_refresh_loop.cancel()

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
                        await message.edit(view=new_view)
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

        text = (
            alert_role_mention
            + f"**{member.name} joined the server**\n\n"
            + f"**👤 User Information**\n┣ User: {member.mention}\n┣ Username: **{member.name}**\n┗ User ID: `{member.id}`\n\n"
            + f"**🔗 Invite Details**\n┣ Inviter: **{i_mention}**\n┣ Username: **{inviter}**\n┗ Code: `{used_code}`\n\n"
            + f"**🛡️ Security Risk Assessment**\n**{alert_status}**\n`{maturity_bar}` **{account_age_days}d** old\n"
            + footer_line(f"Member #{guild.member_count} • Total Members: {guild.member_count}")
        )

        items = [ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=member.display_avatar.url))]
        await welcome_channel.send(view=Layout(*items), allowed_mentions=discord.AllowedMentions(roles=True))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await database.async_save_leave(member.guild.id, member.id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="testjoin", description="Simulate a member join event to test and preview the join alert layout.")
    @has_mod_permission()
    async def testjoin(self, ctx: commands.Context, account_age_days: int = 2):
        alert_status = _risk_status(account_age_days)
        maturity_bar = account_maturity_bar(account_age_days)

        text = (
            "🧪 **Test Join Alert Preview**\n\n"
            f"**{ctx.author.name} (TEST PREVIEW)**\n\n"
            f"**👤 User Information**\n┣ User: {ctx.author.mention}\n┣ Username: **{ctx.author.name}**\n┗ User ID: `{ctx.author.id}`\n\n"
            f"**🔗 Invite Details**\n┣ Inviter: **{ctx.author.mention}**\n┣ Username: **TestInviter#0001**\n┗ Code: `TESTCODE`\n\n"
            f"**🛡️ Security Risk Assessment**\n**{alert_status}**\n`{maturity_bar}` **{account_age_days}d** old\n"
            + footer_line(f"Member #{ctx.guild.member_count} • Total Members: {ctx.guild.member_count} (TEST PREVIEW)")
        )

        items = [ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=ctx.author.display_avatar.url))]
        await ctx.send(view=Layout(*items))

    @commands.hybrid_command(name="leaderboard", aliases=["lb"], description="Displays top inviters based on recorded join history.")
    async def leaderboard(self, ctx: commands.Context):
        results = await database.async_get_leaderboard(ctx.guild.id, limit=10)

        text = "# 🏆 INVITER LEADERBOARD\n-# Top server inviters ranked by recorded join history.\n\n"
        if results:
            medals = ["🥇", "🥈", "🥉"]
            lines = [f"{medals[idx] if idx < 3 else f'`#{idx+1}`'} **{name}** — **{count} joins**"
                     for idx, (name, count) in enumerate(results)]
            text += "\n".join(lines)
        else:
            text += "*No tracked join data available yet.*"
        text += "\n" + footer_line("Leaderboard Metrics")

        await ctx.send(view=SimpleLayout(text))

    @commands.hybrid_command(name="invites", description="View a member's invite history and stats.")
    async def invites(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        totals, left_count, recent = await database.async_get_inviter_stats(ctx.guild.id, member.name)

        total_joins = totals["total_joins"] if totals else 0
        if not total_joins:
            await ctx.send(f"❌ No recorded joins are credited to **{member.display_name}** yet.", ephemeral=True)
            return

        flagged_alts = totals["flagged_alts"] or 0
        retained = total_joins - left_count
        retention_pct = (retained / total_joins * 100) if total_joins else 0.0

        text = (
            f"# 🔗 {member.display_name}'s Invite Stats\n"
            f"Total Invites: **{total_joins}**\n"
            f"Still in Server: **{retained}** (**{retention_pct:.0f}%**)\n"
            f"Left Since Joining: **{left_count}**\n"
        )
        if flagged_alts:
            text += f"\n🚩 **Flagged New Accounts**\n**{flagged_alts}** invited account(s) were under 7 days old at join time.\n"
        if recent:
            lines = []
            for row in recent:
                age_flag = " 🚩" if row["account_age_days"] < 7 else ""
                lines.append(f"• **{row['user_name']}**{age_flag} — {row['join_date']}")
            text += "\n**🕒 Most Recent Invitees**\n" + "\n".join(lines) + "\n"
        text += footer_line("Invite Attribution — matched by username at join time")

        items = [ui.Section(ui.TextDisplay(text), accessory=ui.Thumbnail(media=member.display_avatar.url))]
        await ctx.send(view=Layout(*items))

    @commands.hybrid_command(name="statspanel", aliases=["sp"], description="Deploys an auto-refreshing live server growth dashboard.")
    @has_mod_permission()
    async def statspanel(self, ctx: commands.Context):
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
        panel_message = await ctx.send(view=view)
        await database.async_save_panel_config(ctx.guild.id, ctx.channel.id, panel_message.id)

    @commands.hybrid_command(name="graph", aliases=["g"], description="Displays daily growth trend charts.")
    @has_mod_permission()
    async def graph(self, ctx: commands.Context, days: int = 30):
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
            await ctx.send("⚠️ Prefix length must be 5 characters or fewer.")
            return
        await database.async_set_prefix(ctx.guild.id, new_prefix)
        await ctx.send(view=SimpleLayout(f"🔧 **PREFIX UPDATED**\nServer prefix successfully updated to `{new_prefix}`"))


async def setup(bot: commands.Bot):
    await bot.add_cog(GrowthCog(bot))
