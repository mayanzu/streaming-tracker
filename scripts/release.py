"""发布助手：同步静态资源版本号、写入版本元数据并（可选）核对线上部署。

用法：
    python scripts/release.py                        # 用 git short sha 作为版本发布
    python scripts/release.py --version 1.2.0        # 指定版本号
    python scripts/release.py --check-url http://192.168.31.3:8000
    python scripts/release.py --check-only --check-url http://192.168.31.3:8000

步骤：
1. 计算版本：--version > APP_VERSION 环境变量 > 现有 data/version.json > 默认；
   build_id 取 git short sha（无仓库时回退版本号）；
2. 把 static/index.html 与 static/sw.js 中的所有 ?v= 与 SW_VERSION 同步为 asset_version；
3. 写入 data/version.json（/ready 与 /api/stats 会读取）；
4. --check-url 时 curl /ready、/sw.js 与 / 核对版本，输出 PASS/FAIL。
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INDEX_FILE = BASE_DIR / "static" / "index.html"
SW_FILE = BASE_DIR / "static" / "sw.js"
VERSION_FILE = BASE_DIR / "data" / "version.json"

DEFAULT_VERSION = "1.1.0"
ASSET_QUERY_RE = re.compile(r"(\?v=)[A-Za-z0-9._-]+")
SW_VERSION_RE = re.compile(r"(SW_VERSION\s*=\s*')[^']+(')")


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _git_short_sha():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def resolve_version(explicit=""):
    if explicit.strip():
        return explicit.strip(), _git_short_sha() or explicit.strip()
    env_version = __import__("os").environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version, _git_short_sha() or env_version
    existing = _read_json(VERSION_FILE)
    sha = _git_short_sha()
    version = str(existing.get("app_version") or DEFAULT_VERSION)
    return version, sha or str(existing.get("build_id") or version)


def update_assets(asset_version):
    """同步 index.html/sw.js 的 ?v= 与 SW_VERSION；返回 (index_changed, sw_changed)。"""
    index_text = INDEX_FILE.read_text(encoding="utf-8")
    new_index = ASSET_QUERY_RE.sub(rf"\g<1>{asset_version}", index_text)
    sw_text = SW_FILE.read_text(encoding="utf-8")
    new_sw = SW_VERSION_RE.sub(rf"\g<1>{asset_version}\g<2>", sw_text)
    new_sw = ASSET_QUERY_RE.sub(rf"\g<1>{asset_version}", new_sw)
    return new_index != index_text, new_sw != sw_text, new_index, new_sw


def check_deployment(base_url, expected_version, expected_asset_version=None):
    expected_asset = expected_asset_version or expected_version
    base = base_url.rstrip("/")
    checks = []
    try:
        with urllib.request.urlopen(f"{base}/ready", timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 503 degraded 也会返回完整 JSON，仍要核对版本
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            payload = None
        if payload is None:
            checks.append(("/ready.app_version", False, f"HTTP {exc.code} without JSON body"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        payload = None
        checks.append(("/ready.app_version", False, f"request failed: {exc}"))
    if payload is not None:
        actual = str(payload.get("app_version") or "")
        checks.append(("/ready.app_version", actual == expected_version, f"expected={expected_version} actual={actual}"))

    try:
        with urllib.request.urlopen(f"{base}/sw.js", timeout=8) as response:
            checks.append(("/sw.js", response.status == 200, f"HTTP {response.status}"))
    except urllib.error.HTTPError as exc:
        checks.append(("/sw.js", False, f"HTTP {exc.code}"))
    except (urllib.error.URLError, OSError) as exc:
        checks.append(("/sw.js", False, f"request failed: {exc}"))

    try:
        with urllib.request.urlopen(f"{base}/", timeout=8) as response:
            html = response.read().decode("utf-8", errors="ignore")
        versions = set(re.findall(r"\?v=([A-Za-z0-9._-]+)", html))
        ok = bool(versions) and versions == {expected_asset}
        checks.append(("/ index asset version", ok, f"expected={expected_asset} found={sorted(versions)}"))
    except (urllib.error.URLError, OSError) as exc:
        checks.append(("/ index asset version", False, f"request failed: {exc}"))

    all_ok = all(item[1] for item in checks)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="Sync asset versions and verify deployment")
    parser.add_argument("--version", default="", help="显式版本号（默认 git short sha 关联的现有版本）")
    parser.add_argument("--check-url", default="", help="部署后核对地址，例如 http://192.168.31.3:8000")
    parser.add_argument("--check-only", action="store_true", help="只核对，不改文件")
    args = parser.parse_args()

    version, build_id = resolve_version(args.version)
    asset_version = build_id or version

    if not args.check_only:
        index_changed, sw_changed, new_index, new_sw = update_assets(asset_version)
        if index_changed:
            INDEX_FILE.write_text(new_index, encoding="utf-8")
        if sw_changed:
            SW_FILE.write_text(new_sw, encoding="utf-8")
        VERSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        VERSION_FILE.write_text(json.dumps({
            "app_version": version,
            "build_id": build_id,
            "asset_version": asset_version,
            "released_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"version={version} build_id={build_id} asset_version={asset_version} "
              f"(index={'updated' if index_changed else 'unchanged'}, "
              f"sw={'updated' if sw_changed else 'unchanged'})")

    if args.check_url:
        if not check_deployment(args.check_url, version, asset_version):
            sys.exit(1)


if __name__ == "__main__":
    main()
