# ============================================================
# Spider 测试环境连通性验证脚本
# 用法：
#   python test_env/test_connect.py              # 默认 department_store 库
#   python test_env/test_connect.py baseball_1   # 指定其他 Spider 库
#   python test_env/test_connect.py --host x.x.x.x 库名   # 手动指定宿主 IP
#
# 验证项：① 数据库连通（只读账号） ② SchemaManager 表结构解析
#         ③ SQLExecutor 查询执行  ④ ValueLinker 值采样
#         ⑤ 完整 pipeline 端到端（真实调 LLM 生成 SQL 并执行）
# 本脚本运行前 export 测试环境变量，生产 backend/.env 不受影响
# ============================================================

import os
import re
import subprocess
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)


def load_test_env():
    """加载 test_env/.env.test，并把 DB_HOST 动态解析为当前 WSL 网关（Windows 宿主）IP"""
    env = {}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.test")) as f:
        for line in f:
            m = re.match(r"(\w+)=(.*)", line.strip())
            if m and m.group(2):
                env[m.group(1)] = m.group(2).strip().strip('"')
    # 动态探测网关 IP（覆盖 .env.test 中的静态值，防 WSL 重启漂移）
    if "--host" in sys.argv:
        env["DB_HOST"] = sys.argv[sys.argv.index("--host") + 1]
    else:
        try:
            gw = subprocess.check_output(
                ["ip", "route", "show", "default"], text=True
            ).split()[2]
            env["DB_HOST"] = gw
        except Exception:
            pass  # 探测失败则回退 .env.test 静态值
    return env


TEST_ENV = load_test_env()
# 必须在 import app.* 之前注入环境变量：
# config_handler 导入时加载 backend/.env（不覆盖已有变量），此处优先级最高
for k, v in TEST_ENV.items():
    if v:
        os.environ[k] = v


def check(name, fn):
    try:
        detail = fn()
        print(f"  [PASS] {name}: {detail}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        return False


def main():
    db_name = next((a for a in sys.argv[1:] if not a.startswith("-")), "department_store")
    os.environ["DB_NAME"] = db_name
    print(f"== Spider 测试环境连通性验证 ==\n目标: {os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{db_name} (user={os.environ['DB_USER_NAME']})\n")

    import pymysql
    from app.utils.config_handler import load_yaml
    from app.utils.path_tool import BACKEND_ROOT

    db_cfg = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
    ok = True

    # ① 连通与只读性
    def t1():
        conn = pymysql.connect(
            host=db_cfg["host"], port=int(db_cfg["port"]),
            user=db_cfg["user"], password=db_cfg["password"], database=db_cfg["name"],
        )
        cur = conn.cursor()
        cur.execute("SELECT VERSION()")
        ver = cur.fetchone()[0]
        try:
            cur.execute("CREATE TABLE __perm_probe(id int)")
            conn.rollback()
            raise AssertionError("只读账号可写表！")
        except pymysql.err.OperationalError as e:
            assert e.args[0] == 1142, f"拒绝码异常: {e.args[0]}"
        conn.close()
        return f"MySQL {ver}，只读账号写操作已被数据库层拒绝"

    ok &= check("① 数据库连通+只读性", t1)

    # ② SchemaManager 解析
    from app.nl2sql.schema_manager import SchemaManager

    def t2():
        sm = SchemaManager()
        tables = list(sm._tables.keys())
        assert tables, "未解析到任何表"
        return f"解析 {len(tables)} 张表: {', '.join(sorted(tables)[:6])}{'...' if len(tables) > 6 else ''}"

    ok &= check("② SchemaManager 表结构解析", t2)

    # ③ SQLExecutor 执行（走连接池/超时/截断链路）
    from app.nl2sql.sql_executor import SQLExecutor

    def t3():
        sm = SchemaManager()
        first_table = sorted(sm._tables.keys())[0]   # 动态取库内首张表，适配任意 Spider 库
        ex = SQLExecutor()
        res = ex.execute(f"SELECT * FROM `{first_table}` LIMIT 5")
        assert res["rows"], "查询结果为空"
        return f"SELECT * FROM {first_table} LIMIT 5 -> {len(res['columns'])} 列 × {len(res['rows'])} 行, total={res['total']}"

    ok &= check("③ SQLExecutor 查询执行", t3)

    # ④ ValueLinker 值采样
    from app.nl2sql.value_linker import ValueLinker

    def t4():
        vl = ValueLinker()
        vl._ensure_samples()
        cols = len(vl._samples)
        assert cols > 0, "未采样到任何列值"
        return f"采样 {cols} 个字符串列的 DISTINCT 值（供值链接/纠错诊断）"

    ok &= check("④ ValueLinker 值采样", t4)

    # ⑤ 端到端 pipeline（真实 LLM 调用，session_id=None 不污染会话存储）
    from app.nl2sql.pipeline import NL2SQLPipeline
    from app.api.deps import get_conversation_manager, get_result_cache

    def t5():
        p = NL2SQLPipeline(conversation_manager=get_conversation_manager(), result_cache=get_result_cache())
        sm = SchemaManager()
        first_table = sorted(sm._tables.keys())[0]   # 按库动态提问，适配任意 Spider 库主题
        question = f"{first_table} 表里总共有多少条记录？"
        r = p.run(question, session_id=None)
        assert r.error is None, f"pipeline 报错: {r.error} (kind={r.error_kind})"
        assert r.sql and r.rows, "SQL 未生成或无结果"
        return f"问「{question}」-> intent={r.intent}, SQL={r.sql.strip()[:60]}..., 结果={r.rows[:1]}, 耗时={r.usage['elapsed_ms'] if r.usage else '?'}ms"

    ok &= check("⑤ pipeline 端到端（LLM 生成+执行+解释）", t5)

    print("\n== 结论:", "全部通过 ✓" if ok else "存在失败项 ✗（看上方 [FAIL]）", "==")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
