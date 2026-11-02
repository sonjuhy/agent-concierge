"""일정·메일 요청 처리: 키워드 1차 필터 → 플래너(도구 없는 claude, JSON) → Python 이 Google API 실행.

- 일정 조회: Python 이 목록을 만들어 바로 답한다 (LLM 미사용, 오염 없음)
- 메일 조회: 제목·발신자·미리보기를 외부 텍스트로 감싸 도구 없는 claude 가 요약한다 → 스레드 오염 표시
- 일정 추가 / 메일 초안: Slack [승인] 버튼을 누른 뒤에만 실행한다 (pending_actions 테이블, 24시간 유효)
플래너 입력은 사용자 메시지뿐이지만, 출력도 도구 없는 claude 에서 JSON 으로만 받는다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from agent import scheduler
from agent.cli_runner import CLIRequest, CLIResult, Engine
from agent.config import Settings
from agent.context import KST, build_context
from agent.router import ExternalContent, build_request
from agent import todos as todo_mod
from agent.tools import google
from agent.tools.google_auth import NotAuthorized

log = logging.getLogger(__name__)

Runner = Callable[[CLIRequest, str], Awaitable[CLIResult]]

INTENT_RE = re.compile(
    r"일정|약속|캘린더|스케[줄쥴]|회의|미팅|메일|이메일|gmail|초안|답장|받은\s*편지|calendar|schedule|mail"
    r"|알림|리마인|브리핑|예약|감시|모니터링|분마다|시간마다|(?:에|라고)\s*알려"
    r"|쇼핑|최저가|가격\s*알림|구매|사려고|살만한|만\s*원\s*(?:이하|아래|미만|대)"
    r"|할\s*일|투두|to-?do|체크\s*리스트|완료|끝냈|다\s*했|#\d+"
    # 키워드 없이 시각만 있는 부탁("7시 15분에 약 챙기라고 해줘")도 플래너로. 아니면 플래너가 other 로 돌려보낸다
    r"|\d{1,2}\s*시(?!간)|\d{1,2}:\d{2}|(?:라고|하라고|도록)\s*(?:해|말해|얘기해|전해)\s*(?:줘|주세요|줄래)"
    r"|깨워|챙기라고|잊지\s*않게|까먹지\s*않게",
    re.I,
)
INTENTS = (
    "calendar_list", "calendar_create", "email_list", "email_draft",
    "reminder_add", "mail_watch_add", "schedule_list", "schedule_cancel", "briefing_time",
    "shopping", "todo_add", "todo_list", "todo_update", "todo_done", "todo_reopen", "todo_delete", "other",
)
PENDING_TTL = 24 * 3600
AGY_PLANNER_MODEL = "gemini-3.8-flash-low"  # 실측 약 11초 (기본 모델은 52초)
# 대화 기록의 답한 쪽 표시 중 이 모듈이 만든 것 (Reply.meta / 승인 결과)
SHOPPING_UNSUPPORTED = (
    "쇼핑 검색·가격 알림은 지원하지 않아요. 네이버 쇼핑 검색 API 는 2026-07-31 에 종료됐고, "
    "쿠팡 파트너스 API 는 판매 실적 승인이 필요해요. 쇼핑 앱에서 직접 찾아 주세요."
)
WORKSPACE_SOURCES = ("calendar", "gmail", "scheduler", "workspace", "todo", "승인", "취소")

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "range_start": {"type": "string", "description": "calendar_list: 조회 시작 ISO 8601 (+09:00)"},
        "range_end": {"type": "string", "description": "calendar_list: 조회 끝 ISO 8601 (+09:00)"},
        "event_summary": {"type": "string"},
        "event_start": {"type": "string", "description": "calendar_create: ISO 8601 (+09:00)"},
        "event_end": {"type": "string", "description": "calendar_create: 없으면 시작 1시간 뒤"},
        "event_location": {"type": "string"},
        "email_query": {"type": "string", "description": "email_list: Gmail 검색식 (예: is:unread newer_than:7d)"},
        "draft_to": {"type": "string"},
        "draft_subject": {"type": "string"},
        "draft_body": {"type": "string", "description": "email_draft: 사용자가 요청한 내용으로 작성한 본문"},
        "remind_at": {"type": "string", "description": "reminder_add: 알림 시각 ISO 8601 (+09:00)"},
        "remind_text": {"type": "string", "description": "reminder_add: 알려 줄 내용"},
        "repeat": {"type": "string", "enum": ["none", *scheduler.REPEATS],
                   "description": "reminder_add: 반복 (매일=daily, 평일=weekdays, 주말=weekends, "
                                  "매주 특정 요일=weekly, 매월 특정 날짜=monthly, 한 번=none)"},
        "repeat_weekdays": {"type": "array", "items": {"type": "string", "enum": list(scheduler.WEEKDAY_KEYS)},
                            "description": "repeat=weekly: 요일들"},
        "repeat_day": {"type": "integer", "description": "repeat=monthly: 날짜(1~31)"},
        "watch_minutes": {"type": "integer", "description": "mail_watch_add: 확인 주기(분), 기본 30"},
        "watch_label": {"type": "string", "description": "mail_watch_add: 짧은 이름 (예: 팀장님 메일)"},
        "job_id": {"type": "string", "description": "schedule_cancel: [등록된 예약] 목록에서 고른 작업 ID"},
        "briefing_time": {"type": "string", "description": "briefing_time: HH:MM (24시간)"},
        "todo_title": {"type": "string", "description": "todo_add: 할 일 내용 / todo_update: 바꿀 새 내용 (안 바꾸면 비움)"},
        "todo_due": {"type": "string", "description": "todo_add·todo_update: 기한. 날짜만이면 YYYY-MM-DD, 시각까지면 ISO 8601 (+09:00)"},
        "todo_clear_due": {"type": "boolean", "description": "todo_update: 기한을 없앤다"},
        "todo_ids": {"type": "array", "items": {"type": "integer"},
                     "description": "todo_update·todo_done·todo_reopen·todo_delete: [할 일] 목록의 번호(#)들"},
        "todo_remind_at": {"type": "string", "description": "todo_add·todo_update: 이 할 일을 알려 줄 시각 ISO 8601 (+09:00)"},
        "todo_show_done": {"type": "boolean", "description": "todo_list: 완료한 것도 보여 달라고 했으면 true"},
    },
    "required": ["intent"],
}

PLANNER_SYSTEM = (
    "You convert a Korean user's request to a JSON plan for calendar/email actions. "
    "Use the provided date table to resolve relative dates; all times are KST (+09:00). "
    "Use 'calendar_*' only for Google Calendar events and 'email_*' only for Gmail messages. "
    "reminder_add: notify the user at a time (\"3시에 약 먹으라고 알려줘\"); set repeat for recurring reminders "
    "(\"매일 아침 8시에\", \"평일 9시마다\", \"매주 월요일\", \"매달 25일\") and put the time of the first run in remind_at. "
    "schedule_cancel: pick job_id from the [등록된 예약] list by matching the user's description. "
    "mail_watch_add: periodically watch Gmail for new matching mail (email_query = Gmail search, e.g. from:x@y.com). "
    "schedule_list / schedule_cancel: list or cancel registered reminders, mail watches and the briefing. "
    "briefing_time: change the daily morning briefing time. "
    "shopping: finding products to buy, comparing prices or price-drop alerts. "
    "todo_*: the user's to-do list (할 일) — things to check off, unlike reminder_add which only notifies. "
    "todo_add: add a task (\"내일까지 보고서 제출 할 일 추가\"); if the user also wants a notification for it, "
    "set todo_remind_at instead of using reminder_add. "
    "todo_list / todo_update / todo_done (\"#3 했어\", \"보고서 끝냈어\") / todo_reopen / todo_delete: "
    "pick todo_ids from the [할 일] list by number or by matching the description. "
    "Searching notes, documents, files or meeting minutes (회의록, 노트, 문서, 자료) is 'other', "
    "as is anything else that is not viewing/adding calendar events or reading/drafting email. "
    "Never invent recipients: leave draft_to empty if no address is given."
)


@dataclass
class Plan:
    intent: str
    data: dict = field(default_factory=dict)


@dataclass
class Reply:
    text: str
    ok: bool = True
    tainted: bool = False
    meta: str | None = None
    blocks: list[dict] | None = None  # 승인 버튼 등


def wants_workspace(text: str) -> bool:
    return bool(INTENT_RE.search(text))


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
    except ValueError:
        return None
    return d.replace(tzinfo=KST) if d.tzinfo is None else d.astimezone(KST)


class PendingStore:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS pending_actions (
        id TEXT PRIMARY KEY, thread_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL, created_at REAL NOT NULL
    );
    """

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.executescript(self.SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pending_actions)")}
        if "owner" not in cols:  # 멀티 사용자 이전에 만든 DB
            with self.db:
                self.db.execute("ALTER TABLE pending_actions ADD COLUMN owner TEXT")

    def add(self, thread_key: str, kind: str, payload: dict, owner: str | None = None) -> str:
        aid = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute(
                "INSERT INTO pending_actions (id, thread_key, kind, payload, status, created_at, owner) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (aid, thread_key, kind, json.dumps(payload, ensure_ascii=False), time.time(), owner),
            )
        return aid

    def take(self, aid: str, status: str, owner: str | None = None) -> tuple[str, str, dict] | None:
        """대기 중이고 만료되지 않은 작업을 한 번만 꺼낸다 (버튼 중복 클릭 방지). 요청한 본인만 꺼낼 수 있다."""
        row = self.db.execute(
            "SELECT thread_key, kind, payload, status, created_at, owner FROM pending_actions WHERE id=?", (aid,)
        ).fetchone()
        if not row or row[3] != "pending" or (row[5] is not None and row[5] != owner):
            return None
        new = "expired" if time.time() - row[4] > PENDING_TTL else status
        with self.db:
            self.db.execute("UPDATE pending_actions SET status=? WHERE id=? AND status='pending'", (new, aid))
        return (row[0], row[1], json.loads(row[2])) if new == status else None


