from pathlib import Path

import frontmatter

from indexer import build_index, entities
from indexer.ontology import Ontology

ONTO = """
projects:
  - {name: HANBIT, paths: [Documents/HANBIT], org: 한빛상사}
  - {name: Rocket, paths: [], parent: HANBIT}
  - {name: Loop, paths: [], parent: Loop}
  - {name: Typo, paths: [], org: 없는회사, parent: 없는프로젝트}
doc_types: [발표자료, 보고서, 기타]
tags: []
entities:
  조직:
    - {name: 한빛상사, aliases: [HANBIT]}
    - {name: 누리대, aliases: [누리대학교]}
  인물:
    - name: 나
      aliases: [홍길동]
      소속: [{org: 한빛상사, from: 2024-07, to: 2026-06}, {org: 없는회사}]
"""


def load(tmp_path: Path) -> Ontology:
    p = tmp_path / "onto.yaml"
    p.write_text(ONTO, encoding="utf-8")
    return Ontology.load(p)


def doc(vault: Path, rel: str, project: str, doc_type: str, summary: str, points: str = "") -> None:
    p = vault / "_Extracted" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = f"# x\n\n## AI 핵심 요약\n{points}\n\n---\n## 원본 추출 텍스트\n누리대학교 본문에만"
    p.write_text(frontmatter.dumps(frontmatter.Post(body, project=project, doc_type=doc_type, summary=summary,
                                                    source_file=f"/d/{Path(rel).stem}")))


def test_hierarchy_inheritance_and_bad_refs(tmp_path, caplog):
    o = load(tmp_path)
    assert o.ancestors("Rocket") == ["HANBIT"] and o.org_of("Rocket") == "한빛상사"
    assert o.project("Loop").parent is None  # 자기 자신은 상위가 될 수 없다
    typo = o.project("Typo")
    assert typo.org is None and typo.parent is None and "없는회사" in caplog.text
    (me,) = [e for e in o.entities if e.kind == "인물"]
    assert [a.org for a in me.affiliations] == ["한빛상사"] and me.affiliations[0].period() == "2024-07~2026-06"


def test_entity_notes_and_index(tmp_path):
    o, vault = load(tmp_path), tmp_path / "vault"
    doc(vault, "L/HANBIT/발표.pptx.md", "HANBIT", "발표자료", "사내 발표")
    doc(vault, "L/Rocket/계획.pdf.md", "Rocket", "보고서", "창업 검증")
    doc(vault, "L/채용/이력서.pdf.md", "채용", "기타", "홍길동 이력서", "- HANBIT 재직")
    doc(vault, "L/x/web.pdf.md", "기타", "기타", "WHANBITS 라는 단어")  # 영문 별칭은 단어 경계로만
    stale = vault / "_Entities/조직/옛회사.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(frontmatter.dumps(frontmatter.Post("x", generated=True)))
    mine = vault / "_Entities/조직/내메모.md"
    mine.write_text("사용자 노트")

    assert entities.write(vault, o) == 7  # 프로젝트 4(기타 제외) + 조직 2 + 인물 1
    assert not stale.exists() and mine.exists()

    org = (vault / "_Entities/조직/한빛상사.md").read_text()
    assert "[[_Entities/프로젝트/HANBIT|HANBIT]], [[_Entities/프로젝트/Rocket|Rocket]]" in org
    assert "[[_Entities/인물/나|나]] (2024-07~2026-06)" in org
    assert "## 수행 프로젝트의 문서 (2)" in org and "### 발표자료 (1)" in org
    assert "## 다른 문서에서 언급 (1)" in org and "이력서.pdf" in org and "web.pdf" not in org
    assert "aliases:\n- HANBIT" in org
    assert "## 다른 문서에서 언급 (0)" in (vault / "_Entities/조직/누리대.md").read_text()  # 원문 본문은 보지 않는다
    rocket = (vault / "_Entities/프로젝트/Rocket.md").read_text()
    assert "상위 프로젝트: [[_Entities/프로젝트/HANBIT|HANBIT]]" in rocket and "(상위 프로젝트에서)" in rocket
    assert "## 언급된 문서 (1)" in (vault / "_Entities/인물/나.md").read_text()

    (vault / "_Indexes").mkdir()
    (vault / "_Indexes/ontology_schema.yaml").write_text(ONTO, encoding="utf-8")
    build_index.write(vault)
    index = (vault / "_Indexes/master_index.md").read_text()
    assert "- [[_Entities/프로젝트/HANBIT|HANBIT]] — 한빛상사\n  - [[_Entities/프로젝트/Rocket|Rocket]]" in index
    assert "### Rocket (상위: HANBIT)" in index and "내메모" not in index  # _Entities 는 노트 목록에서 뺀다


def test_entity_fields_normalized_to_registry(tmp_path):
    from indexer import summarize as summ

    o = load(tmp_path)
    schema = summ.json_schema(o)["properties"]
    assert schema["author"]["enum"] == ["나", "타인", "불명"] and schema["issuer"]["enum"] == ["", "한빛상사", "누리대"]
    assert "[조직] 한빛상사 (다른 이름: HANBIT)" in summ.build_prompt(o, "a.pdf", None, False)
    f = summ._entity_fields(o, {"author": "홍길동", "issuer": "누리대학교", "mentions": ["hanbit", "한빛상사", "가짜"],
                                "entity_candidates": ["[[국세청]]", "HANBIT", "  "]})
    assert f == {"author": "나", "issuer": "누리대", "mentions": ["한빛상사"], "entity_candidates": ["국세청"]}
    assert summ._entity_fields(o, {"author": "누군가"})["author"] == "불명"
    empty = Ontology((), ("기타",), ())
    assert summ._entity_fields(empty, {"author": "나"})["author"] is None and "author" not in summ.json_schema(empty)["properties"]


def test_authored_issued_and_candidates(tmp_path):
    o, vault = load(tmp_path), tmp_path / "vault"
    doc(vault, "L/a.pdf.md", "기타", "증빙", "성적증명서")
    p = vault / "_Extracted/L/a.pdf.md"
    post = frontmatter.load(p)
    post.metadata.update(author="타인", issuer="누리대", mentions=["나"], entity_candidates=["국세청", "한빛상사"])
    p.write_text(frontmatter.dumps(post))
    doc(vault, "L/b.pptx.md", "기타", "발표자료", "발표")
    p = vault / "_Extracted/L/b.pptx.md"
    post = frontmatter.load(p)
    post.metadata.update(author="나", entity_candidates=["국세청"])
    p.write_text(frontmatter.dumps(post))

    entities.write(vault, o)
    uni = (vault / "_Entities/조직/누리대.md").read_text()
    assert "## 발급·발행한 문서 (1)" in uni and "a.pdf]] (증빙)" in uni and "## 다른 문서에서 언급 (0)" in uni
    me = (vault / "_Entities/인물/나.md").read_text()
    assert "## 작성한 문서 (1)" in me and "### 발표자료 (1)" in me and "## 언급된 문서 (1)" in me
    cand = (vault / entities.CANDIDATES).read_text()
    assert "**국세청** (2)" in cand and "한빛상사" not in cand.split("---")[-1]  # 이미 기준표에 있는 이름은 후보에서 뺀다

    (vault / "_Indexes").mkdir()
    (vault / "_Indexes/ontology_schema.yaml").write_text(ONTO, encoding="utf-8")
    build_index.write(vault)
    assert "(발표자료 · 작성: 나)" in (vault / "_Indexes/master_index.md").read_text()
