# -*- coding: utf-8 -*-
"""
读取 DRC 规则文件中的 layer 定义。
"""

import json


def read_layer_info(input_file_path, output_layer_path):
    with open(input_file_path, "r") as file:
        lines = file.readlines()

    layer_lines = [line.strip() for line in lines if line.startswith("layer")]
    layer_info = {}

    for line in layer_lines:
        # 去除行尾注释
        line = line.split("//")[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 3 or len(parts) == 4:
            layer_name = parts[1].upper()
            layer_number = int(parts[2])
            layer_info[layer_name] = layer_number

    json_data = {"layer": layer_info}
    with open(output_layer_path, "w") as json_file:
        json.dump(json_data, json_file, indent=4)
    return layer_info

