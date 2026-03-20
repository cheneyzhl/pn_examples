# -*- coding: utf-8 -*-
"""
Baseline 实验配置：大模型直接生成矩形坐标。
"""
import os

# 项目根目录（pn_examples）
PN_EXAMPLES_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 全量 DRC 规则文件路径（用于提取单条规则与 layer 信息）
# 默认使用 pn_examples/datasets/freePDK15/calibreDRC.rul
BASE_RUL_PATH = os.path.join(PN_EXAMPLES_ROOT, "datasets", "freePDK15", "calibreDRC.rul")

# 大模型 API
BASE_URL = "https://api.midsummer.work"
API_KEY = "sk-1KOrQPumQhvb7Xx6kYl2azsUOL7rMpxLAVmMtkiuQ4DTlsI8"
MODEL = "deepseek-ai/DeepSeek-V3"

# 工艺/数据集名称：freePDK15 / asap7 / freepdk-45nm
DATA_NAME = "freePDK15"

# 工作目录：用于生成 GDS、单规则 .rul 和运行 Calibre，drc_report 会写在此目录
WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work")

# 测试规则列表 JSON 路径（若存在则从此文件读取 rule_name + 自然语言描述）
RULES_JSON_PATH = os.path.join(PN_EXAMPLES_ROOT, "baseline_direct_coord", "rules_to_test.json")
