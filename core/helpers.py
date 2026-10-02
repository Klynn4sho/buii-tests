"""
Formatting and rendering helpers shared across cogs: progress bars, the
Components V2 dashboard content builder, the Pillow-based dynamic music
card generator, and the matplotlib join-growth graph.
"""

import asyncio
import colorsys
import hashlib
import io
import math
from datetime import datetime, timezone

import discord
from discord import ui
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from PIL import Image, ImageDraw, ImageFont, ImageColor

from core import database
from core.config import GRAPH_BG, GRAPH_GRID, GRAPH_TEXT, GRAPH_ACCENT, GRAPH_FILL
from matplotlib.ticker import MaxNLocator
from core.components import footer_line


def make_bar(value, max_value, length=10, filled_char="🟩", empty_char="⬛"):
    if max_value <= 0:
        filled_len = 0
    else:
        filled_len = round(length * max(0, min(value, max_value)) / max_value)
    filled_len = max(0, min(length, filled_len))
    return filled_char * filled_len + empty_char * (length - filled_len)


def account_maturity_bar(age_days, cap=30, length=10):
    if age_days < 7:
        return make_bar(age_days, cap, length=length, filled_char="🟥", empty_char="⬛")
    elif age_days < 30:
        return make_bar(age_days, cap, length=length, filled_char="🟨", empty_char="⬛")
    return make_bar(age_days, cap, length=length, filled_char="🟩", empty_char="⬛")


