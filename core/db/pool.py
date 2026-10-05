"""PostgreSQL connection-pool lifecycle and health helpers."""

import logging

import psycopg2
from psycopg2 import pool

from core.config import DB_CONNECTION_STRING, DB_POOL_MIN, DB_POOL_MAX

logger = logging.getLogger(__name__)
db_pool = None


def init_db_pool():
    global db_pool
    if db_pool:
        return
    if not DB_CONNECTION_STRING:
        raise RuntimeError("DB_URL environment variable is not set.")
    try:
        db_pool = pool.ThreadedConnectionPool(DB_POOL_MIN, DB_POOL_MAX, DB_CONNECTION_STRING)
    except psycopg2.Error as exc:
        raise RuntimeError("Could not connect to PostgreSQL. Check DB_URL and database availability.") from exc


def get_db_conn():
    if db_pool is None:
        init_db_pool()
    return db_pool.getconn()


def release_db_conn(conn):
    if db_pool is None or conn is None:
        return
    db_pool.putconn(conn, close=bool(conn.closed))


def close_db_pool():
    global db_pool
    if db_pool is not None:
        db_pool.closeall()
        db_pool = None


def check_db_health():
    conn = get_db_conn()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()
        return True
    except Exception:
        logger.exception("PostgreSQL health check failed")
        try:
            conn.rollback()
        except Exception:
            logger.debug("Unable to rollback after database health-check failure", exc_info=True)
        return False
    finally:
        release_db_conn(conn)
