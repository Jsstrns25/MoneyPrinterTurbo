"""SQLite transactions and a single-worker lock provide restartable local jobs."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from app.argstudio.models import Canon, Episode, Settings, merge_canon
from app.paths import runtime_root


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root else runtime_root() / "storage" / "argstudio"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "studio.sqlite3"
        with self.connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS series (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, premise TEXT NOT NULL,
                settings TEXT NOT NULL, canon TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, series_id TEXT NOT NULL REFERENCES series(id),
                slot TEXT NOT NULL, settings TEXT NOT NULL, story TEXT,
                UNIQUE(series_id, slot));
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(id),
                kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', due REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT,
                UNIQUE(episode_id, kind));
            CREATE TABLE IF NOT EXISTS costs (
                id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, estimate REAL NOT NULL,
                created REAL NOT NULL, note TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS publications (
                job_id TEXT PRIMARY KEY REFERENCES jobs(id), state TEXT NOT NULL,
                metadata TEXT NOT NULL, sha256 TEXT NOT NULL, session TEXT, video_id TEXT,
                error TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, table):
        if table not in {"series", "episodes", "jobs", "costs", "publications"}:
            raise ValueError("Invalid table")
        with self.connect() as db:
            # Upload session URLs are credentials and never go to the UI/logs.
            columns = (
                "job_id,state,metadata,sha256,video_id,error"
                if table == "publications"
                else "*"
            )
            return [dict(r) for r in db.execute(f"SELECT {columns} FROM {table}")]

    def series(self, series_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
            if not row:
                raise ValueError("Unknown series")
            return dict(row)

    def create_series(self, name, premise, settings: Settings):
        if not name.strip() or not premise.strip():
            raise ValueError("Name and premise are required")
        series_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO series(id,name,premise,settings,canon) VALUES(?,?,?,?,?)",
                (
                    series_id,
                    name,
                    premise,
                    settings.model_dump_json(),
                    Canon().model_dump_json(),
                ),
            )
        return series_id

    def enqueue(self, series_id, slot, due=None):
        """Same editorial slot is idempotent, including across process restarts."""
        if not slot.strip():
            raise ValueError("Editorial slot is required")
        series = self.series(series_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM episodes WHERE series_id=? AND slot=?",
                (series_id, slot),
            ).fetchone()
            if existing:
                return existing[0]
            eid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO episodes(id,series_id,slot,settings) VALUES(?,?,?,?)",
                (eid, series_id, slot, series["settings"]),
            )
            db.execute(
                "INSERT INTO jobs(id,episode_id,kind,due) VALUES(?,?,?,?)",
                (uuid.uuid4().hex, eid, "story", time.time() if due is None else due),
            )
            return eid

    def episode(self, eid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
            if not row:
                raise ValueError("Unknown episode")
            return dict(row)

    def require_previous_stories(self, eid):
        with self.connect() as db:
            current = db.execute(
                "SELECT e.series_id,j.due,j.rowid FROM episodes e JOIN jobs j ON j.episode_id=e.id WHERE e.id=? AND j.kind='story'",
                (eid,),
            ).fetchone()
            prior = db.execute(
                """SELECT e.id FROM episodes e JOIN jobs j ON j.episode_id=e.id
                WHERE e.series_id=? AND e.id<>? AND e.story IS NULL AND j.kind='story'
                AND (j.due<? OR (j.due=? AND j.rowid<?)) LIMIT 1""",
                (
                    current["series_id"],
                    eid,
                    current["due"],
                    current["due"],
                    current["rowid"],
                ),
            ).fetchone()
            if prior:
                raise ValueError(
                    "A previous episode has no story; recover it first to preserve continuity"
                )

    def reserve(self, eid, estimate, limit):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            total = db.execute(
                "SELECT coalesce(sum(estimate),0) FROM costs WHERE episode_id=?", (eid,)
            ).fetchone()[0]
            if estimate < 0 or total + estimate > limit + 1e-9:
                raise ValueError("Episode budget exhausted; no request sent")
            db.execute(
                "INSERT INTO costs VALUES(?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    eid,
                    estimate,
                    time.time(),
                    "Reserved estimate; retained after failures; actual billing unknown",
                ),
            )

    def save_story(self, eid, story: Episode, revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ep = db.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
            if ep["story"]:
                return
            series = db.execute(
                "SELECT * FROM series WHERE id=?", (ep["series_id"],)
            ).fetchone()
            if series["revision"] != revision:
                raise ValueError(
                    "Continuity changed; regenerate using the latest canon"
                )
            canon = merge_canon(
                Canon.model_validate_json(series["canon"]), story.canon_delta
            )
            db.execute(
                "UPDATE series SET canon=?,revision=revision+1 WHERE id=?",
                (canon.model_dump_json(), series["id"]),
            )
            db.execute(
                "UPDATE episodes SET story=? WHERE id=?", (story.model_dump_json(), eid)
            )
            for kind in ["long"] + [f"short-{i}" for i in range(len(story.shorts))]:
                db.execute(
                    "INSERT OR IGNORE INTO jobs(id,episode_id,kind,due) VALUES(?,?,?,?)",
                    (uuid.uuid4().hex, eid, kind, time.time()),
                )

    def recover(self):
        # Only call while holding the worker lock, never from a viewing UI.
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='pending',error='Recovered after worker interruption' WHERE state='running'"
            )

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE state='pending' AND due<=? ORDER BY due,rowid LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE jobs SET state='running', attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            return dict(row)

    def finish(self, jid, result=None, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state=?,result=?,error=? WHERE id=?",
                (
                    "failed" if error else "complete",
                    dumps(result) if result else None,
                    error,
                    jid,
                ),
            )

    def retry(self, jid):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE jobs SET state='pending',error=NULL WHERE id=? AND state='failed'",
                (jid,),
            ).rowcount
            if not changed:
                raise ValueError("Only failed jobs can be retried")
