"""
Persistent Views for the invite/growth dashboard: the refresh + CSV-export
dashboard panel, and the time-range dropdown attached to /graph.
"""

import csv
import io
from datetime import datetime, timezone

import discord

from core import database
from core.config import BYPASS_USER_ID
from core.helpers import async_build_dashboard_embed, build_joins_graph_async, themed_footer
from core.config import COLOR_BRAND


class GraphRangeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Last 7 Days", value="7", emoji="📅"),
            discord.SelectOption(label="Last 30 Days", value="30", emoji="📊"),
            discord.SelectOption(label="Last 90 Days", value="90", emoji="📈"),
        ]
        super().__init__(placeholder="Choose time range for growth chart...", options=options, custom_id="select_graph_range")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        days = int(self.values[0])
        graph_file = await build_joins_graph_async(interaction.guild.id, days=days)

        embed = discord.Embed(
            title=f"📈 GROWTH TRENDS — LAST {days} DAYS",
            color=COLOR_BRAND,
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_image(url="attachment://joins_graph.png")
        themed_footer(embed, interaction.client, "Interactive Visual Intelligence")
        await interaction.followup.send(embed=embed, file=graph_file, ephemeral=True)


class GraphView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GraphRangeSelect())


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Refresh Stats", style=discord.ButtonStyle.primary, emoji="🔄", custom_id="btn_refresh_dashboard")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return
        embed = await async_build_dashboard_embed(interaction.guild, interaction.client)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ Dashboard metrics refreshed successfully!", ephemeral=True)

    @discord.ui.button(label="Export History", style=discord.ButtonStyle.secondary, emoji="📥", custom_id="btn_export_csv")
    async def export_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return
        is_admin = interaction.user.guild_permissions.administrator
        is_bypassed = BYPASS_USER_ID is not None and interaction.user.id == BYPASS_USER_ID

        if not (is_admin or is_bypassed):
            await interaction.response.send_message("❌ **Access Denied**: Administrator permissions required.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        rows = await database.async_get_joins_in_range(interaction.guild.id)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['User ID', 'Username', 'Inviter Name', 'Invite Code', 'Join Date UTC', 'Account Age Days'])
        writer.writerows(rows)
        output.seek(0)

        filename = "tracker_export_all_time.csv"
        discord_file = discord.File(fp=io.BytesIO(output.getvalue().encode('utf-8')), filename=filename)
        await interaction.followup.send(content="📊 Here is your exported join history CSV:", file=discord_file, ephemeral=True)
