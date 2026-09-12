"""片流 API 性能基线工具：记录各端点 p50 / p95 / 均值 / 最大值。

用法：
    python scripts/bench_api.py --base-url http://127.0.0.1:8000 --iterations 20
    python scripts/bench_api.py --markdown docs/benchmarks/2026-09-11.md

说明：
- 先做 warmup 次请求，再计时 iterations 次；请求串行，排除并发干扰。
- 输出为 Markdown 表格；--json 可额外保存机器可读结果。
- 详情/推荐端点会自动从列表中取一个作品 id。
"""

import argparse
import json
import math
import time
from pathlib import Path

import httpx


def percentile(values, ratio):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(ratio * len(ordered)) - 1))
    return ordered[index]


def summarize(samples):
    return {
        "count": len(samples),
        "min": min(samples),
        "p50": percentile(samples, 0.50),
        "p95": percentile(samples, 0.95),
        "mean": sum(samples) / len(samples),
        "max": max(samples),
    }


def measure(client, url, iterations, warmup):
    for _ in range(warmup):
        client.get(url).raise_for_status()
    samples = []
    for _ in range(iterations):
        start = time.perf_counter()
        response = client.get(url)
        response.raise_for_status()
        samples.append((time.perf_counter() - start) * 1000)
    return summarize(samples)


def build_endpoints(base_url):
    client = httpx.Client(base_url=base_url, timeout=30)
    try:
        listing = client.get("/api/titles", params={"limit": 1}).json()
        title_id = listing["titles"][0]["id"] if listing.get("titles") else None
    finally:
        client.close()

    endpoints = [
        ("GET /health", "/health"),
        ("GET /api/titles（默认列表）", "/api/titles?limit=40"),
        ("GET /api/titles（评分排序）", "/api/titles?limit=40&sort_by=rating&order=desc"),
        ("GET /api/titles（搜索）", "/api/titles?limit=40&search=%E6%98%9F"),
        ("GET /api/titles（题材筛选）", "/api/titles?limit=40&genre=%E5%89%A7%E6%83%85"),
        ("GET /api/titles（分页第 5 页）", "/api/titles?limit=40&page=5"),
        ("GET /api/stats", "/api/stats"),
        ("GET /api/sync/status", "/api/sync/status"),
        ("GET /api/releases", "/api/releases?days=30&limit=60"),
    ]
    if title_id is not None:
        endpoints.append(("GET /api/titles/{id}（详情）", f"/api/titles/{title_id}"))
        endpoints.append(("GET /api/titles/{id}/related（推荐）", f"/api/titles/{title_id}/related?limit=12"))
    return endpoints


def render_markdown(base_url, iterations, results):
    lines = [
        "# 片流 API 性能基线",
        "",
        f"- 目标实例：`{base_url}`",
        f"- 每端点预热 + 计时：warmup 3 次，计时 {iterations} 次（串行）",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| 端点 | p50 (ms) | p95 (ms) | 均值 (ms) | 最小 (ms) | 最大 (ms) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, stats in results:
        lines.append(
            f"| {label} | {stats['p50']:.1f} | {stats['p95']:.1f} | {stats['mean']:.1f} "
            f"| {stats['min']:.1f} | {stats['max']:.1f} |"
        )
    lines += [
        "",
        "> 单机串行采样，用于回归对比；不代表公网或并发场景。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="片流 API 性能基线")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--markdown", help="输出 Markdown 报告路径")
    parser.add_argument("--json", help="输出 JSON 结果路径")
    args = parser.parse_args()

    endpoints = build_endpoints(args.base_url)
    results = []
    with httpx.Client(base_url=args.base_url, timeout=30) as client:
        for label, path in endpoints:
            stats = measure(client, path, args.iterations, args.warmup)
            results.append((label, stats))
            print(
                f"{label:<44} p50={stats['p50']:7.1f}ms  p95={stats['p95']:7.1f}ms  "
                f"mean={stats['mean']:7.1f}ms  max={stats['max']:7.1f}ms"
            )

    markdown = render_markdown(args.base_url, args.iterations, results)
    if args.markdown:
        path = Path(args.markdown)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(f"\nMarkdown 已写入 {path}")
    else:
        print("\n" + markdown)
    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"base_url": args.base_url, "iterations": args.iterations,
             "results": {label: stats for label, stats in results}},
            ensure_ascii=False, indent=2,
        ), encoding="utf-8")
        print(f"JSON 已写入 {path}")


if __name__ == "__main__":
    main()
