"""Emby / Jellyfin API 客户端（纯标准库实现）。

关键点：通过 POST /Items/{id}/PlaybackInfo 触发 Emby 真实读取媒体文件，
让 ffprobe 探测出真实的 MediaInfo 并写回 Emby 自己的媒体库数据库。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

from . import __version__

USER_AGENT = f"fnOS-Emby-Prewarmer/{__version__}"
DEFAULT_FIELDS = "Path,MediaSources,MediaStreams"
# 导出备份时需要章节信息，单独请求，避免拖慢全库列表
ITEM_FIELDS = "Path,MediaSources,MediaStreams,Chapters"


class EmbyError(Exception):
    """Emby 接口调用失败。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def is_local_host(host: str) -> bool:
    """判断是否是内网/本机地址，用于绕过系统代理。"""
    host = (host or "").strip("[]").lower()
    if not host:
        return True
    if host in ("localhost", "::1", "0.0.0.0"):
        return True
    if "." not in host:  # docker 网络里的容器名，如 emby
        return True
    if host.startswith("127."):
        return True
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


class EmbyClient:
    def __init__(self, base_url: str, token: str, timeout: int = 180):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (token or "").strip()
        self.timeout = int(timeout or 180)
        self.server_name = ""

    def _opener(self) -> urllib.request.OpenerDirector:
        # 内网地址强制直连，避免 NAS 上的 http_proxy 环境变量把请求带偏
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        if is_local_host(host):
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener()

    # ------------------------------------------------------------------ 底层
    def _build_url(self, path: str, params: dict[str, Any] | None) -> str:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean, doseq=True)
        return url

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.token:
            headers["X-Emby-Token"] = self.token
            headers["X-Emby-Authorization"] = (
                f'MediaBrowser Client="fnOS Prewarmer", Device="fnOS", '
                f'DeviceId="fnos-emby-prewarmer", Version="{__version__}", Token="{self.token}"'
            )
        return headers

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        method: str = "GET",
        body: Any = None,
        timeout: int | None = None,
    ) -> Any:
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            self._build_url(path, params), data=data, headers=headers, method=method
        )
        try:
            with self._opener().open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:  # pragma: no cover
                pass
            hint = {401: "API Token 无效", 403: "无权访问该接口", 404: "接口不存在，检查地址是否需要 /emby 前缀"}.get(exc.code, "")
            raise EmbyError(
                f"HTTP {exc.code} {exc.reason}{' - ' + hint if hint else ''}"
                + (f" | {detail}" if detail else ""),
                status=exc.code,
            ) from None
        except urllib.error.URLError as exc:
            raise EmbyError(f"无法连接 Emby（{self.base_url}）：{exc.reason}") from None
        except (socket.timeout, TimeoutError):
            raise EmbyError(f"请求 Emby 超时（>{timeout or self.timeout}s）") from None

        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -------------------------------------------------------------- 连接探测
    def probe(self) -> dict[str, Any]:
        """自动找到可用的 API 前缀，返回服务器公开信息。"""
        base = self.base_url.rstrip("/")
        candidates = [base]
        if base.endswith("/emby"):
            candidates.append(base[: -len("/emby")])
        else:
            candidates.append(base + "/emby")

        last_error: Exception | None = None
        for candidate in dict.fromkeys(candidates):
            self.base_url = candidate
            try:
                info = self.request("/System/Info/Public", timeout=min(self.timeout, 15))
            except EmbyError as exc:
                last_error = exc
                continue
            if isinstance(info, dict) and info.get("ServerName") is not None:
                self.server_name = str(info.get("ServerName") or "")
                return {
                    "base_url": candidate,
                    "server_name": self.server_name,
                    "version": str(info.get("Version", "")),
                    "product": str(info.get("ProductName") or "Emby"),
                }
        if last_error:
            raise last_error
        raise EmbyError("无法识别 Emby 服务")

    def users(self) -> list[dict[str, Any]]:
        data = self.request("/Users")
        return data if isinstance(data, list) else []

    def resolve_user_id(self, preferred: str = "") -> str:
        users = self.users()
        if not users:
            raise EmbyError("Emby 未返回任何用户，请检查 Token 权限")
        if preferred:
            for user in users:
                if str(user.get("Id")) == str(preferred):
                    return str(preferred)
            # 也允许按用户名匹配
            for user in users:
                if str(user.get("Name", "")).lower() == str(preferred).lower():
                    return str(user["Id"])
            raise EmbyError(f"未找到指定用户：{preferred}")
        for user in users:
            if user.get("Policy", {}).get("IsAdministrator"):
                return str(user["Id"])
        return str(users[0]["Id"])

    # ---------------------------------------------------------------- 媒体库
    def iter_items(
        self,
        include_types: str = "Movie,Episode,Video",
        fields: str = DEFAULT_FIELDS,
        page_size: int = 2000,
    ) -> Iterator[dict[str, Any]]:
        """分页拉取整个媒体库条目（含 MediaSources / MediaStreams）。"""
        start = 0
        while True:
            payload = self.request(
                "/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": include_types,
                    "Fields": fields,
                    "StartIndex": start,
                    "Limit": page_size,
                    "EnableImages": "false",
                    "EnableUserData": "false",
                },
            )
            items = (payload or {}).get("Items") or []
            for item in items:
                yield item
            total = int((payload or {}).get("TotalRecordCount") or 0)
            start += len(items)
            if not items or len(items) < page_size or (total and start >= total):
                break

    def iter_items_since(
        self,
        since_ts: float,
        include_types: str = "Movie,Episode,Video",
        fields: str = DEFAULT_FIELDS,
        page_size: int = 500,
    ) -> Iterator[dict[str, Any]]:
        """只拉取 DateCreated 晚于 since_ts 的条目（新片入库检测用）。

        Emby /Items 原生支持 MinDateCreated（ISO 8601 UTC），配合
        SortBy=DateCreated 让服务端过滤，避免全库扫描。
        """
        since_iso = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )
        start = 0
        while True:
            payload = self.request(
                "/Items",
                {
                    "Recursive": "true",
                    "IncludeItemTypes": include_types,
                    "Fields": fields,
                    "MinDateCreated": since_iso,
                    "SortBy": "DateCreated",
                    "SortOrder": "Descending",
                    "StartIndex": start,
                    "Limit": page_size,
                    "EnableImages": "false",
                    "EnableUserData": "false",
                },
            )
            items = (payload or {}).get("Items") or []
            for item in items:
                yield item
            total = int((payload or {}).get("TotalRecordCount") or 0)
            start += len(items)
            if not items or len(items) < page_size or (total and start >= total):
                break

    def get_item(self, user_id: str, item_id: str, fields: str = DEFAULT_FIELDS) -> dict[str, Any]:
        data = self.request(f"/Users/{user_id}/Items/{item_id}", {"Fields": fields})
        return data if isinstance(data, dict) else {}

    def libraries(self) -> list[dict[str, Any]]:
        data = self.request("/Library/MediaFolders")
        return (data or {}).get("Items") or []

    # ---------------------------------------------------------------- 预热
    def prewarm(self, user_id: str, item_id: str) -> Any:
        """核心调用：让 Emby 为该项目准备播放信息，从而触发真实探测。"""
        return self.request(
            f"/Items/{item_id}/PlaybackInfo",
            {
                "UserId": user_id,
                "IsPlayback": "true",
                "AutoOpenLiveStream": "true",
                "MaxStreamingBitrate": "200000000",
            },
            method="POST",
            body={},
        )

    def refresh_item(self, item_id: str) -> Any:
        """STRM 换源后强制刷新元数据（不显式动图片）。

        注意：Emby 4.x 的 ImageRefreshMode 枚举**没有** `None` 这个值，
        传 "None" 会直接 HTTP 400（Requested value 'None' was not found.）。
        这里只显式指定元数据刷新模式，图片走服务端默认策略；
        万一某些版本连 FullRefresh 也不认，退回不带参数的最简调用。
        """
        try:
            return self.request(
                f"/Items/{item_id}/Refresh",
                {"Recursive": "false", "MetadataRefreshMode": "FullRefresh"},
                method="POST",
            )
        except EmbyError:
            return self.request(
                f"/Items/{item_id}/Refresh",
                {"Recursive": "false"},
                method="POST",
            )


# ------------------------------------------------------------------ 工具函数
def media_streams(item: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stream
        for source in (item.get("MediaSources") or [])
        for stream in (source.get("MediaStreams") or [])
    ]


def has_complete_video(item: dict[str, Any]) -> bool:
    """与上游项目一致的判定：存在带编码与分辨率的视频流才算预热完成。"""
    return any(
        stream.get("Type") == "Video"
        and stream.get("Codec")
        and stream.get("Width")
        and stream.get("Height")
        for stream in media_streams(item)
    )


def describe_video(item: dict[str, Any]) -> str:
    """输出形如 1920x1080 HEVC 的简报，用于日志展示。"""
    for stream in media_streams(item):
        if stream.get("Type") == "Video" and stream.get("Width"):
            codec = str(stream.get("Codec") or "?").upper()
            width = stream.get("Width")
            height = stream.get("Height")
            return f"{width}x{height} {codec}"
    return ""
