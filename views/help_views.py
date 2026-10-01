"""
Components V2 help menu, built section by section inside one Container.

Pages:
  - Home: greeting, how to use the menu, category list, dashboard setup.
  - Category: that category's commands, PAGE_SIZE per page.
  - Search: commands matching a name typed into the Search modal.

Controls (all inside the container): a category select; Home / prev / next /
Search / Close buttons; and Dashboard / Privacy / Terms link buttons.

The command list is built live from the bot (prefix commands + the slash
tree, de-duplicated by name), so a newly added command appears here
without touching this file. Only the person who ran help can use the menu.
"""

import functools
import math
from dataclasses import dataclass, field

import discord
from discord import ui
from discord.ext import commands

from core.components import footer_line, notice

DASHBOARD_URL = "https://buii-r7sg.onrender.com/"
LEGAL_URL = "https://klynn4sho.github.io/buii-legal"
PRIVACY_URL = f"{LEGAL_URL}#privacy"
TERMS_URL = f"{LEGAL_URL}#terms"

PAGE_SIZE = 6
MAX_SEARCH_RESULTS = 25


@dataclass(frozen=True)
class Category:
    key: str
    emoji: str
    label: str
    cog: str
    blurb: str


# Order here is the display order. A cog not listed falls into OTHER, so a
# newly added cog never silently vanishes from help.
CATEGORIES = (
    Category("music", "♬", "Music", "MusicCog", "rate tracks, leaderboards & Spotify sync"),
    Category("growth", "☍", "Invites & Growth", "GrowthCog", "invite tracking, join alerts & analytics"),
    Category("staff", "ⓘ", "Staff Directory", "HierarchyCog", "role hierarchy image"),
    Category("admin", "🛠", "Admin & Tools", "AdminCog", "help & slash-command sync"),
)
OTHER = Category("other", "☰", "Other", "", "everything else")


@dataclass
class HelpEntry:
    name: str
    description: str
    category: str
    usage: str = ""
    aliases: list = field(default_factory=list)
    prefix: bool = False
    slash: bool = False


def build_catalog(bot: commands.Bot) -> dict[str, list[HelpEntry]]:
    """Merges prefix commands and slash-tree commands by name into one
    entry each, grouped by category key."""
    cog_to_key = {c.cog: c.key for c in CATEGORIES}
    entries: dict[str, HelpEntry] = {}

    for cmd in bot.commands:
        if cmd.hidden:
            continue
        entries[cmd.name] = HelpEntry(
            name=cmd.name,
            description=cmd.description or cmd.short_doc or "No description.",
            category=cog_to_key.get(cmd.cog_name, OTHER.key),
            usage=cmd.signature or "",
            aliases=list(cmd.aliases),
            prefix=True,
            slash=isinstance(cmd, commands.HybridCommand),
        )

    for app_cmd in bot.tree.get_commands():
        existing = entries.get(app_cmd.name)
        if existing:
            existing.slash = True
            continue
        binding = getattr(app_cmd, "binding", None)
        params = " ".join(
            f"<{p.name}>" if p.required else f"[{p.name}]"
            for p in getattr(app_cmd, "parameters", [])
        )
        entries[app_cmd.name] = HelpEntry(
            name=app_cmd.name,
            description=getattr(app_cmd, "description", None) or "No description.",
            category=cog_to_key.get(binding.qualified_name if binding else None, OTHER.key),
            usage=params,
            prefix=False,
            slash=True,
        )

    grouped: dict[str, list[HelpEntry]] = {}
    for entry in sorted(entries.values(), key=lambda e: e.name):
        grouped.setdefault(entry.category, []).append(entry)
    return grouped


def search_entries(catalog: dict[str, list[HelpEntry]], query: str) -> list[HelpEntry]:
    q = query.strip().lstrip("/").lower()
    if not q:
        return []
    scored = []
    for entries in catalog.values():
        for e in entries:
            names = [n.lower() for n in [e.name, *e.aliases]]
            if q in names:
                score = 0
            elif any(n.startswith(q) for n in names):
                score = 1
            elif any(q in n for n in names):
                score = 2
            elif q in e.description.lower():
                score = 3
            else:
                continue
            scored.append((score, e.name, e))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [e for _, _, e in scored][:MAX_SEARCH_RESULTS]


def format_entry(e: HelpEntry, prefix: str) -> str:
    sig = f" {e.usage}" if e.usage else ""
    forms = []
    if e.prefix:
        forms.append(f"`{prefix}{e.name}{sig}`")
    if e.slash:
        forms.append(f"`/{e.name}{sig}`")
    text = f"**{' · '.join(forms)}**\n{e.description}"
    if e.aliases:
        text += "\n" + f"-# Aliases: {', '.join(f'`{prefix}{a}`' for a in e.aliases)}"
    return text


