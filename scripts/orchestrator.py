#!/usr/bin/env python3
"""
Literature Discovery Orchestrator.

문헌탐색 에이전트 정의(agents/*.md)가 호출하는 통합 CLI.
정형 API 호출 + networkx 그래프 계산은 Python이 결정론적으로 처리하고,
LLM(설정 시)은 결과 JSON을 받아 자연어 종합만 담당한다.

표준 호출 (예시):

    python3 scripts/orchestrator.py related-papers \\
        --keywords "virtual reality learning" --limit 10 \\
        --output-format json --include-packet

    python3 scripts/orchestrator.py citation-network \\
        --doi "10.1234/example" --depth 2 --output-format json

    python3 scripts/orchestrator.py research-trends \\
        "transformer language models" --years 5 --output-format json

출력:
    --output-format json (기본): 구조화 결과를 stdout에 JSON으로
    --output-format markdown: 기존 generate_report() 결과 파일 생성 + 경로 출력
    --output-format both: json stdout + markdown 파일 생성
"""

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

# scripts/literature_discovery 모듈 path 확보 (__main__ 컨텍스트 호환)
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from config import Config  # noqa: E402
from related_paper_finder import RelatedPaperFinder, FinderConfig  # noqa: E402
from citation_network_explorer import (  # noqa: E402
    CitationNetworkExplorer, NetworkConfig,
)
from research_trend_analyzer import ResearchTrendAnalyzer, TrendConfig  # noqa: E402
from intelligence.synthesize import synthesize  # noqa: E402
from intelligence.coverage_manifest import build_manifest, DEFAULT_EXCLUDED  # noqa: E402


