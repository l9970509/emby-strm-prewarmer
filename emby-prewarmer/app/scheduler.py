"""定时调度：定时预热 + 新片入库监听 + 失败队列自动循环重试。

线程每 5 秒醒来一次，判断是否到达执行时间。预热任务本身在独立线程里跑，
调度器只负责"发起"，因此不会阻塞面板。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from .config import ConfigStore
from .engine import Engine
from .state import LogBus, Store, fmt_ts


class Scheduler(threading.Thread):
    def __init__(self, config: ConfigStore, engine: Engine, store: Store, bus: LogBus, syncer=None, strmgen=None):
        super().__init__(daemon=True, name="scheduler")
        self.config = config
        self.engine = engine
        self.store = store
        self.bus = bus
        self.syncer = syncer
        self.strmgen = strmgen
        self._stop = threading.Event()
        self._was_busy = False
        self._next_schedule: float | None = None
        self._next_retry: float | None = None
        self._next_watch: float | None = None
        self._next_sync: float | None = None
        self._next_strm: float | None = None
        self._last_watch_trigger = 0.0
        self._watch_poke = threading.Event()   # webhook 唤醒标志（防抖合并）

    def poke_watch(self, reason: str = "") -> None:
        """外部（Emby Webhook）请求触发一次新片检测；忙时挂起，空闲后执行。"""
        self._watch_poke.set()
        if reason:
            self.bus.info(f"📩 已排队新片检测（{reason}）")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ 主循环
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # pragma: no cover
                self.bus.error(f"调度器异常：{type(exc).__name__}: {exc}")
            self._stop.wait(5)

    def _tick(self) -> None:
        cfg = self.config.get()
        now = time.time()

        # ---- 定时备份同步（独立于预热任务，可与预热并行执行）
        sync_cfg = cfg.get("sync") or {}
        if self.syncer is not None and sync_cfg.get("enabled") and sync_cfg.get("schedule_enabled"):
            last_sync = float(self.store.meta_get("sync_last_ts") or 0)
            self._next_sync = self._compute_next_schedule(
                {"mode": "daily", "daily_times": [sync_cfg.get("schedule_time") or "04:30"]},
                last_sync,
                now,
            )
            if now >= self._next_sync:
                # 先记时间再执行，避免失败时每 5 秒重试风暴
                self.store.meta_set("sync_last_ts", now)
                self.store.save()
                self.bus.info("⏰ 定时备份同步触发", LogBus.SYNC)
                self.syncer.run_async("定时同步")
        else:
            self._next_sync = None

        # ---- 定时 STRM 生成（网盘 → strm，与预热/同步互不阻塞）
        strm_cfg = cfg.get("strm") or {}
        if self.strmgen is not None and strm_cfg.get("enabled") and strm_cfg.get("schedule_enabled"):
            last_strm = float(self.store.meta_get("strm_last_ts") or 0)
            self._next_strm = self._compute_next_schedule(
                {"mode": "daily", "daily_times": [strm_cfg.get("schedule_time") or "02:30"]},
                last_strm,
                now,
            )
            if now >= self._next_strm:
                self.store.meta_set("strm_last_ts", now)
                self.store.save()
                self.bus.info("⏰ 定时 STRM 生成触发", LogBus.STRM)
                self.strmgen.run_async("定时生成")
        else:
            self._next_strm = None

        # 任务刚结束 → 重置自动重试计时，避免立刻又触发
        if self._was_busy and not self.engine.busy():
            self.store.meta_set("retry_last_ts", now)
            self.store.save()
        self._was_busy = self.engine.busy()

        if self.engine.busy():
            return

        # ---- STRM 生成后排队等待的自动预热（生成时预热正忙则在这里补执行）
        if self.store.meta_get("strm_pending_prewarm") and cfg["emby"].get("token"):
            self.store.meta_set("strm_pending_prewarm", False)
            self.store.save()
            if self.engine.start("full"):
                self.bus.info("🔥 STRM 已就绪，空闲后自动开始增量预热", LogBus.STRM)
                return

        schedule = cfg["schedule"]
        last_run = float(self.store.meta_get("schedule_last_ts") or 0)
        if not last_run:
            # 首次没有记录时，以"启动时间"为基准，避免刚装好就立刻跑
            last_run = float(self.store.meta_get("boot_ts") or 0)
            if not last_run:
                self.store.meta_set("boot_ts", now)
                self.store.save()
                last_run = now

        self._next_schedule = self._compute_next_schedule(schedule, last_run, now)
        if (
            schedule["enabled"]
            and self._next_schedule is not None
            and now >= self._next_schedule
        ):
            self.store.meta_set("schedule_last_ts", now)
            self.store.save()
            if schedule["mode"] == "interval":
                mode_text = f"每 {int(schedule['interval_minutes'])} 分钟"
            else:
                mode_text = "每天 " + " / ".join(schedule.get("daily_times") or [schedule.get("daily_time", "03:30")])
            self.bus.info(f"⏰ 定时预热触发（{mode_text}）")
            self.engine.start("full")
            return

        # ---- 新片入库监听（轮询）+ Webhook 唤醒
        watch = cfg["watch"]
        hook = cfg["hook"]
        if not hook["enabled"]:
            self._watch_poke.clear()

        run_watch = False
        watch_reason = ""
        if watch["enabled"]:
            poll = float(watch["poll_seconds"])
            cursor = float(self.store.meta_get("watch_last_ts") or 0)
            if not cursor:
                # 首次开启：以启动时间为基准，避免把整个老库都当"新片"
                cursor = float(self.store.meta_get("boot_ts") or now)
                self.store.meta_set("watch_last_ts", cursor)
                self.store.save()
            self._next_watch = cursor + poll
            # 用"上次实际触发时间"节流：即使游标因 Emby 异常没推进，
            # 也严格按 poll_seconds 间隔触发，避免失败时每 5 秒重试风暴
            if now >= self._next_watch and now >= (self._last_watch_trigger + poll):
                run_watch = True
                watch_reason = "轮询周期到达"
        else:
            self._next_watch = None

        if not run_watch and hook["enabled"] and self._watch_poke.is_set():
            # Emby 入库通知唤醒（burst 通知会合并成一次检测）
            self._watch_poke.clear()
            run_watch = True
            watch_reason = "Emby 入库通知"

        if run_watch:
            self._last_watch_trigger = now
            self.bus.info(f"👀 触发新片检测（{watch_reason}）")
            self.engine.start("watch")
            return

        loop = cfg["failure_loop"]
        retry_last = float(self.store.meta_get("retry_last_ts") or 0)
        interval = float(loop["auto_retry_interval_seconds"])
        self._next_retry = (retry_last + interval) if retry_last else now + interval

        if not loop["enabled"] or not loop["auto_retry"]:
            return
        if now < self._next_retry:
            return
        due = self.store.due_failures(now)
        if not due:
            return
        self.store.meta_set("retry_last_ts", now)
        self.store.save()
        self.bus.info(f"♻️ 失败队列自动循环预热：{len(due)} 个待重试项目")
        self.engine.start("failed")

    # -------------------------------------------------------------- 时间计算
    @staticmethod
    def _compute_next_schedule(schedule: dict[str, Any], last_run: float, now: float) -> float | None:
        if schedule["mode"] == "daily":
            # 支持一天多个执行时刻：在候选里取"上次执行之后最早的一个"
            times = schedule.get("daily_times") or [schedule.get("daily_time") or "03:30"]
            today = datetime.now().replace(second=0, microsecond=0)
            best: datetime | None = None
            for raw in times:
                try:
                    hour, minute = (int(x) for x in str(raw).split(":")[:2])
                except (ValueError, TypeError):
                    continue
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    continue
                slot = today.replace(hour=hour, minute=minute)
                if slot.timestamp() <= last_run:
                    slot += timedelta(days=1)
                if best is None or slot < best:
                    best = slot
            if best is None:
                best = today.replace(hour=3, minute=30)
                if best.timestamp() <= last_run:
                    best += timedelta(days=1)
            return best.timestamp()
        interval = float(schedule["interval_minutes"]) * 60
        return last_run + interval

    def next_run_text(self) -> str:
        cfg = self.config.get()
        if not cfg["schedule"]["enabled"]:
            return "未开启"
        ts = self._next_schedule
        if ts is None:
            return "待计算"
        if ts <= time.time():
            return "即将执行"
        return fmt_ts(ts)

    def next_retry_text(self) -> str:
        cfg = self.config.get()
        loop = cfg["failure_loop"]
        if not loop["enabled"] or not loop["auto_retry"]:
            return "未开启"
        if not self.store.failure_count():
            return "队列为空"
        ts = self._next_retry
        if not ts or ts <= time.time():
            return "即将执行"
        return fmt_ts(ts)

    def next_watch_text(self) -> str:
        cfg = self.config.get()
        watch = cfg["watch"]
        if not watch["enabled"]:
            return "未开启"
        ts = self._next_watch
        if ts is None:
            return "待计算"
        if ts <= time.time():
            return "即将检测"
        return fmt_ts(ts)

    def next_sync_text(self) -> str:
        cfg = self.config.get()
        sync = cfg.get("sync") or {}
        if not sync.get("enabled") or not sync.get("schedule_enabled"):
            return "未开启"
        ts = self._next_sync
        if ts is None:
            return "待计算"
        if ts <= time.time():
            return "即将执行"
        return fmt_ts(ts)

    def next_strm_text(self) -> str:
        cfg = self.config.get()
        strm = cfg.get("strm") or {}
        if not strm.get("enabled") or not strm.get("schedule_enabled"):
            return "未开启"
        ts = self._next_strm
        if ts is None:
            return "待计算"
        if ts <= time.time():
            return "即将执行"
        return fmt_ts(ts)

    def info(self) -> dict[str, Any]:
        cfg = self.config.get()
        loop = cfg["failure_loop"]
        watch = cfg["watch"]
        hook = cfg["hook"]
        sync = cfg.get("sync") or {}
        strm = cfg.get("strm") or {}
        return {
            "schedule_enabled": cfg["schedule"]["enabled"],
            "schedule_mode": cfg["schedule"]["mode"],
            "next_run": self.next_run_text(),
            "retry_enabled": bool(loop["enabled"] and loop["auto_retry"]),
            "next_retry": self.next_retry_text(),
            "watch_enabled": bool(watch["enabled"]),
            "watch_poll_seconds": int(watch["poll_seconds"]),
            "next_watch": self.next_watch_text(),
            "hook_enabled": bool(hook["enabled"]),
            "hook_token_set": bool(hook["token"]),
            "sync_enabled": bool(sync.get("enabled")),
            "next_sync": self.next_sync_text(),
            "strm_enabled": bool(strm.get("enabled")),
            "next_strm": self.next_strm_text(),
        }
