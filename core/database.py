"""
Postgres domain/query layer, shared by every cog.

Invite-tracking and music SQL intentionally share one public facade so the
existing cogs stay simple. Connection-pool lifecycle now lives separately in
core/db/pool.py, which keeps infrastructure concerns out of this large domain
module without breaking the existing database API.
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta

from psycopg2.extras import RealDictCursor

from core.config import DEFAULT_PREFIX

# Connection infrastructure lives in core.db.pool; these aliases preserve the
# existing core.database API used throughout the bot.
from core.db import pool as _db_pool

db_pool = None


def init_db_pool():
    global db_pool
    _db_pool.init_db_pool()
    db_pool = _db_pool.db_pool


def get_db_conn():
    global db_pool
    conn = _db_pool.get_db_conn()
    db_pool = _db_pool.db_pool
    return conn


def release_db_conn(conn):
    _db_pool.release_db_conn(conn)


def close_db_pool():
    global db_pool
    _db_pool.close_db_pool()
    db_pool = None



# In-memory prefix cache so async_get_prefix doesn't hit the DB on every
# message. Populated lazily and kept in sync by async_set_prefix.
guild_prefix_cache = {}


def _raw_check_db_health():
    return _db_pool.check_db_health()


async def async_check_db_health():
    return await asyncio.to_thread(_raw_check_db_health)


# ==========================================================================
# Schema setup
# ==========================================================================

def _raw_init_db():
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS joins (
                guild_id TEXT,
                user_id TEXT,
                user_name TEXT,
                inviter_name TEXT,
                invite_code TEXT,
                join_date TEXT,
                account_age_days INTEGER,
                inviter_id TEXT,
                invite_uses INTEGER,
                invite_source TEXT DEFAULT 'invite',
                PRIMARY KEY (guild_id, user_id, join_date)
            );
        ''')
        cursor.execute("ALTER TABLE joins ADD COLUMN IF NOT EXISTS inviter_id TEXT;")
        cursor.execute("ALTER TABLE joins ADD COLUMN IF NOT EXISTS invite_uses INTEGER;")
        cursor.execute("ALTER TABLE joins ADD COLUMN IF NOT EXISTS invite_source TEXT DEFAULT 'invite';")
        cursor.execute("CREATE INDEX IF NOT EXISTS joins_guild_date_idx ON joins (guild_id, join_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS joins_inviter_id_idx ON joins (guild_id, inviter_id);")
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS leaves (
                guild_id TEXT,
                user_id TEXT,
                leave_date TEXT
            );
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS member_snapshots (
                guild_id BIGINT NOT NULL,
                user_id BIGINT NOT NULL,
                username TEXT,
                display_name TEXT,
                avatar_url TEXT,
                created_at TIMESTAMPTZ,
                joined_at TIMESTAMPTZ,
                first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_left_at TIMESTAMPTZ,
                is_member BOOLEAN DEFAULT TRUE,
                boosting BOOLEAN DEFAULT FALSE,
                roles_json TEXT,
                badges_json TEXT,
                PRIMARY KEY (guild_id, user_id)
            );
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS member_snapshots_guild_status_idx
            ON member_snapshots (guild_id, is_member);
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS config (
                guild_id TEXT PRIMARY KEY,
                log_channel_id TEXT,
                panel_channel_id TEXT,
                panel_message_id TEXT,
                prefix TEXT,
                music_role_id TEXT,
                music_lock_time INTEGER,
                music_channel_id TEXT,
                alert_role_id TEXT,
                mod_role_id TEXT
            );
        ''')
        cursor.execute('ALTER TABLE config ADD COLUMN IF NOT EXISTS prefix TEXT;')
        cursor.execute('ALTER TABLE config ADD COLUMN IF NOT EXISTS music_role_id TEXT;')
        cursor.execute('ALTER TABLE config ADD COLUMN IF NOT EXISTS music_lock_time INTEGER;')
        cursor.execute('ALTER TABLE config ADD COLUMN IF NOT EXISTS music_channel_id TEXT;')
        cursor.execute('ALTER TABLE config ADD COLUMN IF NOT EXISTS alert_role_id TEXT;')
        cursor.execute('ALTER TABLE config ADD COLUMN IF NOT EXISTS mod_role_id TEXT;')
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def init_db():
    await asyncio.to_thread(_raw_init_db)


def _raw_init_music_db():
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS songs (
                id SERIAL PRIMARY KEY,
                guild_id BIGINT, channel_id BIGINT, message_id BIGINT,
                title TEXT, artist TEXT, source TEXT, url TEXT, cover_url TEXT,
                preview_url TEXT, requested_by_id BIGINT, requested_by_name TEXT,
                synced INTEGER DEFAULT 0,
                genre TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        cursor.execute('ALTER TABLE songs ADD COLUMN IF NOT EXISTS genre TEXT;')
        cursor.execute('ALTER TABLE songs ADD COLUMN IF NOT EXISTS closed INTEGER DEFAULT 0;')
        cursor.execute('ALTER TABLE songs ADD COLUMN IF NOT EXISTS preview_used INTEGER DEFAULT 0;')
        cursor.execute('ALTER TABLE songs ADD COLUMN IF NOT EXISTS song_number INTEGER;')
        cursor.execute('''
            WITH missing AS (
                SELECT id, guild_id,
                       ROW_NUMBER() OVER (PARTITION BY guild_id ORDER BY id) AS row_offset
                FROM songs
                WHERE song_number IS NULL
            ),
            current_max AS (
                SELECT guild_id, COALESCE(MAX(song_number), 0) AS max_number
                FROM songs
                GROUP BY guild_id
            )
            UPDATE songs s
            SET song_number = current_max.max_number + missing.row_offset
            FROM missing
            JOIN current_max ON current_max.guild_id = missing.guild_id
            WHERE s.id = missing.id;
        ''')
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS songs_guild_song_number_idx
            ON songs (guild_id, song_number);
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS songs_guild_created_idx
            ON songs (guild_id, created_at DESC);
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ratings (
                song_id INTEGER NOT NULL REFERENCES songs(id),
                user_id BIGINT NOT NULL,
                score INTEGER NOT NULL,
                PRIMARY KEY (song_id, user_id)
            );
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS ratings_song_id_idx
            ON ratings (song_id);
        ''')
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def init_music_db():
    await asyncio.to_thread(_raw_init_music_db)


# ==========================================================================
# Invite tracking: joins / leaves
# ==========================================================================

def _raw_save_join(guild_id, user_id, user_name, inviter_name, invite_code, account_age_days, inviter_id=None, invite_uses=None, invite_source="invite"):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            INSERT INTO joins (
                guild_id, user_id, user_name, inviter_name, invite_code, join_date,
                account_age_days, inviter_id, invite_uses, invite_source
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING;
        ''', (str(guild_id), str(user_id), user_name, inviter_name, invite_code, now,
         int(account_age_days), str(inviter_id) if inviter_id else None,
         invite_uses, invite_source))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_save_join(
    guild_id, user_id, user_name, inviter_name, invite_code, account_age_days,
    inviter_id=None, invite_uses=None, invite_source="invite",
):
    await asyncio.to_thread(
        _raw_save_join, guild_id, user_id, user_name, inviter_name,
        invite_code, account_age_days, inviter_id, invite_uses, invite_source,
    )


def _raw_save_leave(guild_id, user_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('''
            INSERT INTO leaves (guild_id, user_id, leave_date)
            VALUES (%s, %s, %s);
        ''', (str(guild_id), str(user_id), now))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_save_leave(guild_id, user_id):
    await asyncio.to_thread(_raw_save_leave, guild_id, user_id)



def _raw_upsert_member_snapshot(
    guild_id, user_id, username, display_name, avatar_url,
    created_at, joined_at, boosting, roles, badges, is_member=True,
):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        now = datetime.now(timezone.utc)
        cursor.execute('''
            INSERT INTO member_snapshots (
                guild_id, user_id, username, display_name, avatar_url,
                created_at, joined_at, first_seen_at, last_seen_at,
                last_left_at, is_member, boosting, roles_json, badges_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET
                username = EXCLUDED.username,
                display_name = EXCLUDED.display_name,
                avatar_url = EXCLUDED.avatar_url,
                created_at = COALESCE(EXCLUDED.created_at, member_snapshots.created_at),
                joined_at = COALESCE(EXCLUDED.joined_at, member_snapshots.joined_at),
                last_seen_at = EXCLUDED.last_seen_at,
                last_left_at = CASE
                    WHEN EXCLUDED.is_member THEN member_snapshots.last_left_at
                    ELSE EXCLUDED.last_left_at
                END,
                is_member = EXCLUDED.is_member,
                boosting = EXCLUDED.boosting,
                roles_json = EXCLUDED.roles_json,
                badges_json = EXCLUDED.badges_json;
        ''', (
            guild_id, user_id, username, display_name, avatar_url,
            created_at, joined_at, now, now,
            None if is_member else now, is_member, boosting,
            json.dumps(roles or []), json.dumps(badges or []),
        ))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_upsert_member_snapshot(
    guild_id, user_id, username, display_name, avatar_url,
    created_at, joined_at, boosting, roles, badges, is_member=True,
):
    await asyncio.to_thread(
        _raw_upsert_member_snapshot,
        guild_id, user_id, username, display_name, avatar_url,
        created_at, joined_at, boosting, roles, badges, is_member,
    )


def _raw_get_member_snapshot(guild_id, user_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT * FROM member_snapshots
            WHERE guild_id = %s AND user_id = %s;
        ''', (guild_id, user_id))
        row = cursor.fetchone()
        cursor.close()
        if row:
            row["roles"] = json.loads(row.pop("roles_json") or "[]")
            row["badges"] = json.loads(row.pop("badges_json") or "[]")
        return row
    finally:
        release_db_conn(conn)


async def async_get_member_snapshot(guild_id, user_id):
    return await asyncio.to_thread(_raw_get_member_snapshot, guild_id, user_id)


def _raw_search_member_snapshots(guild_id, query, limit=25):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        pattern = "%" + (query or "").strip() + "%"
        cursor.execute('''
            SELECT user_id, username, display_name, is_member
            FROM member_snapshots
            WHERE guild_id = %s
              AND (CAST(user_id AS TEXT) ILIKE %s OR username ILIKE %s OR display_name ILIKE %s)
            ORDER BY is_member DESC, display_name ASC
            LIMIT %s;
        ''', (guild_id, pattern, pattern, pattern, limit))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def async_search_member_snapshots(guild_id, query, limit=25):
    return await asyncio.to_thread(_raw_search_member_snapshots, guild_id, query, limit)


def _raw_get_member_invite_summary(guild_id, user_id, inviter_names):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT user_id, user_name, inviter_name, invite_code, join_date, account_age_days
            FROM joins
            WHERE guild_id = %s AND user_id = %s
            ORDER BY join_date DESC
            LIMIT 1;
        ''', (str(guild_id), str(user_id)))
        latest_join = cursor.fetchone()

        names = [name for name in (inviter_names or []) if name]
        if names:
            cursor.execute('''
                SELECT COUNT(*) AS invite_count
                FROM joins
                WHERE guild_id = %s AND inviter_name = ANY(%s);
            ''', (str(guild_id), names))
            invite_count = cursor.fetchone()["invite_count"]
        else:
            invite_count = 0
        cursor.close()
        return latest_join, invite_count
    finally:
        release_db_conn(conn)


async def async_get_member_invite_summary(guild_id, user_id, inviter_names):
    return await asyncio.to_thread(
        _raw_get_member_invite_summary, guild_id, user_id, inviter_names
    )


def _raw_get_member_history(guild_id, user_id, limit=20):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT 'join' AS event_type, join_date AS event_date,
                   inviter_name, invite_code, account_age_days
            FROM joins WHERE guild_id = %s AND user_id = %s
            UNION ALL
            SELECT 'leave' AS event_type, leave_date AS event_date,
                   NULL, NULL, NULL
            FROM leaves WHERE guild_id = %s AND user_id = %s
            ORDER BY event_date DESC LIMIT %s;
        ''', (str(guild_id), str(user_id), str(guild_id), str(user_id), limit))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def async_get_member_history(guild_id, user_id, limit=20):
    return await asyncio.to_thread(_raw_get_member_history, guild_id, user_id, limit)


# ==========================================================================
# Guild config: log channel / alert role / mod role / prefix / panel / music
# ==========================================================================

def _raw_get_guild_log_channel(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT log_channel_id FROM config WHERE guild_id = %s;', (str(guild_id),))
        res = cursor.fetchone()
        cursor.close()
        return int(res[0]) if res and res[0] else None
    finally:
        release_db_conn(conn)


async def async_get_guild_log_channel(guild_id):
    return await asyncio.to_thread(_raw_get_guild_log_channel, guild_id)


def _raw_set_guild_log_channel(guild_id, channel_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO config (guild_id, log_channel_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET log_channel_id = EXCLUDED.log_channel_id;
        ''', (str(guild_id), str(channel_id) if channel_id is not None else None))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_set_guild_log_channel(guild_id, channel_id):
    await asyncio.to_thread(_raw_set_guild_log_channel, guild_id, channel_id)


def _raw_get_alert_role(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT alert_role_id FROM config WHERE guild_id = %s;', (str(guild_id),))
        res = cursor.fetchone()
        cursor.close()
        return int(res[0]) if res and res[0] else None
    finally:
        release_db_conn(conn)


async def async_get_alert_role(guild_id):
    return await asyncio.to_thread(_raw_get_alert_role, guild_id)


def _raw_set_alert_role(guild_id, role_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO config (guild_id, alert_role_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET alert_role_id = EXCLUDED.alert_role_id;
        ''', (str(guild_id), str(role_id) if role_id is not None else None))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_set_alert_role(guild_id, role_id):
    await asyncio.to_thread(_raw_set_alert_role, guild_id, role_id)


def _raw_get_mod_role(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT mod_role_id FROM config WHERE guild_id = %s;', (str(guild_id),))
        res = cursor.fetchone()
        cursor.close()
        return int(res[0]) if res and res[0] else None
    finally:
        release_db_conn(conn)


async def async_get_mod_role(guild_id):
    return await asyncio.to_thread(_raw_get_mod_role, guild_id)


def _raw_set_mod_role(guild_id, role_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO config (guild_id, mod_role_id)
            VALUES (%s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET mod_role_id = EXCLUDED.mod_role_id;
        ''', (str(guild_id), str(role_id) if role_id is not None else None))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_set_mod_role(guild_id, role_id):
    await asyncio.to_thread(_raw_set_mod_role, guild_id, role_id)


def _raw_get_prefix(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT prefix FROM config WHERE guild_id = %s;', (str(guild_id),))
        res = cursor.fetchone()
        cursor.close()
        return res[0] if res and res[0] else None
    finally:
        release_db_conn(conn)


async def async_get_prefix(guild_id):
    if guild_id in guild_prefix_cache:
        return guild_prefix_cache[guild_id]
    stored = await asyncio.to_thread(_raw_get_prefix, guild_id)
    prefix = stored if stored else DEFAULT_PREFIX
    guild_prefix_cache[guild_id] = prefix
    return prefix


def _raw_set_prefix(guild_id, prefix):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO config (guild_id, prefix)
            VALUES (%s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET prefix = EXCLUDED.prefix;
        ''', (str(guild_id), prefix))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_set_prefix(guild_id, prefix):
    await asyncio.to_thread(_raw_set_prefix, guild_id, prefix)
    guild_prefix_cache[guild_id] = prefix


def _raw_save_panel_config(guild_id, channel_id, message_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO config (guild_id, panel_channel_id, panel_message_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (guild_id) DO UPDATE SET
                panel_channel_id = EXCLUDED.panel_channel_id,
                panel_message_id = EXCLUDED.panel_message_id;
        ''', (str(guild_id), str(channel_id), str(message_id)))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_save_panel_config(guild_id, channel_id, message_id):
    await asyncio.to_thread(_raw_save_panel_config, guild_id, channel_id, message_id)


def _raw_get_panel_config(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT panel_channel_id, panel_message_id FROM config WHERE guild_id = %s;', (str(guild_id),))
        res = cursor.fetchone()
        cursor.close()
        return res if res and res[0] and res[1] else None
    finally:
        release_db_conn(conn)


async def async_get_panel_config(guild_id):
    return await asyncio.to_thread(_raw_get_panel_config, guild_id)


_UNSET = object()  # sentinel distinguishing "field not passed" (skip it) from
                    # None (explicitly clear it) — plain None can't do both.


def _raw_set_music_config(guild_id, role_id=_UNSET, lock_time=_UNSET, channel_id=_UNSET):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO config (guild_id) VALUES (%s) ON CONFLICT (guild_id) DO NOTHING;', (str(guild_id),))
        if role_id is not _UNSET:
            cursor.execute('UPDATE config SET music_role_id = %s WHERE guild_id = %s;',
                            (str(role_id) if role_id is not None else None, str(guild_id)))
        if lock_time is not _UNSET:
            cursor.execute('UPDATE config SET music_lock_time = %s WHERE guild_id = %s;',
                            (int(lock_time) if lock_time is not None else None, str(guild_id)))
        if channel_id is not _UNSET:
            cursor.execute('UPDATE config SET music_channel_id = %s WHERE guild_id = %s;',
                            (str(channel_id) if channel_id is not None else None, str(guild_id)))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def async_set_music_config(guild_id, role_id=_UNSET, lock_time=_UNSET, channel_id=_UNSET):
    await asyncio.to_thread(_raw_set_music_config, guild_id, role_id, lock_time, channel_id)


def _raw_get_music_config(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT music_role_id, music_lock_time, music_channel_id FROM config WHERE guild_id = %s;', (str(guild_id),))
        res = cursor.fetchone()
        cursor.close()
        return res if res else (None, 0, None)
    finally:
        release_db_conn(conn)


async def async_get_music_config(guild_id):
    return await asyncio.to_thread(_raw_get_music_config, guild_id)


# ==========================================================================
# Dashboard / growth analytics
# ==========================================================================

def _raw_compile_dashboard_stats(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM joins WHERE guild_id = %s;', (str(guild_id),))
        total_joins = cursor.fetchone()[0]

        one_day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute('SELECT COUNT(*) FROM joins WHERE guild_id = %s AND join_date >= %s;', (str(guild_id), one_day_ago))
        joins_24h = cursor.fetchone()[0]

        cursor.execute('SELECT COUNT(*) FROM joins WHERE guild_id = %s AND account_age_days < 7;', (str(guild_id),))
        extreme_risk_count = cursor.fetchone()[0]

        cursor.execute('''
            SELECT invite_code, COUNT(*) as code_count
            FROM joins
            WHERE guild_id = %s AND invite_code != 'Unknown'
            GROUP BY invite_code
            ORDER BY code_count DESC
            LIMIT 3;
        ''', (str(guild_id),))
        top_codes = cursor.fetchall()

        cursor.close()
        return total_joins, joins_24h, extreme_risk_count, top_codes
    finally:
        release_db_conn(conn)


async def async_compile_dashboard_stats(guild_id):
    return await asyncio.to_thread(_raw_compile_dashboard_stats, guild_id)


def _raw_get_growth_analytics(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT
                COUNT(*) AS total_joins,
                COUNT(*) FILTER (WHERE join_date >= NOW() - INTERVAL '24 hours') AS joins_24h,
                COUNT(*) FILTER (WHERE join_date >= NOW() - INTERVAL '7 days') AS joins_7d,
                COUNT(*) FILTER (WHERE account_age_days < 7) AS high_risk,
                COUNT(DISTINCT inviter_id) FILTER (WHERE inviter_id IS NOT NULL) AS unique_inviters
            FROM joins WHERE guild_id = %s;
        ''', (str(guild_id),))
        totals = cursor.fetchone()
        cursor.execute('''
            SELECT COUNT(DISTINCT j.user_id) AS left_count
            FROM joins j JOIN leaves l
              ON l.guild_id = j.guild_id AND l.user_id = j.user_id
            WHERE j.guild_id = %s;
        ''', (str(guild_id),))
        left_count = cursor.fetchone()["left_count"]
        cursor.execute('''
            SELECT COALESCE(inviter_id, inviter_name) AS inviter,
                   COUNT(*) AS joins_count
            FROM joins WHERE guild_id = %s
            GROUP BY COALESCE(inviter_id, inviter_name)
            ORDER BY joins_count DESC LIMIT 5;
        ''', (str(guild_id),))
        top_inviters = cursor.fetchall()
        cursor.close()
        return totals, left_count, top_inviters
    finally:
        release_db_conn(conn)


async def async_get_growth_analytics(guild_id):
    return await asyncio.to_thread(_raw_get_growth_analytics, guild_id)


def _raw_get_joins_in_range(guild_id, start_dt=None, limit=None, newest_first=False):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        order = "DESC" if newest_first else "ASC"
        params = [str(guild_id)]
        where = "guild_id = %s"
        if start_dt:
            where += " AND join_date >= %s"
            params.append(start_dt.strftime('%Y-%m-%d %H:%M:%S'))
        query = f"""
            SELECT user_id, user_name, inviter_name, invite_code, join_date, account_age_days
            FROM joins WHERE {where} ORDER BY join_date {order}
        """
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(1, min(int(limit), 10000)))
        query += ";"
        cursor.execute(query, tuple(params))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def async_get_joins_in_range(guild_id, start_dt=None, limit=None, newest_first=False):
    return await asyncio.to_thread(_raw_get_joins_in_range, guild_id, start_dt, limit, newest_first)


def _raw_get_daily_join_counts(guild_id, days=30):
    start_dt = datetime.now(timezone.utc) - timedelta(days=days)
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT join_date FROM joins WHERE guild_id = %s AND join_date >= %s;
        ''', (str(guild_id), start_dt.strftime('%Y-%m-%d %H:%M:%S')))
        rows = cursor.fetchall()
        cursor.close()

        counts = {}
        for (join_date_str,) in rows:
            day = datetime.strptime(join_date_str, '%Y-%m-%d %H:%M:%S').date()
            counts[day] = counts.get(day, 0) + 1

        today = datetime.now(timezone.utc).date()
        series = []
        for i in range(days - 1, -1, -1):
            day = today - timedelta(days=i)
            series.append((day, counts.get(day, 0)))
        return series
    finally:
        release_db_conn(conn)


async def async_get_daily_join_counts(guild_id, days=30):
    return await asyncio.to_thread(_raw_get_daily_join_counts, guild_id, days)


def _raw_get_leaderboard(guild_id, limit=10):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT inviter_name, COUNT(*) as joins_count
            FROM joins
            WHERE guild_id = %s AND inviter_name != 'Unknown / Custom Link'
            GROUP BY inviter_name
            ORDER BY joins_count DESC
            LIMIT %s;
        ''', (str(guild_id), limit))
        results = cursor.fetchall()
        cursor.close()
        return results
    finally:
        release_db_conn(conn)


async def async_get_leaderboard(guild_id, limit=10):
    return await asyncio.to_thread(_raw_get_leaderboard, guild_id, limit)


def _raw_get_inviter_stats(guild_id, inviter_name, recent_limit=10):
    """Matched by inviter_name since that's what `joins` stores (a text
    snapshot taken at join time, not a stable user_id) — the same
    limitation /leaderboard already has.
    """
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT COUNT(*) AS total_joins,
                   COUNT(*) FILTER (WHERE account_age_days < 7) AS flagged_alts
            FROM joins
            WHERE guild_id = %s AND inviter_name = %s;
        ''', (str(guild_id), inviter_name))
        totals = cursor.fetchone()

        cursor.execute('''
            SELECT COUNT(DISTINCT j.user_id) AS left_count
            FROM joins j
            JOIN leaves l ON l.guild_id = j.guild_id AND l.user_id = j.user_id
            WHERE j.guild_id = %s AND j.inviter_name = %s;
        ''', (str(guild_id), inviter_name))
        left_row = cursor.fetchone()

        cursor.execute('''
            SELECT user_name, join_date, account_age_days
            FROM joins
            WHERE guild_id = %s AND inviter_name = %s
            ORDER BY join_date DESC LIMIT %s;
        ''', (str(guild_id), inviter_name, recent_limit))
        recent = cursor.fetchall()

        cursor.close()
        return totals, (left_row["left_count"] if left_row else 0), recent
    finally:
        release_db_conn(conn)


async def async_get_inviter_stats(guild_id, inviter_name, recent_limit=10):
    return await asyncio.to_thread(_raw_get_inviter_stats, guild_id, inviter_name, recent_limit)


def _raw_get_invite_code_stats(guild_id, inviter_name=None):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if inviter_name:
            cursor.execute('''
                SELECT invite_code AS code,
                       COUNT(*) AS invite_count,
                       COUNT(*) FILTER (WHERE account_age_days < 7) AS flagged_count
                FROM joins
                WHERE guild_id = %s AND inviter_name = %s
                  AND invite_code IS NOT NULL
                  AND invite_code != '' AND invite_code != 'Unknown'
                GROUP BY invite_code
                ORDER BY invite_count DESC, code ASC;
            ''', (str(guild_id), inviter_name))
        else:
            cursor.execute('''
                SELECT invite_code AS code,
                       COUNT(*) AS invite_count,
                       COUNT(*) FILTER (WHERE account_age_days < 7) AS flagged_count
                FROM joins
                WHERE guild_id = %s
                  AND invite_code IS NOT NULL
                  AND invite_code != '' AND invite_code != 'Unknown'
                GROUP BY invite_code
                ORDER BY invite_count DESC, code ASC;
            ''', (str(guild_id),))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def async_get_invite_code_stats(guild_id, inviter_name=None):
    return await asyncio.to_thread(_raw_get_invite_code_stats, guild_id, inviter_name)


def _raw_get_invitees(guild_id, inviter_name=None, invite_code=None, limit=25):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        filters = ["guild_id = %s"]
        params = [str(guild_id)]
        if inviter_name:
            filters.append("inviter_name = %s")
            params.append(inviter_name)
        if invite_code:
            filters.append("invite_code = %s")
            params.append(invite_code)
        params.append(limit)
        cursor.execute(
            f'''
                SELECT user_id, user_name, inviter_name, invite_code, join_date, account_age_days
                FROM joins
                WHERE {" AND ".join(filters)}
                ORDER BY join_date DESC
                LIMIT %s;
            ''',
            tuple(params),
        )
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def async_get_invitees(guild_id, inviter_name=None, invite_code=None, limit=25):
    return await asyncio.to_thread(_raw_get_invitees, guild_id, inviter_name, invite_code, limit)


# ==========================================================================
# Music: songs
# ==========================================================================

def _raw_add_song(guild_id, channel_id, title, artist, source, url, requester_id, requester_name, cover_url, preview_url, genre=None):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        # Serialize duplicate detection and per-server numbering in one
        # transaction so simultaneous posts cannot create duplicate rows.
        cursor.execute("SELECT pg_advisory_xact_lock(%s);", (int(guild_id),))

        if url:
            cursor.execute('''
                SELECT * FROM songs
                WHERE guild_id = %s AND url = %s
                ORDER BY id DESC LIMIT 1;
            ''', (guild_id, url))
            existing = cursor.fetchone()
        else:
            existing = None

        if not existing and title:
            cursor.execute('''
                SELECT * FROM songs
                WHERE guild_id = %s
                  AND LOWER(title) = LOWER(%s)
                  AND (artist IS NOT DISTINCT FROM %s
                       OR LOWER(COALESCE(artist, '')) = LOWER(COALESCE(%s, '')))
                ORDER BY id DESC LIMIT 1;
            ''', (guild_id, title, artist, artist))
            existing = cursor.fetchone()

        if existing:
            conn.commit()
            cursor.close()
            return {"existing": dict(existing)}

        cursor.execute("SELECT COALESCE(MAX(song_number), 0) + 1 AS next_number FROM songs WHERE guild_id = %s;", (guild_id,))
        song_number = cursor.fetchone()["next_number"]
        cursor.execute('''
            INSERT INTO songs (guild_id, song_number, channel_id, message_id, title, artist, source, url, cover_url, preview_url,
                                requested_by_id, requested_by_name, synced, genre)
            VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)
            RETURNING id, song_number;
        ''', (guild_id, song_number, channel_id, title, artist, source, url, cover_url, preview_url,
              requester_id, requester_name, genre))
        row = cursor.fetchone()
        conn.commit()
        cursor.close()
        return {"id": row["id"], "song_number": row["song_number"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_conn(conn)


async def add_song(guild_id, channel_id, title, artist, source, url, requester_id, requester_name, cover_url=None, preview_url=None, genre=None):
    return await asyncio.to_thread(_raw_add_song, guild_id, channel_id, title, artist, source, url,
                                   requester_id, requester_name, cover_url, preview_url, genre)


def _raw_set_song_genre(song_id, genre):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE songs SET genre = %s WHERE id = %s;', (genre, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def set_song_genre(song_id: int, genre: str):
    await asyncio.to_thread(_raw_set_song_genre, song_id, genre)


def _raw_find_duplicate_song(guild_id, url, title, artist):
    """Matched by exact source URL first, falling back to a case-insensitive
    title+artist match for links that resolve to the same track via
    different URLs (regional Spotify links, a manual /song lookup that's
    later posted as a link, etc.)."""
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        if url:
            cursor.execute('''
                SELECT * FROM songs WHERE guild_id = %s AND url = %s
                ORDER BY id DESC LIMIT 1;
            ''', (guild_id, url))
            row = cursor.fetchone()
            if row:
                cursor.close()
                return row
        if title:
            cursor.execute('''
                SELECT * FROM songs WHERE guild_id = %s
                    AND LOWER(title) = LOWER(%s)
                    AND (artist IS NOT DISTINCT FROM %s OR LOWER(COALESCE(artist, '')) = LOWER(COALESCE(%s, '')))
                ORDER BY id DESC LIMIT 1;
            ''', (guild_id, title, artist, artist))
            row = cursor.fetchone()
            cursor.close()
            return row
        cursor.close()
        return None
    finally:
        release_db_conn(conn)


async def find_duplicate_song(guild_id: int, url: str, title: str, artist: str):
    return await asyncio.to_thread(_raw_find_duplicate_song, guild_id, url, title, artist)


def _raw_set_song_message_id(guild_id, song_id, message_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE songs SET message_id = %s WHERE guild_id = %s AND id = %s;', (message_id, guild_id, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def set_song_message_id(guild_id: int, song_id: int, message_id: int):
    await asyncio.to_thread(_raw_set_song_message_id, guild_id, song_id, message_id)



def _raw_set_rating(guild_id, song_id, user_id, score):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO ratings (song_id, user_id, score)
            SELECT id, %s, %s FROM songs
            WHERE guild_id = %s AND id = %s
            ON CONFLICT (song_id, user_id) DO UPDATE SET score = EXCLUDED.score;
        ''', (user_id, score, guild_id, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def set_rating(guild_id: int, song_id: int, user_id: int, score: int):
    await asyncio.to_thread(_raw_set_rating, guild_id, song_id, user_id, score)



def _raw_get_song_stats(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT AVG(r.score), COUNT(r.score)
            FROM ratings r JOIN songs s ON s.id = r.song_id
            WHERE s.guild_id = %s AND s.id = %s;
        ''', (guild_id, song_id))
        row = cursor.fetchone()
        cursor.close()
        return (float(row[0]) if row[0] is not None else 0.0), (row[1] or 0)
    finally:
        release_db_conn(conn)


async def get_song_stats(guild_id: int, song_id: int):
    return await asyncio.to_thread(_raw_get_song_stats, guild_id, song_id)



def _raw_get_song_ratings_breakdown(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT r.user_id, r.score
            FROM ratings r JOIN songs s ON s.id = r.song_id
            WHERE s.guild_id = %s AND s.id = %s;
        ''', (guild_id, song_id))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def get_song_ratings_breakdown(guild_id: int, song_id: int):
    return await asyncio.to_thread(_raw_get_song_ratings_breakdown, guild_id, song_id)



def _raw_get_user_stats(guild_id, user_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT COUNT(*) AS total, AVG(score) AS avg_given
            FROM ratings r JOIN songs s ON r.song_id = s.id
            WHERE s.guild_id = %s AND r.user_id = %s;
        ''', (guild_id, user_id))
        stats = cursor.fetchone()

        cursor.execute('''
            SELECT s.title, s.artist, r.score
            FROM ratings r JOIN songs s ON r.song_id = s.id
            WHERE s.guild_id = %s AND r.user_id = %s
            ORDER BY r.score DESC, s.id DESC LIMIT 5;
        ''', (guild_id, user_id))
        top_rated = cursor.fetchall()
        cursor.close()
        return stats, top_rated
    finally:
        release_db_conn(conn)


async def get_user_stats(guild_id: int, user_id: int):
    return await asyncio.to_thread(_raw_get_user_stats, guild_id, user_id)


def _raw_get_song(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('SELECT * FROM songs WHERE guild_id = %s AND id = %s;', (guild_id, song_id))
        row = cursor.fetchone()
        cursor.close()
        return row
    finally:
        release_db_conn(conn)


async def get_song(guild_id: int, song_id: int):
    return await asyncio.to_thread(_raw_get_song, guild_id, song_id)


def _raw_get_song_by_number(guild_id, song_number):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('SELECT * FROM songs WHERE guild_id = %s AND song_number = %s;', (guild_id, song_number))
        row = cursor.fetchone()
        cursor.close()
        return row
    finally:
        release_db_conn(conn)


async def get_song_by_number(guild_id: int, song_number: int):
    return await asyncio.to_thread(_raw_get_song_by_number, guild_id, song_number)



def _raw_claim_preview(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE songs
            SET preview_used = 1
            WHERE guild_id = %s AND id = %s AND COALESCE(preview_used, 0) = 0
            RETURNING id;
        ''', (guild_id, song_id))
        claimed = cursor.fetchone() is not None
        conn.commit()
        cursor.close()
        return claimed
    finally:
        release_db_conn(conn)


async def claim_preview(guild_id: int, song_id: int) -> bool:
    return await asyncio.to_thread(_raw_claim_preview, guild_id, song_id)



def _raw_get_recent_songs(limit):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT id, guild_id, song_number, preview_used, url
            FROM songs ORDER BY id DESC LIMIT %s;
        ''', (limit,))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def get_recent_songs(limit: int = 500):
    return await asyncio.to_thread(_raw_get_recent_songs, limit)



def _raw_get_music_leaderboard(guild_id, limit, min_votes, min_score=0.0):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT s.id, s.song_number, s.title, s.artist, s.created_at, AVG(r.score) AS avg_score, COUNT(r.score) AS votes
            FROM songs s JOIN ratings r ON s.id = r.song_id
            WHERE s.guild_id = %s GROUP BY s.id
            HAVING COUNT(r.score) >= %s AND AVG(r.score) >= %s
            ORDER BY avg_score DESC, votes DESC LIMIT %s;
        ''', (guild_id, min_votes, min_score, limit))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def get_music_leaderboard(guild_id: int, limit: int = 10, min_votes: int = 2, min_score: float = 0.0):
    return await asyncio.to_thread(_raw_get_music_leaderboard, guild_id, limit, min_votes, min_score)


def _raw_get_music_log(guild_id, limit=25):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT id, song_number, title, artist, source, url, requested_by_name,
                   created_at, synced, closed
            FROM songs
            WHERE guild_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s;
        ''', (guild_id, limit))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def get_music_log(guild_id: int, limit: int = 25):
    return await asyncio.to_thread(_raw_get_music_log, guild_id, limit)


def _raw_search_songs(guild_id, query, limit, min_votes, min_score):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        pattern = "%" + query.strip() + "%"
        cursor.execute('''
            SELECT s.id, s.song_number, s.title, s.artist, s.created_at,
                   AVG(r.score) AS avg_score, COUNT(r.score) AS votes
            FROM songs s JOIN ratings r ON s.id = r.song_id
            WHERE s.guild_id = %s
              AND (s.title ILIKE %s OR COALESCE(s.artist, '') ILIKE %s)
            GROUP BY s.id
            HAVING COUNT(r.score) >= %s AND AVG(r.score) >= %s
            ORDER BY avg_score DESC, votes DESC, s.id DESC
            LIMIT %s;
        ''', (guild_id, pattern, pattern, min_votes, min_score, limit))
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def search_songs(guild_id: int, query: str, limit: int = 25,
                       min_votes: int = 2, min_score: float = 0.0):
    return await asyncio.to_thread(_raw_search_songs, guild_id, query, limit, min_votes, min_score)


def _raw_mark_song_synced(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE songs SET synced = 1 WHERE guild_id = %s AND id = %s;', (guild_id, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def mark_song_synced(guild_id: int, song_id: int):
    await asyncio.to_thread(_raw_mark_song_synced, guild_id, song_id)



def _raw_unmark_song_synced(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE songs SET synced = 0 WHERE guild_id = %s AND id = %s;', (guild_id, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def unmark_song_synced(guild_id: int, song_id: int):
    await asyncio.to_thread(_raw_unmark_song_synced, guild_id, song_id)



def _raw_close_song(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('UPDATE songs SET closed = 1 WHERE guild_id = %s AND id = %s;', (guild_id, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def close_song(guild_id: int, song_id: int):
    await asyncio.to_thread(_raw_close_song, guild_id, song_id)



def _raw_get_user_vote(guild_id, song_id, user_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT r.score FROM ratings r JOIN songs s ON s.id = r.song_id
            WHERE s.guild_id = %s AND s.id = %s AND r.user_id = %s;
        ''', (guild_id, song_id, user_id))
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else None
    finally:
        release_db_conn(conn)


async def get_user_vote(guild_id: int, song_id: int, user_id: int):
    return await asyncio.to_thread(_raw_get_user_vote, guild_id, song_id, user_id)



def _raw_delete_song(guild_id, song_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM ratings
            WHERE song_id = (SELECT id FROM songs WHERE guild_id = %s AND id = %s);
        ''', (guild_id, song_id))
        cursor.execute('DELETE FROM songs WHERE guild_id = %s AND id = %s;', (guild_id, song_id))
        conn.commit()
        cursor.close()
    finally:
        release_db_conn(conn)


async def delete_song(guild_id: int, song_id: int):
    await asyncio.to_thread(_raw_delete_song, guild_id, song_id)



def _raw_get_expired_open_songs():
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute('''
            SELECT id, guild_id, channel_id, message_id FROM songs
            WHERE closed = 0 AND created_at <= NOW() - INTERVAL '12 hours';
        ''')
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        release_db_conn(conn)


async def get_expired_open_songs():
    return await asyncio.to_thread(_raw_get_expired_open_songs)


def _raw_renumber_songs(guild_id):
    conn = get_db_conn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT pg_advisory_xact_lock(%s);", (int(guild_id),))
        # Keep the mapping in a transaction-local temp table, and move the
        # live values to unique negative IDs before assigning 1..N. This
        # avoids immediate unique-index collisions while rows are rewritten.
        cursor.execute("DROP TABLE IF EXISTS pg_temp.song_number_map;")
        cursor.execute('''
            CREATE TEMP TABLE song_number_map ON COMMIT DROP AS
            SELECT id, song_number AS old_number,
                   ROW_NUMBER() OVER (ORDER BY id) AS new_number,
                   guild_id, channel_id, message_id
            FROM songs
            WHERE guild_id = %s;
        ''', (guild_id,))
        cursor.execute('''
            UPDATE songs
            SET song_number = -id
            WHERE guild_id = %s;
        ''', (guild_id,))
        cursor.execute('''
            UPDATE songs s
            SET song_number = m.new_number
            FROM song_number_map m
            WHERE s.id = m.id;
        ''')
        cursor.execute('''
            SELECT id, guild_id, old_number, new_number, channel_id, message_id
            FROM song_number_map ORDER BY new_number;
        ''')
        rows = cursor.fetchall()
        conn.commit()
        cursor.close()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_conn(conn)


async def renumber_songs(guild_id: int):
    return await asyncio.to_thread(_raw_renumber_songs, guild_id)
