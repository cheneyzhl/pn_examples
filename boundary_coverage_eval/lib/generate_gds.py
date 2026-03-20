# -*- coding: utf-8 -*-
"""
从 llm_drc-main / baseline_direct_coord 复制：根据矩形坐标生成 GDS、编辑 DRC 脚本、调用 Calibre、读取 drc_report。
"""

import os
import re
import subprocess

from gdsii.elements import Boundary
from gdsii.library import Library
from gdsii.structure import Structure


def generate_layout(one_example, layer_dict, save_path):
    lib = Library(5, b"NEWLIB.DB", 1e-9, 0.001)
    struct = Structure(b"TOPCELL")
    datatype = 0

    for layer_name, coord_list in one_example.items():
        # baseline / gdsii 约定：键如 "NW_1" -> 取 "NW"
        layer_name = layer_name.split("_")[0]
        layer_index = layer_dict[layer_name]
        for coords in coord_list:
            llx = coords["llx"]
            lly = coords["lly"]
            urx = coords["urx"]
            ury = coords["ury"]
            points = [(llx, lly), (urx, lly), (urx, ury), (llx, ury), (llx, lly)]
            polygon = Boundary(layer_index, datatype, points)
            struct.append(polygon)

    lib.append(struct)
    with open(save_path, "wb") as stream:
        lib.save(stream)


def edit_drc_file(layer_dict, drc_file_path, rule_output_path, rule_name, data_name):
    """
    从全量 drc_file_path 中提取单规则，并做 baseline 同款 OPPOSITE/特殊处理。
    """
    with open(drc_file_path, "r") as file:
        lines = file.readlines()
    first_flag = True
    match_rule_flag = False
    inside_rule_block = False
    save_lines = []

    for line_index, line in enumerate(lines):
        if line.strip().startswith("//"):
            continue

        if data_name == "freePDK15":
            rule_match = line.strip().startswith("RULE_")
        elif data_name == "asap7":
            rule_match = (
                len(line.split(".")) >= 3
                and (not line.strip().startswith("@"))
                and line.split(".")[0] in layer_dict
            )
        elif data_name == "freepdk-45nm":
            rule_match = (
                len(line.split(".")) >= 2
                and len(line.split(".")) <= 4
                and (not line.strip().startswith("@"))
                and ((line.split(".")[0].upper() in layer_dict))
            )
        else:
            raise ValueError("Not Implement data_name {}".format(data_name))

        if rule_match:
            current_rule_name = line.strip()
            if current_rule_name[-1] == "{":
                current_rule_name = current_rule_name[:-1].strip()
            if current_rule_name == rule_name:
                match_rule_flag = True
            if first_flag:
                first_flag = False
            inside_rule_block = True

        elif inside_rule_block:
            if match_rule_flag:
                tokens = line.upper().split()
                last_three_words = line.upper().strip().split()[-3:]
                if last_three_words == ["<", "0.001", "SINGULAR"]:
                    continue
                if tokens and tokens[0] == "EXTERNAL" and "OPPOSITE" not in tokens:
                    line = line.rstrip() + " OPPOSITE\n"

            if "}" in line:
                inside_rule_block = False
                if match_rule_flag:
                    save_lines.append(line)
                match_rule_flag = False
                continue

        if (not rule_match and not inside_rule_block) or match_rule_flag:
            save_lines.append(line)

    with open(rule_output_path, "w") as file:
        file.writelines(save_lines)


def edit_script_path(base_script_path, output_layout_path, output_script_path):
    """
    修改单条 .rul 中 LAYOUT PATH 对应的 gds 路径。
    base_script_path 建议传入绝对路径，避免调用方 chdir 后相对路径解析错误。
    """
    path_to_read = os.path.abspath(os.path.normpath(base_script_path))
    with open(path_to_read, "r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()
    pattern = r'(LAYOUT PATH\s+)"(.*)"'

    for line_index, line in enumerate(lines):
        if line.strip().startswith("//"):
            continue
        if re.search(pattern, line):
            lines[line_index] = re.sub(pattern, r'\1"{}"'.format(output_layout_path), line)

    with open(output_script_path, "w") as file:
        file.writelines(lines)


def read_drc_report(file_path):
    """
    baseline 逻辑：读取 drc_report 的最后一行，提取最后一位字符作为 0/non-0。
    """
    with open(file_path, "r") as file:
        lines = file.readlines()
    line = lines[-1]
    if "TOTAL DRC Results Generated" in line:
        result = line.split(":")[-1].strip()[0]
        return result
    return None


def call_calibre_drc(drc_script_path):
    command = "calibre -drc -hier -turbo -hyper {}".format(drc_script_path)
    subprocess.run(command, shell=True, check=True)

