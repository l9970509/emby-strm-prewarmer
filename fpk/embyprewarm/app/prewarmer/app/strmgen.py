"""STRM 生成：把 OpenList 网盘目录同步成本地 .strm 文件。

参考 OpenStrm 的「前缀 + 远端路径」方案：
- strm 内容 = 前缀（OpenList 的 /d 地址或 rclone 挂载点）+ 网盘完整路径；
- 目录结构与网盘一致，`影片.mkv` → `影片.strm`；
- 每个目录是独立任务：可单独启用/停用、单独定时（支持一天多个时刻）、
  单独手动生成，并各自选择「增量」或「全量」模式；
- 增量：对比网盘目录与本地上次生成的目标目录，本地已有该 strm 就跳过，
  缺失才生成（只补缺，不改动已有文件；改前缀请用全量）；
- 全量：忽略是否已存在，整目录重写一遍（改前缀 / 修复内容不一致时用）；
- 可选一并下载 nfo / 字幕 / 海报等小文件（按扩展名白名单 + 大小上限）；
- 重建模式（按任务）：网盘已删除的影片，同步删除本地多余 strm；
- 生成完不主动预热：由 Emby 扫描到新 .strm 后发出入库通知（Webhooks library.new），
  本工具收到通知再触发增量预热，避免生成后立刻预热时 Emby 还未识别新文件。
"""

from __future__ import annotations

import os
import shutil
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

MODE_FULL = "full"
MODE_INCREMENTAL = "incremental"
MODE_LABEL = {MODE_FULL: "全量", MODE_INCREMENTAL: "增量"}


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


def _rel_under_root(remote_root: str, remote_path: str) -> str:
    """远端文件相对任务根目录的完整相对路径（含文件名）。"""
    root = "/" + remote_root.strip("/")
    path = remote_path if remote_path.startswith("/") else "/" + remote_path
    if root != "/" and path.startswith(root + "/"):
        return path[len(root) + 1:]
    return path.strip("/")


def _mirror_rel(remote_path: str) -> str:
    """网盘完整路径 → 相对本地根目录的路径（镜像根：保留 /2dland/动画 这样的层级）。"""
    return remote_path.strip("/")


def _is_under(path: Path, root: Path) -> bool:
    """path 是否位于 root 目录之下（含 root 本身）。"""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _move_legacy(target: Path, candidates: list[Path]) -> Path | None:
    """把旧布局的 strm 搬到新镜像位置（升级兼容）。

    只在「新位置还没有文件」时尝试，逐个候选检查；搬到目标后返回原路径，
    没搬成返回 None。跨盘移动失败时保留原文件不动。
    """
    if target.exists():
        return None
    for old in candidates:
        if old == target:
            continue
        try:
            if not old.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(target))
            return old
        except OSError:
            return None
    return None


def _other_roots(cfg: dict[str, Any], task: dict[str, Any]) -> list[Path]:
    """其它任务的镜像目录，清理时用来避开别人的文件。"""
    roots: list[Path] = []
    mine = str(task.get("id") or "")
    for item in (cfg.get("strm") or {}).get("tasks") or []:
        if mine and str(item.get("id") or "") == mine:
            continue
        local = str(item.get("local") or "").strip()
        remote = str(item.get("remote") or "").strip()
        if local and remote:
            roots.append(Path(local) / _mirror_rel(remote))
    return roots


def normalize_mode(value: Any) -> str:
    return MODE_FULL if str(value or "").strip().lower() == MODE_FULL else MODE_INCREMENTAL


