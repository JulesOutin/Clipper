"""Gestion de la base de données SQLite — schéma, migrations, helpers CRUD."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Statuts possibles pour une vidéo
VIDEO_STATUSES = (
    "pending",
    "downloading",
    "downloaded",
    "transcribing",
    "transcribed",
    "selecting",
    "selected",
    "done",
    "error",
)

# Statuts possibles pour un clip
CLIP_STATUSES = (
    "candidate",
    "validated",
    "rejected",
    "cutting",
    "cut",
    "publishing",
    "published",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    title        TEXT,
    channel      TEXT,
    duration     INTEGER,
    status       TEXT NOT NULL DEFAULT 'pending',
    file_path    TEXT,
    audio_path   TEXT,
    metadata_json TEXT,
    transcript_path TEXT,
    error_msg    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id              TEXT PRIMARY KEY,
    video_id        TEXT NOT NULL REFERENCES videos(id),
    start_time      REAL NOT NULL,
    end_time        REAL NOT NULL,
    duration        REAL NOT NULL,
    subject         TEXT,
    score           INTEGER,
    reason          TEXT,
    status          TEXT NOT NULL DEFAULT 'candidate',
    title_selected  TEXT,
    titles_json     TEXT,
    description     TEXT,
    tags_json       TEXT,
    thumbnail_text  TEXT,
    clip_path       TEXT,
    thumbnail_path  TEXT,
    youtube_video_id TEXT,
    scheduled_at    TEXT,
    published_at    TEXT,
    views_2d        INTEGER,
    views_7d        INTEGER,
    watch_time_2d   REAL,
    watch_time_7d   REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id      TEXT NOT NULL REFERENCES clips(id),
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    error_msg    TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS costs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id     TEXT NOT NULL,
    service      TEXT NOT NULL,
    amount_usd   REAL NOT NULL,
    details_json TEXT,
    created_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> "Database":
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._apply_schema()
        return self

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Base non connectée — appeler connect() d'abord")
        return self._conn

    def _apply_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------ vidéos

    def get_video(self, video_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_video_by_url(self, url: str) -> dict | None:
        """Retrouve une vidéo par son URL exacte ou par son ID extrait de l'URL."""
        row = self.conn.execute(
            "SELECT * FROM videos WHERE url = ?", (url,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_video(self, video_id: str, url: str, **fields) -> dict:
        """Insère ou met à jour une vidéo. Retourne le dict final."""
        existing = self.get_video(video_id)
        now = _now()
        if existing:
            logger.debug("Vidéo %s déjà présente, mise à jour ignorée", video_id)
            return existing
        data = {
            "id": video_id,
            "url": url,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        self.conn.execute(
            f"INSERT INTO videos ({cols}) VALUES ({placeholders})",
            list(data.values()),
        )
        self.conn.commit()
        logger.info("Vidéo %s ajoutée en base", video_id)
        return data

    def update_video(self, video_id: str, **fields) -> None:
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE videos SET {set_clause} WHERE id = ?",
            [*fields.values(), video_id],
        )
        self.conn.commit()

    def list_videos(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM videos WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM videos ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_pending_videos(self) -> list[dict]:
        """Retourne les vidéos qui n'ont pas encore fini ou échoué."""
        rows = self.conn.execute(
            "SELECT * FROM videos WHERE status NOT IN ('done', 'error') ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- clips

    def insert_clip(self, video_id: str, **fields) -> dict:
        now = _now()
        data = {
            "id": _new_id(),
            "video_id": video_id,
            "status": "candidate",
            "created_at": now,
            "updated_at": now,
            **fields,
        }
        cols = ", ".join(data.keys())
        placeholders = ", ".join("?" for _ in data)
        self.conn.execute(
            f"INSERT INTO clips ({cols}) VALUES ({placeholders})",
            list(data.values()),
        )
        self.conn.commit()
        return data

    def get_clip(self, clip_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_clip(self, clip_id: str, **fields) -> None:
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE clips SET {set_clause} WHERE id = ?",
            [*fields.values(), clip_id],
        )
        self.conn.commit()

    def list_clips(self, video_id: str | None = None, status: str | None = None) -> list[dict]:
        query = "SELECT * FROM clips WHERE 1=1"
        params: list[Any] = []
        if video_id:
            query += " AND video_id = ?"
            params.append(video_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY score DESC, start_time ASC"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- coûts

    def record_cost(self, video_id: str, service: str, amount_usd: float, details: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO costs (video_id, service, amount_usd, details_json, created_at) VALUES (?,?,?,?,?)",
            (
                video_id,
                service,
                amount_usd,
                json.dumps(details) if details else None,
                _now(),
            ),
        )
        self.conn.commit()

    def get_costs(self, video_id: str | None = None) -> list[dict]:
        if video_id:
            rows = self.conn.execute(
                "SELECT * FROM costs WHERE video_id = ? ORDER BY created_at ASC",
                (video_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM costs ORDER BY created_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def total_cost(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(amount_usd), 0) FROM costs").fetchone()
        return float(row[0])

    # ----------------------------------------------------------- publish_queue

    def enqueue_clip(self, clip_id: str, scheduled_at: str) -> None:
        self.conn.execute(
            "INSERT INTO publish_queue (clip_id, scheduled_at, status, created_at) VALUES (?,?,?,?)",
            (clip_id, scheduled_at, "pending", _now()),
        )
        self.conn.commit()

    def list_queue(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM publish_queue WHERE status = ? ORDER BY scheduled_at ASC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM publish_queue ORDER BY scheduled_at ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_queue_item(self, item_id: int, **fields) -> None:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE publish_queue SET {set_clause} WHERE id = ?",
            [*fields.values(), item_id],
        )
        self.conn.commit()

    # ------------------------------------------------------- quota YouTube

    def count_uploads_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM publish_queue WHERE status = 'uploaded' AND DATE(created_at) = ?",
            (today,),
        ).fetchone()
        return int(row[0])


def open_db(storage_path: Path) -> Database:
    """Ouvre (ou crée) la base de données dans storage_path/clipper.db."""
    storage_path.mkdir(parents=True, exist_ok=True)
    db_path = storage_path / "clipper.db"
    db = Database(db_path)
    db.connect()
    return db
