# NL2SQL

自然语言查询数据库（NL2SQL）智能体：自然语言提问 → 意图分类 → 两阶段生成（LLM 表字段识别裁剪 / 歧义澄清 → function calling 生成 SQL）→ AST 校验 → 安全守卫 → JOIN 扇出检测（含 LLM 误报裁决）→ EXPLAIN 性能门禁 → 只读执行 → 语义/性能双回路自动纠错 → 业务化解释与一致性裁判，全流程 SSE 流式呈现。

- 后端：Python 3.10+ / FastAPI / LangGraph（流水线编排）/ SQLAlchemy / sqlglot / ChromaDB
- 前端：原生 HTML/CSS/JS（无框架），本地引入 marked / highlight.js / sql-formatter
- 模型：百炼 DashScope（OpenAI 兼容模式），LLM 与 Embedding 均可配置；主模型失败自动降级备用模型
- 数据库：MySQL 8.x（只读账号）
- 存储：Redis 8.x（LangGraph 官方检查点 `langgraph-checkpoint-redis`，依赖内置 RedisJSON / RediSearch 模块）；检查点是唯一事实源
- 评测：内置 Spider 基准自动化评测框架 + 可视化评测日志审查台（`test_env/`，脚本、测试数据与历史结果随仓库分发）

## 功能特性

### 对话查询
- 自然语言 → SQL → 结果表格（本地/远端分页、CSV 导出）
- 多轮省略式追问：上轮 SQL 显式注入 Prompt（"查所有商品"→"按月份分组"→"只看库存高于100的"逐轮递进）
- 会话管理：新建/切换/回放/删除；会话与轮次由 LangGraph 检查点（Redis）承载，Redis 目录键只存 id/标题/时间，重启不丢且不自动过期

### 两阶段生成与歧义澄清
- **第一阶段表字段识别**（`table_selector.py`）：LLM 在全量表结构上只输出精简 JSON（needed 确定使用的表+字段 / maybe 可能使用的表），服务端按真实 Schema 反查补全其余字段（不信任模型清单），第二阶段只收裁剪后的上下文；解析失败回退全量 Schema，不阻断
- **歧义澄清**：问题存在无法靠表结构消歧的缺失信息（如"这几天"）时挂起本轮、推送 clarification 事件向用户反问；用户带 clarify_id 回答后恢复暂存继续生成（评测等无人值守场景可关闭，关闭后回退全量不空等）

### Agent 能力治理（稳定性保障，`app/nl2sql/capabilities/`）
- 模型的工具调用走"统一注册表 → SkillToolGate 裁决 → Grant 凭证（决策 ID + 参数哈希 + TTL）→ 执行"唯一链路，代码层面无旁路
- **默认拒绝 + fail-closed**：无规则显式允许（含未注册能力）即拒；规则文件损坏/Gate 异常时能力不可用，降级为只读普通生成
- 声明式规则 `config/gate_rules.yml`（按能力/风险/阶段/角色匹配），改策略不发版；同参重复申请防死循环、工具轮次封顶（末轮强制出 SQL，有界终止）
- 全部裁决落 `logs/gate_audit.jsonl` 审计；预埋人工审批（ask）、角色/权限码、行级数据范围、高中风险规则，接账号体系即可启用
- 当前对模型开放 1 个工具 `get_table_schema`（按名补取表结构，单次 ≤20 张、表存在性二次校验）；5 份 SQL 写作专项规则（JOIN/聚合/Top-N/集合差/日期）全文内联进生成与纠错提示词

### 意图分流
| 意图 | 行为 |
|---|---|
| query 查询 | 完整链路：表字段识别 → 生成 → 校验 → 守卫 → 扇出检测 → EXPLAIN 门禁 → 执行 → 结果 |
| dml 增删改 | 仅生成 SQL + 解释，**绝不执行**（不进守卫与执行路径） |
| meta 元信息 | 基于表结构元数据直接回答，不生成 SQL |