def format_elapsed(dt: datetime) -> str:
    """Human-readable 'time since' string, e.g. '3h 12m ago'."""
    delta = datetime.now(timezone.utc) - dt
    total_seconds = int(delta.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h ago"
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    if minutes > 0:
        return f"{minutes}m ago"
    return "just now"


# ==========================================================================
# Dashboard content (Components V2)
# ==========================================================================

async def build_dashboard_content_items(guild: discord.Guild) -> list:
    """Builds the dashboard's Components V2 content — a list of ui.Item
    ready to be added into a Container by DashboardView. Kept separate
    from DashboardView itself so the view only owns interactive state
    (the buttons) while this owns what to actually display, mirroring how
    the old async_build_dashboard_embed was a pure data-to-embed function."""
    total, day_count, risk_count, codes = await database.async_compile_dashboard_stats(guild.id)

    risk_bar = make_bar(risk_count, max(total, 1), length=8, filled_char="#", empty_char="-")

    code_lines = []
    if codes:
        for idx, (code, count) in enumerate(codes, start=1):
            code_lines.append(f"**{idx:02d}.** Code `{code}` · **{count}** click{'s' if count != 1 else ''}")
        code_text = "\n".join(code_lines)
    else:
        code_text = "*No custom invite links tracked yet.*"

    header = ui.TextDisplay(
        "# LIVE GROWTH & ANALYTICS DASHBOARD\n"
        "`LIVE MONITOR` · *Auto-refreshes every 60 minutes*"
    )

    if guild.icon:
        header = ui.Section(header, accessory=ui.Thumbnail(guild.icon.url))

    stats = ui.TextDisplay(
        "**📈 Growth Overview**\n"
        f"┣ Total Joins Logged: **{total}**\n"
        f"┗ Joins (Last 24 Hours): **{day_count}**\n"
        "\n"
        "**🛡️ Security Metrics**\n"
        f"┣ Flagged Alts (<7d): **{risk_count}**\n"
        f"┣ Total Members: **{guild.member_count}**\n"
        f"┗ `{risk_bar}`"
    )

    links = ui.TextDisplay(f"**🔗 Top Performing Invite Links**\n{code_text}")

    footer = ui.TextDisplay(footer_line("Live Dashboard"))

    return [header, ui.Separator(), stats, ui.Separator(), links, ui.Separator(spacing=discord.SeparatorSpacing.small), footer]


# ==========================================================================
# Pillow growth dashboard card
# ==========================================================================


async def create_growth_dashboard_card(guild: discord.Guild, total: int, day_count: int,
                                       risk_count: int, codes: list):
    """Render the statspanel as a border-matched Pillow card."""
    W, H = 1200, 650
    bg, panel = "#0F1013", "#1B1D22"
    white, muted, accent = "#F2F3F5", "#A7ADB7", "#6574F5"
    card = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle([12, 12, W - 12, H - 12], radius=24, fill=panel, outline=accent, width=4)

    title_font = get_font(40, bold=True)
    draw.text((48, 42), _truncate_to_width(draw, f"{guild.name} Growth Dashboard", title_font, 930),
              fill=white, font=title_font)
    draw.text((48, 96), "LIVE SERVER MONITOR  ·  refreshes every 60 minutes", fill=muted, font=get_font(19))

    stats = [
        ("TOTAL JOINS", str(total)),
        ("LAST 24 HOURS", str(day_count)),
        ("NEW ACCOUNTS", str(risk_count)),
        ("SERVER MEMBERS", str(guild.member_count)),
    ]
    for (label, value), x in zip(stats, (48, 310, 572, 834)):
        draw.text((x, 170), label, fill=accent, font=get_font(15, bold=True))
        draw.text((x, 201), value, fill=white, font=get_font(30, bold=True))

    draw.text((48, 285), "TOP INVITE CODES", fill=accent, font=get_font(17, bold=True))
    y = 326
    if codes:
        for index, row in enumerate(codes[:5], start=1):
            line = f"{index:02d}. {row[0]}  ·  {row[1]} click{'s' if row[1] != 1 else ''}"
            draw.text((48, y), _truncate_to_width(draw, line, get_font(21), W - 96), fill=white, font=get_font(21))
            y += 36
    else:
        draw.text((48, y), "No custom invite links tracked yet.", fill=muted, font=get_font(20))

    draw.line((48, 545, W - 48, 545), fill="#343740", width=2)
    draw.text((48, 570), "Use the selector below for invite-code details.", fill=muted, font=get_font(18))

    buf = io.BytesIO()
    card.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ==========================================================================
# Join growth graph (matplotlib)
# ==========================================================================

async def build_joins_graph_async(guild_id, days=30):
    series = await database.async_get_daily_join_counts(guild_id, days)

    def _draw():
        W, H = 1200, 620
        bg = ImageColor.getrgb(GRAPH_BG)
        grid = ImageColor.getrgb(GRAPH_GRID)
        text_color = ImageColor.getrgb(GRAPH_TEXT)
        accent = ImageColor.getrgb(GRAPH_ACCENT)
        fill = ImageColor.getrgb(GRAPH_FILL)
        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        left, top, right, bottom = 92, 92, W - 48, H - 88
        counts = [count for _, count in series]
        max_count = max(max(counts, default=0), 1)
        plot_w, plot_h = right - left, bottom - top

        draw.text((left, 30), f"SERVER GROWTH TREND — LAST {days} DAYS",
                  fill=text_color, font=get_font(24, bold=True))
        draw.text((22, top + plot_h // 2), "NEW JOINS", fill=text_color,
                  font=get_font(16, bold=True))

        for tick in range(max_count + 1):
            y = bottom - int(plot_h * tick / max_count)
            draw.line((left, y, right, y), fill=grid, width=1)
            draw.text((left - 48, y - 10), str(tick), fill=text_color, font=get_font(15))

        points = []
        for index, (_, count) in enumerate(series):
            x = left + int(plot_w * index / max(len(series) - 1, 1))
            y = bottom - int(plot_h * count / max_count)
            points.append((x, y))

        if len(points) > 1:
            fill_points = [(points[0][0], bottom), *points, (points[-1][0], bottom)]
            draw.polygon(fill_points, fill=tuple(int((a + b) / 2) for a, b in zip(fill, bg)))
            draw.line(points, fill=accent, width=5, joint="curve")
        for x, y in points:
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=bg, outline=accent, width=3)

        label_step = max(1, len(series) // 6)
        for index in range(0, len(series), label_step):
            date = series[index][0]
            x = left + int(plot_w * index / max(len(series) - 1, 1))
            draw.text((x - 28, bottom + 18), date.strftime("%b %d"), fill=text_color, font=get_font(15))

        draw.line((left, bottom, right, bottom), fill=grid, width=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return discord.File(fp=buf, filename="joins_graph.png")

    return await asyncio.to_thread(_draw)


# ==========================================================================
# Pillow invite/join alert card
# ==========================================================================

async def create_join_card(
    session, avatar_url: str, member_name: str, username: str, user_id: int,
    inviter_name: str, inviter_id: int | None, invite_code: str,
    account_age_days: int, risk_status: str, member_count: int, accent_rgb: tuple,
):
    """Render a compact invite alert image with all raw details."""
    W, H = 1100, 660
    bg, panel = "#111214", "#1E2024"
    muted, white = "#A7ADB7", "#F2F3F5"
    accent = _rgb_to_hex(accent_rgb)
    card = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        [12, 12, W - 12, H - 12],
        radius=24,
        fill=panel,
        outline=accent,
        width=4,
    )

    avatar_size = 180
    avatar = None
    if avatar_url:
        try:
            async with session.get(avatar_url, timeout=5) as resp:
                if resp.status == 200:
                    avatar = Image.open(io.BytesIO(await resp.read())).convert("RGB").resize(
                        (avatar_size, avatar_size), Image.LANCZOS
                    )
        except Exception:
            pass
    if avatar is None:
        avatar = Image.new("RGB", (avatar_size, avatar_size), "#2B2D31")
        ImageDraw.Draw(avatar).text((avatar_size // 2 - 20, avatar_size // 2 - 25), "?", fill=muted, font=get_font(48, bold=True))

    avatar_x, avatar_y = W - 230, 42
    mask = _rounded_mask((avatar_size, avatar_size), 26)
    card.paste(avatar, (avatar_x, avatar_y), mask)
    draw.rounded_rectangle([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size], radius=26, outline=accent, width=3)

    draw.text((52, 44), "NEW MEMBER JOIN", fill=accent, font=get_font(24, bold=True))
    title_font = get_font(42, bold=True)
    draw.text((52, 86), _truncate_to_width(draw, member_name, title_font, 650), fill=white, font=title_font)
    draw.text((52, 140), f"@{username}", fill=muted, font=get_font(22))

    def section(y, label, rows):
        draw.text((52, y), label.upper(), fill=accent, font=get_font(18, bold=True))
        y += 34
        for name, value in rows:
            value_font = get_font(21)
            draw.text((52, y), f"{name}:", fill=muted, font=get_font(21, bold=True))
            draw.text((250, y), _truncate_to_width(draw, str(value), value_font, 790), fill=white, font=value_font)
            y += 34

    section(210, "USER INFORMATION", [("User ID", user_id), ("Username", username)])
    section(320, "INVITE DETAILS", [
        ("Inviter", inviter_name or "Unknown / Custom Link"),
        ("Inviter ID", inviter_id or "Unknown"),
        ("Invite Code", invite_code or "Unknown"),
    ])

    risk_y = 470
    draw.rounded_rectangle([52, risk_y, W - 52, risk_y + 138], radius=16, fill="#16181B", outline=accent, width=2)
    draw.text((76, risk_y + 16), "SECURITY", fill=accent, font=get_font(18, bold=True))

    risk_label = (
        risk_status.replace("🚨 ", "").replace("⚠️ ", "").replace("🟢 ", "")
    )
    draw.text((76, risk_y + 48), risk_label, fill=white, font=get_font(22, bold=True))

    bar_x, bar_y = 76, risk_y + 92
    bar_w, bar_h = W - 152, 14
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                           radius=bar_h // 2, fill="#2B2D31")
    progress = max(0.0, min(account_age_days / 30.0, 1.0))
    fill_w = max(bar_h, int(bar_w * progress)) if progress > 0 else 0
    if fill_w:
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h],
                               radius=bar_h // 2, fill=accent)

    age_text = f"{account_age_days} DAYS OLD"
    age_font = get_font(23, bold=True)
    draw.text((76, risk_y + 110), age_text, fill=accent, font=age_font)
    member_text = f"Server members: {member_count}"
    member_font = get_font(18)
    member_w = draw.textlength(member_text, font=member_font)
    draw.text((W - 76 - member_w, risk_y + 114), member_text, fill=muted, font=member_font)

    buf = io.BytesIO()
    card.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


async def create_invite_stats_card(
    session, avatar_url: str, display_name: str, username: str,
    total_invites: int, retained: int, left_count: int,
    flagged_count: int, recent: list,
):
    """Render a clean Pillow invite-statistics card."""
    W, H = 1100, 610
    bg, panel = "#0F1013", "#1B1D22"
    white, muted, accent = "#F2F3F5", "#A7ADB7", "#6574F5"
    card = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(card)

    draw.rounded_rectangle([12, 12, W - 12, H - 12], radius=24, fill=panel, outline=accent, width=4)

    avatar_size = 142
    avatar = None
    if avatar_url:
        try:
            async with session.get(avatar_url, timeout=5) as resp:
                if resp.status == 200:
                    avatar = Image.open(io.BytesIO(await resp.read())).convert("RGB").resize(
                        (avatar_size, avatar_size), Image.LANCZOS
                    )
        except Exception:
            pass
    if avatar is None:
        avatar = Image.new("RGB", (avatar_size, avatar_size), "#2B2D31")
        ImageDraw.Draw(avatar).text((avatar_size // 2 - 16, avatar_size // 2 - 22), "?", fill=muted, font=get_font(42, bold=True))
    card.paste(avatar, (W - 190, 38), _rounded_mask((avatar_size, avatar_size), 24))
    draw.rounded_rectangle([W - 190, 38, W - 48, 180], radius=24, outline=accent, width=3)

    title_font = get_font(38, bold=True)
    draw.text((48, 44), _truncate_to_width(draw, f"{display_name}'s Invite Stats", title_font, 720), fill=white, font=title_font)
    draw.text((48, 96), f"@{username}", fill=muted, font=get_font(21))

    retention = (retained / total_invites * 100) if total_invites else 0.0
    metrics = [
        ("TOTAL INVITES", str(total_invites)),
        ("STILL IN SERVER", f"{retained} ({retention:.0f}%)"),
        ("LEFT SINCE JOINING", str(left_count)),
        ("FLAGGED NEW ACCOUNTS", str(flagged_count)),
    ]
    x_positions = [48, 300, 570, 820]
    for (label, value), x in zip(metrics, x_positions):
        draw.text((x, 220), label, fill=accent, font=get_font(15, bold=True))
        draw.text((x, 250), value, fill=white, font=get_font(26, bold=True))

    draw.text((48, 325), "RETENTION", fill=muted, font=get_font(16, bold=True))
    bar_x, bar_y, bar_w, bar_h = 48, 356, W - 96, 16
    draw.rounded_rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], radius=8, fill="#30333B")
    if retention > 0:
        fill_w = max(bar_h, int(bar_w * min(retention, 100) / 100))
        draw.rounded_rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], radius=8, fill=accent)

    draw.text((48, 414), "RECENT INVITEES", fill=accent, font=get_font(16, bold=True))
    y = 448
    for row in recent[:4]:
        flag = "  [new]" if row["account_age_days"] < 7 else ""
        line = f"• {row['user_name']}{flag}  ·  {row['join_date']}"
        draw.text((48, y), _truncate_to_width(draw, line, get_font(18), W - 96), fill=white, font=get_font(18))
        y += 30
    if not recent:
        draw.text((48, y), "No recent invitees recorded.", fill=muted, font=get_font(18))

    buf = io.BytesIO()
    card.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf


# ==========================================================================
# Pillow dynamic music rating card
# ==========================================================================

_FONT_CACHE = {}


def get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Loads DejaVu Sans straight out of matplotlib's bundled font files so
    this renders identically on every host, instead of hoping the OS has
    arial.ttf/DejaVuSans.ttf installed."""
    key = (size, bold)
    if key not in _FONT_CACHE:
        props = fm.FontProperties(family="DejaVu Sans", weight="bold" if bold else "normal")
        path = fm.findfont(props, fallback_to_default=True)
        _FONT_CACHE[key] = ImageFont.truetype(path, size)
    return _FONT_CACHE[key]


def _truncate_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + ellipsis, font=font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ellipsis


def _rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0], size[1]], radius=radius, fill=255)
    return mask


def score_color(avg: float) -> str:
    if avg >= 7.5:
        return "#57F287"
    if avg >= 5.0:
        return "#FEE75C"
    return "#ED4245"


def _draw_star(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, fill: str):
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        radius = r if i % 2 == 0 else r * 0.42
        points.append((cx + radius * math.cos(angle), cy - radius * math.sin(angle)))
    draw.polygon(points, fill=fill)


def _extract_dominant_color(cover: "Image.Image"):
    try:
        small = cover.resize((48, 48), Image.LANCZOS).convert("RGB")
        paletted = small.quantize(colors=6, method=Image.MEDIANCUT)
        palette = paletted.getpalette()
        color_counts = sorted(paletted.getcolors(), reverse=True)
        top_index = color_counts[0][1]
        r, g, b = palette[top_index * 3: top_index * 3 + 3]

        h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
        l = min(max(l, 0.42), 0.68)
        s = max(s, 0.45)
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        return (int(r * 255), int(g * 255), int(b * 255))
    except Exception:
        return (88, 101, 242)


def _rgb_to_hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _draw_waveform(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int,
                    seed: str, color, progress: float = 1.0, bg_color="#1E1F22"):
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    bar_w = 3
    gap = 2
    n_bars = max(1, width // (bar_w + gap))

    if isinstance(color, str):
        color = ImageColor.getrgb(color)

    for i in range(n_bars):
        b = digest[i % len(digest)]
        frac = 0.25 + (b / 255.0) * 0.75
        bar_h = max(2, int(height * frac))
        bar_x = x + i * (bar_w + gap)
        bar_top = y + (height - bar_h) // 2
        bar_bottom = bar_top + bar_h

        filled = (i / n_bars) < progress
        if filled:
            draw.rounded_rectangle([bar_x, bar_top, bar_x + bar_w, bar_bottom],
                                    radius=bar_w // 2, fill=color)
        else:
            dim = tuple(int(c * 0.35 + 30 * 0.65) for c in color)
            draw.rounded_rectangle([bar_x, bar_top, bar_x + bar_w, bar_bottom],
                                    radius=bar_w // 2, fill=dim)


def _draw_genre_chip(draw: ImageDraw.ImageDraw, x: int, y: int, genre: str, font, accent_rgb) -> int:
    pad_x, pad_y = 14, 6
    text_w = draw.textlength(genre, font=font)
    chip_w = int(text_w + pad_x * 2)
    chip_h = font.size + pad_y * 2
    fill = tuple(int(c * 0.22) for c in accent_rgb)
    draw.rounded_rectangle([x, y, x + chip_w, y + chip_h], radius=chip_h // 2,
                            outline=_rgb_to_hex(accent_rgb), width=2, fill=fill)
    draw.text((x + pad_x, y + pad_y - 1), genre, fill=_rgb_to_hex(accent_rgb), font=font)
    return chip_w


async def create_music_card(session, title: str, artist: str, cover_url: str,
                             avg: float = 0.0, count: int = 0, genre: str = None, rank: int = None, song_number: int = None):
    cover_bytes = None
    if cover_url:
        try:
            async with session.get(cover_url, timeout=5) as resp:
                if resp.status == 200:
                    cover_bytes = await resp.read()
        except Exception:
            pass

    SCALE = 2
    W, H = 800 * SCALE, 300 * SCALE
    PAD = 24 * SCALE
    ART = 252 * SCALE
    text_x = PAD + ART + (24 * SCALE)
    text_right = W - PAD

    card = Image.new("RGB", (W, H), color="#2B2D31")

    cover = None
    if cover_bytes:
        try:
            cover = Image.open(io.BytesIO(cover_bytes)).convert("RGB").resize((ART, ART), Image.LANCZOS)
        except Exception:
            cover = None
    if cover is None:
        cover = Image.new("RGB", (ART, ART), "#1E1F22")
        ImageDraw.Draw(cover).line([(0, 0), (ART, ART)], fill="#3B3D44", width=4 * SCALE)
        ImageDraw.Draw(cover).line([(ART, 0), (0, ART)], fill="#3B3D44", width=4 * SCALE)

    dominant_rgb = _extract_dominant_color(cover) if cover_bytes else (88, 101, 242)

    top, bottom = (43, 45, 49), (32, 34, 37)
    tint_amt = 0.10
    top = tuple(int(top[i] * (1 - tint_amt) + dominant_rgb[i] * tint_amt) for i in range(3))
    grad = Image.new("RGB", (1, H))
    for y in range(H):
        t = y / H
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    card.paste(grad.resize((W, H)), (0, 0))

    ImageDraw.Draw(card).rectangle([0, 0, 8 * SCALE, H], fill=_rgb_to_hex(dominant_rgb))

    draw = ImageDraw.Draw(card)

    art_box = (PAD, (H - ART) // 2, PAD + ART, (H - ART) // 2 + ART)
    mask = _rounded_mask((ART, ART), radius=18 * SCALE)
    card.paste(cover, art_box[:2], mask)
    draw.rounded_rectangle(art_box, radius=18 * SCALE, outline=_rgb_to_hex(dominant_rgb), width=2 * SCALE)

    if song_number is not None:
        id_text = f"ID #{song_number}"
        id_font = get_font(16 * SCALE, bold=True)
        id_w = draw.textlength(id_text, font=id_font)
        id_x = text_right - id_w
        id_y = art_box[1] + 4 * SCALE
        draw.rounded_rectangle(
            [id_x - 10 * SCALE, id_y, text_right + 6 * SCALE, id_y + 28 * SCALE],
            radius=8 * SCALE,
            fill="#18191C",
            outline=_rgb_to_hex(dominant_rgb),
            width=1 * SCALE,
        )
        draw.text((id_x, id_y + 4 * SCALE), id_text, fill="#FFFFFF", font=id_font)

    if rank is not None:
        badge_r = 20 * SCALE
        badge_cx = art_box[0] + badge_r + 6 * SCALE
        badge_cy = art_box[1] + badge_r + 6 * SCALE
        draw.ellipse([badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r],
                     fill="#18191C", outline=_rgb_to_hex(dominant_rgb), width=2 * SCALE)
        font_rank = get_font(18 * SCALE, bold=True)
        rank_text = f"#{rank}"
        rw = draw.textlength(rank_text, font=font_rank)
        draw.text((badge_cx - rw / 2, badge_cy - 12 * SCALE), rank_text, fill="#FFFFFF", font=font_rank)

    font_title = get_font(34 * SCALE, bold=True)
    font_artist = get_font(22 * SCALE)
    font_score = get_font(34 * SCALE, bold=True)
    font_small = get_font(17 * SCALE)
    font_chip = get_font(15 * SCALE, bold=True)

    top_y = art_box[1]

    chip_w = 0
    if genre:
        chip_h = font_chip.size + 6 * SCALE * 2
        chip_y = top_y + 46 * SCALE + (font_artist.size - chip_h) // 2 + 2 * SCALE
        text_w = draw.textlength(genre.upper(), font=font_chip)
        chip_w = int(text_w + 14 * SCALE * 2) + 10 * SCALE
        chip_x = text_right - (chip_w - 10 * SCALE)
        _draw_genre_chip(draw, chip_x, chip_y, genre.upper(), font_chip, dominant_rgb)

    safe_title = _truncate_to_width(draw, title or "Unknown Title", font_title, text_right - text_x)
    safe_artist = _truncate_to_width(draw, artist or "Unknown Artist", font_artist, text_right - text_x - chip_w)

    draw.text((text_x, top_y), safe_title, fill="#FFFFFF", font=font_title)
    draw.text((text_x, top_y + 46 * SCALE), safe_artist, fill="#B5BAC1", font=font_artist)

    wave_y = top_y + 78 * SCALE
    wave_h = 20 * SCALE
    waveform_seed = f"{title or ''}|{artist or ''}"
    _draw_waveform(draw, text_x, wave_y, text_right - text_x, wave_h,
                   waveform_seed, dominant_rgb, progress=(avg / 10.0) if count > 0 else 0.0)

    bar_y = art_box[3] - 22 * SCALE
    bar_left, bar_right = text_x, text_right
    score_y = wave_y + wave_h + 14 * SCALE

    if count > 0:
        color = score_color(avg)
        _draw_star(draw, text_x + 14 * SCALE, score_y + 12 * SCALE, 14 * SCALE, color)
        draw.text((text_x + 34 * SCALE, score_y), f"{avg:.1f} / 10", fill=color, font=font_score)
        vote_label = f"{count} member rating" + ("s" if count != 1 else "")
        draw.text((text_x, score_y + 50 * SCALE), vote_label, fill="#949BA4", font=font_small)

        bar_h = 14 * SCALE
        draw.rounded_rectangle([bar_left, bar_y, bar_right, bar_y + bar_h], radius=bar_h // 2, fill="#1E1F22")
        fill_width = int((bar_right - bar_left) * max(0.0, min(avg / 10.0, 1.0)))
        if fill_width > bar_h:
            draw.rounded_rectangle([bar_left, bar_y, bar_left + fill_width, bar_y + bar_h], radius=bar_h // 2, fill=color)
    else:
        draw.text((text_x, score_y), "Unrated Track", fill="#DBDEE1", font=font_score)
        draw.text((text_x, score_y + 50 * SCALE), "Be the first to vote using the buttons below!", fill="#949BA4", font=font_small)
        bar_h = 14 * SCALE
        draw.rounded_rectangle([bar_left, bar_y, bar_right, bar_y + bar_h], radius=bar_h // 2, fill="#1E1F22")

    card = card.resize((800, 300), Image.LANCZOS)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    return buf, dominant_rgb
