"""_Indexes/master_index.md 생성 (순수 Python, 토큰 0).

AI 는 문서·노트를 찾을 때 이 파일을 먼저 읽고 필요한 파일만 연다 (점진적 탐색).
- 문서: _Extracted 의 섀도우 마크다운 YAML 헤더(요약·태그·유형)를 프로젝트별로 묶는다.
- 노트: 사용자 노트는 폴더별 제목 목록만 싣는다 (본문은 필요할 때 연다).
- 엔터티: 프로젝트 계층과 조직·인물 목록만 싣고, 관계·관련 문서는 _Entities/ 노트에 둔다 (indexer.entities).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

import frontmatter

from agent.chatlog import LOG_DIR, UNTRUSTED_DIR
from agent.context import KST
from indexer import entities
from indexer.ontology import Ontology

SKIP_DIRS = {".obsidian", ".git", ".trash", ".makemd", ".space", ".claude", ".stfolder", "_Extracted", "_Indexes", "_Assets", entities.DIR, LOG_DIR}


def _docs(vault: Path) -> dict[str, list[tuple[str, dict]]]:
    by_project: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for md in sorted((vault / "_Extracted").rglob("*.md")):
        try:
            meta = frontmatter.load(md).metadata
        except Exception:
            continue
        by_project[str(meta.get("project") or "기타")].append((md.relative_to(vault).as_posix(), meta))
    return by_project


def _notes(vault: Path) -> dict[str, list[str]]:
    by_folder: dict[str, list[str]] = defaultdict(list)
    for md in sorted(vault.rglob("*.md")):
        rel = md.relative_to(vault)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        folder = rel.parts[0] if len(rel.parts) > 1 else "(루트)"
        by_folder[folder].append(rel.as_posix())
    return by_folder


def _chats(vault: Path) -> dict[str, list[str]]:
    """월별 대화 기록 목록 (최근 달이 위). 오염된 스레드(_untrusted)는 AI 가 읽지 않으므로 싣지 않는다."""
    by_month: dict[str, list[str]] = defaultdict(list)
    root = vault / LOG_DIR
    if not root.is_dir():
        return by_month
    for md in sorted(root.rglob("*.md"), reverse=True):
        rel = md.relative_to(root)
        if rel.parts[0] == UNTRUSTED_DIR or len(rel.parts) < 3:
            continue
        by_month[f"{rel.parts[0]}-{rel.parts[1]}"].append(md.relative_to(vault).as_posix())
    return by_month


def load_ontology(vault: Path) -> Ontology | None:
    path = vault / "_Indexes" / "ontology_schema.yaml"
    return Ontology.load(path) if path.is_file() else None


def build(vault: Path, onto: Ontology | None = None) -> str:
    docs = _docs(vault)
    notes = _notes(vault)
    chats = _chats(vault)
    n_docs = sum(len(v) for v in docs.values())
    n_notes = sum(len(v) for v in notes.values())
    n_chats = sum(len(v) for v in chats.values())
    out = [
        "---",
        "type: index",
        f"updated: {datetime.now(KST):%Y-%m-%d %H:%M}",
        f"documents: {n_docs}",
        f"notes: {n_notes}",
        f"chats: {n_chats}",
        "---",
        "# Master Index",
        "",
        "> 자동 생성 파일 — 직접 수정하지 마세요 (`python -m indexer` 가 덮어씀).",
        "> AI: 먼저 이 목록에서 대상을 고른 뒤 해당 파일만 연다. 문서 원본(PDF 등)은 열지 말고 `_Extracted` 요약본을 연다.",
        "",
    ]
    if onto is not None:
        out += [*entities.index_section(onto), ""]
    out.append(f"## 문서 ({n_docs})")
    for project in sorted(docs):
        up = " > ".join(onto.ancestors(project)[::-1]) if onto is not None else ""
        out += ["", f"### {project}" + (f" (상위: {up})" if up else "")]
        for rel, m in docs[project]:
            tags = " ".join(f"#{t}" for t in m.get("tags") or [])
            name = Path(str(m.get("source_file") or rel)).name
            who = m.get("author")
            kind = f"{m.get('doc_type', '기타')} · 작성: {who}" if who and who not in ("타인", "불명") else m.get("doc_type", "기타")
            out.append(f"- [[{rel}|{name}]] ({kind}) {m.get('summary', '')} {tags}".rstrip())
    out += ["", f"## 노트 ({n_notes})"]
    for folder in sorted(notes):
        out += ["", f"### {folder}"]
        out += [f"- [[{rel[:-3]}]]" for rel in notes[folder]]
    # 파일 이름이 "YYYY-MM-DD_HHMM_<첫 질문>" 이라 제목만으로 날짜·주제를 고를 수 있다
    out += ["", f"## 대화 기록 ({n_chats})"]
    for month in sorted(chats, reverse=True):
        out += ["", f"### {month}"]
        out += [f"- [[{rel[:-3]}|{Path(rel).stem}]]" for rel in chats[month]]
    return "\n".join(out) + "\n"


def write(vault: Path) -> Path:
    """엔터티 노트와 master_index.md 를 다시 만든다."""
    onto = load_ontology(vault)
    if onto is not None:
        entities.write(vault, onto)
    path = vault / "_Indexes" / "master_index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(build(vault, onto), encoding="utf-8")
    tmp.replace(path)
    return path
