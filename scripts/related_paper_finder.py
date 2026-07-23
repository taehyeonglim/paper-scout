#!/usr/bin/env python
"""
Related Paper Finder
관련 논문 탐색 에이전트

사용법:
    python related_paper_finder.py --keywords "LLM education" --limit 10
    python related_paper_finder.py --doi "10.1234/example"
    python related_paper_finder.py paper.md --year-range 2020-2024
"""

import argparse
import sys
import logging
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass

# 상위 디렉토리를 path에 추가
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from utils.api_clients import SemanticScholarClient, ArxivClient, ERICClient, KCIClient, OpenAlexClient
from utils.paper_models import Paper, RelevanceLevel
from utils.markdown_writer import MarkdownWriter
from intelligence.semantic_rerank import rerank

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class FinderConfig:
    """검색 설정"""
    limit: int = 10
    year_start: Optional[int] = None
    year_end: Optional[int] = None
    min_citations: int = 0
    include_arxiv: bool = True
    include_eric: bool = True          # ERIC 검색 포함 (교육학)
    include_kci: bool = True           # KCI 검색 포함 (국내 학술지, 키 있을 때만)
    include_openalex: bool = True      # OpenAlex 검색 포함 (usage-based, 키 없어도 동작)
    semantic_threshold: float = 0.8    # 높은 관련성 임계값
    moderate_threshold: float = 0.5    # 중간 관련성 임계값