### 安全防线（三道保险）
1. **AST 静态校验**（sqlglot）：语法错误、幻觉表/字段（含别名解析、CTE 豁免）拦截
2. **安全守卫**：关键词（词边界 + 剥注释防绕过）+ AST 双重过滤，拦 DROP/DELETE/ALTER/TRUNCATE 等危险语句，SELECT 自动注入强制 LIMIT（默认 1000；可经 `NL2SQL_FORCED_LIMIT=0` 关闭，评测时两侧对称不截断）
3. **只读数据库账号**：即使前两道被突破，数据库层拒绝一切写操作

### 多表查询（JOIN 治理）
- **表关系图谱**（`relation_graph.py`）：显式外键 → 命名约定推断 → 采样数据验证三级建图，自动补全多表 JOIN 路径；按 Schema 指纹持久化快照到 `data/relation_store/`，结构不变重启零重建
- **生成期辅助**：相关表关联关系注入 Prompt，并按图邻接扩展候选表，补全 LLM 漏选的关联表
- **笛卡尔积拦截**（`sql_validator.py`）：AST 静态拦截无 ON/USING 的 JOIN（CROSS/NATURAL JOIN 等显式写法放行）
- **JOIN 扇出检测**（`fanout_checker.py`）：基于一对多基数方向识别"一端 JOIN 多端再聚合导致 COUNT/SUM 行数翻倍"，static 零库开销提示 / dynamic 计数确认放大倍数；静态命中后经 **LLM 二次裁决**误报可豁免，确有风险则附修正提示推入纠错循环（同类只纠一次防死循环）

### EXPLAIN 性能门禁（`explain_gate.py`）
- 执行前对最终 SQL 跑 EXPLAIN（不实际执行）：大表（估算 ≥1 万行）type=ALL 且 key=NULL（全表扫描无索引）即拦截
- 执行计划 + 可用索引名回喂 LLM 做性能优化重写，**独立计数（opt_rounds ≤ 2，不挤占语义纠错轮次）**
- 红线：只能让过滤/JOIN 命中索引，严禁增减 LIMIT、弱化过滤、删 DISTINCT/GROUP BY 等改变结果集的手段；第 3 次仍命中也放行执行（可用性优先），EXPLAIN 自身失败放行不阻断

### 准确性增强
- **术语映射**：`config/semantic_map.yml`（42 条），"销售额→SUM(total_money)"、"缺货商品→stock=0"，最长匹配防误替换
- **值链接**：低基数字符串列先 COUNT(DISTINCT)，>400 的高基数列整列放弃；≤400 的列采样至多 200 个值（TTL 1h），实体内部滑窗 + 精确短路 + 双向覆盖率模糊匹配，"机器键盘→机械键盘"；仅中文/编号类问题生效，歧义多候选宁可不猜
- **自动纠错**：可纠正失败（校验失败/执行报错/无 ON 笛卡尔积/JOIN 扇出/值不存在导致 0 行）自动回喂 LLM 重写（最多 2 轮，可配）；值缺失时把该列真实取值列表一并回喂；合法 0 行（如库存>99999）不误触；重写产物全链路复检
- **模型容错**：temperature=0；空正文按 4×/16× 自动扩容重试；主模型故障整体切换降级模型重试一次

### 大库支持（可选开启）
- 表/字段/注释向量化（Chroma 持久化 `data/schema_store/`），提问时检索 Top-K 相关表注入 Prompt
- 小库（表数 < 30）自动退化为全量拼接；表结构变更（指纹变化）自动重建索引；任何失败降级全量
- 预热脚本 `scripts/init_schema_cache.sh`

### 业务解读与裁判
- SQL 业务化中文解释（用了哪些表、什么条件、如何聚合排序），失败只跳过不阻断
- 解读节点同时输出"SQL 是否符合用户问题"裁判标签 conform/risk/unknown（白名单校验，非法标签降级 unknown），前端徽章展示

### 透明过程
- SSE 逐步推送：意图 / 澄清 / SQL / 执行中 / 纠错中（语义/性能，含轮次与原因）/ 结果 / 解读（含裁判标签）/ 统计
- 单卡片聚合回复：「回复 / 查询结果」双视图切换，底部耗时 token 摘要行（悬停看分步明细）

