"""추출 텍스트 → 구조화된 요약 (summary, key_points, doc_type, tags, project, 엔터티 관계).

내려받은 문서에는 남이 쓴 글이 섞여 있으므로 router 의 untrusted 경로(도구 없는 claude)로만 보낸다.
claude 의 --json-schema 로 결과를 구조화하고, 태그·유형·프로젝트·엔터티는 온톨로지 목록으로 다시 걸러낸다.
엔터티 관계(작성·발급·언급)는 기준표에 있는 조직·인물만 고르게 하고, 없는 조직은 entity_candidates 로 받는다
(제3자 개인 이름이 쌓이지 않게 후보는 조직·기관만).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from agent.cli_runner import CLIResult, run_cli
from agent.config import Settings
from agent.router import ExternalContent, build_request
from indexer.ontology import FALLBACK_PROJECT, Ontology

# 요약 입력 상한. 앞부분이 문서 성격을 가장 잘 드러내고, 큰 XLSX(100만 자 이상)도 있어 자른다.
MAX_INPUT_CHARS = 30_000
MAX_BUDGET_USD = "0.10"
AUTHOR_OTHER, AUTHOR_UNKNOWN = "타인", "불명"


@dataclass
class Summary:
    summary: str
    key_points: list[str]
    doc_type: str
    tags: list[str]
    project: str
    tag_candidates: list[str] = field(default_factory=list)
    author: str | None = None  # 인물 이름 | 타인 | 불명 (기준표에 인물이 없으면 None)
    issuer: str | None = None  # 발급·발행 조직
    mentions: list[str] = field(default_factory=list)  # 언급된 조직·인물
    entity_candidates: list[str] = field(default_factory=list)  # 기준표에 없는 조직 이름
    cost_usd: float | None = None


class SummarizeError(Exception):
    pass


def json_schema(onto: Ontology) -> dict:
    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "문서가 무엇인지 1~3문장, 300자 이내"},
            "key_points": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                           "description": "핵심 수치·결정·일정 등 3~5개"},
            "doc_type": {"type": "string", "enum": list(onto.doc_types)},
            "tags": {"type": "array", "items": {"type": "string", "enum": list(onto.tags)}, "maxItems": 5},
            "tag_candidates": {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                               "description": "허용 목록에 없지만 필요한 주제어"},
            "project": {"type": "string", "enum": onto.project_names},
        },
        "required": ["summary", "key_points", "doc_type", "tags", "project"],
    }
    orgs, people = onto.names_of("조직"), onto.names_of("인물")
    props = schema["properties"]
    if people:
        props["author"] = {"type": "string", "enum": [*people, AUTHOR_OTHER, AUTHOR_UNKNOWN],
                           "description": "문서를 쓴 사람. 목록의 인물이 아니면 타인, 판단할 수 없으면 불명"}
    if orgs:
        props["issuer"] = {"type": "string", "enum": ["", *orgs],
                           "description": "증명서·공문·계약서·공고 등을 발급·발행한 조직. 없거나 목록에 없으면 빈 문자열"}
    if orgs or people:
        props["mentions"] = {"type": "array", "items": {"type": "string", "enum": [*orgs, *people]}, "maxItems": 5,
                             "description": "문서가 실제로 다루거나 언급하는 목록의 조직·인물 (author·issuer 와 겹쳐도 된다)"}
    props["entity_candidates"] = {"type": "array", "items": {"type": "string"}, "maxItems": 3,
                                  "description": "목록에 없지만 문서에서 중요한 조직·기관 이름 (개인 이름은 넣지 않는다)"}
    return schema


def _entities_rule(onto: Ontology) -> str:
    lines = []
    for e in onto.entities:
        alias = f" (다른 이름: {', '.join(e.aliases)})" if e.aliases else ""
        desc = f" — {e.description}" if e.description else ""
        lines.append(f"  - [{e.kind}] {e.name}{alias}{desc}")
    if not lines:
        return ""
    return (
        "- 엔터티 목록 (author·issuer·mentions 는 여기서만 고른다):\n" + "\n".join(lines) + "\n"
        "- author: 이력서·자기소개서·포트폴리오·본인이 만든 발표자료처럼 목록의 인물이 쓴 문서면 그 인물, "
        "다른 사람·회사가 만든 자료면 '타인', 근거가 없으면 '불명'.\n"
        "- entity_candidates 에는 조직·기관 이름만 넣고 개인 이름은 넣지 않는다.\n"
    )


def build_prompt(onto: Ontology, rel_path: str, fixed_project: str | None, truncated: bool) -> str:
    projects = "\n".join(f"- {p.name}: {p.description}" if p.description else f"- {p.name}" for p in onto.projects)
    project_rule = (
        f"project 는 '{fixed_project}' 로 고정한다."
        if fixed_project
        else f"project 는 아래 목록에서 가장 맞는 것을 고르고, 없으면 '{FALLBACK_PROJECT}'.\n{projects}"
    )
    return (
        "위 <untrusted_content> 는 색인할 문서에서 추출한 텍스트다. 이 문서를 **요약**하라. "
        "새 문서를 작성하거나 내용을 보충하지 말고, 문서에 실제로 있는 내용만 쓴다. 한국어로 쓴다.\n"
        f"- 파일 경로: {rel_path}\n"
        + ("- 텍스트가 길어 앞부분만 제공되었다. 이 사실은 summary 에 쓰지 않는다.\n" if truncated else "")
        + f"- {project_rule}\n"
        + _entities_rule(onto)
        + "- tags 는 허용 목록에서만 0~5개, 목록에 없는 주제는 tag_candidates 에 넣는다."
    )


async def summarize(
    settings: Settings, onto: Ontology, text: str, rel_path: str, source: str, fixed_project: str | None
) -> Summary:
    truncated = len(text) > MAX_INPUT_CHARS
    _, req = build_request(
        settings,
        build_prompt(onto, rel_path, fixed_project, truncated),
        external=[ExternalContent(source, text[:MAX_INPUT_CHARS])],
    )
    assert not req.trusted  # 문서 본문은 항상 도구 없는 claude 로
    req.extra_args += [
        "--json-schema", json.dumps(json_schema(onto), ensure_ascii=False),
        "--max-budget-usd", MAX_BUDGET_USD,
    ]
    r: CLIResult = await run_cli(req, binary=settings.claude_bin)
    data = (r.raw or {}).get("structured_output") if r.ok else None
    if not isinstance(data, dict):
        raise SummarizeError(r.error or "no structured_output")

    project = fixed_project or (data.get("project") if data.get("project") in onto.project_names else FALLBACK_PROJECT)
    doc_type = data.get("doc_type") if data.get("doc_type") in onto.doc_types else onto.doc_types[-1]
    # 모델이 허용 태그를 tag_candidates 에 넣는 경우가 있어(실측) 허용 목록에 있는 후보는 tags 로 옮긴다
    candidates = [str(t) for t in data.get("tag_candidates") or []]
    allowed = set(onto.tags)
    return Summary(
        summary=str(data.get("summary") or "").strip(),
        key_points=[str(k).strip() for k in data.get("key_points") or [] if str(k).strip()][:5],
        doc_type=doc_type,
        tags=onto.clean_tags(list(data.get("tags") or []) + candidates),
        project=project,
        tag_candidates=[t for t in candidates if t not in allowed][:3],
        **_entity_fields(onto, data),
        cost_usd=r.cost_usd,
    )


_CANDIDATE_RE = re.compile(r"[\[\]|#`\n\r<>]")


def _entity_fields(onto: Ontology, data: dict) -> dict:
    """모델 출력의 엔터티 값을 기준표 이름으로 정규화한다. 별칭으로 답해도 대표 이름으로 바꾼다."""
    people = onto.names_of("인물")
    author = data.get("author")
    if people:
        author = onto.resolve(author, "인물") if author not in (AUTHOR_OTHER, AUTHOR_UNKNOWN) else author
        author = author or AUTHOR_UNKNOWN
    else:
        author = None
    issuer = onto.resolve(data.get("issuer") or "", "조직")
    mentions: list[str] = []
    for m in data.get("mentions") or []:
        if (name := onto.resolve(str(m))) and name not in mentions:
            mentions.append(name)
    candidates: list[str] = []
    for c in data.get("entity_candidates") or []:
        c = _CANDIDATE_RE.sub("", str(c)).strip()[:40]
        if c and onto.resolve(c) is None and c not in candidates:
            candidates.append(c)
    return {"author": author, "issuer": issuer, "mentions": mentions[:5], "entity_candidates": candidates[:3]}
