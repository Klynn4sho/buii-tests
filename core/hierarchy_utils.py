"""
Role classification and image rendering for the /hierarchy staff directory
command. Kept as its own module (like core/music_utils.py is for the music
domain) since it's a self-contained feature unrelated to growth or music.

The renderer draws at 2x and downsamples at the end (same supersampling
trick as create_music_card) so rounded corners, rings and text stay smooth.
"""

import asyncio
import io
import unicodedata
from datetime import datetime, timezone

import discord
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
import matplotlib.font_manager as fm

from core.config import MODERATION_PERMISSIONS, HIERARCHY_IGNORED_ROLE_NAMES
from core.helpers import get_font

SCALE = 2
W = 1300                      # logical canvas width; real pixels = W * SCALE
NEUTRAL = (138, 143, 158)     # accent for roles that never had a color set
MUTED = (108, 110, 124)
WHITE = (240, 241, 246)


def s(v: float) -> int:
    """Logical px -> real px at the supersampled scale."""
    return int(round(v * SCALE))


def clean_display_text(value: str, fallback: str = "Unnamed") -> str:
    """Return text that the bundled DejaVu font can render reliably.

    NFKC handles decorative mathematical/script alphabets. For remaining
    characters, keep Latin/Greek/Cyrillic letters, combining marks, numbers,
    punctuation, spaces, and a small set of common ASCII symbols. Unsupported
    emoji/CJK/private-use glyphs are omitted instead of becoming square boxes.
    """
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    kept = []
    for char in normalized:
        codepoint = ord(char)
        category = unicodedata.category(char)
        supported_script = (
            codepoint <= 0x024F
            or 0x1E00 <= codepoint <= 0x1EFF
            or 0x0370 <= codepoint <= 0x052F
        )
        if supported_script and (category[0] in {"L", "N", "M", "P", "Z"}):
            kept.append(char)
        elif char in "$%&+@#=/_-":
            kept.append(char)
    cleaned = "".join(kept).strip()
    return cleaned or fallback

def is_staff_role(role: discord.Role) -> bool:
    """Check if a role has staff permissions and is not ignored/bot-managed."""
    if role.is_default() or role.is_integration() or role.is_bot_managed():
        return False

    if role.name in HIERARCHY_IGNORED_ROLE_NAMES or str(role.id) in HIERARCHY_IGNORED_ROLE_NAMES:
        return False

    return any(getattr(role.permissions, perm, False) for perm in MODERATION_PERMISSIONS)


async def fetch_image(session, url):
    """Download a dynamic image (avatar, role icon, guild icon) asynchronously."""
    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.read()
                return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        pass
    return None


async def _fetch_all(session, urls):
    """Downloads every unique URL concurrently — a big staff list used to
    fetch avatars one at a time, which is what made this command slow."""
    unique = list(dict.fromkeys(u for u in urls if u))
    results = await asyncio.gather(*(fetch_image(session, u) for u in unique))
    return dict(zip(unique, results))


def accent_for(role: discord.Role):
    return role.color.to_rgb() if role.color.value != 0 else NEUTRAL


# ---------------------------------------------------------------- drawing utils

_GLYPH_FONT_CACHE = {}


def _font_for_char(size: int, bold: bool, char: str):
    """Choose an installed font that can cover a special-character range."""
    category = unicodedata.category(char)
    codepoint = ord(char)
    if codepoint < 0x250 or (0x1E00 <= codepoint <= 0x1EFF):
        return get_font(size, bold=bold)

    families = (
        ["Noto Sans Symbols 2", "Noto Sans Symbols", "Segoe UI Symbol", "DejaVu Sans"]
        if category.startswith("S") or category.startswith("So")
        else ["Noto Sans CJK SC", "Noto Sans", "Arial Unicode MS", "DejaVu Sans"]
    )
    key = (size, bold, tuple(families))
    if key not in _GLYPH_FONT_CACHE:
        path = None
        for family in families:
            try:
                props = fm.FontProperties(family=family, weight="bold" if bold else "normal")
                path = fm.findfont(props, fallback_to_default=False)
                break
            except (ValueError, OSError, TypeError):
                continue
        if path:
            try:
                _GLYPH_FONT_CACHE[key] = ImageFont.truetype(path, size)
            except OSError:
                _GLYPH_FONT_CACHE[key] = None
        else:
            _GLYPH_FONT_CACHE[key] = None
    return _GLYPH_FONT_CACHE[key]


