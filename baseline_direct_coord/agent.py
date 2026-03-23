# -*- coding: utf-8 -*-
"""
问答 Agent：输入规则的自然语言描述，输出该规则对应的 layout 正/反例矩形坐标及标签。
通过 OpenAI 兼容 API（base_url + model）调用大模型。
"""
import json
import logging
import urllib.request
import urllib.error
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# 与 llm_drc 中 Z3 求解后得到的坐标格式一致：每个 example 为
# { "LAYER_1": [ {"llx": int, "lly": int, "urx": int, "ury": int}, ... ], "LAYER_2": [...], ... }
# labels: [ True=正例(通过DRC), False=反例(违反DRC), ... ]


def get_example_format_spec(layer_list: List[str]) -> str:
    """返回给大模型的坐标格式说明（与 llm_drc examples.json 一致，并强调边界 / corner case）。"""
    return (
        "请输出 JSON，且仅包含以下两个键：\\n"
        "\"examples\": 数组，每个元素为一个 layout 例子。每个例子是一个对象，键为层名（带序号，如 NW_1, NW_2），"
        "值为矩形列表；每个矩形为 { \\\"llx\\\": 整数, \\\"lly\\\": 整数, \\\"urx\\\": 整数, \\\"ury\\\": 整数 }。\\n"
        "\"labels\": 数组，与 examples 一一对应。true 表示该 layout 应通过 DRC（正例），false 表示应违反 DRC（反例）。\\n"
        f"本规则涉及的层名（不含序号）为: {layer_list}。层名格式为 层名_序号，例如 NW_1, NW_2。\\n"
        "坐标单位约定（必须遵守，避免单位错误）：你给出的 llx/lly/urx/ury 的数值单位是 nm（1 coordinate unit = 1 nm）。\\n"
        "因此：长度（um）= (x_nm 或 y_nm) * 1e-3；面积（um^2）= (宽_nm * 高_nm) * 1e-6。\\n"
        "请特别关注边界 / corner case：对于规则中的每一个条件变量（例如宽度、间距、面积等阈值条件），"
        "先分析有哪些布尔条件（如 >=T / >T / ==T / <T 等），再枚举这些条件的所有组合（若有 3 个独立条件，则有 2^3=8 种组合）。"
        "对于每一种组合，请至少给出 1 个恰好满足所有条件的正例（刚好通过边界），以及 1 个恰好违反该组合中至少一个关键条件的反例（刚好不通过）。"
    )


# 大模型调用最大重试次数（超时、网络错误等可重试）
MAX_API_RETRIES = 3


def _call_chat_api(
    base_url: str,
    api_key: str,
    model: str,
    user_content: str,
) -> str:
    """调用 OpenAI 兼容的 chat completions API，返回 assistant 的 content 文本。失败时最多重试 MAX_API_RETRIES 次。"""
    url = base_url.rstrip("/") + "/v1/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": user_content}],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            choice = out.get("choices")
            if not choice or not isinstance(choice, list):
                return ""
            msg = choice[0].get("message")
            if not msg or not isinstance(msg, dict):
                return ""
            return (msg.get("content") or "").strip()
        except urllib.error.HTTPError as e:
            body_preview = ""
            try:
                body_preview = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if attempt < MAX_API_RETRIES:
                logger.warning(
                    "API 请求 HTTP %s，第 %d/%d 次重试: %s",
                    e.code, attempt, MAX_API_RETRIES, body_preview,
                )
            else:
                logger.exception("API 请求失败 HTTP %s: %s", e.code, body_preview)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt < MAX_API_RETRIES:
                logger.warning(
                    "API 请求超时/网络异常，第 %d/%d 次重试: %s",
                    attempt, MAX_API_RETRIES, e,
                )
            else:
                logger.exception("API 请求异常（已重试 %d 次）: %s", MAX_API_RETRIES, e)
        except json.JSONDecodeError as e:
            if attempt < MAX_API_RETRIES:
                logger.warning("API 返回非 JSON，第 %d/%d 次重试: %s", attempt, MAX_API_RETRIES, e)
            else:
                logger.exception("API 返回解析失败（已重试 %d 次）: %s", MAX_API_RETRIES, e)
        except Exception as e:
            if attempt < MAX_API_RETRIES:
                logger.warning("API 请求异常，第 %d/%d 次重试: %s", attempt, MAX_API_RETRIES, e)
            else:
                logger.exception("API 请求异常（已重试 %d 次）: %s", MAX_API_RETRIES, e)
    return ""


