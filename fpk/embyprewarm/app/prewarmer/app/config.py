"""配置管理：默认值、读写、类型校验。

配置文件为纯 JSON，便于面板读写与人工编辑。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "emby": {
        # Emby 地址，是否带 /emby 前缀都可以，程序会自动探测
        "url": "http://127.0.0.1:8096",
        # Emby 控制台 -> 高级 -> API 密钥 生成的 Token
        "token": "",
        # 留空则自动使用第一个用户
        "user_id": "",
        "timeout": 180,
    },
    "scan": {
        # Emby 内部看到的媒体根目录，每行一个（注意不是宿主机路径）
        "roots": [],
        # Emby 路径 -> 本机路径，用于读取 strm 文件内容做换源指纹
        "path_mappings": {},
        "only_strm": True,
        "include_types": "Movie,Episode,Video",
    },
    "prewarm": {
        "concurrency": 2,
        "delay_seconds": 1.0,
        "retry_delay_seconds": 10,
        "max_retries": 2,
        "max_items_per_run": 0,
        "verify": True,
    },
    "schedule": {
        "enabled": False,
        "mode": "interval",          # interval | daily
        "interval_minutes": 180,
        # 每天定时可配置多个时刻（HH:MM），daily_time 保留为兼容字段
        "daily_times": ["03:30"],
        "daily_time": "03:30",
    },
    "watch": {
        # 新影片入库监听：定期向 Emby 查询 DateCreated 晚于上次检测的 STRM，
        # 发现新片立刻自动预热（完整 MediaInfo 的自动跳过）
        "enabled": False,
        "poll_seconds": 300,         # 检测间隔
        "overlap_seconds": 60,       # 与上次检测的重叠窗口，避免漏掉边缘条目
    },
    "hook": {
        # Emby 入库通知（Webhook）唤醒：Emby「Webhooks」插件在 library.new 事件
        # 时 POST 到 http://NAS:端口/hook/emby?api_key=令牌，立即触发新片检测
        "enabled": False,
        "token": "",                 # 留空表示不校验 api_key
    },
    "failure_loop": {
        "enabled": True,
        "rounds": 2,                  # 本次运行内，失败项额外再循环几轮
        "round_delay_seconds": 180,
        "auto_retry": True,           # 后台对失败队列持续循环预热
        "auto_retry_interval_seconds": 600,
        "max_attempts": 0,            # 0 = 不限次数
    },
    "export": {
        # 预热成功后，在 .strm 同目录导出 {filename}-mediainfo.json 备份
        "enabled": True,
        # 已有同名备份是否覆盖（STRM 换源导致信息变化时会强制覆盖）
        "overwrite": False,
    },
    "sync": {
        # 备份同步：把 mediainfo.json（可选图片/字幕）按相同目录结构
        # 从来源目录 A 增量复制到目标目录 B（B 已存在跳过，不存在才复制）
        "enabled": False,
        # 来源目录 A（本地路径，每行一个）；留空自动使用媒体库映射后的本机路径
        "source": [],
        "target": "",                # 目标目录 B
        "images": True,              # 一起同步图片（海报/fanart 等）
        "subtitles": True,           # 一起同步字幕
        "after_prewarm": True,       # 预热任务结束后自动同步
        "schedule_enabled": False,   # 每天定时同步
        "schedule_time": "04:30",
    },
    "server": {
        "host": "0.0.0.0",
        "port": 8787,
        "password": "",
    },
    "notify": {
        "webhook_url": "",
        # PushPlus（pushplus.plus）推送 Token，留空不启用
        "pushplus_token": "",
    },
}

# 数值范围约束，避免面板里填出离谱的值把机器拖死
CLAMPS: dict[str, tuple[float, float]] = {
    "emby.timeout": (10, 1800),
    "prewarm.concurrency": (1, 8),
    "prewarm.delay_seconds": (0, 120),
    "prewarm.retry_delay_seconds": (0, 3600),
    "prewarm.max_retries": (0, 10),
    "prewarm.max_items_per_run": (0, 1000000),
    "schedule.interval_minutes": (5, 10080),
    "watch.poll_seconds": (60, 86400),
    "watch.overlap_seconds": (0, 3600),
    "failure_loop.rounds": (0, 10),
    "failure_loop.round_delay_seconds": (0, 86400),
    "failure_loop.auto_retry_interval_seconds": (60, 86400),
    "failure_loop.max_attempts": (0, 100),
    "server.port": (1, 65535),
}


def normalize_root(value: str) -> str:
    """统一目录写法：去掉首尾空白，保证以 / 结尾。"""
    value = (value or "").strip()
    if not value:
        return ""
    if not value.endswith("/"):
        value += "/"
    return value


def _coerce(default: Any, value: Any) -> Any:
    """按默认值的类型把输入值转成正确类型。"""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return "" if value is None else str(value)
    if isinstance(default, list):
        if isinstance(value, str):
            value = [line for line in value.splitlines() if line.strip()]
        if not isinstance(value, list):
            return list(default)
        return [str(item) for item in value if str(item).strip()]
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return copy.deepcopy(default)
        if not default:
            # 默认空字典表示"自由键值"，如 path_mappings
            return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip()}
        merged = {}
        for key, sub_default in default.items():
            merged[key] = _coerce(sub_default, value.get(key, sub_default))
        return merged
    return value


def _apply_clamps(cfg: dict[str, Any]) -> dict[str, Any]:
    for dotted, (low, high) in CLAMPS.items():
        section, key = dotted.split(".", 1)
        if section not in cfg or key not in cfg[section]:
            continue
        raw = cfg[section][key]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        number = max(low, min(high, number))
        # 保持原来的整数/浮点类型
        cfg[section][key] = int(number) if isinstance(raw, int) else number
    return cfg


def sanitize(user_cfg: dict[str, Any]) -> dict[str, Any]:
    """把外部传入的配置合并到默认值之上，并做类型与范围校验。"""
    cfg = _coerce(DEFAULT_CONFIG, user_cfg or {})
    cfg = _apply_clamps(cfg)

    # 根目录规范化
    cfg["scan"]["roots"] = [normalize_root(r) for r in cfg["scan"]["roots"] if r.strip()]

    # 路径映射规范化（键和值都补 /）
    mappings = {}
    for emby_root, host_root in (cfg["scan"]["path_mappings"] or {}).items():
        emby_root = normalize_root(emby_root)
        host_root = (host_root or "").strip()
        if emby_root and host_root:
            mappings[emby_root] = host_root.rstrip("/") or "/"
    cfg["scan"]["path_mappings"] = mappings

    cfg["emby"]["url"] = (cfg["emby"]["url"] or "").strip().rstrip("/")
    cfg["emby"]["token"] = (cfg["emby"]["token"] or "").strip()

    # 定时时刻：支持多时段（daily_times 数组），兼容旧的单值 daily_time
    user_schedule = (user_cfg or {}).get("schedule") or {}
    legacy = (cfg["schedule"].get("daily_time") or "").strip()
    if "daily_times" in user_schedule:
        candidates = list(cfg["schedule"].get("daily_times") or [])
    elif legacy:
        # 老配置文件只有 daily_time：迁移成多时段数组
        candidates = [legacy]
    else:
        candidates = list(cfg["schedule"].get("daily_times") or [])
    times: list[str] = []
    for item in candidates:
        text = str(item).strip().replace("：", ":").replace(".", ":")
        parts = text.split(":")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            slot = f"{hour:02d}:{minute:02d}"
            if slot not in times:
                times.append(slot)
    times.sort()
    cfg["schedule"]["daily_times"] = times or ["03:30"]
    cfg["schedule"]["daily_time"] = cfg["schedule"]["daily_times"][0]
    cfg["notify"]["pushplus_token"] = (cfg["notify"]["pushplus_token"] or "").strip()

    # 备份同步：目录规范化 + 定时时刻校验
    sync_cfg = cfg["sync"]
    sync_cfg["target"] = normalize_root(sync_cfg.get("target") or "")
    sync_cfg["source"] = [
        normalize_root(str(s)) for s in (sync_cfg.get("source") or []) if str(s).strip()
    ]
    raw_time = str(sync_cfg.get("schedule_time") or "04:30").strip().replace("：", ":")
    parts = raw_time.split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        sync_cfg["schedule_time"] = f"{hour:02d}:{minute:02d}"
    except (ValueError, IndexError):
        sync_cfg["schedule_time"] = "04:30"
    return cfg


class ConfigStore:
    """线程安全的配置容器，支持面板热更新。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
        # 环境变量可覆盖 Token，便于容器/命令行注入
        env_token = os.environ.get("EMBY_API_TOKEN", "").strip()
        if env_token:
            raw.setdefault("emby", {})["token"] = env_token
        return sanitize(raw)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            merged = sanitize(patch)
            self._data = merged
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
            return copy.deepcopy(merged)

    def public(self) -> dict[str, Any]:
        """给前端用的配置：Token 打码，避免明文出现在浏览器里。"""
        cfg = self.get()
        token = cfg["emby"]["token"]
        cfg["emby"]["token_set"] = bool(token)
        cfg["emby"]["token"] = ("*" * 8 + token[-4:]) if len(token) > 8 else ("*" * len(token))
        cfg["server"]["password_set"] = bool(cfg["server"]["password"])
        cfg["server"]["password"] = ""
        pushplus = cfg["notify"].get("pushplus_token") or ""
        cfg["notify"]["pushplus_token_set"] = bool(pushplus)
        cfg["notify"]["pushplus_token"] = ("*" * 8 + pushplus[-4:]) if len(pushplus) > 8 else ("*" * len(pushplus))
        return cfg
