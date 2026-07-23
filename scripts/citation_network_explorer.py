#!/usr/bin/env python
"""
Citation Network Explorer
인용 네트워크 분석 에이전트

사용법:
    python citation_network_explorer.py "10.1234/example.doi" --depth 2
    python citation_network_explorer.py --paper-id "649def34..." --direction citing
"""

import argparse
import sys
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

# 상위 디렉토리를 path에 추가
sys.path.insert(0, str(Path(__file__).parent))

try:
    import networkx as nx
    HAS_NETWORKX = True
except ImportError:
    HAS_NETWORKX = False
    print("Warning: networkx not installed. Run: pip install networkx")

try:
    import community as community_louvain
    HAS_LOUVAIN = True
except ImportError:
    HAS_LOUVAIN = False

from config import Config
from utils.api_clients import SemanticScholarClient, OpenCitationsClient
from utils.paper_models import Paper
from utils.markdown_writer import MarkdownWriter

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@dataclass
class NetworkConfig:
    """네트워크 분석 설정"""
    depth: int = 2                      # 탐색 깊이
    direction: str = "both"             # both, citing, cited
    max_nodes: int = 200                # 최대 노드 수
    cluster_method: str = "louvain"     # 클러스터링 방법


class CitationNetworkExplorer:
    """인용 네트워크 분석 에이전트"""

    def __init__(self, config: Config):
        if not HAS_NETWORKX:
            raise ImportError("networkx 라이브러리가 필요합니다: pip install networkx")

        self.config = config
        self.ss_client = SemanticScholarClient(
            api_key=config.semantic_scholar_api_key
        )
        self.oc_client = OpenCitationsClient(
            api_token=config.opencitations_api_token
        )
        self.writer = MarkdownWriter(str(config.output_dir))

        self.graph: Optional[nx.DiGraph] = None
        self.papers: Dict[str, Paper] = {}

    def build_network(
        self,
        seed_paper_id: str,
        network_config: NetworkConfig
    ) -> nx.DiGraph:
        """
        인용 네트워크 구축

        Args:
            seed_paper_id: 시작 논문 ID (Semantic Scholar ID 또는 DOI:xxx)
            network_config: 네트워크 설정

        Returns:
            NetworkX DiGraph
        """
        self.graph = nx.DiGraph()
        self.papers = {}

        visited = set()
        queue = [(seed_paper_id, 0)]  # (paper_id, depth)

        logger.info(f"Building citation network from {seed_paper_id} (depth={network_config.depth})")

        while queue and len(self.graph.nodes) < network_config.max_nodes:
            paper_id, depth = queue.pop(0)

            if paper_id in visited or depth > network_config.depth:
                continue
            visited.add(paper_id)

            # 논문 정보 조회
            paper = self._get_paper_info(paper_id)
            if not paper:
                continue

            self.papers[paper_id] = paper
            self.graph.add_node(
                paper_id,
                title=paper.title,
                year=paper.year,
                citations=paper.citation_count
            )

            logger.debug(f"Added node: {paper_id} (depth={depth})")

            # 인용 관계 탐색
            if network_config.direction in ["both", "citing"]:
                # 이 논문을 인용한 논문들
                citing_papers = self._get_citing_papers(paper_id)
                for citing in citing_papers[:20]:  # 각 방향 최대 20개
                    if citing.paper_id not in visited:
                        self.graph.add_edge(citing.paper_id, paper_id)
                        if depth < network_config.depth:
                            queue.append((citing.paper_id, depth + 1))

            if network_config.direction in ["both", "cited"]:
                # 이 논문이 인용한 논문들
                cited_papers = self._get_cited_papers(paper_id)
                for cited in cited_papers[:20]:
                    if cited.paper_id not in visited:
                        self.graph.add_edge(paper_id, cited.paper_id)
                        if depth < network_config.depth:
                            queue.append((cited.paper_id, depth + 1))

            # 진행 상황 출력
            if len(self.graph.nodes) % 10 == 0:
                logger.info(f"Progress: {len(self.graph.nodes)} nodes, {len(self.graph.edges)} edges")

        logger.info(f"Network built: {len(self.graph.nodes)} nodes, {len(self.graph.edges)} edges")
        return self.graph

    def _get_paper_info(self, paper_id: str) -> Optional[Paper]:
        """논문 정보 조회"""
        return self.ss_client.get_paper(paper_id)

    def _get_citing_papers(self, paper_id: str) -> List[Paper]:
        """이 논문을 인용한 논문들"""
        return self.ss_client.get_paper_citations(paper_id, limit=50)

    def _get_cited_papers(self, paper_id: str) -> List[Paper]:
        """이 논문이 인용한 논문들"""
        return self.ss_client.get_paper_references(paper_id, limit=50)

    def analyze_network(self) -> Dict[str, Any]:
        """
        네트워크 분석 수행

        Returns:
            분석 결과 딕셔너리
        """
        if not self.graph or len(self.graph.nodes) == 0:
            raise ValueError("네트워크가 구축되지 않았습니다.")

        analysis = {
            "statistics": self._calculate_statistics(),
            "pagerank": self._calculate_pagerank(),
            "betweenness": self._calculate_betweenness(),
            "clusters": self._detect_clusters()
        }

        return analysis

    def _calculate_statistics(self) -> Dict[str, Any]:
        """네트워크 기본 통계"""
        num_nodes = self.graph.number_of_nodes()
        return {
            "total_nodes": num_nodes,
            "total_edges": self.graph.number_of_edges(),
            "density": nx.density(self.graph),
            "avg_in_degree": sum(d for _, d in self.graph.in_degree()) / max(1, num_nodes),
            "avg_out_degree": sum(d for _, d in self.graph.out_degree()) / max(1, num_nodes)
        }

    def _calculate_pagerank(self) -> Dict[str, float]:
        """PageRank 계산"""
        try:
            return nx.pagerank(self.graph)
        except (nx.NetworkXException, ValueError, ZeroDivisionError, ImportError):
            return {}

    def _calculate_betweenness(self) -> Dict[str, float]:
        """Betweenness Centrality 계산"""
        try:
            return nx.betweenness_centrality(self.graph)
        except (nx.NetworkXException, ValueError, ZeroDivisionError, ImportError):
            return {}

    def _detect_clusters(self) -> List[Dict]:
        """커뮤니티/클러스터 탐지"""
        if not HAS_LOUVAIN:
            # python-louvain 없으면 connected components 사용
            components = list(nx.weakly_connected_components(self.graph))
            return [
                {
                    "cluster_id": i,
                    "size": len(c),
                    "main_topic": "Unknown"
                }
                for i, c in enumerate(components)
            ]

        try:
            # 무방향 그래프로 변환 (커뮤니티 탐지용)
            undirected = self.graph.to_undirected()
            partition = community_louvain.best_partition(undirected)

            # 클러스터별 그룹화
            clusters = {}
            for node, cluster_id in partition.items():
                if cluster_id not in clusters:
                    clusters[cluster_id] = []
                clusters[cluster_id].append(node)

            result = []
            for cluster_id, nodes in clusters.items():
                cluster_papers = [self.papers.get(n) for n in nodes if n in self.papers]
                result.append({
                    "cluster_id": cluster_id,
                    "size": len(nodes),
                    "papers": cluster_papers,
                    "main_topic": self._infer_cluster_topic(cluster_papers)
                })

            return sorted(result, key=lambda c: c["size"], reverse=True)

        except (nx.NetworkXException, ValueError, TypeError, ZeroDivisionError) as e:
            logger.warning(f"Cluster detection failed: {e}")
            return []

    def _infer_cluster_topic(self, papers: List[Paper]) -> str:
        """클러스터의 주요 토픽 추론 (제목 기반)"""
        if not papers:
            return "Unknown"

        # 제목에서 자주 등장하는 단어 추출
        from collections import Counter

        stopwords = {
            "a", "an", "the", "of", "in", "on", "at", "to", "for", "with",
            "and", "or", "is", "are", "using", "based", "via", "from", "by"
        }

        words = []
        for paper in papers:
            if paper and paper.title:
                title_words = paper.title.lower().split()
                words.extend([w for w in title_words if w.isalnum() and len(w) > 3 and w not in stopwords])

        if not words:
            return "Unknown"

        most_common = Counter(words).most_common(3)
        return ", ".join([w for w, _ in most_common])

    def get_key_papers(self, top_n: int = 10) -> List[Paper]:
        """PageRank 기준 핵심 논문 추출"""
        pagerank = self._calculate_pagerank()
        betweenness = self._calculate_betweenness()

        sorted_papers = sorted(
            pagerank.items(),
            key=lambda x: x[1],
            reverse=True
        )[:top_n]

        key_papers = []
        for paper_id, pr_score in sorted_papers:
            if paper_id in self.papers:
                paper = self.papers[paper_id]
                paper.pagerank = pr_score
                paper.betweenness = betweenness.get(paper_id, 0)
                paper.in_degree = self.graph.in_degree(paper_id)
                paper.out_degree = self.graph.out_degree(paper_id)
                key_papers.append(paper)

        return key_papers

    def generate_mermaid_graph(self, max_nodes: int = 30) -> str:
        """Mermaid 그래프 코드 생성"""
        if not self.graph:
            return "graph LR\n    A[No data]"

        # PageRank 상위 노드만 포함
        pagerank = self._calculate_pagerank()
        top_nodes = sorted(
            pagerank.items(),
            key=lambda x: x[1],
            reverse=True
        )[:max_nodes]
        top_node_ids = {n[0] for n in top_nodes}

        lines = ["graph LR"]

        # 노드 정의
        for node_id in top_node_ids:
            short_title = self._short_title(node_id)
            # Mermaid 노드 ID에서 특수문자 제거
            safe_id = "".join(c if c.isalnum() else "_" for c in node_id[:20])
            lines.append(f"    {safe_id}[\"{short_title}\"]")

        # 엣지 정의
        for source, target in self.graph.edges():
            if source in top_node_ids and target in top_node_ids:
                safe_source = "".join(c if c.isalnum() else "_" for c in source[:20])
                safe_target = "".join(c if c.isalnum() else "_" for c in target[:20])
                lines.append(f"    {safe_source} --> {safe_target}")

        return "\n".join(lines)

    def _short_title(self, paper_id: str) -> str:
        """짧은 제목 생성 (Mermaid용)"""
        if paper_id in self.papers:
            title = self.papers[paper_id].title
            if len(title) > 25:
                return title[:22] + "..."
            return title
        return paper_id[:15]

    def generate_report(self, seed_paper: Paper) -> Path:
        """Markdown 리포트 생성"""
        analysis = self.analyze_network()
        key_papers = self.get_key_papers()
        mermaid_graph = self.generate_mermaid_graph()

        return self.writer.write_citation_network_report(
            seed_paper=seed_paper,
            network_stats=analysis["statistics"],
            key_papers=key_papers,
            clusters=analysis["clusters"],
            mermaid_graph=mermaid_graph
        )


