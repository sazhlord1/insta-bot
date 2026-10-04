"""Tiny SQLite layer. Calls are fast, so they run directly (guarded by a lock)."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,            -- instagram | youtube
    handle     TEXT NOT NULL,            -- username or channel url
    ig_user_id TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    UNIQUE(platform, handle)
);
CREATE TABLE IF NOT EXISTS seen (
    kind   TEXT NOT NULL,                -- ig_post | ig_story | yt | dm_msg
    ext_id TEXT NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (kind, ext_id)
);
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,          -- reel | story
    origin       TEXT NOT NULL,          -- source | dm
    source_label TEXT,
    ext_id       TEXT,
    file_path    TEXT NOT NULL,
    media_type   TEXT NOT NULL,          -- video | photo
    status       TEXT NOT NULL,          -- review | queued | scheduled | publishing | published | rejected | failed
    error        TEXT,
    ig_code      TEXT,
    created_at   INTEGER NOT NULL,
    published_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
"""

DEFAULT_CAPTION = "🎬"


class DB:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ---------- low level ----------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---------- settings ----------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def caption(self) -> str:
        return self.get("caption", DEFAULT_CAPTION)

    # ---------- sources ----------
    def add_source(self, platform: str, handle: str) -> Optional[int]:
        try:
            cur = self._exec(
                "INSERT INTO sources(platform, handle, created_at) VALUES(?, ?, ?)",
                (platform, handle, int(time.time())),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def remove_source(self, source_id: int) -> bool:
        return self._exec("DELETE FROM sources WHERE id=?", (source_id,)).rowcount > 0

    def sources(self, platform: Optional[str] = None) -> list[sqlite3.Row]:
        if platform:
            return self._all("SELECT * FROM sources WHERE platform=? AND active=1 ORDER BY id", (platform,))
        return self._all("SELECT * FROM sources ORDER BY platform, id")

    def set_source_ig_id(self, source_id: int, ig_user_id: str) -> None:
        self._exec("UPDATE sources SET ig_user_id=? WHERE id=?", (ig_user_id, source_id))

    # ---------- seen ----------
    def is_seen(self, kind: str, ext_id: str) -> bool:
        return self._one("SELECT 1 FROM seen WHERE kind=? AND ext_id=?", (kind, str(ext_id))) is not None

    def mark_seen(self, kind: str, ext_id: str) -> None:
        self._exec(
            "INSERT OR IGNORE INTO seen(kind, ext_id, seen_at) VALUES(?, ?, ?)",
            (kind, str(ext_id), int(time.time())),
        )

    # ---------- items ----------
    def add_item(self, kind: str, origin: str, source_label: str, ext_id: str,
                 file_path: str, media_type: str, status: str) -> int:
        cur = self._exec(
            "INSERT INTO items(kind, origin, source_label, ext_id, file_path, media_type, status, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, origin, source_label, str(ext_id), file_path, media_type, status, int(time.time())),
        )
        return cur.lastrowid

    def item(self, item_id: int) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM items WHERE id=?", (item_id,))

    def items_by_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(statuses))
        return self._all(f"SELECT * FROM items WHERE status IN ({marks}) ORDER BY id", statuses)

    def set_status(self, item_id: int, status: str, error: Optional[str] = None,
                   ig_code: Optional[str] = None) -> None:
        published_at = int(time.time()) if status == "published" else None
        self._exec(
            "UPDATE items SET status=?, error=?, ig_code=COALESCE(?, ig_code), "
            "published_at=COALESCE(?, published_at) WHERE id=?",
            (status, error, ig_code, published_at, item_id),
        )

    def published_last_24h(self) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM items WHERE status='published' AND published_at>=?",
            (int(time.time()) - 86400,),
        )
        return row["n"]

    def reset_interrupted(self) -> int:
        """Items that were mid-publish when the bot restarted go back to the queue."""
        return self._exec(
            "UPDATE items SET status='queued' WHERE status IN ('scheduled', 'publishing')"
        ).rowcount

    def stats(self) -> dict:
        rows = self._all("SELECT status, COUNT(*) AS n FROM items GROUP BY status")
        return {r["status"]: r["n"] for r in rows}
