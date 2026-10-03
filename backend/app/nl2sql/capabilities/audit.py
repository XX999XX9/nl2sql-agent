# 决策审计：每个裁决（allow/deny/故障拒绝/防循环拒绝）落一条结构化 JSON 行日志，
# 含请求、命中规则、原因、纠偏指引、上下文快照。可用于回放与合规追溯。
# 【预埋-暂不实现】AuditSink 的数据库/消息队列实现（AuditSink 接口已预留，
# 未来接入账户体系时可直接增加 DbAuditSink，不影响 Gate 代码）。

import json
import logging
import threading
import time
from pathlib import Path
from typing import Protocol


class AuditSink(Protocol):
    def write(self, entry: dict) -> None: ...


class JsonlAuditSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


class AuditLogger:
    def __init__(self, sink: AuditSink, logger_name: str = "gate"):
        self._sink = sink
        self.log = logging.getLogger(logger_name)

    def record(self, entry: dict) -> None:
        entry = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        try:
            self._sink.write(entry)
        except Exception as e:  # 审计落盘失败不改变裁决，仅记录（裁决本身已 fail-closed）
            self.log.warning("Gate 审计落盘失败: %s", e)
        level = logging.WARNING if entry.get("action") == "deny" else logging.INFO
        self.log.log(
            level,
            "GATE %s [%s] %s:%s rule=%s reason=%s",
            entry.get("action", "?").upper(),
            entry.get("phase", "?"), entry.get("kind"), entry.get("name"),
            entry.get("rule_id"), entry.get("reason", ""),
        )
