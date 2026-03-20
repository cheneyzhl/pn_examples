# Baseline 实验：大模型直接生成矩形坐标

本目录实现**补充 baseline 实验**：让大模型根据规则的自然语言描述**直接生成** layout 正/反例的矩形坐标，再用 Calibre 执行 DRC 检查，验证模型给出的正反标签是否与 Calibre 结果一致，从而得到**正确率**，用于对比体现本仓库主方法（Z3 求解 + 验证）的通用性与有效性。

## 思路

1. **问答 Agent**：输入为规则的自然语言描述，输出该规则对应的若干 layout 正/反例的**矩形坐标**及**正/反标签**。坐标格式与 `llm_drc-main` 中 Z3 求解得到的格式一致（每例为 `{ "LAYER_1": [ {"llx", "lly", "urx", "ury"}, ... ], ... }`，`labels` 为 `true`/`false` 数组）。Agent 的 prompt 会要求模型先分析规则中的条件变量，并枚举所有 corner case，为每种组合给出“恰好通过”和“恰好不通过”的正反例。
2. **验证**：对每个例子用 Calibre 执行 DRC 脚本，检查**给出的正/反标签**是否与 **Calibre 的检查结果**一致（无违规视为正例，有违规视为反例）。一致则计为正确，否则不正确。
3. **统计**：按规则与整体统计正确数/总数与正确率。

## 目录与文件

本目录为**独立项目**，不依赖 `llm_drc-main`；所需 GDS/DRC 逻辑已复制在 `lib/` 下。

- **`config.py`**：配置项。含 `BASE_RUL_PATH`、`BASE_URL`、`API_KEY`、`MODEL`、`DATA_NAME`、`WORK_DIR`、`RULES_JSON_PATH`。
- **`agent.py`**：Agent 逻辑。根据规则描述通过 OpenAI 兼容 API 请求大模型生成坐标与标签。
- **`verify.py`**：验证逻辑。使用 `lib/` 内复制的 `generate_layout`、`edit_script_path`、`edit_drc_file`、`call_calibre_drc`、`read_drc_report`、`read_layer_info`，对每组 (坐标, 标签) 生成 GDS、跑 Calibre、比对结果。
- **`lib/`**：从 llm_drc-main 复制的 `generate_gds.py`、`read_drc_file.py`，供本目录独立运行。
- **`run_baseline.py`**：主入口。加载规则列表 → 逐条调用 Agent → 验证 → 输出正确率与明细，并将每条规则结果写入 `result/<rule_name>.json`。支持直接从合并规则集中按工艺选取 freePDK15 前 N 条与 asap7 前 N 条。
- **`rules_to_test.json`**：示例待测规则（rule_name + 自然语言描述 + layers）。可替换为自建列表。
- **`all_rules_freePDK15_asap7.json`**：由 `freePDK15_rules.json` 与 `asap7_rules.json` 合并生成的规则总表，增加 `process` 字段（`freePDK15` 或 `asap7`），用于从两个工艺中各选前 N 条规则。
- **`new_datasets/*.json`**：`new_datasets/freePDK15_gpt_output.json`、`asap7_gpt_output.json`、`freepdk-45nm_gpt_output.json`，存放新的规则描述和 DRC 脚本（不直接使用 constraints 字段）。Baseline 默认会优先使用其中的 rule/script 信息来构造 prompt 和记录结果。

## 使用前准备

1. **依赖**：`pip install -r requirements.txt`（需要 `gdsii`）。
2. **Calibre**：已安装且可在命令行执行 `calibre -drc ...`。
3. **规则文件**：`config.py` 中 `BASE_RUL_PATH` 默认指向 `pn_examples/datasets/freePDK15/calibreDRC.rul`，可按需修改。

## 运行

### 默认：直接跑 new_datasets（推荐）

不指定 `--rules` 时，脚本会默认从 `new_datasets` 下的三个文件读取规则：

- `new_datasets/freePDK15_gpt_output.json`
- `new_datasets/asap7_gpt_output.json`
- `new_datasets/freepdk-45nm_gpt_output.json`

示例：

```bash
cd /path/to/pn_examples/baseline_direct_coord
python run_baseline.py --output summary_all_new.json
```

默认行为：

- freePDK15：跑 `freePDK15_gpt_output.json` 中**全部规则**
- asap7：跑 `asap7_gpt_output.json` 中**全部规则**
- freepdk-45nm：跑 `freepdk-45nm_gpt_output.json` 中**全部规则**

你可以通过以下参数限制每个新数据集跑前几条：

- `--free_n N`：只跑 freePDK15 的前 N 条（默认 100；0 表示全部）
- `--asap_n N`：只跑 asap7 的前 N 条（默认 100；0 表示全部）
- `--freepdk45_n N`：只跑 freepdk-45nm 的前 N 条（默认 100；0 表示全部）

例如，只跑三个数据集各前 10 条规则：

```bash
python run_baseline.py --free_n 10 --asap_n 10 --freepdk45_n 10 --output summary_10_10_10.json
```

### 兼容：显式指定旧的 rules JSON

仍然可以通过 `--rules` 指定旧的规则列表（例如 `rules_to_test.json` 或 `all_rules_freePDK15_asap7.json`），此时：

- 若 JSON 中带有 `process` 字段（如 `all_rules_freePDK15_asap7.json`），则：
  - `--free_n` / `--asap_n` 用来选取各工艺前 N 条规则；
  - 脚本会分别用 `datasets/freePDK15/calibreDRC.rul` 与 `datasets/asap7/calibreDRC.rul` 跑两次，再汇总结果。
- 若 JSON 中不带 `process` 字段，则按 `--max_rules` 简单截取前 N 条。

一般情况下，建议直接使用默认的 `new_datasets` 流程。`--rules` 主要用于调试或兼容旧实验。

**对话日志与中间文件**：

- 每次运行会在 `log/` 下按时间戳创建一个子目录（如 `log/2026-03-17_21-10-00/`），并在该目录下以规则名创建 txt 文件（如 `RULE_NW003.txt`、`WELL.W.1.txt`、`Well.4.txt`），记录该规则的「用户问题」和「大模型回答」。整次运行只会对应一个时间戳子目录。 
- 每条规则对应的工作目录 `work/<规则名>/` 下会生成 `llm_response.json`，包含大模型原始回复及解析后的 `examples`、`labels` 坐标，便于查看中间过程。
- 可通过 `--log_dir` 指定日志根目录（默认 `log/`），通过 `--result_dir` 指定按规则输出结果的目录（默认 `result/`）。

整体 `summary_xxx.json` 中的 `overall_accuracy` 定义为：**所有规则中“完全正确”的规则数 / 总规则数**。只要某条规则中存在一个样本预测与 Calibre 不匹配，该条规则就计为错误，其 `accuracy` 字段为 0.0；只有当该规则下所有样本均匹配时，其 `accuracy` 才为 1.0。每条规则的 `result/<规则名>.json` 仍会保留 `sample_correct` / `sample_total` 以及逐样本的 `details` 便于人工检查。

## 坐标格式说明（与 llm_drc 一致）

每个 layout 例子为 JSON 对象，键为层名（如 `NW_1`、`NW_2`），值为矩形列表；每个矩形为：

- `"llx"`, `"lly"`, `"urx"`, `"ury"`：整数坐标（与 Z3 求解输出一致）。

`labels` 与 `examples` 一一对应：`true` 表示该 layout 应通过 DRC（正例），`false` 表示应违反 DRC（反例）。验证时用 Calibre 跑该规则，无违规即视为正例，有违规即视为反例，再与模型给出的标签比对。
