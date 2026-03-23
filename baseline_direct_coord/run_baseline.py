# -*- coding: utf-8 -*-
"""
Baseline 实验主入口：大模型直接生成矩形坐标，再用 Calibre 验证正/反例标签是否一致，统计正确率。
"""
import os
import sys
import json
import argparse
import logging
from datetime import datetime
from typing import List, Dict, Any

# 保证可导入同目录与 config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
from agent import ask_llm_for_examples, normalize_example_coords
from verify import verify_examples, build_base_script_and_layer_dict

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


NEW_DATASETS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "new_datasets")

_NEW_DATA_CACHE = {}


def load_new_rule_info(data_name: str) -> Dict[str, Dict[str, Any]]:
    """
    从 new_datasets 中加载规则的描述 / 脚本信息（不使用 constraints）。
    data_name: freePDK15 / asap7 / freepdk-45nm
    """
    if data_name in _NEW_DATA_CACHE:
        return _NEW_DATA_CACHE[data_name]

    filename = None
    if data_name == "freePDK15":
        filename = "freePDK15_gpt_output.json"
    elif data_name == "asap7":
        filename = "asap7_gpt_output.json"
    elif data_name == "freepdk-45nm":
        filename = "freepdk-45nm_gpt_output.json"

    info = {}
    if filename:
        path = os.path.join(NEW_DATASETS_DIR, filename)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except Exception as e:
                logger.warning("加载 new_datasets %s 失败: %s", path, e)

    _NEW_DATA_CACHE[data_name] = info
    return info


