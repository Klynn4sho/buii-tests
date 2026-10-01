"""
Shared Components V2 building blocks (requires discord.py 2.6+).

Components V2 replaces embeds entirely: a message either uses
`content`/`embeds`, or a `view=` that's a `discord.ui.LayoutView`, never
both. There is no per-message "color" or "footer" the way embeds had —
those are approximated below with a Container's accent color and a small
subtext caption line, respectively.

Design rule followed in every cog/view: EVERY response is wrapped in a
`discord.ui.Container` (the bordered "boxed" panel). The container has NO
accent color by default; an accent is passed explicitly, and only for
invite- and song-related content (join alerts, /invites, the leaderboards,
the dashboard, rating cards, song listings). Everything else — help,
config confirmations, errors, admin output, the staff directory — is a
plain, un-accented container.

Anything that is just a short one-line reply (an error, a "not found",
an ephemeral acknowledgement) goes through `notice()` so it is boxed
like everything else instead of being a bare string.
"""

import discord
from discord import ui


def subtext(text: str) -> str:
    """Small, muted caption line — the closest V2 equivalent to an embed
    footer, using Discord's '-#' subtext markdown."""
    return f"-# {text}"


def footer_line(text: str = "Buii Analytics Core") -> str:
    return subtext(text)


class Layout(ui.LayoutView):
    """The base boxed layout: any number of top-level items (TextDisplay,
    Section, Separator, MediaGallery...) wrapped in one Container.
    `accent` is a discord.Color (or None for no accent stripe) — leave it
    None unless the content is invite- or song-related."""
    def __init__(self, *items, accent: "discord.Color | None" = None):
        super().__init__(timeout=None)
        self.container = ui.Container(*items, accent_color=accent)
        self.add_item(self.container)


class SimpleLayout(Layout):
    """One text block in a container. Use for confirmations, summaries,
    leaderboards and inspection results."""
    def __init__(self, content: str, accent: "discord.Color | None" = None):
        super().__init__(ui.TextDisplay(content), accent=accent)


class SimpleImageLayout(Layout):
    """One text block plus one image in a container — used for things like
    the staff-directory image."""
    def __init__(self, content: str, file: discord.File, accent: "discord.Color | None" = None):
        super().__init__(
            ui.TextDisplay(content),
            ui.MediaGallery(discord.MediaGalleryItem(file)),
            accent=accent,
        )
        self.file = file


def notice(text: str) -> SimpleLayout:
    """A boxed, un-accented one-liner — the replacement for every bare
    `ctx.send("❌ ...")` / `interaction.response.send_message("...")`
    string, which can't be a container on their own."""
    return SimpleLayout(text)
