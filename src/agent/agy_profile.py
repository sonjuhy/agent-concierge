"""데몬 전용 agy 실행 프로필. 사용자 전역 설정(~/.gemini)과 완전히 분리한다.

실측(2026-10-22, agy 1.2.5)으로 확인한 사실과 그에 따른 구성:
- 강제됨: permissions.deny 의 command(*), read_file(<path>), write_file(<path>)
  → "Matches user-configured deny rule" 로 차단. 우선순위는 deny > ask > allow.
- print 모드에서 확인이 필요한 쓰기는 soft-deny 된다 → 쓰기는 allow 에 명시한 Inbox 만 가능.
- unsandboxed(...) 는 유효하지 않은 규칙이라 무시된다 (agy 로그 경고).
- 강제 안 됨: allowNonWorkspaceAccess=false (워크스페이스 밖 읽기/쓰기가 그대로 됨),
  disabledTools (도구 목록에서 빠지지 않음). 그래서 파일 시스템 범위는 bwrap 으로 제한한다.
- agy 는 settings.json 을 스스로 다시 쓴다 → 매 실행 전에 다시 렌더링한다.

계층:
  bwrap   : 실제 홈 숨김, Vault 는 읽기 전용, _Agent_Inbox 와 프로필 홈만 쓰기 가능
  deny    : 프로필 홈(OAuth 토큰·settings.json) 읽기/쓰기, 셸 명령, URL 접근, MCP,
            _Agent_Inbox 를 뺀 Vault 루트 항목 쓰기 금지 (bwrap 을 끈 경우의 방어선)
  HOME    : 프로필 홈으로 교체 → 전역 allow 목록·trustedWorkspaces 가 섞이지 않음
남는 위험: search_web / send_message / schedule 도구는 끌 수 없다. 그래서 이 프로필은
신뢰된 요청 전용이며, 외부 텍스트는 router 가 계속 도구 없는 claude 로 보낸다.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from agent.chatlog import LOG_DIR, UNTRUSTED_DIR

GLOBAL_TOKEN = Path("~/.gemini/antigravity-cli/antigravity-oauth-token").expanduser()
TOKEN_NAME = "antigravity-oauth-token"
INBOX = "_Agent_Inbox"
# Vault 레이아웃 (사용자 노트는 루트에 자유 구조로 있다)
VAULT_DIRS = (
    "_Indexes", "_Extracted/Local_Docs", "_Extracted/GDrive", INBOX, "_Assets/pdf", "_Assets/pptx",
    f"{LOG_DIR}/{UNTRUSTED_DIR}",
)


def protected_vault_entries(vault: Path) -> list[Path]:
    """_Agent_Inbox 를 뺀 Vault 루트의 모든 항목(.obsidian 포함).

    deny 가 allow 보다 우선이라 'Vault 전체 금지 + Inbox 허용'은 표현할 수 없으므로,
    루트 항목을 하나씩 막는다. 설정은 매 실행 전에 다시 렌더링되므로 새 폴더도 반영된다.
    """
    return sorted(p for p in vault.iterdir() if p.name != INBOX)


def settings_path(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli" / "settings.json"


def render_settings(home: Path, vault: Path, shared: Path | None = None) -> dict:
    deny = [
        "command(*)",
        "read_url(*)",
        "execute_url(*)",
        "mcp(*)",
        # read 를 막으면 write 도 막힌다 (토큰 유출·설정 변조 방지)
        f"read_file({home})",
        # 오염된 스레드의 대화 기록: 외부 텍스트가 섞여 있어 agy 입력으로 다시 들어가면 안 된다
        f"read_file({vault / LOG_DIR / UNTRUSTED_DIR})",
        *(f"write_file({p})" for p in protected_vault_entries(vault)),
    ]
    allow = [f"write_file({vault / INBOX})"]
    if shared is not None:
        allow.append(f"write_file({shared})")  # 가족 공용 폴더 (장보기 목록 등)
    return {
        "allowNonWorkspaceAccess": False,  # 강제되지 않지만 의도를 남겨 둔다
        "trustedWorkspaces": [str(vault)] + ([str(shared)] if shared else []),
        # 확인이 필요한 쓰기는 print 모드에서 자동 거부(soft-deny)되므로 Inbox(와 공용 폴더)만 명시적으로 허용
        "permissions": {"allow": allow, "ask": [], "deny": deny},
    }


class NotLoggedIn(Exception):
    """agy 프로필에 로그인 토큰이 없다 (가족은 각자 로그인해야 한다)."""


def ensure_profile(home: Path, vault: Path, token_source: Path | None = GLOBAL_TOKEN,
                   shared: Path | None = None) -> None:
    """프로필 디렉터리·토큰·설정을 준비한다. 매 실행 전에 호출해도 되도록 멱등이다.

    token_source=None 이면 다른 계정의 토큰을 복사하지 않는다 (가족 프로필).
    """
    if not vault.is_dir():
        raise FileNotFoundError(f"vault not found: {vault}")
    for d in VAULT_DIRS:
        (vault / d).mkdir(parents=True, exist_ok=True)

    cfg_dir = settings_path(home).parent
    cfg_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)

    token = cfg_dir / TOKEN_NAME
    if not token.exists():
        if token_source is None:
            raise NotLoggedIn(f"agy 로그인이 필요합니다 (프로필: {home})")
        if not token_source.is_file():
            raise FileNotFoundError(f"agy token not found: {token_source} (run `agy` and log in first)")
        # 복사본은 이후 데몬 쪽에서 독립적으로 갱신된다
        shutil.copy2(token_source, token)
        os.chmod(token, 0o600)
    if shared is not None:
        shared.mkdir(parents=True, exist_ok=True)

    path = settings_path(home)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(render_settings(home, vault, shared), indent=2), encoding="utf-8")
    tmp.replace(path)


def prepare(home: Path, vault: Path, agy_binary: str, use_bwrap: bool, *, own_token: bool = False,
            shared: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """프로필을 준비하고 run_cli 에 넘길 (wrapper, env) 를 돌려준다.

    own_token=True 면 전역(소유자) agy 토큰을 복사하지 않고 그 프로필에 직접 로그인된 토큰만 쓴다.
    """
    ensure_profile(home, vault, None if own_token else GLOBAL_TOKEN, shared)
    if use_bwrap:
        return bwrap_prefix(home, vault, agy_binary, shared), {}
    return [], {"HOME": str(home)}


def bwrap_prefix(home: Path, vault: Path, agy_binary: str, shared: Path | None = None) -> list[str]:
    """agy 를 감쌀 bwrap 인자. 실제 홈은 tmpfs 로 가리고 필요한 경로만 다시 붙인다."""
    real_home = Path("~").expanduser()
    agy = str(Path(shutil.which(agy_binary) or agy_binary).resolve())
    return [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--tmpfs", str(real_home),
        "--ro-bind", agy, agy,
        "--bind", str(home), str(home),
        "--ro-bind", str(vault), str(vault),
        "--bind", str(vault / INBOX), str(vault / INBOX),
        *(["--bind", str(shared), str(shared)] if shared else []),
        "--chdir", str(vault),
        "--setenv", "HOME", str(home),
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
    ]
