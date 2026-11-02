import sqlite3
from datetime import datetime

from agent.chatlog import ChatLog, make_title
from agent.context import KST
from indexer import build_index

TS = str(datetime(2026, 10, 22, 14, 30, tzinfo=KST).timestamp())
TS2 = str(datetime(2026, 10, 22, 14, 31, tzinfo=KST).timestamp())


def log_for(tmp_path):
    return ChatLog(tmp_path / "vault", sqlite3.connect(":memory:"))


def test_make_title_sanitizes():
    assert make_title('일정 #확인 "다음 주" a/b\n둘째 줄') == "일정 확인 다음 주 ab"
    assert make_title("") == "대화"
    assert len(make_title("가" * 100)) == 40


def test_thread_file_appends_in_order(tmp_path):
    cl = log_for(tmp_path)
    p = cl.append("D1:1", TS, "나", "오늘 무슨 요일이야?")
    cl.append("D1:1", TS2, "agy · 10.0s", "목요일입니다.")
    assert p == tmp_path / "vault/_Chat_Logs/2026/10/2026-10-22_1430_오늘 무슨 요일이야.md"
    text = p.read_text()
    assert text.startswith("---\ntype: chat_log\n") and "tainted: false" in text
    assert text.index("### 14:30 · 나") < text.index("### 14:31 · agy · 10.0s")
    assert cl.path_for("D1:1") == p


def test_same_title_same_minute_gets_suffix(tmp_path):
    cl = log_for(tmp_path)
    a = cl.append("D1:1", TS, "나", "안녕")
    b = cl.append("D1:2", TS, "나", "안녕")
    assert a != b and b.name.endswith("-2.md")


def test_tainted_thread_moves_to_untrusted(tmp_path):
    cl = log_for(tmp_path)
    p = cl.append("D1:1", TS, "나", "안녕")
    q = cl.append("D1:1", TS2, "claude · 5.0s", "메일 요약…", tainted=True)
    assert not p.exists()
    assert q == tmp_path / "vault/_Chat_Logs/_untrusted/2026/10" / p.name
    text = q.read_text()
    assert "tainted: true" in text and "### 14:30 · 나" in text and "메일 요약" in text
    # 처음부터 오염된 스레드는 바로 _untrusted 에 만든다
    r = cl.append("D1:9", TS, "나", "이 메일 요약해줘", tainted=True)
    assert "_untrusted" in r.parts


def test_index_lists_trusted_chats_only(tmp_path):
    cl = log_for(tmp_path)
    cl.append("D1:1", TS, "나", "일정 이야기")
    cl.append("D1:2", TS2, "나", "메일 요약", tainted=True)
    out = build_index.build(tmp_path / "vault")
    assert "## 대화 기록 (1)" in out and "### 2026-10" in out
    assert "2026-10-22_1430_일정 이야기" in out and "메일 요약" not in out
    assert "## 노트 (0)" in out  # 대화 기록은 노트 목록에 섞이지 않는다
