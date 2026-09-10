# Schema 向量检索（大库场景）：将表名/字段/注释向量化入库（Chroma 持久化），
# 提问时检索 Top-K 相关表注入 Prompt，避免全量 Schema 稀释注意力。
# 小库（表数 < threshold）自动退化为全量拼接快路径；
# 表结构变更（指纹变化）时自动重建索引；任何失败降级为全量，不阻断主链路。
# 开关：config/agent.yml features.schema_retriever（默认关闭，可选开启）

import json
import time
from typing import List, Optional

from app.model.factory import EmbeddingClient
from app.nl2sql.schema_manager import SchemaManager
from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

STORE_DIR = BACKEND_ROOT / "data" / "schema_store"       # 索引与快照持久化目录
COLLECTION = "schema_tables"


class SchemaRetriever:
    """表级向量检索：Top-K 相关表；小库/未开启/故障时返回 None 表示走全量"""

    def __init__(self, schema_manager: SchemaManager, embedding_client: EmbeddingClient):
        self.schema_manager = schema_manager
        self.embedding = embedding_client
        cfg = load_yaml(BACKEND_ROOT / "config" / "nl2sql.yml").get("schema_retriever", {})
        self.top_k = int(cfg.get("top_k", 5))
        self.threshold = int(cfg.get("threshold", 30))   # 表数低于该值退化为全量
        self._indexed = False
        self._collection = None
        self._snapshot_file = STORE_DIR / "snapshot.json"

    # ------------------------------------------------------------------ 索引
    @property
    def collection(self):
        """懒加载 Chroma 持久化集合"""
        if self._collection is None:
            import chromadb
            client = chromadb.PersistentClient(path=str(STORE_DIR / "chroma_index"))
            self._collection = client.get_or_create_collection(
                name=COLLECTION, metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _table_descriptions(self) -> List[tuple]:
        """每张表构造 (表名, 描述文本)：表名+表注释+字段名/类型/注释"""
        items = []
        for tbl in self.schema_manager.get_table_names():
            cols = self.schema_manager.get_table(tbl)
            desc_lines = [f"数据表 {tbl}"]
            for c in cols:
                col_comment = f"（{c['comment']}）" if c["comment"] else ""
                desc_lines.append(f"字段 {c['name']} {c['type']}{col_comment}")
            items.append((tbl, "\n".join(desc_lines)))
        return items

    def index(self, force: bool = False) -> bool:
        """构建/重建向量索引；指纹一致且索引非空则跳过。返回是否真正建立索引"""
        items = self._table_descriptions()
        if not items:
            logger.warning("Schema 检索：库中无表，跳过索引")
            return False

        fingerprint = self.schema_manager.schema_fingerprint()
        if not force and self._snapshot_ok(fingerprint, [t for t, _ in items]):
            self._indexed = True
            logger.info("Schema 向量索引命中快照，跳过重建（%d 张表）", len(items))
            return True

        try:
            tables = [t for t, _ in items]
            vectors = self.embedding.embed([d for _, d in items])
            self.collection.upsert(
                ids=tables,
                embeddings=vectors,
                documents=[d for _, d in items],
                metadatas=[{"table": t} for t in tables],
            )
            self._write_snapshot(fingerprint, tables)
            self._indexed = True
            logger.info("Schema 向量索引构建完成：%d 张表", len(tables))
            return True
        except Exception as e:
            logger.error("Schema 向量索引构建失败（降级全量）: %s", e)
            self._indexed = False
            return False

    # ------------------------------------------------------------------ 快照
    def _snapshot_ok(self, fingerprint: str, tables: List[str]) -> bool:
        """快照指纹一致且向量库中已有这些表 → 索引可复用（重启不重复 embedding）"""
        try:
            if not self._snapshot_file.exists():
                return False
            with open(self._snapshot_file, "r", encoding="utf-8") as f:
                snap = json.load(f)
            if snap.get("fingerprint") != fingerprint:
                return False
            return self.collection.count() >= len(tables)
        except Exception:
            return False

    def _write_snapshot(self, fingerprint: str, tables: List[str]):
        try:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            with open(self._snapshot_file, "w", encoding="utf-8") as f:
                json.dump({
                    "fingerprint": fingerprint,
                    "tables": tables,
                    "indexed_at": time.time(),
                }, f, ensure_ascii=False)
        except Exception as e:
            logger.warning("Schema 索引快照写入失败: %s", e)

    # ------------------------------------------------------------------ 检索
    def select_tables(self, question: str, extra_tables: Optional[List[str]] = None) -> Optional[List[str]]:
        """检索相关问题涉及的表；返回 None 表示走全量快路径：
        - 索引不可用（未成功建立）
        - 小库（表数 < threshold，检索收益低于全量注意力代价）
        命中时返回 Top-K 表名 ∪ extra_tables（多轮场景上轮 SQL 涉及表强制并入）"""
        table_count = len(self.schema_manager.get_table_names())
        if table_count < self.threshold:
            return None

        if not self._indexed:
            self.index()

        if not self._indexed:
            return None    # 索引失败（如 embedding 不可用）→ 降级全量

        try:
            q_vec = self.embedding.embed([question])[0]
            res = self.collection.query(
                query_embeddings=[q_vec],
                n_results=min(self.top_k, self.collection.count()),
            )
            tables = list({m["table"] for m in res["metadatas"][0]})
            for t in extra_tables or []:
                if t in self.schema_manager.get_table_names() and t not in tables:
                    tables.append(t)
            logger.info("Schema 检索命中 %d/%d 张表: %s", len(tables), table_count, tables)
            return tables
        except Exception as e:
            logger.error("Schema 检索失败（降级全量）: %s", e)
            return None
