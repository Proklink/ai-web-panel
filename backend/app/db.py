import json
from pathlib import Path

import asyncpg

from . import config

pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect():
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=8, init=_init_conn)
    schema = (Path(__file__).parent / "schema.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema)


async def close():
    if pool:
        await pool.close()


async def fetch(q, *a):
    return await pool.fetch(q, *a)


async def fetchrow(q, *a):
    return await pool.fetchrow(q, *a)


async def fetchval(q, *a):
    return await pool.fetchval(q, *a)


async def execute(q, *a):
    return await pool.execute(q, *a)


# ---- настройки ----
_settings_cache: dict | None = None


async def get_settings() -> dict:
    global _settings_cache
    if _settings_cache is None:
        rows = await fetch("SELECT key, value FROM settings")
        s = dict(config.DEFAULT_SETTINGS)
        s.update({r["key"]: r["value"] for r in rows if r["key"] in config.DEFAULT_SETTINGS})
        _settings_cache = s
    return dict(_settings_cache)


async def set_settings(values: dict):
    global _settings_cache
    for k, v in values.items():
        if k in config.DEFAULT_SETTINGS:
            await execute(
                "INSERT INTO settings(key, value) VALUES($1, $2) ON CONFLICT (key) DO UPDATE SET value = $2", k, v
            )
    _settings_cache = None


async def add_incident(kind: str, message: str, user_id=None, chat_id=None, chat_title=None, details=None):
    return await fetchrow(
        "INSERT INTO incidents(kind, user_id, chat_id, chat_title, message, details) VALUES($1,$2,$3,$4,$5,$6) RETURNING *",
        kind, user_id, chat_id, chat_title, message, details or {},
    )