def load_rules(rules_path: str) -> List[Dict[str, Any]]:
    """
    加载待测规则列表。每项需包含 rule_name 与 rule 描述（自然语言）。
    若文件为 { "RULE_XXX": { "rule": "...", "layers": [...] }, ... } 则转为列表。
    """
    if not os.path.isfile(rules_path):
        return []
    with open(rules_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    rules_list = []
    for rule_name, info in data.items():
        if isinstance(info, dict):
            rules_list.append({
                "rule_name": rule_name,
                "rule": info.get("rule", ""),
                "layers": info.get("layers", []),
                "process": info.get("process"),
            })
        else:
            rules_list.append({"rule_name": rule_name, "rule": str(info), "layers": [], "process": None})
    return rules_list


def extract_rule_script_from_rul(content: str, rule_name: str) -> str:
    """
    从 .rul 文件内容中解析出该规则的 DRC 脚本部分。
    规则块格式：RULE_XXX{ 后接若干行，第一个 @ 为规则名，第二个 @ 为描述，其余非 @ 行为 DRC 脚本。
    """
    lines = content.splitlines()
    in_block = False
    script_lines = []
    for line in lines:
        stripped = line.strip()
        # freePDK15: 行形如 "RULE_NW001{"
        # asap7: 行形如 "WELL.W.1 {"
        if rule_name:
            if stripped.startswith(rule_name):
                in_block = True
                continue
            if stripped.startswith("RULE_") and rule_name.strip() in stripped:
                in_block = True
                continue
        if in_block:
            if stripped == "}":
                break
            if stripped.startswith("@"):
                continue
            if stripped:
                script_lines.append(stripped)
    return "\n".join(script_lines)


def run_baseline(
    base_rul_path: str,
    data_name: str,
    work_dir: str,
    api_key: str,
    rules: List[Dict[str, Any]],
    log_dir: str = "",
    result_dir: str = "",
    skip_calibre: bool = False,
) -> Dict[str, Any]:
    """
    对每条规则：调用 Agent 获取正/反例坐标与标签，再用 Calibre 验证并统计正确率。
    skip_calibre 为 True 时仅保留大模型生成与日志/结果 JSON，不调用 Calibre；不计算 accuracy。

    log_dir: 若非空，先在该目录下按时间戳创建子目录，再在该子目录下以规则名记录该规则的对话日志
             （用户问题 + 大模型回答），txt 格式，便于多线程并行时区分不同规则。
    result_dir: 若非空，在该目录下以规则名写入每条规则的结果 JSON（包含 rule_name/description/script、
                统计信息以及每个样本的 pos），同样便于并行场景按规则汇总。
    每条规则的 work 目录下会写入 llm_response.json，记录大模型原始回答与解析后的坐标。

    Returns:
        汇总结果：总正确数、总例数、正确率、每条规则的明细。
    """
    rule_count = 0
    rule_correct_count = 0
    per_rule_results = []

    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        logger.info("对话日志目录: %s", log_dir)

    if result_dir:
        os.makedirs(result_dir, exist_ok=True)

    for item in rules:
        rule_name = item.get("rule_name", "")
        rule_desc = item.get("rule", "")
        layer_list = item.get("layers", [])
        if not rule_name or not rule_desc:
            logger.warning("跳过缺少 rule_name 或 rule 的项: %s", item)
            continue

        rule_work_dir = os.path.join(work_dir, rule_name.replace("/", "_"))
        os.makedirs(rule_work_dir, exist_ok=True)

        # 1) 提取该规则的 base 脚本与 layer 信息
        try:
            base_script_path, layer_dict = build_base_script_and_layer_dict(
                base_rul_path, rule_name, data_name, rule_work_dir
            )
        except Exception as e:
            logger.exception("规则 %s 提取 base 脚本失败: %s", rule_name, e)
            rule_count += 1
            rule_result_error = {
                "rule_name": rule_name,
                "rule_description": rule_desc,
                "rule_script": "",
                "error": str(e),
                "sample_correct": 0,
                "sample_total": 0,
                "accuracy": 0.0,
            }
            per_rule_results.append(rule_result_error)
            if result_dir:
                try:
                    out_path = os.path.join(result_dir, f"{rule_name}.json")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(rule_result_error, f, ensure_ascii=False, indent=2)
                    logger.info("规则 %s 结果已写入 %s", rule_name, out_path)
                except Exception as e2:
                    logger.warning("写入规则 %s 结果失败: %s", rule_name, e2)
            continue

        if not layer_list and layer_dict:
            layer_list = list(layer_dict.keys())

        # 优先从 new_datasets 中覆盖规则的自然语言描述和脚本 / 约束
        new_info_by_name = load_new_rule_info(data_name)
        new_meta = new_info_by_name.get(rule_name, {})
        if new_meta:
            rule_desc = new_meta.get("rule", rule_desc)

        # 2) 调用 Agent 获取大模型生成的坐标与标签；写对话到 log，写坐标到 work/<rule>/llm_response.json
        save_response_path = os.path.join(rule_work_dir, "llm_response.json")

        rule_log_fp = None
        if log_dir:
            log_path = os.path.join(log_dir, f"{rule_name}.txt")
            try:
                rule_log_fp = open(log_path, "w", encoding="utf-8")
                logger.info("规则 %s 对话日志: %s", rule_name, log_path)
            except Exception as e:
                logger.warning("创建规则 %s 对话日志失败: %s", rule_name, e)
                rule_log_fp = None

        try:
            examples, labels = ask_llm_for_examples(
                rule_description=rule_desc,
                layer_list=layer_list,
                api_key=api_key,
                rule_name=rule_name,
                base_url=getattr(config, "BASE_URL", None),
                model=getattr(config, "MODEL", None),
                log_fp=rule_log_fp,
                save_response_path=save_response_path,
            )
        finally:
            if rule_log_fp is not None:
                try:
                    rule_log_fp.close()
                except Exception:
                    pass

        if not examples or not labels:
            logger.warning("规则 %s 未得到任何正/反例（Agent 占位或 API 未配置）", rule_name)
            rule_script_empty = ""
            try:
                with open(base_script_path, "r", encoding="utf-8", errors="replace") as f:
                    rule_script_empty = extract_rule_script_from_rul(f.read(), rule_name)
            except Exception:
                pass
            rule_count += 1
            rule_result_no_examples = {
                "rule_name": rule_name,
                "rule_description": rule_desc,
                "rule_script": rule_script_empty,
                "sample_correct": 0,
                "sample_total": 0,
                "accuracy": 0.0,
                "message": "no examples from agent",
            }
            per_rule_results.append(rule_result_no_examples)
            if result_dir:
                try:
                    out_path = os.path.join(result_dir, f"{rule_name}.json")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(rule_result_no_examples, f, ensure_ascii=False, indent=2)
                    logger.info("规则 %s 结果已写入 %s", rule_name, out_path)
                except Exception as e:
                    logger.warning("写入规则 %s 结果失败: %s", rule_name, e)
            continue

        examples = [normalize_example_coords(ex) for ex in examples]
        if len(labels) > len(examples):
            labels = labels[: len(examples)]
        elif len(examples) > len(labels):
            examples = examples[: len(labels)]

        # 从 .rul 中解析出 DRC 脚本（规则块中除两个 @ 行以外的部分）
        rule_script = ""
        if new_meta and new_meta.get("script"):
            rule_script = new_meta.get("script", "")
        else:
            try:
                with open(base_script_path, "r", encoding="utf-8", errors="replace") as f:
                    rule_script = extract_rule_script_from_rul(f.read(), rule_name)
            except Exception as e:
                logger.warning("解析 rule_script 失败: %s", e)

        if skip_calibre:
            rule_count += 1
            details = [
                {"idx": i, "predicted_label": labels[i], "pos": examples[i]}
                for i in range(len(examples))
            ]
            rule_result = {
                "rule_name": rule_name,
                "rule_description": rule_desc,
                "rule_script": rule_script,
                "calibre_skipped": True,
                "sample_correct": None,
                "sample_total": None,
                "accuracy": None,
                "details": details,
            }
            per_rule_results.append(rule_result)
            if result_dir:
                try:
                    out_path = os.path.join(result_dir, f"{rule_name}.json")
                    with open(out_path, "w", encoding="utf-8") as f:
                        json.dump(rule_result, f, ensure_ascii=False, indent=2)
                    logger.info("规则 %s 结果已写入 %s（未跑 Calibre）", rule_name, out_path)
                except Exception as e:
                    logger.warning("写入规则 %s 结果失败: %s", rule_name, e)
            logger.info("规则 %s: 已跳过 Calibre，样本数=%d", rule_name, len(examples))
            continue

        # 3) Calibre 验证
        correct, total, details = verify_examples(
            rule_name=rule_name,
            examples=examples,
            labels=labels,
            layer_dict=layer_dict,
            base_script_path=base_script_path,
            work_dir=rule_work_dir,
        )
        rule_count += 1
        rule_correct = (total > 0 and all(d.get("match") for d in details))
        if rule_correct:
            rule_correct_count += 1

        # 每条 detail 中记录对应坐标（pos），便于人工检查
        for d in details:
            idx = d.get("idx")
            if idx is not None and 0 <= idx < len(examples):
                d["pos"] = examples[idx]

        rule_result = {
            "rule_name": rule_name,
            "rule_description": rule_desc,
            "rule_script": rule_script,
            "calibre_skipped": False,
            "sample_correct": correct,
            "sample_total": total,
            "accuracy": 1.0 if rule_correct else 0.0,
            "details": details,
        }
        per_rule_results.append(rule_result)
        if result_dir:
            try:
                out_path = os.path.join(result_dir, f"{rule_name}.json")
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(rule_result, f, ensure_ascii=False, indent=2)
                logger.info("规则 %s 结果已写入 %s", rule_name, out_path)
            except Exception as e:
                logger.warning("写入规则 %s 结果失败: %s", rule_name, e)
        logger.info(
            "规则 %s: sample_correct=%d, sample_total=%d, rule_correct=%s",
            rule_name,
            correct,
            total,
            rule_correct,
        )

    if skip_calibre:
        return {
            "correct_rules": None,
            "total_rules": rule_count,
            "overall_accuracy": None,
            "calibre_skipped": True,
            "per_rule": per_rule_results,
        }
    overall_accuracy = (rule_correct_count / rule_count) if rule_count else 0.0
    return {
        "correct_rules": rule_correct_count,
        "total_rules": rule_count,
        "overall_accuracy": round(overall_accuracy, 4),
        "calibre_skipped": False,
        "per_rule": per_rule_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Baseline: 大模型直接生成矩形坐标，Calibre 验证正反例正确率")
    parser.add_argument("--base_rul", type=str, default="", help="全量 DRC 规则文件路径（覆盖 config）")
    parser.add_argument("--api_key", type=str, default="", help="API Key（覆盖 config）")
    parser.add_argument("--rules", type=str, default="", help="规则列表 JSON 路径（为空时默认使用 new_datasets）")
    parser.add_argument("--work_dir", type=str, default="", help="工作目录（覆盖 config）")
    parser.add_argument("--data_name", type=str, default="", help="data_name，如 freePDK15（覆盖 config）")
    parser.add_argument("--output", type=str, default="", help="整体结果汇总 JSON 路径（可选）")
    parser.add_argument("--max_rules", type=int, default=0, help="仅跑 rules JSON 的前 N 条（0 表示全部，且未按工艺分类）")
    parser.add_argument("--free_n", type=int, default=100, help="当使用 all_rules_freePDK15_asap7.json 时，选取 freePDK15 前 N 条（0 表示不限制）")
    parser.add_argument("--asap_n", type=int, default=100, help="当使用 all_rules_freePDK15_asap7.json 时，选取 asap7 前 N 条（0 表示不限制）")
    parser.add_argument("--freepdk45_n", type=int, default=100, help="当使用 new_datasets 时，选取 freepdk-45nm_gpt_output.json 前 N 条（0 表示全部）")
    parser.add_argument("--log_dir", type=str, default="", help="对话日志根目录，默认 log/")
    parser.add_argument("--result_dir", type=str, default="", help="按规则输出结果 JSON 的目录，默认 result/")
    parser.add_argument(
        "--llm_only",
        action="store_true",
        help="仅调用大模型生成正/反例并写日志与 result；跳过 Calibre，不计算 overall_accuracy",
    )
    args = parser.parse_args()

    base_rul_path = args.base_rul or config.BASE_RUL_PATH
    api_key = args.api_key or config.API_KEY
    rules_path = args.rules  # 为空时默认走 new_datasets 流程
    work_dir = args.work_dir or config.WORK_DIR
    data_name = args.data_name or config.DATA_NAME  # 若使用 all_rules_freePDK15_asap7.json，将按 process 拆分并覆盖该值
    max_rules = args.max_rules
    free_n = args.free_n
    asap_n = args.asap_n
    freepdk45_n = args.freepdk45_n
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = args.log_dir or os.path.join(script_dir, "log")
    result_dir = args.result_dir or os.path.join(script_dir, "result")
    skip_calibre = args.llm_only

    if not base_rul_path or not os.path.isfile(base_rul_path):
        logger.error("请配置有效的 base_rul 路径（当前: %s）", base_rul_path)
        sys.exit(1)

    total_result = {
        "correct_rules": None if skip_calibre else 0,
        "total_rules": 0,
        "overall_accuracy": None if skip_calibre else 0.0,
        "per_rule": [],
    }
    if skip_calibre:
        total_result["calibre_skipped"] = True

    # 情况 1：未显式指定 rules，默认从 new_datasets 跑三个工艺
    if not rules_path:
        # freePDK15
        free_info = load_new_rule_info("freePDK15")
        free_items = list(free_info.items())
        if freepdk15_n := free_n:  # 复用 free_n 作为 freePDK15 的条数
            free_items = free_items[:freepdk15_n]
        if free_items:
            free_rules = [
                {"rule_name": name, "rule": meta.get("rule", ""), "layers": []}
                for name, meta in free_items
            ]
            free_base_rul = os.path.join(os.path.dirname(script_dir), "datasets", "freePDK15", "calibreDRC.rul")
            free_result = run_baseline(
                base_rul_path=free_base_rul,
                data_name="freePDK15",
                work_dir=work_dir,
                api_key=api_key,
                rules=free_rules,
                log_dir=log_dir,
                result_dir=result_dir,
                skip_calibre=skip_calibre,
            )
            if not skip_calibre:
                total_result["correct_rules"] += free_result["correct_rules"]
            total_result["total_rules"] += free_result["total_rules"]
            total_result["per_rule"].extend(free_result["per_rule"])

        # asap7
        asap_info = load_new_rule_info("asap7")
        asap_items = list(asap_info.items())
        if asap_n > 0:
            asap_items = asap_items[:asap_n]
        if asap_items:
            asap_rules = [
                {"rule_name": name, "rule": meta.get("rule", ""), "layers": []}
                for name, meta in asap_items
            ]
            asap_base_rul = os.path.join(os.path.dirname(script_dir), "datasets", "asap7", "calibreDRC.rul")
            asap_result = run_baseline(
                base_rul_path=asap_base_rul,
                data_name="asap7",
                work_dir=work_dir,
                api_key=api_key,
                rules=asap_rules,
                log_dir=log_dir,
                result_dir=result_dir,
                skip_calibre=skip_calibre,
            )
            if not skip_calibre:
                total_result["correct_rules"] += asap_result["correct_rules"]
            total_result["total_rules"] += asap_result["total_rules"]
            total_result["per_rule"].extend(asap_result["per_rule"])

        # freepdk-45nm
        fp45_info = load_new_rule_info("freepdk-45nm")
        fp45_items = list(fp45_info.items())
        if freepdk45_n > 0:
            fp45_items = fp45_items[:freepdk45_n]
        if fp45_items:
            fp45_rules = [
                {"rule_name": name, "rule": meta.get("rule", ""), "layers": []}
                for name, meta in fp45_items
            ]
            fp45_base_rul = os.path.join(os.path.dirname(script_dir), "datasets", "freepdk-45nm", "calibreDRC.rul")
            fp45_result = run_baseline(
                base_rul_path=fp45_base_rul,
                data_name="freepdk-45nm",
                work_dir=work_dir,
                api_key=api_key,
                rules=fp45_rules,
                log_dir=log_dir,
                result_dir=result_dir,
                skip_calibre=skip_calibre,
            )
            if not skip_calibre:
                total_result["correct_rules"] += fp45_result["correct_rules"]
            total_result["total_rules"] += fp45_result["total_rules"]
            total_result["per_rule"].extend(fp45_result["per_rule"])

        if skip_calibre:
            total_result["overall_accuracy"] = None
        elif total_result["total_rules"] > 0:
            total_result["overall_accuracy"] = round(
                total_result["correct_rules"] / total_result["total_rules"], 4
            )
        else:
            total_result["overall_accuracy"] = 0.0

    else:
        # 情况 2：显式指定 rules JSON（兼容旧逻辑，如 all_rules_freePDK15_asap7.json）
        rules = load_rules(rules_path)

        # 若规则中带有 process 字段（如 all_rules_freePDK15_asap7.json），按工艺拆分选择，
        # 分别调用 run_baseline（freePDK15/asap7 各跑一遍），最后再合并结果。
        has_process = any(r.get("process") for r in rules)

        if has_process and (free_n > 0 or asap_n > 0):
            free_rules_all = [r for r in rules if r.get("process") == "freePDK15"]
            asap_rules_all = [r for r in rules if r.get("process") == "asap7"]

            free_rules = free_rules_all[:free_n] if free_n > 0 else free_rules_all
            asap_rules = asap_rules_all[:asap_n] if asap_n > 0 else asap_rules_all
            logger.info("按工艺选择规则：freePDK15=%d, asap7=%d", len(free_rules), len(asap_rules))

            # 1) 跑 freePDK15
            if free_rules:
                free_base_rul = os.path.join(os.path.dirname(script_dir), "datasets", "freePDK15", "calibreDRC.rul")
                free_result = run_baseline(
                    base_rul_path=free_base_rul,
                    data_name="freePDK15",
                    work_dir=work_dir,
                    api_key=api_key,
                    rules=free_rules,
                    log_dir=log_dir,
                    result_dir=result_dir,
                    skip_calibre=skip_calibre,
                )
                if not skip_calibre:
                    total_result["correct_rules"] += free_result["correct_rules"]
                total_result["total_rules"] += free_result["total_rules"]
                total_result["per_rule"].extend(free_result["per_rule"])

            # 2) 跑 asap7
            if asap_rules:
                asap_base_rul = os.path.join(os.path.dirname(script_dir), "datasets", "asap7", "calibreDRC.rul")
                asap_result = run_baseline(
                    base_rul_path=asap_base_rul,
                    data_name="asap7",
                    work_dir=work_dir,
                    api_key=api_key,
                    rules=asap_rules,
                    log_dir=log_dir,
                    result_dir=result_dir,
                    skip_calibre=skip_calibre,
                )
                if not skip_calibre:
                    total_result["correct_rules"] += asap_result["correct_rules"]
                total_result["total_rules"] += asap_result["total_rules"]
                total_result["per_rule"].extend(asap_result["per_rule"])

            if skip_calibre:
                total_result["overall_accuracy"] = None
            elif total_result["total_rules"] > 0:
                total_result["overall_accuracy"] = round(
                    total_result["correct_rules"] / total_result["total_rules"], 4
                )
            else:
                total_result["overall_accuracy"] = 0.0

        else:
            # 简单按顺序截取前 max_rules 条，不区分工艺
            if max_rules > 0:
                rules = rules[:max_rules]
                logger.info("仅按顺序选取前 %d 条规则（未区分工艺）", max_rules)
            if not rules:
                logger.warning("未加载到任何规则（路径: %s），将使用空列表运行（仅做流程测试）", rules_path)

            result = run_baseline(
                base_rul_path=base_rul_path,
                data_name=data_name,
                work_dir=work_dir,
                api_key=api_key,
                rules=rules,
                log_dir=log_dir,
                result_dir=result_dir,
                skip_calibre=skip_calibre,
            )
            total_result = result

    if total_result.get("overall_accuracy") is not None:
        logger.info(
            "Baseline 汇总: correct_rules=%d, total_rules=%d, accuracy=%.2f%%",
            total_result["correct_rules"],
            total_result["total_rules"],
            total_result["overall_accuracy"] * 100,
        )
    else:
        logger.info(
            "Baseline 汇总（已跳过 Calibre）: total_rules=%s",
            total_result.get("total_rules"),
        )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(total_result, f, ensure_ascii=False, indent=2)
        logger.info("结果已写入 %s", args.output)
    else:
        print(json.dumps(total_result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
