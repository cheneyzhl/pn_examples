# -*- coding: utf-8 -*-
"""
Boundary coverage eval 配置
"""

import os

PN_EXAMPLES_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 基线项目输出目录
BASELINE_RESULT_DIR = os.path.join(PN_EXAMPLES_ROOT, "baseline_direct_coord", "result")

# new_datasets 输入目录
NEW_DATASETS_DIR = os.path.join(PN_EXAMPLES_ROOT, "new_datasets")

# 全量 DRC rule 文件（用于提取单条 rule 块 + 读取 layer）
BASE_RUL_PATHS = {
    "freePDK15": os.path.join(PN_EXAMPLES_ROOT, "datasets", "freePDK15", "calibreDRC.rul"),
    "asap7": os.path.join(PN_EXAMPLES_ROOT, "datasets", "asap7", "calibreDRC.rul"),
    "freepdk-45nm": os.path.join(PN_EXAMPLES_ROOT, "datasets", "freepdk-45nm", "calibreDRC.rul"),
}

# 工作目录（生成 GDS、写入单规则 .rul、drc_report 等）
WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")

