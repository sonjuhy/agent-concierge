import sqlite3

from agent.actions import SHOPPING_UNSUPPORTED, PendingStore, Workspace, wants_workspace
from tests.test_actions import FakeApi, PlanRunner
from tests.test_slack_app import settings  # noqa: F401


def test_shopping_keywords_reach_planner():
    for q in ["로봇청소기 쇼핑 좀 해줘", "에어팟 최저가 알려줘", "가격 알림 걸어줘"]:
        assert wants_workspace(q), q


async def test_shopping_is_unsupported(settings):  # noqa: F811
    ws = Workspace(settings, PendingStore(sqlite3.connect(":memory:")), PlanRunner({"intent": "shopping"}),
                   api=FakeApi())
    r = await ws.handle("t", "로봇청소기 30만원 이하로 찾아줘")
    assert r.text == SHOPPING_UNSUPPORTED and not r.ok
