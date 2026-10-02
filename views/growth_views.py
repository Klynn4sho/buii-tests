"""
Components V2 layouts for the invite/growth dashboard: the refresh +
CSV-export dashboard panel, and the time-range picker attached to /graph.

Both bundle interactive components (buttons/a select menu) with their
explanatory content inside a Container. They are invite content, so they
keep the brand accent color (see core/components.py for the accent rule).
"""

import csv
import io

import discord
from discord import ui

from core import database
from core.config import BYPASS_USER_ID, COLOR_BRAND
from core.components import SimpleLayout, footer_line, notice
from core.helpers import build_dashboard_content_items, build_joins_graph_async


async def _notify_error(interaction: discord.Interaction, text: str):
    """Best-effort boxed error reply, whether or not the interaction has
    already been acknowledged."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(view=notice(text), ephemeral=True)
        else:
            await interaction.response.send_message(view=notice(text), ephemeral=True)
    except Exception:
        pass


class JoinValueButton(ui.DynamicItem[ui.Button], template=r"join_value\|(?P<kind>user|inviter|code)\|(?P<value>[A-Za-z0-9_-]+)"): 
    def __init__(self, item, kind, value):
        super().__init__(item)
        self.kind = kind
        self.value = value

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(item, match.group("kind"), match.group("value"))

    async def callback(self, interaction):
        labels = {"user": "User ID", "inviter": "Inviter ID", "code": "Invite Code"}
        await interaction.response.send_message(
            view=SimpleLayout(
                f"### {labels[self.kind]}\n\n**Value:** `{self.value}`"
            ),
            ephemeral=True,
        )


class JoinAlertView(ui.LayoutView):
    """Invite alert image plus private copyable-value responses."""
    def __init__(self, card_file, ping_text, user_id, inviter_id, invite_code, accent, member_id):
        super().__init__(timeout=None)
        self.file = card_file

        def value_button(label, value, suffix):
            button = ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"join_value|{suffix}|{value}")

            async def callback(interaction):
                await interaction.response.send_message(
                    view=SimpleLayout(
                        f"### {label}\n\n**Value:** `{value}`"
                    ),
                    ephemeral=True,
                )

            button.callback = callback
            return button

        buttons = [
            value_button("User ID", str(user_id), "user"),
            value_button("Inviter ID", str(inviter_id or "Unknown"), "inviter"),
            value_button("Invite Code", str(invite_code or "Unknown"), "code"),
        ]
        self.container = ui.Container(
            ui.TextDisplay(ping_text),
            ui.MediaGallery(discord.MediaGalleryItem(card_file)),
            ui.ActionRow(*buttons),
        )
        self.add_item(self.container)


class InviteCodeSelectView(ui.LayoutView):
    """Ephemeral invite-code picker with per-code invite details."""
    def __init__(self, guild_id: int, inviter_name: str, code_rows: list[dict]):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.inviter_name = inviter_name
        self.code_rows = code_rows[:25]

        options = [
            discord.SelectOption(
                label=str(row["code"])[:100],
                value=str(row["code"])[:100],
                description=f"{row['invite_count']} invite(s) · {row['flagged_count']} new account(s)"[:100],
            )
            for row in self.code_rows
        ]
        self.select = ui.Select(placeholder="Choose an invite code…", options=options)
        self.select.callback = self.on_select
        self.add_item(ui.Container(
            ui.TextDisplay("## Created Invite Codes\nSelect a code to view its invite details."),
            ui.ActionRow(self.select),
        ))

    async def on_select(self, interaction: discord.Interaction):
        code = self.select.values[0]
        invitees = await database.async_get_invitees(
            self.guild_id, self.inviter_name, invite_code=code, limit=25
        )
        row = next((item for item in self.code_rows if str(item["code"]) == code), None)
        lines = [
            f"## Invite Code: `{code}`",
            f"Invites: **{row['invite_count'] if row else len(invitees)}**",
            f"Flagged accounts: **{row['flagged_count'] if row else 0}**",
            "",
        ]
        if invitees:
            lines.append("**People invited**")
            for item in invitees:
                flag = " · new account" if item["account_age_days"] < 7 else ""
                lines.append(f"• **{item['user_name']}** · {item['join_date']}{flag}")
        else:
            lines.append("*No invite records found for this code.*")
        await interaction.response.send_message(view=SimpleLayout("\n".join(lines)), ephemeral=True)


class InviteStatsView(ui.LayoutView):
    """Pillow invite-stats card with private detail actions."""
    def __init__(self, card_file, guild_id: int, inviter_name: str, code_rows: list[dict]):
        super().__init__(timeout=None)
        self.file = card_file
        self.guild_id = guild_id
        self.inviter_name = inviter_name
        self.code_rows = code_rows

        codes_button = ui.Button(label="Invite Codes", style=discord.ButtonStyle.secondary)
        invitees_button = ui.Button(label="Invited People", style=discord.ButtonStyle.secondary)
        codes_button.callback = self.on_codes
        invitees_button.callback = self.on_invitees

        self.container = ui.Container(
            ui.MediaGallery(discord.MediaGalleryItem(card_file)),
            ui.ActionRow(codes_button, invitees_button),
        )
        self.add_item(self.container)

    async def on_codes(self, interaction: discord.Interaction):
        if not self.code_rows:
            await interaction.response.send_message(
                view=notice("No recorded invite codes were found for this member."),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=InviteCodeSelectView(self.guild_id, self.inviter_name, self.code_rows),
            ephemeral=True,
        )

    async def on_invitees(self, interaction: discord.Interaction):
        rows = await database.async_get_invitees(
            self.guild_id, self.inviter_name, limit=25
        )
        lines = [f"## People invited by {self.inviter_name}", ""]
        if rows:
            for row in rows:
                flag = " · new account" if row["account_age_days"] < 7 else ""
                lines.append(f"• **{row['user_name']}** · {row['join_date']}{flag}")
        else:
            lines.append("*No invite records found.*")
        await interaction.response.send_message(
            view=SimpleLayout("\n".join(lines)),
            ephemeral=True,
        )


class DashboardView(ui.LayoutView):
    def __init__(self, content_items: list | None = None):
        super().__init__(timeout=None)

        items = content_items or [ui.TextDisplay("Loading dashboard…")]
        self.container = ui.Container(*items)

        refresh_btn = ui.Button(label="Refresh Stats", style=discord.ButtonStyle.primary,
                                 custom_id="btn_refresh_dashboard")
        refresh_btn.callback = self.on_refresh

        export_btn = ui.Button(label="Export History", style=discord.ButtonStyle.secondary,
                                custom_id="btn_export_csv")
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
        try:
            new_view = await DashboardView.build(interaction.guild)
            await interaction.response.edit_message(view=new_view)
            await interaction.followup.send(view=notice("✅ Dashboard metrics refreshed successfully!"), ephemeral=True)
        except Exception as e:
            print(f"[growth] dashboard refresh failed: {e!r}")
            await _notify_error(interaction, "❌ Couldn't refresh the dashboard — try again in a moment.")

    async def on_export(self, interaction: discord.Interaction):
        if not interaction.guild:
            return
        is_admin = interaction.user.guild_permissions.administrator
        is_bypassed = BYPASS_USER_ID is not None and interaction.user.id == BYPASS_USER_ID

        if not (is_admin or is_bypassed):
            await interaction.response.send_message(
                view=notice("❌ **Access Denied**: Administrator permissions required."), ephemeral=True
            )
            return

        try:
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
            # restriction only applies to messages carrying a LayoutView. This
            # is a raw file delivery, so it stays a plain attachment message.
            await interaction.followup.send(content="📊 Here is your exported join history CSV:", file=discord_file, ephemeral=True)
        except Exception as e:
            print(f"[growth] CSV export failed: {e!r}")
            await _notify_error(interaction, "❌ Couldn't build the CSV export — try again in a moment.")


class GraphView(ui.LayoutView):
    """Built fresh per invocation/range-change via GraphView.build() — the
    image already carries the visual weight, and the select menu (an
    interactive component) lives alongside it in the same container."""
    def __init__(self, days: int, graph_file: discord.File):
        super().__init__(timeout=None)
        self.days = days
        self.file = graph_file

        self.range_select = ui.Select(
            placeholder="Choose time range for growth chart...",
            custom_id="select_graph_range",
            options=[
                discord.SelectOption(label="Last 7 Days", value="7", default=days == 7),
                discord.SelectOption(label="Last 30 Days", value="30", default=days == 30),
                discord.SelectOption(label="Last 90 Days", value="90", default=days == 90),
            ],
        )
        self.range_select.callback = self.on_range_change

        self.container = ui.Container(
            ui.TextDisplay(f"# GROWTH TRENDS — LAST {days} DAYS"),
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
        try:
            await interaction.response.defer()
            days = int(self.range_select.values[0])
            new_view = await GraphView.build(interaction.guild.id, days=days)
            # edit_original_response goes through the webhook edit endpoint,
            # which (unlike InteractionResponse.edit_message in some library
            # versions) reliably accepts a brand-new file upload as an
            # attachment — needed here since the range change means a whole
            # new image, not just new text.
            await interaction.edit_original_response(view=new_view, attachments=[new_view.file])
        except Exception as e:
            print(f"[growth] graph range change failed: {e!r}")
            await _notify_error(interaction, "❌ Couldn't update the growth chart — try again in a moment.")
