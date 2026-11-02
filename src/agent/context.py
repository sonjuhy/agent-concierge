"""매 호출 프롬프트 앞에 붙이는 시간 맥락. "다음 주 화요일" 같은 상대 날짜 해석 오류를 막는다."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = "월화수목금토일"


def _week(monday: date) -> str:
    return " ".join(f"{d:%m/%d}({WEEKDAYS[i]})" for i, d in enumerate(monday + timedelta(days=n) for n in range(7)))


def build_context(now: datetime | None = None) -> str:
    now = (now or datetime.now(KST)).astimezone(KST)
    monday = now.date() - timedelta(days=now.weekday())
    return (
        f"[현재 시각] {now:%Y-%m-%d %H:%M} ({WEEKDAYS[now.weekday()]}) KST\n"
        f"[이번 주] {_week(monday)}\n"
        f"[다음 주] {_week(monday + timedelta(days=7))}"
    )
