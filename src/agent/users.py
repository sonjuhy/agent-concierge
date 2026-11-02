"""사용자 목록 (멀티 사용자). docs/MULTI_USER.md 참고.

data/users.yaml 이 없으면 기존 1인 구성(SLACK_ALLOWED_USER_ID + .env 의 Vault·agy 프로필·Google 토큰)으로 동작한다.

    shared: ~/Family/공유                  # 모두 읽고 쓰는 공용 폴더 (선택)
    users:
      - name: 나
        slack_id: U0XXXXXXXXX
        role: owner                         # owner: claude(--deep·메일 요약)·문서 색인·헬스체크
      - name: 가족A
        slack_id: U0XXXXXXX
        role: family                        # family: agy 만 사용, 메일은 목록만

owner 는 .env 의 Vault·agy 프로필·Google 토큰을 그대로 쓴다.
family 는 ~/Family/<이름>/ (폴더), data/users/<이름>/agy-home, data/users/<이름>/google_token.json 을 쓴다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from agent.config import Settings

ROOT = Path(__file__).resolve().parents[2]
USERS_FILE = ROOT / "data" / "users.yaml"
FAMILY_ROOT = Path("~/Family").expanduser()
OWNER_GOOGLE_TOKEN = ROOT / "data" / "google_token.json"


@dataclass(frozen=True)
class User:
    name: str
    slack_id: str
    role: str  # owner | family
    vault: Path
    agy_home: Path
    google_token: Path

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    def settings(self, base: Settings, shared: Path | None = None) -> Settings:
        """이 사용자의 폴더·agy 프로필로 바꾼 설정 (라우터·agy 격리가 이 값을 쓴다)."""
        return replace(base, vault=self.vault, agy_home=self.agy_home,
                       agy_own_token=not self.is_owner, shared=shared)


@dataclass(frozen=True)
class Users:
    users: tuple[User, ...]
    shared: Path | None = None

    def by_slack(self, slack_id: str | None) -> User | None:
        return next((u for u in self.users if u.slack_id == slack_id), None)

    def by_name(self, name: str | None) -> User | None:
        return next((u for u in self.users if u.name == name), None)

    @property
    def owner(self) -> User:
        return next(u for u in self.users if u.is_owner)

    @classmethod
    def load(cls, base: Settings, path: Path = USERS_FILE, data_dir: Path = ROOT / "data") -> Users:
        """data_dir: 가족의 agy 프로필·Google 토큰을 둘 곳 (테스트는 임시 폴더)."""
        owner_id = os.environ.get("SLACK_ALLOWED_USER_ID", "")
        if not path.exists():
            return cls((User("owner", owner_id, "owner", base.vault, base.agy_home, OWNER_GOOGLE_TOKEN),))
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        users = []
        for u in data.get("users") or []:
            name, role = str(u["name"]), u.get("role", "family")
            if role == "owner":
                users.append(User(name, str(u.get("slack_id") or owner_id), "owner",
                                  base.vault, base.agy_home, OWNER_GOOGLE_TOKEN))
            else:
                home = data_dir / "users" / name
                vault = Path(u.get("vault") or FAMILY_ROOT / name).expanduser()
                users.append(User(name, str(u["slack_id"]), "family", vault,
                                  home / "agy-home", home / "google_token.json"))
        if sum(u.is_owner for u in users) != 1:
            raise ValueError("users.yaml 에 owner 가 정확히 한 명 있어야 합니다")
        if len({u.slack_id for u in users}) != len(users) or len({u.name for u in users}) != len(users):
            raise ValueError("users.yaml 의 name·slack_id 가 중복됩니다")
        for u in users:  # 검증을 통과한 뒤에만 가족 폴더를 만든다
            if not u.is_owner:
                u.vault.mkdir(parents=True, exist_ok=True)
        shared = data.get("shared")
        return cls(tuple(users), Path(shared).expanduser() if shared else None)
