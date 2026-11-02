"""환경 변수 기반 설정. 운영 환경에서는 systemd EnvironmentFile 이 .env 를 주입한다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """개발용 최소 .env 로더. 이미 설정된 환경 변수는 덮어쓰지 않는다."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# 색인 원본 루트 기준 상대 경로(glob 가능). 민감 폴더는 기본 제외하고, 필요하면 .env 에서 뺀다.
DEFAULT_DOC_EXCLUDES: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    agy_bin: str
    claude_bin: str
    agy_model: str | None
    claude_model: str | None
    untrusted_model: str
    agy_timeout: float
    claude_timeout: float
    vault: Path
    agy_home: Path = Path("data/agy-home")
    agy_bwrap: bool = True
    agy_effort: str | None = None  # low | medium | high (agy --effort)
    # 멀티 사용자: 가족 프로필은 소유자 agy 토큰을 복사하지 않는다 / 모두 쓰는 공용 폴더
    agy_own_token: bool = False
    shared: Path | None = None
    # 문서 색인: 원본은 제자리에 두고 요약본만 Vault/_Extracted 에 만든다
    doc_sources: tuple[Path, ...] = ()
    doc_excludes: tuple[str, ...] = DEFAULT_DOC_EXCLUDES
    doc_max_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        vault = Path(env("AGENT_VAULT") or "~/Obsidian").expanduser().resolve()
        return cls(
            agy_bin=env("AGENT_AGY_BIN") or "agy",
            claude_bin=env("AGENT_CLAUDE_BIN") or "claude",
            agy_model=env("AGENT_AGY_MODEL") or None,
            claude_model=env("AGENT_CLAUDE_MODEL") or None,
            untrusted_model=env("AGENT_UNTRUSTED_MODEL") or "claude-haiku-5-5",
            agy_timeout=float(env("AGENT_AGY_TIMEOUT") or 120),
            claude_timeout=float(env("AGENT_CLAUDE_TIMEOUT") or 300),
            vault=vault,
            agy_home=Path(env("AGENT_AGY_HOME") or "~/agent/data/agy-home").expanduser().resolve(),
            agy_bwrap=(env("AGENT_AGY_BWRAP") or "1") != "0",
            agy_effort=env("AGENT_AGY_EFFORT") or None,
            doc_sources=tuple(
                Path(p).expanduser().resolve()
                for p in _split(env("AGENT_DOC_SOURCES")) or ["~/Documents", str(vault / "_Assets")]
            ),
            doc_excludes=tuple(_split(env("AGENT_DOC_EXCLUDES"))) or DEFAULT_DOC_EXCLUDES,
            doc_max_bytes=int(float(env("AGENT_DOC_MAX_MB") or 50) * 1024 * 1024),
        )


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]
