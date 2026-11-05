#!/usr/bin/env python3
"""fnOS Emby 媒体信息预热工具 —— 启动入口。

用法：
    python3 run.py                 # 启动 Web 面板（默认 http://0.0.0.0:8787）
    python3 run.py serve --port 8787
    python3 run.py once            # 命令行执行一次全量增量预热
    python3 run.py retry           # 只重试失败队列
    python3 run.py export          # 为已有媒体信息补导出 mediainfo.json 备份
    python3 run.py scan            # 只扫描并列出待预热项目
    python3 run.py test            # 测试 Emby 连接
    python3 run.py init            # 只生成默认配置文件

fpk 版本额外支持 --data-dir / --log-dir，把运行数据与日志放到 TRIM_PKGVAR。
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app import __version__  # noqa: E402
from app.config import ConfigStore  # noqa: E402
from app.emby import EmbyClient, EmbyError  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.scheduler import Scheduler  # noqa: E402
from app.state import LogBus, Store  # noqa: E402
from app.strmgen import StrmGenerator  # noqa: E402
from app.syncer import Syncer  # noqa: E402
from app.web import Context, serve, serve_unix, start_in_thread  # noqa: E402


def local_ips() -> list[str]:
    ips = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def build(
    config_path: Path,
    console: bool = True,
    data_dir: Path | None = None,
    log_dir: Path | None = None,
) -> tuple[ConfigStore, Store, LogBus, Engine, Scheduler, Syncer, StrmGenerator]:
    config_path = Path(config_path).resolve()
    home = config_path.parent
    # fpk 版：配置、运行数据、日志分别落在 TRIM_PKGETC / TRIM_PKGVAR / TRIM_PKGVAR/logs
    data_home = Path(data_dir).resolve() if data_dir else home / "data"
    log_home = Path(log_dir).resolve() if log_dir else home / "logs"
    config = ConfigStore(config_path)
    if not config_path.exists():
        config.save(config.get())
    store = Store(data_home / "state.json")
    bus = LogBus(log_home / "prewarmer.log", console=console)
    engine = Engine(config, store, bus)
    syncer = Syncer(config, store, bus)
    syncer.engine = engine  # 同步完成后由引擎推送通知（PushPlus / Webhook）
    engine.syncer = syncer
    strmgen = StrmGenerator(config, store, bus)
    strmgen.engine = engine  # 生成完成后可推送通知；预热由 Emby 入库通知触发
    scheduler = Scheduler(config, engine, store, bus, syncer=syncer, strmgen=strmgen)
    return config, store, bus, engine, scheduler, syncer, strmgen


def resolve_dirs(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    """数据目录/日志目录：命令行优先，其次环境变量（fpk 由 cmd/main 注入）。"""
    data_dir = args.data_dir or os.environ.get("PREWARM_DATA_DIR") or None
    log_dir = args.log_dir or os.environ.get("PREWARM_LOG_DIR") or None
    return (
        Path(data_dir).expanduser() if data_dir else None,
        Path(log_dir).expanduser() if log_dir else None,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    data_dir, log_dir = resolve_dirs(args)
    config, store, bus, engine, scheduler, syncer, strmgen = build(
        Path(args.config), data_dir=data_dir, log_dir=log_dir
    )
    cfg = config.get()
    host = args.host or cfg["server"]["host"]
    port = args.port or int(cfg["server"]["port"])
    # 统一网关：飞牛把 /app/<appname>/** 原样转发到 Unix Socket（含前缀），
    # 因此服务内部要剥掉前缀再匹配路由。
    socket_path = args.socket or os.environ.get("PREWARM_SOCKET") or ""
    prefix = (args.prefix or os.environ.get("PREWARM_PREFIX") or "").strip()
    if prefix and not prefix.startswith("/"):
        prefix = "/" + prefix

    scheduler.start()
    ctx = Context(
        config, store, bus, engine, scheduler, BASE_DIR / "web",
        prefix=prefix, syncer=syncer, strmgen=strmgen,
    )

    if socket_path:
        httpd = serve_unix(ctx, socket_path)
        # Emby 入库通知需要一个 Emby 能访问到的 TCP 地址：
        # 该端口只开放 /hook/（webhook），面板本体仍仅走统一网关
        ctx.gateway_only = True
        hookd = None
        try:
            hookd = serve(ctx, host, port)
            start_in_thread(hookd)
            bus.info(f"入库通知监听：http://<NAS_IP>:{port}/hook/emby")
        except OSError as exc:
            bus.warn(f"入库通知端口监听失败（{exc}），Webhook 暂不可用")
        bus.info(f"面板已启动：统一网关 {prefix or '/app/embyprewarm'}（socket {socket_path}）")
        print(f"\n  Emby 媒体信息预热面板 v{__version__}")
        print(f"  监听方式：Unix Socket {socket_path}")
        print(f"  网关前缀：{prefix or '/app/embyprewarm'}/")
        print(f"  入库通知：http://<NAS_IP>:{port}/hook/emby")
    else:
        httpd = serve(ctx, host, port)
        hookd = None
        bus.info(f"面板已启动：http://{host}:{port}  (版本 {__version__})")
        print(f"\n  Emby 媒体信息预热面板 v{__version__}")
        print(f"  监听地址：http://{host}:{port}")
        for ip in local_ips():
            print(f"  局域网访问：http://{ip}:{port}")
        print(f"  入库通知：http://<NAS_IP>:{port}/hook/emby")
    if cfg["schedule"]["enabled"]:
        print(f"  定时预热：{'每 %d 分钟' % int(cfg['schedule']['interval_minutes']) if cfg['schedule']['mode'] == 'interval' else '每天 ' + cfg['schedule']['daily_time']}")
    if not cfg["emby"]["token"]:
        print("  ⚠️  尚未配置 Emby API Token，请在面板右上角「设置」中填写\n")
    else:
        print()

    stopping = {"flag": False}

    def shutdown(signum, frame):  # noqa: ANN001, ARG001
        if stopping["flag"]:
            return
        stopping["flag"] = True
        bus.warn("收到退出信号，正在关闭…")
        engine.stop()
        scheduler.stop()
        for server in (httpd, hookd):
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever()
    finally:
        if hookd is not None:
            hookd.server_close()
        httpd.server_close()
        store.save()
    return 0


def cmd_batch(args: argparse.Namespace, mode: str) -> int:
    data_dir, log_dir = resolve_dirs(args)
    config, store, bus, engine, scheduler, syncer, strmgen = build(
        Path(args.config), data_dir=data_dir, log_dir=log_dir
    )
    if not config.get()["emby"]["token"]:
        print("错误：尚未配置 Emby API Token，请先运行 python3 run.py 在面板中设置，或编辑 config.json")
        return 2
    engine.start(mode)
    engine.wait()
    status = engine.status()
    print(
        f"\n结果：成功 {status['success']} / 失败 {status['failed']} / "
        f"备份导出 {status['exported']}（跳过 {status['export_skipped']}） / 用时 {status['elapsed']}s"
    )
    return 2 if status["failed"] else 0


def cmd_strm(args: argparse.Namespace) -> int:
    """命令行执行一次 STRM 生成（网盘 → 本地 .strm）。"""
    data_dir, log_dir = resolve_dirs(args)
    config, store, bus, engine, scheduler, syncer, strmgen = build(
        Path(args.config), data_dir=data_dir, log_dir=log_dir
    )
    result = strmgen.run("命令行", task_id=args.task, mode=args.mode)
    if result.get("error"):
        print(f"❌ {result['error']}")
        return 2
    print(
        f"✅ STRM 生成完成：新增 {result.get('created', 0)} / 更新 {result.get('updated', 0)} / "
        f"跳过 {result.get('skipped', 0)} / 删除 {result.get('deleted', 0)} / 失败 {result.get('failed', 0)}"
    )
    return 0 if result.get("ok") else 2


def cmd_test(args: argparse.Namespace) -> int:
    data_dir, log_dir = resolve_dirs(args)
    config, *_ = build(Path(args.config), console=True, data_dir=data_dir, log_dir=log_dir)
    cfg = config.get()
    client = EmbyClient(cfg["emby"]["url"], cfg["emby"]["token"], 20)
    try:
        info = client.probe()
        users = client.users()
        print(f"✅ 连接成功：{info['product']}「{info['server_name']}」{info['version']}")
        print(f"   API 前缀：{info['base_url']}")
        print(f"   用户数量：{len(users)}")
        print(f"   用户 ID：{client.resolve_user_id(cfg['emby']['user_id'])}")
        return 0
    except EmbyError as exc:
        print(f"❌ 连接失败：{exc}")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="fnOS Emby 媒体信息预热工具")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "once", "retry", "export", "scan", "strm", "test", "init"])
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--socket", default=None, help="监听 Unix Socket（飞牛统一网关，优先于 --host/--port）")
    parser.add_argument("--prefix", default=None, help="统一网关路径前缀，例如 /app/embyprewarm")
    parser.add_argument("--data-dir", default=None, help="运行数据目录（默认 <config 目录>/data）")
    parser.add_argument("--log-dir", default=None, help="日志目录（默认 <config 目录>/logs）")
    parser.add_argument("--task", default=None, help="strm：只生成指定任务 ID（默认全部启用的任务）")
    parser.add_argument("--mode", default=None, choices=["full", "incremental"], help="strm：全量或增量生成")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    if args.command == "init":
        path = Path(args.config)
        if not path.exists():
            ConfigStore(path).save(ConfigStore(path).get())
        print(f"配置文件已就绪：{path}")
        return 0
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "test":
        return cmd_test(args)
    if args.command == "strm":
        return cmd_strm(args)
    return cmd_batch(
        args,
        {"once": "full", "retry": "failed", "export": "export", "scan": "scan"}[args.command],
    )


if __name__ == "__main__":
    raise SystemExit(main())
