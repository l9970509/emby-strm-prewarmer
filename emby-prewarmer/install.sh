#!/usr/bin/env bash
# Emby 媒体信息预热工具 —— 飞牛 OS 原生安装脚本
# 用法：
#   sudo bash install.sh              # 默认安装到 /opt/emby-prewarmer，端口 8787
#   sudo bash install.sh --port 9000  # 自定义端口
#   sudo bash install.sh --dir /vol1/1000/apps/emby-prewarmer

set -euo pipefail

APP_NAME="emby-prewarmer"
SERVICE_NAME="emby-prewarmer"
DEFAULT_DIR="/opt/emby-prewarmer"
PORT=8787
INSTALL_DIR="$DEFAULT_DIR"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --dir)  INSTALL_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "用法: sudo bash install.sh [--port 8787] [--dir /opt/emby-prewarmer]"
      exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "❌ 请使用 root 权限运行：sudo bash install.sh"
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------- 环境检查
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ 未找到 python3，请先安装：apt install -y python3"
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  echo "❌ Python 版本过低（当前 $PY_VER），需要 3.8 及以上"
  exit 1
fi
echo "✅ Python $PY_VER"

# ---------------------------------------------------------------- 复制文件
echo "📦 安装到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r "$SRC_DIR/app" "$INSTALL_DIR/"
cp -r "$SRC_DIR/web" "$INSTALL_DIR/"
cp "$SRC_DIR/run.py" "$INSTALL_DIR/"
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/logs"

if [[ ! -f "$INSTALL_DIR/config.json" ]]; then
  cat > "$INSTALL_DIR/config.json" <<EOF
{
  "emby": {
    "url": "http://127.0.0.1:8096",
    "token": "",
    "user_id": "",
    "timeout": 180
  },
  "scan": {
    "roots": [],
    "path_mappings": {},
    "only_strm": true,
    "include_types": "Movie,Episode,Video"
  },
  "prewarm": {
    "concurrency": 2,
    "delay_seconds": 1.0,
    "retry_delay_seconds": 10,
    "max_retries": 2,
    "max_items_per_run": 0,
    "verify": true
  },
  "schedule": {
    "enabled": false,
    "mode": "interval",
    "interval_minutes": 180,
    "daily_time": "03:30"
  },
  "failure_loop": {
    "enabled": true,
    "rounds": 2,
    "round_delay_seconds": 180,
    "auto_retry": true,
    "auto_retry_interval_seconds": 600,
    "max_attempts": 0
  },
  "export": {
    "enabled": true,
    "overwrite": false
  },
  "server": {
    "host": "0.0.0.0",
    "port": $PORT,
    "password": ""
  },
  "notify": {
    "webhook_url": ""
  }
}
EOF
  echo "✅ 已生成默认配置 config.json"
else
  echo "ℹ️  已存在 config.json，保留原有配置"
  python3 - "$INSTALL_DIR/config.json" "$PORT" <<'PY' || true
import json, sys
path, port = sys.argv[1], int(sys.argv[2])
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    sys.exit(0)
data.setdefault("server", {})["port"] = data.get("server", {}).get("port") or port
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
fi

# ---------------------------------------------------------------- systemd
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=Emby STRM MediaInfo Prewarmer (fnOS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/run.py serve --config $INSTALL_DIR/config.json
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "🔄 启动服务"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "✅ 服务已启动"
else
  echo "❌ 服务启动失败，请查看：journalctl -u $SERVICE_NAME -n 50 --no-pager"
  exit 1
fi

# ---------------------------------------------------------------- 提示
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "==============================================="
echo "  Emby 媒体信息预热工具 安装完成"
echo "==============================================="
echo "  面板地址: http://${IP:-你的NAS地址}:$PORT"
echo "  安装目录: $INSTALL_DIR"
echo "  配置文件: $INSTALL_DIR/config.json"
echo "  运行日志: $INSTALL_DIR/logs/prewarmer.log"
echo ""
echo "  下一步："
echo "   1) 浏览器打开面板，点右上角「设置」"
echo "   2) 填写 Emby 地址与 API Token（控制台 → 高级 → API 密钥）"
echo "   3) 填写媒体根目录（Emby 内部路径）与路径映射"
echo "   4) 点「测试连接」→ 保存配置 → 一键预热"
echo "==============================================="