def _compat_chars(text: str) -> list[str]:
    # Pillow's fallback font can draw unsupported characters as square tofu
    # glyphs. Normalize decorative alphabets, then keep only characters the
    # hierarchy card intentionally supports.
    return list(clean_display_text(text, ""))


def _compat_width(draw, text: str, size: int, bold: bool) -> int:
    return sum(draw.textlength(char, font=_font_for_char(size, bold, char))
               for char in _compat_chars(text)
               if _font_for_char(size, bold, char) is not None)


def _compat_fit(draw, text: str, size: int, bold: bool, max_width: int) -> str:
    chars = _compat_chars(text)
    if _compat_width(draw, text, size, bold) <= max_width:
        return "".join(chars)
    ellipsis = "…"
    output = []
    width = 0
    for char in chars:
        font = _font_for_char(size, bold, char)
        if font is None:
            continue
        next_width = width + draw.textlength(char, font=font)
        if next_width + draw.textlength(ellipsis, font=get_font(size, bold)) > max_width:
            break
        output.append(char)
        width = next_width
    return "".join(output).rstrip() + ellipsis


def _draw_compat_text(draw, xy, text: str, size: int, bold: bool, fill, anchor="lm"):
    x, y = xy
    chars = _compat_chars(text)
    fonts = [(_font_for_char(size, bold, char), char) for char in chars]
    fonts = [(font, char) for font, char in fonts if font is not None]
    total = sum(draw.textlength(char, font=font) for font, char in fonts)
    if anchor == "mm":
        x -= total / 2
    elif anchor == "rm":
        x -= total
    for font, char in fonts:
        draw.text((x, y), char, font=font, fill=fill, anchor="lm")
        x += draw.textlength(char, font=font)


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(c, font=font) for c in text) + tracking * max(len(text) - 1, 0)


def _tracked_text(draw, x, y, text, font, fill, tracking, anchor="lm"):
    """Letter-spaced text (the wide-tracked caps in the header/footer)."""
    for c in text:
        draw.text((x, y), c, font=font, fill=fill, anchor=anchor)
        x += draw.textlength(c, font=font) + tracking
    return x


def _fit(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid] + "…", font=font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _circle(img, d):
    img = img.resize((d, d), Image.LANCZOS)
    mask = Image.new("L", (d, d), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, d - 1, d - 1], fill=255)
    out = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _hgradient(w, h, rgb, a_start, a_end):
    strip = Image.new("RGBA", (w, 1))
    px = strip.load()
    for x in range(w):
        t = x / max(w - 1, 1)
        px[x, 0] = (*rgb, int(a_start + (a_end - a_start) * t))
    return strip.resize((w, h))


def _background(w, h):
    strip = Image.new("RGBA", (1, h))
    px = strip.load()
    top, bottom = (5, 20, 24), (7, 11, 18)
    for y in range(h):
        t = y / max(h - 1, 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
    bg = strip.resize((w, h))

    # Teal glass atmosphere: a cool upper glow, a cyan lower bloom, and
    # barely-visible diagonal light bands behind the content.
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-w * 0.20, -h * 0.42, w * 0.62, h * 0.34], fill=(35, 201, 185, 115))
    gd.ellipse([w * 0.48, h * 0.62, w * 1.18, h * 1.22], fill=(28, 157, 180, 70))
    gd.polygon([(0, h * 0.42), (w * 0.62, 0), (w * 0.82, 0), (w * 0.18, h * 0.56)], fill=(75, 230, 218, 22))
    glow = glow.filter(ImageFilter.GaussianBlur(s(120)))
    return Image.alpha_composite(bg, glow)


