"""
Role classification and image rendering for the /hierarchy staff directory
command. Kept as its own module (like core/music_utils.py is for the music
domain) since it's a self-contained feature unrelated to growth or music.
"""

import io

import discord
from PIL import Image, ImageDraw

from core.config import MODERATION_PERMISSIONS, HIERARCHY_IGNORED_ROLE_NAMES
from core.helpers import get_font


def is_staff_role(role: discord.Role) -> bool:
    """Check if a role has staff permissions and is not ignored/bot-managed."""
    if role.is_default() or role.is_integration() or role.is_bot_managed():
        return False

    if role.name in HIERARCHY_IGNORED_ROLE_NAMES or str(role.id) in HIERARCHY_IGNORED_ROLE_NAMES:
        return False

    return any(getattr(role.permissions, perm, False) for perm in MODERATION_PERMISSIONS)


async def fetch_image(session, url):
    """Download a dynamic image (avatar or role icon) asynchronously."""
    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.read()
                return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        pass
    return None


def create_circular_avatar(image, size):
    """Crop an avatar image into a smooth circle."""
    image = image.resize((size, size), Image.Resampling.LANCZOS)
    mask = Image.new("L", (size * 2, size * 2), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size * 2, size * 2), fill=255)
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)

    circular = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    circular.paste(image, (0, 0), mask)
    return circular


def hex_or_default(role_color: discord.Color):
    """Return role color RGB tuple or default purple accent."""
    if role_color.value != 0:
        return role_color.to_rgb()
    return (138, 43, 226)  # default fallback purple accent


async def build_hierarchy_image(staff_roles: list[discord.Role], session) -> tuple[io.BytesIO, int, int]:
    """Renders the full staff-hierarchy PNG.

    Returns (png_buffer, total_unique_staff_users, vacant_role_count).
    """
    canvas_width = 850
    row_height = 70
    canvas_height = 150 + (len(staff_roles) * row_height)

    img = Image.new("RGBA", (canvas_width, canvas_height), (15, 16, 21, 255))
    draw = ImageDraw.Draw(img)

    # Reuses the bot's shared DejaVu Sans font loader (core.helpers.get_font)
    # instead of hardcoded "arial.ttf"/"arialbd.ttf" paths, which only exist
    # on Windows and would otherwise silently fall back to Pillow's tiny
    # built-in bitmap font on a Linux host — the exact problem
    # create_music_card already solves the same way.
    title_font = get_font(22, bold=False)
    subtitle_font = get_font(13, bold=False)
    rank_font = get_font(16, bold=True)
    role_font = get_font(15, bold=True)
    text_font = get_font(13, bold=False)
    badge_font = get_font(12, bold=True)

    draw.text((40, 35), "SERVER HIERARCHY", fill=(255, 255, 255), font=title_font)
    draw.text((40, 65), "STAFF DIRECTORY", fill=(142, 146, 151), font=subtitle_font)

    start_y = 100
    total_staff_users = set()
    vacant_count = 0

    for i, role in enumerate(staff_roles):
        current_y = start_y + (i * row_height)
        members = [m for m in role.members if not m.bot]
        accent_color = hex_or_default(role.color)

        if members:
            for m in members:
                total_staff_users.add(m.id)
        else:
            vacant_count += 1

        # Main row box container
        draw.rounded_rectangle(
            [30, current_y, canvas_width - 30, current_y + 55],
            radius=10,
            fill=(24, 25, 32, 255)
        )

        # Left role-color accent bar
        draw.rounded_rectangle(
            [30, current_y, 36, current_y + 55],
            radius=4,
            fill=accent_color
        )

        # Rank number (01, 02...)
        rank_text = f"{i + 1:02d}"
        draw.text((50, current_y + 18), rank_text, fill=accent_color, font=rank_font)

        # Role icon (if the server has one for this role)
        role_name_x = 90
        if role.icon:
            icon_img = await fetch_image(session, role.icon.url)
            if icon_img:
                icon_img = icon_img.resize((20, 20), Image.Resampling.LANCZOS)
                img.paste(icon_img, (90, current_y + 12), icon_img)
                role_name_x = 118

        # Role name & subtext
        draw.text((role_name_x, current_y + 12), role.name, fill=(255, 255, 255), font=role_font)

        subtext = f"{len(members)} members" if members else "Vacant"
        subtext_color = (142, 146, 151) if members else (255, 85, 85)
        draw.text((90, current_y + 32), subtext, fill=subtext_color, font=subtitle_font)

        # Member avatars & overflow counter (+X more)
        avatar_x = 280
        avatar_size = 32
        max_visible = 4

        visible_members = members[:max_visible]
        overflow = len(members) - max_visible

        for member in visible_members:
            avatar_img = await fetch_image(session, member.display_avatar.with_size(64).url)
            if avatar_img:
                circle_avatar = create_circular_avatar(avatar_img, avatar_size)
                img.paste(circle_avatar, (avatar_x, current_y + 11), circle_avatar)

            display_name = member.display_name[:10]
            draw.text((avatar_x + 38, current_y + 20), display_name, fill=(220, 221, 222), font=text_font)
            avatar_x += 120

        if overflow > 0:
            badge_box = [avatar_x, current_y + 14, avatar_x + 65, current_y + 38]
            draw.rounded_rectangle(badge_box, radius=12, fill=(40, 43, 58, 255))
            draw.text((avatar_x + 12, current_y + 18), f"+{overflow} more", fill=(0, 255, 170), font=badge_font)

    # Top-right stat badge
    badge_x = canvas_width - 340
    stats_text = f"• {len(total_staff_users)} Staff   • {len(staff_roles)} Tiers   • {vacant_count} Vacant"

    draw.rounded_rectangle([badge_x, 35, canvas_width - 30, 65], radius=6, fill=(30, 31, 41, 255))
    draw.text((badge_x + 15, 42), stats_text, fill=(0, 255, 170), font=text_font)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer, len(total_staff_users), vacant_count