def approval_blocks(aid: str, title: str, lines: list[str]) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n" + "\n".join(lines)}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "pending_approve", "value": aid, "style": "primary",
             "text": {"type": "plain_text", "text": "승인"}},
            {"type": "button", "action_id": "pending_cancel", "value": aid,
             "text": {"type": "plain_text", "text": "취소"}},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "승인해야 실행됩니다 · 24시간 뒤 만료"}]},
    ]


class Workspace:
    def __init__(self, settings: Settings, pending: PendingStore, run: Runner, api=google, sched=None,
                 owner: str | None = None, planner: str = "claude", mail_mode: str = "summary"):
        """owner: 사용자 이름 (승인·예약의 주인). planner: claude(도구 없음) | agy(가족 — 자기 계정, --json-schema).
        mail_mode: summary(도구 없는 claude 요약) | list(발신자·제목만, LLM 없음 — 가족)."""
        self.settings = settings
        self.pending = pending
        self.run = run
        self.api = api
        self.sched = sched  # APScheduler (None 이면 예약 기능 꺼짐)
        self.owner = owner
        self.planner = planner
        self.mail_mode = mail_mode
        self.todos = todo_mod.TodoStore(pending.db)

    async def plan(self, text: str, history: list[tuple[str, str]] | None = None) -> Plan:
        convo = ""
        if history:
            lines = [f"{'나' if who == 'user' else '봇'}: {t[:300]}" for who, t in history[-6:]]
            convo = "[이전 대화 — 날짜·대상이 생략된 요청은 이 맥락으로 해석]\n" + "\n".join(lines) + "\n\n"
        jobs = ""
        mine = scheduler.jobs_of(self.sched, self.owner) if self.sched is not None else []
        if mine:
            # "약 알림 취소해줘" 처럼 이름으로 취소할 수 있게 본인 예약 목록을 함께 준다
            jobs = "[등록된 예약]\n" + "\n".join(f"- {j.id}: {j.name}" for j in mine) + "\n\n"
        items = self.todos.list(self.owner, include_done=True, done_limit=5)
        tasks = ("[할 일]\n" + "\n".join(f"- #{t.id}: {t.title}" + (f" (기한 {t.due})" if t.due else "")
                                        + (" [완료]" if t.done else "") for t in items) + "\n\n") if items else ""
        prompt = f"{build_context()}\n\n{jobs}{tasks}{convo}[요청]\n{text}"
        if self.planner == "agy":
            return await self._plan_agy(prompt)
        req = CLIRequest(
            Engine.CLAUDE,
            prompt,
            model=self.settings.untrusted_model,
            system_prompt=PLANNER_SYSTEM,
            trusted=False,  # 도구 없음
            timeout=60,
            extra_args=["--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False), "--max-budget-usd", "0.05"],
        )
        r = await self.run(req, self.settings.claude_bin)
        data = (r.raw or {}).get("structured_output") if r.ok else None
        if not isinstance(data, dict) or data.get("intent") not in INTENTS:
            log.warning("planner failed: %s", r.error)
            return Plan("other")
        return Plan(data["intent"], data)

    async def _plan_agy(self, prompt: str) -> Plan:
        """가족용 플래너: 각자의 agy 계정 + --json-schema (입력이 본인 문장뿐이라 agy 에 맡겨도 안전, 약 11초)."""
        _, req = build_request(self.settings, f"{prompt}\n\n{PLANNER_SYSTEM}\n도구를 쓰지 말고 JSON 계획으로만 답하라.")
        req.model = AGY_PLANNER_MODEL
        req.extra_args += ["--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False)]
        r = await self.run(req, self.settings.agy_bin)
        data = (r.raw or {}).get("structured_output") if r.ok else None
        if not isinstance(data, dict) or data.get("intent") not in INTENTS:
            log.warning("agy planner failed: %s", r.error)
            return Plan("other")
        return Plan(data["intent"], data)

    async def handle(self, thread_key: str, text: str, history: list[tuple[str, str]] | None = None) -> Reply | None:
        """일정·메일 요청이면 처리한 Reply 를, 아니면 None 을 돌려준다.

        직전 봇 답이 일정·메일·예약 처리였다면 키워드가 없어도("그날 4시에 결혼식 있지 않았어?") 플래너로 보낸다.
        """
        last_bot = next((who for who, _ in reversed(history or []) if who != "user"), "")
        follow_up = last_bot.startswith(WORKSPACE_SOURCES)
        if not (wants_workspace(text) or follow_up):
            return None
        p = await self.plan(text, history)
        if p.intent == "other":
            return None
        try:
            return await getattr(self, f"_{p.intent}")(thread_key, text, p.data)
        except NotAuthorized as e:
            return Reply(f"Google 연결이 끊겼습니다. 서버에서 다시 로그인해 주세요.\n`{e}`", ok=False)

    async def _calendar_list(self, thread_key: str, text: str, d: dict) -> Reply:
        now = datetime.now(KST)
        start = _dt(d.get("range_start")) or now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = _dt(d.get("range_end")) or start + timedelta(days=7)
        if end <= start:
            end = start + timedelta(days=1)
        events = await asyncio.to_thread(self.api.list_events, start, end)
        return Reply(self.api.format_events(events, start, end), meta="calendar")

    async def _calendar_create(self, thread_key: str, text: str, d: dict) -> Reply:
        start = _dt(d.get("event_start"))
        summary = (d.get("event_summary") or "").strip()
        if not start or not summary:
            return Reply("일정 제목과 시작 시각을 알 수 없습니다. 예: `내일 오후 3시 치과 예약 일정 추가해줘`", ok=False)
        end = _dt(d.get("event_end")) or start + timedelta(hours=1)
        payload = {"summary": summary, "start": start.isoformat(), "end": end.isoformat(),
                   "location": d.get("event_location") or ""}
        aid = self.pending.add(thread_key, "calendar_create", payload, self.owner)
        lines = [f"• 제목: {summary}", f"• 시간: {start:%Y-%m-%d %H:%M} ~ {end:%H:%M}"]
        if payload["location"]:
            lines.append(f"• 장소: {payload['location']}")
        return Reply("일정을 추가할까요?\n" + "\n".join(lines), blocks=approval_blocks(aid, "일정 추가", lines))

    async def _email_list(self, thread_key: str, text: str, d: dict) -> Reply:
        query = d.get("email_query") or "is:unread newer_than:7d"
        mails = await asyncio.to_thread(self.api.list_mail, query, 20)
        if not mails:
            return Reply(f"`{query}` 조건에 맞는 메일이 없습니다.", meta="gmail")
        if self.mail_mode == "list":
            # 가족: 도구 없는 엔진(claude)이 없어 AI 가 메일을 읽지 않는다. 제목도 외부 텍스트라 대화는 오염 표시
            lines = [f"*안 읽은 메일 {len(mails)}통* (`{query}`)"] + [f"- [{m.date}] {m.sender} — {m.subject}" for m in mails]
            return Reply("\n".join(lines), tainted=True, meta=f"gmail · {len(mails)}통")
        listing = "\n".join(f"- [{m.date}] {m.sender} | {m.subject} | {m.snippet}" for m in mails)
        # 메일 내용은 외부 텍스트 → 도구 없는 claude 로만 요약
        _, req = build_request(self.settings, text, external=[ExternalContent(f"gmail:{query}", listing)])
        r = await self.run(req, self.settings.claude_bin)
        if not r.ok:
            return Reply(f"메일 요약 실패: {(r.error or '')[:300]}", ok=False, tainted=True)
        return Reply(r.text, tainted=True, meta=f"gmail · {len(mails)}통 · claude · {r.duration:.1f}s")

    async def _email_draft(self, thread_key: str, text: str, d: dict) -> Reply:
        to, subject, body = (d.get("draft_to") or "").strip(), (d.get("draft_subject") or "").strip(), d.get("draft_body") or ""
        if "@" not in to or not body.strip():
            return Reply("받는 사람 주소와 내용을 알려 주세요. 예: `a@b.com 에게 회의 일정 확인 메일 초안 써줘`", ok=False)
        aid = self.pending.add(thread_key, "email_draft", {"to": to, "subject": subject, "body": body}, self.owner)
        lines = [f"• 받는 사람: {to}", f"• 제목: {subject or '(없음)'}", "• 본문:", f"```{body[:1500]}```"]
        # text 는 알림·대화 기록용이라 상세 내용을 함께 싣는다
        return Reply("메일 초안을 만들까요? (발송은 하지 않습니다)\n" + "\n".join(lines),
                     blocks=approval_blocks(aid, "메일 초안 작성", lines))

    # ---- 예약 작업 (읽기 전용 알림이라 승인 없이 등록) ----
    def _need_sched(self) -> Reply | None:
        return None if self.sched is not None else Reply("예약 기능이 꺼져 있습니다.", ok=False)

    async def _reminder_add(self, thread_key: str, text: str, d: dict) -> Reply:
        if r := self._need_sched():
            return r
        when, what = _dt(d.get("remind_at")), (d.get("remind_text") or "").strip()
        if not when or not what:
            return Reply("언제, 무엇을 알려 드릴지 알려 주세요. 예: `오늘 오후 3시에 약 먹으라고 알려줘`", ok=False)
        repeat = d.get("repeat") or "none"
        if repeat != "none":
            try:
                jid = scheduler.add_recurring(self.sched, f"{when:%H:%M}", repeat, what, user=self.owner,
                                              weekdays=d.get("repeat_weekdays"), day=d.get("repeat_day"))
            except ValueError as e:
                return Reply(str(e), ok=False)
            nxt = self.sched.get_job(jid).next_run_time
            label = scheduler.describe_repeat(repeat, d.get("repeat_weekdays"), d.get("repeat_day"))
            first = f" (첫 알림 {nxt:%m/%d}({google.WEEKDAYS[nxt.weekday()]}) {nxt:%H:%M})" if nxt else ""
            return Reply(f"🔁 {label} {when:%H:%M}에 알려드릴게요: {what}{first}\n"
                         "그만 받으려면 `○○ 알림 취소해줘`라고 말해 주세요.", meta="scheduler")
        try:
            scheduler.add_reminder(self.sched, when, what, user=self.owner)
        except ValueError as e:
            return Reply(f"{e} ({when:%m/%d %H:%M})", ok=False)
        day = google.WEEKDAYS[when.weekday()]
        return Reply(f"⏰ {when:%m/%d}({day}) {when:%H:%M}에 알려드릴게요: {what}", meta="scheduler")

    async def _mail_watch_add(self, thread_key: str, text: str, d: dict) -> Reply:
        if r := self._need_sched():
            return r
        query = (d.get("email_query") or "").strip()
        if not query:
            return Reply("어떤 메일을 감시할지 알려 주세요. 예: `boss@x.com 에서 오는 메일 30분마다 확인해줘`", ok=False)
        minutes = int(d.get("watch_minutes") or 30)
        label = (d.get("watch_label") or query)[:40]
        scheduler.add_mail_watch(self.sched, query, minutes, label, user=self.owner)
        return Reply(f"📬 `{query}` 새 메일을 {max(minutes, scheduler.MIN_WATCH_MINUTES)}분마다 확인해 알려드릴게요. "
                     "(지금 있는 메일은 기준으로만 기록)", meta="scheduler")

    async def _schedule_list(self, thread_key: str, text: str, d: dict) -> Reply:
        return self._need_sched() or Reply(scheduler.list_jobs(self.sched, self.owner), meta="scheduler")

    async def _schedule_cancel(self, thread_key: str, text: str, d: dict) -> Reply:
        if r := self._need_sched():
            return r
        jid = (d.get("job_id") or "").strip("` ")
        if jid and scheduler.remove(self.sched, jid, self.owner):
            return Reply(f"`{jid}` 예약을 취소했습니다.", meta="scheduler")
        return Reply("취소할 작업 ID를 알려 주세요.\n" + scheduler.list_jobs(self.sched, self.owner), ok=not jid)

    # ---- 할 일 (체크 목록. 알림은 원할 때만 scheduler 에 건다) ----
    def _due(self, d: dict) -> str | None:
        raw = (d.get("todo_due") or "").strip()
        if not raw:
            return None
        if len(raw) == 10:  # YYYY-MM-DD
            try:
                return date.fromisoformat(raw).isoformat()
            except ValueError:
                return None
        dt = _dt(raw)
        return dt.isoformat(timespec="minutes") if dt else None

    def _remind(self, t: todo_mod.Todo, d: dict) -> str:
        """todo_remind_at 이 있으면 알림을 (다시) 건다. 결과 문구를 돌려준다."""
        when = _dt(d.get("todo_remind_at"))
        if not when:
            return ""
        if self.sched is None:
            return "\n(예약 기능이 꺼져 있어 알림은 걸지 못했어요)"
        try:
            jid = scheduler.add_todo_reminder(self.sched, when, t.id, t.title, user=self.owner)
        except ValueError as e:
            return f"\n(알림은 걸지 못했어요: {e})"
        scheduler.cancel_job(self.sched, t.remind_job)
        self.todos.set_remind_job(t.id, jid)
        return f"\n🔔 {when:%m/%d}({google.WEEKDAYS[when.weekday()]}) {when:%H:%M}에 알려드릴게요."

    def _pick(self, d: dict) -> tuple[list[todo_mod.Todo], Reply | None]:
        ids = [int(i) for i in d.get("todo_ids") or [] if str(i).lstrip("#").isdigit()]
        found = [t for i in ids if (t := self.todos.get(i, self.owner))]
        if not found:
            return [], Reply("어떤 할 일인지 모르겠어요. 번호로 말해 주세요.\n"
                             + todo_mod.format_list(self.todos.list(self.owner)), ok=False, meta="todo")
        return found, None

    async def _todo_add(self, thread_key: str, text: str, d: dict) -> Reply:
        title = (d.get("todo_title") or "").strip()
        if not title:
            return Reply("무엇을 할 일로 추가할까요? 예: `금요일까지 보고서 제출 할 일 추가해줘`", ok=False)
        t = self.todos.add(title, self._due(d), self.owner)
        note = self._remind(t, d)
        return Reply(f"📝 할 일 추가: {todo_mod.line(self.todos.get(t.id, self.owner))}{note}", meta="todo")

    async def _todo_list(self, thread_key: str, text: str, d: dict) -> Reply:
        items = self.todos.list(self.owner, include_done=bool(d.get("todo_show_done")))
        return Reply(todo_mod.format_list(items), meta="todo")

    async def _todo_update(self, thread_key: str, text: str, d: dict) -> Reply:
        found, err = self._pick(d)
        if err:
            return err
        t = found[0]
        title = (d.get("todo_title") or "").strip() or None
        due = self._due(d)
        if title or due or d.get("todo_clear_due"):
            t = self.todos.update(t.id, self.owner, title=title, due=due, clear_due=bool(d.get("todo_clear_due")))
        note = self._remind(t, d)
        if not (title or due or d.get("todo_clear_due") or note):
            return Reply("무엇을 바꿀지 알려 주세요. 예: `#3 금요일까지로 바꿔줘`, `#3 내일 9시에 알려줘`", ok=False)
        return Reply(f"✏️ 수정했어요: {todo_mod.line(self.todos.get(t.id, self.owner))}{note}", meta="todo")

    def complete(self, tid: int, done: bool = True) -> todo_mod.Todo | None:
        t = self.todos.set_status(tid, self.owner, done)
        if t and done:
            scheduler.cancel_job(self.sched, t.remind_job)
            self.todos.set_remind_job(t.id, None)
        return self.todos.get(tid, self.owner) if t else None

    async def _todo_done(self, thread_key: str, text: str, d: dict) -> Reply:
        found, err = self._pick(d)
        if err:
            return err
        done = [self.complete(t.id) for t in found]
        left = len(self.todos.list(self.owner))
        return Reply("✅ 완료: " + ", ".join(f"`#{t.id}` {t.title}" for t in done if t) + f"\n남은 할 일 {left}개",
                     meta="todo")

    async def _todo_reopen(self, thread_key: str, text: str, d: dict) -> Reply:
        found, err = self._pick(d)
        if err:
            return err
        again = [self.complete(t.id, done=False) for t in found]
        return Reply("↩️ 다시 열었어요: " + ", ".join(f"`#{t.id}` {t.title}" for t in again if t), meta="todo")

    async def _todo_delete(self, thread_key: str, text: str, d: dict) -> Reply:
        found, err = self._pick(d)
        if err:
            return err
        for t in found:
            scheduler.cancel_job(self.sched, t.remind_job)
            self.todos.delete(t.id, self.owner)
        return Reply("🗑️ 지웠어요: " + ", ".join(f"`#{t.id}` {t.title}" for t in found), meta="todo")

    async def _shopping(self, thread_key: str, text: str, d: dict) -> Reply:
        return Reply(SHOPPING_UNSUPPORTED, ok=False, meta="workspace")

    async def _briefing_time(self, thread_key: str, text: str, d: dict) -> Reply:
        if r := self._need_sched():
            return r
        hhmm = d.get("briefing_time") or ""
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", hhmm):
            return Reply("브리핑 시각을 알려 주세요. 예: `아침 브리핑 8시로 바꿔줘`", ok=False)
        return Reply(scheduler.set_briefing(self.sched, hhmm, self.owner), meta="scheduler")

    async def approve(self, aid: str, owner: str | None = None) -> tuple[str, str] | None:
        """승인된 작업을 실행하고 (thread_key, 결과 메시지) 를 돌려준다. 이미 처리됐거나 만료면 None."""
        taken = self.pending.take(aid, "done", owner if owner is not None else self.owner)
        if not taken:
            return None
        thread_key, kind, p = taken
        try:
            if kind == "calendar_create":
                ev = await asyncio.to_thread(self.api.create_event, p["summary"], datetime.fromisoformat(p["start"]),
                                             datetime.fromisoformat(p["end"]), p.get("location", ""))
                return thread_key, f"✅ 일정을 추가했습니다: {p['summary']} ({ev.link})"
            if kind == "email_draft":
                await asyncio.to_thread(self.api.create_draft, p["to"], p["subject"], p["body"])
                return thread_key, f"✅ Gmail 임시보관함에 초안을 만들었습니다 (받는 사람: {p['to']}). 발송은 Gmail 에서 직접 해 주세요."
        except NotAuthorized as e:
            return thread_key, f"❌ Google 연결이 끊겼습니다: {e}"
        except Exception as e:
            log.exception("action %s failed", aid)
            return thread_key, f"❌ 실행 실패: {e}"
        return thread_key, f"알 수 없는 작업: {kind}"

    def cancel(self, aid: str, owner: str | None = None) -> str | None:
        taken = self.pending.take(aid, "cancelled", owner if owner is not None else self.owner)
        return taken[0] if taken else None
