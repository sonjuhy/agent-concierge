"""할 일(to-do) 저장소. 예약(리마인드)과 달리 '끝냈는지'를 기록한다.

Slack 대화로 추가·수정·완료·삭제하고, 원하면 알림을 걸 수 있다 (알림은 scheduler 의 todo 알림 잡).
번호(#id)는 사용자별이 아니라 전체 일련번호다. 다른 사람의 할 일은 owner 로 걸러 보이지도 바뀌지도 않는다.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime

from agent.context import KST

WEEKDAYS = "월화수목금토일"


@dataclass(frozen=True)
class Todo:
    id: int
    title: str
    due: str | None  # YYYY-MM-DD 또는 ISO 8601 (시각까지)
    status: str  # open | done
    created_at: float
    done_at: float | None
    remind_job: str | None

    @property
    def done(self) -> bool:
        return self.status == "done"

    def due_date(self) -> date | None:
        if not self.due:
            return None
        try:
            return datetime.fromisoformat(self.due).date()
        except ValueError:
            return None

    def overdue(self, today: date | None = None) -> bool:
        d = self.due_date()
        return bool(d and not self.done and d < (today or datetime.now(KST).date()))


class TodoStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS todos (
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT, title TEXT NOT NULL, due TEXT,
        status TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL, done_at REAL, remind_job TEXT
    );
    CREATE INDEX IF NOT EXISTS todos_owner ON todos(owner, status);
    """
    COLS = "id, title, due, status, created_at, done_at, remind_job"

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(self.SCHEMA)

    def add(self, title: str, due: str | None = None, owner: str | None = None) -> Todo:
        with self.db:
            cur = self.db.execute("INSERT INTO todos (owner, title, due, created_at) VALUES (?, ?, ?, ?)",
                                  (owner, title.strip(), due or None, time.time()))
        return self.get(cur.lastrowid, owner)  # type: ignore[return-value]

    def get(self, tid: int, owner: str | None = None) -> Todo | None:
        row = self.db.execute(f"SELECT {self.COLS} FROM todos WHERE id=? AND owner IS ?", (tid, owner)).fetchone()
        return Todo(*row) if row else None

    def list(self, owner: str | None = None, include_done: bool = False, done_limit: int = 10) -> list[Todo]:
        rows = self.db.execute(
            f"SELECT {self.COLS} FROM todos WHERE owner IS ? AND status='open' "
            "ORDER BY due IS NULL, due, id", (owner,)).fetchall()
        out = [Todo(*r) for r in rows]
        if include_done:
            rows = self.db.execute(
                f"SELECT {self.COLS} FROM todos WHERE owner IS ? AND status='done' ORDER BY done_at DESC LIMIT ?",
                (owner, done_limit)).fetchall()
            out += [Todo(*r) for r in rows]
        return out

    def update(self, tid: int, owner: str | None = None, *, title: str | None = None, due: str | None = None,
               clear_due: bool = False) -> Todo | None:
        t = self.get(tid, owner)
        if t is None:
            return None
        new_due = None if clear_due else (due or t.due)
        with self.db:
            self.db.execute("UPDATE todos SET title=?, due=? WHERE id=?", ((title or t.title).strip(), new_due, tid))
        return self.get(tid, owner)

    def set_status(self, tid: int, owner: str | None, done: bool) -> Todo | None:
        if self.get(tid, owner) is None:
            return None
        with self.db:
            self.db.execute("UPDATE todos SET status=?, done_at=? WHERE id=?",
                            ("done" if done else "open", time.time() if done else None, tid))
        return self.get(tid, owner)

    def set_remind_job(self, tid: int, job_id: str | None) -> None:
        with self.db:
            self.db.execute("UPDATE todos SET remind_job=? WHERE id=?", (job_id, tid))

    def delete(self, tid: int, owner: str | None = None) -> Todo | None:
        t = self.get(tid, owner)
        if t is not None:
            with self.db:
                self.db.execute("DELETE FROM todos WHERE id=?", (tid,))
        return t


def format_due(due: str | None) -> str:
    if not due:
        return ""
    try:
        d = datetime.fromisoformat(due)
    except ValueError:
        return due
    day = f"{d:%m/%d}({WEEKDAYS[d.weekday()]})"
    return f"{day} {d:%H:%M}" if "T" in due else day


def line(t: Todo, today: date | None = None) -> str:
    box = "☑" if t.done else "☐"
    due = format_due(t.due)
    flag = " ⚠️ 지남" if t.overdue(today) else ""
    bell = " 🔔" if t.remind_job and not t.done else ""
    title = f"~{t.title}~" if t.done else t.title
    return f"{box} `#{t.id}` {title}" + (f" — {due}" if due else "") + flag + bell


def format_list(todos: list[Todo], today: date | None = None) -> str:
    open_ = [t for t in todos if not t.done]
    done = [t for t in todos if t.done]
    if not todos:
        return "할 일이 없어요. `내일까지 보고서 제출 할 일 추가해줘` 처럼 말해 주세요."
    out = [f"*할 일 {len(open_)}개*"] + [line(t, today) for t in open_]
    if done:
        out += ["", f"_최근 완료 {len(done)}개_"] + [line(t, today) for t in done]
    out.append("\n_완료: `#3 했어` · 수정: `#3 금요일까지로 바꿔줘` · 알림: `#3 내일 9시에 알려줘`_")
    return "\n".join(out)
