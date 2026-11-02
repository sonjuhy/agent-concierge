import json
from pathlib import Path

import pytest

from agent import agy_profile


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    token = tmp_path / "global-token"
    token.write_text("tok")
    monkeypatch.setattr(agy_profile, "GLOBAL_TOKEN", token)
    return tmp_path / "agy-home", vault, token


def test_render_settings_denies_dangerous_actions(env):
    home, vault, _ = env
    for name in ("AI Agent Project", ".obsidian", "_Agent_Inbox"):
        (vault / name).mkdir()
    (vault / "Vault.md").write_text("x")
    s = agy_profile.render_settings(home, vault)
    deny = s["permissions"]["deny"]
    assert s["permissions"]["allow"] == [f"write_file({vault / '_Agent_Inbox'})"]
    for rule in ("command(*)", "read_url(*)", "execute_url(*)", "mcp(*)", f"read_file({home})"):
        assert rule in deny
    for name in ("AI Agent Project", ".obsidian", "Vault.md"):
        assert f"write_file({vault / name})" in deny
    assert not any("_Agent_Inbox" in r for r in deny)
    assert f"read_file({vault / '_Chat_Logs' / '_untrusted'})" in deny


def test_ensure_profile_creates_vault_layout(env):
    home, vault, _ = env
    agy_profile.prepare(home, vault, "agy", use_bwrap=False)
    for d in agy_profile.VAULT_DIRS:
        assert (vault / d).is_dir()
    deny = json.loads(agy_profile.settings_path(home).read_text())["permissions"]["deny"]
    # 방금 만든 _ 폴더들도 Inbox 를 빼고 모두 쓰기 금지
    assert f"write_file({vault / '_Indexes'})" in deny
    assert f"write_file({vault / '_Assets'})" in deny
    assert f"write_file({vault / '_Chat_Logs'})" in deny  # 대화 기록은 Python 데몬만 쓴다


def test_ensure_profile_copies_token_once_and_rewrites_settings(env):
    home, vault, token = env
    agy_profile.prepare(home, vault, "agy", use_bwrap=False)
    copied = home / ".gemini/antigravity-cli" / agy_profile.TOKEN_NAME
    assert copied.read_text() == "tok"
    assert oct(copied.stat().st_mode & 0o777) == "0o600"
    assert (vault / "_Agent_Inbox").is_dir()

    # agy 가 설정을 덮어써도 다음 실행 전에 복구되고, 이미 있는 토큰은 건드리지 않는다
    sp = agy_profile.settings_path(home)
    sp.write_text("{}")
    copied.write_text("refreshed")
    token.write_text("global-changed")
    agy_profile.prepare(home, vault, "agy", use_bwrap=False)
    assert "command(*)" in json.loads(sp.read_text())["permissions"]["deny"]
    assert copied.read_text() == "refreshed"


def test_prepare_without_bwrap_swaps_home(env):
    home, vault, _ = env
    wrapper, extra_env = agy_profile.prepare(home, vault, "agy", use_bwrap=False)
    assert wrapper == [] and extra_env == {"HOME": str(home)}


def test_prepare_with_bwrap_hides_real_home(env):
    home, vault, _ = env
    wrapper, extra_env = agy_profile.prepare(home, vault, "/bin/true", use_bwrap=True)
    assert wrapper[0] == "bwrap" and extra_env == {}
    pairs = list(zip(wrapper, wrapper[1:], wrapper[2:]))
    real_home = str(Path("~").expanduser())
    assert ("--tmpfs", real_home) in list(zip(wrapper, wrapper[1:]))
    assert ("--ro-bind", str(vault), str(vault)) in pairs
    assert ("--bind", str(vault / "_Agent_Inbox"), str(vault / "_Agent_Inbox")) in pairs
    assert ("--setenv", "HOME", str(home)) in pairs
    # 실제 홈 tmpfs 는 다시 붙이는 바인드들보다 먼저 와야 한다
    assert wrapper.index(real_home) < wrapper.index(str(home))


def test_missing_vault_or_token(env, tmp_path, monkeypatch):
    home, vault, _ = env
    with pytest.raises(FileNotFoundError):
        agy_profile.prepare(home, tmp_path / "nope", "agy", use_bwrap=False)
    monkeypatch.setattr(agy_profile, "GLOBAL_TOKEN", tmp_path / "no-token")
    with pytest.raises(FileNotFoundError):
        agy_profile.prepare(home, vault, "agy", use_bwrap=False)
