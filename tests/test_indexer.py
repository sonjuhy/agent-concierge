import os
import time
from pathlib import Path, PurePosixPath

import frontmatter
import pymupdf
import pytest

from agent import agy_profile
from agent.cli_runner import CLIResult, Engine
from agent.config import Settings
from indexer import build_index, pipeline, summarize as summ
from indexer.extract import extract
from indexer.hashdb import HashDB, sha256_file
from indexer.ontology import Ontology

ONTO = """
version: 1
projects:
  - {name: HANBIT, paths: [Documents/HANBIT], description: 회사 프로젝트}
  - {name: Sub, paths: [Documents/HANBIT/sub]}
  - {name: Free, paths: []}
doc_types: [보고서, 기타]
tags: [예산, 일정, AI]
"""


def make_pdf(path: Path, text: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.save(path)


@pytest.fixture
def onto(tmp_path):
    p = tmp_path / "onto.yaml"
    p.write_text(ONTO, encoding="utf-8")
    return Ontology.load(p)


def test_ontology_project_for_longest_prefix_and_fallback(onto):
    assert onto.project_for("Documents", PurePosixPath("HANBIT/a.pdf")) == "HANBIT"
    assert onto.project_for("Documents", PurePosixPath("HANBIT/sub/b.pdf")) == "Sub"
    assert onto.project_for("Documents", PurePosixPath("HANBITX/a.pdf")) is None
    assert "기타" in onto.project_names  # 자동 추가
    assert onto.clean_tags(["AI", "없는태그", "AI", "예산"]) == ["AI", "예산"]


def test_hashdb_roundtrip(tmp_path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    db = HashDB(":memory:")
    db.upsert(f, sha256_file(f), 5, 1.0, "ok", shadow=tmp_path / "a.md")
    row = db.get(f)
    assert row.status == "ok" and row.shadow.endswith("a.md")
    db.touch(f, 6, 2.0)
    assert db.get(f).mtime == 2.0
    assert db.stats() == {"ok": 1}
    db.delete(str(f))
    assert db.get(f) is None


def test_extract_pdf_text_scanned_and_unsupported(tmp_path):
    make_pdf(tmp_path / "t.pdf", "Quarterly budget plan for logistics automation. " * 3)
    make_pdf(tmp_path / "blank.pdf", None)
    (tmp_path / "a.hwp").write_bytes(b"x")
    ok = extract(tmp_path / "t.pdf")
    assert ok.status == "ok" and "budget" in ok.text and ok.pages == 1
    assert extract(tmp_path / "blank.pdf").status == "needs_ocr"
    assert extract(tmp_path / "a.hwp").status == "unsupported"


async def test_summarize_uses_untrusted_claude_and_filters(monkeypatch, onto, tmp_path):
    seen = {}

    async def fake_run(req, binary):
        seen["req"] = req
        return CLIResult(Engine.CLAUDE, ok=True, cost_usd=0.002, raw={"structured_output": {
            "summary": "예산 계획", "key_points": ["1.2억"], "doc_type": "없는유형",
            "tags": ["예산", "가짜"], "project": "Free", "tag_candidates": ["물류", "AI"]}})

    monkeypatch.setattr(summ, "run_cli", fake_run)
    s = settings_for(tmp_path, monkeypatch)
    out = await summ.summarize(s, onto, "x" * (summ.MAX_INPUT_CHARS + 10), "Documents/HANBIT/a.pdf", "file:a", "HANBIT")
    req = seen["req"]
    assert req.engine is Engine.CLAUDE and not req.trusted
    assert "--json-schema" in req.extra_args and "--max-budget-usd" in req.extra_args
    assert "앞부분만" in req.prompt and "'HANBIT' 로 고정" in req.prompt
    assert req.prompt.count("x") <= summ.MAX_INPUT_CHARS + 10
    # 허용 태그(AI)가 후보에 들어오면 tags 로 옮긴다
    assert (out.project, out.doc_type, out.tags, out.tag_candidates) == ("HANBIT", "기타", ["예산", "AI"], ["물류"])


def settings_for(tmp_path, monkeypatch, sources=()):
    vault = tmp_path / "vault"
    (vault / "_Indexes").mkdir(parents=True, exist_ok=True)
    (vault / "_Indexes" / "ontology_schema.yaml").write_text(ONTO, encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setattr(agy_profile, "GLOBAL_TOKEN", token)
    return Settings(
        agy_bin="agy", claude_bin="claude", agy_model=None, claude_model=None,
        untrusted_model="claude-haiku-5-5", agy_timeout=120, claude_timeout=300,
        vault=vault, agy_home=tmp_path / "agy-home", agy_bwrap=False,
        doc_sources=tuple(sources), doc_excludes=(), doc_max_bytes=10_000_000,
    )


def age(path: Path, seconds: float = 3600) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


async def test_pipeline_incremental(monkeypatch, tmp_path):
    docs = tmp_path / "Documents"
    a, blank = docs / "HANBIT" / "a.pdf", docs / "b.pdf"
    make_pdf(a, "Budget plan " * 10)
    make_pdf(blank, None)
    fresh = docs / "fresh.pdf"
    make_pdf(fresh, "still syncing " * 10)
    for p in (a, blank):
        age(p)
    calls = []

    async def fake_summarize(settings, onto, text, rel, source, fixed):
        calls.append(rel)
        return summ.Summary("요약 " + rel, ["포인트"], "보고서", ["예산"], fixed or "기타")

    monkeypatch.setattr(pipeline, "summarize", fake_summarize)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    s = settings_for(tmp_path, monkeypatch, [docs])

    c = await pipeline.run(s, limit=None, dry_run=False, reindex=False)
    assert calls == ["Documents/HANBIT/a.pdf"]
    assert (c["ok"], c["needs_ocr"], c["settling"]) == (1, 1, 1)
    shadow = s.vault / "_Extracted/Local_Docs/Documents/HANBIT/a.pdf.md"
    meta = frontmatter.load(shadow).metadata
    assert meta["project"] == "HANBIT" and meta["source_file"] == str(a) and meta["tags"] == ["예산"]
    index = (s.vault / "_Indexes/master_index.md").read_text()
    assert "### HANBIT" in index and "a.pdf.md|a.pdf" in index

    # 두 번째 실행: 바뀐 게 없으면 요약하지 않는다
    calls.clear()
    c = await pipeline.run(s, limit=None, dry_run=False, reindex=False)
    assert calls == [] and c["unchanged"] == 2

    # 내용이 바뀌면 다시 요약, 지우면 요약본도 지운다
    make_pdf(a, "Changed plan " * 10)
    age(a, 1800)
    blank.unlink()
    c = await pipeline.run(s, limit=None, dry_run=False, reindex=False)
    assert calls == ["Documents/HANBIT/a.pdf"] and c["deleted"] == 1
    a.unlink()
    age(fresh)
    calls.clear()
    c = await pipeline.run(s, limit=None, dry_run=False, reindex=False)
    assert not shadow.exists() and c["deleted"] == 1 and calls == ["Documents/fresh.pdf"]


async def test_reindex_only_subtree_keeps_others(monkeypatch, tmp_path):
    docs = tmp_path / "Documents"
    a, b = docs / "자격증" / "a.pdf", docs / "HANBIT" / "b.pdf"
    for p in (a, b):
        make_pdf(p, "Exam questions " * 10)
        age(p)
    calls = []

    async def fake_summarize(settings, onto, text, rel, source, fixed):
        calls.append(rel)
        return summ.Summary("s", [], "기타", [], fixed or "기타")

    monkeypatch.setattr(pipeline, "summarize", fake_summarize)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    s = settings_for(tmp_path, monkeypatch, [docs])
    await pipeline.run(s, limit=None, dry_run=False, reindex=False)
    calls.clear()

    c = await pipeline.run(s, limit=None, dry_run=False, reindex=True, only="Documents/자격증/")
    assert calls == ["Documents/자격증/a.pdf"]
    assert c["out_of_scope"] == 1 and c["deleted"] == 0
    assert (s.vault / "_Extracted/Local_Docs/Documents/HANBIT/b.pdf.md").exists()


def test_build_index_lists_docs_and_notes(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Career").mkdir(parents=True)
    (vault / "Career" / "지원 현황.md").write_text("x")
    (vault / "Vault.md").write_text("x")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "x.md").write_text("x")
    d = vault / "_Extracted/Local_Docs/Documents/a.pdf.md"
    d.parent.mkdir(parents=True)
    d.write_text(frontmatter.dumps(frontmatter.Post("body", project="P", summary="요약문", tags=["AI"],
                                                    doc_type="보고서", source_file="/x/a.pdf")))
    out = build_index.build(vault)
    assert "## 문서 (1)" in out and "### P" in out
    assert "[[_Extracted/Local_Docs/Documents/a.pdf.md|a.pdf]] (보고서) 요약문 #AI" in out
    assert "## 노트 (2)" in out and "[[Career/지원 현황]]" in out and ".obsidian" not in out
