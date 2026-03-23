# Baseline：大模型直接生成矩形坐标

本目录为 **baseline**：让大模型根据规则的**自然语言描述**直接输出 layout 正/反例的**矩形坐标**与**正/反标签**，可选再用 **Calibre** 跑 DRC，检验模型标签与工具结果是否一致并统计正确率，用于与主方法（如 Z3 求解 + 验证）对比。

## 思路

1. **Agent**（`agent.py`）：调用 OpenAI 兼容 API，输入规则描述，输出 `examples`（每层若干矩形）与 `labels`。
2. **归一化**（`normalize_example_coords`）：将坐标转为整数矩形列表；**兼容**模型把某层写成「单个矩形对象」而非「矩形数组」的情况（见下文「坐标格式」）。
3. **验证**（`verify.py`，非 `--llm_only`）：对每个例子生成 GDS、跑 Calibre、读 `drc_report`，与模型标签比对。
4. **统计**：按规则与整体汇总；整体 **overall_accuracy** = 完全正确的规则数 / 总规则数（一条规则内任一样本不匹配则该规则计错）。

## 目录与文件

| 文件 | 说明 |
|------|------|
| `config.py` | `BASE_RUL_PATH`、`BASE_URL`、`API_KEY`、`MODEL`、`DATA_NAME`、`WORK_DIR`、`RULES_JSON_PATH` |
| `agent.py` | 请求大模型、解析 JSON、`normalize_example_coords` |
| `verify.py` | Calibre 验证；`lib/` 内为 GDS/DRC 辅助逻辑 |
| `run_baseline.py` | 主入口：加载规则 → Agent →（可选）验证 → 写 summary / `result/` / `work/` |
| `patch_summary_pos_from_work.py` | 从 `work/*/llm_response.json` 回填 summary 中**空的** `details[].pos`（见下文） |
| `rules_to_test.json` | 示例规则列表 |
| `all_rules_freePDK15_asap7.json` | 合并规则表（可带 `process` 字段） |
| `../new_datasets/*_gpt_output.json` | 默认规则来源（描述等） |

## 使用前准备

1. **依赖**：`pip install -r requirements.txt`（含 `gdsii` 等）。仅 `--llm_only` 时可不跑 Calibre，但仍建议安装依赖。
2. **Calibre**：完整验证需可执行 `calibre -drc ...`；`--llm_only` 下可不装 Calibre（仍会读 `.rul` 解析层与脚本片段）。
3. **配置**：在 `config.py` 中设置 `BASE_URL`、`API_KEY`、`MODEL`；**勿将密钥提交到版本库**。
4. **`BASE_RUL_PATH`**：`main` 会检查该路径存在（默认 `pn_examples/datasets/freePDK15/calibreDRC.rul`）。使用默认 **new_datasets** 流程时，各工艺实际使用 `datasets/<工艺>/calibreDRC.rul`。

## 运行

### 默认：new_datasets（推荐）

不指定 `--rules` 时，从 `new_datasets` 读取三个文件：

- `freePDK15_gpt_output.json`
- `asap7_gpt_output.json`
- `freepdk-45nm_gpt_output.json`

```bash
cd /path/to/pn_examples/baseline_direct_coord
python run_baseline.py --output summary_all_new.json
```

**条数限制**（`0` 表示该工艺不额外截断，以脚本默认值为准）：

| 参数 | 含义 |
|------|------|
| `--free_n N` | freePDK15 前 N 条（默认 100） |
| `--asap_n N` | asap7 前 N 条（默认 100） |
| `--freepdk45_n N` | freepdk-45nm 前 N 条（默认 100） |

示例：三个数据集各跑前 10 条：

```bash
python run_baseline.py --free_n 10 --asap_n 10 --freepdk45_n 10 --output summary_10_10_10.json
```

### 仅大模型（跳过 Calibre）：`--llm_only`

- 仍会解析对应工艺的 `calibreDRC.rul`（层信息、`rule_script`），并调用大模型。
- **不**生成用于校验的 GDS、**不**执行 Calibre；`overall_accuracy` / `correct_rules` 为 `null`，汇总中带 `calibre_skipped: true`。
- 每条 `result` 中 `details` 含 `idx`、`predicted_label`、`pos`（无 Calibre 的 `match` 等字段）。

```bash
python run_baseline.py --llm_only --output summary_llm_only.json
```

### 显式指定 `--rules`

可指向 `rules_to_test.json` 或 `all_rules_freePDK15_asap7.json` 等。若 JSON 含 `process` 字段，会按 freePDK15 / asap7 拆分并选用对应 `calibreDRC.rul`；否则可用 `--max_rules` 截取前 N 条。

### 其他常用参数

| 参数 | 说明 |
|------|------|
| `--base_rul` | 覆盖 `config.BASE_RUL_PATH`（无 `process` 的单文件规则列表时使用） |
| `--api_key` | 覆盖配置中的 API Key |
| `--work_dir` | 工作目录（默认 `config.WORK_DIR`） |
| `--log_dir` | 对话日志根目录（默认 `log/`） |
| `--result_dir` | 每条规则一个 JSON（默认 `result/`） |
| `--output` | 整体汇总 JSON 路径；不指定则打印到 stdout |

## 输出说明

- **`log/`**：若配置，其下可按规则名生成 `.txt`（用户问题 + 模型回答）。
- **`work/<规则名>/`**：`llm_response.json`（原始回复与解析后的 `examples`/`labels`）、单规则 `.rul` 等中间文件。
- **`result/<规则名>.json`**：单规则结果（含 `details`、`pos` 等）。
- **汇总 JSON**：`overall_accuracy` = 全对规则数 / 总规则数；单条规则 `accuracy` 为 0/1（该规则下全部样本与 Calibre 一致才为 1）。

## 坐标格式与后处理

- 每个 layout 为对象：键为 **层名_序号**（如 `NW_1`、`M1_2`），值为 **矩形列表** `[{llx,lly,urx,ury}, ...]`。`lib/generate_gds.py` 用 `键名.split('_')[0]` 映射到 GDS 层（如 `M1_1` → `M1`）。
- **常见模型输出**：某层只给一个矩形时，误写成 `"M1_1": { "llx": ... }`（对象）而非数组。`normalize_example_coords` 会把这种**单层单矩形 dict**自动包成 `[{...}]`，避免 `pos` 变成空 `{}`。
- **单位**：prompt 中约定坐标为 **nm**；长度(µm)、面积(µm²) 换算见 `agent.get_example_format_spec`。

## 回填 summary 中空 `pos`

若历史运行的汇总里出现 `"pos": {}`，可用脚本从 `work/<规则名>/llm_response.json` 重新归一化并写回：

```bash
python patch_summary_pos_from_work.py summary_all_new_llm_only.json
# 仅预览：加 --dry_run
# 不写 result/：--result_dir ""
```

默认会同步更新 `result/<规则名>.json`（若存在且 summary 中该规则曾有空 `pos`）。

## 与下游评估

其他目录（如 `boundary_coverage_eval`）若引用本目录的 `summary_*.json`，请保证其中 `details[].pos` 非空且格式正确；必要时先运行上述 `patch_summary_pos_from_work.py`。
