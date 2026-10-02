"""Server overview command rendered as a Components V2 information card."""

import discord
from discord import ui
from discord.ext import commands

from core.components import Layout, footer_line, notice

class ServerInfoCog(commands.Cog, name="ServerInfoCog"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="serverinfo", aliases=["server", "guildinfo"], description="Show an overview of this server and its current structure.")
    async def serverinfo(self, ctx: commands.Context):
        guild = ctx.guild
        if guild is None:
            await ctx.send(view=notice("This command can only be used inside a server."))
            return

        members = list(guild.members)
        humans = sum(not member.bot for member in members)
        bots = sum(member.bot for member in members)
        owner = guild.owner
        owner_text = owner.display_name if owner else f"ID {guild.owner_id}"
        verification = str(guild.verification_level).replace("VerificationLevel.", "").replace("_", " ").title()
        boost_text = f"Tier {guild.premium_tier} · {guild.premium_subscription_count or 0} boosts"
        feature_text = ", ".join(feature.replace("_", " ").title() for feature in guild.features[:5]) or "None"

        header_text = ui.TextDisplay(
            f"# {discord.utils.escape_markdown(guild.name)}\n"
            f"-# Server overview · {guild.member_count or len(members):,} members"
        )
        header = (
            ui.Section(header_text, accessory=ui.Thumbnail(media=guild.icon.url))
            if guild.icon else header_text
        )
        overview = (
            "### Overview\n"
            f"**Owner**  ·  {discord.utils.escape_markdown(owner_text)}\n"
            f"**Created**  ·  {discord.utils.format_dt(guild.created_at, style='F')} ({discord.utils.format_dt(guild.created_at, style='R')})\n"
            f"**Server ID**  ·  `{guild.id}`\n"
            f"**Verification**  ·  {verification}"
        )
        people = (
            "### People & structure\n"
            f"**Members**  ·  `{humans:,}` humans · `{bots:,}` bots\n"
            f"**Channels**  ·  `{len(guild.channels):,}` total · `{len(guild.text_channels):,}` text · `{len(guild.voice_channels):,}` voice\n"
            f"**Roles**  ·  `{max(0, len(guild.roles) - 1):,}` custom roles"
        )
        community = (
            "### Community\n"
            f"**Boosting**  ·  {boost_text}\n"
            f"**Features**  ·  {discord.utils.escape_markdown(feature_text)}\n"
            f"**Shard**  ·  `{guild.shard_id}`"
        )

        view = Layout(
            header,
            ui.Separator(),
            ui.TextDisplay(overview),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(people),
            ui.Separator(spacing=discord.SeparatorSpacing.small),
            ui.TextDisplay(community),
            ui.Separator(),
            ui.TextDisplay(footer_line("Server Information")),
        )
        await ctx.send(view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(ServerInfoCog(bot))