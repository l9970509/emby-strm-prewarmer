"""预热引擎：扫描、分类、预热、失败循环重试。

设计要点（与上游 emby-strm-prewarmer 保持一致）：
1. 只处理媒体库中 Path 以 .strm 结尾且位于指定根目录下的条目；
2. 只有"存在带 Codec/Width/Height 的视频流"才算预热完成，否则需要预热；
3. 预热方式为 POST /Items/{id}/PlaybackInfo?IsPlayback=true&AutoOpenLiveStream=true，
   由 Emby 自己去读真实媒体文件并写回 MediaInfo；
4. 通过 STRM 文件内容 sha256 指纹识别"同名换源"，此时先 Refresh 再预热。
"""

from __future__ import annotations

import hashlib
import html
import json
import threading
import time
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import mediainfo
from .config import ConfigStore
from .emby import ITEM_FIELDS, EmbyClient, EmbyError, describe_video, has_complete_video
from .state import LogBus, Store, now_iso

MODES = {
    "full": "一键预热",
    "failed": "仅重试失败",
    "export": "导出备份",
    "scan": "扫描预览",
    "watch": "新片预热",
}


@dataclass
class Target:
    item_id: str
    path: str
    name: str
    reason: str          # incomplete | changed | retry
    fingerprint: dict[str, Any] | None = None

    @property
    def display(self) -> str:
        return self.name or Path(self.path).name


def _host_path(emby_path: str, mappings: dict[str, str]) -> Path | None:
    """把 Emby 内部路径翻译成当前系统能读到的路径。"""
    for emby_root in sorted(mappings, key=len, reverse=True):
        if emby_path.startswith(emby_root):
            rest = emby_path[len(emby_root):]
            host_root = mappings[emby_root]
            candidate = Path(host_root) / rest.lstrip("/\\")
            if candidate.is_file():
                return candidate
    direct = Path(emby_path)
    return direct if direct.is_file() else None


