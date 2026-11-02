"""_Indexes/ontology_schema.yaml 로딩. 프로젝트·태그·문서 유형·엔터티(조직·인물)의 단일 기준표.

관계 중 바뀌지 않는 사실(프로젝트 상위·수행조직, 인물 소속)은 사람이 기준표에 적는다.
기준표에 없는 이름을 가리키는 관계는 버리고 경고만 남긴다 (오타가 엔터티를 새로 만들지 않게).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

FALLBACK_PROJECT = "기타"
ENTITY_KINDS = ("조직", "인물")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Project:
    name: str
    paths: tuple[str, ...]
    description: str
    parent: str | None = None  # 상위 프로젝트
    org: str | None = None  # 수행조직


@dataclass(frozen=True)
class Affiliation:
    org: str
    start: str = ""  # YYYY-MM
    end: str = ""

    def period(self) -> str:
        return f"{self.start}~{self.end}" if self.start or self.end else ""


@dataclass(frozen=True)
class Entity:
    kind: str  # ENTITY_KINDS
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    affiliations: tuple[Affiliation, ...] = ()  # 인물 → 조직 (소속)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


@dataclass(frozen=True)
class Ontology:
    projects: tuple[Project, ...]
    doc_types: tuple[str, ...]
    tags: tuple[str, ...]
    entities: tuple[Entity, ...] = ()

    @classmethod
    def load(cls, path: Path) -> Ontology:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("entities") or {}
        entities = tuple(
            Entity(kind, str(e["name"]), tuple(str(a) for a in e.get("aliases") or ()), e.get("description") or "",
                   tuple(Affiliation(str(a["org"]), str(a.get("from") or ""), str(a.get("to") or ""))
                         for a in e.get("소속") or ()))
            for kind in ENTITY_KINDS for e in raw.get(kind) or ()
        )
        orgs = {e.name for e in entities if e.kind == "조직"}
        names = {p["name"] for p in data.get("projects") or ()}

        def ref(value, known: set[str], what: str, owner: str) -> str | None:
            if value and str(value) not in known:
                log.warning("ontology: %s 의 %s '%s' 이(가) 기준표에 없어 무시합니다", owner, what, value)
                return None
            return str(value) if value else None

        projects = tuple(
            Project(p["name"], tuple(p.get("paths") or ()), p.get("description") or "",
                    ref(p.get("parent"), names - {p["name"]}, "상위 프로젝트", p["name"]),
                    ref(p.get("org"), orgs, "수행조직", p["name"]))
            for p in data.get("projects") or ()
        )
        if not any(p.name == FALLBACK_PROJECT for p in projects):
            projects += (Project(FALLBACK_PROJECT, (), ""),)
        entities = tuple(
            Entity(e.kind, e.name, e.aliases, e.description,
                   tuple(a for a in e.affiliations if ref(a.org, orgs, "소속", e.name)))
            for e in entities
        )
        doc_types = tuple(data.get("doc_types") or ()) or ("기타",)
        return cls(projects, doc_types, tuple(data.get("tags") or ()), entities)

    @property
    def project_names(self) -> list[str]:
        return [p.name for p in self.projects]

    def names_of(self, kind: str) -> list[str]:
        return [e.name for e in self.entities if e.kind == kind]

    def resolve(self, name: str, kind: str | None = None) -> str | None:
        """이름·별칭(대소문자 무시)을 기준표의 대표 이름으로. 없으면 None."""
        key = str(name).strip().casefold()
        for e in self.entities:
            if (kind is None or e.kind == kind) and key in (n.casefold() for n in e.names):
                return e.name
        return None

    def project(self, name: str) -> Project | None:
        return next((p for p in self.projects if p.name == name), None)

    def ancestors(self, name: str) -> list[str]:
        """상위 프로젝트들 (가까운 순). 순환이 있어도 멈춘다."""
        out: list[str] = []
        p = self.project(name)
        while p and p.parent and p.parent not in out and p.parent != name:
            out.append(p.parent)
            p = self.project(p.parent)
        return out

    def org_of(self, name: str) -> str | None:
        """프로젝트의 수행조직. 직접 적지 않았으면 상위 프로젝트에서 물려받는다."""
        for n in (name, *self.ancestors(name)):
            if (p := self.project(n)) and p.org:
                return p.org
        return None

    def project_for(self, root_name: str, rel: PurePosixPath) -> str | None:
        """경로 접두사로 프로젝트를 정한다. 가장 긴 접두사가 이긴다. 없으면 None (AI 가 고름)."""
        full = f"{root_name}/{rel}"
        best: tuple[int, str] | None = None
        for p in self.projects:
            for prefix in p.paths:
                prefix = prefix.strip("/")
                if (full == prefix or full.startswith(prefix + "/")) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), p.name)
        return best[1] if best else None

    def clean_tags(self, tags: list[str], limit: int = 5) -> list[str]:
        allowed = set(self.tags)
        out: list[str] = []
        for t in tags:
            if t in allowed and t not in out:
                out.append(t)
        return out[:limit]