def parse_args():
    """CLI 인자 파싱"""
    parser = argparse.ArgumentParser(
        description="Citation Network Explorer - 인용 네트워크 분석",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # DOI로 네트워크 분석
  python citation_network_explorer.py --doi "10.1145/3292500.3330672"

  # Paper ID로 분석
  python citation_network_explorer.py --paper-id "649def34f8be52c8b66281af98ae884c09aef38b"

  # 깊이와 방향 설정
  python citation_network_explorer.py --doi "10.1234/example" --depth 3 --direction citing
        """
    )

    # 입력 옵션
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--doi", "-d",
        help="논문 DOI"
    )
    input_group.add_argument(
        "--paper-id", "-p",
        help="Semantic Scholar Paper ID"
    )

    # 네트워크 설정
    parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="탐색 깊이 (기본: 2)"
    )
    parser.add_argument(
        "--direction",
        choices=["both", "citing", "cited"],
        default="both",
        help="탐색 방향 (기본: both)"
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=200,
        help="최대 노드 수 (기본: 200)"
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

    network_config = NetworkConfig(
        depth=args.depth,
        direction=args.direction,
        max_nodes=args.max_nodes
    )

    explorer = CitationNetworkExplorer(config)

    # Paper ID 결정
    if args.doi:
        paper_id = f"DOI:{args.doi}"
    else:
        paper_id = args.paper_id

    try:
        print(f"\n인용 네트워크 구축 중... (깊이: {args.depth}, 방향: {args.direction})")
        graph = explorer.build_network(paper_id, network_config)

        print(f"\n{'='*60}")
        print(f"네트워크 구축 완료")
        print(f"{'='*60}")
        print(f"  노드 수: {graph.number_of_nodes()}")
        print(f"  엣지 수: {graph.number_of_edges()}")

        # 분석 수행
        analysis = explorer.analyze_network()
        stats = analysis["statistics"]
        print(f"  밀도:    {stats['density']:.4f}")
        print(f"  클러스터: {len(analysis['clusters'])}개")
        print(f"{'='*60}\n")

        # 핵심 논문 출력
        key_papers = explorer.get_key_papers(5)
        if key_papers:
            print("핵심 논문 (PageRank 기준):")
            for i, paper in enumerate(key_papers, 1):
                print(f"  {i}. {paper.title[:50]}... (PR: {paper.pagerank:.4f})")
            print()

        # 리포트 생성
        if not args.no_report:
            seed_paper = explorer._get_paper_info(paper_id)
            if seed_paper:
                report_path = explorer.generate_report(seed_paper)
                print(f"리포트 저장: {report_path}")

    except (ImportError, OSError, ValueError, RuntimeError, nx.NetworkXException) as e:
        logger.error(f"분석 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
