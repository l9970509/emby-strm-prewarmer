"""配置管理：默认值、读写、类型校验。

配置文件为纯 JSON，便于面板读写与人工编辑。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any


def normalize_prefix(raw: Any) -> str:
    """规范化 STRM 前缀：去尾斜杠、补协议。

    用户常只填 ``192.168.101.99:5244/d``（漏了 http://），拼出来的 strm 没有协议头，
    Emby / 播放器无法识别。这里统一补全；rclone 挂载点（/mnt/115）、Windows 路径原样保留。
    """
    p = str(raw or "").strip().rstrip("/")
    if not p:
        return ""
    if p.lower().startswith(("http://", "https://")):
        return p
    # rclone / 本地挂载点：/mnt/115、\\nas\share、D:\media
    if p.startswith(("/", "\\")):
        return p
    if len(p) > 1 and p[1] == ":":
        return p
    # 形如 192.168.101.99:5244/d、nas.local/d、emby.example.com
    return "http://" + p


def is_masked_secret(value: Any) -> bool:
    """判断回传的密钥是不是「打码占位值」，而不是用户真正输入的密钥。

    前端拿到的是 ``public()`` 处理过的 ``********wxyz`` 形式，真实令牌/密码
    不会以 ``*`` 开头，所以只要为空或首字符是 ``*`` 就认定「未修改」。
    """
    text = str(value or "").strip()
    if not text:
        return True
    if text.startswith("*"):
        return True
    return set(text) <= {"*", "•", "·"}


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
        # 排除的文件夹路径（每行一个）：可填相对来源 A 的路径、绝对路径或文件夹名
        "exclude": [],
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
    "strm": {
        # STRM 生成：从 OpenList（AList 兼容）网盘目录同步出 .strm 文件，
        # 生成完成后可自动触发增量预热，与备份同步串成闭环
        "enabled": False,
        "openlist_url": "http://127.0.0.1:5244",
        # 长期令牌（OpenList 后台 → 其他 → 令牌）；留空则用账号密码登录换 token
        "openlist_token": "",
        "openlist_username": "",
        "openlist_password": "",
        # strm 内容前缀：OpenList 的 http://IP:5244/d 或 rclone 挂载点，
        # 实际写入 = 前缀 + 网盘完整路径；支持 {path} 占位符
        "prefix": "",
        # 生成任务（每个目录一项，各自独立定时与手动生成）：
        # [{id, name, remote, local, enabled, mode, rebuild, schedule_enabled, schedule_times[]}]
        "tasks": [],
        "exts": "mkv,mp4,ts,m2ts,iso,avi,wmv,mov,flv,rmvb,webm,mpg,mpeg",
        # 一并下载的附件扩展名（nfo/字幕/海报），留空表示不下载
        "extras": "nfo,srt,ass,ssa,sub,sup,vtt,jpg,jpeg,png,webp",
        "max_file_mb": 200,          # 附件下载大小上限
        # 以下两项为 1.7.0 的全局定时/重建，仅用于旧配置迁移，新配置按任务保存
        "rebuild": False,
        "schedule_enabled": False,
        "schedule_time": "02:30",
    },
    "notify": {
        "webhook_url": "",
        # PushPlus（pushplus.plus）推送 Token，留空不启用
        "pushplus_token": "",
    },
    "log": {
        # 日志文件保留天数：落盘的每日日志超过该天数自动清除（0 表示永久保留）
        "retention_days": 7,
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
    "strm.max_file_mb": (0, 4096),
    "log.retention_days": (0, 365),
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


def _norm_clock(value: Any, fallback: str) -> str:
    """把 "2:5" / "02：05" 规范成 "02:05"；不合法时用 fallback。"""
    text = str(value or "").strip().replace("：", ":")
    parts = text.split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return f"{hour:02d}:{minute:02d}"
    except (ValueError, IndexError):
        return fallback


def _norm_time_list(value: Any) -> list[str]:
    """接收 ["02:30", "14:00"] 或 "02:30,14:00"，去重并排序。"""
    if isinstance(value, str):
        items: list[Any] = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    out: set[str] = set()
    for item in items:
        for piece in str(item or "").replace("，", ",").split(","):
            text = piece.strip().replace("：", ":")
            if not text:
                continue
            normalized = _norm_clock(text, "")
            if normalized:
                out.add(normalized)
    return sorted(out)


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
    # 排除路径：兼容「每行/逗号分隔的字符串」与「数组」两种写法。
    # 注意 _coerce 已按默认值(list)把字符串按换行拆过，所以这里再按逗号拆每项。
    raw_exclude = sync_cfg.get("exclude") or []
    if isinstance(raw_exclude, str):
        raw_exclude = [raw_exclude]
    seen_ex: set[str] = set()
    excludes: list[str] = []
    if isinstance(raw_exclude, (list, tuple)):
        for item in raw_exclude:
            for piece in str(item or "").replace("，", ",").split(","):
                text = piece.strip().replace("\\", "/").rstrip("/")
                if text and text not in seen_ex:
                    seen_ex.add(text)
                    excludes.append(text)
    sync_cfg["exclude"] = excludes
    raw_time = str(sync_cfg.get("schedule_time") or "04:30").strip().replace("：", ":")
    parts = raw_time.split(":")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        sync_cfg["schedule_time"] = f"{hour:02d}:{minute:02d}"
    except (ValueError, IndexError):
        sync_cfg["schedule_time"] = "04:30"

    # ---------------------------------------------------------- STRM 生成
    strm_cfg = cfg["strm"]
    strm_cfg["openlist_url"] = (strm_cfg.get("openlist_url") or "").strip().rstrip("/")
    strm_cfg["openlist_token"] = (strm_cfg.get("openlist_token") or "").strip()
    strm_cfg["openlist_username"] = (strm_cfg.get("openlist_username") or "").strip()
    strm_cfg["prefix"] = normalize_prefix(strm_cfg.get("prefix"))
    if not strm_cfg["prefix"] and strm_cfg["openlist_url"]:
        # 没填前缀就跟 OpenList 地址走：<地址>/d + 网盘完整路径
        strm_cfg["prefix"] = strm_cfg["openlist_url"] + "/d"
    raw_strm_time = str(strm_cfg.get("schedule_time") or "02:30").strip().replace("：", ":")
    legacy_strm_time = _norm_clock(raw_strm_time, "02:30")
    strm_cfg["schedule_time"] = legacy_strm_time

    # 生成任务：每个目录一项，兼容旧版 [{remote, local}] 与 "remote = local" 行写法
    raw_user_strm = (user_cfg or {}).get("strm") or {}
    raw_tasks = raw_user_strm.get("tasks")
    legacy_sched_on = bool(raw_user_strm.get("schedule_enabled"))
    legacy_rebuild = bool(raw_user_strm.get("rebuild"))
    tasks: list[dict[str, Any]] = []
    if isinstance(raw_tasks, str):
        raw_tasks = [line for line in raw_tasks.splitlines() if line.strip()]
    if isinstance(raw_tasks, list):
        for index, item in enumerate(raw_tasks):
            remote = local = ""
            raw: dict[str, Any] = {}
            if isinstance(item, dict):
                raw = item
                remote = str(item.get("remote") or "").strip()
                local = str(item.get("local") or "").strip()
            elif isinstance(item, str):
                line = item.strip().replace("＝", "=")
                if "=" in line:
                    remote, local = (part.strip() for part in line.split("=", 1))
                elif "|" in line:
                    remote, local = (part.strip() for part in line.split("|", 1))
            if not remote or not local:
                continue
            if not remote.startswith("/"):
                remote = "/" + remote
            remote = remote.rstrip("/") or "/"
            local = local.rstrip("/")

            # 任务 ID 必须稳定：缺省时按网盘目录生成，保证定时记录能对上
            task_id = str(raw.get("id") or "").strip()
            if not task_id:
                task_id = "t" + hashlib.md5(remote.encode("utf-8")).hexdigest()[:8]
            if any(t["id"] == task_id for t in tasks):
                task_id = f"{task_id}-{index + 1}"

            name = str(raw.get("name") or "").strip()
            if not name:
                name = remote.rstrip("/").split("/")[-1] or f"任务 {index + 1}"

            # 定时时刻：优先任务自己的；旧配置没写则继承全局设置
            if "schedule_times" in raw or "schedule_enabled" in raw:
                times = _norm_time_list(raw.get("schedule_times"))
                flag = raw.get("schedule_enabled", True if times else False)
                sched_on = bool(flag) and bool(times)
            else:
                times = [legacy_strm_time] if legacy_sched_on else []
                sched_on = legacy_sched_on

            tasks.append(
                {
                    "id": task_id,
                    "name": name,
                    "remote": remote,
                    "local": local,
                    # 上次使用的本地根目录：换了目录后用来把已生成的 strm 自动搬过去
                    "prev_local": str(raw.get("prev_local") or "").strip(),
                    "enabled": _coerce(True, raw.get("enabled", True)),
                    # 此任务不同步：其输出目录自动加入备份同步排除列表
                    "no_sync": _coerce(False, raw.get("no_sync", False)),
                    "mode": "full" if str(raw.get("mode") or "").strip().lower() == "full" else "incremental",
                    "rebuild": _coerce(True, raw["rebuild"]) if "rebuild" in raw else legacy_rebuild,
                    "schedule_enabled": sched_on,
                    "schedule_times": times,
                }
            )
    strm_cfg["tasks"] = tasks
    # 全局定时/重建仅作旧配置兼容，逻辑已按任务走
    strm_cfg["schedule_enabled"] = any(t["schedule_enabled"] for t in tasks)
    strm_cfg["rebuild"] = any(t["rebuild"] for t in tasks)
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
            # 本地目录被改动时记住旧位置，STRM 生成时据此把已生成的文件搬过去
            old_tasks = {
                str(t.get("id")): t
                for t in ((self._data.get("strm") or {}).get("tasks") or [])
            }
            for task in merged["strm"]["tasks"]:
                previous = old_tasks.get(str(task.get("id")))
                if previous is None:
                    continue
                old_local = str(previous.get("local") or "").strip()
                if old_local and old_local != task["local"]:
                    task["prev_local"] = old_local
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
        # 存下来的就是打码值，说明曾被前端整体回传覆盖过，需要用户重填
        cfg["emby"]["token_corrupt"] = is_masked_secret(token) and bool(token)
        cfg["emby"]["token"] = ("*" * 8 + token[-4:]) if len(token) > 8 else ("*" * len(token))
        cfg["server"]["password_set"] = bool(cfg["server"]["password"])
        cfg["server"]["password"] = ""
        pushplus = cfg["notify"].get("pushplus_token") or ""
        cfg["notify"]["pushplus_token_set"] = bool(pushplus)
        cfg["notify"]["pushplus_token_corrupt"] = is_masked_secret(pushplus) and bool(pushplus)
        cfg["notify"]["pushplus_token"] = ("*" * 8 + pushplus[-4:]) if len(pushplus) > 8 else ("*" * len(pushplus))
        strm = cfg["strm"]
        ol_token = strm.get("openlist_token") or ""
        strm["openlist_token_set"] = bool(ol_token)
        strm["openlist_token_corrupt"] = is_masked_secret(ol_token) and bool(ol_token)
        strm["openlist_token"] = ("*" * 8 + ol_token[-4:]) if len(ol_token) > 8 else ("*" * len(ol_token))
        ol_password = strm.get("openlist_password") or ""
        strm["openlist_password_set"] = bool(ol_password)
        strm["openlist_password_corrupt"] = is_masked_secret(ol_password) and bool(ol_password)
        strm["openlist_password"] = "" if not ol_password else "*" * 8
        return cfg
