"""Slack 대화를 Vault 에 스레드별 마크다운으로 백업한다 (Slack 무료 플랜의 90일 노출·1년 삭제 대비).

- 위치: <Vault>/_Chat_Logs/YYYY/MM/YYYY-MM-DD_HHMM_<첫 질문>.md — Python 데몬만 쓴다.
  (_Agent_Inbox 가 아닌 이유: agy 가 쓸 수 있는 유일한 폴더라 인젝션된 agy 가 기록을 고칠 수 있음)
- 오염(tainted)된 스레드는 _Chat_Logs/_untrusted/ 로 옮긴다. 외부 텍스트가 섞인 기록이 나중에 agy(도구 있음)의
  입력으로 다시 들어가지 않도록, agy 프로필은 이 폴더 읽기를 금지한다.
- 스레드 ↔ 파일 매핑은 sessions DB(chat_logs 테이블)에 둔다.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path

from agent.context import KST

LOG_DIR = "_Chat_Logs"
UNTRUSTED_DIR = "_untrusted"
TITLE_MAX = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_logs (
    thread_key TEXT PRIMARY KEY,
    path       TEXT NOT NULL
);
"""


def slack_time(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), KST)


def make_title(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else "대화"
    title = re.sub(r'[\\/:*?"<>|#^\[\]`]', "", first)
    title = re.sub(r"\s+", " ", title).strip()
    return (title[:TITLE_MAX].rstrip() or "대화")


class ChatLog:
    def __init__(self, vault: Path, db: sqlite3.Connection):
        self.root = vault / LOG_DIR
        self.db = db
        self.db.executescript(SCHEMA)

    def path_for(self, thread_key: str) -> Path | None:
        row = self.db.execute("SELECT path FROM chat_logs WHERE thread_key=?", (thread_key,)).fetchone()
        return Path(row[0]) if row else None

    def _create(self, thread_key: str, started: datetime, first_text: str, tainted: bool) -> Path:
        base = self.root / (UNTRUSTED_DIR if tainted else "") / f"{started:%Y}" / f"{started:%m}"
        title = make_title(first_text)
        stem = f"{started:%Y-%m-%d_%H%M}_{title}"
        path = base / f"{stem}.md"
        n = 2
        while path.exists():
            path = base / f"{stem}-{n}.md"
            n += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._header(thread_key, started, title, tainted), encoding="utf-8")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO chat_logs VALUES (?, ?)", (thread_key, str(path)))
        return path

    @staticmethod
    def _header(thread_key: str, started: datetime, title: str, tainted: bool) -> str:
        return (
            "---\n"
            "type: chat_log\n"
            f"thread: \"{thread_key}\"\n"
            f"started: {started:%Y-%m-%d %H:%M}\n"
            f"tainted: {'true' if tainted else 'false'}\n"
            "---\n"
            f"# {title}\n"
        )

    def append(self, thread_key: str, ts: str, who: str, text: str, tainted: bool = False) -> Path:
        """메시지 한 건을 스레드 파일 끝에 붙인다. 스레드가 오염되면 파일을 _untrusted 로 옮긴다."""
        when = slack_time(ts)
        path = self.path_for(thread_key)
        if path is None or not path.exists():
            path = self._create(thread_key, when, text, tainted)
        elif tainted and UNTRUSTED_DIR not in path.relative_to(self.root).parts:
            path = self._quarantine(thread_key, path)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n### {when:%H:%M} · {who}\n\n{text.rstrip()}\n")
        return path

    def _quarantine(self, thread_key: str, path: Path) -> Path:
        rel = path.relative_to(self.root)
        dest = self.root / UNTRUSTED_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text(encoding="utf-8").replace("tainted: false", "tainted: true", 1)
        dest.write_text(content, encoding="utf-8")
        path.unlink()
        with self.db:
            self.db.execute("UPDATE chat_logs SET path=? WHERE thread_key=?", (str(dest), thread_key))
        return dest
