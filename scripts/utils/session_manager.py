"""
Literature Discovery Team - Session Manager

Deep Research 세션 상태 관리
- 세션 생성/저장/로드/재개
- state.json 기반 상태 관리
- sources.jsonl 저장

Credits:
    fivetaku의 Deep Research Kit (orchestrator.py)에서 영감을 받아
    학술 검색에 맞게 간소화하여 재구현함.
    https://github.com/fivetaku/deep-research-kit
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class SessionStatus(Enum):
    """세션 상태"""
    INITIALIZED = "initialized"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"  # 재개 가능 상태


@dataclass
class SessionConfig:
    """세션 설정"""
    depth: str = "medium"
    sources: str = "all"
    year_start: Optional[int] = None
    year_end: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "depth": self.depth,
            "sources": self.sources,
            "year_range": [self.year_start, self.year_end] if self.year_start else None
        }


@dataclass
class SessionProgress:
    """세션 진행 상태"""
    iteration: int = 0
    papers_found: int = 0
    queries_executed: List[str] = field(default_factory=list)
    keywords_discovered: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "iteration": self.iteration,
            "papers_found": self.papers_found,
            "queries_executed": self.queries_executed,
            "keywords_discovered": self.keywords_discovered
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionProgress":
        return cls(
            iteration=data.get("iteration", 0),
            papers_found=data.get("papers_found", 0),
            queries_executed=data.get("queries_executed", []),
            keywords_discovered=data.get("keywords_discovered", [])
        )


class SessionManager:
    """
    Deep Research 세션 관리자

    세션 폴더 구조:
    RESEARCH/{topic}_{timestamp}/
    ├── state.json              # 세션 상태
    ├── README.md               # 네비게이션 가이드
    ├── sources/
    │   ├── sources.jsonl       # 수집된 소스 메타데이터
    │   └── bibliography.md     # 참고문헌
    └── outputs/
        ├── 00_executive_summary.md
        ├── 01_influential_papers.md
        └── ...
    """

    def __init__(self, base_dir: str = ""):
        resolved = Path(base_dir) if base_dir else Path(
            os.environ.get("PAPER_SCOUT_OUTPUT_DIR") or Path.cwd() / "literature-discovery") / "sessions"
        self.base_dir = resolved
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.session_id: Optional[str] = None
        self.session_path: Optional[Path] = None
        self.state: Dict[str, Any] = {}
        self._initialized = False

    def create_session(
        self,
        topic: str,
        config: Optional[SessionConfig] = None
    ) -> str:
        """
        새 세션 생성

        Args:
            topic: 연구 주제
            config: 세션 설정

        Returns:
            session_id
        """
        if config is None:
            config = SessionConfig()

        # 세션 ID 생성
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_topic = self._sanitize_topic(topic)
        self.session_id = f"{safe_topic}_{timestamp}"
        self.session_path = self.base_dir / self.session_id

        # 폴더 구조 생성
        folders = [
            "sources",
            "outputs",
        ]
        for folder in folders:
            (self.session_path / folder).mkdir(parents=True, exist_ok=True)

        # 상태 초기화
        self.state = {
            "session_id": self.session_id,
            "topic": topic,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "status": SessionStatus.INITIALIZED.value,
            "config": config.to_dict(),
            "progress": SessionProgress().to_dict(),
            "sources_count": 0,
            "quality_distribution": {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0},
            "errors": []
        }

        self._initialized = True
        self._save_state()
        self._create_readme()

        logger.info(f"Session created: {self.session_id}")
        return self.session_id

    def load_session(self, session_id: str) -> bool:
        """
        기존 세션 로드

        Args:
            session_id: 세션 ID

        Returns:
            로드 성공 여부
        """
        self.session_path = self.base_dir / session_id
        state_file = self.session_path / "state.json"

        if not state_file.exists():
            logger.error(f"Session not found: {session_id}")
            return False

        with open(state_file, "r", encoding="utf-8") as f:
            self.state = json.load(f)

        self.session_id = session_id
        self._initialized = True

        logger.info(f"Session loaded: {session_id}")
        return True

    def get_progress(self) -> SessionProgress:
        """현재 진행 상태 반환"""
        self._ensure_initialized()
        return SessionProgress.from_dict(self.state.get("progress", {}))

    def update_progress(self, progress: SessionProgress):
        """진행 상태 업데이트"""
        self._ensure_initialized()
        self.state["progress"] = progress.to_dict()
        self.state["status"] = SessionStatus.IN_PROGRESS.value
        self._save_state()

    def add_source(self, source_data: Dict[str, Any]):
        """
        소스 추가 (sources.jsonl에 저장)

        Args:
            source_data: 소스 메타데이터 딕셔너리
        """
        self._ensure_initialized()

        sources_file = self.session_path / "sources" / "sources.jsonl"
        with open(sources_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(source_data, ensure_ascii=False) + "\n")

        # 카운터 증가
        self.state["sources_count"] += 1

        # 품질 등급 분포 업데이트
        grade = source_data.get("quality_grade", "C")
        if grade in self.state["quality_distribution"]:
            self.state["quality_distribution"][grade] += 1

        # 10개마다 상태 저장 (성능)
        if self.state["sources_count"] % 10 == 0:
            self._save_state()

    def get_sources(self) -> List[Dict[str, Any]]:
        """저장된 소스 목록 반환"""
        self._ensure_initialized()

        sources_file = self.session_path / "sources" / "sources.jsonl"
        sources = []

        if sources_file.exists():
            with open(sources_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            sources.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue

        return sources

    def save_output(self, filename: str, content: str):
        """출력 파일 저장"""
        self._ensure_initialized()
        output_path = self.session_path / "outputs" / filename

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.debug(f"Output saved: {filename}")

    def save_bibliography(self, content: str):
        """참고문헌 저장"""
        self._ensure_initialized()
        bib_path = self.session_path / "sources" / "bibliography.md"

        with open(bib_path, "w", encoding="utf-8") as f:
            f.write(content)

    def mark_completed(self):
        """세션 완료 처리"""
        self._ensure_initialized()
        self.state["status"] = SessionStatus.COMPLETED.value
        self._save_state()
        self._create_readme()
        logger.info(f"Session completed: {self.session_id}")

    def mark_paused(self):
        """세션 일시정지 (재개 가능)"""
        self._ensure_initialized()
        self.state["status"] = SessionStatus.PAUSED.value
        self._save_state()
        logger.info(f"Session paused: {self.session_id}")

    def mark_failed(self, error: str):
        """세션 실패 처리"""
        self._ensure_initialized()
        self.state["status"] = SessionStatus.FAILED.value
        self.state["errors"].append({
            "error": error,
            "timestamp": datetime.now().isoformat()
        })
        self._save_state()
        logger.error(f"Session failed: {self.session_id} - {error}")

    def can_resume(self) -> bool:
        """재개 가능 여부"""
        self._ensure_initialized()
        return self.state.get("status") in [
            SessionStatus.IN_PROGRESS.value,
            SessionStatus.PAUSED.value
        ]

    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        모든 세션 목록 반환

        Returns:
            세션 정보 리스트 (최신순 정렬)
        """
        sessions = []

        if not self.base_dir.exists():
            return sessions

        for folder in self.base_dir.iterdir():
            if folder.is_dir():
                state_file = folder / "state.json"
                if state_file.exists():
                    try:
                        with open(state_file, "r", encoding="utf-8") as f:
                            state = json.load(f)
                        sessions.append({
                            "session_id": state.get("session_id"),
                            "topic": state.get("topic"),
                            "status": state.get("status"),
                            "sources_count": state.get("sources_count", 0),
                            "created_at": state.get("created_at"),
                            "updated_at": state.get("updated_at"),
                            "can_resume": state.get("status") in [
                                SessionStatus.IN_PROGRESS.value,
                                SessionStatus.PAUSED.value
                            ]
                        })
                    except (json.JSONDecodeError, KeyError):
                        continue

        # 최신순 정렬
        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return sessions

    def _ensure_initialized(self):
        """세션 초기화 확인"""
        if not self._initialized:
            raise ValueError("Session not initialized. Call create_session() or load_session() first.")

    def _save_state(self):
        """상태 저장"""
        self._ensure_initialized()
        self.state["updated_at"] = datetime.now().isoformat()
        state_file = self.session_path / "state.json"

        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def _sanitize_topic(self, topic: str) -> str:
        """토픽 문자열 정규화 (파일명용)"""
        # 특수문자 제거, 공백은 언더스코어로
        sanitized = re.sub(r'[^\w\s-]', '', topic)
        sanitized = re.sub(r'\s+', '_', sanitized)
        return sanitized[:50].strip('_')

    def _create_readme(self):
        """세션 README.md 생성"""
        self._ensure_initialized()

        progress = self.state.get("progress", {})
        quality = self.state.get("quality_distribution", {})

        readme_content = f"""# Deep Research: {self.state["topic"]}

## Session Info

| Field | Value |
|-------|-------|
| **Session ID** | `{self.session_id}` |
| **Status** | {self.state.get("status", "unknown")} |
| **Created** | {self.state.get("created_at", "")} |
| **Updated** | {self.state.get("updated_at", "")} |
| **Sources** | {self.state.get("sources_count", 0)} |

## Configuration

- **Depth**: {self.state.get("config", {}).get("depth", "medium")}
- **Sources**: {self.state.get("config", {}).get("sources", "all")}
- **Year Range**: {self.state.get("config", {}).get("year_range", "all")}

## Progress

- **Iterations**: {progress.get("iteration", 0)}
- **Papers Found**: {progress.get("papers_found", 0)}
- **Queries Executed**: {len(progress.get("queries_executed", []))}

## Quality Distribution

| Grade | Count | Description |
|-------|-------|-------------|
| A | {quality.get("A", 0)} | Top-tier journals |
| B | {quality.get("B", 0)} | Major conferences/journals |
| C | {quality.get("C", 0)} | Standard publications |
| D | {quality.get("D", 0)} | Preprints |
| E | {quality.get("E", 0)} | Other |

## Folder Structure

```
{self.session_id}/
├── state.json              # Session state (resumable)
├── README.md               # This file
├── sources/
│   ├── sources.jsonl       # All source metadata
│   └── bibliography.md     # Formatted references
└── outputs/
    ├── 00_executive_summary.md
    ├── 01_influential_papers.md
    ├── 02_recent_papers.md
    └── 03_trends.md
```

## Resume Command

```bash
python deep_researcher.py --resume {self.session_id}
```

---
*Generated by Deep Researcher - Literature Discovery Team*

*Session management inspired by [fivetaku/deep-research-kit](https://github.com/fivetaku/deep-research-kit)*
"""
        readme_path = self.session_path / "README.md"
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(readme_content)
