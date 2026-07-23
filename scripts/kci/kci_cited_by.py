#!/usr/bin/env python3
"""KCI 역인용(cited-by) 조회 CLI — 국내 피인용 추적용

문헌 제목으로 KCI 참고문헌 DB(referenceSearch)를 검색해 그 문헌을 참고문헌에
실은 국내 논문들을 찾는다. total = KCI 국내 피인용 수 proxy. NRF 성과보고·
연구 영향력 파악용 (2026-07-22 랩핑, 시맨틱스 실측 확정).

사용:
    python3 scripts/kci/kci_cited_by.py "메타버스를 활용한 고등학생 진로체험"
    python3 scripts/kci/kci_cited_by.py "제목" --author 임태형 --resolve 5
    python3 scripts/kci/kci_cited_by.py "제목" --json

주의: page 미지원(실측)이라 1회 조회 상한 100건 — total이 100을 넘으면
--ref-year(피인용 문헌 발행연도)로 분할 조회.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _load_env() -> None:
    """셸 export 없이도 동작하도록 .env에서 KCI_API_KEY 로드 (사용자 작업 디렉토리 기준)"""
    if os.environ.get("KCI_API_KEY"):
        return
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("KCI_API_KEY="):
            os.environ["KCI_API_KEY"] = line.split("=", 1)[1].strip()
            return


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KCI 역인용 조회 — 이 문헌을 참고문헌에 실은 국내 논문 찾기"
    )
    parser.add_argument("title", help="피인용 문헌 제목 (부분 일치)")
    parser.add_argument("--author", help="피인용 문헌 저자 (검색 정밀화)")
    parser.add_argument("--ref-year", type=int, dest="ref_year",
                        help="피인용 문헌의 발행연도 필터 (동명 문헌 구분·100건 초과 분할용)")
    parser.add_argument("--limit", type=int, default=100,
                        help="조회 건수 (기본 100 = page 미지원 환경의 최대 커버리지)")
    parser.add_argument("--resolve", type=int, default=0, metavar="N",
                        help="인용 논문 상위 N편을 articleDetail로 해석 (제목/저널/연도 — 건당 1호출)")
    parser.add_argument("--json", action="store_true", help="JSON 출력")
    args = parser.parse_args()

    _load_env()
    if not os.environ.get("KCI_API_KEY"):
        print("KCI_API_KEY가 없습니다 (.env 또는 환경변수에 설정)", file=sys.stderr)
        return 1

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
    from kci.kci_core import KCIClient

    client = KCIClient()
    result = client.search_references(
        title=args.title,
        author=args.author,
        ref_year=args.ref_year,
        limit=args.limit,
    )

    total = result["total"]
    references = result["references"]
    unique_ids = sorted({r["article_id"] for r in references if r["article_id"]})

    resolved = []
    for article_id in unique_ids[: max(0, args.resolve)]:
        md = client.get_by_kci_id(article_id)
        if md:
            resolved.append({
                "article_id": article_id,
                "title": md.title,
                "journal": md.journal,
                "year": md.year,
                "url": md.url,
            })
    resolved.sort(key=lambda x: x["year"] or 0, reverse=True)

    if args.json:
        print(json.dumps(
            {"query": args.title, "total": total,
             "unique_citing_articles": len(unique_ids),
             "references": references, "resolved_citing": resolved},
            ensure_ascii=False, indent=2,
        ))
        return 0

    print(f"■ KCI 역인용 조회: {args.title!r}"
          + (f" (저자: {args.author})" if args.author else ""))
    if total == 0:
        print("  국내 피인용 기록 없음 (KCI 참고문헌 DB 기준)")
        return 0

    print(f"  국내 피인용(참고문헌 등재): 총 {total}건 / 조회 {len(references)}건 / 고유 인용 논문 {len(unique_ids)}편")
    if total > len(references):
        print(f"  ⚠ page 미지원으로 1회 최대 100건 — 잔여 {total - len(references)}건은 --ref-year 분할로 조회")

    if resolved:
        print(f"  인용 논문 (상위 {len(resolved)}편 해석, 연도 내림차순):")
        for r in resolved:
            print(f"    [{r['year'] or '----'}] {r['title'][:52]} | {r['journal']} ({r['article_id']})")
    elif unique_ids:
        print(f"  인용 논문 KCI ID (미해석 — --resolve N으로 제목/저널 조회): {', '.join(unique_ids[:8])}"
              + (" …" if len(unique_ids) > 8 else ""))

    if references:
        print("  참고문헌 원형 샘플:")
        for r in references[:3]:
            print(f"    - {r['reference'][:88]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
