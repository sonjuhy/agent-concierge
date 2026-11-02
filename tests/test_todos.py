from datetime import date, datetime, timedelta

import pytest

from agent import scheduler
from agent.actions import PendingStore, Workspace, wants_workspace
from agent.context import KST
from agent.todos import TodoStore, format_list
from tests.test_actions import FakeApi, PlanRunner
from tests.test_scheduler import env  # noqa: F401
from tests.test_slack_app import settings  # noqa: F401


def ws(settings, store, plan, sched=None):  # noqa: F811
    return Workspace(settings, PendingStore(store.db), PlanRunner(plan), api=FakeApi(), sched=sched, owner="owner")


def test_store_owner_isolation_and_order():
    import sqlite3
    s = TodoStore(sqlite3.connect(":memory:"))
    a = s.add("보고서", "2026-11-02", "me")
    b = s.add("장보기", None, "me")
    c = s.add("숙제", "2026-11-01", "kid")
    assert [t.id for t in s.list("me")] == [a.id, b.id]  # 기한 있는 것 먼저, 없는 것 뒤
    assert s.get(c.id, "me") is None and s.set_status(c.id, "me", True) is None and s.delete(c.id, "me") is None
    s.set_status(a.id, "me", True)
    assert [t.id for t in s.list("me")] == [b.id] and s.list("me", include_done=True)[-1].done
    assert s.update(b.id, "me", due="2026-11-03").due == "2026-11-03"
    assert s.update(b.id, "me", clear_due=True).due is None
    out = format_list(s.list("me", include_done=True), today=date(2026, 11, 5))
    assert f"☐ `#{b.id}` 장보기" in out and f"☑ `#{a.id}` ~보고서~" in out


def test_keywords():
    for q in ["할 일 보여줘", "보고서 끝냈어", "#3 했어", "투두 추가해줘", "todo 정리"]:
        assert wants_workspace(q), q


async def test_add_with_reminder_then_done_cancels_it(settings, env):  # noqa: F811
    sched, client, store = env
    when = (datetime.now(KST) + timedelta(days=1)).replace(microsecond=0)
    r = await ws(settings, store, {"intent": "todo_add", "todo_title": "보고서 제출", "todo_due": "2026-11-07",
                                   "todo_remind_at": when.isoformat()}, sched).handle("t", "금요일까지 보고서 할 일 추가")
    assert r.meta == "todo" and "보고서 제출" in r.text and "11/07(토)" in r.text and "🔔" in r.text
    (t,) = TodoStore(store.db).list("owner")
    job = sched.get_job(t.remind_job)
    assert job and job.args == (t.id,)

    # 플래너에 [할 일] 목록이 실린다
    runner = PlanRunner({"intent": "todo_done", "todo_ids": [t.id]})
    w = Workspace(settings, PendingStore(store.db), runner, api=FakeApi(), sched=sched, owner="owner")
    r = await w.handle("t", "보고서 끝냈어")
    assert f"- #{t.id}: 보고서 제출 (기한 2026-11-07)" in runner.calls[0].prompt
    assert "✅ 완료" in r.text and "남은 할 일 0개" in r.text
    assert sched.get_job(t.remind_job) is None and TodoStore(store.db).get(t.id, "owner").remind_job is None


async def test_update_reopen_delete_and_unknown_id(settings, env):  # noqa: F811
    sched, _, store = env
    t = TodoStore(store.db).add("장보기", None, "owner")
    r = await ws(settings, store, {"intent": "todo_update", "todo_ids": [t.id], "todo_title": "마트 장보기",
                                   "todo_due": "2026-11-03T18:00:00+09:00"}, sched).handle("t", f"#{t.id} 바꿔줘")
    assert "마트 장보기" in r.text and "11/03(화) 18:00" in r.text
    r = await ws(settings, store, {"intent": "todo_done", "todo_ids": [999]}, sched).handle("t", "#999 했어")
    assert not r.ok and "번호로" in r.text
    await ws(settings, store, {"intent": "todo_done", "todo_ids": [t.id]}, sched).handle("t", "했어")
    r = await ws(settings, store, {"intent": "todo_reopen", "todo_ids": [t.id]}, sched).handle("t", f"#{t.id} 다시 열어줘")
    assert "다시 열었어요" in r.text and not TodoStore(store.db).get(t.id, "owner").done
    r = await ws(settings, store, {"intent": "todo_delete", "todo_ids": [t.id]}, sched).handle("t", "할 일 지워줘")
    assert "지웠어요" in r.text and TodoStore(store.db).list("owner") == []


async def test_reminder_job_skips_done_and_has_button(env):  # noqa: F811
    _, client, store = env
    todos = TodoStore(store.db)
    t = todos.add("약 사기", None, "owner")
    await scheduler.job_todo_reminder(t.id)
    post = client.posts[-1]
    assert "약 사기" in post["text"] and post["blocks"][-1]["elements"][0]["action_id"] == "todo_done"
    todos.set_status(t.id, "owner", True)
    await scheduler.job_todo_reminder(t.id)
    assert len(client.posts) == 1


async def test_briefing_section(env):  # noqa: F811
    _, _, store = env
    todos = TodoStore(store.db)
    today = datetime.now(KST).date()
    todos.add("지난 것", (today - timedelta(days=1)).isoformat(), "owner")
    todos.add("오늘", today.isoformat(), "owner")
    todos.add("나중", (today + timedelta(days=5)).isoformat(), "owner")
    out = scheduler.todo_section(None)
    assert "지난 것" in out and "⚠️ 지남" in out and "오늘" in out and "나중" not in out and "그 밖에 1개" in out
    assert scheduler.todo_section("nobody") is not None  # 모르는 이름은 소유자로


@pytest.mark.parametrize("value,expected", [("1", "✅ 완료"), ("x", "본인 할 일이 아닙니다")])
async def test_done_button(settings, env, value, expected):  # noqa: F811
    from agent.slack_app import Agent

    _, client, store = env
    TodoStore(store.db).add("버튼 테스트", None, "owner")
    updates = []

    async def chat_update(**kw):
        updates.append(kw)

    client.chat_update = chat_update
    agent = Agent(settings, store, client, "U_ME", run=PlanRunner({}), chatlog={}, workspace={"owner": ws(settings, store, {})})
    await agent.on_todo_done({"user": {"id": "U_ME"}, "actions": [{"value": value}], "channel": {"id": "D"},
                              "message": {"ts": "1", "blocks": [{"type": "markdown", "text": "x"}]}})
    assert expected in updates[-1]["text"]


def test_time_phrases_reach_planner():
    for q in ["다음 주 월요일 출근할때(오전 7시 15분) 약 챙기라고 해줘", "내일 6시에 깨워줘", "7:30 회의 자료 보라고 말해줘",
              "우산 까먹지 않게 해줘"]:
        assert wants_workspace(q), q
    for q in ["3시간 동안 뭐 하지?", "파이썬 리스트 정렬 방법"]:
        assert not wants_workspace(q), q