class RelatedPaperFinder:
    """관련 논문 탐색 에이전트"""

    def __init__(self, config: Config):
        self.config = config
        self.ss_client = SemanticScholarClient(
            api_key=config.semantic_scholar_api_key
        )
        self.arxiv_client = ArxivClient()
        self.eric_client = ERICClient()
        self.kci_client = KCIClient(api_key=config.kci_api_key)
        self.openalex_client = OpenAlexClient(api_key=config.openalex_api_key)
        self.writer = MarkdownWriter(str(config.output_dir))
        self.last_rerank_mode = "heuristic_fallback"
        self.last_rerank_dropped = 0
        self.last_near_matches = []

    def find_by_keywords(
        self,
        keywords: str,
        finder_config: FinderConfig
    ) -> Tuple[Paper, List[Paper], List[Paper]]:
        """
        키워드 기반 관련 논문 검색

        Returns:
            (seed_paper, highly_relevant, moderately_relevant) 튜플
        """
        logger.info(f"Searching for papers with keywords: '{keywords}'")

        # 검색 수행
        year_range = None
        if finder_config.year_start and finder_config.year_end:
            year_range = (finder_config.year_start, finder_config.year_end)

        # Semantic Scholar 검색
        ss_papers = self.ss_client.search_papers(
            keywords,
            limit=finder_config.limit * 2,  # 여유있게 검색
            year_range=year_range
        )

        # arXiv 검색 (선택적)
        arxiv_papers = []
        if finder_config.include_arxiv:
            arxiv_papers = self.arxiv_client.search_papers(
                keywords,
                limit=finder_config.limit,
                year_range=year_range
            )

        # ERIC 검색 (교육학 - 선택적)
        eric_papers = []
        if finder_config.include_eric:
            eric_papers = self.eric_client.search_papers(
                keywords,
                limit=finder_config.limit,
                year_range=year_range
            )

        # KCI 검색 (국내 학술지 - 선택적, 인증키 있을 때만)
        kci_papers = []
        if finder_config.include_kci and self.kci_client.available:
            kci_papers = self.kci_client.search_papers(
                keywords,
                limit=finder_config.limit,
                year_range=year_range
            )

        # OpenAlex 검색 (선택적)
        openalex_papers = []
        if finder_config.include_openalex:
            openalex_papers = self.openalex_client.search_papers(
                keywords,
                limit=finder_config.limit,
                year_range=year_range
            )

        # 결과 통합 및 중복 제거
        all_papers = self._merge_and_deduplicate(
            ss_papers, arxiv_papers, eric_papers, kci_papers, openalex_papers
        )

        # 인용 수 필터링
        if finder_config.min_citations > 0:
            all_papers = [p for p in all_papers if p.citation_count >= finder_config.min_citations]

        # 의미 관련성 재채점 (LLM 설정 시, 실패 시 검색 순위 fallback)
        all_papers = self._apply_semantic_rerank(keywords, all_papers, finder_config)

        # 관련성 분류
        highly_relevant, moderately_relevant = self._classify_relevance(
            all_papers, finder_config
        )

        # 결과 수 제한
        highly_relevant = highly_relevant[:finder_config.limit]
        moderately_relevant = moderately_relevant[:finder_config.limit]

        # Seed paper (가상 - 키워드 검색이므로)
        seed_paper = Paper(
            paper_id="query",
            title=f"Search: {keywords}",
            authors=[],
        )

        return seed_paper, highly_relevant, moderately_relevant

    def find_by_doi(
        self,
        doi: str,
        finder_config: FinderConfig
    ) -> Tuple[Paper, List[Paper], List[Paper]]:
        """
        DOI 기반 관련 논문 검색

        Returns:
            (seed_paper, highly_relevant, moderately_relevant) 튜플
        """
        logger.info(f"Finding related papers for DOI: {doi}")

        # Seed paper 조회
        seed_paper = self.ss_client.get_paper(f"DOI:{doi}")
        if not seed_paper:
            logger.error(f"Paper not found: {doi}")
            raise ValueError(f"논문을 찾을 수 없습니다: {doi}")

        return self._find_related_to_paper(seed_paper, finder_config)

    def find_by_paper_id(
        self,
        paper_id: str,
        finder_config: FinderConfig
    ) -> Tuple[Paper, List[Paper], List[Paper]]:
        """
        Semantic Scholar Paper ID 기반 관련 논문 검색
        """
        logger.info(f"Finding related papers for ID: {paper_id}")

        seed_paper = self.ss_client.get_paper(paper_id)
        if not seed_paper:
            raise ValueError(f"논문을 찾을 수 없습니다: {paper_id}")

        return self._find_related_to_paper(seed_paper, finder_config)

    def _find_related_to_paper(
        self,
        seed_paper: Paper,
        finder_config: FinderConfig
    ) -> Tuple[Paper, List[Paper], List[Paper]]:
        """논문 기반 관련 논문 검색 (내부 메서드)"""

        all_papers = []

        # 1. 추천 논문 (시맨틱 유사도)
        recommendations = self.ss_client.get_recommendations(
            seed_paper.paper_id,
            limit=finder_config.limit
        )
        for paper in recommendations:
            paper.relevance_score = 0.9  # 추천은 높은 점수
            paper.relevance_reason = "semantic_similarity"
        all_papers.extend(recommendations)

        # 2. 인용 논문
        citing_papers = self.ss_client.get_paper_citations(
            seed_paper.paper_id,
            limit=finder_config.limit
        )
        for paper in citing_papers:
            paper.relevance_score = 0.7
            paper.relevance_reason = "cites_seed"
        all_papers.extend(citing_papers)

        # 3. 참조 논문
        referenced_papers = self.ss_client.get_paper_references(
            seed_paper.paper_id,
            limit=finder_config.limit
        )
        for paper in referenced_papers:
            paper.relevance_score = 0.65
            paper.relevance_reason = "cited_by_seed"
        all_papers.extend(referenced_papers)

        # 4. 키워드 검색 (제목 기반)
        if seed_paper.title:
            # 제목에서 주요 단어 추출 (간단한 방법)
            keywords = self._extract_keywords_from_title(seed_paper.title)
            if keywords:
                keyword_papers = self.ss_client.search_papers(
                    keywords,
                    limit=finder_config.limit
                )
                for paper in keyword_papers:
                    paper.relevance_score = 0.6
                    paper.relevance_reason = "keyword_match"
                all_papers.extend(keyword_papers)

        # 중복 제거 (같은 논문이 여러 소스에서 발견될 수 있음)
        unique_papers = self._deduplicate_papers(all_papers, seed_paper.paper_id)

        # 인용 수 필터링
        if finder_config.min_citations > 0:
            unique_papers = [p for p in unique_papers if p.citation_count >= finder_config.min_citations]

        # 연도 필터링
        if finder_config.year_start or finder_config.year_end:
            unique_papers = self._filter_by_year(
                unique_papers,
                finder_config.year_start,
                finder_config.year_end
            )

        # 의미 관련성 재채점 (seed 논문 제목 기준; LLM 실패 시 source-type 점수 fallback)
        unique_papers = self._apply_semantic_rerank(
            seed_paper.title or "", unique_papers, finder_config
        )

        # 관련성 분류
        highly_relevant, moderately_relevant = self._classify_relevance(
            unique_papers, finder_config
        )

        # 결과 수 제한
        highly_relevant = highly_relevant[:finder_config.limit]
        moderately_relevant = moderately_relevant[:finder_config.limit]

        return seed_paper, highly_relevant, moderately_relevant

    def _merge_and_deduplicate(
        self,
        ss_papers: List[Paper],
        arxiv_papers: List[Paper],
        eric_papers: List[Paper] = None,
        kci_papers: List[Paper] = None,
        openalex_papers: List[Paper] = None
    ) -> List[Paper]:
        """여러 소스의 결과 통합 및 중복 제거"""
        seen_titles = set()
        seen_dois = set()
        unique_papers = []

        all_sources = (
            ss_papers + arxiv_papers + (eric_papers or []) + (kci_papers or [])
            + (openalex_papers or [])
        )
        for paper in all_sources:
            # DOI로 중복 체크
            if paper.doi and paper.doi in seen_dois:
                continue
            if paper.doi:
                seen_dois.add(paper.doi)

            # 제목으로 중복 체크 (정규화)
            normalized_title = paper.title.lower().strip() if paper.title else ""
            if normalized_title and normalized_title in seen_titles:
                continue
            if normalized_title:
                seen_titles.add(normalized_title)

            unique_papers.append(paper)

        return unique_papers

    def _deduplicate_papers(
        self,
        papers: List[Paper],
        exclude_id: str
    ) -> List[Paper]:
        """중복 제거 (seed paper 제외)"""
        seen_ids = {exclude_id}
        seen_titles = set()
        unique = []

        for paper in papers:
            if paper.paper_id in seen_ids:
                continue
            seen_ids.add(paper.paper_id)

            normalized_title = paper.title.lower().strip() if paper.title else ""
            if normalized_title in seen_titles:
                continue
            if normalized_title:
                seen_titles.add(normalized_title)

            unique.append(paper)

        return unique

    def _apply_semantic_rerank(self, query_intent, papers, finder_config):
        """검색 결과를 의미 관련성으로 재채점한다. fallback 시 검색 순위 점수 보존."""
        # fallback 대비 기본 점수 부여 — 소스 내 순위 기반 (병합 순서 무관).
        # 전역 인덱스 기반이면 _merge_and_deduplicate 마지막 병합 소스가
        # 구조적으로 관련성 임계 아래로 밀려 전멸한다 (2026-07-24 무키 스모크
        # 실측: OpenAlex 4편 전부 인덱스 10~13 → 0.5 이하 탈락). 각 소스의
        # 자체 검색 순위(rank)가 결정론 관련성 신호이므로 그것만 사용한다.
        source_rank: dict = {}
        for paper in papers:
            rank = source_rank.get(paper.source_db, 0)
            source_rank[paper.source_db] = rank + 1
            if paper.relevance_score == 0.0:
                paper.relevance_score = max(0.3, 1.0 - (rank * 0.05))
                paper.relevance_reason = "keyword_match"

        result = rerank(query_intent, papers, top_k=len(papers))
        self.last_rerank_mode = result.mode
        self.last_rerank_dropped = result.dropped
        self.last_near_matches = result.near_matches
        return result.papers

    def _classify_relevance(
        self,
        papers: List[Paper],
        config: FinderConfig
    ) -> Tuple[List[Paper], List[Paper]]:
        """관련성 점수 기준 분류"""
        highly_relevant = []
        moderately_relevant = []

        for paper in papers:
            if paper.relevance_score >= config.semantic_threshold:
                paper.relevance_level = RelevanceLevel.HIGH
                highly_relevant.append(paper)
            elif paper.relevance_score >= config.moderate_threshold:
                paper.relevance_level = RelevanceLevel.MODERATE
                moderately_relevant.append(paper)

        # 관련성 점수 내림차순 정렬
        highly_relevant.sort(key=lambda p: p.relevance_score, reverse=True)
        moderately_relevant.sort(key=lambda p: p.relevance_score, reverse=True)

        return highly_relevant, moderately_relevant

    def _filter_by_year(
        self,
        papers: List[Paper],
        year_start: Optional[int],
        year_end: Optional[int]
    ) -> List[Paper]:
        """연도 필터링"""
        filtered = []
        for paper in papers:
            if not paper.year:
                continue
            if year_start and paper.year < year_start:
                continue
            if year_end and paper.year > year_end:
                continue
            filtered.append(paper)
        return filtered

    def _extract_keywords_from_title(self, title: str) -> str:
        """제목에서 키워드 추출 (간단한 방법)"""
        # 불용어
        stopwords = {
            "a", "an", "the", "of", "in", "on", "at", "to", "for", "with",
            "and", "or", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would",
            "can", "could", "may", "might", "must", "shall", "should",
            "this", "that", "these", "those", "it", "its",
            "using", "based", "via", "through", "from", "by"
        }

        words = title.lower().split()
        keywords = [w for w in words if w.isalnum() and len(w) > 2 and w not in stopwords]

        # 상위 5개 키워드
        return " ".join(keywords[:5])

    def generate_report(
        self,
        seed_paper: Paper,
        highly_relevant: List[Paper],
        moderately_relevant: List[Paper],
        search_query: Optional[str] = None
    ) -> Path:
        """Markdown 리포트 생성"""
        citation_stats = {
            "papers_citing_this": seed_paper.citation_count,
            "papers_this_cites": seed_paper.reference_count,
            "key_citing_papers": len([p for p in highly_relevant if p.relevance_reason == "cites_seed"])
        }

        return self.writer.write_related_papers_report(
            query_paper=seed_paper,
            highly_relevant=highly_relevant,
            moderately_relevant=moderately_relevant,
            citation_stats=citation_stats,
            search_query=search_query
        )

    def generate_discovery_packet(
        self,
        seed_paper: Paper,
        highly_relevant: List[Paper],
        moderately_relevant: List[Paper],
        search_query: Optional[str] = None,
        output_path: Optional[Path] = None,
        synthesis: Optional[dict] = None,
    ) -> Path:
        """후속 처리용 기계 판독 discovery packet 생성.

        결정론 부분 (search_metadata, core_papers, doi_registry)은 Python이 채우고,
        의미적 부분 (conflicting_views, limitations, follow_up_questions)은 빈 배열 +
        consumer/LLM 보강 hook으로 남김.

        Args:
            output_path: 명시 시 해당 경로에 저장, 미지정 시
                         {config.output_dir}/discovery-packets/{timestamp}.yaml

        Returns:
            저장된 packet 파일 경로
        """
        import yaml
        from datetime import datetime

        if output_path is None:
            base = Path(self.config.output_dir) / "discovery-packets"
            base.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_query = (search_query or "query").replace("/", "_")[:40]
            output_path = base / f"{ts}_{safe_query}.yaml"
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        all_papers = list(highly_relevant) + list(moderately_relevant)
        total_found = len(all_papers)
        # deduplicate by paper_id (seed search 결과는 이미 dedup된 상태지만 안전)
        seen_ids: set[str] = set()
        deduped: List[Paper] = []
        for p in all_papers:
            if p.paper_id and p.paper_id not in seen_ids:
                seen_ids.add(p.paper_id)
                deduped.append(p)

        core_papers = [
            {
                "doi": p.doi,
                "title": p.title,
                "authors": p.author_names,
                "year": p.year,
                "venue": p.venue,
                "relevance_score": round(float(p.relevance_score), 3),
                "citation_count": int(p.citation_count or 0),
                "relevance_reason": p.relevance_reason,
                "source_db": p.source_db,
                "quality_grade": p.quality_grade,
            }
            for p in sorted(deduped, key=lambda x: -float(x.relevance_score))[:max(10, len(highly_relevant))]
        ]

        # DOI Registry (검증된 DOI 목록 — 필수 필드)
        doi_registry = []
        for p in deduped:
            if not p.doi:
                continue
            cite_key = self._build_citation_key(p)
            doi_registry.append({
                "citation_key": cite_key,
                "doi": p.doi,
                "title": p.title,
                "journal": p.venue,
                "year": p.year,
                # Semantic Scholar 응답 자체는 DOI 검증된 상태로 봄
                "verified": True,
                "verified_date": datetime.now().isoformat(),
            })

        packet = {
            "search_metadata": {
                "search_query": search_query or "",
                "search_date": datetime.now().isoformat(),
                "source_agents": ["related-paper-finder"],
                "total_found": total_found,
                "deduplicated_count": len(deduped),
            },
            "core_papers": core_papers,
            # 의미적 필드 — synthesis 주입 (없으면 빈 배열)
            "conflicting_views": (synthesis or {}).get("conflicts", []),
            "limitations": (synthesis or {}).get("limitations", []),
            "follow_up_questions": (synthesis or {}).get("gaps", []),
            "doi_registry": doi_registry,
            "_schema_note": (
                "conflicting_views/limitations/follow_up_questions는 synthesis(orchestrator) 주입 "
                "또는 빈 배열(synthesis=None) — consumer(LLM 종합 또는 사용자)가 보강 가능."
            ),
        }

        with open(output_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(packet, f, allow_unicode=True, sort_keys=False)
        logger.info(f"discovery_packet 생성: {output_path}")
        return output_path

    @staticmethod
    def _build_citation_key(paper: Paper) -> str:
        """citation_key 생성: first_author_lastname + year (간이 휴리스틱)."""
        first = paper.first_author or ""
        # "Lim, Taehyeong" → "Lim", "Taehyeong Lim" → "Lim"
        last = first.split(",")[0].strip().split()[-1] if first else "Unknown"
        year = str(paper.year) if paper.year else "ND"
        return f"{last}{year}"


def parse_args():
    """CLI 인자 파싱"""
    parser = argparse.ArgumentParser(
        description="Related Paper Finder - 관련 논문 탐색",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 키워드로 검색
  python related_paper_finder.py --keywords "LLM keyword extraction"

  # DOI로 검색
  python related_paper_finder.py --doi "10.1234/example"

  # Semantic Scholar Paper ID로 검색
  python related_paper_finder.py --paper-id "649def34f8be52c8b66281af98ae884c09aef38b"

  # 연도 필터링
  python related_paper_finder.py --keywords "deep learning" --year-range 2020-2024 --limit 20
        """
    )

    # 입력 옵션 (상호 배타적)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--keywords", "-k",
        help="검색 키워드"
    )
    input_group.add_argument(
        "--doi", "-d",
        help="논문 DOI"
    )
    input_group.add_argument(
        "--paper-id", "-p",
        help="Semantic Scholar Paper ID"
    )

    # 필터 옵션
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=10,
        help="결과 수 제한 (기본: 10)"
    )
    parser.add_argument(
        "--year-range", "-y",
        help="연도 범위 (예: 2020-2024)"
    )
    parser.add_argument(
        "--min-citations",
        type=int,
        default=0,
        help="최소 인용 수 필터"
    )
    parser.add_argument(
        "--no-arxiv",
        action="store_true",
        help="arXiv 검색 제외"
    )
    parser.add_argument(
        "--no-eric",
        action="store_true",
        help="ERIC 검색 제외 (교육학)"
    )

    # 출력 옵션
    parser.add_argument(
        "--output", "-o",
        help="출력 파일 경로"
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="리포트 파일 생성 안 함"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="상세 로그 출력"
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # 로그 레벨 설정
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 설정 로드
    config = Config.load()

    # 검색 설정
    year_start, year_end = None, None
    if args.year_range:
        parts = args.year_range.split("-")
        year_start, year_end = int(parts[0]), int(parts[1])

    finder_config = FinderConfig(
        limit=args.limit,
        year_start=year_start,
        year_end=year_end,
        min_citations=args.min_citations,
        include_arxiv=not args.no_arxiv,
        include_eric=not args.no_eric
    )

    # 에이전트 실행
    finder = RelatedPaperFinder(config)

    try:
        if args.keywords:
            seed_paper, highly_relevant, moderately_relevant = finder.find_by_keywords(
                args.keywords, finder_config
            )
            search_query = args.keywords
        elif args.doi:
            seed_paper, highly_relevant, moderately_relevant = finder.find_by_doi(
                args.doi, finder_config
            )
            search_query = args.doi
        else:  # paper_id
            seed_paper, highly_relevant, moderately_relevant = finder.find_by_paper_id(
                args.paper_id, finder_config
            )
            search_query = args.paper_id

    except (OSError, ValueError, RuntimeError) as e:
        logger.error(f"검색 실패: {e}")
        sys.exit(1)

    # 결과 출력
    print(f"\n{'='*60}")
    print(f"검색 완료")
    print(f"{'='*60}")
    print(f"  Highly Relevant:     {len(highly_relevant)}편")
    print(f"  Moderately Relevant: {len(moderately_relevant)}편")
    print(f"{'='*60}\n")

    # 상위 결과 미리보기
    if highly_relevant:
        print("Top Highly Relevant Papers:")
        for i, paper in enumerate(highly_relevant[:5], 1):
            year = paper.year or "N/A"
            cites = paper.citation_count
            print(f"  {i}. [{year}] {paper.title[:60]}... (Citations: {cites})")
        print()

    # 리포트 생성
    if not args.no_report:
        report_path = finder.generate_report(
            seed_paper,
            highly_relevant,
            moderately_relevant,
            search_query=search_query
        )
        print(f"리포트 저장: {report_path}")


if __name__ == "__main__":
    main()
