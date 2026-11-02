"""요청을 Tier 1(agy) / Tier 2(claude) / untrusted(claude, 도구 없음)로 분기한다.

결정 근거 (0단계 스파이크, 2026-10-22):
- agy -p 는 파일 쓰기를 승인 없이 실행하고 --sandbox 로도 막히지 않는다.
- bwrap 으로 파일 쓰기는 막을 수 있지만, agy 는 OAuth 토큰 파일 읽기 권한과 네트워크가
  있어야 동작하므로 read_file + search_web/send_message/schedule 경로의 유출은 막을 수 없다.
- claude 의 --tools "" --strict-mcp-config 는 도구 호출 자체를 제거한다(실측 확인).
따라서 외부 텍스트가 섞인 작업은 항상 도구 없는 claude 로 보낸다.

오염(taint) 규칙: untrusted 작업의 출력도 외부 텍스트의 영향을 받았으므로, 그 결과가 들어간
스레드는 tainted 로 표시하고 이후 요청도 untrusted 경로로 보낸다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent import agy_profile
from agent.cli_runner import CLIRequest, Engine
from agent.config import Settings
from agent.security import UNTRUSTED_SYSTEM_PROMPT, wrap_untrusted

_DEEP_FLAG_RE = re.compile(r"(?<!\S)--(deep|claude)(?!\S)")


@dataclass(frozen=True)
class ExternalContent:
    source: str  # 예: "gmail:<message-id>", "web:<url>"
    text: str


@dataclass(frozen=True)
class Route:
    engine: Engine
    trusted: bool
    reason: str


def decide(text: str, *, has_external: bool, thread_tainted: bool) -> Route:
    if has_external:
        return Route(Engine.CLAUDE, trusted=False, reason="external content")
    if thread_tainted:
        return Route(Engine.CLAUDE, trusted=False, reason="tainted thread")
    if _DEEP_FLAG_RE.search(text):
        return Route(Engine.CLAUDE, trusted=True, reason="--deep")
    return Route(Engine.AGY, trusted=True, reason="default")


def strip_flags(text: str) -> str:
    return re.sub(r"\s{2,}", " ", _DEEP_FLAG_RE.sub("", text)).strip()


def build_request(
    settings: Settings,
    text: str,
    *,
    external: list[ExternalContent] | None = None,
    thread_tainted: bool = False,
    resume: str | None = None,
    cwd: str | None = None,
    preamble: str = "",
) -> tuple[Route, CLIRequest]:
    """preamble: 이전 대화 등 프롬프트 앞에 붙일 글. 경로 판단(--deep 등)에는 쓰지 않는다."""
    external = external or []
    route = decide(text, has_external=bool(external), thread_tainted=thread_tainted)
    prompt = strip_flags(text)
    if preamble and route.trusted:
        prompt = f"{preamble}\n\n{prompt}"

    if not route.trusted:
        blocks = "\n\n".join(wrap_untrusted(e.text, e.source) for e in external)
        return route, CLIRequest(
            Engine.CLAUDE,
            f"{blocks}\n\n{prompt}" if blocks else prompt,
            model=settings.untrusted_model,
            # 신뢰 세션에 외부 텍스트를 섞지 않도록 untrusted 호출은 항상 1회성
            resume=None,
            system_prompt=UNTRUSTED_SYSTEM_PROMPT,
            trusted=False,
            timeout=settings.claude_timeout,
        )

    if route.engine is Engine.CLAUDE:
        return route, CLIRequest(
            Engine.CLAUDE,
            prompt,
            model=settings.claude_model,
            resume=resume,
            cwd=cwd,
            timeout=settings.claude_timeout,
        )
    # agy 는 항상 데몬 전용 프로필로, Vault 를 워크스페이스로 실행한다 (agy_profile 참고)
    wrapper, env = agy_profile.prepare(
        settings.agy_home, settings.vault, settings.agy_bin, settings.agy_bwrap,
        own_token=settings.agy_own_token, shared=settings.shared,
    )
    return route, CLIRequest(
        Engine.AGY,
        # agy 에는 시스템 프롬프트 옵션이 없어 매 요청 앞에 지침을 붙인다
        f"{agy_instructions(settings.vault)}\n\n{prompt}",
        model=settings.agy_model,
        resume=resume,
        cwd=str(settings.vault),
        timeout=settings.agy_timeout,
        wrapper=wrapper,
        env=env,
        extra_args=["--effort", settings.agy_effort] if settings.agy_effort else [],
    )


def agy_instructions(vault) -> str:
    """agy 가 간단한 질문에도 하위 에이전트로 Vault 를 탐색하다 타임아웃되는 것을 막는 지침.

    실측(2026-10-22): "방금 뭐라고 물었지?" 에 셸 명령 시도 → 거부 → invoke_subagent 로
    2분간 모델 30회 호출 후 타임아웃. 하위 에이전트는 권한 규칙으로 끌 수 없다.
    일정 조회 요청에는 캘린더 파일 경로를 추측해 read_file 을 57단계 반복하다 타임아웃.
    """
    return (
        "[작업 지침] 당신은 Slack 으로 대화하는 개인 비서다. 한국어로 간결하게 답한다.\n"
        "- 대화 내용이나 일반 지식으로 답할 수 있으면 도구를 쓰지 말고 바로 답한다.\n"
        "- 하위 에이전트(invoke_subagent, define_subagent)와 schedule 도구는 사용하지 않는다.\n"
        "- 셸 명령은 실행할 수 없다. 파일은 read_file 로 읽고, 필요한 파일만 최소한으로 연다.\n"
        f"- 노트·문서를 찾을 때는 먼저 {vault}/_Indexes/master_index.md 를 읽고, 거기서 고른 파일만 연다. "
        "문서(PDF 등)는 원본이 아니라 목록에 있는 _Extracted 요약본을 연다. "
        "지난 대화는 같은 파일의 '대화 기록' 섹션(날짜_첫 질문)에서 고른다.\n"
        "- 프로젝트·조직·인물이 걸린 질문(\"A사에서 만든 발표자료\", \"프로젝트X 관련 문서\")은 master_index 의 '엔터티' "
        f"섹션에서 고른 {vault}/_Entities/ 노트를 먼저 연다. 그 노트에 관계(상위·수행조직·소속)와 문서 유형별 목록이 있다.\n"
        "- 경로를 추측해 파일을 반복해서 열지 않는다. 두세 번 찾아도 없으면 없다고 답한다.\n"
        "- 일정(Google Calendar)·메일(Gmail) 조회와 일정 추가·메일 초안은 시스템이 따로 처리하고, 그 결과는 "
        "[이전 대화]에 '봇(calendar)', '봇(gmail)' 등으로 실려 온다. 질문이 이전 결과에 대한 것이면(\"이 중에서 …\", "
        "\"그 일정 몇 시야?\") 그 내용을 근거로 바로 답한다. 이전 결과로 답할 수 없는 새 조회가 필요할 때만, 도구로 "
        "찾지 말고 '\"11월 일정 보여줘\"처럼 기간을 넣어 다시 물어봐 달라'고 안내한다.\n"
        "- 당신은 알림·예약·할 일을 등록하거나 일정을 추가할 수 없다. 그런 부탁을 받으면 '알려드릴게요', '등록했어요'처럼 "
        "된 것처럼 답하지 말고, 아직 등록되지 않았다고 밝힌 뒤 '\"11월 2일 오전 7시 15분에 약 챙기라고 알려줘\"처럼 "
        "날짜·시각과 함께 다시 말해 달라'고 안내한다.\n"
        f"- 노트 저장소(Obsidian Vault)는 {vault} 이다. 파일 쓰기는 {vault}/_Agent_Inbox/ 에만 가능하다."
    )