def _row_card(w, h, accent, strength):
    """One rounded role row: dark base, accent tint fading left->right,
    accent outline and a left accent bar, all clipped to the rounded shape."""
    radius = s(16)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)

    glass = tuple(int(accent[i] * 0.38 + (45, 212, 191)[i] * 0.62) for i in range(3))
    card = Image.new("RGBA", (w, h), (14, 31, 37, 226))
    card = Image.alpha_composite(card, _hgradient(w, h, glass, int(70 * strength), 0))

    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([s(1), s(1), w - s(2), h - s(2)], radius=radius,
                          fill=(31, 56, 61, 46),
                          outline=(*glass, int(125 * strength) + 25), width=s(1))
    od.line((s(18), s(2), w - s(18), s(2)), fill=(180, 255, 247, 48), width=s(1))
    card = Image.alpha_composite(card, ov)
    card.putalpha(ImageChops.multiply(card.getchannel("A"), mask))
    return card


# ------------------------------------------------------------------- main render

async def build_hierarchy_image(guild: discord.Guild, staff_roles: list, session,
                                 requested_by: str = "") -> tuple:
    """Renders the staff-hierarchy PNG.

    Returns (png_buffer, total_unique_staff_users, vacant_role_count).
    """
    # ---- fonts (shared DejaVu loader — no Windows-only arial.ttf paths)
    f_title = get_font(s(40), bold=True)
    f_tracked_lg = get_font(s(17), bold=True)
    f_tracked_sm = get_font(s(12), bold=True)
    f_cap = get_font(s(14), bold=True)
    f_pill = get_font(s(15), bold=True)
    f_rank = get_font(s(20), bold=True)
    f_role = get_font(s(22), bold=True)
    f_sub = get_font(s(15), bold=False)
    f_name = get_font(s(17), bold=True)
    f_foot = get_font(s(14), bold=False)

    clean_guild_name = clean_display_text(guild.name, "Server")
    clean_requester = clean_display_text(requested_by, "") if requested_by else ""

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # ---- gather rows + stats
    rows, staff_ids, vacant = [], set(), 0
    for role in staff_roles:
        members = [m for m in role.members if not m.bot]
        if members:
            staff_ids.update(m.id for m in members)
        else:
            vacant += 1
        rows.append((role, members, accent_for(role)))

    # ---- layout constants (logical px)
    margin, row_h, pitch, rows_top = 64, 88, 102, 206
    row_w = W - margin * 2
    avatar_d, ring, name_gap, slot_gap = 46, 3, 12, 28
    avatar_start, avail_right = 300, row_w - 28
    overflow_w = 96

    # ---- decide how many members fit per row (measured, not guessed)
    plans = []
    for role, members, accent in rows:
        visible, x = [], avatar_start
        for i, m in enumerate(members):
            raw_name = clean_display_text(m.display_name or m.name, "Member")
            name = _compat_fit(measure, raw_name, s(17), True, s(150))
            if not name.strip("… "):
                name = clean_display_text(m.name, "Member")
            tw = _compat_width(measure, name, s(17), True) / SCALE
            w = avatar_d + name_gap + tw
            reserve = overflow_w if len(members) - i - 1 > 0 else 0
            if x + w + reserve <= avail_right:
                visible.append((m, name, x))
                x += w + slot_gap
            else:
                break
        plans.append((visible, len(members) - len(visible), x))

    # ---- download every image concurrently
    urls = []
    guild_icon_url = guild.icon.with_size(256).url if guild.icon else None
    urls.append(guild_icon_url)
    for (role, _, _), (visible, _, _) in zip(rows, plans):
        if role.icon:
            urls.append(role.icon.with_size(64).url)
        urls.extend(m.display_avatar.with_size(128).url for m, _, _ in visible)
    images = await _fetch_all(session, urls)

    # ---- canvas
    footer_y = rows_top + len(rows) * pitch - (pitch - row_h) + 34
    H = footer_y + 66
    img = _background(s(W), s(H))
    draw = ImageDraw.Draw(img)

    # ---- header: translucent bits first (pills, divider, icon ring)
    hov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(hov)
    hd.rounded_rectangle(
        [s(margin), s(34), s(W - margin), s(154)],
        radius=s(22),
        fill=(20, 48, 53, 92),
        outline=(91, 239, 222, 42),
        width=s(1),
    )

    pills = [((74, 222, 128), f"{len(staff_ids)} Staff"),
             ((129, 140, 248), f"{len(rows)} Tiers"),
             ((251, 191, 36), f"{vacant} Vacant")]
    pill_h, pill_pad, dot_d, dot_gap, pill_gap = 40, 18, 10, 10, 12
    pill_widths = [pill_pad + dot_d + dot_gap + measure.textlength(t, font=f_pill) / SCALE + pill_pad
                   for _, t in pills]
    px = W - margin - sum(pill_widths) - pill_gap * (len(pills) - 1)
    pill_cy = 84
    pill_x0 = px
    pill_positions = []
    for width in pill_widths:
        hd.rounded_rectangle([s(px), s(pill_cy - pill_h / 2), s(px + width), s(pill_cy + pill_h / 2)],
                              radius=s(pill_h / 2), fill=(30, 30, 43, 235),
                              outline=(255, 255, 255, 30), width=s(1.2))
        pill_positions.append(px)
        px += width + pill_gap

    hd.rectangle([s(margin + 20), s(176), s(W - margin - 20), s(176) + max(1, s(1))], fill=(104, 238, 224, 42))
    icon_d, icon_x, icon_y = 88, margin, 52
    hd.ellipse([s(icon_x - 3), s(icon_y - 3), s(icon_x + icon_d + 3), s(icon_y + icon_d + 3)],
               outline=(255, 255, 255, 70), width=s(1.5))
    img.alpha_composite(hov)

    # ---- header: opaque content
    icon_img = images.get(guild_icon_url)
    if icon_img:
        img.alpha_composite(_circle(icon_img, s(icon_d)), dest=(s(icon_x), s(icon_y)))
    else:
        ph = Image.new("RGBA", (s(icon_d), s(icon_d)), (88, 101, 242, 255))
        img.alpha_composite(_circle(ph, s(icon_d)), dest=(s(icon_x), s(icon_y)))
        draw.text((s(icon_x + icon_d / 2), s(icon_y + icon_d / 2)), (clean_guild_name[:1] or "?").upper(),
                  font=f_title, fill=(225, 255, 251), anchor="mm")

    title_x = icon_x + icon_d + 24
    title_max = (pill_x0 - 330) - title_x        # leave room for the right-hand title
    title_text = _compat_fit(
        measure,
        clean_guild_name,
        s(40),
        True,
        s(max(title_max, 200)),
    )
    _draw_compat_text(draw, (s(title_x), s(88)), title_text, s(40), True, (226, 255, 251), anchor="lm")
    _tracked_text(draw, s(title_x), s(126), "STAFF DIRECTORY", f_cap, (116, 177, 180), s(4))

    for (rgb, text), x0, width in zip(pills, pill_positions, pill_widths):
        draw.ellipse([s(x0 + pill_pad), s(pill_cy - dot_d / 2), s(x0 + pill_pad + dot_d), s(pill_cy + dot_d / 2)],
                     fill=rgb)
        draw.text((s(x0 + pill_pad + dot_d + dot_gap), s(pill_cy)), text, font=f_pill,
                  fill=(236, 237, 243), anchor="lm")

    heading = "SERVER HIERARCHY"
    heading_w = _tracked_width(measure, heading, f_tracked_lg, s(5)) / SCALE
    _tracked_text(draw, s(pill_x0 - 28 - heading_w), s(pill_cy), heading, f_tracked_lg, (197, 246, 240), s(5))

    tagline = "ROLES  ·  PEOPLE  ·  STRUCTURE"
    tag_w = _tracked_width(measure, tagline, f_tracked_sm, s(4)) / SCALE
    _tracked_text(draw, s(W - margin - tag_w), s(128), tagline, f_tracked_sm, (86, 88, 100), s(4))

    # ---- role rows
    for i, ((role, members, accent), (visible, overflow, end_x)) in enumerate(zip(rows, plans)):
        y = rows_top + i * pitch
        strength = 1.0 if role.color.value != 0 else 0.45
        if i == 0 and role.color.value != 0:
            strength = 1.15
        img.alpha_composite(_row_card(s(row_w), s(row_h), accent, strength), dest=(s(margin), s(y)))

        cy = y + row_h / 2
        rx = margin  # row-local x -> canvas x helper below
        draw.text((s(rx + 30), s(cy)), f"{i + 1:02d}", font=f_rank, fill=accent, anchor="lm")

        name_x = rx + 80
        if role.icon:
            role_icon = images.get(role.icon.with_size(64).url)
            if role_icon:
                img.alpha_composite(role_icon.resize((s(26), s(26)), Image.LANCZOS),
                                    dest=(s(name_x), s(y + 18)))
                name_x += 34
        raw_role_name = clean_display_text(role.name, "Unnamed role")
        role_text = _compat_fit(measure, raw_role_name, s(22), True, s(170 - (name_x - (rx + 80))))
        if not role_text.strip("… "):
            role_text = clean_display_text(role.name, "Unnamed role")
        _draw_compat_text(draw, (s(name_x), s(y + 32)), role_text, s(22), True, (255, 255, 255), anchor="lm")

        if members:
            sub = f"{len(members)} member" + ("s" if len(members) != 1 else "")
            sub_color = tuple(int(c * 0.85 + 30 * 0.15) for c in accent) if role.color.value != 0 else (140, 143, 158)
        else:
            sub, sub_color = "Vacant", (255, 95, 95)
        draw.text((s(rx + 80), s(y + 62)), sub, font=f_sub, fill=sub_color, anchor="lm")

        for m, name, x in visible:
            ax = rx + x
            draw.ellipse([s(ax), s(cy - avatar_d / 2), s(ax + avatar_d), s(cy + avatar_d / 2)], fill=accent)
            inner = avatar_d - ring * 2
            av = images.get(m.display_avatar.with_size(128).url)
            if av is None:
                av = Image.new("RGBA", (16, 16), (60, 62, 74, 255))
            img.alpha_composite(_circle(av, s(inner)), dest=(s(ax + ring), s(cy - inner / 2)))
            _draw_compat_text(draw, (s(ax + avatar_d + name_gap), s(cy)), name, s(17), True, (245, 245, 250), anchor="lm")

        if overflow > 0:
            ox = rx + end_x
            label = f"+{overflow} more"
            lw = measure.textlength(label, font=f_pill) / SCALE
            pill = Image.new("RGBA", (s(lw + 28), s(32)), (0, 0, 0, 0))
            ImageDraw.Draw(pill).rounded_rectangle([0, 0, s(lw + 28) - 1, s(32) - 1], radius=s(16),
                                                    fill=(38, 40, 56, 255), outline=(*accent, 150), width=s(1.2))
            img.alpha_composite(pill, dest=(s(ox), s(cy - 16)))
            draw.text((s(ox + 14), s(cy)), label, font=f_pill, fill=(0, 255, 170), anchor="lm")

    # ---- footer
    fov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(fov).rectangle([s(margin), s(footer_y - 22), s(W - margin), s(footer_y - 22) + max(1, s(1))],
                                   fill=(255, 255, 255, 20))
    img.alpha_composite(fov)
    left = "Highest role first" + (f"   ·   {clean_requester}" if clean_requester else "")
    draw.text((s(margin), s(footer_y + 6)), left, font=f_foot, fill=MUTED, anchor="lm")
    draw.text((s(W - margin), s(footer_y + 6)), datetime.now(timezone.utc).strftime("%d %b %Y"),
              font=f_foot, fill=MUTED, anchor="rm")

    out = img.convert("RGB").resize((W, H), Image.LANCZOS)
    buffer = io.BytesIO()
    out.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer, len(staff_ids), vacant
