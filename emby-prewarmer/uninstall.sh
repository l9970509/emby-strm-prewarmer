#!/usr/bin/env bash
# Emby 媒体信息预热工具 —— 卸载脚本
# 默认保留 /opt/emby-prewarmer（含配置与日志），需要彻底删除请加 --purge

set -euo pipefail

SERVICE_NAME="emby-prewarmer"
INSTALL_DIR="/opt/emby-prewarmer"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    -h|--help)
      echo "用法: sudo bash uninstall.sh [--dir /opt/emby-prewarmer] [--purge]"
      exit 0 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "❌ 请使用 root 权限运行：sudo bash uninstall.sh"
  exit 1
fi

echo "🛑 停止并禁用服务"
systemctl stop "$SERVICE_NAME" 2>/dev/null || true
systemctl disable "$SERVICE_NAME" 2>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

if [[ "$PURGE" -eq 1 ]]; then
  echo "🗑  删除安装目录 $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
else
  echo "ℹ️  已保留 $INSTALL_DIR（配置与日志仍在，确认无用后手动删除）"
fi

echo "✅ 卸载完成"
