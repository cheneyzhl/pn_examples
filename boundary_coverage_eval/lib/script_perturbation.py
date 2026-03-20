# -*- coding: utf-8 -*-
"""
从 DRC script body 中抽取“数值边界”，并生成扰动后的 corner scripts。

说明：
- 本项目不依赖 new_datasets 里的 constraints 字段（按你的要求忽略）。
- 抽取逻辑基于启发式规则：识别形如
  - INTERNAL|LENGTH|AREA|HOLES <var> <op> <number>
  - ANGLE <var>? <op> <number> ...（同一行里可能有多个 <op><number>）
- 对每个边界常数生成 +/- delta 两种扰动，形成 2^n corner combinations。
-
新增（默认推荐）：
- 布尔分解模式：把每个可解析比较子句当作一个“条件变量”，
  对每个变量做 正向/反向 两种形式，形成 2^n corner combinations。
  该模式更贴近 llm_drc-main 的 get_basic_subexpressions + generate_all_combinations 思路。
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


_NUM_RE = r"-?\d+(?:\.\d+)?"
_CMP_RE = re.compile(r"(?P<op><=|>=|<|>)\s*(?P<num>{})".format(_NUM_RE))


@dataclass(frozen=True)
class BoundaryTarget:
    line_idx: int
    kind: str
    var: str
    op: str
    num_str: str
    num_start: int  # within the line
    num_end: int  # within the line
    decimals: int

    def num_float(self) -> float:
        return float(self.num_str)


def negate_operator(op: str) -> str:
    """
    比较运算符取反（逻辑非）。
    """
    mapping = {
        "<": ">=",
        "<=": ">",
        ">": "<=",
        ">=": "<",
    }
    if op not in mapping:
        raise ValueError(f"Unsupported op for negation: {op}")
    return mapping[op]


def _infer_decimals(num_str: str) -> int:
    if "." not in num_str:
        return 0
    return max(0, len(num_str.split(".")[1]))


def extract_boundary_targets(script_body: str) -> List[BoundaryTarget]:
    """
    返回可扰动的边界常数列表（按脚本出现顺序）。
    """
    targets: List[BoundaryTarget] = []
    lines = script_body.splitlines()

    for line_idx, raw_line in enumerate(lines):
        line = raw_line.split("//")[0].strip()
        if not line:
            continue

        # INTERNAL|LENGTH|AREA|HOLES <var> <rest...>
        m = re.match(r"^(INTERNAL|LENGTH|AREA|HOLES)\s+(?P<var>[A-Za-z0-9_]+)\s*(?P<rest>.*)$", line)
        if m:
            kind = m.group(1)
            var = m.group("var")
            rest = m.group("rest")
            # 在原始 raw_line 上找 span，更利于替换（这里用 line 做相同串）
            # 为了让 span 与 raw_line 一致，必须用 raw_line.split("//")[0].strip() 这一份 line。
            # 我们用 line 内部替换，因此 span 以 line 为准（generation 里也以 line 为准）。
            for cmp_m in _CMP_RE.finditer(rest):
                num_str = cmp_m.group("num")
                num_start_in_rest = cmp_m.start("num")
                # 在整行 line 里的起止位置
                # line = "<prefix><rest>", 需要计算 rest 在 line 中的起点
                rest_offset = raw_line.split("//")[0].strip().find(rest)
                if rest_offset < 0:
                    rest_offset = 0
                num_start = rest_offset + num_start_in_rest
                num_end = num_start + len(num_str)
                targets.append(
                    BoundaryTarget(
                        line_idx=line_idx,
                        kind=kind,
                        var=var,
                        op=cmp_m.group("op"),
                        num_str=num_str,
                        num_start=num_start,
                        num_end=num_end,
                        decimals=_infer_decimals(num_str),
                    )
                )
            continue

        # ANGLE <var>? <rest...>
        m = re.match(r"^ANGLE\s+(?P<var>[A-Za-z0-9_]+)\s+(?P<rest>.*)$", line)
        if m:
            kind = "ANGLE"
            var = m.group("var")
            rest = m.group("rest")
            for cmp_m in _CMP_RE.finditer(rest):
                num_str = cmp_m.group("num")
                num_start_in_rest = cmp_m.start("num")
                rest_offset = raw_line.split("//")[0].strip().find(rest)
                if rest_offset < 0:
                    rest_offset = 0
                num_start = rest_offset + num_start_in_rest
                num_end = num_start + len(num_str)
                targets.append(
                    BoundaryTarget(
                        line_idx=line_idx,
                        kind=kind,
                        var=var,
                        op=cmp_m.group("op"),
                        num_str=num_str,
                        num_start=num_start,
                        num_end=num_end,
                        decimals=_infer_decimals(num_str),
                    )
                )
            continue

        # 也支持直接写成 ANGLE <rest...>（没有显式 var）
        m = re.match(r"^ANGLE\s+(?P<rest>.*)$", line)
        if m:
            kind = "ANGLE"
            var = ""
            rest = m.group("rest")
            for cmp_m in _CMP_RE.finditer(rest):
                num_str = cmp_m.group("num")
                num_start_in_rest = cmp_m.start("num")
                rest_offset = raw_line.split("//")[0].strip().find(rest)
                if rest_offset < 0:
                    rest_offset = 0
                num_start = rest_offset + num_start_in_rest
                num_end = num_start + len(num_str)
                targets.append(
                    BoundaryTarget(
                        line_idx=line_idx,
                        kind=kind,
                        var=var,
                        op=cmp_m.group("op"),
                        num_str=num_str,
                        num_start=num_start,
                        num_end=num_end,
                        decimals=_infer_decimals(num_str),
                    )
                )
            continue

    return targets


def _format_number_like(original_num_str: str, new_value: float, min_decimals: Optional[int] = None) -> str:
    """
    按原始格式输出，可选 min_decimals 确保足够精度（用于 delta 扰动时避免舍入导致 corner 相同）。
    """
    decimals = _infer_decimals(original_num_str)
    if min_decimals is not None and min_decimals > decimals:
        decimals = min_decimals
    if decimals == 0:
        # 整数常见：如 0, 90
        return str(int(round(new_value)))
    fmt = "{:." + str(decimals) + "f}"
    s = fmt.format(new_value)
    return s


def apply_boolean_decomposition(script_body: str, targets: List[BoundaryTarget], bits: List[int]) -> str:
    """
    bits: len == len(targets)
      - bit=1: 保留原比较（正向）
      - bit=0: 使用比较取反（反向）
    """
    assert len(bits) == len(targets)

    lines = [ln.split("//")[0].strip() for ln in script_body.splitlines()]

    # 每个目标还需要比较符号的 span；这里重跑正则定位对应片段
    per_line_targets: Dict[int, List[Tuple[int, BoundaryTarget]]] = {}
    for t_idx, t in enumerate(targets):
        per_line_targets.setdefault(t.line_idx, []).append((t_idx, t))

    new_lines = copy.deepcopy(lines)
    for line_idx, lst in per_line_targets.items():
        line = new_lines[line_idx]
        # 为了稳定替换，逐个在当前 line 中搜索 "op + num_str"
        # 注意：同一行可能有多个条件（例如 >a <b），按原 targets 顺序替换首次匹配。
        for t_idx, t in lst:
            old_fragment_pattern = r"(?P<op><=|>=|<|>)\s*(?P<num>{})".format(re.escape(t.num_str))
            m = re.search(old_fragment_pattern, line)
            if not m:
                continue
            old_op = m.group("op")
            old_num = m.group("num")
            if bits[t_idx] == 1:
                new_op = old_op
            else:
                try:
                    new_op = negate_operator(old_op)
                except ValueError:
                    new_op = old_op
            new_fragment = f"{new_op} {old_num}"
            line = line[: m.start()] + new_fragment + line[m.end() :]
        new_lines[line_idx] = line

    return "\n".join([ln for ln in new_lines if ln.strip()])


def apply_perturbations(script_body: str, targets: List[BoundaryTarget], bits: List[int], delta: Optional[float]) -> str:
    """
    bits: len == len(targets)，每个 bit 取 0 -> -delta, 1 -> +delta
    delta: 若为 None，则对每个 target 按 decimals 自动推断一个单位步长（10^-decimals）。
    """
    assert len(bits) == len(targets)

    # 用 splitlines 保留行粒度；按 line_idx 修改 line 内部片段
    lines_raw = script_body.splitlines()
    lines = [ln.split("//")[0].rstrip("\n") for ln in lines_raw]
    # 注意：这里 lines 的每行没有去掉前后空格；为了匹配 targets 的 span，我们用 targets 提取时的 line.strip() 版本并不一致。
    # 为简单起见：我们按行去掉注释并保留 trim 后的行，再替换 span。
    # 因此：targets 的 num_start/num_end 是在 strip 后的行字符串上计算的（extract_boundary_targets 内用 line = raw_line.strip()）。
    # 我们也在这里使用 strip 后的行来替换。
    lines = [ln.strip() for ln in lines]

    # 组装每行的修改列表，避免多次替换导致 span 失效
    per_line_targets: Dict[int, List[Tuple[int, BoundaryTarget]]] = {}
    for t_idx, t in enumerate(targets):
        per_line_targets.setdefault(t.line_idx, []).append((t_idx, t))

    new_lines = copy.deepcopy(lines)
    for line_idx, lst in per_line_targets.items():
        # 让替换从后往前进行
        lst_sorted = sorted(lst, key=lambda x: x[1].num_start, reverse=True)
        line = new_lines[line_idx]
        for t_idx, t in lst_sorted:
            original = t.num_str
            v = float(original)
            if delta is None:
                unit = math.pow(10.0, -t.decimals) if t.decimals > 0 else 1.0
                d = unit
                min_decimals = None
            else:
                d = float(delta)
                # 确保格式精度足以区分 v-delta 与 v+delta，避免舍入后 corner 相同
                min_decimals = max(1, math.ceil(-math.log10(d))) if d > 0 else None
            sign = -1.0 if bits[t_idx] == 0 else 1.0
            nv = v + sign * d
            if nv < 0:
                nv = 0.0
            nv_s = _format_number_like(original, nv, min_decimals=min_decimals)

            line = line[: t.num_start] + nv_s + line[t.num_end :]
        new_lines[line_idx] = line

    return "\n".join(new_lines)


def build_corners(
    script_body: str,
    targets: List[BoundaryTarget],
    delta: Optional[float],
    max_corners: int,
) -> List[Dict]:
    n = len(targets)
    if n == 0:
        return []
    total = 2**n
    if total > max_corners:
        raise ValueError(f"Extracted targets={n} too many for max_corners={max_corners}. total corners={total}.")

    corners: List[Dict] = []
    for corner_idx in range(total):
        bits = [(corner_idx >> i) & 1 for i in range(n)]
        corner_id = f"corner_{corner_idx:0{max(3, len(str(total-1)))}d}"
        perturbed = apply_perturbations(script_body, targets, bits, delta=delta)
        corners.append(
            {
                "corner_id": corner_id,
                "corner_idx": corner_idx,
                "bits": bits,
                "targets": [
                    {
                        "kind": t.kind,
                        "var": t.var,
                        "op": t.op,
                        "num_str": t.num_str,
                        "decimals": t.decimals,
                        "line_idx": t.line_idx,
                    }
                    for t in targets
                ],
                "script_body": perturbed,
            }
        )
    return corners


def build_corners_boolean(script_body: str, targets: List[BoundaryTarget], max_corners: int) -> List[Dict]:
    """
    布尔分解版本：
    每个 target 当作一个条件变量，取值 in {positive, negated}，共 2^n 个 corner。
    """
    n = len(targets)
    if n == 0:
        return []
    total = 2**n
    if total > max_corners:
        raise ValueError(f"Extracted targets={n} too many for max_corners={max_corners}. total corners={total}.")

    corners: List[Dict] = []
    for corner_idx in range(total):
        bits = [(corner_idx >> i) & 1 for i in range(n)]
        corner_id = f"corner_{corner_idx:0{max(3, len(str(total-1)))}d}"
        decomp_script = apply_boolean_decomposition(script_body, targets, bits)
        corners.append(
            {
                "corner_id": corner_id,
                "corner_idx": corner_idx,
                "bits": bits,
                "decomposition_mode": "boolean",
                "targets": [
                    {
                        "kind": t.kind,
                        "var": t.var,
                        "op": t.op,
                        "num_str": t.num_str,
                        "decimals": t.decimals,
                        "line_idx": t.line_idx,
                    }
                    for t in targets
                ],
                "script_body": decomp_script,
            }
        )
    return corners

