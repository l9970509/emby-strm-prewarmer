"""媒体信息备份：把 Emby 探测出的 MediaInfo 导出到 .strm 同目录。

备份文件格式与 StrmTool（jinlin-teck/StrmTool）保持一致，便于日后直接用
StrmTool 的「恢复 STRM 媒体信息」任务还原：

  [
    {
      "MediaSourceInfo": { ...Emby MediaSourceInfo 原样字段... },
      "Chapters": [ ... ],
      "ZeroFingerprintConfidence": null,
      "EmbeddedImage": null,
      "AudioMetadata": null
    }
  ]

约定：
- 文件名：{strm主文件名}-mediainfo.json，与 .strm 同目录
- MediaSourceInfo.Id / ItemId / Path 置空（脱敏，且这些值恢复时无意义）
- 外挂字幕（Protocol=File）的 Path 只保留文件名
- Chapters[].ImageTag 置空
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePath
from typing import Any

BACKUP_SUFFIX = "-mediainfo.json"

# 恢复方校验所需的关键字段
REQUIRED_HINT = ("RunTimeTicks", "Container", "Size", "MediaStreams")


def map_to_host(emby_path: str, mappings: dict[str, str]) -> Path | None:
    """把 Emby 内部路径映射成当前系统路径（不要求文件已存在）。"""
    if not emby_path:
        return None
    for emby_root in sorted(mappings, key=len, reverse=True):
        if emby_path.startswith(emby_root):
            rest = emby_path[len(emby_root):].lstrip("/\\")
            return Path(mappings[emby_root]) / rest
    return Path(emby_path)


def backup_path(strm_path: str, mappings: dict[str, str]) -> Path | None:
    host = map_to_host(strm_path, mappings)
    if host is None:
        return None
    return host.with_name(host.stem + BACKUP_SUFFIX)


def _sanitize_source(source: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(source)
    data["Id"] = None
    data["ItemId"] = None
    data["Path"] = None
    for stream in data.get("MediaStreams") or []:
        if not isinstance(stream, dict):
            continue
        if (
            stream.get("IsExternal")
            and str(stream.get("Type", "")).lower() == "subtitle"
            and str(stream.get("Protocol", "")).lower() == "file"
        ):
            raw = stream.get("Path") or ""
            if raw:
                # 只保留文件名，避免把宿主机绝对路径写进备份
                stream["Path"] = PurePath(str(raw).replace("\\", "/")).name
    return data


def _sanitize_chapters(chapters: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    cleaned = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        row = copy.deepcopy(chapter)
        row["ImageTag"] = None
        cleaned.append(row)
    return cleaned


def build_payload(item: dict[str, Any]) -> list[dict[str, Any]]:
    """由 Emby 的媒体条目构造备份内容（每个 MediaSource 一个元素）。"""
    payload = []
    chapters = _sanitize_chapters(item.get("Chapters"))
    for source in item.get("MediaSources") or []:
        if not isinstance(source, dict):
            continue
        payload.append(
            {
                "MediaSourceInfo": _sanitize_source(source),
                "Chapters": chapters,
                "ZeroFingerprintConfidence": None,
                "EmbeddedImage": None,
                "AudioMetadata": None,
            }
        )
    return payload


def collect_warnings(payload: list[dict[str, Any]]) -> list[str]:
    """检查恢复方会校验的关键字段，缺了就提示（仍会写入，方便人工排查）。"""
    warnings: list[str] = []
    if not payload:
        warnings.append("没有可用的 MediaSources")
        return warnings
    first = payload[0].get("MediaSourceInfo") or {}
    for field in REQUIRED_HINT:
        value = first.get(field)
        if field == "MediaStreams":
            if not value:
                warnings.append("缺少 MediaStreams")
        elif field == "Size":
            try:
                if float(value or 0) <= 0:
                    warnings.append("Size 为空或 0（恢复校验可能不通过）")
            except (TypeError, ValueError):
                warnings.append("Size 非法")
        elif value in (None, ""):
            warnings.append(f"缺少 {field}")
    return warnings


def export_item(
    item: dict[str, Any],
    strm_path: str,
    mappings: dict[str, str],
    overwrite: bool = False,
) -> dict[str, Any]:
    """导出一个项目的备份文件。

    返回 {status: ok|exists|skipped|failed, path, warnings, error}
    status:
      ok      已写入
      exists  已存在同名备份且未开启覆盖
      skipped 路径无法映射 / 目标目录不可写
      failed  其它异常
    """
    target = backup_path(strm_path, mappings)
    if target is None:
        return {"status": "skipped", "error": "无法解析本地路径", "path": None, "warnings": []}

    if target.exists() and not overwrite:
        return {"status": "exists", "path": str(target), "warnings": []}

    payload = build_payload(item)
    if not payload:
        return {"status": "failed", "error": "没有 MediaSources", "path": str(target), "warnings": []}

    parent = target.parent
    if not parent.is_dir():
        return {
            "status": "skipped",
            "error": f"目标目录不存在：{parent}（检查「路径映射」）",
            "path": str(target),
            "warnings": [],
        }

    warnings = collect_warnings(payload)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"status": "failed", "error": f"写入失败：{exc}", "path": str(target), "warnings": warnings}

    return {"status": "ok", "path": str(target), "warnings": warnings}
