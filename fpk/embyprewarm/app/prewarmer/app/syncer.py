"""备份同步：把预热导出的 mediainfo.json（可选图片/字幕）增量同步到目标目录。

规则：
- 目录结构与来源目录 A 保持一致；
- 目标 B 已存在的文件直接跳过，不存在的才复制（增量、可重复执行）；
- 三种触发方式：预热结束后自动、面板手动"立即同步"（先预览明细再确认）、每天定时；
- 同步日志走独立 sync 通道，与预热日志分开；
- 有实际复制（变动）时推送 PushPlus / Webhook 通知，无变动不打扰。

来源目录 A 默认就是预热备份所在位置（.strm 同目录），因此留空时自动使用
媒体库的本机路径（路径映射后的 scan.roots）。
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .config import ConfigStore
from .state import LogBus, Store

# 需要随 mediainfo.json 一起同步的附属文件
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tbn", ".bmp"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".sup", ".vtt"}
MEDIAINFO_SUFFIX = "-mediainfo.json"

# 预览/通知明细的最大条数（超出只给统计）
PLAN_ITEM_LIMIT = 1000
NOTIFY_DETAIL_LIMIT = 80


def _path_text(value: str) -> str:
    """把路径统一成 /a/b/c/ 形式，便于按「连续路径段」判断是否落在排除目录内。"""
    return "/" + str(value or "").replace("\\", "/").strip("/") + "/"


def _excluded_rules(cfg: dict[str, Any]) -> list[str]:
    """汇总排除规则：同步设置里手填的路径 + 标记「此任务不同步」的任务输出目录。

    任务输出目录 = 本地根目录 + 网盘完整路径（镜像结构），同时加入「完整输出目录」
    与「网盘路径」两种写法：前者匹配绝对路径，后者匹配相对来源 A 的路径，
    这样即使来源 A 与任务本地根不是同一个挂载点也能正确排除。
    """
    rules: list[str] = []
    for raw in (cfg["sync"].get("exclude") or []):
        text = str(raw or "").strip().replace("\\", "/").strip("/")
        if text:
            rules.append(text)
    for task in ((cfg.get("strm") or {}).get("tasks") or []):
        if not isinstance(task, dict) or not task.get("no_sync"):
            continue
        remote = str(task.get("remote") or "").strip().replace("\\", "/").strip("/")
        local = str(task.get("local") or "").strip().replace("\\", "/").strip("/")
        if remote:
            rules.append(remote)
        if local and remote:
            rules.append(f"{local}/{remote}")
    seen: set[str] = set()
    unique: list[str] = []
    for rule in rules:
        if rule not in seen:
            seen.add(rule)
            unique.append(rule)
    return unique


def _is_excluded(abs_path: str, rel_path: str, rules: list[str]) -> bool:
    """文件的绝对路径或相对来源 A 的路径，任一包含排除目录这段路径即跳过。

    支持三种写法：相对来源 A 的子路径、绝对路径、以及单独一个文件夹名。
    """
    if not rules:
        return False
    abs_text = _path_text(abs_path)
    rel_text = _path_text(rel_path)
    for rule in rules:
        needle = _path_text(rule)
        if needle == "//":
            continue
        if needle in abs_text or needle in rel_text:
            return True
    return False


class Syncer:
    """增量同步器：线程安全，同一时间只允许一个同步任务。"""

    def __init__(self, config: ConfigStore, store: Store, bus: LogBus):
        self.config = config
        self.store = store
        self.bus = bus
        self.engine = None  # 由 run.py 注入，用于推送同步完成通知
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    # ---------------------------------------------------------------- 对外
    def plan(self) -> dict[str, Any]:
        """同步预览：只对比不写入，返回将复制/跳过的文件明细。"""
        cfg = self.config.get()
        sources = self._source_roots(cfg)
        target = (cfg["sync"].get("target") or "").strip()
        if not target:
            return {"ok": False, "error": "未配置目标目录 B"}
        if not sources:
            return {"ok": False, "error": "未配置来源目录 A，且媒体库本机路径不可用"}

        target_path = Path(target)
        rules = _excluded_rules(cfg)
        items: list[dict[str, Any]] = []
        copied = skipped = missing = excluded = 0
        for source in sources:
            root = Path(source)
            if not root.is_dir():
                missing += 1
                continue
            for path in root.rglob("*"):
                try:
                    if not path.is_file() or not self._wanted(cfg, path):
                        continue
                    rel = path.relative_to(root).as_posix()
                    if _is_excluded(path.as_posix(), rel, rules):
                        excluded += 1
                        continue
                    action = "skip" if (target_path / rel).exists() else "copy"
                    if action == "copy":
                        copied += 1
                    else:
                        skipped += 1
                    if len(items) < PLAN_ITEM_LIMIT:
                        try:
                            size = path.stat().st_size
                        except OSError:
                            size = 0
                        items.append({"rel": rel, "action": action, "size": size})
                except OSError:
                    continue
        if not items and not copied and not skipped:
            if missing:
                return {"ok": False, "error": "来源目录不存在或不可读"}
            return {
                "ok": True, "empty": True,
                "sources": sources, "target": target,
                "copied": 0, "skipped": 0, "excluded": excluded,
                "excluded_rules": rules, "items": [],
                "note": "来源目录里没有可同步的文件（或已全部同步）",
            }
        return {
            "ok": True,
            "empty": False,
            "sources": sources,
            "target": target,
            "copied": copied,
            "skipped": skipped,
            "excluded": excluded,
            "excluded_rules": rules,
            "missing_sources": missing,
            "items": items,
            "truncated": (copied + skipped) > len(items),
        }

    def run(self, reason: str = "手动") -> dict[str, Any]:
        """执行一次同步（阻塞），返回结果摘要。"""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "同步正在进行中，请稍后再试"}
            self._running = True
        try:
            return self._run(reason)
        finally:
            self._running = False

    def run_async(self, reason: str = "手动") -> None:
        """后台线程执行同步，日志走 LogBus sync 通道。"""
        threading.Thread(target=self.run, args=(reason,), daemon=True, name="sync").start()

    def last_result(self) -> dict[str, Any]:
        return dict(self.store.meta_get("sync_last") or {})

    # ---------------------------------------------------------------- 内部
    def _source_roots(self, cfg: dict[str, Any]) -> list[str]:
        """来源目录：优先用户配置；留空时用媒体库映射后的本机路径。"""
        roots = [str(r) for r in (cfg["sync"].get("source") or []) if str(r).strip()]
        if roots:
            return roots
        mappings = cfg["scan"]["path_mappings"] or {}
        if mappings:
            return [str(v).rstrip("/") + "/" for v in mappings.values() if str(v).strip()]
        return [str(r) for r in cfg["scan"]["roots"] if str(r).strip()]

    @staticmethod
    def _wanted(cfg: dict[str, Any], path: Path) -> bool:
        sync = cfg["sync"]
        want_images = bool(sync.get("images", True))
        want_subs = bool(sync.get("subtitles", True))
        name = path.name.lower()
        ext = path.suffix.lower()
        if name.endswith(MEDIAINFO_SUFFIX):
            return True
        if want_images and ext in IMAGE_EXTS:
            return True
        return want_subs and ext in SUBTITLE_EXTS

    def _run(self, reason: str) -> dict[str, Any]:
        started = time.time()
        cfg = self.config.get()
        sync = cfg["sync"]

        target = (sync.get("target") or "").strip()
        if not target:
            return self._finish(started, reason, ok=False, error="未配置目标目录 B")
        sources = self._source_roots(cfg)
        if not sources:
            return self._finish(
                started, reason, ok=False,
                error="未配置来源目录 A，且媒体库本机路径不可用",
            )

        target_path = Path(target)
        try:
            target_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._finish(started, reason, ok=False, error=f"目标目录不可用：{exc}")

        copied = skipped = failed = excluded = 0
        details: list[dict[str, str]] = []
        errors: list[str] = []
        rules = _excluded_rules(cfg)
        self.bus.info(
            f"🔄 开始备份同步（{reason}）：A={sources[0]}{' 等' if len(sources) > 1 else ''} → B={target}",
            LogBus.SYNC,
        )
        if rules:
            self.bus.info(
                f"🚫 已排除 {len(rules)} 个目录：{'、'.join(rules[:5])}"
                f"{' 等' if len(rules) > 5 else ''}",
                LogBus.SYNC,
            )

        for source in sources:
            root = Path(source)
            if not root.is_dir():
                errors.append(f"来源目录不存在：{source}")
                failed += 1
                self.bus.warn(f"同步失败：{errors[-1]}", LogBus.SYNC)
                continue
            for path in root.rglob("*"):
                try:
                    if not path.is_file() or not self._wanted(cfg, path):
                        continue
                    rel = path.relative_to(root).as_posix()
                    if _is_excluded(path.as_posix(), rel, rules):
                        excluded += 1
                        continue
                    dst = target_path / rel
                    if dst.exists():
                        skipped += 1
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dst)
                    copied += 1
                    details.append({"rel": rel, "name": rel, "status": "ok"})
                    self.bus.ok(f"　└─ 复制 {rel}", LogBus.SYNC)
                except OSError as exc:
                    failed += 1
                    if len(errors) < 8:
                        errors.append(f"{path}: {exc}")
                    self.bus.warn(f"同步失败：{path}: {exc}", LogBus.SYNC)

        note = f"复制 {copied} / 跳过 {skipped} / 排除 {excluded} / 失败 {failed}"
        level_ok = failed == 0
        if level_ok:
            self.bus.ok(f"✅ 备份同步完成（{reason}）：{note}", LogBus.SYNC)
        else:
            self.bus.warn(f"⚠️ 备份同步完成（{reason}）：{note}", LogBus.SYNC)
        result = self._finish(
            started, reason, ok=level_ok,
            copied=copied, skipped=skipped, excluded=excluded, failed=failed, note=note,
            details=details[:NOTIFY_DETAIL_LIMIT],
        )
        # 与预热通知一致：只推送有实际变动的任务（复制数 > 0，门槛在 notify_sync 内）
        if copied > 0 and self.engine is not None:
            try:
                self.engine.notify_sync(result, result.get("details") or [])
            except Exception as exc:  # 通知失败不影响同步结果
                self.bus.warn(f"同步通知推送失败：{exc}", LogBus.SYNC)
        return result

    def _finish(
        self,
        started: float,
        reason: str,
        ok: bool,
        error: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        result = {
            "ts": time.time(),
            "ts_text": time.strftime("%m-%d %H:%M"),
            "reason": reason,
            "ok": ok,
            "error": error,
            "duration": round(time.time() - started, 1),
            **extra,
        }
        self.store.meta_set("sync_last", result)
        self.store.meta_set("sync_last_ts", result["ts"])
        try:
            self.store.save()
        except Exception:
            pass
        return result
