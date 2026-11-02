"""예약 작업: 아침 브리핑, 시각·반복 알림, 메일 감시 (APScheduler + SQLite 잡스토어).

주기적 감시는 AI 가 아니라 Python 이 한다. AI(플래너)는 Slack 요청에서 미리 정한 템플릿의 파라미터(JSON)만
뽑고, 등록·실행은 여기서 한다. 백그라운드 루프에서 CLI 를 반복 호출하지 않으므로 계정 차단 위험이 없다.

템플릿:
- briefing (id=daily_briefing[:<이름>]): 매일 HH:MM — (소유자) 헬스체크 경고 + 오늘 일정 + 안 읽은 메일 요약
  (가족) 오늘 일정 + 안 읽은 메일 목록 (LLM 없음)
- reminder / repeat: 지정 시각 1회 / 반복 — "⏰ 알림: …"
- mail_watch: N분마다 Gmail 검색식의 새 메일을 발신자·제목만 알림 (LLM 미사용)

멀티 사용자: 각 잡은 kwargs["user"] 에 주인 이름을 갖고, 그 사람의 DM 으로 보낸다. user 가 없는 잡(멀티 사용자
이전에 만든 것 포함)은 소유자 것이다. 잡 함수는 피클 가능한 모듈 함수여야 해서, 실행에 필요한 객체는 RUNTIME 에 둔다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from agent import health
from agent.cli_runner import run_cli
from agent.config import Settings
from agent.context import KST
from agent.router import ExternalContent, build_request
from agent.sessions import Store
from agent import todos as todo_mod
from agent.tools import google
from agent.tools.google_auth import NotAuthorized
from agent.users import OWNER_GOOGLE_TOKEN, User, Users

log = logging.getLogger(__name__)

BRIEFING_ID = "daily_briefing"
DEFAULT_BRIEFING_TIME = "07:30"
MIN_WATCH_MINUTES = 5

SEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_watch_seen (job_id TEXT NOT NULL, msg_id TEXT NOT NULL, PRIMARY KEY (job_id, msg_id));
"""


@dataclass
class Runtime:
    settings: Settings
    client: Any  # Slack AsyncWebClient
    store: Store
    users: Users
    dm_channels: dict[str, str] = field(default_factory=dict)
    apis: dict[str, Any] = field(default_factory=dict)  # 사용자 이름 → Google 호출 (소유자는 google 모듈)

    def user(self, name: str | None) -> User:
        return self.users.by_name(name) or self.users.owner

    def api(self, name: str | None) -> Any:
        u = self.user(name)
        if u.name not in self.apis:
            self.apis[u.name] = google if u.is_owner else google.GoogleApi(u.google_token)
        return self.apis[u.name]


RUNTIME: Runtime | None = None


def _rt() -> Runtime:
    if RUNTIME is None:
        raise RuntimeError("scheduler runtime not initialized")
    return RUNTIME


def owner_of(job) -> str:
    """잡의 주인 이름 (kwargs 에 없으면 소유자)."""
    return job.kwargs.get("user") or _rt().users.owner.name


def jobs_of(sched: AsyncIOScheduler | None, user: str | None) -> list:
    """user 의 잡만. user=None 이면 전부 (1인 구성)."""
    if sched is None:
        return []
    jobs = sched.get_jobs()
    return jobs if user is None else [j for j in jobs if owner_of(j) == user]


def _kw(user: str | None) -> dict:
    return {"user": user} if user else {}


async def _dm(user: str | None) -> str:
    rt = _rt()
    u = rt.user(user)
    if u.name not in rt.dm_channels:
        resp = await rt.client.conversations_open(users=u.slack_id)
        rt.dm_channels[u.name] = resp["channel"]["id"]
    return rt.dm_channels[u.name]


