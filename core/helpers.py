"""
Formatting and rendering helpers shared across cogs: progress bars, embed
footers, the Pillow-based dynamic music card generator, the live dashboard
embed builder, and the matplotlib join-growth graph.
"""

import asyncio
import colorsys
import hashlib
import io
import math
from datetime import datetime, timezone

import discord
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from PIL import Image, ImageDraw, ImageFont, ImageColor

from core import database
from core.config import GRAPH_BG, GRAPH_GRID, GRAPH_TEXT, GRAPH_ACCENT, GRAPH_FILL, COLOR_BRAND


# ==========================================================================
# Text / embed helpers
# ==========================================================================

def themed_footer(embed: discord.Embed, bot: discord.Client, text: str = "Buii Analytics Core") -> discord.Embed:
    icon_url = bot.user.display_avatar.url if bot.user else None
    embed.set_footer(text=f"⚙️ {text} • System Operational", icon_url=icon_url)
    return embed


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
# Dashboard embed
# ==========================================================================

async def async_build_dashboard_embed(guild: discord.Guild, bot: discord.Client) -> discord.Embed:
    total, day_count, risk_count, codes = await database.async_compile_dashboard_stats(guild.id)

    embed = discord.Embed(
        title="📊 LIVE GROWTH & ANALYTICS DASHBOARD",
        description="`⚡ LIVE MONITOR` • *Auto-refreshes every 60 minutes*",
        color=COLOR_BRAND,
        timestamp=datetime.now(timezone.utc)
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(
        name="📈 Growth Overview",
        value=(
            f"┣ Total Joins Logged: **{total}**\n"
            f"┗ Joins (Last 24 Hours): **{day_count}**"
        ),
        inline=True
    )

    risk_bar = make_bar(risk_count, max(total, 1), length=8, filled_char="🟥", empty_char="⬛")
    embed.add_field(
        name="🛡️ Security Metrics",
        value=(
            f"┣ Flagged Alts (<7d): **{risk_count}**\n"
            f"┣ Total Members: **{guild.member_count}**\n"
            f"┗ `{risk_bar}`"
        ),
        inline=True
    )

    code_lines = []
    if codes:
        medals = ["🥇", "🥈", "🥉"]
        for idx, (code, count) in enumerate(codes):
            medal = medals[idx] if idx < 3 else "🔹"
            code_lines.append(f"{medal} Code `{code}` — **{count} clicks**")
        code_text = "\n".join(code_lines)
    else:
        code_text = "*No custom invite links tracked yet.*"

    embed.add_field(name="🔗 Top Performing Invite Links", value=code_text, inline=False)
    themed_footer(embed, bot, "Live Dashboard")
    return embed


# ==========================================================================
# Join growth graph (matplotlib)
# ==========================================================================

async def build_joins_graph_async(guild_id, days=30):
    series = await database.async_get_daily_join_counts(guild_id, days)

    def _draw():
        dates = [d for d, _ in series]
        counts = [c for _, c in series]

        fig, ax = plt.subplots(figsize=(8.5, 4.2), dpi=180)
        fig.patch.set_facecolor(GRAPH_BG)
        ax.set_facecolor(GRAPH_BG)

        ax.plot(dates, counts, color=GRAPH_ACCENT, linewidth=2.8, marker="o",
                markersize=5, markerfacecolor="#FFFFFF", markeredgecolor=GRAPH_ACCENT, markeredgewidth=2)
        ax.fill_between(dates, counts, color=GRAPH_FILL, alpha=0.18)

        ax.set_title(f"SERVER GROWTH TREND — LAST {days} DAYS", fontsize=11, fontweight="bold", color=GRAPH_TEXT, pad=14, loc="left")
        ax.set_ylabel("NEW JOINS", color=GRAPH_TEXT, fontsize=9, fontweight="bold")
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))

        ax.tick_params(colors=GRAPH_TEXT, labelsize=8.5)
        ax.grid(axis="y", color=GRAPH_GRID, linestyle="--", alpha=0.5, linewidth=0.7)
        ax.set_axisbelow(True)

        for spine_name, spine in ax.spines.items():
            if spine_name == "bottom":
                spine.set_color(GRAPH_GRID)
                spine.set_linewidth(1)
            else:
                spine.set_visible(False)

        ax.set_ylim(bottom=0)
        fig.autofmt_xdate()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=GRAPH_BG, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return discord.File(fp=buf, filename="joins_graph.png")

    return await asyncio.to_thread(_draw)


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


def _score_color(avg: float) -> str:
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
                             avg: float = 0.0, count: int = 0, genre: str = None, rank: int = None):
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
        color = _score_color(avg)
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