### 多用户与访问控制
- 登录只输入用户名（无密码），身份存 `localStorage`，所有请求经 `X-User-Id` 请求头携带；后端据此隔离会话、检查点线程与 Trace（线程 id = `user_id:session_id`）
- 顶栏入口统一为纯文字：映射表 / 日志 / 切换用户；Trace 入口在评测日志页右上角
- 评测日志审查台需输入访问口令（当前固定 `666`），验证结果存 `sessionStorage`，同一标签页内进入 Trace 及返回不再重复验证

### 事实源与 Trace
- 唯一事实源是 LangGraph 检查点（Redis + 官方 `RedisSaver`）：会话记录、Trace、断点续跑都从同一份检查点投影，Redis 目录键不存问答正文，避免双写不一致
- **会话视图**投影检查点状态中的 `turns`（DeltaChannel 增量通道，只保留提问与回答）；**Trace 视图**投影同一线程的检查点历史（节点、状态、事件、下一步）
- 检查点写入开启 AOF 落盘；每轮 SSE `done` 事件回传 `turn_id`，供前端"引用历史轮"使用

### 评测体系（`test_env/`，详见目录内注释）
- Spider 682 题 / 20 库批量评测：按库分组、断点续跑、冒烟限量、全量 Schema vs 向量检索两轮对比报告、gold 结果跨轮缓存、失败题一键重跑（run_fail.py）
- 执行准确率口径：值规范化（Decimal 控精度、日期转 ISO）后无序 multiset 比对，行序无关；列数相同前提下仅列顺序不同判等价（明细打 col_reordered 标记可审计）；评测环境 pred/gold 两侧均不截断
- **评测日志审查台**（`eval_logs.html`）：15 类错误标签筛选、逐题 gold/pred SQL 与结果集左右并排对比

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
│   │   │   └── routes/              # chat / result / session / traces / schema_info / prompt_optimize / eval_logs
│   │   ├── schemas/                 # Pydantic 请求响应模型 + SSE 事件协议定义
│   │   ├── nl2sql/                  # 核心模块（见下文），含 capabilities/ 能力治理子包
│   │   ├── persistence/             # 事实源接入：redis_store（连接/键空间）/ redis_checkpointer（官方 RedisSaver 适配）/ store（会话与 Trace 投影）
│   │   ├── model/factory.py         # LLM / Embedding 客户端工厂（空正文扩容+降级模型）
│   │   ├── optimizer/               # 提示词优化（optimizer/evaluator/strategies）
│   │   ├── formatter/               # 输出规范化（normalizer/output_formatter/templates）
│   │   ├── metrics/                 # turn_tracker（耗时/分步埋点）+ token_counter
│   │   └── utils/                   # 路径/日志/配置/文件工具
│   ├── config/                      # YAML 配置（含 gate_rules.yml 能力准入规则，见下文）
│   ├── prompts/                     # 提示词模板（.txt）+ skills/ SQL 写作专项规则（5 个 .md）
│   ├── data/                        # 运行时数据（不入库）：schema_store / relation_store（会话与 Trace 已迁至 Redis，session_store.json 为遗留文件）
│   ├── logs/                        # 运行日志（按日期滚动）+ gate_audit.jsonl 能力裁决审计
│   └── run.py                       # uvicorn 启动入口
├── frontend/
│   ├── login.html                   # 登录页（仅输入用户名）
│   ├── index.html                   # 对话页（顶栏：映射表 / 日志 / 切换用户）
│   ├── eval_logs.html               # 评测日志审查台（口令门 + 错误筛选 + gold/pred 左右对比，右上角 Trace 入口）
│   ├── traces.html                  # Trace 页（检查点历史：节点/状态/事件/下一步）
│   ├── prompt_optimize.html         # 提示词优化工作台
│   ├── static/css|js|libs           # 样式/脚本/本地第三方库
│   └── nginx.conf                   # 生产静态托管 + /api 反代
├── scripts/                         # start_backend / start_frontend / init_schema_cache
├── test_env/                        # 本地 Spider 基准评测环境（run_eval/run_fail/测试服务脚本/历史结果；脚本与结果入库）
├── test_queries.csv                 # Spider 测试集（682 题 / 20 库）
├── docs/                            # 项目文档（项目说明书、链路图、改造报告等，多数不入库）
├── docker-compose.yml
└── architecture.txt                 # 文件级架构地图
```

> 注：`backend/data/`（运行时数据与本地生成的向量/关系索引）、`backend/.env`（密钥）、`test_env/.env.test`（评测凭据）在 `.gitignore` 中；`test_env/` 的评测脚本、测试数据与历史结果随仓库分发。

## 环境要求

- Python 3.10+（推荐 conda 环境）
- MySQL 8.x（只读账号）
- Redis 8.x（需内置 RedisJSON / RediSearch；`docker-compose up -d redis` 或 apt 源安装 Redis 8 均可）
- 百炼/DashScope API Key

## 配置详解

### backend/.env（密钥与连接，不入库）

```bash
# LLM（百炼 OpenAI 兼容模式）
BAILIAN_API_KEY="sk-xxx"
BAILIAN_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
BAILIAN_MODEL="deepseek-v4-flash-0731"          # 主模型（推理模型已适配 reasoning_content）
BAILIAN_FALLBACK_MODEL="qwen-plus"              # 降级模型（主模型失败/空正文扩容失败后重试一次）
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

