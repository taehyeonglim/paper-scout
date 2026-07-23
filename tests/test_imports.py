"""전 모듈 import 스모크 — 의존/경로 파손을 가장 싸게 잡는 가드."""
import importlib

import pytest

MODULES = [
    "config",
    "orchestrator",
    "related_paper_finder",
    "citation_network_explorer",
    "research_trend_analyzer",
    "deep_researcher",
    "intelligence.semantic_rerank",
    "intelligence.synthesize",
    "intelligence.coverage_manifest",
    "modules.paper_fetcher",
    "utils.api_clients",
    "utils.paper_models",
    "utils.quality_grader",
    "utils.rate_limiter",
    "utils.cache",
    "utils.markdown_writer",
    "utils.session_manager",
    "kci.kci_core",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)
