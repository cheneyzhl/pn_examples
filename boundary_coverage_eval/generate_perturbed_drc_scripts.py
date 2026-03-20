# -*- coding: utf-8 -*-
"""
扰动 DRC script 生成流程：
从 pn_examples/new_datasets 读取每条规则的 script（忽略 constraints），
抽取其中的数值边界，并对每个边界做 +/- delta 扰动，生成 corner combinations。

输出：
  output_dir/<data_name>/<rule_name>/corner_<id>/script.txt
  output_dir/<data_name>/<rule_name>/corner_<id>/meta.json
  output_dir/<data_name>/<rule_name>/rule_meta.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# 让 `from lib...` 在任意 cwd 下都能工作
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from lib.script_perturbation import (
    BoundaryTarget,
    build_corners,
    build_corners_boolean,
    extract_boundary_targets,
)


def _load_new_dataset(data_name: str, new_datasets_dir: str) -> Dict[str, Any]:
    filename = None
    if data_name == "freePDK15":
        filename = "freePDK15_gpt_output.json"
    elif data_name == "asap7":
        filename = "asap7_gpt_output.json"
    elif data_name == "freepdk-45nm":
        filename = "freepdk-45nm_gpt_output.json"
    else:
        raise ValueError(f"Unknown data_name={data_name}")

    path = os.path.join(new_datasets_dir, filename)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_makedirs(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Generate perturbed DRC scripts (boundary corners).")
    parser.add_argument("--data_name", type=str, default="", choices=["", "freePDK15", "asap7", "freepdk-45nm"],
                        help="Single data_name; ignored when --all_datasets is set.")
    parser.add_argument("--all_datasets", action="store_true",
                        help="Process all three datasets (freePDK15, asap7, freepdk-45nm) into output_dir.")
    parser.add_argument("--new_datasets_dir", type=str, default="", help="Override pn_examples/new_datasets path")
    parser.add_argument("--output_dir", type=str, required=True,
                        help="Output folder. Structure: output_dir/<data_name>/<rule_name>/corner_*/")

    parser.add_argument("--delta", type=float, default=None, help="Absolute perturbation delta (optional).")
    parser.add_argument(
        "--decomposition_mode",
        type=str,
        choices=["boolean", "numeric"],
        default="boolean",
        help="boolean: 条件变量正/反布尔分解；numeric: 数值 +/- 扰动。",
    )
    parser.add_argument(
        "--max_targets",
        type=int,
        default=8,
        help="Limit number of extracted boundary targets per rule to avoid explosion (2^n corners).",
    )
    parser.add_argument("--max_rules", type=int, default=0, help="0 means all.")
    args = parser.parse_args()

    if not args.new_datasets_dir:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        pn_examples_dir = os.path.dirname(script_dir)
        new_datasets_dir = os.path.join(pn_examples_dir, "new_datasets")
    else:
        new_datasets_dir = args.new_datasets_dir

    if args.all_datasets:
        data_names = ["freePDK15", "asap7", "freepdk-45nm"]
    else:
        data_names = [args.data_name or "freePDK15"]

    total_rules = 0
    for data_name in data_names:
        try:
            new_dataset = _load_new_dataset(data_name, new_datasets_dir)
        except Exception as e:
            print(f"[WARN] Skip {data_name}: {e}")
            continue
        rule_items = list(new_dataset.items())
        if args.max_rules and args.max_rules > 0:
            rule_items = rule_items[: args.max_rules]

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        for rule_name, meta in rule_items:
            script_body = meta.get("script", "")
            if not script_body.strip():
                continue

            targets = extract_boundary_targets(script_body)
            targets = targets[: args.max_targets]
            if not targets:
                # 没有抽到可扰动边界，仍然产出一个 corner_000，方便下游流程不报错
                targets = []

            if args.decomposition_mode == "boolean":
                corners = build_corners_boolean(
                    script_body=script_body,
                    targets=targets,
                    max_corners=(1 << len(targets)) if len(targets) > 0 else 1,
                )
            else:
                corners = build_corners(
                    script_body=script_body,
                    targets=targets,
                    delta=args.delta,
                    max_corners=(1 << len(targets)) if len(targets) > 0 else 1,
                )
            if not corners:
                corners = [
                    {"corner_id": "corner_000", "corner_idx": 0, "bits": [], "targets": [], "script_body": script_body}
                ]

            # 统一规则：原本的脚本=正确，所有扰动脚本=错误
            # boolean: corner 全 bits=1 时等价于原 script -> correct；否则 perturbed -> incorrect
            # numeric: 有 targets 时 build_corners 全为扰动，需插入 corner_original；无 targets 时 fallback 已含原 script
            if args.decomposition_mode == "numeric" and targets and corners:
                original_corner = {
                    "corner_id": "corner_original",
                    "corner_idx": -1,
                    "bits": [],
                    "targets": [],
                    "script_body": script_body,
                    "script_expected_correct": True,
                }
                corners = [original_corner] + corners

            for c in corners:
                bits = c.get("bits", [])
                if "script_expected_correct" in c:
                    pass  # 已设定（如 corner_original）
                elif not bits:
                    c["script_expected_correct"] = True  # 无 targets 时 corner_000 为原 script
                elif args.decomposition_mode == "boolean":
                    c["script_expected_correct"] = all(b == 1 for b in bits)
                else:
                    c["script_expected_correct"] = False

            rule_out_dir = os.path.join(args.output_dir, data_name, rule_name)
            _safe_makedirs(rule_out_dir)

            # 保存 rule_meta
            rule_meta_path = os.path.join(rule_out_dir, "rule_meta.json")
            with open(rule_meta_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "rule_name": rule_name,
                        "data_name": data_name,
                        "extracted_targets_total": len(extract_boundary_targets(script_body)),
                        "used_targets": [
                            {
                                "kind": t.kind,
                                "var": t.var,
                                "op": t.op,
                                "num_str": t.num_str,
                                "decimals": t.decimals,
                                "line_idx": t.line_idx,
                                "num_start": t.num_start,
                                "num_end": t.num_end,
                            }
                            for t in targets
                        ],
                        "delta": args.delta,
                        "decomposition_mode": args.decomposition_mode,
                        "generated_corners_count": len(corners),
                        "generated_at": ts,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )

            for c in corners:
                corner_id = c["corner_id"]
                corner_dir = os.path.join(rule_out_dir, corner_id)
                _safe_makedirs(corner_dir)

                script_path = os.path.join(corner_dir, "script.txt")
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(c["script_body"].strip() + "\n")

                meta_path = os.path.join(corner_dir, "meta.json")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "corner_id": corner_id,
                            "corner_idx": c.get("corner_idx", None),
                            "bits": c.get("bits", []),
                            "targets_used": c.get("targets", []),
                            "script_expected_correct": c.get("script_expected_correct", False),
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
            total_rules += 1

    print(f"[OK] Generated perturbed scripts into: {args.output_dir} (total_rules={total_rules})")


if __name__ == "__main__":
    main()