async def _post(text: str, tainted_reason: str | None = None, user: str | None = None,
                extra_blocks: list[dict] | None = None) -> None:
    rt = _rt()
    channel = await _dm(user)
    resp = await rt.client.chat_postMessage(channel=channel, text=text[:3000],
                                            blocks=[{"type": "markdown", "text": text[:11000]}, *(extra_blocks or [])])
    if tainted_reason:
        # 외부 텍스트(메일)가 섞인 메시지의 스레드에서 이어지는 대화는 도구 없는 경로로만
        rt.store.mark_tainted(f"{channel}:{resp['ts']}", tainted_reason)


# ---- 잡 함수 (APScheduler 가 호출) ----
async def job_briefing(user: str | None = None) -> None:
    rt = _rt()
    u = rt.user(user)
    api = rt.api(user)
    now = datetime.now(KST)
    parts = [f"## ☀️ {now:%m/%d}({google.WEEKDAYS[now.weekday()]}) 아침 브리핑"]

    checks, bad = [], []
    if u.is_owner:  # 서버 상태 점검은 관리자(소유자)에게만
        checks = await health.run_all(rt.settings, rt.users)
        bad = [c for c in checks if not c.ok]
        if bad:
            parts.append("### ⚠️ 점검 필요\n" + health.format_checks(bad))

    tainted = None
    try:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        events = await asyncio.to_thread(api.list_events, start, start + timedelta(days=1))
        parts.append("### 오늘 일정\n" + google.format_events(events, start, start + timedelta(days=1)))
        mails = await asyncio.to_thread(api.list_mail, "is:unread newer_than:1d", 15)
        if mails and u.is_owner:
            listing = "\n".join(f"- {m.sender} | {m.subject} | {m.snippet}" for m in mails)
            _, req = build_request(rt.settings, "지난 하루 동안 온 안 읽은 메일 중 중요해 보이는 것을 3줄 이내로 요약해줘.",
                                   external=[ExternalContent("gmail:briefing", listing)])
            r = await run_cli(req, binary=rt.settings.claude_bin)
            parts.append(f"### 안 읽은 메일 {len(mails)}통\n" + (r.text if r.ok else f"요약 실패: {r.error}"))
            tainted = "briefing mail"
        elif mails:  # 가족: AI 가 메일을 읽지 않고 목록만
            parts.append(f"### 안 읽은 메일 {len(mails)}통\n" + "\n".join(f"- {m.sender} — {m.subject}" for m in mails))
            tainted = "briefing mail"
        else:
            parts.append("### 안 읽은 메일\n지난 하루 동안 없음")
    except NotAuthorized as e:
        parts.append(f"### Google\n연결 안 됨: {e}")

    if section := todo_section(user):
        parts.append(section)

    if u.is_owner:
        parts.append("### 시스템\n" + ("모두 정상" if not bad else f"{len(checks) - len(bad)}/{len(checks)} 정상"))
    await _post("\n\n".join(parts), tainted, user)


def todo_section(user: str | None) -> str | None:
    """브리핑용: 지난 것·오늘까지인 할 일을 먼저, 나머지는 개수만."""
    items = todo_mod.TodoStore(_rt().store.db).list(_rt().user(user).name)
    if not items:
        return None
    today = datetime.now(KST).date()
    urgent = [t for t in items if (d := t.due_date()) and d <= today]
    rest = len(items) - len(urgent)
    lines = [todo_mod.line(t, today) for t in urgent] or ["오늘까지인 할 일 없음"]
    if rest:
        lines.append(f"그 밖에 {rest}개 (`할 일 보여줘`)")
    return "### 할 일\n" + "\n".join(lines)


def todo_done_blocks(tid: int) -> list[dict]:
    return [{"type": "actions", "elements": [
        {"type": "button", "action_id": "todo_done", "value": str(tid), "style": "primary",
         "text": {"type": "plain_text", "text": "완료"}}]}]


async def job_todo_reminder(todo_id: int, user: str | None = None) -> None:
    """할 일 알림. 그사이 끝냈거나 지웠으면 보내지 않는다."""
    rt = _rt()
    t = todo_mod.TodoStore(rt.store.db).get(todo_id, rt.user(user).name)
    if t is None or t.done:
        return
    due = todo_mod.format_due(t.due)
    await _post(f"📝 할 일 알림: *{t.title}*" + (f" (기한 {due})" if due else "") + f" `#{t.id}`",
                user=user, extra_blocks=todo_done_blocks(t.id))


