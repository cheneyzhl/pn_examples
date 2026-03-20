# -*- coding: utf-8 -*-
"""
读取 baseline_direct_coord 的输出 JSON，并把其中的 (pos, predicted_label) 统一成一个列表。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def load_baseline_rule_result(result_json_path: str) -> Dict[str, Any]:
    with open(result_json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def extract_samples_from_baseline_result(rule_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    返回 samples: [{idx, predicted_label, pos}, ...]，按 idx 升序。
    """
    details = rule_result.get("details", [])
    samples: List[Dict[str, Any]] = []
    for d in details:
        idx = d.get("idx")
        predicted_label = d.get("predicted_label")
        pos = d.get("pos")
        if pos is None:
            # 理论上 baseline 会把 pos 写入 details；这里做兜底
            pos = {}
        samples.append({"idx": idx, "predicted_label": predicted_label, "pos": pos})

    samples = [s for s in samples if s["idx"] is not None]
    samples.sort(key=lambda x: x["idx"])
    return samples


def extract_pos_and_labels_from_baseline_result(result_json_path: str) -> Tuple[List[Dict[str, Any]], List[bool]]:
    rule_result = load_baseline_rule_result(result_json_path)
    samples = extract_samples_from_baseline_result(rule_result)
    pos_list = [s["pos"] for s in samples]
    label_list = [bool(s["predicted_label"]) for s in samples]
    return pos_list, label_list

