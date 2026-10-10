from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any
from .models import Paper


class StateStore:
    """Transactional state; legacy JSON is imported once and preserved."""
    def __init__(self, path: str | Path):
        original = Path(path)
        self.path = original.with_suffix(".sqlite3") if original.suffix == ".json" else original
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, updated REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, canonical TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (scope TEXT, locator TEXT, updated REAL, data TEXT,
                PRIMARY KEY(scope, locator));
            CREATE TABLE IF NOT EXISTS pages (id TEXT PRIMARY KEY, updated REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY);
        """)
        if original != self.path and original.exists() and not self.db.execute(
                "SELECT 1 FROM migrations WHERE id='legacy_json'").fetchone():
            data = json.loads(original.read_text(encoding="utf-8"))
            with self.db:
                for key, value in data.get("papers", {}).items():
                    self.db.execute("INSERT OR IGNORE INTO state VALUES (?, ?)",
                                    (key, json.dumps(value, ensure_ascii=False)))
                self.db.execute("INSERT INTO migrations VALUES ('legacy_json')")

    def get(self, paper: Paper) -> dict[str, Any]:
        keys = [paper.stable_id, paper.doi.lower(), paper.source_id, paper.source_id.removeprefix("openreview:")]
        for key in keys:
            if key:
                key = self.canonical(key)
                row = self.db.execute("SELECT data FROM state WHERE id=?", (key,)).fetchone()
                if row:
                    return json.loads(row[0])
        return {}

    def canonical(self, key: str) -> str:
        visited = set()
        while key not in visited:
            visited.add(key)
            row = self.db.execute("SELECT canonical FROM aliases WHERE alias=?", (key,)).fetchone()
            if not row:
                return key
            key = row[0]
        return key

    def alias(self, paper: Paper, original: Paper) -> None:
        target = self.canonical(original.stable_id)
        if target != paper.stable_id:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?)", (paper.stable_id, target))

    def delete_page(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM pages WHERE id=?", (key,))

    def has(self, paper: Paper) -> bool:
        return bool(self.get(paper))

    def mark(self, paper: Paper, **values: Any) -> None:
        with self.db:
            data = {**self.get(paper), "title": paper.title, "venue": paper.venue,
                    "year": paper.year, **values}
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)",
                            (self.canonical(paper.stable_id), json.dumps(data, ensure_ascii=False)))

    def cached(self, key: str, ttl_hours: float, offline: bool = False) -> list[Paper] | None:
        row = self.db.execute("SELECT updated, data FROM snapshots WHERE id=?", (key,)).fetchone()
        if not row or (not offline and time.time()-row[0] >= ttl_hours*3600):
            return None
        return [Paper(**data) for data in json.loads(row[1])]

    def cache(self, key: str, papers: list[Paper]) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                            (key, time.time(), json.dumps([asdict(p) for p in papers], ensure_ascii=False)))

    def save_metadata(self, scope: str, paper: Paper) -> None:
        locator = paper.landing_url or paper.source_id or paper.stable_id
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?, ?, ?)",
                            (scope, locator, time.time(), json.dumps(asdict(paper), ensure_ascii=False)))

    def metadata(self, scope: str, locator: str, ttl_hours: float, offline=False):
        row = self.db.execute("SELECT updated, data FROM metadata WHERE scope=? AND locator=?", (scope, locator)).fetchone()
        if row and (offline or time.time()-row[0] < ttl_hours*3600):
            return Paper(**json.loads(row[1]))
        return None

    def partial(self, scope: str, ttl_hours: float, offline=False):
        rows = self.db.execute("SELECT updated, data FROM metadata WHERE scope=? ORDER BY rowid", (scope,))
        return [Paper(**json.loads(data)) for updated, data in rows if offline or time.time()-updated < ttl_hours*3600]

    def page(self, key: str, ttl_hours: float):
        row = self.db.execute("SELECT updated, data FROM pages WHERE id=?", (key,)).fetchone()
        return json.loads(row[1]) if row and time.time()-row[0] < ttl_hours*3600 else None

    def save_page(self, key: str, data) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO pages VALUES (?, ?, ?)",
                            (key, time.time(), json.dumps(data, ensure_ascii=False)))

    def close(self) -> None:
        self.db.close()


def filename_for(paper: Paper) -> str:
    digest = hashlib.sha256(paper.stable_id.encode()).hexdigest()[:12]
    title = "".join(char if char.isalnum() else "_" for char in paper.title).strip("_")
    return f"{title[:100]}_{digest}.pdf"