async def job_reminder(text: str, user: str | None = None) -> None:
    await _post(f"⏰ 알림: {text}", user=user)


async def job_mail_watch(query: str, label: str, user: str | None = None) -> None:
    rt = _rt()
    db: sqlite3.Connection = rt.store.db
    db.executescript(SEEN_SCHEMA)
    job_id = _watch_id(query, user)
    mails = await asyncio.to_thread(rt.api(user).list_mail, query, 20)
    seen = {r[0] for r in db.execute("SELECT msg_id FROM mail_watch_seen WHERE job_id=?", (job_id,))}
    first_run = not seen
    new = [m for m in mails if m.id not in seen]
    with db:
        db.executemany("INSERT OR IGNORE INTO mail_watch_seen VALUES (?, ?)", [(job_id, m.id) for m in mails])
    if first_run or not new:  # 첫 실행은 기존 메일을 기준선으로만 기록
        return
    lines = "\n".join(f"- {m.sender} — {m.subject}" for m in new)
    await _post(f"📬 새 메일 ({label}) {len(new)}통\n{lines}", tainted_reason="mail watch", user=user)


# ---- 등록·조회 ----
def create(settings: Settings, client: Any, store: Store, users: Users | str,
           jobs_db: Path | None = None) -> AsyncIOScheduler:
    """jobs_db 를 주면 SQLite 에 예약을 저장해 재시작 후에도 유지한다 (테스트는 메모리).
    users: 사용자 목록 (하위 호환으로 소유자 Slack ID 문자열도 받는다)."""
    global RUNTIME
    if isinstance(users, str):
        users = Users((User("owner", users, "owner", settings.vault, settings.agy_home, OWNER_GOOGLE_TOKEN),))
    RUNTIME = Runtime(settings, client, store, users)
    jobstores = {"default": SQLAlchemyJobStore(url=f"sqlite:///{jobs_db}")} if jobs_db else {}
    return AsyncIOScheduler(jobstores=jobstores, timezone=KST,
                            job_defaults={"coalesce": True, "misfire_grace_time": 3600})


def _briefing_id(user: str | None) -> str:
    if user is None or user == _rt().users.owner.name:
        return BRIEFING_ID  # 소유자 브리핑 id 는 멀티 사용자 이전과 같다
    return f"{BRIEFING_ID}:{user}"


def ensure_briefing(sched: AsyncIOScheduler, hhmm: str = DEFAULT_BRIEFING_TIME, user: str | None = None) -> None:
    if sched.get_job(_briefing_id(user)) is None:
        set_briefing(sched, hhmm, user)


def set_briefing(sched: AsyncIOScheduler, hhmm: str, user: str | None = None) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    owner = user if user and user != _rt().users.owner.name else None
    sched.add_job(job_briefing, CronTrigger(hour=h, minute=m, timezone=KST), id=_briefing_id(user),
                  kwargs=_kw(owner), name=f"아침 브리핑 매일 {h:02d}:{m:02d}", replace_existing=True)
    return f"아침 브리핑을 매일 {h:02d}:{m:02d}에 보내드릴게요."


def add_reminder(sched: AsyncIOScheduler, when: datetime, text: str, user: str | None = None) -> str:
    if when <= datetime.now(KST):
        raise ValueError("이미 지난 시각입니다.")
    jid = f"reminder:{uuid.uuid4().hex[:8]}"
    sched.add_job(job_reminder, DateTrigger(run_date=when), args=[text], kwargs=_kw(user), id=jid,
                  name=f"{when:%m/%d %H:%M} 알림: {text[:40]}")
    return jid


