"""把飞牛安装向导 / 应用设置里收集的值同步进 config.json。

飞牛会把向导字段名原样作为环境变量传给生命周期脚本，
这里负责把 wizard_* 变量翻译成程序自己的配置结构。

用法：
    python3 app/bootstrap.py --config /path/to/config.json --port 8799
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigStore, normalize_root  # noqa: E402


def split_list(raw: str) -> list[str]:
    """逗号 / 分号 / 换行 分隔的列表。"""
    if not raw:
        return []
    text = raw.replace("\r", "\n").replace(";", ",").replace("\n", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def split_map(raw: str) -> dict[str, str]:
    """`Emby路径=本机路径` 形式的映射，逗号或换行分隔。"""
    result: dict[str, str] = {}
    for pair in split_list(raw):
        if "=" not in pair:
            continue
        emby_path, host_path = pair.split("=", 1)
        emby_path = normalize_root(emby_path)
        host_path = host_path.strip()
        if emby_path and host_path:
            result[emby_path] = host_path.rstrip("/") or "/"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="同步飞牛向导配置到 config.json")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    path = Path(args.config)
    store = ConfigStore(path)
    cfg = store.get()
    env = os.environ

    url = (env.get("wizard_emby_url") or "").strip()
    if url:
        cfg["emby"]["url"] = url.rstrip("/")

    token = (env.get("wizard_emby_token") or "").strip()
    if token:
        cfg["emby"]["token"] = token

    roots = split_list(env.get("wizard_roots") or "")
    if roots and not cfg["scan"]["roots"]:
        # 仅在当前没有媒体根目录时应用安装向导的值；
        # 升级重放向导不会覆盖用户在面板里修改过的 roots
        cfg["scan"]["roots"] = roots

    # 应用设置（wizard/config）里填写的值视为用户主动修改，直接覆盖
    force_roots = split_list(env.get("wizard_roots_force") or "")
    if force_roots:
        cfg["scan"]["roots"] = force_roots

    mappings = split_map(env.get("wizard_path_map") or "")
    if mappings:
        cfg["scan"]["path_mappings"].update(mappings)

    if args.port:
        cfg["server"]["port"] = int(args.port)

    cfg["server"]["host"] = "0.0.0.0"
    store.save(cfg)

    print(f"配置已写入：{path}")
    print(f"  Emby 地址：{cfg['emby']['url']}")
    print(f"  Token：{'已设置' if cfg['emby']['token'] else '未设置'}")
    print(f"  媒体根目录：{cfg['scan']['roots'] or '未设置'}")
    print(f"  路径映射：{cfg['scan']['path_mappings'] or '未设置'}")
    print(f"  监听端口：{cfg['server']['port']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
