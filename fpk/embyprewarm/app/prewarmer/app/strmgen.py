"""STRM 生成：把 OpenList 网盘目录同步成本地 .strm 文件。

参考 OpenStrm 的「前缀 + 远端路径」方案：
- strm 内容 = 前缀（OpenList 的 /d 地址或 rclone 挂载点）+ 网盘相对路径；
- 目录结构与网盘一致，`影片.mkv` → `影片.strm`；
- 增量：本地已存在且内容一致的 strm 跳过，不重复写盘；
- 可选一并下载 nfo / 字幕 / 海报等小文件（按扩展名白名单 + 大小上限）；
- 重建模式：网盘已删除的影片，同步删除本地多余 strm；
- 生成完成后可自动触发增量预热，配合「预热后自动备份同步」形成闭环：
  网盘更新 → 生成 STRM → 自动预热 → 自动备份同步。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import ConfigStore
from .openlist import OpenListClient, OpenListError
from .state import LogBus, Store

# 预览/通知明细上限（超出只给统计）
PLAN_ITEM_LIMIT = 1000
NOTIFY_DETAIL_LIMIT = 80
# 单次执行写入的详细日志上限，避免几万条刷屏
LOG_DETAIL_LIMIT = 300

DEFAULT_MEDIA_EXTS = "mkv,mp4,ts,m2ts,iso,avi,wmv,mov,flv,rmvb,webm,mpg,mpeg"
DEFAULT_EXTRA_EXTS = "nfo,srt,ass,ssa,sub,sup,vtt,jpg,jpeg,png,webp"


def parse_exts(text: str, fallback: str = "") -> set[str]:
    """把 "mkv, mp4" 解析成 {".mkv", ".mp4"}。"""
    raw = (text or "").strip() or fallback
    out: set[str] = set()
    for part in raw.replace("，", ",").split(","):
        ext = part.strip().lower().lstrip("*")
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        out.add(ext)
    return out


def _rel_dir(remote_root: str, remote_path: str) -> str:
    """远端文件相对任务根目录的目录部分（不含文件名）。"""
    root = "/" + remote_root.strip("/")
    path = remote_path if remote_path.startswith("/") else "/" + remote_path
    if path == root:
        return ""
    if path.startswith(root + "/"):
        return path[len(root) + 1:].rsplit("/", 1)[0]
    return path.strip("/").rsplit("/", 1)[0]


class StrmGenerator:
    """STRM 生成器：线程安全，同一时间只跑一个任务。"""

    def __init__(self, config: ConfigStore, store: Store, bus: LogBus):
        self.config = config
        self.store = store
        self.bus = bus
        self.engine = None  # 由 run.py 注入，用于通知与生成后预热
        self._lock = threading.Lock()
        self._running = False
        self._stop = threading.Event()

    # ---------------------------------------------------------------- 对外
    @property
    def running(self) -> bool:
        return self._running

    def stop(self) -> bool:
        if not self._running:
            return False
        self._stop.set()
        self.bus.warn("已请求停止 STRM 生成，等待当前文件处理完…", LogBus.STRM)
        return True

    def last_result(self) -> dict[str, Any]:
        return dict(self.store.meta_get("strm_last") or {})

    def plan(self) -> dict[str, Any]:
        """生成预览：只对比不写盘，返回将创建/更新/跳过的明细。"""
        cfg = self.config.get()
        try:
            client, tasks = self._prepare(cfg)
            if not tasks:
                return {"ok": False, "error": "未配置同步任务（网盘目录 → 本地目录）"}
            # 先验证连接/登录，避免"预览成功但一条也读不到"
            client.ensure_token()
        except OpenListError as exc:
            return {"ok": False, "error": str(exc)}

        total = {"create": 0, "update": 0, "skip": 0, "delete": 0, "extra": 0, "extra_skip": 0}
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for task in tasks:
            built = self._build_task(cfg, client, task, errors, preview=True)
            for key in total:
                total[key] += built["counts"].get(key, 0)
            for item in built["items"]:
                if len(items) < PLAN_ITEM_LIMIT:
                    items.append(item)

        changed = total["create"] + total["update"] + total["delete"] + total["extra"]
        if not changed and not total["skip"] and errors:
            return {"ok": False, "error": errors[0], "errors": errors[:5]}
        return {
            "ok": True,
            "empty": changed == 0,
            "tasks": len(tasks),
            "prefix": cfg["strm"].get("prefix") or "",
            "items": items,
            "errors": errors[:5],
            "truncated": sum(total.values()) > len(items),
            "note": "没有需要生成/更新的文件" if changed == 0 else "",
            **total,
        }

    def run(self, reason: str = "手动") -> dict[str, Any]:
        """执行一次生成（阻塞），返回结果摘要。"""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "STRM 生成正在进行中，请稍后再试"}
            self._running = True
            self._stop.clear()
        try:
            return self._run(reason)
        finally:
            self._running = False

    def run_async(self, reason: str = "手动") -> None:
        threading.Thread(target=self.run, args=(reason,), daemon=True, name="strm").start()

    # ---------------------------------------------------------------- 内部
    def _prepare(self, cfg: dict[str, Any]) -> tuple[OpenListClient, list[dict[str, str]]]:
        strm = cfg.get("strm") or {}
        client = OpenListClient(
            strm.get("openlist_url") or "",
            token=strm.get("openlist_token") or "",
            username=strm.get("openlist_username") or "",
            password=strm.get("openlist_password") or "",
            timeout=30,
        )
        tasks = [t for t in (strm.get("tasks") or []) if t.get("remote") and t.get("local")]
        return client, tasks

    def _build_task(
        self,
        cfg: dict[str, Any],
        client: OpenListClient,
        task: dict[str, str],
        errors: list[str],
        preview: bool = False,
    ) -> dict[str, Any]:
        """扫描一个任务：遍历远端、对比本地，算出动作清单。"""
        strm = cfg["strm"]
        media_exts = parse_exts(strm.get("exts") or "", DEFAULT_MEDIA_EXTS)
        extra_exts = parse_exts(strm.get("extras") or "", "")
        prefix = strm.get("prefix") or ""
        local_root = Path(task["local"])
        remote_root = task["remote"]

        counts = {"create": 0, "update": 0, "skip": 0, "delete": 0, "extra": 0, "extra_skip": 0}
        items: list[dict[str, Any]] = []
        writes: list[dict[str, Any]] = []
        downloads: list[dict[str, Any]] = []
        keep: set[str] = set()

        files, walk_errors = client.walk(remote_root)
        if walk_errors:
            errors.extend(walk_errors[:3])

        for entry in files:
            name = entry["name"]
            ext = os.path.splitext(name)[1].lower()
            rel_dir = _rel_dir(remote_root, entry["path"])
            if ext in media_exts:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                strm_rel = os.path.splitext(rel)[0] + ".strm"
                target = local_root / strm_rel
                content = OpenListClient.build_url(prefix, entry["path"]) + "\n"
                keep.add(str(target))
                action = "create"
                if target.exists():
                    try:
                        old = target.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        old = ""
                    action = "skip" if old.strip() == content.strip() else "update"
                counts[action] += 1
                writes.append(
                    {
                        "action": action,
                        "path": target,
                        "content": content,
                        "rel": strm_rel,
                        "size": entry["size"],
                        "remote": entry["path"],
                    }
                )
                if len(items) < PLAN_ITEM_LIMIT:
                    items.append(
                        {
                            "rel": strm_rel,
                            "action": action,
                            "size": entry["size"],
                            "remote": entry["path"],
                        }
                    )
            elif extra_exts and ext in extra_exts:
                rel = f"{rel_dir}/{name}" if rel_dir else name
                target = local_root / rel
                if target.exists():
                    counts["extra_skip"] += 1
                    continue
                counts["extra"] += 1
                downloads.append({"path": target, "rel": rel, "remote": entry["path"], "size": entry["size"]})
                if len(items) < PLAN_ITEM_LIMIT:
                    items.append(
                        {"rel": rel, "action": "extra", "size": entry["size"], "remote": entry["path"]}
                    )

        # 重建模式：删除网盘已不存在的本地 strm
        deletes: list[Path] = []
        if strm.get("rebuild") and local_root.is_dir():
            for path in local_root.rglob("*.strm"):
                if str(path) in keep:
                    continue
                # 只处理本次任务目录范围内的文件
                deletes.append(path)
                if len(items) < PLAN_ITEM_LIMIT:
                    items.append(
                        {
                            "rel": str(path.relative_to(local_root).as_posix()),
                            "action": "delete",
                            "size": 0,
                            "remote": "",
                        }
                    )
            counts["delete"] = len(deletes)

        return {
            "task": task,
            "counts": counts,
            "items": items,
            "writes": writes,
            "downloads": downloads,
            "deletes": deletes,
            "files": len(files),
            "preview": preview,
        }

    def _run(self, reason: str) -> dict[str, Any]:
        started = time.time()
        cfg = self.config.get()
        strm = cfg.get("strm") or {}

        try:
            client, tasks = self._prepare(cfg)
            if not tasks:
                return self._finish(started, reason, ok=False, error="未配置同步任务")
            client.ensure_token()
        except OpenListError as exc:
            return self._finish(started, reason, ok=False, error=str(exc))

        prefix = strm.get("prefix") or ""
        self.bus.info(
            f"📼 开始生成 STRM（{reason}）：{len(tasks)} 个任务 · 前缀 {prefix or '(未设置)'}",
            LogBus.STRM,
        )

        created = updated = skipped = deleted = extras = extras_skipped = failed = 0
        files_seen = 0
        errors: list[str] = []
        details: list[dict[str, str]] = []
        log_lines = 0

        for task in tasks:
            local_root = Path(task["local"])
            self.bus.info(f"　└─ 扫描网盘目录 {task['remote']} → {local_root}", LogBus.STRM)
            built = self._build_task(cfg, client, task, errors)
            files_seen += built["files"]
            counts = built["counts"]
            created += counts["create"]
            updated += counts["update"]
            skipped += counts["skip"]
            deleted += counts["delete"]
            extras += counts["extra"]
            extras_skipped += counts["extra_skip"]

            for item in built["writes"]:
                if self._stop.is_set():
                    errors.append("任务被手动停止")
                    break
                if item["action"] == "skip":
                    continue
                try:
                    item["path"].parent.mkdir(parents=True, exist_ok=True)
                    tmp = item["path"].with_suffix(item["path"].suffix + ".tmp")
                    tmp.write_text(item["content"], encoding="utf-8")
                    os.replace(tmp, item["path"])
                    if log_lines < LOG_DETAIL_LIMIT:
                        mark = "新增" if item["action"] == "create" else "更新"
                        self.bus.ok(f"　　✓ {mark} {item['rel']}", LogBus.STRM)
                        log_lines += 1
                    details.append(
                        {
                            "rel": item["rel"],
                            "name": item["rel"],
                            "status": "ok",
                            "action": item["action"],
                        }
                    )
                except OSError as exc:
                    failed += 1
                    if len(errors) < 8:
                        errors.append(f"{item['rel']}：{exc}")
                    self.bus.warn(f"　　✗ 写入失败 {item['rel']}：{exc}", LogBus.STRM)

            max_bytes = int(strm.get("max_file_mb") or 0) * 1024 * 1024
            for item in built["downloads"]:
                if self._stop.is_set():
                    errors.append("任务被手动停止")
                    break
                if max_bytes and item["size"] > max_bytes:
                    extras_skipped += 1
                    continue
                try:
                    item["path"].parent.mkdir(parents=True, exist_ok=True)
                    client.download(item["remote"], item["path"], max_bytes=max_bytes)
                    if log_lines < LOG_DETAIL_LIMIT:
                        self.bus.ok(f"　　⤓ 附件 {item['rel']}", LogBus.STRM)
                        log_lines += 1
                except OpenListError as exc:
                    extras -= 1
                    extras_skipped += 1
                    if len(errors) < 8:
                        errors.append(f"附件 {item['rel']}：{exc}")
                    self.bus.warn(f"　　✗ 附件失败 {item['rel']}：{exc}", LogBus.STRM)

            for path in built["deletes"]:
                try:
                    path.unlink()
                    if log_lines < LOG_DETAIL_LIMIT:
                        self.bus.info(f"　　− 删除 {path.name}", LogBus.STRM)
                        log_lines += 1
                except OSError as exc:
                    failed += 1
                    if len(errors) < 8:
                        errors.append(f"删除 {path}：{exc}")

        note = (
            f"新增 {created} / 更新 {updated} / 跳过 {skipped}"
            + (f" / 删除 {deleted}" if deleted else "")
            + (f" / 附件 {extras}" if extras or extras_skipped else "")
            + (f" / 失败 {failed}" if failed else "")
        )
        level_ok = failed == 0 and not errors
        if level_ok:
            self.bus.ok(f"✅ STRM 生成完成（{reason}）：{note}", LogBus.STRM)
        else:
            self.bus.warn(f"⚠️ STRM 生成完成（{reason}）：{note}", LogBus.STRM)

        result = self._finish(
            started,
            reason,
            ok=failed == 0,
            created=created,
            updated=updated,
            skipped=skipped,
            deleted=deleted,
            extras=extras,
            extras_skipped=extras_skipped,
            failed=failed,
            files=files_seen,
            tasks=len(tasks),
            note=note,
            errors=errors[:8],
            details=details[:NOTIFY_DETAIL_LIMIT],
        )

        # 有实际变动才通知（与预热/同步一致）
        if (created + updated + deleted) > 0 and self.engine is not None:
            try:
                self.engine.notify_strm(result, details[:NOTIFY_DETAIL_LIMIT])
            except Exception as exc:
                self.bus.warn(f"STRM 生成通知推送失败：{exc}", LogBus.STRM)

        # 闭环：生成完自动触发增量预热 → 预热后自动备份同步
        if (created + updated) > 0 and (strm.get("after_generate") or "prewarm") == "prewarm":
            self._trigger_prewarm()
        return result

    def _trigger_prewarm(self) -> None:
        self.store.meta_set("strm_pending_prewarm", True)
        self.store.save()
        engine = self.engine
        if engine is None:
            return
        if engine.busy():
            self.bus.info("⏳ 预热任务正在运行，STRM 生成后将在空闲时自动补一次增量预热", LogBus.STRM)
            return
        started = engine.start("full")
        if started:
            self.store.meta_set("strm_pending_prewarm", False)
            self.store.save()
            self.bus.info("🔥 新 STRM 已就绪，自动开始增量预热", LogBus.STRM)
        else:
            self.bus.info("⏳ 预热暂不可启动，已排队等待", LogBus.STRM)

    def _finish(self, started: float, reason: str, ok: bool, error: str = "", **extra: Any) -> dict[str, Any]:
        result = {
            "ts": time.time(),
            "ts_text": time.strftime("%m-%d %H:%M"),
            "reason": reason,
            "ok": ok,
            "error": error,
            "duration": round(time.time() - started, 1),
            **extra,
        }
        self.store.meta_set("strm_last", result)
        self.store.meta_set("strm_last_ts", result["ts"])
        try:
            self.store.save()
        except Exception:
            pass
        return result
