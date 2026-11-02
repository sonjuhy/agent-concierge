"""Slack 스레드 ↔ CLI 세션 매핑과 호출 기록 (SQLite).

스레드 하나에 엔진별 세션을 따로 둔다 (agy 대화와 --deep claude 대화는 서로 이어 갈 수 없음).
untrusted 호출은 세션을 남기지 않고, 대신 스레드를 tainted 로 표시한다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from agent.cli_runner import CLIResult, Engine

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_key TEXT NOT NULL,
    engine     TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (thread_key, engine)
);
CREATE TABLE IF NOT EXISTS tainted_threads (
    thread_key TEXT PRIMARY KEY,
    reason     TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY,
    thread_key TEXT NOT NULL,
    who        TEXT NOT NULL,   -- user | bot
    text       TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_thread ON turns (thread_key, id);
CREATE TABLE IF NOT EXISTS conv_alias (
    channel TEXT NOT NULL,
    ts      TEXT NOT NULL,   -- 사용자·봇 메시지 ts
    conv_ts TEXT NOT NULL,   -- 그 메시지가 속한 대화
    PRIMARY KEY (channel, ts)
);
CREATE TABLE IF NOT EXISTS dm_active (
    channel TEXT PRIMARY KEY,
    conv_ts TEXT NOT NULL,
    last_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    thread_key TEXT,
    engine     TEXT NOT NULL,
    trusted    INTEGER NOT NULL,
    ok         INTEGER NOT NULL,
    timed_out  INTEGER NOT NULL,
    duration   REAL,
    usage      TEXT,
    cost_usd   REAL,
    error      TEXT
);
"""


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def get_session(self, thread_key: str, engine: Engine) -> str | None:
        row = self.db.execute(
            "SELECT session_id FROM sessions WHERE thread_key=? AND engine=?", (thread_key, engine.value)
        ).fetchone()
        return row[0] if row else None

    def set_session(self, thread_key: str, engine: Engine, session_id: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?) "
                "ON CONFLICT(thread_key, engine) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at",
                (thread_key, engine.value, session_id, time.time()),
            )

    def is_tainted(self, thread_key: str) -> bool:
        return self.db.execute("SELECT 1 FROM tainted_threads WHERE thread_key=?", (thread_key,)).fetchone() is not None

    def mark_tainted(self, thread_key: str, reason: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO tainted_threads VALUES (?, ?, ?)", (thread_key, reason, time.time())
            )

    # ---- 대화 단위 ----
    # Slack 스레드 = 대화였지만, DM 에서는 스레드 없이 입력창에 이어 쓰는 경우가 많다(실측).
    # 그래서 메시지마다 어느 대화에 속하는지(conv_alias)와 DM 의 마지막 활동(dm_active)을 기록한다.
    def conv_of(self, channel: str, ts: str) -> str | None:
        row = self.db.execute("SELECT conv_ts FROM conv_alias WHERE channel=? AND ts=?", (channel, ts)).fetchone()
        return row[0] if row else None

    def active_dm_conv(self, channel: str, window: float, now: float | None = None) -> str | None:
        row = self.db.execute("SELECT conv_ts, last_at FROM dm_active WHERE channel=?", (channel,)).fetchone()
        if row and (now or time.time()) - row[1] <= window:
            return row[0]
        return None

    def touch_conv(self, channel: str, ts: str | None, conv_ts: str, dm: bool) -> None:
        with self.db:
            if ts:
                self.db.execute("INSERT OR REPLACE INTO conv_alias VALUES (?, ?, ?)", (channel, ts, conv_ts))
            if dm:
                self.db.execute("INSERT OR REPLACE INTO dm_active VALUES (?, ?, ?)", (channel, conv_ts, time.time()))

    def add_turn(self, thread_key: str, who: str, text: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO turns (thread_key, who, text, ts) VALUES (?, ?, ?, ?)",
                            (thread_key, who, text[:2000], time.time()))

    def recent_turns(self, thread_key: str, limit: int = 6) -> list[tuple[str, str]]:
        """스레드의 최근 대화 (오래된 것부터). 플래너가 "그 날", "그 일정" 같은 표현을 풀 때 쓴다."""
        rows = self.db.execute(
            "SELECT who, text FROM turns WHERE thread_key=? ORDER BY id DESC LIMIT ?", (thread_key, limit)
        ).fetchall()
        return rows[::-1]

    def log_call(self, thread_key: str, trusted: bool, r: CLIResult) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO calls (ts, thread_key, engine, trusted, ok, timed_out, duration, usage, cost_usd, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), thread_key, r.engine.value, int(trusted), int(r.ok), int(r.timed_out),
                    r.duration, json.dumps(r.usage), r.cost_usd, (r.error or "")[:1000] or None,
                ),
            )
