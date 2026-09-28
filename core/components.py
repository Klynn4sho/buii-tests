"""
Shared Components V2 building blocks (requires discord.py 2.6+).

Components V2 replaces embeds entirely: a message either uses
`content`/`embeds`, or a `view=` that's a `discord.ui.LayoutView`, never
both. There is no per-message "color" or "footer" the way embeds had —
those are approximated below with an accent-colored Container and a
small subtext caption line, respectively.

Design rule followed in every cog/view: `discord.ui.Container` renders
with a bordered, accent-colored panel — visually the same "boxed" look an
embed had. It's used ONLY when a message bundles interactive components
(buttons/selects) with their explanatory text/media, since the frame
usefully marks the whole thing as one interactive widget (the dashboard
panel, the graph range picker, the rating card, a confirm/cancel prompt).
Everything purely informational — confirmations, leaderboards, stat
summaries, inspection results — uses bare TextDisplay/Section/MediaGallery
directly on the LayoutView instead: no border, just cleanly formatted
rich text.
"""

import discord
from discord import ui


def subtext(text: str) -> str:
    """Small, muted caption line — the closest V2 equivalent to an embed
    footer, using Discord's '-#' subtext markdown."""
    return f"-# {text}"


def footer_line(text: str = "Buii Analytics Core") -> str:
    return subtext(f"⚙️ {text} • System Operational")


class SimpleLayout(ui.LayoutView):
    """A borderless layout: one TextDisplay block, no Container. Use for
    any response that's purely informational (confirmations, summaries,
    leaderboards, inspection results) — nothing here needs a border."""
    def __init__(self, content: str):
        super().__init__(timeout=None)
        self.add_item(ui.TextDisplay(content))


class SimpleImageLayout(ui.LayoutView):
    """A borderless layout pairing one text block with one image — used
    for things like the growth graph, where the image already carries the
    visual weight and a border around it would just add clutter."""
    def __init__(self, content: str, file: discord.File):
        super().__init__(timeout=None)
        self.add_item(ui.TextDisplay(content))
        self.add_item(ui.MediaGallery(discord.MediaGalleryItem(file)))
        self.file = file


class Layout(ui.LayoutView):
    """Borderless layout built from an arbitrary list of top-level items —
    the general-purpose version of SimpleLayout/SimpleImageLayout for
    responses with more structure (Sections, Separators, several text
    blocks) than a single text string covers. Still no Container: nothing
    passed to this is interactive, so no border is warranted."""
    def __init__(self, *items):
        super().__init__(timeout=None)
        for item in items:
            self.add_item(item)
