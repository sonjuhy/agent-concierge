"""색인 상태 DB (SQLite). 원본 경로별 해시·상태를 기록해 바뀐 파일만 다시 처리한다."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    path        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    shadow      TEXT,
    status      TEXT NOT NULL,   -- ok | needs_ocr | unsupported | error
    error       TEXT,
    cost_usd    REAL,
    updated_at  REAL NOT NULL
);
"""


@dataclass(frozen=True)
class DocRow:
    path: str
    sha256: str
    size: int
    mtime: float
    shadow: str | None
    status: str
    error: str | None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class HashDB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def get(self, path: Path) -> DocRow | None:
        row = self.db.execute(
            "SELECT path, sha256, size, mtime, shadow, status, error FROM docs WHERE path=?", (str(path),)
        ).fetchone()
        return DocRow(*row) if row else None

    def upsert(
        self,
        path: Path,
        sha256: str,
        size: int,
        mtime: float,
        status: str,
        shadow: Path | None = None,
        error: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "sha256=excluded.sha256, size=excluded.size, mtime=excluded.mtime, shadow=excluded.shadow, "
                "status=excluded.status, error=excluded.error, cost_usd=excluded.cost_usd, updated_at=excluded.updated_at",
                (str(path), sha256, size, mtime, str(shadow) if shadow else None, status, error, cost_usd, time.time()),
            )

    def touch(self, path: Path, size: int, mtime: float) -> None:
        """내용(해시)은 같고 mtime 만 바뀐 경우."""
        with self.db:
            self.db.execute("UPDATE docs SET size=?, mtime=? WHERE path=?", (size, mtime, str(path)))

    def delete(self, path: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM docs WHERE path=?", (path,))

    def paths(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT path FROM docs")}

    def stats(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status, count(*) FROM docs GROUP BY status").fetchall())
