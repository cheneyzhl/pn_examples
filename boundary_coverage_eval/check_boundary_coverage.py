# -*- coding: utf-8 -*-
"""
边界覆盖检测流程：
1) 读取 baseline_direct_coord 的正反例（pos 与 predicted_label）
2) 读取扰动 script（corner_*）及其预期标签 script_expected_correct（正确/错误）
3) 对每个 corner 跑 Calibre，用正反例验证：若 calibre 结果与预期正反例标签一致，则推断该 script 正确，否则错误
4) 将推断的 script 正误与预期 script_expected_correct 比较，完全匹配则说明正反例覆盖所有边界，否则不够全面
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# 让 `from lib...` 在任意 cwd 下都能工作
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from lib.baseline_output_loader import extract_samples_from_baseline_result
from lib.generate_gds import call_calibre_drc, edit_drc_file, edit_script_path, generate_layout, read_drc_report
from lib.read_drc_file import read_layer_info
from lib.rul_patch import patch_rule_script_body
from lib.script_perturbation import extract_boundary_targets


def _safe_listdir(path: str) -> List[str]:
    if not os.path.isdir(path):
        return []
    return sorted(os.listdir(path))


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_base_script_and_layer_dict(base_rul_path: str, rule_name: str, data_name: str, work_dir: str) -> Tuple[str, Dict[str, int]]:
    os.makedirs(work_dir, exist_ok=True)
    layer_json_path = os.path.join(work_dir, "layers.json")
    layer_dict = read_layer_info(base_rul_path, layer_json_path)
    base_script_path = os.path.join(work_dir, f"{rule_name}.rul")
    edit_drc_file(layer_dict, base_rul_path, base_script_path, rule_name, data_name)
    return base_script_path, layer_dict


def _read_corner_script_body(corner_script_dir: str) -> str:
    script_path = os.path.join(corner_script_dir, "script.txt")
    with open(script_path, "r", encoding="utf-8") as f:
        return f.read()


def _get_corner_ids(rule_generated_dir: str) -> List[str]:
    # corner_XXX/
    ids = []
    for name in _safe_listdir(rule_generated_dir):
        if name.startswith("corner_") and os.path.isdir(os.path.join(rule_generated_dir, name)):
            ids.append(name)
    return ids


def _load_corner_script_expected_correct(corner_script_dir: str) -> Optional[bool]:
    """从 corner meta.json 读取 script_expected_correct（脚本预期正误）。旧格式无此字段时返回 None。"""
    meta_path = os.path.join(corner_script_dir, "meta.json")
    if not os.path.isfile(meta_path):
        return None
    try:
        meta = _load_json(meta_path)
        if "script_expected_correct" in meta:
            return bool(meta["script_expected_correct"])
    except Exception:
        pass
    return None


def _load_rule_metadata(new_datasets_dir: str, data_name: str, rule_name: str) -> Tuple[str, str]:
    """从 new_datasets JSON 读取 rule 描述与 script，返回 (rule_description, rule_script)。"""
    filename_map = {
        "freePDK15": "freePDK15_gpt_output.json",
        "asap7": "asap7_gpt_output.json",
        "freepdk-45nm": "freepdk-45nm_gpt_output.json",
    }
    path = os.path.join(new_datasets_dir, filename_map.get(data_name, ""))
    if not os.path.isfile(path):
        return "", ""
    try:
        data = _load_json(path)
        entry = data.get(rule_name, {})
        if isinstance(entry, dict):
            return str(entry.get("rule", "")), str(entry.get("script", ""))
    except Exception:
        pass
    return "", ""


def _calibre_label_from_report(drv_char: Optional[str]) -> bool:
    # baseline 约定：drv_char == "0" 表示无违规（通过）
    if drv_char is None:
        return False
    return str(drv_char).strip() == "0"


def _enrich_eval_json(
    eval_data: Dict[str, Any],
    rule_name: str,
    data_name: str,
    baseline_rule_result: Any,
    rule_generated_scripts_dir: str,
    new_datasets_dir: str,
) -> Dict[str, Any]:
    """
    为已有 eval.json 补全 rule_description、rule_script、perturbed_scripts、samples 中的 pos。
    用于 skip_existing 时加载的旧格式结果，确保输出结构完整。
    """
    out = dict(eval_data)
    changed = False

    if not out.get("rule_description") or not out.get("rule_script"):
        desc, script = _load_rule_metadata(new_datasets_dir, data_name, rule_name)
        if desc or script:
            out["rule_description"] = desc
            out["rule_script"] = script
            changed = True

    if not out.get("perturbed_scripts") or not isinstance(out["perturbed_scripts"], dict):
        perturbed: Dict[str, str] = {}
        for cid in _get_corner_ids(rule_generated_scripts_dir):
            try:
                perturbed[cid] = _read_corner_script_body(os.path.join(rule_generated_scripts_dir, cid))
            except Exception:
                pass
        if perturbed:
            out["perturbed_scripts"] = perturbed
            changed = True

    # 补全 samples 中的 pos
    if isinstance(baseline_rule_result, str):
        baseline_rule_result = _load_json(baseline_rule_result)
    samples_baseline = extract_samples_from_baseline_result(baseline_rule_result)
    pos_by_idx = {s["idx"]: s["pos"] for s in samples_baseline if s.get("idx") is not None}

    need_pos = False
    for pc in out.get("per_corner", []):
        for s in pc.get("samples", []):
            if "pos" not in s and s.get("idx") is not None and pos_by_idx.get(s["idx"]) is not None:
                need_pos = True
                break
        if need_pos:
            break
    if need_pos and pos_by_idx:
        for pc in out.get("per_corner", []):
            for s in pc.get("samples", []):
                idx = s.get("idx")
                if idx is not None and "pos" not in s:
                    s["pos"] = pos_by_idx.get(idx)
        changed = True

    return out


def _ensure_full_eval_format(
    eval_data: Dict[str, Any],
    rule_name: str,
    data_name: str,
    baseline_rule_result: Any,
    rule_generated_scripts_dir: str,
    new_datasets_dir: str,
) -> Dict[str, Any]:
    """确保 eval 含 rule_description、rule_script、perturbed_scripts、samples.pos，供 skip_existing 时补全。"""
    return _enrich_eval_json(eval_data, rule_name, data_name, baseline_rule_result, rule_generated_scripts_dir, new_datasets_dir)


def run_detection_for_rule(
    rule_name: str,
    data_name: str,
    base_rul_path: str,
    baseline_rule_result: Any,
    rule_generated_scripts_dir: str,
    output_rule_dir: str,
    judge_mode: str,
    new_datasets_dir: str = "",
    drc_report_name: str = "drc_report",
    skip_existing: bool = False,
) -> Dict[str, Any]:
    """
    仅支持从 baseline summary/result 导入正反例的 pos 与 predicted_label，在本项目内仿 baseline_direct_coord
    生成 GDS 与 .rul，并调用 Calibre。不导入 baseline work 目录下的 GDS/RUL。
    baseline_rule_result: 可为 JSON 文件路径 (str) 或已加载的 dict（如 summary per_rule 项）
    """
    output_rule_dir = os.path.abspath(output_rule_dir)
    os.makedirs(output_rule_dir, exist_ok=True)

    if not new_datasets_dir:
        from config import NEW_DATASETS_DIR as _nd
        new_datasets_dir = _nd
    _nd = new_datasets_dir or (__import__("config", fromlist=["NEW_DATASETS_DIR"]).NEW_DATASETS_DIR if new_datasets_dir == "" else "")
    if _nd == "":
        try:
            from config import NEW_DATASETS_DIR
            _nd = NEW_DATASETS_DIR
        except ImportError:
            pass
    rule_description, rule_script = _load_rule_metadata(_nd or ".", data_name, rule_name)
    perturbed_scripts: Dict[str, str] = {}

    corner_ids = _get_corner_ids(rule_generated_scripts_dir)
    if not corner_ids:
        raise ValueError(f"No corner_* dirs found for rule={rule_name} under {rule_generated_scripts_dir}")

    # 读取 baseline 样本与标签（仅支持从输入获取 pos 与 predicted_label，不读取 baseline work 目录）
    if isinstance(baseline_rule_result, str):
        baseline_rule_result = _load_json(baseline_rule_result)
    samples = extract_samples_from_baseline_result(baseline_rule_result)
    if not samples:
        raise ValueError(f"Baseline result has no samples for rule={rule_name}")

    predicted_labels_by_idx = {s["idx"]: bool(s["predicted_label"]) for s in samples}
    pos_by_idx = {s["idx"]: s["pos"] for s in samples}
    idx_list = [s["idx"] for s in samples]
    missing_pos = [i for i in idx_list if not pos_by_idx.get(i)]
    if missing_pos:
        raise ValueError(
            f"Rule {rule_name} has samples with missing pos at idx {missing_pos}. "
            "Only coordinates (pos) and labels from baseline summary/result are supported."
        )

    # 构造 GDS layer_dict + base .rul 模板（仿 baseline_direct_coord）
    output_rule_dir_abs = os.path.abspath(output_rule_dir)
    rule_work_dir = os.path.abspath(os.path.join(os.path.dirname(output_rule_dir_abs), "work", data_name, rule_name))
    base_template_rul_path, layer_dict = _build_base_script_and_layer_dict(
        base_rul_path=base_rul_path,
        rule_name=rule_name,
        data_name=data_name,
        work_dir=rule_work_dir,
    )

    # 在本项目内生成 GDS（仿 baseline_direct_coord 的 generate_layout）
    gds_dir = os.path.join(rule_work_dir, "gds")
    os.makedirs(gds_dir, exist_ok=True)
    gds_paths: Dict[int, str] = {}
    for idx in idx_list:
        gds_path = os.path.join(gds_dir, f"example_{idx}.gds")
        gds_paths[idx] = os.path.abspath(gds_path)
        if skip_existing and os.path.isfile(gds_path):
            continue
        generate_layout(pos_by_idx[idx], layer_dict, gds_path)

    per_corner: List[Dict[str, Any]] = []

    # 对每个 corner：
    # 1) patch base_rule_template -> corner_rule_template.rul
    # 2) 对每个样本：edit_script_path 设定 LAYOUT PATH -> example_{idx}.rul，然后跑 calibre，读 drc_report，和 predicted_label 比较
    for corner_id in corner_ids:
        corner_script_dir = os.path.join(rule_generated_scripts_dir, corner_id)
        corner_dir = os.path.join(rule_work_dir, corner_id)
        corner_dir_abs = os.path.abspath(os.path.normpath(corner_dir))
        os.makedirs(corner_dir_abs, exist_ok=True)

        corner_template_rul_path = os.path.join(corner_dir_abs, f"{corner_id}.rul")
        corner_template_rul_abs = os.path.abspath(os.path.normpath(corner_template_rul_path))
        corner_script_body = _read_corner_script_body(corner_script_dir)
        perturbed_scripts[corner_id] = corner_script_body
        if not (skip_existing and os.path.isfile(corner_template_rul_abs)):
            patch_rule_script_body(
                base_rule_template_path=base_template_rul_path,
                rule_name=rule_name,
                new_script_body=corner_script_body,
                output_path=corner_template_rul_abs,
            )
        if not os.path.isfile(corner_template_rul_abs):
            raise FileNotFoundError(
                f"corner template not created: {corner_template_rul_abs} (rule={rule_name} corner={corner_id})"
            )

        corner_sample_results: List[Dict[str, Any]] = []
        corner_any_mismatch = False
        corner_all_match = True

        orig_cwd = os.getcwd()
        try:
            # calibre 报告写入当前目录
            os.chdir(corner_dir_abs)
            for idx in idx_list:
                gds_abs_path = os.path.abspath(gds_paths[idx])
                example_rul_path = os.path.join(corner_dir_abs, f"example_{idx}.rul")
                edit_script_path(corner_template_rul_abs, gds_abs_path, example_rul_path)

                drc_report_path = os.path.join(corner_dir_abs, drc_report_name)
                if os.path.exists(drc_report_path):
                    try:
                        os.remove(drc_report_path)
                    except OSError:
                        pass

                predicted_label = predicted_labels_by_idx[idx]
                calibre_label: Optional[bool] = None
                match = False
                error: Optional[str] = None

                try:
                    call_calibre_drc(os.path.abspath(example_rul_path))
                    drv_char = read_drc_report(drc_report_path)
                    calibre_label = _calibre_label_from_report(drv_char)
                    match = (calibre_label == predicted_label)
                except Exception as e:
                    error = str(e)
                    calibre_label = None
                    match = False

                if not match:
                    corner_any_mismatch = True
                    corner_all_match = False
                corner_sample_results.append(
                    {
                        "idx": idx,
                        "predicted_label": predicted_label,
                        "calibre_label": calibre_label,
                        "match": match,
                        "error": error,
                        "pos": pos_by_idx.get(idx),
                    }
                )
        finally:
            os.chdir(orig_cwd)

        # 新逻辑：script_predicted_correct = 正反例验证结果与预期一致（calibre 与 predicted_label 全匹配）
        # corner_ok = 推断的 script 正误与预期 script_expected_correct 一致；无预期时跳过
        script_predicted_correct = corner_all_match
        script_expected_correct = _load_corner_script_expected_correct(corner_script_dir)
        if script_expected_correct is not None:
            corner_ok = (script_predicted_correct == script_expected_correct)
        else:
            corner_ok = None  # 旧 meta 无 script_expected_correct，无法评估

        per_corner.append(
            {
                "corner_id": corner_id,
                "script_expected_correct": script_expected_correct,
                "script_predicted_correct": script_predicted_correct,
                "corner_any_mismatch": corner_any_mismatch,
                "corner_all_match": corner_all_match,
                "corner_ok": corner_ok,
                "samples": corner_sample_results,
            }
        )

    # 以规则为单位：仅对已知 script_expected_correct 的 corner 做 coverage 判定
    corners_with_expected = [c for c in per_corner if c.get("script_expected_correct") is not None]
    all_corners_ok = all(c["corner_ok"] for c in corners_with_expected) if corners_with_expected else False
    all_corners_match = all(c["corner_all_match"] for c in per_corner)
    all_corners_detect = all(c["corner_any_mismatch"] for c in per_corner)

    # 额外给一个可追溯字段：解析 corner script 中抽取到多少个目标
    # （注意：生成流程可能截断了 targets，这里只做观察，不参与 judge）
    corner_target_hint = None
    try:
        first_corner_script = _read_corner_script_body(os.path.join(rule_generated_scripts_dir, corner_ids[0]))
        corner_target_hint = len(extract_boundary_targets(first_corner_script))
    except Exception:
        corner_target_hint = None

    return {
        "rule_name": rule_name,
        "rule_description": rule_description,
        "rule_script": rule_script,
        "perturbed_scripts": perturbed_scripts,
        "data_name": data_name,
        "judge_mode": judge_mode,
        "corner_count": len(corner_ids),
        "sample_count": len(idx_list),
        "all_corners_match": bool(all_corners_match),
        "all_corners_detect": bool(all_corners_detect),
        "all_corners_ok": bool(all_corners_ok),
        "per_corner": per_corner,
    }


def _build_rule_to_data_name_map(new_datasets_dir: str) -> Dict[str, str]:
    """从 new_datasets 三个 JSON 构建 rule_name -> data_name 映射"""
    mapping: Dict[str, str] = {}
    for data_name, filename in [
        ("freePDK15", "freePDK15_gpt_output.json"),
        ("asap7", "asap7_gpt_output.json"),
        ("freepdk-45nm", "freepdk-45nm_gpt_output.json"),
    ]:
        path = os.path.join(new_datasets_dir, filename)
        if os.path.isfile(path):
            try:
                data = _load_json(path)
                for rn in data.keys():
                    mapping[rn] = data_name
            except Exception:
                pass
    return mapping


def main():
    parser = argparse.ArgumentParser(description="Check boundary coverage using baseline pos/labels and perturbed DRC scripts.")
    parser.add_argument("--data_name", type=str, choices=["freePDK15", "asap7", "freepdk-45nm"], default="",
                        help="Single data_name; 当使用 --baseline_summary 时可选（从 summary 推断各规则 data_name）")
    parser.add_argument("--baseline_result_dir", type=str, default="", help="baseline_direct_coord/result/*.json")
    parser.add_argument("--baseline_summary", type=str, default="",
                        help="baseline 汇总 JSON，如 summary_1_1_1.json；与 run_baseline.py --output 输出格式兼容")
    parser.add_argument("--generated_scripts_dir", type=str, required=True, help="generate_perturbed_drc_scripts.py output dir")
    parser.add_argument("--output_dir", type=str, required=True, help="Output directory")
    parser.add_argument("--judge_mode", type=str, choices=["detect", "match"], default="match")
    parser.add_argument("--drc_report_name", type=str, default="drc_report")
    parser.add_argument("--max_rules", type=int, default=0, help="0 means all")
    parser.add_argument("--skip_existing", action="store_true", help="Skip if outputs exist.")
    args = parser.parse_args()

    from config import BASELINE_RESULT_DIR, BASE_RUL_PATHS, NEW_DATASETS_DIR

    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)

    results: List[Dict[str, Any]] = []
    correct_by_judge = 0
    match_passed = 0
    detect_passed = 0

    if args.baseline_summary and os.path.isfile(args.baseline_summary):
        summary_data = _load_json(args.baseline_summary)
        per_rule_list = summary_data.get("per_rule", [])
        rule_to_data = _build_rule_to_data_name_map(NEW_DATASETS_DIR)

        rule_tasks: List[Tuple[str, str, Dict[str, Any]]] = []
        for pr in per_rule_list:
            rule_name = pr.get("rule_name", "")
            if not rule_name:
                continue
            data_name = rule_to_data.get(rule_name)
            if not data_name:
                continue
            gen_rule_dir = os.path.join(args.generated_scripts_dir, data_name, rule_name)
            if not os.path.isdir(gen_rule_dir) or not os.path.isfile(os.path.join(gen_rule_dir, "rule_meta.json")):
                continue
            rule_tasks.append((rule_name, data_name, pr))

        if args.max_rules > 0:
            rule_tasks = rule_tasks[: args.max_rules]

        if not rule_tasks:
            print("[WARN] 无待评估规则。请检查：1) summary per_rule 与 rule_to_data 映射是否一致；"
                  "2) perturbed_datasets/<data_name>/<rule_name>/rule_meta.json 是否存在。")
        else:
            print(f"[INFO] 待评估规则数: {len(rule_tasks)}")

        for i, (rule_name, data_name, baseline_rule_result) in enumerate(rule_tasks, 1):
            base_rul_path = BASE_RUL_PATHS[data_name]
            rule_generated_scripts_dir = os.path.join(args.generated_scripts_dir, data_name, rule_name)
            out_rule_dir = os.path.join(output_dir, "result", rule_name)
            out_rule_json = os.path.join(out_rule_dir, "eval.json")
            skip = args.skip_existing and os.path.isfile(out_rule_json)
            print(f"[{'skip' if skip else 'run'}] ({i}/{len(rule_tasks)}) {rule_name}")
            if skip:
                eval_json = _load_json(out_rule_json)
                eval_json = _enrich_eval_json(
                    eval_json, rule_name, data_name, baseline_rule_result,
                    rule_generated_scripts_dir, NEW_DATASETS_DIR,
                )
                os.makedirs(out_rule_dir, exist_ok=True)
                with open(out_rule_json, "w", encoding="utf-8") as f:
                    json.dump(eval_json, f, ensure_ascii=False, indent=2)
                results.append(eval_json)
            if not skip:
                res = run_detection_for_rule(
                    rule_name=rule_name,
                    data_name=data_name,
                    base_rul_path=base_rul_path,
                    baseline_rule_result=baseline_rule_result,
                    rule_generated_scripts_dir=rule_generated_scripts_dir,
                    output_rule_dir=out_rule_dir,
                    judge_mode=args.judge_mode,
                    new_datasets_dir=NEW_DATASETS_DIR,
                    drc_report_name=args.drc_report_name,
                    skip_existing=args.skip_existing,
                )
                os.makedirs(out_rule_dir, exist_ok=True)
                with open(out_rule_json, "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=2)
                results.append(res)

            if results:
                last = results[-1]
                if last.get("all_corners_ok"):
                    correct_by_judge += 1
                if last.get("all_corners_match"):
                    match_passed += 1
                if last.get("all_corners_detect"):
                    detect_passed += 1

    else:
        data_name = args.data_name or "freePDK15"
        baseline_result_dir = args.baseline_result_dir or BASELINE_RESULT_DIR
        base_rul_path = BASE_RUL_PATHS[data_name]
        generated_data_dir = os.path.join(args.generated_scripts_dir, data_name)
        if not os.path.isdir(generated_data_dir):
            raise ValueError(f"generated_scripts_dir data_name dir not found: {generated_data_dir}")

        rule_names = []
        for name in _safe_listdir(generated_data_dir):
            rule_dir = os.path.join(generated_data_dir, name)
            if os.path.isdir(rule_dir) and os.path.isfile(os.path.join(rule_dir, "rule_meta.json")):
                rule_names.append(name)
        rule_names.sort()
        if args.max_rules > 0:
            rule_names = rule_names[: args.max_rules]

        n_with_baseline = sum(1 for rn in rule_names if os.path.isfile(os.path.join(baseline_result_dir, f"{rn}.json")))
        if n_with_baseline > 0:
            print(f"[INFO] 待评估规则数: {n_with_baseline}")

        for rule_name in rule_names:
            baseline_path = os.path.join(baseline_result_dir, f"{rule_name}.json")
            if not os.path.isfile(baseline_path):
                continue

            rule_generated_scripts_dir = os.path.join(args.generated_scripts_dir, data_name, rule_name)
            out_rule_dir = os.path.join(output_dir, "result", rule_name)
            out_rule_json = os.path.join(out_rule_dir, "eval.json")
            skip = args.skip_existing and os.path.isfile(out_rule_json)
            print(f"[{'skip' if skip else 'run'}] {rule_name}")
            if skip:
                eval_json = _load_json(out_rule_json)
                eval_json = _enrich_eval_json(
                    eval_json, rule_name, data_name, baseline_path,
                    rule_generated_scripts_dir, NEW_DATASETS_DIR,
                )
                os.makedirs(out_rule_dir, exist_ok=True)
                with open(out_rule_json, "w", encoding="utf-8") as f:
                    json.dump(eval_json, f, ensure_ascii=False, indent=2)
                results.append(eval_json)
            else:
                res = run_detection_for_rule(
                    rule_name=rule_name,
                    data_name=data_name,
                    base_rul_path=base_rul_path,
                    baseline_rule_result=baseline_path,
                    rule_generated_scripts_dir=rule_generated_scripts_dir,
                    output_rule_dir=out_rule_dir,
                    judge_mode=args.judge_mode,
                    new_datasets_dir=NEW_DATASETS_DIR,
                    drc_report_name=args.drc_report_name,
                    skip_existing=args.skip_existing,
                )
                os.makedirs(out_rule_dir, exist_ok=True)
                with open(out_rule_json, "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=2)
                results.append(res)

            if results:
                last = results[-1]
                if last.get("all_corners_ok"):
                    correct_by_judge += 1
                if last.get("all_corners_match"):
                    match_passed += 1
                if last.get("all_corners_detect"):
                    detect_passed += 1

    total = len(results)
    accuracy = (correct_by_judge / total) if total > 0 else 0.0
    accuracy_match = (match_passed / total) if total > 0 else 0.0
    accuracy_detect = (detect_passed / total) if total > 0 else 0.0

    summary = {
        "data_name": args.data_name,
        "judge_mode": args.judge_mode,
        "total_rules_evaluated": total,
        "passed_rules_by_judge_mode": correct_by_judge,
        "boundary_coverage_accuracy": accuracy,
        "boundary_coverage_accuracy_match": accuracy_match,
        "boundary_coverage_accuracy_detect": accuracy_detect,
        "details": results,
    }

    with open(os.path.join(output_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"[OK] Completed. accuracy={accuracy} ({correct_by_judge}/{total})")


if __name__ == "__main__":
    main()