def _guard(fn):
    """Wraps a help callback so an unexpected error becomes a boxed notice
    for the user instead of a silent 'interaction failed'."""
    @functools.wraps(fn)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        try:
            return await fn(self, interaction, *args, **kwargs)
        except Exception as e:
            print(f"[help] {fn.__name__} failed: {e!r}")
            try:
                message = notice("❌ Something went wrong with the help menu — try running help again.")
                if interaction.response.is_done():
                    await interaction.followup.send(view=message, ephemeral=True)
                else:
                    await interaction.response.send_message(view=message, ephemeral=True)
            except Exception:
                pass
    return wrapper


class SearchModal(ui.Modal, title="Search commands"):
    query = ui.TextInput(label="Command name", placeholder="e.g. leaderboard", min_length=1, max_length=40)

    def __init__(self, help_view: "HelpView"):
        super().__init__()
        self.help_view = help_view

    async def on_submit(self, interaction: discord.Interaction):
        hv = self.help_view
        q = self.query.value.strip()
        if q.lower().startswith(hv.prefix.lower()):
            q = q[len(hv.prefix):]
        hv.mode = "search"
        hv.category = None
        hv.query = q
        hv.results = search_entries(hv.catalog, q)
        hv.page = 0
        hv._render()
        await interaction.response.edit_message(view=hv)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[help] search modal failed: {error!r}")
        try:
            message = notice("❌ Couldn't run that search.")
            if interaction.response.is_done():
                await interaction.followup.send(view=message, ephemeral=True)
            else:
                await interaction.response.send_message(view=message, ephemeral=True)
        except Exception:
            pass


