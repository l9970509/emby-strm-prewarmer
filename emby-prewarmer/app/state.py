"""运行时状态：失败队列、STRM 指纹、运行历史、日志总线。"""

from __future__ import annotations

import collections
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class LogBus:
    """环形日志缓冲 + 落盘，供面板轮询增量拉取。

    支持三通道：prewarm（预热）/ sync（备份同步）/ strm（STRM 生成），面板分别展示。
    """

    PREWARM = "prewarm"
    SYNC = "sync"
    STRM = "strm"

    def __init__(self, path: Path, maxlen: int = 4000, max_bytes: int = 10 * 1024 * 1024, console: bool = False):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.console = console
        self._lock = threading.Lock()
        self._seq = 0
        self._lines: collections.deque[dict[str, Any]] = collections.deque(maxlen=maxlen)

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                if backup.exists():
                    backup.unlink()
                self.path.rename(backup)
        except OSError:
            pass

    def emit(self, level: str, message: str, channel: str = PREWARM) -> None:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "ts": now_iso(), "level": level, "msg": message, "ch": channel}
            self._lines.append(entry)
            if self.console:
                print(f"{entry['ts']} {level.upper():5s} [{channel}] {message}", flush=True)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{entry['ts']} {level.upper():5s} [{channel}] {message}\n")
            except OSError:
                pass

    def info(self, message: str, channel: str = PREWARM) -> None:
        self.emit("info", message, channel)

    def ok(self, message: str, channel: str = PREWARM) -> None:
        self.emit("ok", message, channel)

    def warn(self, message: str, channel: str = PREWARM) -> None:
        self.emit("warn", message, channel)

    def error(self, message: str, channel: str = PREWARM) -> None:
        self.emit("error", message, channel)

    @staticmethod
    def _channel_of(entry: dict[str, Any]) -> str:
        return str(entry.get("ch") or LogBus.PREWARM)

    def since(self, seq: int, channel: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                entry
                for entry in self._lines
                if entry["seq"] > seq and (channel is None or self._channel_of(entry) == channel)
            ]

    def tail(self, count: int = 200, channel: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            entries = [
                entry
                for entry in self._lines
                if channel is None or self._channel_of(entry) == channel
            ]
            return entries[-count:]

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq


class Store:
    """持久化状态文件 data/state.json。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {
            "failures": {},      # item_id -> 失败记录
            "fingerprints": {},  # item_id -> {path, sha256, size}
            "history": [],       # 最近运行记录
            "summary": {},       # 上次扫描统计
            "meta": {},          # 调度相关（上次定时执行时间等）
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            for key in self.data:
                if key in raw and isinstance(raw[key], type(self.data[key])):
                    self.data[key] = raw[key]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                tmp.write_text(
                    json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                tmp.replace(self.path)
            except OSError:
                pass

    # ------------------------------------------------------------ 失败队列
    def failures_list(self) -> list[dict[str, Any]]:
        with self._lock:
            items = []
            for item_id, record in self.data["failures"].items():
                entry = dict(record)
                entry["id"] = item_id
                entry["next_retry_text"] = fmt_ts(record.get("next_retry_at"))
                items.append(entry)
        items.sort(key=lambda x: (x.get("status") != "pending", x.get("last_failed_at") or 0))
        return items

    def failure_count(self) -> int:
        with self._lock:
            return len(self.data["failures"])

    def record_failure(
        self,
        item_id: str,
        path: str,
        name: str,
        error: str,
        backoff_seconds: float,
        max_attempts: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            failures = self.data["failures"]
            record = failures.get(item_id) or {
                "path": path,
                "name": name,
                "attempts": 0,
                "first_failed_at": time.time(),
                "status": "pending",
            }
            record["path"] = path
            record["name"] = name or record.get("name") or ""
            record["error"] = error
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["last_failed_at"] = time.time()
            record["last_failed_text"] = now_iso()
            record["next_retry_at"] = time.time() + max(30.0, backoff_seconds)
            if max_attempts and record["attempts"] >= max_attempts:
                record["status"] = "dead"
            else:
                record["status"] = "pending"
            failures[item_id] = record
            return dict(record)

    def record_success(self, item_id: str) -> None:
        with self._lock:
            self.data["failures"].pop(item_id, None)

    def due_failures(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        with self._lock:
            return [
                item_id
                for item_id, record in self.data["failures"].items()
                if record.get("status") == "pending" and float(record.get("next_retry_at") or 0) <= now
            ]

    def pending_failure_ids(self, include_dead: bool = False) -> list[str]:
        with self._lock:
            return [
                item_id
                for item_id, record in self.data["failures"].items()
                if include_dead or record.get("status") == "pending"
            ]

    def clear_failures(self) -> int:
        with self._lock:
            count = len(self.data["failures"])
            self.data["failures"] = {}
            return count

    def requeue_failures(self, item_ids: list[str] | None = None) -> int:
        """把失败项重新排队，attempts 归零，等待下一轮自动重试。"""
        with self._lock:
            count = 0
            for item_id, record in self.data["failures"].items():
                if item_ids is not None and item_id not in item_ids:
                    continue
                record["status"] = "pending"
                record["attempts"] = 0
                record["next_retry_at"] = 0
                count += 1
            return count

    def drop_failures(self, item_ids: list[str]) -> int:
        with self._lock:
            count = 0
            for item_id in item_ids:
                if self.data["failures"].pop(item_id, None) is not None:
                    count += 1
            return count

    # -------------------------------------------------------------- 指纹
    def get_fingerprint(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.data["fingerprints"].get(item_id)

    def set_fingerprint(self, item_id: str, path: str, fingerprint: dict[str, Any]) -> None:
        with self._lock:
            self.data["fingerprints"][item_id] = {"path": path, **fingerprint}

    # -------------------------------------------------------------- 历史
    def add_history(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.data["history"].insert(0, entry)
            self.data["history"] = self.data["history"][:50]
        self.save()

    def history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.data["history"])

    # -------------------------------------------------------------- 统计
    def set_summary(self, summary: dict[str, Any]) -> None:
        with self._lock:
            self.data["summary"] = summary
        self.save()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data["summary"])

    def adjust_summary(
        self,
        total: int = 0,
        complete: int = 0,
        pending: int = 0,
    ) -> None:
        """任务结束后对统计做增量修正，让面板数字即时更新。"""
        with self._lock:
            s = self.data["summary"]
            if not s:
                return
            s["total"] = max(0, int(s.get("total", 0)) + total)
            s["complete"] = max(0, int(s.get("complete", 0)) + complete)
            s["pending"] = max(0, int(s.get("pending", 0)) + pending)
        self.save()

    # -------------------------------------------------------------- meta
    def meta_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.data["meta"].get(key, default)

    def meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data["meta"][key] = value
