"""内置 HTTP 服务：静态面板 + REST API。

零第三方依赖（http.server），单进程即可在飞牛 OS 上原生运行。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import ConfigStore
from .emby import EmbyClient, EmbyError
from .engine import MODES, Engine
from .scheduler import Scheduler
from .state import LogBus, Store

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Context:
    def __init__(
        self,
        config: ConfigStore,
        store: Store,
        bus: LogBus,
        engine: Engine,
        scheduler: Scheduler,
        web_dir: Path,
        prefix: str = "",
        syncer: Any = None,
    ):
        self.config = config
        self.store = store
        self.bus = bus
        self.engine = engine
        self.scheduler = scheduler
        self.syncer = syncer
        self.web_dir = Path(web_dir)
        # 统一网关前缀，例如 /app/embyprewarm；为空表示直接端口访问
        self.prefix = (prefix or "").rstrip("/")
        # 网关模式下的 TCP 监听仅服务 /hook/（Emby 入库通知），
        # 面板本体只走 Unix Socket，避免绕过 fnOS 登录态
        self.gateway_only = False


class Handler(BaseHTTPRequestHandler):
    server_version = "fnOS-Emby-Prewarmer/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础工具
    @property
    def ctx(self) -> Context:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音访问日志
        return

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _authed(self) -> bool:
        # 经飞牛统一网关转发时，登录态已由系统校验（只信任本机 Unix Socket 连接，
        # 避免直连端口伪造 X-Trim-* 头绕过密码）
        if self._via_gateway and self.headers.get("X-Trim-Userid"):
            return True
        password = (self.ctx.config.get()["server"].get("password") or "").strip()
        if not password:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8", "ignore")
            _, _, supplied = raw.partition(":")
        except Exception:
            return False
        return supplied == password

    @property
    def _via_gateway(self) -> bool:
        """请求是否来自统一网关（Unix Socket）。"""
        try:
            return getattr(self.connection, "family", None) == socket.AF_UNIX
        except Exception:
            return False

    def gateway_user(self) -> dict[str, str]:
        if not self._via_gateway:
            return {"username": "", "uid": "", "is_admin": ""}
        return {
            "username": (self.headers.get("X-Trim-Username") or "").strip(),
            "uid": (self.headers.get("X-Trim-Userid") or "").strip(),
            "is_admin": (self.headers.get("X-Trim-Isadmin") or "").strip(),
        }

    def _normalize_path(self) -> str | None:
        """剥掉网关前缀；返回 None 表示已经发出重定向响应。"""
        parsed = urlparse(self.path)
        path = parsed.path or "/"
        prefix = self.ctx.prefix
        if not prefix:
            return path
        if path == prefix:
            # 少一个斜杠会让相对资源解析错误，补上
            location = prefix + "/" + (("?" + parsed.query) if parsed.query else "")
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
        return path

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Emby Prewarmer"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _static(self, relative: str) -> None:
        if not relative or relative == "/":
            relative = "index.html"
        relative = relative.lstrip("/")
        base = self.ctx.web_dir.resolve()
        target = (base / relative).resolve()
        if not str(target).startswith(str(base)) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            CONTENT_TYPES.get(target.suffix.lower())
            or mimetypes.guess_type(str(target))[0]
            or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------- 路由
    def _dispatch_guard(self, path: str) -> bool:
        """公共前置检查；返回 False 表示已直接响应。"""
        if (
            self.ctx.gateway_only
            and not self._via_gateway
            and not path.startswith("/hook")
        ):
            # 网关模式：TCP 端口只开放给 Emby Webhook，面板走统一网关
            self._json({"ok": False, "error": "面板仅允许通过飞牛统一网关访问"}, HTTPStatus.FORBIDDEN)
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = self._normalize_path()
        if path is None:
            return
        if not self._dispatch_guard(path):
            return
        if not self._require_auth():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path.startswith("/api/"):
            self._handle_api_get(path, query)
        else:
            self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = self._normalize_path()
        if path is None:
            return
        if path == "/hook/emby" or path.startswith("/hook/"):
            # Emby 入库通知：无登录态，用 api_key 校验
            self._handle_webhook(path)
            return
        if not self._dispatch_guard(path):
            return
        if not self._require_auth():
            return
        body = self._read_json()

        if path == "/api/config":
            incoming = dict(body.get("config") or {})
            current = self.ctx.config.get()
            emby = incoming.setdefault("emby", {})
            # 前端拿到的是打码 Token，未修改时保持原值
            if not emby.get("token") or set(str(emby.get("token"))) == {"*"}:
                emby["token"] = current["emby"]["token"]
            server = incoming.setdefault("server", {})
            if not server.get("password"):
                server["password"] = current["server"]["password"]
            notify = incoming.setdefault("notify", {})
            pushplus = str(notify.get("pushplus_token") or "")
            # 前端拿到的是打码 Token，未修改时保持原值
            if not pushplus or set(pushplus) == {"*"}:
                notify["pushplus_token"] = current["notify"].get("pushplus_token", "")
            saved = self.ctx.config.save(incoming)
            self.ctx.bus.info("配置已更新")
            self._json({"ok": True, "config": self.ctx.config.public(), "saved": saved})
        elif path == "/api/test":
            self._json(self._test_connection(body))
        elif path == "/api/notify/test":
            self._json(self._test_notify(body))
        elif path == "/api/sync":
            if self.ctx.syncer is None:
                self._json({"ok": False, "error": "同步器未初始化"}, 500)
                return
            if self.ctx.syncer.running:
                self._json({"ok": False, "error": "同步正在进行中，请稍后再试"})
                return
            self.ctx.syncer.run_async("手动同步")
            self._json({"ok": True})
        elif path == "/api/run":
            mode = str(body.get("mode") or "full")
            if mode not in MODES:
                self._json({"ok": False, "error": f"未知模式 {mode}"}, 400)
                return
            started = self.ctx.engine.start(mode)
            self._json(
                {
                    "ok": started,
                    "error": "" if started else "已有任务在运行中",
                    "mode": mode,
                }
            )
        elif path == "/api/stop":
            self._json({"ok": self.ctx.engine.stop()})
        elif path == "/api/failures/action":
            action = str(body.get("action") or "")
            ids = [str(i) for i in (body.get("ids") or [])]
            if action == "requeue":
                count = self.ctx.store.requeue_failures(ids or None)
                self.ctx.bus.info(f"已重新排队 {count} 个失败项目")
            elif action == "clear":
                count = self.ctx.store.clear_failures()
                self.ctx.bus.info(f"已清空失败队列（{count} 条）")
            elif action == "drop":
                count = self.ctx.store.drop_failures(ids)
                self.ctx.bus.info(f"已移除 {count} 条失败记录")
            elif action == "prewarm":
                # 手动预热：立即对指定失败项目执行一次预热（忽略退避等待）
                if not ids:
                    self._json({"ok": False, "error": "未指定要预热的项目"}, 400)
                    return
                started = self.ctx.engine.start("failed", only_ids=ids)
                if started:
                    self.ctx.bus.info(f"手动预热：开始预热 {len(ids)} 个指定项目")
                self._json({
                    "ok": started,
                    "count": len(ids),
                    "error": "" if started else "已有任务在运行中，请稍后再试",
                })
                return
            else:
                self._json({"ok": False, "error": "未知操作"}, 400)
                return
            self.ctx.store.save()
            self._json({"ok": True, "count": count, "failures": self.ctx.store.failures_list()})
        else:
            self._json({"ok": False, "error": "接口不存在"}, 404)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/status":
            status = self.ctx.engine.status()
            status.update(self.ctx.scheduler.info())
            status["summary"] = self.ctx.store.summary()
            status["failures"] = self.ctx.store.failure_count()
            # 面板默认只展示最近 5 条，其余按需拉取 /api/history
            history = self.ctx.store.history()
            status["history"] = history[:12]
            status["history_count"] = len(history)
            status["sync"] = {
                "running": bool(self.ctx.syncer and self.ctx.syncer.running),
                "last": self.ctx.store.meta_get("sync_last") or {},
            }
            status["log_seq"] = self.ctx.bus.seq
            self._json(status)
        elif path == "/api/logs":
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            channel = (query.get("channel") or [""])[0].strip() or None
            if since:
                lines = self.ctx.bus.since(since, channel)
            else:
                lines = self.ctx.bus.tail(300, channel)
            self._json({"lines": lines, "seq": self.ctx.bus.seq})
        elif path == "/api/config":
            self._json({"config": self.ctx.config.public()})
        elif path == "/api/failures":
            self._json({"failures": self.ctx.store.failures_list()})
        elif path == "/api/history":
            self._json({"history": self.ctx.store.history()})
        elif path == "/api/sync/plan":
            if self.ctx.syncer is None:
                self._json({"ok": False, "error": "同步器未初始化"}, 500)
                return
            if self.ctx.syncer.running:
                self._json({"ok": False, "error": "同步正在进行中，请稍后再试"})
                return
            self._json(self.ctx.syncer.plan())
        else:
            self._json({"ok": False, "error": "接口不存在"}, 404)

    # ------------------------------------------------- 入库通知（Webhook）
    def _handle_webhook(self, path: str) -> None:
        cfg = self.ctx.config.get()
        hook = cfg.get("hook", {})
        if not hook.get("enabled"):
            self._json({"ok": False, "error": "入库通知未启用"}, HTTPStatus.FORBIDDEN)
            return
        expected = (hook.get("token") or "").strip()
        if expected:
            query = parse_qs(urlparse(self.path).query)
            supplied = (query.get("api_key") or query.get("token") or [""])[0].strip()
            if supplied != expected:
                self._json({"ok": False, "error": "api_key 校验失败"}, HTTPStatus.UNAUTHORIZED)
                return

        body = self._read_json()
        event = str(body.get("Event") or body.get("event") or "").strip().lower()
        item = body.get("Item") or body.get("item") or {}
        item_id = str(item.get("Id") or "")
        name = str(item.get("Name") or "")
        item_type = str(item.get("Type") or "")

        # 与 qmediasync 对齐：新媒体入库事件为 library.new
        if event not in ("library.new", "item.added"):
            self.ctx.bus.info(f"📩 忽略 Emby 通知事件：{event or '(空)'}")
            self._json({"ok": True, "ignored": event or "empty"})
            return
        if item_type and item_type not in ("Movie", "Episode", "Video", "MusicVideo"):
            self.ctx.bus.info(f"📩 忽略非媒体条目通知：{item_type} {name}")
            self._json({"ok": True, "ignored": item_type})
            return
        if not item_id:
            self._json({"ok": False, "error": "通知缺少 Item.Id"}, HTTPStatus.BAD_REQUEST)
            return

        self.ctx.bus.info(f"📩 收到 Emby 入库通知：{name or item_id}（{item_type or '未知类型'}）")
        self.ctx.scheduler.poke_watch()
        self._json({"ok": True, "event": event, "item": item_id})

    # ------------------------------------------------------------ 通知测试
    def _test_notify(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self.ctx.config.get()
        incoming = (body.get("config") or {}).get("notify") or {}
        channel = str(body.get("channel") or "pushplus").strip().lower()

        if channel == "webhook":
            url = str(incoming.get("webhook_url") or "").strip()
            if not url:
                url = str(cfg["notify"].get("webhook_url") or "").strip()
            ok, message = self.ctx.engine.webhook_test(url)
            return {"ok": ok, "message": message, "error": "" if ok else message}

        token = str(incoming.get("pushplus_token") or "").strip()
        # 前端拿到的是打码 Token（或留空表示不修改）
        if not token or set(token) == {"*"}:
            token = str(cfg["notify"].get("pushplus_token") or "").strip()
        if not token:
            return {"ok": False, "error": "请先填写并保存 PushPlus Token"}
        ok, message = self.ctx.engine.pushplus_test(token)
        return {"ok": ok, "message": message, "error": "" if ok else message}

    # ------------------------------------------------------------ 连接测试
    def _test_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = self.ctx.config.get()
        incoming = (body.get("config") or {}).get("emby") or {}
        url = (incoming.get("url") or cfg["emby"]["url"] or "").strip()
        token = (incoming.get("token") or "").strip()
        if not token or set(token) == {"*"}:
            token = cfg["emby"]["token"]
        if not url:
            return {"ok": False, "error": "请填写 Emby 地址"}
        client = EmbyClient(url, token, min(int(cfg["emby"]["timeout"]), 30))
        try:
            info = client.probe()
            users = client.users()
            libraries = client.libraries()
            strm_count = 0
            truncated = False
            for index, item in enumerate(client.iter_items("Movie,Episode,Video"), 1):
                if index > 20000:
                    truncated = True
                    break
                if str(item.get("Path") or "").lower().endswith(".strm"):
                    strm_count += 1
            return {
                "ok": True,
                "base_url": info["base_url"],
                "server_name": info["server_name"],
                "version": info["version"],
                "product": info["product"],
                "users": len(users),
                "libraries": [str(lib.get("Name")) for lib in libraries][:20],
                "strm_count": strm_count,
                "truncated": truncated,
            }
        except EmbyError as exc:
            return {"ok": False, "error": str(exc)}


class UnixHTTPServer(ThreadingHTTPServer):
    """监听 Unix Socket 的 HTTP 服务（飞牛统一网关用）。"""

    # 个别 Windows 版 Python 没有 AF_UNIX，这里降级占位，serve_unix 会显式报错
    address_family = getattr(socket, "AF_UNIX", socket.AF_INET)
    daemon_threads = True

    def server_bind(self) -> None:  # 跳过 getfqdn 反查，unix 地址没有主机名
        self.socket.bind(self.server_address)  # type: ignore[arg-type]
        self.server_name = "fnos-gateway"
        self.server_port = 0

    def get_request(self):  # type: ignore[no-untyped-def]
        request, _ = self.socket.accept()
        return request, ("unix", 0)


def serve(ctx: Context, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.ctx = ctx  # type: ignore[attr-defined]
    return httpd


def serve_unix(ctx: Context, socket_path: str | Path, mode: int = 0o666) -> UnixHTTPServer:
    """监听 Unix Socket；网关以独立用户转发请求，因此默认放宽到 0666。"""
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("当前 Python 不支持 Unix Socket，无法使用统一网关模式")
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
    except OSError:
        pass
    httpd = UnixHTTPServer(str(path), Handler)
    httpd.ctx = ctx  # type: ignore[attr-defined]
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return httpd


def start_in_thread(httpd: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="http")
    thread.start()
    return thread
