"""pyflakes 'undefined name' 정적 검사 — import 스모크가 못 잡는 본문 전용 참조 파손 가드."""
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def test_no_undefined_names():
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(SCRIPTS)],
        capture_output=True, text=True,
    )
    undefined = [l for l in result.stdout.splitlines() if "undefined name" in l]
    assert undefined == [], "\n".join(undefined)
