"""
Literature Discovery Team - Markdown Writer

SKILL.md 형식에 맞는 Markdown 리포트 생성
"""

import os
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional
import logging

from .paper_models import Paper, RelevanceLevel

logger = logging.getLogger(__name__)


class MarkdownWriter:
    """
    SKILL.md 형식에 맞는 Markdown 리포트 생성기
    """

    def __init__(self, output_dir: str = ""):
        resolved = Path(output_dir) if output_dir else Path(
            os.environ.get("PAPER_SCOUT_OUTPUT_DIR") or Path.cwd() / "literature-discovery")
        self.output_dir = resolved
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _generate_filename(self, prefix: str, topic: str) -> str:
        """파일명 생성"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 특수문자 제거
        safe_topic = "".join(c if c.isalnum() or c in " _-" else "_" for c in topic)
        safe_topic = safe_topic[:50]  # 길이 제한
        return f"{prefix}_{safe_topic}_{timestamp}.md"

    def _format_paper_entry(self, paper: Paper, index: int, show_relevance: bool = True) -> str:
        """개별 논문 Markdown 포맷팅"""
        lines = []

        # 제목
        title = paper.title or "Untitled"
        if paper.url:
            lines.append(f"### {index}. [{title}]({paper.url})")
        elif paper.doi:
            lines.append(f"### {index}. [{title}](https://doi.org/{paper.doi})")
        else:
            lines.append(f"### {index}. {title}")

        # 메타정보
        authors = ", ".join(paper.author_names[:3])
        if len(paper.author_names) > 3:
            authors += " et al."

        meta = []
        if authors:
            meta.append(f"**Authors**: {authors}")
        if paper.year:
            meta.append(f"**Year**: {paper.year}")
        if paper.venue:
            meta.append(f"**Venue**: {paper.venue}")
        if paper.citation_count:
            meta.append(f"**Citations**: {paper.citation_count}")

        if meta:
            lines.append(" | ".join(meta))

        # 관련성 (Related Paper Finder용)
        if show_relevance and paper.relevance_score > 0:
            lines.append(f"**Relevance Score**: {paper.relevance_score:.2f}")
            if paper.relevance_reason:
                lines.append(f"**Relevance**: {paper.relevance_reason}")

        # 초록
        if paper.abstract:
            abstract = paper.abstract[:500]
            if len(paper.abstract) > 500:
                abstract += "..."
            lines.append(f"\n> {abstract}")

        lines.append("")  # 빈 줄
        return "\n".join(lines)

    def write_related_papers_report(
        self,
        query_paper: Paper,
        highly_relevant: List[Paper],
        moderately_relevant: List[Paper],
        citation_stats: Dict[str, int],
        search_query: Optional[str] = None
    ) -> Path:
        """
        Related Paper Finder 출력 (SKILL.md 형식)

        Returns:
            생성된 파일 경로
        """
        lines = []

        # 헤더
        topic = search_query or query_paper.title or "Unknown"
        lines.append(f"# Related Papers: \"{topic[:60]}\"")
        lines.append(f"\n*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")

        # 검색 기준 논문 정보
        if query_paper.paper_id:
            lines.append("## Seed Paper")
            lines.append(self._format_paper_entry(query_paper, 0, show_relevance=False))

        # 인용 통계
        lines.append("## Citation Network")
        lines.append(f"- Papers citing this work: **{citation_stats.get('papers_citing_this', 0)}**")
        lines.append(f"- Papers this work cites: **{citation_stats.get('papers_this_cites', 0)}**")
        lines.append(f"- Key citing papers found: **{citation_stats.get('key_citing_papers', 0)}**")
        lines.append("")

        # Highly Relevant Papers
        lines.append(f"## Highly Relevant (Score > 0.8) - {len(highly_relevant)} papers")
        lines.append("")
        if highly_relevant:
            for i, paper in enumerate(highly_relevant, 1):
                lines.append(self._format_paper_entry(paper, i))
        else:
            lines.append("*No highly relevant papers found.*\n")

        # Moderately Relevant Papers
        lines.append(f"## Moderately Relevant (Score 0.5-0.8) - {len(moderately_relevant)} papers")
        lines.append("")
        if moderately_relevant:
            for i, paper in enumerate(moderately_relevant, 1):
                lines.append(self._format_paper_entry(paper, i))
        else:
            lines.append("*No moderately relevant papers found.*\n")

        # 파일 저장
        filename = self._generate_filename("related_papers", topic)
        filepath = self.output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"Related papers report saved: {filepath}")
        return filepath

    def write_citation_network_report(
        self,
        seed_paper: Paper,
        network_stats: Dict[str, Any],
        key_papers: List[Paper],
        clusters: List[Dict],
        mermaid_graph: str
    ) -> Path:
        """
        Citation Network Explorer 출력 (SKILL.md 형식)

        Returns:
            생성된 파일 경로
        """
        lines = []

        # 헤더
        topic = seed_paper.title or "Unknown"
        lines.append(f"# Citation Network Analysis: \"{topic[:60]}\"")
        lines.append(f"\n*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")

        # Network Statistics
        lines.append("## Network Statistics")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Total Nodes | {network_stats.get('total_nodes', 0)} |")
        lines.append(f"| Total Edges | {network_stats.get('total_edges', 0)} |")
        lines.append(f"| Network Density | {network_stats.get('density', 0):.4f} |")
        lines.append(f"| Avg In-Degree | {network_stats.get('avg_in_degree', 0):.2f} |")
        lines.append(f"| Avg Out-Degree | {network_stats.get('avg_out_degree', 0):.2f} |")
        lines.append("")

        # Key Papers by PageRank
        lines.append("## Key Papers (by PageRank)")
        lines.append("")
        lines.append("| Rank | Paper | Year | Citations | PageRank |")
        lines.append("|------|-------|------|-----------|----------|")

        for i, paper in enumerate(key_papers[:10], 1):
            title = paper.title[:40] + "..." if len(paper.title) > 40 else paper.title
            lines.append(f"| {i} | {title} | {paper.year or 'N/A'} | {paper.citation_count} | {paper.pagerank:.4f} |")
        lines.append("")

        # Research Timeline (Mermaid)
        lines.append("## Research Timeline")
        lines.append("")
        lines.append("```mermaid")
        lines.append(mermaid_graph)
        lines.append("```")
        lines.append("")

        # Clusters
        lines.append("## Clusters")
        lines.append("")
        for cluster in clusters[:5]:  # 상위 5개 클러스터
            cluster_id = cluster.get("cluster_id", "?")
            size = cluster.get("size", 0)
            main_topic = cluster.get("main_topic", "Unknown")
            lines.append(f"- **Cluster {cluster_id}** - {main_topic}: {size} papers")
        lines.append("")

        # 파일 저장
        filename = self._generate_filename("citation_network", topic)
        filepath = self.output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"Citation network report saved: {filepath}")
        return filepath

    def write_trend_report(
        self,
        topic: str,
        publication_trend: List[Dict],
        emerging_topics: List[Dict],
        declining_topics: List[Dict],
        top_authors: List[Dict],
        top_venues: List[Dict]
    ) -> Path:
        """
        Research Trend Analyzer 출력 (SKILL.md 형식)

        Returns:
            생성된 파일 경로
        """
        lines = []

        # 헤더
        lines.append(f"# Research Trend Analysis: \"{topic}\"")
        lines.append(f"\n*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")

        # Publication Trend
        lines.append("## Publication Trend")
        lines.append("")
        lines.append("| Year | Papers | Growth |")
        lines.append("|------|--------|--------|")

        for entry in publication_trend:
            year = entry.get("year", "")
            papers = entry.get("papers", 0)
            growth = entry.get("growth")
            growth_str = f"{growth:+.1f}%" if growth is not None else "N/A"
            lines.append(f"| {year} | {papers} | {growth_str} |")
        lines.append("")

        # Emerging Topics
        lines.append("## Emerging Topics")
        lines.append("")
        if emerging_topics:
            for i, topic_data in enumerate(emerging_topics[:10], 1):
                keyword = topic_data.get("keyword", "")
                growth = topic_data.get("growth_rate", 0)
                count = topic_data.get("recent_count", 0)
                lines.append(f"{i}. **{keyword}** - Growth: +{growth:.0f}% ({count} recent papers)")
        else:
            lines.append("*No emerging topics identified.*")
        lines.append("")

        # Declining Topics
        lines.append("## Declining Topics")
        lines.append("")
        if declining_topics:
            for i, topic_data in enumerate(declining_topics[:10], 1):
                keyword = topic_data.get("keyword", "")
                decline = topic_data.get("decline_rate", 0)
                lines.append(f"{i}. **{keyword}** - Decline: -{decline:.0f}%")
        else:
            lines.append("*No declining topics identified.*")
        lines.append("")

        # Top Authors
        lines.append("## Key Researchers")
        lines.append("")
        lines.append("| Author | Papers | Citations | Affiliations |")
        lines.append("|--------|--------|-----------|--------------|")

        for author in top_authors[:10]:
            name = author.get("name", "")
            papers = author.get("papers", 0)
            citations = author.get("citations", 0)
            affiliations = ", ".join(author.get("affiliations", [])[:2]) or "N/A"
            if len(affiliations) > 30:
                affiliations = affiliations[:27] + "..."
            lines.append(f"| {name} | {papers} | {citations} | {affiliations} |")
        lines.append("")

        # Top Venues
        lines.append("## Top Venues")
        lines.append("")
        lines.append("| Venue | Papers | Total Citations | Avg Citations |")
        lines.append("|-------|--------|-----------------|---------------|")

        for venue in top_venues[:10]:
            venue_name = venue.get("venue", "")[:40]
            papers = venue.get("papers", 0)
            total_cit = venue.get("total_citations", 0)
            avg_cit = venue.get("avg_citations", 0)
            lines.append(f"| {venue_name} | {papers} | {total_cit} | {avg_cit:.1f} |")
        lines.append("")

        # 파일 저장
        filename = self._generate_filename("trend_analysis", topic)
        filepath = self.output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        logger.info(f"Trend report saved: {filepath}")
        return filepath