### Redis（检查点事实源）

```bash
# 方式一：Docker（仓库自带 compose，镜像 redis:8，AOF 落盘）
docker-compose up -d redis

# 方式二：本机安装 Redis 8 后直接启动（需 appendonly 落盘）
redis-server --appendonly yes
```

验证模块就绪：`redis-cli MODULE LIST` 应包含 `ReJSON`、`search`，且 `redis-cli FT._LIST` 可执行（官方检查点依赖这两个模块建索引）。
连接地址与前缀通过 `NL2SQL_REDIS_URL`、`NL2SQL_REDIS_PREFIX` 覆盖；旧版 Redis 6/7（无上述模块）无法运行。

### backend/config/*.yml

| 文件 | 配置项 | 说明 | 生效状态 |
|---|---|---|---|
| agent.yml | llm.* | 模型/温度/max_tokens/超时，`${VAR}` 从 .env 插值 | 生效 |
| agent.yml | embedding.* | 向量化模型（大库检索用） | 生效 |
| agent.yml | features.schema_retriever | 大库 Top-K 表检索（true 开启） | **默认关** |
| agent.yml | features.metrics_enabled / intent_classifier / value_linker / self_correction / prompt_optimize | 各功能模块开关（默认开） | **生效** |
| agent.yml | features.relation_graph / join_static_check / fanout_check | JOIN 治理三开关：表关联关系图谱 / 无 ON 笛卡尔积拦截 / 扇出检测（默认全开） | **生效** |
| agent.yml | features.explain_gate | EXPLAIN 性能门禁（大表全表扫描拦截 + 独立性能优化轮；环境变量 NL2SQL_EXPLAIN_GATE 可覆盖，评测默认关） | **生效** |
| agent.yml | features.sql_explainer / clarification | 业务解读+裁判标签 / 表筛选歧义澄清（均可用环境变量关闭；评测默认关，澄清关闭后回退全量不空等） | **生效** |
| gate_rules.yml | rules / meta.fail_closed / approval_enabled | 能力（工具）准入声明式规则：默认拒绝、fail-closed、高风险转人工审批（预埋） | **生效** |
| database.yml | url/连接池/执行超时 | MySQL 连接 | 生效 |
| database.yml | forced_limit | SELECT 强制 LIMIT（默认 1000；环境变量 NL2SQL_FORCED_LIMIT 可覆盖，0=不追加） | 生效 |
| database.yml | result_row_limit | 结果截断行数（默认 1000；环境变量 NL2SQL_RESULT_ROW_LIMIT 可覆盖，0=不截断） | 生效 |
| nl2sql.yml | correction.max_rounds | 纠错最大轮数（0=关闭） | 生效 |
| nl2sql.yml | schema_retriever.top_k / threshold | 检索表数 / 小库退化阈值 | 生效 |
| nl2sql.yml | relation.infer_by_naming / verify_inferred | 关系图谱命名推断 / 采样验证开关（默认均 true） | 生效 |
| nl2sql.yml | fanout.mode | 扇出检测模式 off / static（默认）/ dynamic | 生效 |
| nl2sql.yml | explain.large_table_rows / max_opt_rounds | EXPLAIN 大表判定行数（1 万）/ 性能优化独立轮次上限（2） | 生效 |
| nl2sql.yml | capabilities.max_cap_rounds | 单次生成内模型工具调用轮次封顶（末轮强制出 SQL） | 生效 |
| semantic_map.yml | 术语映射表 | 表/字段/指标/条件四类 | 生效 |
| optimizer.yml | eval_cases / allow_write_back | 评估用例数 / 写回开关 | 生效 |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| NL2SQL_PORT | 8002 | 服务端口 |
| NL2SQL_DEBUG | false | 热重载（开发） |
| NL2SQL_SERVE_FRONTEND | true | 后端托管前端静态资源（生产 nginx 部署时设 false） |
| NL2SQL_FORCED_LIMIT | 未设置（取 yml=1000） | 安全守卫强制 LIMIT 行数；**0=不追加**（评测/测试服务默认 0） |
| NL2SQL_RESULT_ROW_LIMIT | 未设置（取 yml=1000） | 执行结果 fetch 后截断行数；**0=不截断**（评测/测试服务默认 0） |
| NL2SQL_SQL_EXPLAINER | 未设置（取 features=true） | 业务解读+裁判开关：on/off（评测脚本默认 off） |
| NL2SQL_EXPLAIN_GATE | 未设置（取 features=true） | EXPLAIN 性能门禁开关：on/off（评测脚本默认 off） |
| NL2SQL_CLARIFICATION | 未设置（取 features=true） | 歧义澄清反问开关：on/off（评测脚本默认 off） |
| NL2SQL_REDIS_URL | redis://localhost:6379/0 | 检查点与会话目录所在 Redis |
| NL2SQL_REDIS_PREFIX | nl2sql | Redis 键空间前缀（官方检查点前缀不能含冒号） |
| NL2SQL_CHECKPOINTING | 未设置（开启） | 检查点开关；off 时返回 None（批量评测不需要 Trace 时可关） |
| BAILIAN_FALLBACK_MODEL | qwen-plus | 降级模型：主模型异常/扩容后仍空响应时整体重试一次 |

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
cp backend/.env.example backend/.env 2>/dev/null || true   # 首次：填写 LLM / MySQL 凭据
docker compose up -d --build                                # redis + backend + frontend
```

- `backend/.env` 由 compose 的 `env_file` 注入容器（密钥不写进镜像）
- 前端由 nginx 容器托管并反代 `/api`，后端设 `NL2SQL_SERVE_FRONTEND=false`
- 访问 http://localhost/（后端 API 直连端口为 8002）

### 大库模式（表数 ≥ 30 时建议开启）

```bash
# 1. agent.yml: features.schema_retriever: true
# 2. 预热索引（首次或表结构变更后；--force 强制重建）
bash scripts/init_schema_cache.sh
```

## 访问入口

| 入口 | 地址 |
|---|---|
| 登录页 | http://localhost:8002/login.html （未登录访问其他页面自动跳转） |
| 对话界面 | http://localhost:8002 |
| 评测日志审查台 | http://localhost:8002/eval_logs.html （需口令 666） |
| Trace 页 | http://localhost:8002/traces.html?run_id=... （从评测日志页右上角进入） |
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
| GET | /api/traces?session_id= | 该用户的运行（Trace）列表 |
| GET | /api/traces/{run_id} | 单次运行的检查点历史投影（节点/状态/事件/下一步） |
| GET | /api/schema/tables | 数据字典：表结构 |
| GET | /api/schema/semantic-map | 数据字典：语义映射 |
| GET | /api/prompt_optimize/strategies | 优化策略列表 |
| GET | /api/prompt_optimize/files | 可加载/写回的提示词文件 |
| GET | /api/prompt_optimize/file?name= | 读取提示词文件 |
| POST | /api/prompt_optimize | 优化（evaluate=true 附 A/B 评估） |
| POST | /api/prompt_optimize/apply | 写回提示词文件（自动 .bak） |
| GET | /api/eval/runs | 评测日志审查台：历次评测批次列表与汇总（只读，路径约束防穿越） |
| GET | /api/eval/run | 单批次逐题明细（错误分类筛选、结果集预览瘦身） |
| GET | /health | 健康检查 |

### SSE 事件协议（/api/chat/stream）

```
data: {"type": "intent",        "data": {"intent": "query|dml|meta"}}
data: {"type": "clarification", "data": {"clarify_id": "...", "questions": ["..."]}}   # 需用户补充，本轮挂起
data: {"type": "sql",           "data": {"sql": "SELECT ..."}}
data: {"type": "executing",     "data": {"sql": "SELECT ..."}}
data: {"type": "correcting",    "data": {"round": 1, "kind": "semantic|perf", "error": "...", "sql": "重写后 SQL"}}
data: {"type": "result",        "data": {"query_id", "columns", "rows", "total", "truncated", "executed", "page_size"}}
data: {"type": "explanation",   "data": {"text": "业务化中文解释", "label": "conform|risk|unknown"}}
data: {"type": "usage",         "data": {"elapsed_ms", "total_tokens", "prompt_tokens", "completion_tokens", "steps": [{"name", "elapsed_ms", "total_tokens"}]}}
data: {"type": "error",         "data": {"message", "kind"}}
data: {"type": "done",          "data": {"turn_id?", "clarify_id?", "need_clarification?"}}
```

## 日志与审计

- `backend/logs/` 按日期滚动：`agent_YYYYMMDD.log`（全链路，保留 30 天）
- `backend/logs/gate_audit_YYYYMMDD.jsonl`：能力 Gate 裁决审计（allow/deny/bypass、规则 ID、参数、上下文快照）

## 基准评测成绩（test_env/，Spider 子集 682 题 / 20 库）

| 指标 | 最近一次全量实测（2026-10-05，全量 Schema、无截断） |
|---|---|
| 执行准确率 | **94.1%（642/682）**；其中 48 题为"仅列顺序不同"的等价宽容（严格列序口径 87.1%） |
| 最终 SQL 语法通过率 | **100%** |
| 流水线崩溃 / gold 不可执行 | 0 / 0 |
| 单题延迟 | 中位 8.1s，均值 12.3s |

评测运行方式见 `test_env/run_eval.py` 文件头注释（断点续跑 / `--db` / `--limit` / `--rerun` / 两轮对比），结果在评测日志审查台按错误类型筛选审查。

## 常见问题

- **页面空白/样式丢失**：HTML 已设 no-cache；如遇缓存请 Ctrl+F5 强刷。
- **数据库 Access denied**：见上文"只读账号建账"的来源 IP 说明。
- **会话丢失**：会话与轮次存在 Redis 检查点里；清空 Redis 或更换 `NL2SQL_REDIS_PREFIX` 会重建，旧数据不可恢复。
- **历史会话/Trace 打不开**：本轮已从自研检查点切换到官方 `langgraph-checkpoint-redis`，键布局不兼容，切换前产生的会话与 Trace 无法读取（建议清掉旧 `nl2sql:ckpt:*` / `nl2sql:writes:*`）。
- **Redis 启动报缺少索引/模块**：确认使用 Redis 8（内置 ReJSON/search），旧版 Redis 6/7 无法加载官方检查点依赖的模块。
- **大库检索未生效**：确认 `features.schema_retriever: true` 且已跑预热脚本；表数 < threshold 时有意走全量。
