#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 work/<规则名>/llm_response.json 重新做 normalize_example_coords，
回填 summary JSON 中 details[].pos 为空 {} 的项（兼容模型把单层写成单个 dict 的情况）。

用法:
  python patch_summary_pos_from_work.py summary_all_new_llm_only.json
  python patch_summary_pos_from_work.py summary_all_new_llm_only.json --work_dir work --dry_run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import normalize_example_coords


def _pos_is_empty(pos) -> bool:
    if pos is None:
        return True
    if isinstance(pos, dict) and len(pos) == 0:
        return True
    return False


def patch_summary(
    summary_path: str,
    work_dir: str,
    result_dir: Optional[str],
    dry_run: bool,
) -> Tuple[int, int, int]:
    """
    Returns: (rules_with_empty_pos, rules_patched, details_patched)
    """
    with open(summary_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    per_rule = data.get("per_rule", [])
    rules_with_empty = 0
    rules_patched = 0
    details_patched = 0

    for rule in per_rule:
        rn = rule.get("rule_name") or ""
        if not rn:
            continue
        details = rule.get("details") or []
        need = any(_pos_is_empty(d.get("pos")) for d in details if isinstance(d, dict))
        if not need:
            continue
        rules_with_empty += 1

        safe_name = rn.replace("/", "_")
        llm_path = os.path.join(work_dir, safe_name, "llm_response.json")
        if not os.path.isfile(llm_path):
            print(f"[skip] {rn}: 无文件 {llm_path}", file=sys.stderr)
            continue

        with open(llm_path, "r", encoding="utf-8") as f:
            llm = json.load(f)
        raw_examples = llm.get("examples") or []
        labels = llm.get("labels") or []
        normalized = [normalize_example_coords(ex) for ex in raw_examples if isinstance(ex, dict)]
        n = min(len(normalized), len(labels))
        normalized = normalized[:n]

        patched_this_rule = 0
        for d in details:
            if not isinstance(d, dict):
                continue
            if not _pos_is_empty(d.get("pos")):
                continue
            idx = d.get("idx")
            if idx is None or not isinstance(idx, int):
                continue
            if 0 <= idx < len(normalized):
                d["pos"] = normalized[idx]
                patched_this_rule += 1
                details_patched += 1

        if patched_this_rule:
            rules_patched += 1
            print(f"[ok] {rn}: 回填 {patched_this_rule} 条 pos", file=sys.stderr)
            if result_dir and not dry_run:
                rp = os.path.join(result_dir, f"{safe_name}.json")
                if os.path.isfile(rp):
                    try:
                        with open(rp, "r", encoding="utf-8") as rf:
                            one = json.load(rf)
                        one["details"] = rule["details"]
                        with open(rp, "w", encoding="utf-8") as wf:
                            json.dump(one, wf, ensure_ascii=False, indent=2)
                        print(f"[ok] 同步 result: {rp}", file=sys.stderr)
                    except Exception as e:
                        print(f"[warn] 写入 {rp} 失败: {e}", file=sys.stderr)

    if dry_run:
        print(f"[dry_run] 将写回: {summary_path}", file=sys.stderr)
        return rules_with_empty, rules_patched, details_patched

    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(
        f"已写回 {summary_path}；曾含空 pos 的规则数={rules_with_empty}，成功补全规则数={rules_patched}，detail 条数={details_patched}",
        file=sys.stderr,
    )
    return rules_with_empty, rules_patched, details_patched


def main():
    ap = argparse.ArgumentParser(description="从 work/*/llm_response.json 回填 summary 中空 pos")
    ap.add_argument("summary_json", help="summary_xxx.json 路径")
    ap.add_argument("--work_dir", default="work", help="工作目录（内含各规则子目录）")
    ap.add_argument(
        "--result_dir",
        default="result",
        help="若存在与规则同名的 json，同步回填 details（默认 result/，传空禁用）",
    )
    ap.add_argument("--dry_run", action="store_true", help="只统计不写文件")
    args = ap.parse_args()

    base = os.path.dirname(os.path.abspath(args.summary_json))
    work_dir = args.work_dir
    if not os.path.isabs(work_dir):
        work_dir = os.path.join(base, work_dir)

    result_dir = args.result_dir
    if result_dir and not os.path.isabs(result_dir):
        result_dir = os.path.join(base, result_dir)
    if result_dir == "":
        result_dir = None

    patch_summary(os.path.abspath(args.summary_json), work_dir, result_dir, args.dry_run)


if __name__ == "__main__":
    main()