def fingerprint_of(emby_path: str, mappings: dict[str, str]) -> dict[str, Any] | None:
    local = _host_path(emby_path, mappings)
    if local is None:
        return None
    try:
        content = local.read_bytes()
    except OSError:
        return None
    return {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


class Engine:
    def __init__(self, config: ConfigStore, store: Store, bus: LogBus):
        self.config = config
        self.store = store
        self.bus = bus
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._failed_ids: set[str] = set()
        self._only_ids: set[str] = set()
        # 本次运行的预热明细（用于完成通知）：id -> {id,name,status,note}
        self._details: dict[str, dict[str, str]] = {}
        # 备份同步器（由 run.py 注入）；预热结束后可自动触发
        self.syncer: Any = None
        self._state: dict[str, Any] = {
            "running": False,
            "mode": "",
            "phase": "",
            "total": 0,
            "done": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "exported": 0,
            "export_skipped": 0,
            "export_failed": 0,
            "active": [],
            "started_at": None,
            "finished_at": None,
            "last_error": "",
            "last_summary": {},
        }

    # ------------------------------------------------------------------ 控制
    def busy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, mode: str = "full", only_ids: list[str] | None = None) -> bool:
        if mode not in MODES:
            raise ValueError(f"未知模式：{mode}")
        with self._lock:
            if self.busy():
                return False
            self._stop.clear()
            self._failed_ids = set()
            self._details = {}
            # 手动预热指定项目（来自失败队列等）：只处理这些条目
            self._only_ids = {str(i) for i in (only_ids or [])}
            self._state.update(
                running=True,
                mode=mode,
                phase="准备",
                total=0,
                done=0,
                success=0,
                failed=0,
                skipped=0,
                exported=0,
                export_skipped=0,
                export_failed=0,
                active=[],
                started_at=time.time(),
                finished_at=None,
                last_error="",
            )
            self._thread = threading.Thread(
                target=self._run, args=(mode,), daemon=True, name=f"prewarm-{mode}"
            )
            self._thread.start()
            return True

    def stop(self) -> bool:
        if not self.busy():
            return False
        self._stop.set()
        self.bus.warn("收到停止指令，正在结束当前项目…")
        return True

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread:
            thread.join(timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state["started_at"] and state["running"]:
            state["elapsed"] = round(time.time() - state["started_at"], 1)
        elif state["started_at"] and state["finished_at"]:
            state["elapsed"] = round(state["finished_at"] - state["started_at"], 1)
        else:
            state["elapsed"] = 0
        state["busy"] = self.busy()
        state["mode_text"] = MODES.get(state["mode"], state["mode"])
        return state

    def _set(self, **kwargs: Any) -> None:
        with self._lock:
            self._state.update(kwargs)

    def _bump(self, key: str, delta: int = 1) -> int:
        with self._lock:
            self._state[key] = int(self._state.get(key, 0)) + delta
            return self._state[key]

    # -------------------------------------------------------------- 主流程
    def _run(self, mode: str) -> None:
        started = time.time()
        cfg = self.config.get()
        collect_summary: dict[str, int] | None = None
        incremental = False
        self.bus.info("=" * 60)
        self.bus.info(f"任务开始：{MODES[mode]}")
        exit_note = ""
        try:
            client = EmbyClient(cfg["emby"]["url"], cfg["emby"]["token"], cfg["emby"]["timeout"])
            if not cfg["emby"]["url"]:
                raise EmbyError("未配置 Emby 地址")
            if not cfg["emby"]["token"]:
                raise EmbyError("未配置 Emby API Token")

            self._set(phase="连接 Emby")
            info = client.probe()
            self.bus.ok(
                f"已连接 {info['product']}「{info['server_name']}」{info['version']} @ {info['base_url']}"
            )

            user_id = client.resolve_user_id(cfg["emby"]["user_id"])
            watch_mode = mode == "watch"
            if watch_mode:
                # 入库监听：只查 DateCreated 晚于上次游标的条目，不做全库扫描
                cursor = float(
                    self.store.meta_get("watch_last_ts")
                    or self.store.meta_get("boot_ts")
                    or time.time()
                )
                since_ts = cursor - float(cfg["watch"]["overlap_seconds"])
                self._set(phase="检测新入库")
                self.bus.info(
                    "正在查询新入库条目（"
                    + datetime.fromtimestamp(since_ts).strftime("%m-%d %H:%M")
                    + " 之后）…"
                )
                targets, complete_targets, summary = self._collect(client, cfg, since_ts=since_ts)
                collect_summary = summary
                incremental = True
                # 游标推进到本次查询时刻：失败项由失败队列兜底，不会丢
                self.store.meta_set("watch_last_ts", time.time())
                self.store.save()
                self.bus.info(
                    f"入库检测：新增 STRM {summary['total']} 个 / "
                    f"待预热 {len(targets)} 个 / 失败队列 {self.store.failure_count()} 个"
                )
                if not targets:
                    exit_note = "没有新入库影片"
                    self.bus.ok(exit_note)
                    return
            else:
                self._set(phase="扫描媒体库")
                self.bus.info("正在读取媒体库条目…")
                targets, complete_targets, summary = self._collect(client, cfg)
                collect_summary = summary
                self.store.set_summary({**summary, "ts": time.time()})
                self.bus.info(
                    f"扫描完成：STRM {summary['total']} 个 / 已完整 {summary['complete']} 个 / "
                    f"待预热 {summary['pending']} 个 / 失败队列 {self.store.failure_count()} 个"
                )
                if summary["unreadable"]:
                    self.bus.warn(
                        f"有 {summary['unreadable']} 个 STRM 无法读取本地文件，"
                        "换源指纹不可用（不影响预热，建议检查「路径映射」）"
                    )

            # ---- 仅导出备份：给已有 MediaInfo 但还没有 json 的条目补备份
            if mode == "export":
                if not complete_targets:
                    exit_note = "没有可导出备份的项目"
                    self.bus.warn(exit_note)
                    return
                ok, failed = self._run_export(client, user_id, cfg, complete_targets)
                if failed:
                    exit_note = f"导出完成：成功 {len(ok)} 个，失败 {len(failed)} 个"
                    self.bus.warn(exit_note)
                else:
                    exit_note = f"导出完成：成功 {len(ok)} 个"
                    self.bus.ok(exit_note)
                return

            if mode == "failed":
                if self._only_ids:
                    # 手动预热：只处理指定项目，不受退避时间限制
                    wanted = self._only_ids
                    self.bus.info(f"手动预热：指定 {len(wanted)} 个项目")
                else:
                    wanted = set(self.store.pending_failure_ids(include_dead=True))
                targets = [t for t in targets if t.item_id in wanted]
                self.bus.info(f"过滤失败队列：{len(targets)} 个待重试项目")

            max_items = int(cfg["prewarm"]["max_items_per_run"])
            if max_items and len(targets) > max_items:
                self.bus.warn(f"单次上限 {max_items}，本次只处理前 {max_items} 个")
                targets = targets[:max_items]

            if mode == "scan":
                self._set(phase="扫描预览", total=len(targets))
                for index, target in enumerate(targets[:100], 1):
                    self.bus.info(
                        f"[{index}] {target.display} | 原因={target.reason} | {target.path}"
                    )
                if len(targets) > 100:
                    self.bus.info(f"… 其余 {len(targets) - 100} 个已省略")
                exit_note = f"扫描完成，{len(targets)} 个待预热"
                self.bus.ok(exit_note)
                return

            if not targets:
                exit_note = "没有需要预热的项目"
                self.bus.ok(exit_note + "，媒体库已是最新状态")
                return

            ok, failed = self._run_targets(client, user_id, cfg, targets, "预热")
            self.bus.info(f"首轮结束：成功 {len(ok)} 个，失败 {len(failed)} 个")

            # ---- 失败项目循环预热（同一次运行内）
            loop_cfg = cfg["failure_loop"]
            if (
                failed
                and loop_cfg["enabled"]
                and int(loop_cfg["rounds"]) > 0
                and not self._stop.is_set()
            ):
                for round_index in range(1, int(loop_cfg["rounds"]) + 1):
                    if self._stop.is_set():
                        break
                    delay = float(loop_cfg["round_delay_seconds"])
                    self.bus.info(
                        f"失败循环 {round_index}/{int(loop_cfg['rounds'])}："
                        f"{len(failed)} 个项目，{int(delay)} 秒后重试"
                    )
                    if not self._sleep(delay):
                        break
                    retry_targets = [replace(t, reason="retry") for t in failed]
                    round_ok, failed = self._run_targets(
                        client, user_id, cfg, retry_targets, f"失败重试 第{round_index}轮"
                    )
                    ok.extend(round_ok)
                    self.bus.info(f"第 {round_index} 轮结束：成功 {len(round_ok)} 个，仍失败 {len(failed)} 个")
                    if not failed:
                        break

            if failed:
                exit_note = f"完成，成功 {len(ok)} 个，失败 {len(failed)} 个（已加入失败队列，稍后自动重试）"
                self.bus.warn(exit_note)
            else:
                exit_note = f"全部完成，成功 {len(ok)} 个"
                self.bus.ok(exit_note)

        except EmbyError as exc:
            exit_note = f"Emby 接口错误：{exc}"
            self.bus.error(exit_note)
            self._set(last_error=str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            exit_note = f"任务异常：{type(exc).__name__}: {exc}"
            self.bus.error(exit_note)
            self._set(last_error=exit_note)
        finally:
            duration = time.time() - started
            status = self.status()
            details = list(self._details.values())
            started_ts = status.get("started_at") or started
            # 任务结束后立即修正统计卡片（成功项即时反映到 已完整/待预热/条目总数，
            # 新入库条目由 watch 增量结果累加，不必等下一次全库扫描）
            if mode in ("full", "failed", "watch") and collect_summary is not None:
                success = int(status.get("success") or 0)
                if incremental:
                    self.store.adjust_summary(
                        total=int(collect_summary.get("total") or 0),
                        complete=int(collect_summary.get("complete") or 0) + success,
                        pending=int(collect_summary.get("pending") or 0) - success,
                    )
                else:
                    self.store.adjust_summary(complete=success, pending=-success)
            entry = {
                "ts": now_iso(),
                "started": datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S"),
                "mode": MODES.get(mode, mode),
                "total": status["total"],
                "success": status["success"],
                "failed": status["failed"],
                "exported": status.get("exported", 0),
                "prewarmed": len(details),
                "duration": round(duration, 1),
                "note": exit_note,
            }
            self.store.add_history(entry)
            self.store.save()
            self._set(running=False, finished_at=time.time(), phase="空闲", active=[], last_summary=entry)
            self.bus.info(f"任务结束，用时 {duration:.1f}s")
            self._notify(cfg, entry, details)
            self._maybe_sync(cfg, entry)

    # ------------------------------------------------------------ 扫描分类
    def _collect(
        self,
        client: EmbyClient,
        cfg: dict[str, Any],
        since_ts: float | None = None,
    ) -> tuple[list[Target], list[Target], dict[str, int]]:
        roots = tuple(cfg["scan"]["roots"])
        only_strm = bool(cfg["scan"]["only_strm"])
        mappings = cfg["scan"]["path_mappings"]
        failures = set(self.store.pending_failure_ids(include_dead=True))

        targets: list[Target] = []
        complete_targets: list[Target] = []
        total = complete = pending = unreadable = 0
        seen = 0

        if since_ts is not None:
            source = client.iter_items_since(since_ts, cfg["scan"]["include_types"])
        else:
            source = client.iter_items(cfg["scan"]["include_types"])

        for item in source:
            path = str(item.get("Path") or "")
            if not path:
                continue
            if only_strm and not path.lower().endswith(".strm"):
                continue
            if roots and not path.startswith(roots):
                continue
            item_id = str(item.get("Id") or "")
            if not item_id:
                continue

            seen += 1
            if seen % 500 == 0:
                self._set(phase=f"扫描媒体库（{seen}）")
                self.bus.info(f"已检查 {seen} 个 STRM 条目…")

            done = has_complete_video(item)
            fp = fingerprint_of(path, mappings)
            if fp is None:
                unreadable += 1
            prev = self.store.get_fingerprint(item_id)
            reason = ""

            if not done:
                pending += 1
                reason = "incomplete"
            else:
                complete += 1
                if item_id in failures:
                    # 之前失败但现在已经完整，说明被其它途径修好了
                    self.store.record_success(item_id)
                    failures.discard(item_id)
                if fp is not None:
                    if prev is None:
                        self.store.set_fingerprint(item_id, path, fp)
                    elif prev.get("sha256") != fp.get("sha256"):
                        reason = "changed"
            total += 1

            if reason:
                targets.append(
                    Target(
                        item_id=item_id,
                        path=path,
                        name=str(item.get("Name") or ""),
                        reason=reason,
                        fingerprint=fp,
                    )
                )
            elif done:
                complete_targets.append(
                    Target(
                        item_id=item_id,
                        path=path,
                        name=str(item.get("Name") or ""),
                        reason="complete",
                        fingerprint=fp,
                    )
                )

        return targets, complete_targets, {
            "total": total,
            "complete": complete,
            "pending": pending,
            "unreadable": unreadable,
        }

    # ------------------------------------------------------------ 执行预热
    def _run_targets(
        self,
        client: EmbyClient,
        user_id: str,
        cfg: dict[str, Any],
        targets: list[Target],
        label: str,
    ) -> tuple[list[Target], list[Target]]:
        ok: list[Target] = []
        failed: list[Target] = []
        if not targets:
            return ok, failed

        concurrency = max(1, int(cfg["prewarm"]["concurrency"]))
        self._set(phase=label, total=len(targets), done=0, active=[])
        self.bus.info(f"{label} 开始：共 {len(targets)} 个项目，并发 {concurrency}")

        pending: dict[Any, Target] = {}
        index = 0
        done_count = 0

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="pw") as pool:
            while True:
                while (
                    len(pending) < concurrency
                    and index < len(targets)
                    and not self._stop.is_set()
                ):
                    target = targets[index]
                    index += 1
                    future = pool.submit(self._process_one, client, user_id, cfg, target)
                    pending[future] = target

                if not pending:
                    break

                finished, _ = wait(list(pending.keys()), return_when=FIRST_COMPLETED)
                for future in finished:
                    target = pending.pop(future)
                    done_count += 1
                    try:
                        status, error, detail, item = future.result()
                    except Exception as exc:  # pragma: no cover
                        status, error, detail, item = "fail", f"{type(exc).__name__}: {exc}", "", {}

                    if status == "ok":
                        ok.append(target)
                        self._failed_ids.discard(target.item_id)
                        self._details[target.item_id] = {
                            "id": target.item_id,
                            "name": target.display,
                            "status": "ok",
                            "note": detail,
                        }
                        self.store.record_success(target.item_id)
                        if target.fingerprint:
                            self.store.set_fingerprint(target.item_id, target.path, target.fingerprint)
                        self._bump("success")
                        self._set(failed=len(self._failed_ids))
                        self.bus.ok(
                            f"[{done_count}/{len(targets)}] ✓ {target.display} {detail} | {target.path}"
                        )
                        self._export_backup(cfg, target, item)
                    elif status == "fail":
                        failed.append(target)
                        self._failed_ids.add(target.item_id)
                        self._details[target.item_id] = {
                            "id": target.item_id,
                            "name": target.display,
                            "status": "fail",
                            "note": error,
                        }
                        record = self.store.record_failure(
                            target.item_id,
                            target.path,
                            target.display,
                            error,
                            self._backoff(cfg, self._attempts_of(target.item_id)),
                            int(cfg["failure_loop"]["max_attempts"]),
                        )
                        self._set(failed=len(self._failed_ids))
                        self.bus.error(
                            f"[{done_count}/{len(targets)}] ✗ {target.display} "
                            f"(累计失败 {record['attempts']} 次) {error}"
                        )
                    else:  # stopped
                        self.bus.warn(f"[{done_count}/{len(targets)}] 已跳过 {target.display}")

                    self._set(
                        done=done_count,
                        active=[t.display for t in list(pending.values())[:4]],
                    )
                    self._maybe_save()

        if self._stop.is_set():
            self.bus.warn(f"{label} 已被手动停止")
        return ok, failed

    def _process_one(
        self,
        client: EmbyClient,
        user_id: str,
        cfg: dict[str, Any],
        target: Target,
    ) -> tuple[str, str, str, dict[str, Any]]:
        attempts = int(cfg["prewarm"]["max_retries"]) + 1
        retry_delay = float(cfg["prewarm"]["retry_delay_seconds"])
        verify = bool(cfg["prewarm"]["verify"])
        want_export = bool(cfg["export"]["enabled"])
        last_error = ""

        try:
            for attempt in range(attempts):
                if self._stop.is_set():
                    return "stopped", "已停止", "", {}
                try:
                    if target.reason == "changed":
                        self.bus.info(f"检测到换源，先刷新元数据：{target.display}")
                        client.refresh_item(target.item_id)
                        self._sleep(2)

                    client.prewarm(user_id, target.item_id)

                    item: dict[str, Any] = {}
                    detail = ""
                    # 需要校验或需要导出备份时，回读一次带章节的完整条目
                    if verify or want_export:
                        item = client.get_item(user_id, target.item_id, ITEM_FIELDS)
                    if verify:
                        if not has_complete_video(item):
                            raise EmbyError("PlaybackInfo 已返回，但 MediaInfo 仍不完整")
                        detail = describe_video(item)
                    return "ok", "", detail, item
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}" if not isinstance(exc, EmbyError) else str(exc)
                    if attempt + 1 < attempts:
                        self.bus.warn(
                            f"重试 {attempt + 1}/{attempts - 1} {target.display}：{last_error}"
                        )
                        if not self._sleep(retry_delay):
                            return "stopped", "已停止", "", {}
            return "fail", last_error, "", {}
        finally:
            delay = float(cfg["prewarm"]["delay_seconds"])
            if delay > 0 and not self._stop.is_set():
                self._sleep(delay)

    # ------------------------------------------------------- 备份导出（预热后）
    def _export_backup(self, cfg: dict[str, Any], target: Target, item: dict[str, Any]) -> None:
        if not cfg["export"]["enabled"] or not item:
            return
        # STRM 换源后媒体信息已变，此时必须覆盖旧备份
        overwrite = bool(cfg["export"]["overwrite"]) or target.reason == "changed"
        result = mediainfo.export_item(
            item, target.path, cfg["scan"]["path_mappings"], overwrite
        )
        status = result["status"]
        if status == "ok":
            self._bump("exported")
            self.bus.ok(f"　└─ 备份已导出 {Path(result['path']).name}")
            for warning in result["warnings"]:
                self.bus.warn(f"　└─ 备份字段提示：{warning}（{target.display}）")
        elif status == "exists":
            self._bump("export_skipped")
            self.bus.info(f"　└─ 已有备份，跳过 {Path(result['path']).name}")
        else:
            self._bump("export_failed")
            self.bus.warn(
                f"　└─ 备份导出失败（{status}）：{result.get('error') or result.get('path') or ''}"
            )

    def _run_export(
        self,
        client: EmbyClient,
        user_id: str,
        cfg: dict[str, Any],
        targets: list[Target],
    ) -> tuple[list[Target], list[Target]]:
        """为"已有完整 MediaInfo 但缺少 json 备份"的项目批量补备份。"""
        ok: list[Target] = []
        failed: list[Target] = []
        if not targets:
            return ok, failed

        mappings = cfg["scan"]["path_mappings"]
        overwrite = bool(cfg["export"]["overwrite"])
        total = len(targets)
        done = 0
        workers = min(4, max(1, int(cfg["prewarm"]["concurrency"]) + 1))
        self._set(phase="导出备份", total=total, done=0, active=[])
        self.bus.info(f"导出备份开始：共 {total} 个项目，并发 {workers}")

        def work(target: Target) -> tuple[str, str]:
            if self._stop.is_set():
                return "stopped", "已停止"
            try:
                item = client.get_item(user_id, target.item_id, ITEM_FIELDS)
            except Exception as exc:
                return "failed", f"读取条目失败：{exc}"
            result = mediainfo.export_item(item, target.path, mappings, overwrite)
            status = result["status"]
            if status == "ok":
                return "ok", str(result["path"])
            if status == "exists":
                return "exists", str(result["path"])
            return "failed", str(result.get("error") or "")

        chunk = workers * 6
        for start in range(0, total, chunk):
            if self._stop.is_set():
                break
            batch = targets[start:start + chunk]
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ex") as pool:
                futures = {pool.submit(work, target): target for target in batch}
                for future in as_completed(futures):
                    target = futures[future]
                    done += 1
                    try:
                        status, message = future.result()
                    except Exception as exc:  # pragma: no cover
                        status, message = "failed", f"{type(exc).__name__}: {exc}"

                    if status == "ok":
                        ok.append(target)
                        self._bump("success")
                        self._bump("exported")
                        self.bus.ok(f"[{done}/{total}] 备份 {target.display} → {Path(message).name}")
                    elif status == "exists":
                        self._bump("export_skipped")
                        self.bus.info(f"[{done}/{total}] 已有备份，跳过 {target.display}")
                    elif status == "stopped":
                        continue
                    else:
                        failed.append(target)
                        self._bump("export_failed")
                        self.bus.warn(f"[{done}/{total}] 备份失败 {target.display}：{message}")

                    self._set(done=done, failed=len(failed), active=[])

        if self._stop.is_set():
            self.bus.warn("导出备份已被手动停止")
        return ok, failed

    # --------------------------------------------------------------- 辅助
    def _attempts_of(self, item_id: str) -> int:
        record = self.store.data["failures"].get(item_id) or {}
        return int(record.get("attempts", 0)) + 1

    @staticmethod
    def _backoff(cfg: dict[str, Any], attempts: int) -> float:
        base = float(cfg["failure_loop"]["auto_retry_interval_seconds"])
        return min(6 * 3600, base * max(1, attempts))

    def _sleep(self, seconds: float) -> bool:
        """可被停止指令打断的等待；返回 False 表示任务已被要求停止。"""
        if seconds <= 0:
            return not self._stop.is_set()
        return not self._stop.wait(seconds)

    def _maybe_save(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_save > 5:
            self._last_save = now
            self.store.save()

    def _maybe_sync(self, cfg: dict[str, Any], entry: dict[str, Any]) -> None:
        """预热结束后按配置自动把备份同步到目标目录（后台执行，不阻塞）。"""
        syncer = self.syncer
        if syncer is None:
            return
        sync = cfg.get("sync") or {}
        if not sync.get("enabled") or not sync.get("after_prewarm"):
            return
        # 没有实际预热/导出的任务（空扫描、纯扫描预览）不同步
        if not (int(entry.get("prewarmed") or 0) or int(entry.get("exported") or 0)):
            return
        self.bus.info("🔄 预热结束，自动执行备份同步…")
        syncer.run_async("预热结束后自动同步")

    def _notify(self, cfg: dict[str, Any], entry: dict[str, Any], details: list[dict[str, str]]) -> None:
        # 只有真正预热过影片的任务才通知：空扫描、纯导出、没有新片的任务都不打扰
        if not int(entry.get("prewarmed") or 0):
            return
        notify = cfg.get("notify", {}) or {}
        url = (notify.get("webhook_url") or "").strip()
        token = (notify.get("pushplus_token") or "").strip()

        if url:
            payload = {
                "title": "Emby 媒体信息预热完成",
                "text": (
                    f"开始时间：{entry['started']}\n"
                    f"模式：{entry['mode']}\n持续时长：{self._duration_text(entry['duration'])}\n"
                    f"预热条目：{entry['prewarmed']} 个（成功 {entry['success']} · 失败 {entry['failed']}）\n"
                    f"说明：{entry['note']}"
                ),
                **entry,
                "details": details,
            }
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=10).read()
            except Exception as exc:
                self.bus.warn(f"通知推送失败：{exc}")

        if token:
            title = (
                f"Emby 预热完成：{entry['mode']} 成功 {entry['success']}"
                + (f" / 失败 {entry['failed']}" if entry["failed"] else "")
            )
            self._push_plus(token, title, self._pushplus_content(entry, details))

    @staticmethod
    def _duration_text(seconds: Any) -> str:
        try:
            total = int(round(float(seconds or 0)))
        except (TypeError, ValueError):
            total = 0
        if total < 60:
            return f"{total} 秒"
        minutes, sec = divmod(total, 60)
        if minutes < 60:
            return f"{minutes} 分 {sec} 秒"
        hours, minutes = divmod(minutes, 60)
        return f"{hours} 小时 {minutes} 分"

    @classmethod
    def _pushplus_content(cls, entry: dict[str, Any], details: list[dict[str, str]]) -> str:
        """PushPlus 正文（html 模板）：开始时间 / 时长 / 明细 / 成功失败数。"""
        lines = [
            f"<b>开始时间</b>：{html.escape(str(entry.get('started') or entry.get('ts') or ''))}",
            f"<b>持续时长</b>：{cls._duration_text(entry.get('duration'))}",
            f"<b>任务类型</b>：{html.escape(str(entry.get('mode') or ''))}",
            f"<b>预热条目</b>：{int(entry.get('prewarmed') or 0)} 个",
            f"<b>成功 / 失败</b>：<span style=\"color:#15a05a\">{int(entry.get('success') or 0)}</span> / "
            f"<span style=\"color:#e11d48\">{int(entry.get('failed') or 0)}</span>",
        ]
        if entry.get("exported"):
            lines.append(f"<b>备份导出</b>：{int(entry['exported'])} 个")
        if details:
            lines.append("<br><b>预热明细</b>")
            limit = 80
            for index, item in enumerate(details[:limit], 1):
                ok = item.get("status") == "ok"
                mark = "✅" if ok else "❌"
                note = (item.get("note") or "").strip()
                name = html.escape(str(item.get("name") or item.get("id") or ""))
                tail = f" · {html.escape(note)}" if note else ""
                lines.append(f"{index}. {mark} {name}{tail}")
            if len(details) > limit:
                lines.append(f"<i>… 其余 {len(details) - limit} 条已省略</i>")
        if entry.get("note"):
            lines.append(f"<br><b>说明</b>：{html.escape(str(entry['note']))}")
        return "<br>".join(lines)

    def pushplus_test(self, token: str) -> tuple[bool, str]:
        """向指定 Token 推送一条测试消息（面板"发送测试通知"用）。"""
        token = (token or "").strip()
        if not token:
            return False, "未填写 PushPlus Token"
        started = now_iso()
        entry = {
            "ts": started,
            "started": started,
            "mode": "通知测试",
            "prewarmed": 2,
            "success": 1,
            "failed": 1,
            "exported": 1,
            "duration": 96.4,
            "note": "这是一条测试消息，收到即表示 PushPlus 配置正确。",
        }
        details = [
            {"id": "test-ok", "name": "示例影片 · 4K 成功样例", "status": "ok", "note": "3840x2160 HEVC"},
            {"id": "test-fail", "name": "示例影片 · 失败样例", "status": "fail", "note": "PlaybackInfo 超时（示例）"},
        ]
        content = "【测试消息】以下为示例数据，并非真实预热结果。<br>" + self._pushplus_content(entry, details)
        return self._push_plus(token, "Emby 预热通知测试", content)

    def _push_plus(self, token: str, title: str, content: str) -> tuple[bool, str]:
        payload = {
            "token": token,
            "title": title,
            "content": content,
            "template": "html",
        }
        try:
            request = urllib.request.Request(
                "https://www.pushplus.plus/send",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            raw = urllib.request.urlopen(request, timeout=15).read()
            data = json.loads(raw.decode("utf-8", "ignore") or "{}")
            if int(data.get("code") or 0) == 200:
                self.bus.ok("PushPlus 通知已发送")
                return True, "测试通知已发送，请在微信查看"
            message = str(data.get("msg") or data)
            self.bus.warn(f"PushPlus 通知失败：{message}")
            return False, message
        except Exception as exc:
            self.bus.warn(f"PushPlus 通知推送失败：{exc}")
            return False, str(exc)

    # ---------------------------------------------------- 备份同步通知
    def notify_sync(self, result: dict[str, Any], details: list[dict[str, str]]) -> None:
        """备份同步完成通知：与预热通知同格式，只推送有实际复制（变动）的任务。"""
        copied = int(result.get("copied") or 0)
        if copied <= 0:
            return  # 无变动不通知（与预热通知"只推有实际预热"同一原则）
        cfg = self.config.get()
        notify = cfg.get("notify", {}) or {}
        url = (notify.get("webhook_url") or "").strip()
        token = (notify.get("pushplus_token") or "").strip()
        if not url and not token:
            return
        skipped = int(result.get("skipped") or 0)
        failed = int(result.get("failed") or 0)
        started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(result.get("ts") or time.time()) - float(result.get("duration") or 0)))
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "started": started,
            "mode": f"备份同步（{result.get('reason') or '手动'}）",
            "copied": copied,
            "skipped": skipped,
            "failed": failed,
            "duration": result.get("duration") or 0,
            "note": result.get("note") or result.get("error") or "",
        }
        if url:
            payload = {
                "title": "Emby 备份同步完成",
                "text": (
                    f"开始时间：{started}\n"
                    f"触发方式：{entry['mode']}\n持续时长：{self._duration_text(entry['duration'])}\n"
                    f"复制 {copied} · 跳过 {skipped} · 失败 {failed}\n"
                    f"说明：{entry['note']}"
                ),
                **entry,
                "details": details,
            }
            try:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(request, timeout=10).read()
                self.bus.ok("备份同步 Webhook 通知已发送", LogBus.SYNC)
            except Exception as exc:
                self.bus.warn(f"备份同步 Webhook 通知推送失败：{exc}", LogBus.SYNC)
        if token:
            title = f"Emby 备份同步完成：复制 {copied} 个" + (f" / 失败 {failed}" if failed else "")
            self._push_plus(token, title, self._sync_pushplus_content(entry, details))

    @classmethod
    def _sync_pushplus_content(cls, entry: dict[str, Any], details: list[dict[str, str]]) -> str:
        """同步通知正文（html 模板），结构与预热通知一致。"""
        lines = [
            f"<b>开始时间</b>：{html.escape(str(entry.get('started') or ''))}",
            f"<b>持续时长</b>：{cls._duration_text(entry.get('duration'))}",
            f"<b>任务类型</b>：{html.escape(str(entry.get('mode') or ''))}",
            f"<b>复制 / 跳过 / 失败</b>：<span style=\"color:#15a05a\">{int(entry.get('copied') or 0)}</span> / "
            f"{int(entry.get('skipped') or 0)} / "
            f"<span style=\"color:#e11d48\">{int(entry.get('failed') or 0)}</span>",
        ]
        if details:
            lines.append("<br><b>同步明细</b>")
            for index, item in enumerate(details, 1):
                rel = html.escape(str(item.get("rel") or item.get("name") or ""))
                lines.append(f"{index}. ✅ {rel}")
        if entry.get("note"):
            lines.append(f"<br><b>说明</b>：{html.escape(str(entry['note']))}")
        return "<br>".join(lines)

    def webhook_test(self, url: str) -> tuple[bool, str]:
        """向指定 Webhook 地址推送一条测试消息（面板"Webhook 测试"用）。"""
        url = (url or "").strip()
        if not url:
            return False, "未填写 Webhook 地址"
        started = now_iso()
        payload = {
            "title": "Emby 预热通知测试（Webhook）",
            "text": "【测试消息】收到即表示 Webhook 地址配置正确。",
            "test": True,
            "ts": started,
            "started": started,
            "mode": "通知测试",
            "prewarmed": 2,
            "success": 1,
            "failed": 1,
            "exported": 1,
            "duration": 96.4,
            "details": [
                {"id": "test-ok", "name": "示例影片 · 4K 成功样例", "status": "ok", "note": "3840x2160 HEVC"},
                {"id": "test-fail", "name": "示例影片 · 失败样例", "status": "fail", "note": "PlaybackInfo 超时（示例）"},
            ],
        }
        try:
            request = urllib.request.Request(
                url,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(request, timeout=15).read()
            return True, "测试消息已发送"
        except Exception as exc:
            return False, str(exc)