class HelpView(ui.LayoutView):
    def __init__(self, bot: commands.Bot, invoker: discord.abc.User, prefix: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.invoker = invoker
        self.prefix = prefix
        self.catalog = build_catalog(bot)
        self.categories = [c for c in (*CATEGORIES, OTHER) if self.catalog.get(c.key)]

        self.mode = "home"          # "home" | "category" | "search"
        self.category: str | None = None
        self.query = ""
        self.results: list[HelpEntry] = []
        self.page = 0

        self.sent_message: discord.Message | None = None  # set by the caller after sending
        self._interactive: list = []  # buttons/select disabled on timeout
        self._render()

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _category(self, key: str) -> Category:
        return next((c for c in self.categories if c.key == key), OTHER)

    def _entries(self) -> list[HelpEntry]:
        if self.mode == "category":
            return self.catalog.get(self.category, [])
        if self.mode == "search":
            return self.results
        return []

    def _page_count(self) -> int:
        return max(1, math.ceil(len(self._entries()) / PAGE_SIZE))

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _header(self, title: str, crumb: str):
        text = ui.TextDisplay(f"# {title}\n-# {crumb}")
        if self.bot.user:
            return ui.Section(text, accessory=ui.Thumbnail(media=self.bot.user.display_avatar.url))
        return text

    def _small_sep(self):
        return ui.Separator(spacing=discord.SeparatorSpacing.small)

    def _home_items(self) -> list:
        name = discord.utils.escape_markdown(self.invoker.display_name)
        total = sum(len(v) for v in self.catalog.values())

        greeting = (
            f"### **Hey {name} 👋**\n"
            f"I'm **Buii** — invite tracking, growth analytics and music ratings in one bot.\n"
            f"Run commands with the prefix `{self.prefix}` or slash `/`."
        )
        how_to = (
            "### **How to use this menu**\n"
            "🗁 **Category menu** below — pick a category to browse its commands\n"
            "🔍︎ **Search** button — jump straight to any command by name\n"
            "⫘ **Link buttons** — dashboard, privacy policy & terms"
            "☍ **Link buttons** — dashboard, privacy policy & terms"
        )
        browse_lines = [
            f"- **{c.label}** — {len(self.catalog[c.key])} command"
            f"{'s' if len(self.catalog[c.key]) != 1 else ''} · {c.blurb}"
            for c in self.categories
        ]
        browse = f"### **Browse by category** ({total} commands)\n" + "\n".join(browse_lines)
        setup = (
            "### **Set it up in clicks**\n"
            f"🌏︎ **[Open the dashboard]({DASHBOARD_URL})** — configure the bot visually, no commands needed.\n"
            "-# Join log channel · Alert & mod roles · Prefix · Music channel, role & lock"
        )

        return [
            self._header("🕮 Buii — Help", f"Home · for {name}"),
            ui.Separator(),
            ui.TextDisplay(greeting),
            self._small_sep(),
            ui.TextDisplay(how_to),
            self._small_sep(),
            ui.TextDisplay(browse),
            self._small_sep(),
            ui.TextDisplay(setup),
        ]

    def _list_items(self) -> list:
        entries = self._entries()
        pages = self._page_count()
        self.page = max(0, min(self.page, pages - 1))
        chunk = entries[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]
        page_label = f" · page {self.page + 1}/{pages}" if pages > 1 else ""

        if self.mode == "category":
            cat = self._category(self.category)
            title = f"{cat.emoji} {cat.label}"
            crumb = f"Home › {cat.label}{page_label} · {len(entries)} commands"
            empty = "*No commands in this category.*"
        else:
            shown = discord.utils.escape_markdown(self.query[:40])
            title = "🔍︎ Search"
            crumb = f"Home › Search “{shown}”{page_label} · {len(entries)} result{'s' if len(entries) != 1 else ''}"
            empty = f"*No commands matched **{shown}**. Try a shorter name.*"

        body = "\n\n".join(format_entry(e, self.prefix) for e in chunk) if chunk else empty
        return [
            self._header(title, crumb),
            ui.Separator(),
            ui.TextDisplay(body),
        ]

    def _category_select(self) -> ui.Select:
        options = [
            discord.SelectOption(
                label=c.label, value=c.key, emoji=c.emoji,
                description=f"{len(self.catalog[c.key])} command{'s' if len(self.catalog[c.key]) != 1 else ''}",
                default=(self.mode == "category" and self.category == c.key),
            )
            for c in self.categories
        ]
        select = ui.Select(placeholder="Select a category…", options=options)

        async def on_pick(interaction: discord.Interaction):
            await self._pick(interaction, select.values[0])

        select.callback = on_pick
        return select

    def _controls(self) -> list:
        in_list = self.mode != "home"
        pages = self._page_count()

        home = ui.Button(label="🏠︎ Home", style=discord.ButtonStyle.secondary, disabled=not in_list)
        prev = ui.Button(label="◄", style=discord.ButtonStyle.secondary, disabled=(not in_list or self.page <= 0))
        nxt = ui.Button(label="►", style=discord.ButtonStyle.secondary, disabled=(not in_list or self.page >= pages - 1))
        search = ui.Button(label="🔍︎ Search", style=discord.ButtonStyle.primary)
        close = ui.Button(label="✕ Close", style=discord.ButtonStyle.danger)

        home.callback = self.on_home
        prev.callback = self.on_prev
        nxt.callback = self.on_next
        search.callback = self.on_search
        close.callback = self.on_close
        return [home, prev, nxt, search, close]

    def _links(self) -> list:
        return [
            ui.Button(label="Dashboard", style=discord.ButtonStyle.link, url=DASHBOARD_URL),
            ui.Button(label="Privacy", style=discord.ButtonStyle.link, url=PRIVACY_URL),
            ui.Button(label="Terms", style=discord.ButtonStyle.link, url=TERMS_URL),
        ]

    def _render(self):
        self.clear_items()
        content = self._home_items() if self.mode == "home" else self._list_items()
        select = self._category_select()
        controls = self._controls()
        self._interactive = [select, *controls]
        self.add_item(ui.Container(
            *content,
            ui.Separator(),
            ui.ActionRow(*self._links()),
            ui.Separator(),
            ui.ActionRow(select),
            ui.ActionRow(*controls),
        ))

    async def _refresh(self, interaction: discord.Interaction):
        self._render()
        await interaction.response.edit_message(view=self)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    @_guard
    async def _pick(self, interaction: discord.Interaction, key: str):
        self.mode = "category"
        self.category = key
        self.page = 0
        await self._refresh(interaction)

    @_guard
    async def on_home(self, interaction: discord.Interaction):
        self.mode = "home"
        self.category = None
        self.page = 0
        await self._refresh(interaction)

    @_guard
    async def on_prev(self, interaction: discord.Interaction):
        self.page -= 1
        await self._refresh(interaction)

    @_guard
    async def on_next(self, interaction: discord.Interaction):
        self.page += 1
        await self._refresh(interaction)

    @_guard
    async def on_search(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SearchModal(self))

    @_guard
    async def on_close(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except Exception:
            try:
                await interaction.message.delete()
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker.id:
            await interaction.response.send_message(
                view=notice("❌ This help menu belongs to someone else — run the help command to get your own."),
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self):
        for item in self._interactive:
            item.disabled = True
        if self.sent_message is not None:
            try:
                await self.sent_message.edit(view=self)
            except Exception:
                pass