# Boundary Coverage Eval

用于评估：`baseline_direct_coord` 生成的正/反例（layout 矩形坐标）是否能**完整区分**所有扰动 script 的正误（正确/错误），从而判断正反例是否覆盖了"边界 corner cases"。

**核心逻辑：**
1. **标记脚本预期正误**：对每个规则，原本的脚本为**正确脚本**，所有的扰动脚本均为**错误脚本**。生成时在 corner 的 `meta.json` 中写入 `script_expected_correct`。
2. **正反例验证**：对每个 corner（包含原脚本与扰动脚本），用 baseline 的正/反例跑 Calibre 检查。若 calibre 结果与正反例的 `predicted_label` 完全一致，则推断该 script **正确**；否则推断**错误**。
3. **覆盖判定**：将每个 script 的预期标签与检查得到的正误标签进行匹配。**完全匹配**说明正反例能检查出所有错误，覆盖了边界；**否则**说明该规则对应的正反例有误，不能检测所有边界情况。

项目包含两个独立流程：
## 1) 扰动脚本生成
入口：`generate_perturbed_drc_scripts.py`

会输出每条规则在每个 `corner_id` 下的扰动 script body（`script.txt`）与元信息（`meta.json`）。`meta.json` 含 `script_expected_correct`（该 corner 的 script 预期为正确/错误），供检测流程使用。

## 2) 边界覆盖检测
入口：`check_boundary_coverage.py`

会读取基线正/反例结果 + 之前生成的扰动 script，运行 Calibre 并统计每条规则是否满足判定标准。

---

## 运行方式示例

推荐使用 conda 环境 `llmdrc`（含 gdsii、Calibre 等依赖）：
```bash
conda activate llmdrc
cd /path/to/pn_examples/boundary_coverage_eval
```

### 1) 生成扰动脚本

支持两种分解模式（`--decomposition_mode`）：
- **boolean**：对每个条件变量的比较运算符取反（如 `<` → `>=`），生成 2^n 个 corner。注意：Calibre 的 `INTERNAL` 等关键字不支持部分取反形式（如 `>=` 单独使用），可能导致 `Error CNS3: improper constraint range`。
- **numeric**：对每个条件变量的数值做 ±delta 扰动，生成 2^n 个 corner。输出均为合法 Calibre 语法，**若 boolean 模式下遇 Calibre 报错，推荐改用此模式**。

**单数据集**（如仅 freePDK15）：
```bash
cd /share/home/zenghuanlong/pn_examples/boundary_coverage_eval
python generate_perturbed_drc_scripts.py --data_name freePDK15 --output_dir ../perturbed_datasets --decomposition_mode boolean
```

**三个数据集**（freePDK15、asap7、freepdk-45nm 对应 new_datasets 下三个 JSON）：
```bash
python generate_perturbed_drc_scripts.py --all_datasets --output_dir ../perturbed_datasets --decomposition_mode boolean
```

**数值扰动模式**（遇 Calibre 报错时推荐）：
```bash
python generate_perturbed_drc_scripts.py --all_datasets --output_dir ../perturbed_datasets \
  --decomposition_mode numeric --delta 0.001
```
`--delta`：数值扰动步长（单位 um）；不指定时按小数位数自动推断（如 0.160 → ±0.001）。

输出结构：`perturbed_datasets/<data_name>/<rule_name>/corner_*/script.txt`，三个数据集各有一份扰动版本。

**扰动模式说明**（`--decomposition_mode`）：

| 模式 | 对每个条件变量的扰动 | corner 数 | 备注 |
|------|----------------------|-----------|------|
| `boolean`（默认） | 原比较 / 取反比较（如 `<` → `>=`） | 2^n | 部分取反形式可能触发 Calibre `Error CNS3` |
| `numeric` | 阈值 -delta / +delta | 1+2^n（含 corner_original） | 输出均为合法 Calibre 语法，会插入原脚本作为 corner_original |

两种模式都会对命题的每个条件变量施加边界扰动，生成 2^n 个 corner；区别在于 boolean 翻转运算符，numeric 扰动数值。

**何时使用 numeric 模式**：若 boolean 模式下 Calibre 报 `Error CNS3 - improper constraint range for this operation or keyword: internal`，说明取反后的形式（如 `INTERNAL layer >= value`）不被 Calibre 接受，可改用 numeric 模式。

### 2) 边界覆盖检测

**方式 A：使用 baseline summary（与 run_baseline.py --output 对齐）**
适用于 `python run_baseline.py --free_n 10 --asap_n 10 --freepdk45_n 10 --output summary_10_10_10.json` 的输出：
```bash
python check_boundary_coverage.py \
  --baseline_summary ../baseline_direct_coord/summary_1_1_1.json \
  --generated_scripts_dir ../perturbed_datasets \
  --output_dir eval_results
```
会自动从 new_datasets 推断每条规则所属 data_name。仅从 summary 读取 pos 与 predicted_label，GDS 与 .rul 在本项目内生成。

