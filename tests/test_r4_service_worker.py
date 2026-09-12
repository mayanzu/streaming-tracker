"""R4-01 Service Worker 外壳提交语义：运行 node VM 生命周期测试。

`tests/sw_shell_test.mjs` 覆盖首次安装、核心资源失败不提交、可选资源失败与
activate 条件清理；node 不可用时跳过，失败时输出子进程 stdout/stderr。
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_TEST = "tests/sw_shell_test.mjs"


def test_service_worker_shell_commit_semantics():
    node = shutil.which("node")
    if not node:
        pytest.skip("未找到 node，跳过 Service Worker 外壳测试")
    result = subprocess.run(
        [node, NODE_TEST],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, (
        "tests/sw_shell_test.mjs 断言失败\n"
        f"exit={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
