"""SQLite-backed per-user provider session store."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

DB_PATH = Path(os.environ.get("SESSION_DB", "/data/sessions.db"))
if not DB_PATH.parent.exists():
    # local fallback when /data isn't mounted (dev mode)
    DB_PATH = Path(__file__).parent / "sessions" / "sessions.db"
    DB_PATH.parent.mkdir(exist_ok=True)


SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_sessions (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    storage_state TEXT NOT NULL,
    created_at INTEGER DEFAULT (strftime('%s','now')),
    last_used_at INTEGER DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_sessions_provider ON provider_sessions(provider);
"""


async def init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    print(f"[db] ready at {DB_PATH}")


async def save_session(provider: str, storage_state: dict[str, Any]) -> str:
    sid = uuid.uuid4().hex
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO provider_sessions (id, provider, storage_state) VALUES (?, ?, ?)",
            (sid, provider, json.dumps(storage_state)),
        )
        await db.commit()
    return sid


async def load_session(sid: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT provider, storage_state FROM provider_sessions WHERE id = ?",
            (sid,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute(
            "UPDATE provider_sessions SET last_used_at = strftime('%s','now') WHERE id = ?",
            (sid,),
        )
        await db.commit()
        return {"provider": row[0], "storage_state": json.loads(row[1])}


async def delete_session(sid: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM provider_sessions WHERE id = ?", (sid,))
        await db.commit()