**方式 B：使用 baseline result 目录 + 单 data_name**
```bash
python check_boundary_coverage.py --data_name freePDK15 \
  --baseline_result_dir ../baseline_direct_coord/result \
  --generated_scripts_dir ../perturbed_datasets \
  --output_dir eval_results_free
```
同样仅从 result JSON 读取 pos 与 predicted_label，GDS 与 .rul 在本项目内仿 baseline_direct_coord 生成。

---

## 重要说明（检测逻辑）

检测使用** script 正误匹配**逻辑：
- **script_expected_correct**：原脚本=正确（True），所有扰动脚本=错误（False）。boolean 模式中全 bits=1 的 corner 等价于原 script；numeric 模式会显式插入 `corner_original` 作为原脚本。
- **script_predicted_correct**：由正反例 Calibre 检查推断——当 calibre 结果与 `predicted_label` 完全一致时，推断 script 正确；否则推断错误。
- **corner_ok**：`script_predicted_correct == script_expected_correct` 时通过。若 meta 无 `script_expected_correct`（旧格式），该 corner 不参与覆盖判定。

---

## 条件变量定义（与 llm_drc-main 对齐说明）

- `llm_drc-main`：条件变量来自 Z3 约束的原子子表达式（`get_basic_subexpressions`），然后按每个变量的 `expr / Not(expr)` 做布尔组合。
- 本项目（当前默认）：在没有使用 `constraints` 字段的前提下，从 DRC `script` 抽取原子比较子句（如 `< 0.160`、`> 0.014`），将其作为条件变量，并按"原比较 / 取反比较"做布尔组合。
- 因此在"按条件变量做 2^n 角点布尔分解"这一点上是对齐的；差异仅在变量来源（Z3 表达式 vs DRC script）。

---

## 常见问题

### 1. .rul 文件为什么 asap7 比 freePDK15 长很多？

生成逻辑与 `baseline_direct_coord` 一致：从全量 `calibreDRC.rul` 提取**前置信息**（LAYOUT、layer 定义、派生层、connectivity 等）+ **单条目标规则**。不同工艺的前置长度不同：asap7 约 1200+ 行，freePDK15 约 200 行。Calibre 需要完整 layer 定义才能运行，因此无法精简。**属于正常现象**，不是 bug。

### 2. samples 中的 `error: null` 是什么意思？

`error: null` 表示 Calibre 执行**未发生异常**（正常跑完）。若 `match: false` 但 `error: null`，说明是**语义不匹配**（Calibre 与 baseline 判定的通过/违规不同），而非运行错误。若 Calibre 崩溃或报错，`error` 会记录异常信息。

### 3. 旧版 perturbed_datasets 无 script_expected_correct 怎么办？

若 corner 的 `meta.json` 中无 `script_expected_correct` 字段（旧格式），该 corner 将不参与覆盖判定，`corner_ok` 为 `null`。需重新运行 `generate_perturbed_drc_scripts.py` 生成新的 perturbed_datasets。

---

## 输出目录与 eval.json 字段说明

检测完成后，`--output_dir` 下结构为：

```
output_dir/
├── summary.json          # 汇总（总规则数、通过数、accuracy 等）
└── result/
    └── <rule_name>/
        └── eval.json     # 单规则详细结果
```

### eval.json 字段（result/\<rule_name\>/eval.json）

| 字段 | 说明 |
|------|------|
| `rule_name` | 规则名称，如 RULE_NW003、WELL.W.1 |
| `rule_description` | 规则描述（来自 new_datasets），如 "Minimum width of NW is 160nm" |
| `rule_script` | 原始 DRC script（来自 new_datasets，未扰动） |
| `perturbed_scripts` | `{corner_id: script_body}`，各 corner 扰动后的 script，便于查看做了哪些边界扰动 |
| `data_name` | 工艺库：freePDK15 / asap7 / freepdk-45nm |
| `judge_mode` | 判定模式：match / detect |
| `corner_count` | 扰动 corner 数量 |
| `sample_count` | 样本数量 |
| `all_corners_match` | 是否所有 corner 下 calibre 与 predicted_label 一致（辅助观察） |
| `all_corners_detect` | 是否所有 corner 下至少有一个 mismatch（辅助观察） |
| `all_corners_ok` | 是否所有含 script_expected_correct 的 corner 其推断与预期一致（覆盖判定） |
| `per_corner` | 各 corner 的明细 |

**per_corner 每项：** `corner_id`、`script_expected_correct`、`script_predicted_correct`、`corner_any_mismatch`、`corner_all_match`、`corner_ok`、`samples`

**samples 每项（与 baseline summary 的 details 对齐）：**

| 字段 | 说明 |
|------|------|
| `idx` | 样本索引 |
| `predicted_label` | baseline 预测（true=正例通过，false=反例违规） |
| `calibre_label` | Calibre 实际结果（true=通过，false=违规） |
| `match` | 二者是否一致 |
| `error` | 异常信息，`null` 表示无异常 |
| `pos` | 矩形坐标 `{layer: [{llx,lly,urx,ury}, ...]}`，便于查看 layout |

### summary.json

根目录汇总：`total_rules_evaluated`、`passed_rules_by_judge_mode`、`boundary_coverage_accuracy` 等，`details` 为各规则 eval.json 的合并列表。
 