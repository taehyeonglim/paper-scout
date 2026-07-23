#!/usr/bin/env python
"""
Research Trend Analyzer
연구 트렌드 분석 에이전트

사용법:
    python research_trend_analyzer.py "deep learning NLP" --years 5
    python research_trend_analyzer.py "LLM education" --top-n 20
"""

import argparse
import sys
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from collections import Counter, defaultdict
from datetime import datetime

# 상위 디렉토리를 path에 추가
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from utils.api_clients import SemanticScholarClient, OpenAlexClient
from utils.paper_models import Paper, TrendData
from utils.markdown_writer import MarkdownWriter

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class TrendConfig:
    """트렌드 분석 설정"""
    years: int = 5                      # 분석 기간 (년)
    top_n: int = 10                     # 상위 N개 결과
    min_papers: int = 3                 # 최소 논문 수 (트렌드 계산용)
    papers_per_year: int = 100          # 연도당 검색할 논문 수


class ResearchTrendAnalyzer:
    """연구 트렌드 분석 에이전트"""

    # 불용어
    STOPWORDS = {
        "a", "an", "the", "of", "in", "on", "at", "to", "for", "with",
        "and", "or", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would",
        "can", "could", "may", "might", "must", "shall", "should",
        "this", "that", "these", "those", "it", "its", "as", "by",
        "using", "based", "via", "through", "from", "into", "about",
        "new", "novel", "proposed", "approach", "method", "study",
        "analysis", "research", "paper", "model", "system", "data"
    }

    def __init__(self, config: Config):
        self.config = config
        self.ss_client = SemanticScholarClient(
            api_key=config.semantic_scholar_api_key
        )
        self.openalex_client = OpenAlexClient(api_key=config.openalex_api_key)
        self.writer = MarkdownWriter(str(config.output_dir))

    def analyze_trend(
        self,
        topic: str,
        trend_config: TrendConfig
    ) -> Dict[str, Any]:
        """
        연구 트렌드 분석 메인 로직

        Returns:
            분석 결과 딕셔너리
        """
        current_year = datetime.now().year
        start_year = current_year - trend_config.years

        logger.info(f"Analyzing trend for '{topic}' ({start_year}-{current_year})")

        # 1. 연도별 데이터 수집
        papers_by_year = self._collect_papers_by_year(
            topic, start_year, current_year, trend_config.papers_per_year
        )

        # 2. 출판 트렌드 분석
        publication_trend = self._analyze_publication_trend(papers_by_year)

        # 3. 키워드 진화 분석
        keyword_evolution = self._analyze_keyword_evolution(papers_by_year)

        # 4. Emerging/Declining 토픽 식별
        emerging_topics = self._identify_emerging_topics(
            keyword_evolution, trend_config
        )
        declining_topics = self._identify_declining_topics(
            keyword_evolution, trend_config
        )

        # 5. 핵심 저자 분석
        top_authors = self._analyze_top_authors(papers_by_year, trend_config)

        # 6. 주요 학회/저널 분석
        top_venues = self._analyze_top_venues(papers_by_year, trend_config)

        return {
            "topic": topic,
            "period": f"{start_year}-{current_year}",
            "total_papers": sum(len(papers) for papers in papers_by_year.values()),
            "publication_trend": publication_trend,
            "keyword_evolution": keyword_evolution,
            "emerging_topics": emerging_topics,
            "declining_topics": declining_topics,
            "top_authors": top_authors,
            "top_venues": top_venues
        }

    def _collect_papers_by_year(
        self,
        topic: str,
        start_year: int,
        end_year: int,
        papers_per_year: int
    ) -> Dict[int, List[Paper]]:
        """연도별 논문 수집"""
        papers_by_year = {}

        for year in range(start_year, end_year + 1):
            logger.info(f"Collecting papers for {year}...")

            papers = self.ss_client.search_papers(
                topic,
                limit=papers_per_year,
                year_range=(year, year)
            )

            oa_papers = self.openalex_client.search_papers(
                topic, limit=papers_per_year, year_range=(year, year)
            )
            papers = self._dedupe_by_doi_title(list(papers) + list(oa_papers))

            papers_by_year[year] = papers
            logger.debug(f"  {year}: {len(papers)} papers")

        return papers_by_year

    def _dedupe_by_doi_title(self, papers):
        """DOI 우선, 제목 정규화 보조 중복 제거 (소스 간 겹침 흡수)"""
        seen_dois, seen_titles, unique = set(), set(), []
        for p in papers:
            if p.doi and p.doi in seen_dois:
                continue
            title_key = (p.title or "").lower().strip()
            if not p.doi and title_key and title_key in seen_titles:
                continue
            if p.doi:
                seen_dois.add(p.doi)
            if title_key:
                seen_titles.add(title_key)
            unique.append(p)
        return unique

    def _analyze_publication_trend(
        self,
        papers_by_year: Dict[int, List[Paper]]
    ) -> List[Dict[str, Any]]:
        """연도별 출판 트렌드 분석"""
        trend = []
        prev_count = None

        for year in sorted(papers_by_year.keys()):
            count = len(papers_by_year[year])
            growth = None

            if prev_count and prev_count > 0:
                growth = ((count - prev_count) / prev_count) * 100

            trend.append({
                "year": year,
                "papers": count,
                "growth": growth
            })

            prev_count = count

        return trend

    def _analyze_keyword_evolution(
        self,
        papers_by_year: Dict[int, List[Paper]]
    ) -> Dict[str, List[TrendData]]:
        """키워드 진화 분석"""
        keyword_by_year = defaultdict(lambda: defaultdict(int))

        for year, papers in papers_by_year.items():
            for paper in papers:
                keywords = self._extract_keywords(paper)
                for keyword in keywords:
                    keyword_by_year[keyword][year] += 1

        # TrendData 형식으로 변환
        evolution = {}
        for keyword, year_counts in keyword_by_year.items():
            total_count = sum(year_counts.values())
            if total_count >= 3:  # 최소 3번 이상 등장한 키워드만
                evolution[keyword] = [
                    TrendData(
                        keyword=keyword,
                        year=year,
                        count=count
                    )
                    for year, count in sorted(year_counts.items())
                ]

        return evolution

    def _extract_keywords(self, paper: Paper) -> List[str]:
        """논문에서 키워드 추출"""
        text = f"{paper.title or ''}"
        if paper.abstract:
            text += f" {paper.abstract[:500]}"

        # 소문자 변환 및 토큰화
        words = text.lower().split()

        # 필터링
        keywords = []
        for word in words:
            # 알파벳만, 4자 이상, 불용어 아님
            clean_word = "".join(c for c in word if c.isalpha())
            if len(clean_word) >= 4 and clean_word not in self.STOPWORDS:
                keywords.append(clean_word)

        return keywords

    def _identify_emerging_topics(
        self,
        keyword_evolution: Dict[str, List[TrendData]],
        config: TrendConfig
    ) -> List[Dict[str, Any]]:
        """급부상 토픽 식별"""
        emerging = []

        for keyword, trend_data in keyword_evolution.items():
            if len(trend_data) < 2:
                continue

            # 최근 2년 vs 이전 비교
            recent = sum(t.count for t in trend_data[-2:])
            earlier = sum(t.count for t in trend_data[:-2]) or 1

            growth_rate = ((recent - earlier) / earlier) * 100

            if growth_rate > 30 and recent >= config.min_papers:
                emerging.append({
                    "keyword": keyword,
                    "growth_rate": growth_rate,
                    "recent_count": recent,
                    "total_count": sum(t.count for t in trend_data)
                })

        return sorted(emerging, key=lambda x: x["growth_rate"], reverse=True)[:config.top_n]

    def _identify_declining_topics(
        self,
        keyword_evolution: Dict[str, List[TrendData]],
        config: TrendConfig
    ) -> List[Dict[str, Any]]:
        """관심 감소 토픽 식별"""
        declining = []

        for keyword, trend_data in keyword_evolution.items():
            if len(trend_data) < 3:
                continue

            earlier = sum(t.count for t in trend_data[:-2])
            recent = sum(t.count for t in trend_data[-2:])

            if earlier >= config.min_papers:
                decline_rate = ((earlier - recent) / earlier) * 100

                if decline_rate > 20:
                    declining.append({
                        "keyword": keyword,
                        "decline_rate": decline_rate,
                        "recent_count": recent,
                        "earlier_count": earlier
                    })

        return sorted(declining, key=lambda x: x["decline_rate"], reverse=True)[:config.top_n]

    def _analyze_top_authors(
        self,
        papers_by_year: Dict[int, List[Paper]],
        config: TrendConfig
    ) -> List[Dict[str, Any]]:
        """핵심 저자 분석"""
        author_stats = defaultdict(lambda: {
            "papers": 0,
            "citations": 0,
            "years": set(),
            "affiliations": set()
        })

        for year, papers in papers_by_year.items():
            for paper in papers:
                for author in paper.authors:
                    stats = author_stats[author.name]
                    stats["papers"] += 1
                    stats["citations"] += paper.citation_count
                    stats["years"].add(year)
                    if author.affiliation:
                        stats["affiliations"].add(author.affiliation)

        # 점수 계산 및 정렬
        authors = []
        for name, stats in author_stats.items():
            if stats["papers"] >= 2:  # 최소 2편 이상
                authors.append({
                    "name": name,
                    "papers": stats["papers"],
                    "citations": stats["citations"],
                    "active_years": len(stats["years"]),
                    "affiliations": list(stats["affiliations"])[:3]
                })

        # 논문 수 * log(인용수+1) 점수로 정렬
        import math
        return sorted(
            authors,
            key=lambda x: x["papers"] * math.log(x["citations"] + 1 + 1),
            reverse=True
        )[:config.top_n]

    def _analyze_top_venues(
        self,
        papers_by_year: Dict[int, List[Paper]],
        config: TrendConfig
    ) -> List[Dict[str, Any]]:
        """주요 학회/저널 분석"""
        venue_counter = Counter()
        venue_citations = defaultdict(int)

        for year, papers in papers_by_year.items():
            for paper in papers:
                if paper.venue:
                    venue_counter[paper.venue] += 1
                    venue_citations[paper.venue] += paper.citation_count

        venues = []
        for venue, count in venue_counter.most_common(config.top_n * 2):
            if count >= 2:  # 최소 2편 이상
                venues.append({
                    "venue": venue,
                    "papers": count,
                    "total_citations": venue_citations[venue],
                    "avg_citations": venue_citations[venue] / count
                })

        return venues[:config.top_n]

    def generate_report(self, analysis_result: Dict[str, Any]) -> Path:
        """Markdown 리포트 생성"""
        return self.writer.write_trend_report(
            topic=analysis_result["topic"],
            publication_trend=analysis_result["publication_trend"],
            emerging_topics=analysis_result["emerging_topics"],
            declining_topics=analysis_result["declining_topics"],
            top_authors=analysis_result["top_authors"],
            top_venues=analysis_result["top_venues"]
        )


