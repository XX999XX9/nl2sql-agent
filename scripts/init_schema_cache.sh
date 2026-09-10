#!/usr/bin/env bash
# 首次启动预热：解析表结构 + 构建 Schema 向量索引（data/schema_store/ 持久化）
# 可选参数：--force 强制重建索引（表结构变更或索引损坏时使用）
set -euo pipefail
cd "$(dirname "$0")/../backend"

FORCE=""
[ "${1:-}" = "--force" ] && FORCE="--force"

PYTHON="${PYTHON:-python}"
$PYTHON - "$FORCE" <<'PYEOF'
import sys

force = "--force" in sys.argv

from app.model.factory import ModelFactory
from app.nl2sql.schema_manager import SchemaManager
from app.nl2sql.schema_retriever import SchemaRetriever

sm = SchemaManager()
tables = sm.get_table_names()
print(f"表结构解析完成：{len(tables)} 张表 -> {', '.join(tables)}")

retriever = SchemaRetriever(sm, ModelFactory().create_embedding())
built = retriever.index(force=force)
if built:
    print("Schema 向量索引就绪（data/schema_store/），大库检索模式可用")
else:
    print("Schema 向量索引未建立（库为空或构建失败；小库场景无需索引）")
PYEOF
