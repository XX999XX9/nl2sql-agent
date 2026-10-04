# NL2SQL

自然语言查询数据库（NL2SQL）智能体：自然语言提问 → 意图分类 → SQL 生成 → AST 校验 → 安全守卫 → JOIN 扇出检测 → 执行 → 自动纠错 → 业务化解释，全流程 SSE 流式呈现。

- 后端：Python 3.10+ / FastAPI / LangGraph（流水线编排）/ SQLAlchemy / sqlglot / ChromaDB
- 前端：原生 HTML/CSS/JS（无框架），本地引入 marked / highlight.js / sql-formatter
- 模型：百炼 DashScope（OpenAI 兼容模式），LLM 与 Embedding 均可配置
- 数据库：MySQL 8.x（只读账号）

## 功能特性

### 对话查询
- 自然语言 → SQL → 结果表格（本地/远端分页、CSV 导出）
- 多轮省略式追问：上轮 SQL 显式注入 Prompt（"查所有商品"→"按月份分组"→"只看库存高于100的"逐轮递进）
- 会话管理：新建/切换/回放/删除；持久化到 `backend/data/session_store.json`，重启不丢

### 意图分流
| 意图 | 行为 |
|---|---|
| query 查询 | 完整链路：生成 → 校验 → 守卫 → JOIN 扇出检测 → 执行 → 结果 |
| dml 增删改 | 仅生成 SQL + 解释，**绝不执行**（不进守卫与执行路径） |
| meta 元信息 | 基于表结构元数据直接回答，不生成 SQL |

### 安全防线（三道保险）
1. **AST 静态校验**（sqlglot）：语法错误、幻觉表/字段（含别名解析、CTE 豁免）拦截
2. **安全守卫**：关键词（词边界 + 剥注释防绕过）+ AST 双重过滤，拦 DROP/DELETE/ALTER/TRUNCATE 等危险语句，SELECT 自动注入强制 LIMIT
3. **只读数据库账号**：即使前两道被突破，数据库层拒绝一切写操作

### 多表查询（JOIN 治理）
- **表关系图谱**（`relation_graph.py`）：显式外键 → 命名约定推断 → 采样数据验证三级建图，自动补全多表 JOIN 路径；按 Schema 指纹持久化快照到 `data/relation_store/`，结构不变重启零重建
- **生成期辅助**：相关表关联关系注入 Prompt，并按图邻接扩展候选表，补全 LLM 漏选的关联表
- **笛卡尔积拦截**（`sql_validator.py`）：AST 静态拦截无 ON/USING 的 JOIN（CROSS/NATURAL JOIN 等显式写法放行）
- **JOIN 扇出检测**（`fanout_checker.py`）：基于一对多基数方向识别"一端 JOIN 多端再聚合导致 COUNT/SUM 行数翻倍"，检出即附修正提示推入纠错循环（同类只纠一次防死循环）

### 准确性增强
- **术语映射**：`config/semantic_map.yml`（42 条），"销售额→SUM(total_money)"、"缺货商品→stock=0"，最长匹配防误替换
- **值链接**：字符串列 DISTINCT 采样（每列上限 200，TTL 1h）+ 滑动窗口模糊匹配，"机器键盘→机械键盘"、"笔记本架→笔记本支架"，对状态/类别等可枚举列效果最好
- **自动纠错**：可纠正失败（校验失败/执行报错/无 ON 笛卡尔积/JOIN 扇出/值不存在导致 0 行）自动回喂 LLM 重写（最多 2 轮，可配）；值缺失时把该列真实取值列表一并回喂；合法 0 行（如库存>99999）不误触

### 大库支持（可选开启）
- 表/字段/注释向量化（Chroma 持久化 `data/schema_store/`），提问时检索 Top-K 相关表注入 Prompt
- 小库（表数 < 30）自动退化为全量拼接；表结构变更（指纹变化）自动重建索引；任何失败降级全量
- 预热脚本 `scripts/init_schema_cache.sh`

### 透明过程
- SSE 逐步推送：意图 / SQL / 执行中 / 纠错中 / 结果 / 解读 / 统计
- 单卡片聚合回复：「回复 / 查询结果」双视图切换，底部耗时 token 摘要行（悬停看分步明细）

### 提示词优化工作台
- 5 种策略（清晰化/加约束/结构化/少样本增强/精简）
- 优化前后行级 diff 对比（绿增红删）
- A/B 用例评估：LLM 生成测试用例 → 双版本执行 → 逐例判定（位置交替防偏置）→ 评分结论
- 一键写回 `prompts/` 文件（自动备份 .bak；路径穿越与元提示词覆盖防护）

