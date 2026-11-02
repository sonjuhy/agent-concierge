"""Google Calendar / Gmail 호출. AI 가 아니라 Python 이 직접 부른다.

읽기(일정 조회, 메일 목록)는 바로 실행하고, 쓰기(일정 추가, 메일 초안)는 Slack 승인 뒤에만 실행한다
(승인 흐름은 agent.actions). 메일 발송·일정 수정/삭제는 아직 제공하지 않는다.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path

from googleapiclient.discovery import build

from agent.context import KST
from agent.tools.google_auth import load_credentials

WEEKDAYS = "월화수목금토일"


@dataclass(frozen=True)
class Event:
    summary: str
    start: datetime | None  # None 이 아니면 시각 있는 일정
    end: datetime | None
    start_date: str  # YYYY-MM-DD (종일 일정 포함 정렬·표시용)
    all_day: bool
    location: str = ""
    link: str = ""
    calendar: str = ""  # 기본 캘린더가 아니면 캘린더 이름


@dataclass(frozen=True)
class Mail:
    id: str
    sender: str
    subject: str
    date: str
    snippet: str


def _service(name: str, version: str, token_file: Path | None = None):
    creds = load_credentials(token_file) if token_file else load_credentials()
    return build(name, version, credentials=creds, cache_discovery=False)


class GoogleApi:
    """한 사용자의 Google 토큰에 묶인 호출 모음 (멀티 사용자). 모듈 함수와 같은 이름·인자를 쓴다."""

    format_events = staticmethod(lambda events, start, end: format_events(events, start, end))

    def __init__(self, token_file: Path):
        self.token_file = token_file

    def list_events(self, start: datetime, end: datetime, max_results: int = 50) -> list[Event]:
        return list_events(start, end, max_results, token_file=self.token_file)

    def create_event(self, summary: str, start: datetime, end: datetime, location: str = "", description: str = "") -> Event:
        return create_event(summary, start, end, location, description, token_file=self.token_file)

    def list_mail(self, query: str, max_results: int = 20) -> list[Mail]:
        return list_mail(query, max_results, token_file=self.token_file)

    def create_draft(self, to: str, subject: str, body: str) -> str:
        return create_draft(to, subject, body, token_file=self.token_file)


def _parse_event(e: dict, calendar: str = "") -> Event:
    s, en = e.get("start", {}), e.get("end", {})
    if "dateTime" in s:
        start = datetime.fromisoformat(s["dateTime"]).astimezone(KST)
        end = datetime.fromisoformat(en["dateTime"]).astimezone(KST) if "dateTime" in en else None
        return Event(e.get("summary") or "(제목 없음)", start, end, f"{start:%Y-%m-%d}", False,
                     e.get("location") or "", e.get("htmlLink") or "", calendar)
    return Event(e.get("summary") or "(제목 없음)", None, None, s.get("date", ""), True,
                 e.get("location") or "", e.get("htmlLink") or "", calendar)


def visible_calendars(service) -> list[tuple[str, str]]:
    """Google 캘린더 화면에서 표시(체크)된 캘린더들 (id, 표시 이름). 기본 캘린더 이름은 빈 문자열.

    calendar.calendarlist.readonly 권한이 없으면(재로그인 전) 기본 캘린더만 돌려준다.
    """
    from googleapiclient.errors import HttpError

    try:
        items = service.calendarList().list(showHidden=False).execute().get("items", [])
    except HttpError as e:
        if e.resp.status == 403:
            return [("primary", "")]
        raise
    out = []
    for c in items:
        if c.get("deleted") or c.get("selected") is False:
            continue
        out.append((c["id"], "" if c.get("primary") else (c.get("summaryOverride") or c.get("summary") or c["id"])))
    return out or [("primary", "")]


def list_events(start: datetime, end: datetime, max_results: int = 50, token_file: Path | None = None) -> list[Event]:
    service = _service("calendar", "v3", token_file)
    events: list[Event] = []
    for cal_id, name in visible_calendars(service):
        items = (
            service.events()
            .list(calendarId=cal_id, timeMin=start.isoformat(), timeMax=end.isoformat(),
                  singleEvents=True, orderBy="startTime", maxResults=max_results)
            .execute().get("items", [])
        )
        events += [_parse_event(e, name) for e in items if e.get("status") != "cancelled"]
    # 종일 일정이 먼저, 그다음 시작 시각 순
    return sorted(events, key=lambda ev: (ev.start_date, not ev.all_day, ev.start or start))


def format_events(events: list[Event], start: datetime, end: datetime) -> str:
    """LLM 없이 Python 으로 일정 목록을 만든다 (초대장 제목 등 외부 텍스트가 AI 입력이 되지 않게)."""
    last = end - timedelta(microseconds=1)  # 끝이 자정(배타적)이면 전날까지로 표시
    head = f"*{start:%m/%d}({WEEKDAYS[start.weekday()]}) ~ {last:%m/%d}({WEEKDAYS[last.weekday()]}) 일정 {len(events)}건*"
    if not events:
        return head + "\n일정이 없습니다."
    lines, day = [head], None
    # 여러 캘린더가 섞이면 기본 캘린더 일정에도 이름을 붙여 구분되게 한다
    mixed = any(ev.calendar for ev in events)
    for ev in events:
        d = datetime.fromisoformat(ev.start_date)
        if ev.start_date != day:
            day = ev.start_date
            lines.append(f"\n*{d:%m/%d}({WEEKDAYS[d.weekday()]})*")
        when = "종일" if ev.all_day else f"{ev.start:%H:%M}" + (f"–{ev.end:%H:%M}" if ev.end else "")
        where = f" @ {ev.location}" if ev.location else ""
        name = ev.calendar or ("기본" if mixed else "")
        cal = f" `{name}`" if name else ""
        lines.append(f"- {when} {ev.summary}{where}{cal}")
    return "\n".join(lines)


def create_event(summary: str, start: datetime, end: datetime, location: str = "", description: str = "",
                 token_file: Path | None = None) -> Event:
    body = {
        "summary": summary,
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Seoul"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Seoul"},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    return _parse_event(_service("calendar", "v3", token_file).events().insert(calendarId="primary", body=body).execute())


def _header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def list_mail(query: str, max_results: int = 20, token_file: Path | None = None) -> list[Mail]:
    gm = _service("gmail", "v1", token_file).users()
    ids = gm.messages().list(userId="me", q=query, maxResults=max_results).execute().get("messages", [])
    out = []
    for m in ids:
        msg = gm.messages().get(userId="me", id=m["id"], format="metadata",
                                metadataHeaders=["From", "Subject", "Date"]).execute()
        try:
            date = f"{parsedate_to_datetime(_header(msg, 'Date')).astimezone(KST):%m/%d %H:%M}"
        except Exception:
            date = ""
        out.append(Mail(m["id"], _header(msg, "From"), _header(msg, "Subject"), date, msg.get("snippet", "")))
    return out


def create_draft(to: str, subject: str, body: str, token_file: Path | None = None) -> str:
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    d = _service("gmail", "v1", token_file).users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
    return d.get("id", "")
