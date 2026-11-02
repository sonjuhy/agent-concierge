from collections import Counter
from pathlib import Path, PurePosixPath

import pytest

from agent.config import DEFAULT_DOC_EXCLUDES, Settings
from indexer.sources import is_excluded, iter_documents, shadow_path

EXCLUDES = ("SecondBrain", "개인")


def touch(p: Path, size: int = 10) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


@pytest.fixture
def docs_root(tmp_path):
    root = tmp_path / "Documents"
    touch(root / "채용" / "공고.pdf")
    touch(root / "채용" / "공고.pptx")
    touch(root / "채용" / "2024" / "old.pdf")
    touch(root / "HANBIT" / "deck.PPTX")
    touch(root / "HANBIT" / "notes.txt")
    touch(root / "HANBIT" / "~$deck.pptx")
    touch(root / "HANBIT" / "big.pdf", size=2000)
    touch(root / "개인" / "contract.pdf")
    touch(root / "SecondBrain" / "a.pdf")
    touch(root / "proj" / ".git" / "x.pdf")
    touch(root / "proj" / "node_modules" / "y.pdf")
    return root


def rels(docs):
    return sorted(str(d.rel) for d in docs)


def test_scan_applies_excludes_extensions_temp_and_size(docs_root):
    skipped = Counter()
    docs = list(iter_documents([docs_root], EXCLUDES, max_bytes=1000, skipped=skipped))
    assert rels(docs) == ["HANBIT/deck.PPTX", "채용/2024/old.pdf", "채용/공고.pdf", "채용/공고.pptx"]
    assert skipped["too large"] == 1
    assert skipped["excluded file"] == 1  # ~$deck.pptx


def test_glob_exclude_on_subfolder(docs_root):
    docs = list(iter_documents([docs_root], ["채용/20*"], max_bytes=10_000))
    assert "채용/2024/old.pdf" not in rels(docs)
    assert "채용/공고.pdf" in rels(docs)
    # 기본 제외를 덮어쓰면 민감 폴더도 포함된다
    assert "개인/contract.pdf" in rels(docs)


def test_missing_source_is_skipped(tmp_path):
    skipped = Counter()
    assert list(iter_documents([tmp_path / "nope"], [], 10, skipped=skipped)) == []
    assert skipped["missing source"] == 1


def test_is_excluded_matches_parent_prefix():
    assert is_excluded(PurePosixPath("USB/a/b.pdf"), ["USB"])
    assert not is_excluded(PurePosixPath("USBX/b.pdf"), ["USB"])
    assert is_excluded(PurePosixPath("a/.git/b.pdf"), [])


def test_shadow_path_keeps_extension_and_tree(docs_root, tmp_path):
    vault = tmp_path / "vault"
    docs = {str(d.rel): d for d in iter_documents([docs_root], EXCLUDES, 1000)}
    pdf = shadow_path(docs["채용/공고.pdf"], vault)
    pptx = shadow_path(docs["채용/공고.pptx"], vault)
    assert pdf == vault / "_Extracted/Local_Docs/Documents/채용/공고.pdf.md"
    assert pdf != pptx


def test_settings_doc_defaults(monkeypatch, tmp_path):
    for k in ("AGENT_DOC_SOURCES", "AGENT_DOC_EXCLUDES", "AGENT_DOC_MAX_MB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENT_VAULT", str(tmp_path / "vault"))
    s = Settings.from_env()
    assert s.doc_sources == (Path("~/Documents").expanduser().resolve(), (tmp_path / "vault" / "_Assets").resolve())
    assert s.doc_excludes == DEFAULT_DOC_EXCLUDES
    assert s.doc_max_bytes == 50 * 1024 * 1024

    monkeypatch.setenv("AGENT_DOC_SOURCES", f"{tmp_path}/a, {tmp_path}/b")
    monkeypatch.setenv("AGENT_DOC_EXCLUDES", "x, y/*")
    monkeypatch.setenv("AGENT_DOC_MAX_MB", "1.5")
    s = Settings.from_env()
    assert s.doc_sources == (tmp_path / "a", tmp_path / "b")
    assert s.doc_excludes == ("x", "y/*")
    assert s.doc_max_bytes == int(1.5 * 1024 * 1024)