class StrmGenerator:
    """STRM 生成器：线程安全，同一时间只跑一个任务，其余排队。"""

    def __init__(self, config: ConfigStore, store: Store, bus: LogBus):
        self.config = config
        self.store = store
        self.bus = bus
        self.engine = None  # 由 run.py 注入，用于通知与生成后预热
        self._lock = threading.Lock()
        self._running = False
        self._stop = threading.Event()
        self._queue: list[tuple[str, str | None, str | None]] = []

    # ---------------------------------------------------------------- 对外
    @property
    def running(self) -> bool:
        return self._running

    @property
    def queued(self) -> int:
        return len(self._queue)

    def stop(self) -> bool:
        if not self._running:
            return False
        self._stop.set()
        self.bus.warn("已请求停止 STRM 生成，等待当前文件处理完…", LogBus.STRM)
        return True

    def last_result(self) -> dict[str, Any]:
        return dict(self.store.meta_get("strm_last") or {})

    def task_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.store.meta_get("strm_task_state") or {})

    def tasks(self, only_id: str | None = None, include_disabled: bool = False) -> list[dict[str, Any]]:
        """当前配置里的生成任务（按需过滤）。"""
        cfg = self.config.get()
        items = [t for t in ((cfg.get("strm") or {}).get("tasks") or []) if t.get("remote") and t.get("local")]
        if only_id:
            return [t for t in items if str(t.get("id")) == only_id]
        if include_disabled:
            return items
        return [t for t in items if t.get("enabled", True)]

    def browse(self, path: str = "/") -> dict[str, Any]:
        """浏览 OpenList 目录树，供面板选择生成目录。"""
        cfg = self.config.get()
        current = "/" + str(path or "").strip("/")
        if current == "/":
            current = "/"
        try:
            client = self._client(cfg)
            client.ensure_token()
            entries = client.list_dir(current)
        except OpenListError as exc:
            return {"ok": False, "error": str(exc), "path": current}
        dirs = [
            {
                "name": entry["name"],
                "path": (current.rstrip("/") + "/" + entry["name"]) if current != "/" else "/" + entry["name"],
            }
            for entry in entries
            if entry["is_dir"] and entry["name"]
        ]
        dirs.sort(key=lambda item: item["name"].lower())
        parent = "/" if current == "/" else (current.rsplit("/", 1)[0] or "/")
        return {
            "ok": True,
            "path": current,
            "parent": parent,
            "dirs": dirs,
            "file_count": sum(1 for entry in entries if not entry["is_dir"]),
        }

    def plan(self, task_id: str | None = None, mode: str | None = None) -> dict[str, Any]:
        """生成预览：只对比不写盘，返回将创建/更新/跳过的明细。"""
        cfg = self.config.get()
        try:
            selected = self._select_tasks(cfg, task_id)
            if not selected:
                return {"ok": False, "error": "没有启用的生成任务，请先在「生成设置」里添加目录"}
            client = self._client(cfg)
            # 先验证连接/登录，避免"预览成功但一条也读不到"
            client.ensure_token()
        except OpenListError as exc:
            return {"ok": False, "error": str(exc)}

        total = {
            "create": 0,
            "update": 0,
            "skip": 0,
            "delete": 0,
            "extra": 0,
            "extra_skip": 0,
            "migrate": 0,
        }
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        groups: list[dict[str, Any]] = []
        for task in selected:
            task_mode = normalize_mode(mode or task.get("mode"))
            built = self._build_task(
                cfg, client, task, errors, mode=task_mode, other_roots=_other_roots(cfg, task)
            )
            for key in total:
                total[key] += built["counts"].get(key, 0)
            for item in built["items"]:
                if len(items) < PLAN_ITEM_LIMIT:
                    item["task"] = task.get("name") or task.get("remote")
                    item["task_id"] = task.get("id")
                    item["mode"] = task_mode
                    items.append(item)
            groups.append(
                {
                    "id": task.get("id"),
                    "name": task.get("name") or task.get("remote"),
                    "remote": task.get("remote"),
                    "local": task.get("local"),
                    "local_dir": str(Path(task["local"]) / _mirror_rel(task["remote"])),
                    "mode": task_mode,
                    "counts": built["counts"],
                }
            )

        changed = total["create"] + total["update"] + total["delete"] + total["extra"]
        if not changed and not total["skip"] and errors:
            return {"ok": False, "error": errors[0], "errors": errors[:5], "groups": groups}
        return {
            "ok": True,
            "empty": changed == 0,
            "tasks": len(selected),
            "groups": groups,
            "prefix": cfg["strm"].get("prefix") or "",
            "items": items,
            "errors": errors[:5],
            "truncated": sum(total.values()) > len(items),
            "note": "没有需要生成/更新的文件" if changed == 0 else "",
            **total,
        }

    def run(self, reason: str = "手动", task_id: str | None = None, mode: str | None = None) -> dict[str, Any]:
        """执行一次生成（阻塞），返回结果摘要。"""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "STRM 生成正在进行中，请稍后再试"}
            self._running = True
            self._stop.clear()
        try:
            return self._run(reason, task_id, mode)
        finally:
            with self._lock:
                self._running = False

    def run_async(self, reason: str = "手动", task_id: str | None = None, mode: str | None = None) -> bool:
        """后台执行；正在运行则排队，返回 False 表示已排队。"""
        with self._lock:
            if self._running:
                self._queue.append((reason, task_id, mode))
                return False
            self._running = True
        threading.Thread(
            target=self._worker, args=(reason, task_id, mode), daemon=True, name="strm"
        ).start()
        return True

    # ---------------------------------------------------------------- 内部
    def _worker(self, reason: str, task_id: str | None, mode: str | None) -> None:
        while True:
            try:
                self._run(reason, task_id, mode)
            except Exception as exc:  # 兜底：后台线程不能因为异常静默退出
                self.bus.error(f"STRM 生成异常：{exc}", LogBus.STRM)
            with self._lock:
                if not self._queue:
                    self._running = False
                    return
                reason, task_id, mode = self._queue.pop(0)
                self._stop.clear()

    def _client(self, cfg: dict[str, Any]) -> OpenListClient:
        strm = cfg.get("strm") or {}
        return OpenListClient(
            strm.get("openlist_url") or "",
            token=strm.get("openlist_token") or "",
            username=strm.get("openlist_username") or "",
            password=strm.get("openlist_password") or "",
            timeout=30,
        )

    @staticmethod
    def _select_tasks(cfg: dict[str, Any], task_id: str | None) -> list[dict[str, Any]]:
        all_tasks = [
            t for t in ((cfg.get("strm") or {}).get("tasks") or []) if t.get("remote") and t.get("local")
        ]
        if task_id:
            return [t for t in all_tasks if str(t.get("id")) == task_id]
        return [t for t in all_tasks if t.get("enabled", True)]

    def _build_task(
        self,
        cfg: dict[str, Any],
        client: OpenListClient,
        task: dict[str, Any],
        errors: list[str],
        mode: str = MODE_INCREMENTAL,
        other_roots: list[Path] | None = None,
    ) -> dict[str, Any]:
        """扫描一个任务：遍历远端、对比本地，算出动作清单。

        本地目录是「镜像根」：网盘 /2dland/动画/某番/01.mkv 会落到
        <本地根>/2dland/动画/某番/01.strm，目录结构与网盘完全一致。
        """
        strm = cfg["strm"]
        media_exts = parse_exts(strm.get("exts") or "", DEFAULT_MEDIA_EXTS)
        extra_exts = parse_exts(strm.get("extras") or "", "")
        prefix = strm.get("prefix") or ""
        local_root = Path(task["local"])
        remote_root = task["remote"]
        full = normalize_mode(mode) == MODE_FULL
        # 该任务实际落盘的目录（镜像根 + 网盘路径）
        task_dir = local_root / _mirror_rel(remote_root)
        prev_local = Path(task["prev_local"]) if task.get("prev_local") else None

        counts = {
            "create": 0,
            "update": 0,
            "skip": 0,
            "delete": 0,
            "extra": 0,
            "extra_skip": 0,
            "migrate": 0,
        }
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
            mirrored = _mirror_rel(entry["path"])
            if ext in media_exts:
                strm_rel = os.path.splitext(mirrored)[0] + ".strm"
                target = local_root / strm_rel
                content = OpenListClient.build_url(prefix, entry["path"]) + "\n"
                keep.add(str(target))

                # 旧版布局（本地目录 = 直接落盘目录）的文件位置，用于升级后自动搬家
                under_root = _rel_under_root(remote_root, entry["path"])
                legacy_rel = os.path.splitext(under_root)[0] + ".strm"
                legacy: list[Path] = [local_root / legacy_rel]
                if prev_local is not None:
                    legacy.append(prev_local / legacy_rel)
                    # 上一版已经是镜像根、只是换了磁盘/根目录
                    legacy.append(prev_local / strm_rel)
                migrated = None
                if not target.exists():
                    migrated = _move_legacy(target, legacy)

                exists = target.exists()
                if full:
                    # 全量：不看旧内容，全部重写（已有算更新，没有算新增）
                    action = "update" if exists else "create"
                elif exists:
                    # 增量：本地已有就算过一遍，但仍校验 strm 里的链接；
                    # 链接和当前前缀/路径不一致（换过前缀、换过源）才更新。
                    try:
                        old = target.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        old = ""
                    action = "skip" if old.strip() == content.strip() else "update"
                else:
                    action = "create"
                if migrated is not None:
                    counts["migrate"] += 1
                counts[action] += 1
                writes.append(
                    {
                        "action": action,
                        "path": target,
                        "content": content,
                        "rel": strm_rel,
                        "size": entry["size"],
                        "remote": entry["path"],
                        "migrated_from": str(migrated) if migrated else "",
                    }
                )
                if len(items) < PLAN_ITEM_LIMIT:
                    items.append(
                        {
                            "rel": strm_rel,
                            "action": action,
                            "size": entry["size"],
                            "remote": entry["path"],
                            "migrated": bool(migrated),
                        }
                    )
            elif extra_exts and ext in extra_exts:
                rel = mirrored
                target = local_root / rel
                under_root = _rel_under_root(remote_root, entry["path"])
                legacy = [local_root / under_root]
                if prev_local is not None:
                    legacy.append(prev_local / under_root)
                    legacy.append(prev_local / rel)
                if not target.exists() and _move_legacy(target, legacy) is not None:
                    counts["migrate"] += 1
                if target.exists():
                    counts["extra_skip"] += 1
                    continue
                counts["extra"] += 1
                downloads.append({"path": target, "rel": rel, "remote": entry["path"], "size": entry["size"]})
                if len(items) < PLAN_ITEM_LIMIT:
                    items.append(
                        {"rel": rel, "action": "extra", "size": entry["size"], "remote": entry["path"]}
                    )

        # 重建模式：删除网盘已不存在的本地 strm（限定在本任务自己的镜像目录里）
        deletes: list[Path] = []
        roots = [p for p in (other_roots or []) if p]
        if task.get("rebuild") and task_dir.is_dir():
            for path in task_dir.rglob("*.strm"):
                if str(path) in keep:
                    continue
                if any(_is_under(path, root) for root in roots):
                    continue
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
            "mode": normalize_mode(mode),
            "counts": counts,
            "items": items,
            "writes": writes,
            "downloads": downloads,
            "deletes": deletes,
            "files": len(files),
        }

    def _run(self, reason: str, task_id: str | None = None, mode: str | None = None) -> dict[str, Any]:
        started = time.time()
        cfg = self.config.get()
        strm = cfg.get("strm") or {}

        try:
            selected = self._select_tasks(cfg, task_id)
            if not selected:
                error = "任务不存在或未启用" if task_id else "没有启用的生成任务"
                return self._finish(started, reason, ok=False, error=error)
            client = self._client(cfg)
            client.ensure_token()
        except OpenListError as exc:
            return self._finish(started, reason, ok=False, error=str(exc))

        scope = selected[0].get("name") if len(selected) == 1 else f"{len(selected)} 个任务"
        override_mode = normalize_mode(mode) if mode else ""
        self.bus.info(
            f"📼 开始生成 STRM（{reason}）：{scope}"
            + (f" · 强制{MODE_LABEL[override_mode]}模式" if override_mode else "")
            + f" · 前缀 {strm.get('prefix') or '(未设置)'}",
            LogBus.STRM,
        )

        totals = {
            "create": 0,
            "update": 0,
            "skip": 0,
            "delete": 0,
            "extra": 0,
            "extra_skip": 0,
            "migrate": 0,
        }
        failed = 0
        files_seen = 0
        errors: list[str] = []
        details: list[dict[str, str]] = []
        log_lines = 0
        stopped = False
        states = self.task_states()

        for task in selected:
            task_started = time.time()
            task_mode = normalize_mode(override_mode or task.get("mode"))
            local_root = Path(task["local"])
            local_dir = local_root / _mirror_rel(task["remote"])
            label = task.get("name") or task.get("remote")
            self.bus.info(
                f"　└─ [{MODE_LABEL[task_mode]}] {label}：{task['remote']} → {local_dir}",
                LogBus.STRM,
            )
            built = self._build_task(
                cfg, client, task, errors, mode=task_mode, other_roots=_other_roots(cfg, task)
            )
            files_seen += built["files"]
            counts = built["counts"]
            for key in totals:
                totals[key] += counts[key]
            task_failed = 0

            for item in built["writes"]:
                if self._stop.is_set():
                    errors.append("任务被手动停止")
                    stopped = True
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
                    task_failed += 1
                    if len(errors) < 8:
                        errors.append(f"{item['rel']}：{exc}")
                    self.bus.warn(f"　　✗ 写入失败 {item['rel']}：{exc}", LogBus.STRM)

            max_bytes = int(strm.get("max_file_mb") or 0) * 1024 * 1024
            for item in built["downloads"]:
                if self._stop.is_set():
                    errors.append("任务被手动停止")
                    stopped = True
                    break
                if max_bytes and item["size"] > max_bytes:
                    totals["extra"] -= 1
                    totals["extra_skip"] += 1
                    continue
                try:
                    item["path"].parent.mkdir(parents=True, exist_ok=True)
                    client.download(item["remote"], item["path"], max_bytes=max_bytes)
                    if log_lines < LOG_DETAIL_LIMIT:
                        self.bus.ok(f"　　⤓ 附件 {item['rel']}", LogBus.STRM)
                        log_lines += 1
                except OpenListError as exc:
                    totals["extra"] -= 1
                    totals["extra_skip"] += 1
                    failed += 1
                    task_failed += 1
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
                    task_failed += 1
                    if len(errors) < 8:
                        errors.append(f"删除 {path}：{exc}")

            # 记录该任务自己的最近一次结果（与定时基准互不覆盖）
            entry = states.setdefault(str(task.get("id") or label), {})
            entry.update(
                {
                    "ts": time.time(),
                    "ts_text": time.strftime("%m-%d %H:%M"),
                    "mode": task_mode,
                    "reason": reason,
                    "ok": task_failed == 0,
                    "created": counts["create"],
                    "updated": counts["update"],
                    "skipped": counts["skip"],
                    "migrated": counts["migrate"],
                    "deleted": counts["delete"],
                    "extras": counts["extra"],
                    "failed": task_failed,
                    "files": built["files"],
                    "duration": round(time.time() - task_started, 1),
                }
            )
            self.store.meta_set("strm_task_state", states)
            try:
                self.store.save()
            except Exception:
                pass

            self.bus.info(
                f"　└─ {label} 完成：新增 {counts['create']} / 更新 {counts['update']} / "
                f"跳过 {counts['skip']}"
                + (f" / 迁移 {counts['migrate']}" if counts["migrate"] else "")
                + (f" / 删除 {counts['delete']}" if counts["delete"] else ""),
                LogBus.STRM,
            )
            if stopped:
                break

        note = (
            f"新增 {totals['create']} / 更新 {totals['update']} / 跳过 {totals['skip']}"
            + (f" / 迁移 {totals['migrate']}" if totals["migrate"] else "")
            + (f" / 删除 {totals['delete']}" if totals["delete"] else "")
            + (f" / 附件 {totals['extra']}" if totals["extra"] or totals["extra_skip"] else "")
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
            scope=scope,
            task_id=task_id or "",
            created=totals["create"],
            updated=totals["update"],
            skipped=totals["skip"],
            migrated=totals["migrate"],
            deleted=totals["delete"],
            extras=totals["extra"],
            extras_skipped=totals["extra_skip"],
            failed=failed,
            files=files_seen,
            tasks=len(selected),
            note=note,
            errors=errors[:8],
            details=details[:NOTIFY_DETAIL_LIMIT],
        )

        # 有实际变动才通知（与预热/同步一致）
        if (totals["create"] + totals["update"] + totals["delete"]) > 0 and self.engine is not None:
            try:
                self.engine.notify_strm(result, details[:NOTIFY_DETAIL_LIMIT])
            except Exception as exc:
                self.bus.warn(f"STRM 生成通知推送失败：{exc}", LogBus.STRM)

        return result

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
