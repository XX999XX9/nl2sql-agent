# test_env/ — Spider 测试环境（与项目文件隔离）

本目录是针对 Docker 测试集（[cloudera/spiderman](https://github.com/cloudera/spiderman)，Spider 基准 146 个数据库）的测试环境，所有测试专用文件集中在此，**不改动项目任何生产配置**。

## 环境拓扑

```
Ubuntu (项目所在) ──WSL──▶ Windows 宿主 (网关IP: ip route show default)
                              │ :3306 (Docker Desktop 端口发布)
                              ▼
                     容器 spiderman-mysql (MySQL 9.0.0)
                     └─ Spider 测试集 146 库（root / PeterParker）
                        + 只读账号 nl2sql_test（本环境创建，仅 SELECT）
```

- Ubuntu 本机 3306 被生产 MySQL 8.0 占用，**必须走 Windows 宿主网关 IP** 访问 Docker 容器（脚本会自动探测，WSL 重启后 IP 漂移也无需改配置）
- MySQL 9.0 认证插件保持默认 `caching_sha2_password`（PyMySQL ≥1.1 + cryptography 支持，无需也无法改 native_password）

## 文件清单

| 文件 | 作用 |
|---|---|
| `.env.test` | 测试凭据（只读账号 nl2sql_test；gitignore 不入库） |
| `test_connect.py` | 连通性验证 5 项：数据库连通+只读性 / Schema 解析 / 查询执行 / 值采样 / pipeline 端到端（真实 LLM） |
| `run_test_server.sh` | 以测试环境启动后端（默认 8003 端口，不与生产 8002 冲突） |
| `run_eval.py` | **Spider 测试集批量评测**（682 题/20 库）：执行准确率（Spider 官方 multiset 口径）、语法通过率、耗时、token、逐库准确率、纠错统计；默认直接运行=第1轮全量 Schema 加载，`--run {1,2,all}` 可选检索轮/对比报告；`--limit`/`--db` 冒烟或指定库；`--rerun` 清空 latest 重跑（默认断点续跑）；结束时终端打印全部错误题目与原因 |
| `run_fail.py` | **最新版失败题专项复测**：只重跑 `results/latest/detail_fail.jsonl`，强制全量 Schema 加载（不启用检索）；产物写入 `results/latest/fail_test/fail-<时间>/`，同样拆 pass/fail 并给出挽回率 |
| `results/` | 评测产物目录：`latest/{detail_pass,detail_fail}.jsonl+summary.json`（最近一次全量结果，每次全量跑覆盖）、`all_history/`（全量测试历史：`all-<开始时间>/` 普通全量轮、`all-retrieval-<时间>/` 检索轮，均为同三件套）、`history/`（非全量测试历史：`<库名>-<时间>/` 单库、`smoke-<时间>/` 冒烟，每次全新）、`latest/fail_test/fail-<时间>/`（失败题专项复测）、`gold_cache.json` 标准答案缓存（跨次复用）、`report.md` 对比报告 |
| `schema_index/{库名}/` | 第2轮评测的 Schema 向量索引（按库隔离，不污染项目 data/） |
| `relation_index/{库名}/relations.json` | JOIN 关系图谱快照（按 schema 指纹失效，全量/单库/失败复测共享，命中即零重建） |

## 切换原理（为什么不用改项目代码）

`config_handler` 导入时加载 `backend/.env` 但**不覆盖已有环境变量**。测试脚本先 `export` 测试 `DB_*` 再启动/导入，`database.yml` 的 `${DB_*}` 插值即拿到测试值。生产 `.env` 与 `config/` 全程零改动。

## 使用

```bash
# 1. 连通性验证（默认 department_store 库，14 表零售场景）
python test_env/test_connect.py
#    换库：python test_env/test_connect.py baseball_1   # 26 表/55万行，压测用
#    指定宿主IP：python test_env/test_connect.py --host x.x.x.x 库名

# 2. 启动测试后端（默认 8003）
bash test_env/run_test_server.sh
bash test_env/run_test_server.sh baseball_1 8004

# 3. 验证测试服务
curl -X POST http://localhost:8003/api/chat -H "Content-Type: application/json" \
     -d '{"message": "总共有多少个客户？"}'

# 4. Spider 批量评测（682 题/20 库）
python test_env/run_eval.py                 # 全量·全量Schema；中断后直接重跑=断点续跑
python test_env/run_eval.py --rerun         # 清空 latest/ 后全新跑（同时生成 all-<时间>/ 归档）
python test_env/run_eval.py --db car_1      # 单库：产物进 car_1-<时间>/ 全新目录
python test_env/run_fail.py                 # 只复测 latest/detail_fail，强制全量 Schema
```

## 推荐 Spider 测试库

| 库 | 表数 | 行数 | 场景 |
|---|---|---|---|
| `department_store` | 14 | 167 | 默认演示库（经典零售：客户/订单/商品/员工） |
| `baseball_1` | 26 | 550,957 | 大库+大结果集（结果截断/分页/执行耗时观测） |
| `concert_singer` | 4 | 小 | 快速冒烟 |

## 注意事项

- 测试会话会写入 `backend/data/session_store.json`（模块内常量，未做隔离），跑测试后如需清理可调用 `DELETE /api/session` 或删除该文件
- `features.schema_retriever` 保持默认关闭：Spider 单库最多 26 表 < 阈值 30，检索路径会自动退化全量，无需开启
- 完整 146 库清单：`docker exec spiderman-mysql mysql -uroot -pPeterParker -e "SHOW DATABASES"`
