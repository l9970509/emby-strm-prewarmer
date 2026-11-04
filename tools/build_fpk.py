#!/usr/bin/env python3
"""构建飞牛 fpk 安装包（embyprewarm）。

直接从 fpk/embyprewarm/ 源目录构建，复刻 1.5.0 已验证通过的结构：
- 外层：gzip(ustar)，含 manifest / ICON / cmd/ / config/ / wizard/ / app.tgz
- 内层 app.tgz：gzip(ustar)，含 prewarmer/ ui/ config/
- 可执行位：cmd/* 与 run.py、bootstrap.py 为 0755，其余 0644

用法：python tools/build_fpk.py [输出文件路径]
"""
from __future__ import annotations

import gzip
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "fpk" / "embyprewarm"


def _version() -> str:
    for line in (SRC / "manifest").read_text(encoding="utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    return "0.0.0"


OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / f"embyprewarm-{_version()}.fpk"

DIR_MODE = 0o755
EXEC_MODE = 0o755
FILE_MODE = 0o644


def add_file(tf: tarfile.TarFile, path: Path, arcname: str, mode: int) -> None:
    info = tarfile.TarInfo(arcname)
    data = path.read_bytes()
    info.size = len(data)
    info.mode = mode
    info.mtime = 0
    info.uname = ""
    info.gname = ""
    tf.addfile(info, io.BytesIO(data))


def add_dir(tf: tarfile.TarFile, arcname: str) -> None:
    info = tarfile.TarInfo(arcname)
    info.type = tarfile.DIRTYPE
    info.mode = DIR_MODE
    info.mtime = 0
    info.uname = ""
    info.gname = ""
    tf.addfile(info)


def build_inner() -> bytes:
    """内层 app.tgz：app/ 下内容（prewarmer、ui）+ 顶层 config/。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        app_dir = SRC / "app"
        for entry in sorted(app_dir.iterdir()):
            name = entry.name
            if entry.is_dir():
                for p in sorted(entry.rglob("*")):
                    arc = f"{name}/{p.relative_to(entry).as_posix()}"
                    if p.is_dir():
                        add_dir(tf, arc)
                    else:
                        mode = EXEC_MODE if p.name in ("run.py", "bootstrap.py") else FILE_MODE
                        add_file(tf, p, arc, mode)
                add_dir(tf, name)
            else:
                add_file(tf, entry, name, FILE_MODE)
        # 内层同样带一份 config（与 1.5.0 已验证包一致）
        for p in sorted((SRC / "config").rglob("*")):
            arc = f"config/{p.relative_to(SRC / 'config').as_posix()}"
            add_file(tf, p, arc, FILE_MODE)
    return gzip.compress(buf.getvalue(), mtime=0)


def build_outer(inner: bytes, out: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        # app.tgz
        info = tarfile.TarInfo("app.tgz")
        info.size = len(inner)
        info.mode = FILE_MODE
        info.mtime = 0
        info.uname = ""
        info.gname = ""
        tf.addfile(info, io.BytesIO(inner))
        # cmd/（全部可执行）
        add_dir(tf, "cmd")
        for p in sorted((SRC / "cmd").iterdir()):
            add_file(tf, p, f"cmd/{p.name}", EXEC_MODE)
        # config/
        add_dir(tf, "config")
        for p in sorted((SRC / "config").iterdir()):
            add_file(tf, p, f"config/{p.name}", FILE_MODE)
        # wizard/
        add_dir(tf, "wizard")
        for p in sorted((SRC / "wizard").iterdir()):
            add_file(tf, p, f"wizard/{p.name}", FILE_MODE)
        # 根文件
        for name in ("manifest", "ICON.PNG", "ICON_256.PNG"):
            add_file(tf, SRC / name, name, FILE_MODE)
    out.write_bytes(gzip.compress(buf.getvalue(), mtime=0))


def main() -> None:
    for name in ("manifest", "ICON.PNG", "ICON_256.PNG"):
        assert (SRC / name).is_file(), f"缺少 {name}"
    assert (SRC / "config" / "privilege").is_file()
    assert (SRC / "config" / "resource").is_file()
    inner = build_inner()
    build_outer(inner, OUT)
    print(f"OK: {OUT}  ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
