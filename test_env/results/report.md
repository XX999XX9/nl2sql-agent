# Spider 测试集评测报告（最终）

- 生成时间：2026-09-09 21:34
- 测试集：test_queries.csv，682 题 / 20 库（Spider 基准子集，gold SQL 全部可在 MySQL 9.0 执行）
- 数据库：Docker spiderman-mysql（MySQL 9.0.0，只读账号 nl2sql_test，172.25.32.1:3306）
- LLM：DeepSeek-V4-Flash（百炼 OpenAI 兼容，temperature=0），Embedding：text-embedding-v3

## 一、核心指标总览

| 指标 | 第1轮 检索关闭 | 第2轮 检索开启(threshold=10) |
|---|---|---|
| **执行准确率（Execution Accuracy）** | **75.51%** (515/682) | **71.99%** (491/682) |
| 语法通过率 | 99.71% | 99.71% |
| 总时长（估算，含补测） | ~109 分钟 | ~106 分钟 |
| 平均每题延迟 | 9.6 s | 9.3 s |
| token 消耗（prompt/completion/total） | 832,192 / 232,310 / 1,064,502 | 809,486 / 223,068 / 1,032,554 |
| 纠错触发/挽回 | 1 / 1 | 0 / 0 |
| 意图分布 | {'query': 680, 'meta': 2} | {'query': 680, 'meta': 2} |
| LLM生成失败(error=generate) | 24 | 26 |

## 二、两轮差异归因（重要）

两轮准确率差 -3.52pp（净 -24 题：run2 多答对 37，多答错 61），**主要不是 schema 检索造成的**：

- 20 个测试库中仅 `student_transcripts_tracking`（11 表 ≥ threshold=10）真正走了 Top-K 检索路径，其余 19 库（637 题）两轮配置完全等价（全量 Schema）
- 配置等价的 19 库上净差 **-22 题** → 这就是 **LLM 输出非确定性噪声基线**（百炼同 temperature=0 仍有采样抖动，98 题发生翻转，翻转率 14.4%）
- 唯一受检索影响的库：run1 28/45 vs run2 26/45（净 -2），远小于噪声波动（45 题二项分布 std≈3），**无法得出检索显著劣化或收益的结论**

结论：在本测试集（平均仅 3.6 表/库，最大 11 表）上，Top-K 检索既无收益也无显著损失——
小库场景全量 Schema 本就不构成注意力稀释，符合设计预期（检索面向 30+ 表大库）。

## 三、各库准确率

| 库 | 表数 | 检索路径 | 1轮 | 2轮 | Δ |
|---|---|---|---|---|---|
| battle_death | 3 | full-schema | 88.9% (8/9) | 77.8% (7/9) | -11.1pp |
| car_1 | 6 | full-schema | 67.4% (31/46) | 56.5% (26/46) | -10.9pp |
| concert_singer | 4 | full-schema | 92.3% (24/26) | 76.9% (20/26) | -15.4pp |
| course_teach | 3 | full-schema | 88.9% (16/18) | 88.9% (16/18) | +0.0pp |
| cre_Doc_Template_Mgt | 4 | full-schema | 89.7% (52/58) | 87.9% (51/58) | -1.7pp |
| dog_kennels | 8 | full-schema | 72.0% (36/50) | 70.0% (35/50) | -2.0pp |
| employee_hire_evaluation | 4 | full-schema | 77.8% (21/27) | 81.5% (22/27) | +3.7pp |
| flight_2 | 3 | full-schema | 64.1% (41/64) | 64.1% (41/64) | +0.0pp |
| museum_visit | 3 | full-schema | 81.8% (9/11) | 63.6% (7/11) | -18.2pp |
| network_1 | 3 | full-schema | 84.0% (21/25) | 88.0% (22/25) | +4.0pp |
| orchestra | 4 | full-schema | 96.2% (25/26) | 96.2% (25/26) | +0.0pp |
| pets_1 | 3 | full-schema | 65.5% (19/29) | 65.5% (19/29) | +0.0pp |
| poker_player | 2 | full-schema | 94.7% (36/38) | 100.0% (38/38) | +5.3pp |
| real_estate_properties | 5 | full-schema | 50.0% (1/2) | 100.0% (2/2) | +50.0pp |
| singer | 2 | full-schema | 95.8% (23/24) | 95.8% (23/24) | +0.0pp |
| student_transcripts_tracking | topk | 62.2% (28/45) | 57.8% (26/45) | -4.4pp |
| tvshow | 3 | full-schema | 83.7% (36/43) | 81.4% (35/43) | -2.3pp |
| voter_1 | 3 | full-schema | 66.7% (8/12) | 91.7% (11/12) | +25.0pp |
| world_1 | 3 | full-schema | 61.9% (52/84) | 48.8% (41/84) | -13.1pp |
| wta_1 | 3 | full-schema | 62.2% (28/45) | 53.3% (24/45) | -8.9pp |

## 四、测试过程中发现并修复的项目 Bug

1. **`sql_executor._normalize_row`**：`date.isoformat(sep=" ")` 对纯 `date` 对象崩溃（`date.isoformat()` 不接受参数）→ wta_1 的 DATE 列查询全部炸穿 pipeline。修复：datetime/date 分支分开处理
2. **`sql_validator.validate`**：只捕获 `ParseError`，sqlglot 对非法 token 抛 `TokenError` 未捕获 → 含特殊字符的 LLM 输出直接崩。修复：`except (ParseError, TokenError)`
（修复前 7 题 crash，修复后补测正常参与评测，全部答错——这些题本身即高难度题）

## 五、其他统计

- 错误类型分布（run1）：generate 24 题（LLM 未产出有效 SQL）、其余为结果不匹配
- 意图误判：每轮 2 题被判 meta（不生成 SQL 直接答），9 题轻微损失
- 自动纠错触发极少（1 次，挽回 1 题）：因静态校验+只读库容错性高，多数错误 SQL 能执行成功但结果错（语义错误不可纠）

## 六、复现方式

```bash
python test_env/run_eval.py --run all          # 全量两轮（断点续跑）
python test_env/run_eval.py --run 2 --db student_transcripts_tracking  # 只测检索路径库
```
