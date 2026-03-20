# -*- coding: utf-8 -*-
"""
把“扰动 script body”塞回到单条 rule 的 .rul 模板里（保留 layer/LAYOUT PATH 等结构）。
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def _find_rule_block(lines: List[str], rule_name: str) -> Tuple[int, int]:
    """
    返回 (start_idx, end_idx)：
    - start_idx：rule 块起始行索引（含 `{`）
    - end_idx：rule 块结束行索引（等于 `}`）
    """
    start_idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        # freePDK15: "RULE_NW003{"
        # asap7:    "WELL.W.1 {"
        # 这里保守：只要 stripped 以 rule_name 开头即可
        if s.startswith(rule_name) and "{" in s:
            start_idx = i
            break

    if start_idx < 0:
        raise ValueError(f"Cannot find rule block start for rule_name={rule_name}")

    inside = True
    for j in range(start_idx + 1, len(lines)):
        if lines[j].strip() == "}":
            return start_idx, j
    raise ValueError(f"Cannot find rule block end '}}' for rule_name={rule_name}")


def patch_rule_script_body(
    base_rule_template_path: str,
    rule_name: str,
    new_script_body: str,
    output_path: str,
) -> None:
    """
    用 new_script_body（仅包含内部 DRC 命令）替换 base_rule_template 的内部命令段。
    """
    with open(base_rule_template_path, "r", encoding="utf-8", errors="replace") as f:
        base_lines = f.readlines()

    start_idx, end_idx = _find_rule_block(base_lines, rule_name)

    # 解析要插入的内部脚本行
    new_internal_lines = [ln.strip() for ln in new_script_body.splitlines() if ln.strip()]

    # 找到内部脚本首次出现的位置：第一个非 '@' / 非空 / 非注释 的行
    insertion_point: Optional[int] = None
    for i in range(start_idx + 1, end_idx):
        s = base_lines[i].strip()
        if not s or s.startswith("//") or s.startswith("@"):
            continue
        insertion_point = i
        break
    if insertion_point is None:
        insertion_point = end_idx

    # 组装新内容：
    # - start_idx 行保留
    # - insertion_point 前保留（通常是 @ 指令/注释）
    # - 插入 new_internal_lines
    # - end_idx 前保留可能存在的 '@' 指令（丢弃其它非 '@' 行）
    before = base_lines[: insertion_point]
    kept_after: List[str] = []
    for i in range(insertion_point, end_idx):
        s = base_lines[i].strip()
        if s.startswith("@") or (not s) or s.startswith("//"):
            kept_after.append(base_lines[i])

    out_lines = before + [ln + "\n" for ln in new_internal_lines] + kept_after + base_lines[end_idx:]

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)

