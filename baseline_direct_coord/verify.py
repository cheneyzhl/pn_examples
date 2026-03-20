# -*- coding: utf-8 -*-
"""
用 Calibre 执行 DRC 脚本，检查大模型给出的正/反例标签是否与 Calibre 检查结果一致。
使用本仓库 lib 内复制的 generate_gds / read_drc_file，项目可独立运行。
"""
import os
import logging
from typing import List, Dict, Any, Tuple, Optional

from lib import (
    generate_layout,
    edit_script_path,
    edit_drc_file,
    call_calibre_drc,
    read_drc_report,
    read_layer_info,
)

logger = logging.getLogger(__name__)


def verify_examples(
    rule_name: str,
    examples: List[Dict[str, Any]],
    labels: List[bool],
    layer_dict: Dict[str, int],
    base_script_path: str,
    work_dir: str,
    drc_report_name: str = "drc_report",
) -> Tuple[int, int, List[Dict[str, Any]]]:
    """
    对每组 (example, label) 生成 GDS、运行 Calibre、读取 drc_report，判断预测标签是否与 Calibre 结果一致。

    Args:
        rule_name: 规则名，仅用于日志。
        examples: 与 llm_drc 格式一致，每项为 { "LAYER_1": [ {llx,lly,urx,ury}, ... ], ... }。
        labels: 与 examples 等长，True=正例（应通过 DRC），False=反例（应违规）。
        layer_dict: 层名 -> GDS layer number，与 generate_layout 所需一致。
        base_script_path: 已提取出的单条规则 .rul 路径（内含 LAYOUT PATH 占位）。
        work_dir: 工作目录，GDS 与每例的 .rul 写在此目录，Calibre 在此目录运行（drc_report 写在此）。
        drc_report_name: Calibre 输出的 summary 文件名，默认 "drc_report"。

    Returns:
        (correct_count, total_count, details):
          - correct_count: 标签与 Calibre 结果一致的数量。
          - total_count: 参与验证的数量。
          - details: 每条的详情列表，每项 {"idx", "predicted_label", "calibre_label", "match", "error"?}。
    """
    os.makedirs(work_dir, exist_ok=True)
    drc_report_path = os.path.join(work_dir, drc_report_name)
    details = []
    correct = 0
    total = 0
    orig_cwd = os.getcwd()

    for i, (one_example, pred_label) in enumerate(zip(examples, labels)):
        total += 1
        gds_path = os.path.join(work_dir, f"example_{i}.gds")
        script_path = os.path.join(work_dir, f"example_{i}.rul")
        gds_path_abs = os.path.abspath(gds_path)
        script_path_abs = os.path.abspath(script_path)

        try:
            generate_layout(one_example, layer_dict, gds_path)
            edit_script_path(base_script_path, gds_path_abs, script_path)

            if os.path.exists(drc_report_path):
                try:
                    os.remove(drc_report_path)
                except OSError:
                    pass
            try:
                os.chdir(work_dir)
                call_calibre_drc(script_path_abs)
            finally:
                os.chdir(orig_cwd)

            if not os.path.exists(drc_report_path):
                details.append({
                    "idx": i,
                    "predicted_label": pred_label,
                    "calibre_label": None,
                    "match": False,
                    "error": "drc_report not found after Calibre run",
                })
                continue
            drv_char = read_drc_report(drc_report_path)
            # 与 llm_drc 一致：0 表示无违规（通过），非 0 表示有违规
            calibre_label = (drv_char == "0") if drv_char else False
            match = bool(calibre_label == pred_label)
            if match:
                correct += 1
            details.append({
                "idx": i,
                "predicted_label": pred_label,
                "calibre_label": calibre_label,
                "match": match,
            })
        except Exception as e:
            try:
                os.chdir(orig_cwd)
            except OSError:
                pass
            logger.exception("验证第 %d 例时出错: %s", i, e)
            details.append({
                "idx": i,
                "predicted_label": pred_label,
                "calibre_label": None,
                "match": False,
                "error": str(e),
            })

    return correct, total, details


def build_base_script_and_layer_dict(
    base_rul_path: str,
    rule_name: str,
    data_name: str,
    work_dir: str,
    layer_json_path: Optional[str] = None,
) -> Tuple[str, Dict[str, int]]:
    """
    从全量 base_rul 中提取单条规则脚本，并读取 layer 信息。

    Args:
        base_rul_path: 全量 calibreDRC.rul 路径。
        rule_name: 规则名（如 RULE_NW001）。
        data_name: freePDK15 / asap7 / freepdk-45nm。
        work_dir: 输出目录，base 脚本写在此目录。
        layer_json_path: read_layer_info 输出的 layer json 路径，可选。

    Returns:
        (base_script_path, layer_dict)。
    """
    os.makedirs(work_dir, exist_ok=True)
    if layer_json_path is None:
        layer_json_path = os.path.join(work_dir, "layers.json")
    layer_dict = read_layer_info(base_rul_path, layer_json_path)
    base_script_path = os.path.join(work_dir, f"{rule_name}.rul")
    edit_drc_file(layer_dict, base_rul_path, base_script_path, rule_name, data_name)
    return base_script_path, layer_dict
