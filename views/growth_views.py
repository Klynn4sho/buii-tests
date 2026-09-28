"""
Components V2 layouts for the invite/growth dashboard: the refresh +
CSV-export dashboard panel, and the time-range picker attached to /graph.

Both bundle interactive components (buttons/a select menu) with their
explanatory content, so both use a bordered Container — per the project's
rule, that's exactly the case a border earns its keep.
"""

import csv
import io

import discord
from discord import ui

from core import database
from core.config import BYPASS_USER_ID, COLOR_BRAND
from core.components import footer_line
from core.helpers import build_dashboard_content_items, build_joins_graph_async


class DashboardView(ui.LayoutView):
    def __init__(self, content_items: list | None = None):
        super().__init__(timeout=None)

        items = content_items or [ui.TextDisplay("Loading dashboard…")]
        self.container = ui.Container(*items, accent_color=COLOR_BRAND)

        refresh_btn = ui.Button(label="Refresh Stats", style=discord.ButtonStyle.primary,
                                 emoji="🔄", custom_id="btn_refresh_dashboard")
        refresh_btn.callback = self.on_refresh

        export_btn = ui.Button(label="Export History", style=discord.ButtonStyle.secondary,
                                emoji="📥", custom_id="btn_export_csv")
        export_btn.callback = self.on_export

        self.container.add_item(ui.ActionRow(refresh_btn, export_btn))
        self.add_item(self.container)

    @classmethod
    async def build(cls, guild: discord.Guild) -> "DashboardView":
        items = await build_dashboard_content_items(guild)
        return cls(items)

    async def on_refresh(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        new_view = await DashboardView.build(interaction.guild)
        await interaction.response.edit_message(view=new_view)
        await interaction.followup.send("✅ Dashboard metrics refreshed successfully!", ephemeral=True)

    async def on_export(self, interaction: discord.Interaction):
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
        # A plain followup (no view=) is a normal message, so content + a
        # file attachment together is fine here — the "no content/embeds"
        # restriction only applies to messages carrying a LayoutView.
        await interaction.followup.send(content="📊 Here is your exported join history CSV:", file=discord_file, ephemeral=True)


class GraphView(ui.LayoutView):
    """Built fresh per invocation/range-change via GraphView.build() — the
    image already carries the visual weight, so this only wraps it in a
    Container because the select menu (an interactive component) needs to
    live alongside it; a bare image+text with no border would be the
    borderless choice if the picker weren't attached."""
    def __init__(self, days: int, graph_file: discord.File):
        super().__init__(timeout=None)
        self.days = days
        self.file = graph_file

        self.range_select = ui.Select(
            placeholder="Choose time range for growth chart...",
            custom_id="select_graph_range",
            options=[
                discord.SelectOption(label="Last 7 Days", value="7", emoji="📅", default=days == 7),
                discord.SelectOption(label="Last 30 Days", value="30", emoji="📊", default=days == 30),
                discord.SelectOption(label="Last 90 Days", value="90", emoji="📈", default=days == 90),
            ],
        )
        self.range_select.callback = self.on_range_change

        self.container = ui.Container(
            ui.TextDisplay(f"# 📈 GROWTH TRENDS — LAST {days} DAYS"),
            ui.MediaGallery(discord.MediaGalleryItem(graph_file)),
            ui.ActionRow(self.range_select),
            ui.TextDisplay(footer_line("Visual Intelligence")),
            accent_color=COLOR_BRAND,
        )
        self.add_item(self.container)

    @classmethod
    async def build(cls, guild_id: int, days: int = 30) -> "GraphView":
        graph_file = await build_joins_graph_async(guild_id, days=days)
        return cls(days=days, graph_file=graph_file)

    async def on_range_change(self, interaction: discord.Interaction):
        await interaction.response.defer()
        days = int(self.range_select.values[0])
        new_view = await GraphView.build(interaction.guild.id, days=days)
        # edit_original_response goes through the webhook edit endpoint,
        # which (unlike InteractionResponse.edit_message in some library
        # versions) reliably accepts a brand-new file upload as an
        # attachment — needed here since the range change means a whole
        # new image, not just new text.
        await interaction.edit_original_response(view=new_view, attachments=[new_view.file])