## 目录结构

```
nl2sql-agent/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口：路由注册/CORS/静态托管/异常处理
│   │   ├── config.py                # 端口/调试/托管开关
│   │   ├── api/
│   │   │   ├── deps.py              # 全局单例（pipeline/会话/缓存/优化器）
│   │   │   └── routes/              # chat / result / session / schema_info / prompt_optimize
│   │   ├── schemas/                 # Pydantic 请求响应模型 + SSE 事件协议定义
│   │   ├── nl2sql/                  # 核心模块（17 个，含关系图谱/扇出检测，见下文）
│   │   ├── model/factory.py         # LLM / Embedding 客户端工厂
│   │   ├── optimizer/               # 提示词优化（optimizer/evaluator/strategies）
│   │   ├── formatter/               # 输出规范化（normalizer/output_formatter/templates）
│   │   ├── metrics/                 # turn_tracker（耗时/分步埋点）+ token_counter
│   │   └── utils/                   # 路径/日志/配置/文件工具
│   ├── config/                      # YAML 配置（见下文）
│   ├── prompts/                     # 提示词模板（7 个 .txt）
│   ├── data/                        # 运行时数据（不入库）：session_store / schema_store / relation_store
│   ├── logs/                        # 运行日志（按日期滚动）
│   └── run.py                       # uvicorn 启动入口
├── frontend/
│   ├── index.html                   # 对话页
│   ├── prompt_optimize.html         # 提示词优化工作台
│   ├── static/css|js|libs           # 样式/脚本/本地第三方库
│   └── nginx.conf                   # 生产静态托管 + /api 反代
├── scripts/                         # start_backend / start_frontend / init_schema_cache
├── test_env/                        # 本地 Spider 基准评测环境（测试脚本/数据/结果，不入库）
├── docker-compose.yml
└── architecture.txt                 # 架构规范文档
```

> 注：`test_env/`（Spider 评测脚本、测试凭据、评测结果与本地生成的向量/关系索引）、`backend/data/`（运行时数据）、`backend/.env`（密钥）均在 `.gitignore` 中；克隆仓库后需自建 `.env` 配置，评测数据不随仓库分发。

## 环境要求

- Python 3.10+（推荐 conda 环境）
- MySQL 8.x（只读账号）
- 百炼/DashScope API Key

## 配置详解

### backend/.env（密钥与连接，不入库）

```bash
# LLM（百炼 OpenAI 兼容模式）
BAILIAN_API_KEY="sk-xxx"
BAILIAN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
BAILIAN_MODEL="deepseek-v4-flash-0731"          # 主模型（推理模型已适配 reasoning_content）
BAILIAN_EMBEDDING_MODEL="text-embedding-v3"      # 可选：大库检索用

# MySQL（只读账号）
DB_USER_NAME="readonly_user_for_agent"
DB_PASSWORD="xxx"
DB_HOST="127.0.0.1"
DB_PORT="3306"
DB_NAME="test"
```

只读账号建账（管理员执行）：

```sql
CREATE USER 'readonly_user_for_agent'@'%' IDENTIFIED BY 'xxx';
GRANT SELECT ON test.* TO 'readonly_user_for_agent'@'%';
FLUSH PRIVILEGES;
```

> 注意：MySQL 按 `用户@来源IP` 匹配。应用走 TCP 连接，来源是机器 IP；用 `'%'` 或对应 IP 建账号，仅建 `@localhost` 会 Access denied。

### backend/config/*.yml

| 文件 | 配置项 | 说明 | 生效状态 |
|---|---|---|---|
| agent.yml | llm.* | 模型/温度/max_tokens/超时，`${VAR}` 从 .env 插值 | 生效 |
| agent.yml | embedding.* | 向量化模型（大库检索用） | 生效 |
| agent.yml | features.schema_retriever | 大库 Top-K 表检索（true 开启） | **生效** |
| agent.yml | features.metrics_enabled / intent_classifier / value_linker / self_correction / prompt_optimize | 各功能模块开关（默认开） | **生效** |
| agent.yml | features.relation_graph / join_static_check / fanout_check | JOIN 治理三开关：表关联关系图谱 / 无 ON 笛卡尔积拦截 / 扇出检测（默认全开） | **生效** |
| database.yml | url/连接池/执行超时 | MySQL 连接 | 生效 |
| database.yml | force_limit | SELECT 强制 LIMIT | 生效 |
| database.yml | result_row_limit | 结果截断行数 | 生效 |
| nl2sql.yml | correction.max_rounds | 纠错最大轮数（0=关闭） | 生效 |
| nl2sql.yml | schema_retriever.top_k / threshold | 检索表数 / 小库退化阈值 | 生效 |
| nl2sql.yml | relation.infer_by_naming / verify_inferred | 关系图谱命名推断 / 采样验证开关（默认均 true） | 生效 |
| nl2sql.yml | fanout.mode | 扇出检测模式 off / static（默认）/ dynamic | 生效 |
| nl2sql.yml | result.truncate_rows | 预留（结果截断实际读 database.yml result_row_limit） | 预留 |
| semantic_map.yml | 术语映射表 | 表/字段/指标/条件四类 | 生效 |
| optimizer.yml | eval_cases / allow_write_back | 评估用例数 / 写回开关 | 生效 |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| NL2SQL_PORT | 8002 | 服务端口 |
| NL2SQL_DEBUG | false | 热重载（开发） |
| NL2SQL_SERVE_FRONTEND | true | 后端托管前端静态资源（生产 nginx 部署时设 false） |