def add_todo_reminder(sched: AsyncIOScheduler, when: datetime, todo_id: int, title: str,
                      user: str | None = None) -> str:
    if when <= datetime.now(KST):
        raise ValueError("이미 지난 시각입니다.")
    jid = f"todo:{todo_id}:{uuid.uuid4().hex[:6]}"
    sched.add_job(job_todo_reminder, DateTrigger(run_date=when), args=[todo_id], kwargs=_kw(user), id=jid,
                  name=f"{when:%m/%d %H:%M} 할 일 알림: {title[:40]}")
    return jid


def cancel_job(sched: AsyncIOScheduler | None, job_id: str | None) -> None:
    if sched is not None and job_id and sched.get_job(job_id):
        sched.remove_job(job_id)


REPEATS = ("daily", "weekdays", "weekends", "weekly", "monthly")
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def describe_repeat(repeat: str, weekdays: list[str] | None = None, day: int | None = None) -> str:
    if repeat == "daily":
        return "매일"
    if repeat == "weekdays":
        return "평일"
    if repeat == "weekends":
        return "주말"
    if repeat == "weekly":
        return "매주 " + "·".join(google.WEEKDAYS[WEEKDAY_KEYS.index(d)] for d in weekdays or []) + "요일"
    return f"매월 {day}일"


def add_recurring(sched: AsyncIOScheduler, hhmm: str, repeat: str, text: str,
                  weekdays: list[str] | None = None, day: int | None = None, user: str | None = None) -> str:
    """반복 알림. repeat: daily | weekdays | weekends | weekly(weekdays 필요) | monthly(day 필요)."""
    h, m = (int(x) for x in hhmm.split(":"))
    if repeat not in REPEATS:
        raise ValueError(f"알 수 없는 반복: {repeat}")
    days = [d for d in WEEKDAY_KEYS if d in (weekdays or [])]  # 요일 순서로 정리
    if repeat == "weekly" and not days:
        raise ValueError("어느 요일에 알릴지 알려 주세요.")
    if repeat == "monthly" and not (day and 1 <= day <= 31):
        raise ValueError("매월 며칠에 알릴지 알려 주세요.")
    dow = {"weekdays": "mon-fri", "weekends": "sat,sun", "weekly": ",".join(days)}.get(repeat)
    trigger = CronTrigger(hour=h, minute=m, day_of_week=dow, day=day if repeat == "monthly" else None, timezone=KST)
    label = describe_repeat(repeat, days, day)
    jid = f"repeat:{uuid.uuid4().hex[:8]}"
    sched.add_job(job_reminder, trigger, args=[text], kwargs=_kw(user), id=jid,
                  name=f"{label} {h:02d}:{m:02d} 알림: {text[:40]}")
    return jid


def _watch_id(query: str, user: str | None) -> str:
    if user is None or user == _rt().users.owner.name:
        return f"mailwatch:{query}"
    return f"mailwatch:{user}:{query}"


def add_mail_watch(sched: AsyncIOScheduler, query: str, minutes: int, label: str, user: str | None = None) -> str:
    minutes = max(MIN_WATCH_MINUTES, minutes)
    jid = _watch_id(query, user)
    sched.add_job(job_mail_watch, IntervalTrigger(minutes=minutes), args=[query, label], kwargs=_kw(user), id=jid,
                  name=f"메일 감시 {minutes}분마다: {label}", replace_existing=True,
                  next_run_time=datetime.now(KST))
    return jid


def list_jobs(sched: AsyncIOScheduler, user: str | None = None) -> str:
    jobs = jobs_of(sched, user)
    if not jobs:
        return "등록된 예약 작업이 없습니다."
    lines = ["*예약 작업*"]
    for j in jobs:
        nxt = f"{j.next_run_time.astimezone(KST):%m/%d %H:%M}" if j.next_run_time else "-"
        lines.append(f"- `{j.id}` {j.name} (다음 {nxt})")
    return "\n".join(lines)


def remove(sched: AsyncIOScheduler, job_id: str, user: str | None = None) -> bool:
    """본인 잡만 지울 수 있다 (user=None 이면 1인 구성)."""
    job = sched.get_job(job_id)
    if job is None or (user is not None and owner_of(job) != user):
        return False
    sched.remove_job(job_id)
    return True
