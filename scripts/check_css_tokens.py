"""CSS 设计令牌守门脚本（第一版）：

硬性规则（出现即失败）：
1. 不得再新增旧强调色 rgba(224, 120, 86, ...)（当前强调色是 #dfa47d）；
2. !important 只允许出现在 .hidden 与 prefers-reduced-motion 降级中；
3. @keyframes 不得重名。

基线规则（不得比基线更多）：
<style.css 中的裸十六进制色 / rgba / 非令牌 font-size / 非令牌 border-radius。
历史存量已冻结在 scripts/css_token_baseline.json；新增样式应引用 :root 令牌。
用 --update-baseline 在完成清理后更新基线。
"""

import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CSS_FILE = BASE_DIR / "static" / "css" / "style.css"
BASELINE_FILE = Path(__file__).resolve().parent / "css_token_baseline.json"

ROOT_BLOCK_RE = re.compile(r":root\s*\{[^}]*\}", re.DOTALL)
HEX_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGBA_RE = re.compile(r"rgba\(")
FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([^;]+);")
RADIUS_RE = re.compile(r"border-radius\s*:\s*([^;]+);")
KEYFRAMES_RE = re.compile(r"@keyframes\s+([A-Za-z0-9_-]+)")
IMPORTANT_RE = re.compile(r"!important")


def _outside_root(css_text):
    return ROOT_BLOCK_RE.sub("", css_text)


def _raw_value_count(pattern, css_text):
    count = 0
    for match in pattern.finditer(css_text):
        value = match.group(1).strip()
        if "var(" in value:
            continue
        count += 1
    return count


def collect_counts(css_text):
    body = _outside_root(css_text)
    keyframes = KEYFRAMES_RE.findall(css_text)
    duplicates = sorted({name for name in keyframes if keyframes.count(name) > 1})
    important_lines = [
        line.strip() for line in css_text.splitlines() if IMPORTANT_RE.search(line)
    ]
    allowed_important = 0
    if re.search(r"\.hidden\s*\{[^}]*!important", css_text):
        allowed_important += 1
    reduced = re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(.*)\}", css_text, re.DOTALL)
    if reduced:
        allowed_important += len(IMPORTANT_RE.findall(reduced.group(1)))
    return {
        "legacy_accent": len(re.findall(r"rgba\(\s*224\s*,\s*120\s*,\s*86", css_text)),
        "important_total": len(important_lines),
        "important_allowed": allowed_important,
        "duplicate_keyframes": duplicates,
        "raw_hex_outside_root": len(HEX_COLOR_RE.findall(body)),
        "raw_rgba_outside_root": len(RGBA_RE.findall(body)),
        "raw_font_size": _raw_value_count(FONT_SIZE_RE, body),
        "raw_border_radius": _raw_value_count(RADIUS_RE, body),
    }


def load_baseline():
    try:
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_baseline(counts):
    BASELINE_FILE.write_text(json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Check CSS design-token discipline")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    css_text = CSS_FILE.read_text(encoding="utf-8")
    counts = collect_counts(css_text)

    if args.update_baseline or load_baseline() is None:
        save_baseline(counts)
        print(f"baseline written: {BASELINE_FILE}")
        return 0

    baseline = load_baseline()
    failures = []
    if counts["legacy_accent"] > 0:
        failures.append(f"legacy accent color rgba(224,120,86,...) found {counts['legacy_accent']} times")
    if counts["important_total"] > counts["important_allowed"]:
        failures.append(
            f"!important outside allowed contexts: {counts['important_total']} > {counts['important_allowed']}"
        )
    if counts["duplicate_keyframes"]:
        failures.append(f"duplicate @keyframes: {', '.join(counts['duplicate_keyframes'])}")

    for key in ("raw_hex_outside_root", "raw_rgba_outside_root", "raw_font_size", "raw_border_radius"):
        if counts[key] > baseline.get(key, 0):
            failures.append(f"{key} grew: {counts[key]} > baseline {baseline.get(key, 0)}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print("提示：新增样式请引用 :root 设计令牌；确需更新基线用 --update-baseline")
        return 1

    print(
        "CSS tokens OK: legacy_accent=0, "
        f"important={counts['important_total']}/{counts['important_allowed']}, "
        f"raw_hex={counts['raw_hex_outside_root']}, raw_rgba={counts['raw_rgba_outside_root']}, "
        f"raw_font_size={counts['raw_font_size']}, raw_border_radius={counts['raw_border_radius']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