def parse_args():
    """CLI 인자 파싱"""
    parser = argparse.ArgumentParser(
        description="Research Trend Analyzer - 연구 트렌드 분석",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 분석 (최근 5년)
  python research_trend_analyzer.py "deep learning NLP"

  # 10년 분석
  python research_trend_analyzer.py "machine learning" --years 10

  # 상위 20개 결과
  python research_trend_analyzer.py "transformer" --top-n 20
        """
    )

    parser.add_argument(
        "topic",
        help="분석할 연구 주제/키워드"
    )
    parser.add_argument(
        "--years", "-y",
        type=int,
        default=5,
        help="분석 기간 (년, 기본: 5)"
    )
    parser.add_argument(
        "--top-n", "-n",
        type=int,
        default=10,
        help="상위 N개 결과 (기본: 10)"
    )
    parser.add_argument(
        "--papers-per-year",
        type=int,
        default=100,
        help="연도당 검색할 논문 수 (기본: 100)"
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

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    config = Config.load()

    trend_config = TrendConfig(
        years=args.years,
        top_n=args.top_n,
        papers_per_year=args.papers_per_year
    )

    analyzer = ResearchTrendAnalyzer(config)

    try:
        print(f"\n트렌드 분석 중: '{args.topic}' (최근 {args.years}년)")
        print("데이터 수집 중... (API 호출이 많아 시간이 걸릴 수 있습니다)")

        result = analyzer.analyze_trend(args.topic, trend_config)

        print(f"\n{'='*60}")
        print(f"분석 완료: {args.topic}")
        print(f"{'='*60}")
        print(f"  분석 기간:    {result['period']}")
        print(f"  총 논문 수:   {result['total_papers']}편")
        print(f"  Emerging:     {len(result['emerging_topics'])}개 토픽")
        print(f"  Declining:    {len(result['declining_topics'])}개 토픽")
        print(f"  Top Authors:  {len(result['top_authors'])}명")
        print(f"  Top Venues:   {len(result['top_venues'])}개")
        print(f"{'='*60}\n")

        # Emerging 토픽 미리보기
        if result["emerging_topics"]:
            print("Emerging Topics:")
            for i, topic in enumerate(result["emerging_topics"][:5], 1):
                print(f"  {i}. {topic['keyword']} (+{topic['growth_rate']:.0f}%)")
            print()

        # Top Authors 미리보기
        if result["top_authors"]:
            print("Top Authors:")
            for i, author in enumerate(result["top_authors"][:5], 1):
                print(f"  {i}. {author['name']} ({author['papers']} papers)")
            print()

        # 리포트 생성
        if not args.no_report:
            report_path = analyzer.generate_report(result)
            print(f"리포트 저장: {report_path}")

    except (OSError, ValueError, RuntimeError) as e:
        logger.error(f"분석 실패: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
