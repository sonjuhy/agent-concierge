from datetime import datetime, timedelta

import pytest

from agent import health, scheduler
from agent.actions import PendingStore, Workspace
from agent.cli_runner import CLIResult, Engine
from agent.context import KST
from agent.sessions import Store
from agent.tools.google import Mail
from tests.test_actions import FakeApi, PlanRunner
from tests.test_slack_app import settings  # noqa: F401


class DMClient:
    def __init__(self):
        self.posts = []

    async def conversations_open(self, users):
        return {"channel": {"id": "DM1"}}

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"{len(self.posts)}.0"}


@pytest.fixture
async def env(settings, monkeypatch):  # noqa: F811
    store = Store(":memory:")
    client = DMClient()
    sched = scheduler.create(settings, client, store, "U_ME")
    sched.start(paused=True)
    scheduler.RUNTIME.apis["owner"] = FakeApi()
    yield sched, client, store
    sched.shutdown(wait=False)


async def test_reminder_registration_and_post(env):
    sched, client, _ = env
    with pytest.raises(ValueError):
        scheduler.add_reminder(sched, datetime.now(KST) - timedelta(minutes=1), "과거")
    jid = scheduler.add_reminder(sched, datetime.now(KST) + timedelta(hours=1), "약 먹기")
    assert jid in scheduler.list_jobs(sched) and "약 먹기" in scheduler.list_jobs(sched)
    await scheduler.job_reminder("약 먹기")
    assert client.posts[-1]["channel"] == "DM1" and "⏰ 알림: 약 먹기" in client.posts[-1]["text"]
    assert scheduler.remove(sched, jid) and not scheduler.remove(sched, jid)


async def test_briefing_default_and_replace(env):
    sched, _, _ = env
    scheduler.ensure_briefing(sched, "07:30")
    scheduler.ensure_briefing(sched, "09:00")  # 이미 있으면 바꾸지 않음
    assert "07:30" in sched.get_job(scheduler.BRIEFING_ID).name
    scheduler.set_briefing(sched, "8:05")
    assert "08:05" in sched.get_job(scheduler.BRIEFING_ID).name


async def test_mail_watch_baseline_then_new(env):
    sched, client, store = env
    await scheduler.job_mail_watch("from:boss", "팀장")
    assert client.posts == []  # 첫 실행은 기준선만
    scheduler.RUNTIME.apis["owner"].list_mail = lambda q, n: [Mail("1", "boss", "old", "", ""), Mail("2", "boss", "새 보고서", "", "")]
    await scheduler.job_mail_watch("from:boss", "팀장")
    assert "새 보고서" in client.posts[-1]["text"] and "old" not in client.posts[-1]["text"]
    assert store.is_tainted("DM1:1.0")
    await scheduler.job_mail_watch("from:boss", "팀장")
    assert len(client.posts) == 1  # 같은 메일은 다시 알리지 않음


async def test_briefing_job(env, monkeypatch):
    _, client, store = env

    async def fake_checks(s, users=None):
        return [health.Check("agy", True, "ok"), health.Check("Syncthing", False, "WindowsPC 연결 끊김")]

    async def fake_run(req, binary):
        assert not req.trusted and "<untrusted_content" in req.prompt
        return CLIResult(Engine.CLAUDE, ok=True, text="보고서 요청 메일이 중요")

    monkeypatch.setattr(scheduler.health, "run_all", fake_checks)
    monkeypatch.setattr(scheduler, "run_cli", fake_run)
    await scheduler.job_briefing()
    text = client.posts[-1]["text"] + client.posts[-1]["blocks"][0]["text"]
    assert "점검 필요" in text and "WindowsPC 연결 끊김" in text
    assert "팀 회의" in text and "보고서 요청 메일이 중요" in text and "1/2 정상" in text
    assert store.is_tainted("DM1:1.0")


