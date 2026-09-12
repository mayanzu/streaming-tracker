"""R4-05/R4-06/R4-02 前端探针的 pytest 包装器。

运行：python -m pytest tests/test_r4_app_js.py -q
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_r4_probes():
    result = subprocess.run(
        ["node", "tests/test_r4_app_js.mjs"],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