def ask_llm_for_examples(
    rule_description: str,
    layer_list: List[str],
    api_key: Optional[str] = None,
    rule_name: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    log_fp=None,
    save_response_path: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[bool]]:
    """
    根据规则的自然语言描述，请求大模型生成正/反例矩形坐标及标签。
    """
    if not api_key:
        logger.warning("API_KEY 未配置，返回空正反例列表。")
        return [], []
    if not base_url or not model:
        logger.warning("base_url 或 model 未配置，返回空正反例列表。")
        return [], []

    prompt = (
        "规则描述：{}\n\n"
        "请根据上述 DRC 规则，系统性地生成覆盖所有边界情况（corner case）的 layout 正例与反例的矩形坐标。\n"
        "正例：满足该规则的 layout；反例：违反该规则的 layout。\n"
        "要求：先在心中分析出规则中涉及的所有条件变量，再将这些条件视为若干布尔变量，枚举它们的所有组合；"
        "对于每一种组合，至少给出 1 个恰好满足条件边界的正例和 1 个恰好违反边界的反例。\n"
        "在输出 JSON 前，请对每个正/反例重新用“单位约定 + 阈值判定”做自检，确保满足：正例=通过域，反例=违规域。自检失败则不要输出。"
        "{}"
    ).format(rule_description, get_example_format_spec(layer_list))
    response_text = _call_chat_api(base_url, api_key, model, prompt)

    if log_fp is not None:
        try:
            sep = "======== 规则: {} ========\n".format(rule_name or "")
            log_fp.write(sep)
            log_fp.write("用户问题\n")
            log_fp.write(prompt)
            log_fp.write("\n\n大模型回答\n")
            log_fp.write(response_text or "(空)")
            log_fp.write("\n\n")
            log_fp.flush()
        except Exception as e:
            logger.warning("写入对话日志失败: %s", e)

    if not response_text:
        return [], []

    examples, labels = parse_llm_response_to_examples_labels(response_text)

    if save_response_path:
        try:
            with open(save_response_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "raw_response": response_text,
                        "examples": examples,
                        "labels": labels,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.warning("写入中间文件 %s 失败: %s", save_response_path, e)

    return examples, labels


def parse_llm_response_to_examples_labels(response_text: str) -> tuple[List[Dict[str, Any]], List[bool]]:
    """
    将大模型返回的文本解析为 (examples, labels)。
    """
    text = response_text.strip()
    if "```json" in text:
        start = text.find("```json") + len("```json")
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()
    elif "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end != -1:
            text = text[start:end].strip()
    try:
        data = json.loads(text)
        examples = data.get("examples", [])
        labels = [bool(x) for x in data.get("labels", [])]
        if len(examples) != len(labels):
            logger.warning("examples 与 labels 长度不一致，已截断为较短长度。")
            n = min(len(examples), len(labels))
            examples, labels = examples[:n], labels[:n]
        return examples, labels
    except json.JSONDecodeError as e:
        logger.warning("解析大模型返回 JSON 失败: %s", e)
        return [], []


def _is_single_rect_dict(obj: Any) -> bool:
    """判断是否为单个矩形对象（与矩形列表相对）。"""
    if not isinstance(obj, dict):
        return False
    return all(k in obj for k in ("llx", "lly", "urx", "ury"))


def normalize_example_coords(example: Dict[str, Any]) -> Dict[str, Any]:
    """
    确保每个矩形为整数且包含 llx, lly, urx, ury。
    层名中的后缀（如 _1, _2）保留，与 generate_layout 的键格式一致。
    若某层值为单个矩形 dict（常见模型输出），自动包成单元素列表，避免整例被清空。
    """
    out = {}
    for layer_key, rects in example.items():
        if _is_single_rect_dict(rects):
            rects = [rects]
        if not isinstance(rects, list):
            continue
        out_rects = []
        for r in rects:
            if not isinstance(r, dict):
                continue
            try:
                out_rects.append({
                    "llx": int(r["llx"]),
                    "lly": int(r["lly"]),
                    "urx": int(r["urx"]),
                    "ury": int(r["ury"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        if out_rects:
            out[layer_key] = out_rects
    return out