def _to_jsonable(obj: Any) -> Any:
    """dataclass/Enum/Path 등을 JSON 직렬화 가능한 형태로 재귀 변환."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Enum):
        return obj.value if hasattr(obj, "value") else str(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x) for x in obj]
    return str(obj)


def _paper_summary(paper) -> dict:
    """Paper dataclass → orchestrator 표준 출력 dict."""
    return {
        "paper_id": paper.paper_id,
        "title": paper.title,
        "doi": paper.doi,
        "authors": paper.author_names,
        "year": paper.year,
        "venue": paper.venue,
        "citation_count": paper.citation_count,
        "abstract": (paper.abstract or "")[:500],
        "url": paper.url,
        "relevance_score": round(paper.relevance_score, 3),
        "relevance_reason": paper.relevance_reason,
        "relevance_level": (
            paper.relevance_level.value
            if hasattr(paper.relevance_level, "value")
            else str(paper.relevance_level)
        ),
        "pagerank": round(paper.pagerank, 4) if paper.pagerank else 0.0,
        "betweenness": round(paper.betweenness, 4) if paper.betweenness else 0.0,
        "in_degree": paper.in_degree,
        "out_degree": paper.out_degree,
        "cluster_id": paper.cluster_id,
        "quality_grade": paper.quality_grade,
        "source_db": paper.source_db,
    }


# ──────────────────────────────────────────────────────────
# related-papers
# ──────────────────────────────────────────────────────────

def run_related_papers(args: argparse.Namespace) -> dict:
    config = Config.load()
    finder = RelatedPaperFinder(config)

    year_start = year_end = None
    if args.year_range:
        try:
            ys, ye = args.year_range.split("-")
            year_start, year_end = int(ys), int(ye)
        except ValueError:
            print(
                f"[WARN] --year-range 형식 오류 '{args.year_range}' (예: 2020-2024) — 무시",
                file=sys.stderr,
            )

    fcfg = FinderConfig(
        limit=args.limit,
        year_start=year_start,
        year_end=year_end,
        min_citations=args.min_citations,
        include_arxiv=not args.no_arxiv,
        include_eric=not args.no_eric,
        include_kci=not args.no_kci,
    )
    kci_active = (not args.no_kci) and finder.kci_client.available

    if args.keywords:
        seed, hr, mr = finder.find_by_keywords(args.keywords, fcfg)
        search_query = args.keywords
    elif args.doi:
        seed, hr, mr = finder.find_by_doi(args.doi, fcfg)
        search_query = args.doi
    else:
        seed, hr, mr = finder.find_by_paper_id(args.paper_id, fcfg)
        search_query = args.paper_id

    output = {
        "type": "related_papers",
        "search_query": search_query,
        "search_metadata": {
            "search_date": datetime.now().isoformat(),
            "sources": [
                s for s, on in (
                    ("semantic_scholar", True),
                    ("arxiv", not args.no_arxiv),
                    ("eric", not args.no_eric),
                    ("kci", kci_active),
                ) if on
            ],
            "limit": args.limit,
            "year_range": args.year_range,
            "min_citations": args.min_citations,
        },
        "seed_paper": _paper_summary(seed) if seed else None,
        "highly_relevant": [_paper_summary(p) for p in hr],
        "moderately_relevant": [_paper_summary(p) for p in mr],
    }

    # 종합 (on-topic 상위 논문 통독) — LLM 실패 시 빈 결과
    on_topic = list(hr) + list(mr)
    syn = synthesize(search_query, on_topic)
    output["synthesis"] = {
        "themes": syn.themes, "consensus": syn.consensus, "conflicts": syn.conflicts,
        "gaps": syn.gaps, "limitations": syn.limitations,
        "cited_ids": syn.cited_ids, "flagged_uncited": syn.flagged_uncited, "mode": syn.mode,
    }

    # 커버리지 매니페스트 (결정론 정직성 신호)
    counts = {}
    for p in on_topic:
        counts[p.source_db or "unknown"] = counts.get(p.source_db or "unknown", 0) + 1
    # KCI가 이번 검색에서 미가용이면(키 미설정/--no-kci) 제외 목록에 정직하게 노출
    excluded = list(DEFAULT_EXCLUDED)
    if not kci_active:
        excluded.append("KCI (키 미설정 또는 --no-kci)")
    output["coverage_manifest"] = build_manifest(
        search_query=search_query,
        sources_queried=output["search_metadata"]["sources"],
        counts_per_source=counts,
        year_range=args.year_range,
        total_retrieved=len(on_topic) + getattr(finder, "last_rerank_dropped", 0),
        returned=len(on_topic),
        rerank_mode=getattr(finder, "last_rerank_mode", "heuristic_fallback"),
        rerank_dropped=getattr(finder, "last_rerank_dropped", 0),
        influential_threshold=50,
        influential_count=sum(1 for p in on_topic if p.citation_count >= 50),
        excluded_sources=excluded,
    )

    # on-topic 결과가 희소하면(<3) 근접 후보를 명시적으로 노출 (정직성: relevant 아님, 각 reason 동반)
    if len(on_topic) < 3:
        output["near_matches"] = [_paper_summary(p) for p in getattr(finder, "last_near_matches", [])]

    # --include-packet: discovery packet YAML 생성 (후속 처리용 기계 판독 패킷)
    if args.include_packet:
        if hasattr(finder, "generate_discovery_packet"):
            packet_path = finder.generate_discovery_packet(
                seed_paper=seed,
                highly_relevant=hr,
                moderately_relevant=mr,
                search_query=search_query,
                synthesis=output.get("synthesis"),
            )
            output["discovery_packet_path"] = str(packet_path)
        else:
            output["discovery_packet_error"] = (
                "generate_discovery_packet() 미구현"
            )

    # --output-format markdown / both: 기존 generate_report() 호출
    if args.output_format in ("markdown", "both"):
        report_path = finder.generate_report(seed, hr, mr, search_query)
        output["markdown_report_path"] = str(report_path)

    return output


# ──────────────────────────────────────────────────────────
# citation-network
# ──────────────────────────────────────────────────────────

def run_citation_network(args: argparse.Namespace) -> dict:
    config = Config.load()
    explorer = CitationNetworkExplorer(config)

    ncfg = NetworkConfig(
        depth=args.depth,
        direction=args.direction,
        max_nodes=args.max_nodes,
    )
    seed_id = f"DOI:{args.doi}" if args.doi else args.paper_id
    graph = explorer.build_network(seed_id, ncfg)
    analysis = explorer.analyze_network()
    key_papers = explorer.get_key_papers(top_n=args.top_n)

    output = {
        "type": "citation_network",
        "seed_paper_id": seed_id,
        "network": {
            "nodes": graph.number_of_nodes() if graph else 0,
            "edges": graph.number_of_edges() if graph else 0,
            "statistics": _to_jsonable(analysis.get("statistics", {})),
            "clusters": _to_jsonable(analysis.get("clusters", [])),
        },
        "key_papers": [_paper_summary(p) for p in key_papers],
    }

    if args.output_format in ("markdown", "both"):
        # generate_report는 seed_paper(Paper)를 요구 — explorer.papers dict에서 직접 lookup
        # (build_network에서 graph.add_node에는 title/year/citations만 저장하므로 'paper' attr 없음)
        seed_paper = explorer.papers.get(seed_id) if explorer.papers else None
        if seed_paper:
            report_path = explorer.generate_report(seed_paper)
            output["markdown_report_path"] = str(report_path)
        else:
            output["markdown_report_error"] = (
                f"seed paper 정보 없음 (explorer.papers에 {seed_id} 키 미존재) "
                "— JSON 결과만 사용 가능"
            )

    return output


# ──────────────────────────────────────────────────────────
# research-trends
# ──────────────────────────────────────────────────────────

def run_research_trends(args: argparse.Namespace) -> dict:
    config = Config.load()
    analyzer = ResearchTrendAnalyzer(config)

    tcfg = TrendConfig(
        years=args.years,
        top_n=args.top_n,
        papers_per_year=args.papers_per_year,
    )
    result = analyzer.analyze_trend(args.topic, tcfg)

    # publication_trend / emerging_topics / top_authors 등은 모두
    # dict/list 구조이므로 _to_jsonable로 안전 변환
    output = {
        "type": "research_trends",
        "topic": result.get("topic"),
        "period": result.get("period"),
        "total_papers": result.get("total_papers"),
        "publication_trend": _to_jsonable(result.get("publication_trend", {})),
        "keyword_evolution": _to_jsonable(result.get("keyword_evolution", {})),
        "emerging_topics": _to_jsonable(result.get("emerging_topics", [])),
        "declining_topics": _to_jsonable(result.get("declining_topics", [])),
        "top_authors": _to_jsonable(result.get("top_authors", [])),
        "top_venues": _to_jsonable(result.get("top_venues", [])),
    }

    if args.output_format in ("markdown", "both"):
        report_path = analyzer.generate_report(result)
        output["markdown_report_path"] = str(report_path)

    return output


# ──────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Literature Discovery Orchestrator — 문헌탐색 에이전트가 "
            "호출하는 통합 CLI"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # related-papers
    rp = subparsers.add_parser("related-papers", help="키워드/DOI 기반 관련 논문 검색")
    rp_input = rp.add_mutually_exclusive_group(required=True)
    rp_input.add_argument("--keywords", help="검색 키워드")
    rp_input.add_argument("--doi", help="seed DOI")
    rp_input.add_argument("--paper-id", help="Semantic Scholar Paper ID")
    rp.add_argument("--limit", type=int, default=10)
    rp.add_argument("--year-range", help="YYYY-YYYY")
    rp.add_argument("--min-citations", type=int, default=0)
    rp.add_argument("--no-arxiv", action="store_true")
    rp.add_argument("--no-eric", action="store_true")
    rp.add_argument("--no-kci", action="store_true")
    rp.add_argument(
        "--include-packet",
        action="store_true",
        help="discovery_packet.yaml 생성 (후속 처리용 기계 판독 패킷)",
    )
    rp.add_argument(
        "--output-format",
        choices=["json", "markdown", "both"],
        default="json",
    )

    # citation-network
    cn = subparsers.add_parser("citation-network", help="인용 네트워크 분석")
    cn_input = cn.add_mutually_exclusive_group(required=True)
    cn_input.add_argument("--doi", help="seed DOI")
    cn_input.add_argument("--paper-id", help="Semantic Scholar Paper ID")
    cn.add_argument("--depth", type=int, default=2)
    cn.add_argument("--direction", choices=["both", "citing", "cited"], default="both")
    cn.add_argument("--max-nodes", type=int, default=200)
    cn.add_argument("--top-n", type=int, default=10)
    cn.add_argument(
        "--output-format",
        choices=["json", "markdown", "both"],
        default="json",
    )

    # research-trends
    rt = subparsers.add_parser("research-trends", help="연구 트렌드 분석")
    rt.add_argument("topic", help="연구 주제")
    rt.add_argument("--years", type=int, default=5)
    rt.add_argument("--top-n", type=int, default=10)
    rt.add_argument("--papers-per-year", type=int, default=100)
    rt.add_argument(
        "--output-format",
        choices=["json", "markdown", "both"],
        default="json",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "related-papers":
            result = run_related_papers(args)
        elif args.command == "citation-network":
            result = run_citation_network(args)
        elif args.command == "research-trends":
            result = run_research_trends(args)
        else:
            print(f"[ERROR] unknown command: {args.command}", file=sys.stderr)
            return 2
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
        err = {
            "type": "error",
            "command": args.command,
            "error_class": exc.__class__.__name__,
            "error_message": str(exc),
        }
        print(json.dumps(err, indent=2, ensure_ascii=False))
        return 1

    print(json.dumps(_to_jsonable(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
