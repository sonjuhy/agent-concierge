"""_Entities/ 엔터티 노트 생성 (순수 Python, 토큰 0).

기준표(ontology_schema.yaml)의 프로젝트·조직·인물마다 노트를 하나씩 만들고, 관계를 Obsidian 링크로 적는다.
문서 쪽 frontmatter 는 건드리지 않는다 — 엔터티 노트가 문서를 링크하므로 Obsidian 백링크·그래프에 그대로 잡힌다.

- 프로젝트: 상위·하위 프로젝트, 수행조직, 속한 문서(문서 유형별)
- 조직: 수행 프로젝트(하위 포함), 소속 인물, 발급·발행한 문서, 언급한 문서
- 인물: 소속(기간), 작성한 문서, 언급된 문서
- _후보.md: 요약 AI 가 제안한 기준표에 없는 조직 (기준표에 추가할지 사람이 고른다)

작성·발급·언급은 요약 AI 가 기준표 이름 안에서 고른 값(문서 frontmatter 의 author·issuer·mentions)이다.
언급에는 요약·핵심 요약에 이름·별칭이 문자열로 들어 있는 문서도 더한다 (재색인 전 문서도 잡히게).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from indexer.ontology import FALLBACK_PROJECT, Ontology

DIR = "_Entities"
CANDIDATES = f"{DIR}/_후보.md"
KIND_DIRS = {"프로젝트": "프로젝트", "조직": "조직", "인물": "인물"}
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')


@dataclass(frozen=True)
class Doc:
    rel: str  # Vault 기준 경로 (.md 포함)
    name: str  # 원본 파일 이름
    project: str
    doc_type: str
    text: str  # 요약 + 핵심 요약 (이름 찾기용)
    author: str = ""
    issuer: str = ""
    mentions: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


def _key_points(body: str) -> str:
    m = re.search(r"## AI 핵심 요약\n(.*?)(?:\n---|\Z)", body, re.S)
    return m.group(1) if m else ""


def load_docs(vault: Path) -> list[Doc]:
    out: list[Doc] = []
    for md in sorted((vault / "_Extracted").rglob("*.md")):
        try:
            post = frontmatter.load(md)
        except Exception:
            continue
        m = post.metadata
        rel = md.relative_to(vault).as_posix()
        out.append(Doc(rel, Path(str(m.get("source_file") or rel)).name, str(m.get("project") or FALLBACK_PROJECT),
                       str(m.get("doc_type") or "기타"), f"{m.get('summary') or ''}\n{_key_points(post.content)}",
                       str(m.get("author") or ""), str(m.get("issuer") or ""),
                       tuple(str(x) for x in m.get("mentions") or ()),
                       tuple(str(x) for x in m.get("entity_candidates") or ())))
    return out


def note_path(kind: str, name: str) -> str:
    """Vault 기준 엔터티 노트 경로 (.md 제외)."""
    return f"{DIR}/{KIND_DIRS[kind]}/{_UNSAFE.sub('_', name).strip() or '_'}"


def link(kind: str, name: str) -> str:
    return f"[[{note_path(kind, name)}|{name}]]"


def _doc_link(d: Doc) -> str:
    return f"[[{d.rel[:-3]}|{d.name}]]"


def _mentions(name: str, names: tuple[str, ...], docs: list[Doc]) -> list[Doc]:
    """AI 가 mentions 에 고른 문서 + 요약에 이름·별칭이 나오는 문서."""
    # 짧은 영문 별칭(예: WISE)이 다른 단어 속에서 잡히지 않게 영문은 단어 경계로 찾는다
    pats = [re.compile(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", re.I) if n.isascii() else re.compile(re.escape(n))
            for n in names if len(n) >= 2]
    return [d for d in docs if name in d.mentions or any(p.search(d.text) for p in pats)]


def _by_type(docs: list[Doc]) -> list[str]:
    groups: dict[str, list[Doc]] = defaultdict(list)
    for d in docs:
        groups[d.doc_type].append(d)
    out: list[str] = []
    for t in sorted(groups):
        out += ["", f"### {t} ({len(groups[t])})", *(f"- {_doc_link(d)}" for d in groups[t])]
    return out


def _note(kind: str, name: str, body: list[str], aliases: tuple[str, ...] = ()) -> str:
    meta = {"type": "entity", "kind": kind, "generated": True}
    if aliases:
        meta["aliases"] = list(aliases)
    header = ["> 자동 생성 — 직접 수정하지 마세요. 관계는 `_Indexes/ontology_schema.yaml` 에서 고칩니다.", ""]
    return frontmatter.dumps(frontmatter.Post("\n".join([f"# {name}", "", *header, *body]), **meta)) + "\n"


def render(onto: Ontology, docs: list[Doc]) -> dict[str, str]:
    """{Vault 기준 경로(.md 포함): 내용}"""
    by_project: dict[str, list[Doc]] = defaultdict(list)
    for d in docs:
        by_project[d.project].append(d)
    children: dict[str, list[str]] = defaultdict(list)
    for p in onto.projects:
        if p.parent:
            children[p.parent].append(p.name)
    orgs = [e for e in onto.entities if e.kind == "조직"]
    people = [e for e in onto.entities if e.kind == "인물"]
    notes: dict[str, str] = {}

    for p in onto.projects:
        if p.name == FALLBACK_PROJECT:
            continue
        rel: list[str] = []
        if p.description:
            rel += [p.description, ""]
        if p.parent:
            rel.append(f"- 상위 프로젝트: {link('프로젝트', p.parent)}")
        if children[p.name]:
            rel.append("- 하위 프로젝트: " + ", ".join(link("프로젝트", c) for c in children[p.name]))
        if org := onto.org_of(p.name):
            inherited = "" if p.org else " (상위 프로젝트에서)"
            rel.append(f"- 수행조직: {link('조직', org)}{inherited}")
        own = by_project[p.name]
        body = [*rel, "", f"## 문서 ({len(own)})", *_by_type(own)]
        notes[note_path("프로젝트", p.name) + ".md"] = _note("프로젝트", p.name, body)

    for o in orgs:
        projects = [p.name for p in onto.projects if onto.org_of(p.name) == o.name]
        members = [(e, a) for e in people for a in e.affiliations if a.org == o.name]
        project_docs = [d for n in projects for d in by_project[n]]
        issued = [d for d in docs if d.issuer == o.name]
        mentioned = [d for d in _mentions(o.name, o.names, docs) if d.project not in projects and d not in issued]
        body = [o.description, ""] if o.description else []
        if projects:
            body.append("- 수행 프로젝트: " + ", ".join(link("프로젝트", n) for n in projects))
        for e, a in members:
            body.append(f"- 소속 인물: {link('인물', e.name)}" + (f" ({a.period()})" if a.period() else ""))
        body += ["", f"## 수행 프로젝트의 문서 ({len(project_docs)})", *_by_type(project_docs)]
        body += ["", f"## 발급·발행한 문서 ({len(issued)})", *(f"- {_doc_link(d)} ({d.doc_type})" for d in issued)]
        body += ["", f"## 다른 문서에서 언급 ({len(mentioned)})", *(f"- {_doc_link(d)} ({d.project})" for d in mentioned)]
        notes[note_path("조직", o.name) + ".md"] = _note("조직", o.name, body, o.aliases)

    for e in people:
        body = [e.description, ""] if e.description else []
        for a in e.affiliations:
            body.append(f"- 소속: {link('조직', a.org)}" + (f" ({a.period()})" if a.period() else ""))
        authored = [d for d in docs if d.author == e.name]
        mentioned = [d for d in _mentions(e.name, e.names, docs) if d not in authored]
        body += ["", f"## 작성한 문서 ({len(authored)})", *_by_type(authored)]
        body += ["", f"## 언급된 문서 ({len(mentioned)})", *(f"- {_doc_link(d)} ({d.project})" for d in mentioned)]
        notes[note_path("인물", e.name) + ".md"] = _note("인물", e.name, body, e.aliases)

    candidates: dict[str, list[Doc]] = defaultdict(list)
    for d in docs:
        for c in d.candidates:
            if onto.resolve(c) is None:  # 그새 기준표에 추가됐으면 빼기
                candidates[c].append(d)
    if candidates:
        body = ["요약 AI 가 제안한 조직 중 기준표에 없는 것 (많이 나온 순). 필요한 것만 "
                "`_Indexes/ontology_schema.yaml` 의 entities.조직 에 추가하면 다음 색인부터 연결된다.", ""]
        for c, ds in sorted(candidates.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            body.append(f"- **{c}** ({len(ds)}): " + ", ".join(_doc_link(d) for d in ds[:5]))
        notes[CANDIDATES] = _note("후보", "엔터티 후보", body)
    return notes


def index_section(onto: Ontology) -> list[str]:
    """master_index.md 의 엔터티 섹션: 프로젝트 계층과 조직·인물 목록 (관계 상세는 각 노트에)."""
    children: dict[str | None, list[str]] = defaultdict(list)
    for p in onto.projects:
        if p.name != FALLBACK_PROJECT:
            children[p.parent].append(p.name)

    def tree(parent: str | None, depth: int) -> list[str]:
        lines = []
        for n in children[parent]:
            org = onto.org_of(n)
            lines.append("  " * depth + f"- {link('프로젝트', n)}" + (f" — {org}" if org and depth == 0 else ""))
            lines += tree(n, depth + 1)
        return lines

    orgs = [e for e in onto.entities if e.kind == "조직"]
    people = [e for e in onto.entities if e.kind == "인물"]
    out = ["## 엔터티", "", "> 프로젝트·조직·인물 사이의 관계와 관련 문서는 각 노트에 있다. 이런 질문은 해당 노트부터 연다.",
           "", "### 프로젝트", *tree(None, 0)]
    if orgs:
        out += ["", "### 조직", *(f"- {link('조직', o.name)}" + (f" ({', '.join(o.aliases)})" if o.aliases else "")
                                 for o in orgs)]
    if people:
        out += ["", "### 인물", *(f"- {link('인물', e.name)}" for e in people)]
    return out


def write(vault: Path, onto: Ontology) -> int:
    """노트를 새로 쓰고, 기준표에서 빠진 엔터티의 (자동 생성) 노트는 지운다. 쓴 노트 수를 돌려준다."""
    notes = render(onto, load_docs(vault))
    root = vault / DIR
    for old in root.rglob("*.md") if root.is_dir() else ():
        rel = old.relative_to(vault).as_posix()
        if rel not in notes:
            try:
                generated = frontmatter.load(old).metadata.get("generated") is True
            except Exception:
                generated = False
            if generated:  # 사용자가 직접 만든 노트는 지우지 않는다
                old.unlink()
    for rel, text in notes.items():
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue  # 바뀐 게 없으면 쓰지 않는다 (Syncthing 전송·mtime 변화 줄이기)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    return len(notes)
