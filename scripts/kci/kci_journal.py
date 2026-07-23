#!/usr/bin/env python3
"""KCI 저널 프로필 조회 CLI — 국내 투고처 평가용

저널명(부분 일치) 또는 제어번호로 등재구분·연도별 KCI IF·SJR·자기인용률·
변경 이력을 조회한다. citation/citationDetail Open API 랩핑 (2026-07-22).

사용:
    python3 scripts/kci/kci_journal.py 교육정보미디어연구
    python3 scripts/kci/kci_journal.py --id 000750
    python3 scripts/kci/kci_journal.py 교육공학연구 --json
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


def _fmt(value, width: int) -> str:
    return f"{value:>{width}}" if value is not None else "-".rjust(width)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="KCI 저널 프로필 조회 (등재구분·IF 추이·SJR·자기인용률)"
    )
    parser.add_argument("journal", nargs="?", help="저널명 (부분 일치)")
    parser.add_argument("--id", dest="journal_id", help="저널 제어번호 직접 지정 (예: 000750, SER000002778)")
    parser.add_argument("--year", type=int, help="인용지수 기준연도 (기본: 직전 연도)")
    parser.add_argument("--json", action="store_true", help="JSON 출력 (matches + detail)")
    args = parser.parse_args()

    if not args.journal and not args.journal_id:
        parser.error("저널명 또는 --id 중 하나가 필요합니다")

    _load_env()
    if not os.environ.get("KCI_API_KEY"):
        print("KCI_API_KEY가 없습니다 (.env 또는 환경변수에 설정)", file=sys.stderr)
        return 1

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/
    from kci.kci_core import KCIClient

    client = KCIClient()

    matches = []
    journal_id = args.journal_id
    if not journal_id:
        matches = client.search_journal_citations(journal=args.journal, year=args.year, limit=10)
        if not matches:
            print(f"KCI에서 저널을 찾지 못했습니다: {args.journal!r}", file=sys.stderr)
            return 1
        journal_id = matches[0]["journal_id"]

    detail = client.get_journal_detail(journal_id)
    if not detail:
        print(f"저널 상세 조회 실패: id={journal_id}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({"matches": matches, "detail": detail}, ensure_ascii=False, indent=2))
        return 0

    name_sub = detail["name_foreign"] or detail["name_kor_abbr"]
    print(f"■ {detail['name_kor']}" + (f" ({name_sub})" if name_sub else ""))
    print(f"  등재구분   : {detail['kci_registration'] or '-'}"
          + (f" / 해외: {detail['foreign_registration']}" if detail["foreign_registration"] else ""))
    print(f"  분야       : {detail['major']}")
    print(f"  ISSN       : {detail['issn'] or '-'}"
          + (f" / eISSN {detail['eissn']}" if detail["eissn"] else ""))
    print(f"  발행기관   : {detail['publisher']['name_kor']}"
          + (f" ({detail['publisher']['homepage']})" if detail["publisher"]["homepage"] else ""))
    if detail["frequency"]:
        print(f"  간기       : {detail['frequency']}")
    if detail["current_issue"]:
        print(f"  최근 발행  : {detail['current_issue']}")

    if len(matches) > 1:
        print(f"  ※ 유사 저널명 매치 {len(matches)}건 — 첫 매치 표시 (--id로 특정 가능):")
        for m in matches[1:4]:
            print(f"     - {m['journal_name']} (id={m['journal_id']}, {m['publisher_name']})")

    history = detail["citation_index_history"][:5]
    if history:
        print("  연도별 인용지수:")
        print("      연도 |    IF | IF(5y) |   SJR | 즉시성 | 자기인용률")
        for h in history:
            print(
                f"      {h['year']} | {_fmt(h['impact_factor'], 5)} |"
                f" {_fmt(h['impact_factor_5y'], 6)} | {_fmt(h['sjr'], 5)} |"
                f" {_fmt(h['immediacy_index'], 6)} | {_fmt(h['self_cited_rate'], 8)}%"
            )

    changes = detail["change_history"][:3]
    if changes:
        print("  최근 변경 이력:")
        for c in changes:
            print(f"      {c['date']:>10} | {c['note']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
