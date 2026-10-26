# Spider 测试集评测报告

- 生成时间：2026-09-21 12:36:27
- 测试集：test_queries.csv（682 题 / 20 库）
- 数据库：Docker spiderman-mysql（MySQL 9.0.0，只读账号）

| 指标 | 第1轮 检索关闭 | 第2轮 检索开启(threshold=10) |
|---|---|---|
| 可评估题数（gold 可执行） | 682 | 682 |
| **执行准确率** | **75.37%** | **71.99%** |
| 语法通过率 | 99.71% | 99.71% |
| 总耗时 | 68.0 分钟 | 0.6 分钟 |
| 平均每题 | 16.91 s | 9.35 s |
| token 消耗(prompt/completion/total) | 1331461 / 364377 / 1695838 | 809486 / 223068 / 1032554 |
| 纠错触发 / 挽回 | 11 | 0 |

## 各库准确率

| 库 | 表数 | 检索路径 | 1轮准确率 | 2轮准确率 | Δ |
|---|---|---|---|---|---|
| battle_death | full-schema | 77.78% (7/9) | 77.78% (7/9) | +0.0pp |
| car_1 | full-schema | 67.39% (31/46) | 56.52% (26/46) | -10.9pp |
| concert_singer | full-schema | 80.77% (21/26) | 76.92% (20/26) | -3.8pp |
| course_teach | full-schema | 83.33% (15/18) | 88.89% (16/18) | +5.6pp |
| cre_Doc_Template_Mgt | full-schema | 89.66% (52/58) | 87.93% (51/58) | -1.7pp |
| dog_kennels | full-schema | 70.00% (35/50) | 70.00% (35/50) | +0.0pp |
| employee_hire_evaluation | full-schema | 81.48% (22/27) | 81.48% (22/27) | +0.0pp |
| flight_2 | full-schema | 65.62% (42/64) | 64.06% (41/64) | -1.6pp |
| museum_visit | full-schema | 72.73% (8/11) | 63.64% (7/11) | -9.1pp |
| network_1 | full-schema | 84.00% (21/25) | 88.00% (22/25) | +4.0pp |
| orchestra | full-schema | 96.15% (25/26) | 96.15% (25/26) | +0.0pp |
| pets_1 | full-schema | 68.97% (20/29) | 65.52% (19/29) | -3.4pp |
| poker_player | full-schema | 97.37% (37/38) | 100.00% (38/38) | +2.6pp |
| real_estate_properties | full-schema | 100.00% (2/2) | 100.00% (2/2) | +0.0pp |
| singer | full-schema | 87.50% (21/24) | 95.83% (23/24) | +8.3pp |
| student_transcripts_tracking | topk | 66.67% (30/45) | 57.78% (26/45) | -8.9pp |
| tvshow | full-schema | 86.05% (37/43) | 81.40% (35/43) | -4.7pp |
| voter_1 | full-schema | 91.67% (11/12) | 91.67% (11/12) | +0.0pp |
| world_1 | full-schema | 57.14% (48/84) | 48.81% (41/84) | -8.3pp |
| wta_1 | full-schema | 64.44% (29/45) | 53.33% (24/45) | -11.1pp |

## 两轮逐题对比

- 第2轮答对、第1轮答错（检索带来收益）：24 题
- 第2轮答错、第1轮答对（检索引入退化）：47 题
  - [收益] 5f06c74b (cre_Doc_Template_Mgt): Return the different document ids along with the number of p
  - [收益] 8e47b8dd (wta_1): Find the highest rank of losers in all matches.
  - [收益] 333c491a (student_transcripts_tracking): What is the description for the section named h?
  - [收益] 074e8891 (flight_2): How many flights depart from City Aberdeen?
  - [收益] ba8fcd58 (car_1): Which distinctive models are produced by maker with the full
  - [收益] c1c21e0d (course_teach): What is the name of each teacher and what course they teach?
  - [收益] 93acb72d (dog_kennels): Find the distinct breed type and size type combinations for 
  - [收益] d5236ce8 (flight_2): Which airlines have a flight with destination airport AHD?
  - [收益] d89d3a9f (singer): For each singer name, what is the total sales for their song
  - [收益] 5239c600 (dog_kennels): What are each owner's first name and their dogs's name?
  - [退化] 128056e9 (museum_visit): Find the id and name of the museum that has the most staff m
  - [退化] a1b09c6f (student_transcripts_tracking): How is the math course described?
  - [退化] 7f618ee8 (world_1): Give the country codes for countries in which people speak l
  - [退化] 9d6c71c4 (flight_2): List the airport code and name in the city of Anthony.
  - [退化] aa49d3fc (flight_2): What are flight numbers of flights departing from City "Aber
  - [退化] 06801f22 (dog_kennels): Which owner owns the most dogs? List the owner id, first nam
  - [退化] d2195a60 (network_1): Return the names of friends of the high school student Kyle.
  - [退化] 8fbfbf70 (flight_2): What are flight numbers of flights arriving at City "Aberdee
  - [退化] 0ea8a2ab (student_transcripts_tracking): Find the first name of the students who permanently live in 
  - [退化] 09a1e9c0 (student_transcripts_tracking): What is the date and id of the transcript with at least 2 co