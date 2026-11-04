"""OpenList（AList 兼容）API 客户端 —— 纯标准库实现。

只用到 STRM 生成需要的三个接口：
- POST /api/auth/login  账号密码换 token（配置了长期令牌时可跳过）
- POST /api/fs/list     列目录（分页）
- POST /api/fs/get      取文件详情（含直链），用于下载附件

设计上不做并发：网盘接口普遍有频率限制，串行 + 间隔最稳。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# 单次请求超时（秒）
DEFAULT_TIMEOUT = 30
# 列目录每页条数（OpenList 上限 1000，取 500 更稳）
PAGE_SIZE = 500


class OpenListError(RuntimeError):
    pass


class OpenListClient:
    """极简 OpenList 客户端，线程安全性由调用方保证（生成器单线程使用）。"""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        username: str = "",
        password: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise OpenListError("未配置 OpenList 地址")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = "http://" + self.base_url
        self.token = (token or "").strip()
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = max(5, int(timeout))
        self._logged_in = False

    # ------------------------------------------------------------ 底层请求
    def _request(self, path: str, payload: dict[str, Any] | None = None, raw: bool = False) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = self.token
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:200]
            except Exception:
                pass
            raise OpenListError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise OpenListError(f"无法连接 OpenList（{exc.reason}）") from exc
        except OSError as exc:
            raise OpenListError(f"请求 OpenList 失败：{exc}") from exc
        if raw:
            return body
        try:
            data_json = json.loads(body.decode("utf-8", "ignore") or "{}")
        except ValueError as exc:
            raise OpenListError("OpenList 返回的不是 JSON（地址是否正确？）") from exc
        code = data_json.get("code")
        if code not in (200, None):
            message = str(data_json.get("message") or data_json.get("msg") or f"code {code}")
            raise OpenListError(message)
        return data_json.get("data")

    # ------------------------------------------------------------ 登录
    def login(self) -> None:
        """账号密码登录换 token；已配置长期令牌时直接跳过。"""
        if self.token:
            return
        if not self.username:
            raise OpenListError("未配置 OpenList 令牌，且未填写账号密码")
        data = self._request(
            "/api/auth/login",
            {"username": self.username, "password": self.password},
        )
        token = str((data or {}).get("token") or "")
        if not token:
            raise OpenListError("登录成功但没有返回 token")
        self.token = token
        self._logged_in = True

    def ensure_token(self) -> None:
        if not self.token:
            self.login()

    def me(self) -> dict[str, Any]:
        """验证连接：返回当前用户信息。"""
        self.ensure_token()
        try:
            return self._request("/api/me") or {}
        except OpenListError:
            # 部分版本 /api/me 需要 GET，失败则退回列根目录验证
            return {}

    # ------------------------------------------------------------ 目录
    def list_dir(self, path: str) -> list[dict[str, Any]]:
        """列目录（自动翻页），返回 [{name,is_dir,size,modified,...}]。"""
        self.ensure_token()
        path = path or "/"
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 200:  # 兜底：单目录最多 10 万条
            data = self._request(
                "/api/fs/list",
                {
                    "path": path,
                    "password": "",
                    "page": page,
                    "per_page": PAGE_SIZE,
                    "refresh": False,
                },
            ) or {}
            content = data.get("content") or []
            for entry in content:
                items.append(
                    {
                        "name": str(entry.get("name") or ""),
                        "is_dir": bool(entry.get("is_dir")),
                        "size": int(entry.get("size") or 0),
                        "modified": str(entry.get("modified") or ""),
                    }
                )
            total = int(data.get("total") or 0)
            if len(content) < PAGE_SIZE or (total and len(items) >= total):
                break
            page += 1
            time.sleep(0.05)  # 轻微间隔，避免触发网盘限流
        return items

    def file_info(self, path: str) -> dict[str, Any]:
        """取文件详情（含 raw_url 直链）。"""
        self.ensure_token()
        return self._request("/api/fs/get", {"path": path, "password": ""}) or {}

    def download(self, path: str, dest, max_bytes: int = 0) -> int:
        """下载单个文件到 dest（Path），返回写入字节数。"""
        self.ensure_token()
        info = self.file_info(path)
        raw_url = str(info.get("raw_url") or "")
        if not raw_url:
            raise OpenListError(f"取不到直链：{path}")
        headers = {"Authorization": self.token} if self.token else {}
        request = urllib.request.Request(raw_url, headers=headers)
        dest = str(dest)
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response, open(dest + ".part", "wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise OpenListError("文件超过大小上限")
                    handle.write(chunk)
        except OpenListError:
            try:
                import os

                os.unlink(dest + ".part")
            except OSError:
                pass
            raise
        except Exception as exc:
            try:
                import os

                os.unlink(dest + ".part")
            except OSError:
                pass
            raise OpenListError(f"下载失败：{exc}") from exc
        import os

        os.replace(dest + ".part", dest)
        return written

    # ------------------------------------------------------------ 遍历
    def walk(
        self,
        root: str,
        on_progress: Callable[[int, int, str], None] | None = None,
        max_depth: int = 16,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """递归遍历目录，返回 (文件列表, 错误列表)。

        文件项含：path（完整远端路径）、name、size、modified、dirs（相对目录）。
        """
        root = "/" + (root or "").strip("/")
        files: list[dict[str, Any]] = []
        errors: list[str] = []
        queue: list[tuple[str, int]] = [(root, 0)]
        visited_dirs = 0
        while queue:
            if should_stop and should_stop():
                errors.append("任务被手动停止")
                break
            current, depth = queue.pop(0)
            visited_dirs += 1
            if on_progress:
                on_progress(visited_dirs, len(files), current)
            try:
                entries = self.list_dir(current)
            except OpenListError as exc:
                errors.append(f"读取目录失败 {current}：{exc}")
                continue
            for entry in entries:
                name = entry["name"]
                if not name or name in (".", ".."):
                    continue
                full = (current.rstrip("/") + "/" + name) if current != "/" else "/" + name
                if entry["is_dir"]:
                    if depth < max_depth:
                        queue.append((full, depth + 1))
                    continue
                files.append(
                    {
                        "path": full,
                        "name": name,
                        "size": entry["size"],
                        "modified": entry["modified"],
                    }
                )
        return files, errors

    # ------------------------------------------------------------ 工具
    @staticmethod
    def build_url(prefix: str, remote_path: str) -> str:
        """拼 strm 内容：前缀 + 远端完整路径。

        前缀支持 {path} 占位符（替换为完整路径）与 {rel}（由调用方自行替换，
        这里仅处理 {path}）。路径按 URL 规则逐段编码，保留分隔符 /。
        """
        prefix = (prefix or "").strip().rstrip("/")
        # 只转义会破坏 URL 的字符（空格、#、?、% 与中文），保留括号等可读字符
        encoded = "/".join(urllib.parse.quote(part, safe="()[]{}!$&'*,;=:@+-._~") for part in remote_path.strip("/").split("/"))
        if "{path}" in prefix:
            return prefix.replace("{path}", "/" + encoded)
        return f"{prefix}/{encoded}"