async def test_recurring_triggers(env):
    sched, _, _ = env
    cases = [
        ("daily", None, None, "매일 08:00"),
        ("weekdays", None, None, "평일 08:00"),
        ("weekends", None, None, "주말 08:00"),
        ("weekly", ["fri", "mon"], None, "매주 월·금요일 08:00"),
        ("monthly", None, 25, "매월 25일 08:00"),
    ]
    for repeat, days, day, label in cases:
        jid = scheduler.add_recurring(sched, "08:00", repeat, "약 먹기", weekdays=days, day=day)
        job = sched.get_job(jid)
        assert job.name.startswith(label), job.name
        nxt = job.trigger.get_next_fire_time(None, datetime(2026, 10, 28, 9, 0, tzinfo=KST))
        assert (nxt.hour, nxt.minute) == (8, 0)
        if repeat == "weekdays":
            assert nxt.weekday() < 5
        if repeat == "weekends":
            assert nxt.weekday() >= 5
        if repeat == "weekly":
            assert nxt.weekday() in (0, 4)
        if repeat == "monthly":
            assert nxt.day == 25
    with pytest.raises(ValueError):
        scheduler.add_recurring(sched, "08:00", "weekly", "x")
    with pytest.raises(ValueError):
        scheduler.add_recurring(sched, "08:00", "monthly", "x", day=40)


async def test_workspace_recurring_and_cancel_by_name(env, settings):  # noqa: F811
    sched, _, _ = env
    when = datetime(2026, 10, 29, 8, 0, tzinfo=KST)

    def ws(plan):
        return Workspace(settings, PendingStore(Store(":memory:").db), PlanRunner(plan), api=FakeApi(), sched=sched)

    w = ws({"intent": "reminder_add", "remind_at": when.isoformat(), "remind_text": "약 먹기", "repeat": "weekdays"})
    r = await w.handle("t", "평일 아침 8시마다 약 먹으라고 알려줘")
    assert r.ok and "🔁 평일 08:00에 알려드릴게요: 약 먹기" in r.text
    jid = sched.get_jobs()[0].id
    # 취소 요청 때 플래너가 이름으로 고를 수 있도록 예약 목록이 프롬프트에 들어간다
    w2 = ws({"intent": "schedule_cancel", "job_id": jid})
    r = await w2.handle("t", "약 알림 취소해줘")
    assert "[등록된 예약]" in w2.run.calls[0].prompt and jid in w2.run.calls[0].prompt
    assert "취소했습니다" in r.text and sched.get_job(jid) is None


async def test_workspace_schedule_intents(env, settings):  # noqa: F811
    sched, _, _ = env
    when = (datetime.now(KST) + timedelta(hours=2)).replace(microsecond=0)

    def ws(plan):
        return Workspace(settings, PendingStore(Store(":memory:").db), PlanRunner(plan), api=FakeApi(), sched=sched)

    r = await ws({"intent": "reminder_add", "remind_at": when.isoformat(), "remind_text": "회의 준비"}).handle("t", "2시간 뒤에 알려줘")
    assert r.ok and "회의 준비" in r.text and len(sched.get_jobs()) == 1
    r = await ws({"intent": "mail_watch_add", "email_query": "from:boss", "watch_minutes": 1}).handle("t", "팀장 메일 감시해줘")
    assert "5분마다" in r.text  # 최소 주기
    r = await ws({"intent": "schedule_list"}).handle("t", "예약 작업 보여줘")
    assert "mailwatch:from:boss" in r.text
    r = await ws({"intent": "schedule_cancel", "job_id": "mailwatch:from:boss"}).handle("t", "메일 감시 취소")
    assert "취소했습니다" in r.text and sched.get_job("mailwatch:from:boss") is None
    r = await ws({"intent": "briefing_time", "briefing_time": "25:00"}).handle("t", "브리핑 시간 바꿔")
    assert not r.ok
    r = await ws({"intent": "briefing_time", "briefing_time": "08:00"}).handle("t", "브리핑 8시로")
    assert "08:00" in r.text
