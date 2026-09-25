"""
Invite tracking & growth analytics: join/leave logging, risk scoring for
new accounts, the live auto-refreshing dashboard panel, growth graphs,
inviter leaderboard, and the guild config commands (log channel, alert
role, mod role, prefix).
"""

import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from core import database
from core.checks import has_mod_permission
from core.config import COLOR_DANGER, COLOR_WARNING, COLOR_SUCCESS, COLOR_BRAND
from core.helpers import themed_footer, make_bar, account_maturity_bar, async_build_dashboard_embed, build_joins_graph_async
from views.growth_views import DashboardView, GraphView


class GrowthCog(commands.Cog, name="GrowthCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.invites_cache: dict[int, dict[str, int]] = {}
        self.invite_locks: dict[int, asyncio.Lock] = {}
        self._ready_once = False

    async def cog_load(self):
        # Persistent views need to exist before Discord dispatches any
        # interaction for them, and don't depend on gateway/guild state, so
        # they're safe to register as soon as the cog loads.
        self.bot.add_view(DashboardView())
        self.bot.add_view(GraphView())

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
                        new_embed = await async_build_dashboard_embed(guild, self.bot)
                        await message.edit(embed=new_embed, view=DashboardView())
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
        account_created = member.created_at
        age_timedelta = now - account_created
        account_age_days = age_timedelta.days

        if account_age_days < 7:
            alert_status = "🚨 EXTREME RISK (New Account)"
            embed_color = COLOR_DANGER
        elif account_age_days < 30:
            alert_status = "⚠️ HIGH RISK (Under 30 Days)"
            embed_color = COLOR_WARNING
        else:
            alert_status = "🟢 LOW RISK (Established User)"
            embed_color = COLOR_SUCCESS

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

        embed = discord.Embed(color=embed_color, timestamp=datetime.now(timezone.utc))
        embed.set_author(name=f"{member.name} joined the server", icon_url=member.display_avatar.url)
        if member.avatar:
            embed.set_thumbnail(url=member.avatar.url)

        embed.add_field(
            name="👤 User Information",
            value=f"┣ User: **{member.mention}**\n┣ Username: **{member.name}**\n┗ User ID: `{member.id}`",
            inline=True
        )
        embed.add_field(
            name="🔗 Invite Details",
            value=f"┣ Inviter: **{i_mention}**\n┣ Username: **{inviter}**\n┗ Code: `{used_code}`",
            inline=True
        )
        embed.add_field(
            name="🛡️ Security Risk Assessment",
            value=(
                f"**{alert_status}**\n"
                f"`{maturity_bar}` **{account_age_days}d** old"
            ),
            inline=False
        )
        themed_footer(embed, self.bot, f"Member #{guild.member_count} • Total Members: {guild.member_count}")

        alert_content = None
        if account_age_days < 7:
            alert_role_id = await database.async_get_alert_role(guild.id)
            if alert_role_id:
                alert_role = guild.get_role(alert_role_id)
                if alert_role:
                    alert_content = alert_role.mention

        await welcome_channel.send(content=alert_content, embed=embed,
                                    allowed_mentions=discord.AllowedMentions(roles=True))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await database.async_save_leave(member.guild.id, member.id)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @commands.hybrid_command(name="testjoin", description="Simulate a member join event to test and preview the join alert embed.")
    @has_mod_permission()
    async def testjoin(self, ctx: commands.Context, account_age_days: int = 2):
        now = datetime.now(timezone.utc)
        if account_age_days < 7:
            alert_status = "🚨 EXTREME RISK (New Account)"
            embed_color = COLOR_DANGER
        elif account_age_days < 30:
            alert_status = "⚠️ HIGH RISK (Under 30 Days)"
            embed_color = COLOR_WARNING
        else:
            alert_status = "🟢 LOW RISK (Established User)"
            embed_color = COLOR_SUCCESS

        maturity_bar = account_maturity_bar(account_age_days)

        embed = discord.Embed(color=embed_color, timestamp=now)
        embed.set_author(name=f"{ctx.author.name} (TEST PREVIEW)", icon_url=ctx.author.display_avatar.url)
        if ctx.author.avatar:
            embed.set_thumbnail(url=ctx.author.avatar.url)

        embed.add_field(name="👤 User Information", value=f"┣ User: **{ctx.author.mention}**\n┣ Username: **{ctx.author.name}**\n┗ User ID: `{ctx.author.id}`", inline=True)
        embed.add_field(name="🔗 Invite Details", value=f"┣ Inviter: **{ctx.author.mention}**\n┣ Username: **TestInviter#0001**\n┗ Code: `TESTCODE`", inline=True)
        embed.add_field(name="🛡️ Security Risk Assessment", value=(f"**{alert_status}**\n`{maturity_bar}` **{account_age_days}d** old"), inline=False)
        themed_footer(embed, self.bot, f"Member #{ctx.guild.member_count} • Total Members: {ctx.guild.member_count} (TEST PREVIEW)")

        await ctx.send(content="🧪 **Test Join Alert Preview:**", embed=embed)

    @commands.hybrid_command(name="leaderboard", aliases=["lb"], description="Displays top inviters based on recorded join history.")
    async def leaderboard(self, ctx: commands.Context):
        results = await database.async_get_leaderboard(ctx.guild.id, limit=10)
        embed = discord.Embed(title="🏆 INVITER LEADERBOARD", description="Top server inviters ranked by recorded join history.", color=COLOR_BRAND, timestamp=datetime.now(timezone.utc))
        if results:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for idx, (inviter_name, count) in enumerate(results):
                medal = medals[idx] if idx < 3 else f"`#{idx+1}`"
                lines.append(f"{medal} **{inviter_name}** — **{count} joins**")
            embed.add_field(name="Top Rankings", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Top Rankings", value="*No tracked join data available yet.*", inline=False)

        themed_footer(embed, self.bot, "Leaderboard Metrics")
        await ctx.send(embed=embed)

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

        embed = discord.Embed(
            title=f"🔗 {member.display_name}'s Invite Stats",
            color=COLOR_BRAND,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Total Invites", value=f"**{total_joins}**", inline=True)
        embed.add_field(name="Still in Server", value=f"**{retained}** (**{retention_pct:.0f}%**)", inline=True)
        embed.add_field(name="Left Since Joining", value=f"**{left_count}**", inline=True)
        if flagged_alts:
            embed.add_field(name="🚩 Flagged New Accounts", value=f"**{flagged_alts}** invited account(s) were under 7 days old at join time.", inline=False)

        if recent:
            lines = []
            for row in recent:
                age_flag = " 🚩" if row["account_age_days"] < 7 else ""
                lines.append(f"• **{row['user_name']}**{age_flag} — {row['join_date']}")
            embed.add_field(name="🕒 Most Recent Invitees", value="\n".join(lines), inline=False)

        themed_footer(embed, self.bot, "Invite Attribution — matched by username at join time")
        await ctx.send(embed=embed)

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

        embed = await async_build_dashboard_embed(ctx.guild, self.bot)
        panel_message = await ctx.send(embed=embed, view=DashboardView())
        await database.async_save_panel_config(ctx.guild.id, ctx.channel.id, panel_message.id)

    @commands.hybrid_command(name="graph", aliases=["g"], description="Displays daily growth trend charts.")
    @has_mod_permission()
    async def graph(self, ctx: commands.Context, days: int = 30):
        graph_file = await build_joins_graph_async(ctx.guild.id, days=days)
        embed = discord.Embed(title=f"📈 GROWTH TRENDS — LAST {days} DAYS", color=COLOR_BRAND, timestamp=datetime.now(timezone.utc))
        embed.set_image(url="attachment://joins_graph.png")
        themed_footer(embed, self.bot, "Visual Intelligence")
        await ctx.send(embed=embed, file=graph_file, view=GraphView())

    @commands.hybrid_command(name="setlog", aliases=["sl"], description="Lock incoming join alert notifications to this channel.")
    @has_mod_permission()
    async def setlog(self, ctx: commands.Context):
        await database.async_set_guild_log_channel(ctx.guild.id, ctx.channel.id)
        embed = discord.Embed(title="🎯 LOG CHANNEL LOCKED", description=f"Join notification alerts successfully locked to {ctx.channel.mention}.", color=COLOR_SUCCESS)
        themed_footer(embed, self.bot)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setalertrole", aliases=["sar"], description="Set the role pinged when a high-risk (new account) join is detected.")
    @has_mod_permission()
    async def setalertrole(self, ctx: commands.Context, role: discord.Role = None):
        if role is None:
            await database.async_set_alert_role(ctx.guild.id, None)
            embed = discord.Embed(title="🔕 ALERT ROLE CLEARED", description="Extreme-risk join alerts will no longer ping a role.", color=COLOR_SUCCESS)
        else:
            await database.async_set_alert_role(ctx.guild.id, role.id)
            embed = discord.Embed(title="🚨 ALERT ROLE SET", description=f"Will now ping {role.mention} when an extreme-risk (new account, <7d old) join is detected.", color=COLOR_SUCCESS)
        themed_footer(embed, self.bot)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setmodrole", description="Set a role that can manage bot config commands without full Manage Server permission.")
    @commands.has_permissions(administrator=True)
    async def setmodrole(self, ctx: commands.Context, role: discord.Role = None):
        # Deliberately administrator-gated, not @has_mod_permission() — a
        # mod role shouldn't be able to grant/change/revoke itself.
        if role is None:
            await database.async_set_mod_role(ctx.guild.id, None)
            embed = discord.Embed(title="🔕 MOD ROLE CLEARED", description="Only members with Manage Server can use bot config commands now.", color=COLOR_SUCCESS)
        else:
            await database.async_set_mod_role(ctx.guild.id, role.id)
            embed = discord.Embed(title="🛠️ MOD ROLE SET", description=f"{role.mention} can now use bot config commands (setlog, setprefix, music settings, etc.) without needing Manage Server.", color=COLOR_SUCCESS)
        themed_footer(embed, self.bot)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="setprefix", aliases=["pfx"], description="Modify server command prefix.")
    @has_mod_permission()
    async def setprefix(self, ctx: commands.Context, new_prefix: str):
        if len(new_prefix) > 5:
            await ctx.send("⚠️ Prefix length must be 5 characters or fewer.")
            return
        await database.async_set_prefix(ctx.guild.id, new_prefix)
        embed = discord.Embed(title="🔧 PREFIX UPDATED", description=f"Server prefix successfully updated to `{new_prefix}`", color=COLOR_SUCCESS)
        themed_footer(embed, self.bot)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(GrowthCog(bot))