## 启动服务

### 开发

```bash
# 方式一：脚本（装依赖 + 启动）
bash scripts/start_backend.sh

# 方式二：手动
cd backend
pip install -r requirements.txt
python run.py                                            # A
uvicorn app.main:app --host 0.0.0.0 --port 8002          # B（等价）
```

### 生产

```bash
NL2SQL_SERVE_FRONTEND=false python run.py &     # 后端
bash scripts/start_frontend.sh nginx            # 前端静态托管 + /api 反代 8002
```

### Docker

```bash
docker-compose up -d
```

### 大库模式（表数 ≥ 30 时建议开启）

```bash
# 1. agent.yml: features.schema_retriever: true
# 2. 预热索引（首次或表结构变更后；--force 强制重建）
bash scripts/init_schema_cache.sh
```

## 访问入口

| 入口 | 地址 |
|---|---|
| 对话界面 | http://localhost:8002 |
| 提示词优化工作台 | http://localhost:8002/prompt_optimize.html |
| API 文档（Swagger） | http://localhost:8002/docs |
| 健康检查 | http://localhost:8002/health |

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/chat | 非流式对话（JSON 进 JSON 出） |
| POST | /api/chat/stream | 流式对话（SSE） |
| GET | /api/result/{query_id}?page=&page_size= | 结果分页拉取 |
| POST | /api/session | 创建会话 |
| GET | /api/session | 会话列表 |
| GET | /api/session/{id} | 会话详情（含历史轮次，回放用） |
| DELETE | /api/session/{id} | 删除会话 |
| GET | /api/schema/tables | 数据字典：表结构 |
| GET | /api/schema/semantic-map | 数据字典：语义映射 |
| GET | /api/prompt_optimize/strategies | 优化策略列表 |
| GET | /api/prompt_optimize/files | 可加载/写回的提示词文件 |
| GET | /api/prompt_optimize/file?name= | 读取提示词文件 |
| POST | /api/prompt_optimize | 优化（evaluate=true 附 A/B 评估） |
| POST | /api/prompt_optimize/apply | 写回提示词文件（自动 .bak） |
| GET | /health | 健康检查 |

### SSE 事件协议（/api/chat/stream）

```
data: {"type": "intent",      "data": {"intent": "query|dml|meta"}}
data: {"type": "sql",         "data": {"sql": "SELECT ..."}}
data: {"type": "executing",   "data": {"sql": "SELECT ..."}}
data: {"type": "correcting",  "data": {"round": 1, "error": "...", "sql": "重写后 SQL"}}
data: {"type": "result",      "data": {"query_id", "columns", "rows", "total", "truncated", "executed", "page_size"}}
data: {"type": "explanation", "data": {"text": "业务化中文解释"}}
data: {"type": "usage",       "data": {"elapsed_ms", "total_tokens", "prompt_tokens", "completion_tokens", "steps": [{"name", "elapsed_ms", "total_tokens"}]}}
data: {"type": "error",       "data": {"message", "kind"}}
data: {"type": "done",        "data": {}}
```

## 日志

`backend/logs/` 按日期滚动：`agent_YYYYMMDD.log`（全链路）、`sql_audit_YYYYMMDD.log`（SQL 执行审计）。

## 常见问题

- **页面空白/样式丢失**：HTML 已设 no-cache；如遇缓存请 Ctrl+F5 强刷。
- **数据库 Access denied**：见上文"只读账号建账"的来源 IP 说明。
- **会话丢失**：会话持久化在 `backend/data/session_store.json`；若删除该文件或更换 DB_NAME 则重建。
- **大库检索未生效**：确认 `features.schema_retriever: true` 且已跑预热脚本；表数 < threshold 时有意走全量。
