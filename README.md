# Emby 媒体信息预热工具（飞牛 OS 原生）

为 Emby 中的 **STRM 媒体**提前获取真实 MediaInfo，消除首次播放前 ffprobe 探测导致的长时间转圈。

原理与 [emby-strm-prewarmer](https://github.com/ado-uta/emby-strm-prewarmer) 一致：通过 Emby API 主动调用
`PlaybackInfo`，让 Emby 真正读一次网盘 / 302 直链媒体文件，把分辨率、编码、码率、音轨等**真实**信息
写进 Emby 自己的媒体库数据库。

- ✅ 不下载完整视频，不修改 STRM / NFO，不注入虚假媒体信息
- ✅ 自动跳过已预热影片，只做增量；同名 STRM 换源自动识别（内容指纹）
- ✅ 预热完成后导出 `{filename}-mediainfo.json` 备份（StrmTool 兼容格式，可反向恢复）
- ✅ 备份可同步到独立备份目录（对比增量、结构一致、可选图片字幕）
- ✅ PushPlus / Webhook 完成通知，只在有实际变动时推送
- ✅ 纯 Python 标准库，零第三方依赖；提供飞牛 fpk 原生安装包（统一网关，桌面内打开面板）

## 功能一览

| 功能 | 说明 |
| --- | --- |
| 一键预热 | 面板按钮 / `python3 run.py once`，全量增量预热 |
| 定时预热 | 固定间隔，或每天多个时刻（如 03:30、12:00、22:00） |
| 新片入库监听 | 按入库时间增量查询 Emby，发现新片立即预热 |
| 入库通知唤醒 | 接收 Emby Webhooks 的 `library.new`，秒级响应 |
| 失败循环预热 | 单项重试 + 运行内多轮 + 后台指数退避持续重试 + 手动预热 |
| 媒体信息备份 | 自动导出 mediainfo.json，另有全库补导出模式 |
| 备份同步 | 同步到备份目录 B，B 已存在跳过、不存在复制；手动/预热后/定时触发 |
| Web 面板 | 三页面（预热 / 备份同步 / 任务记录）、双通道日志、失败队列、在线改配置 |
| 消息通知 | PushPlus（微信）与通用 Webhook，附发送测试按钮 |

## 仓库结构

```
.
├── emby-prewarmer/          # 主程序源码（纯标准库 Python + 原生前端）
│   ├── run.py               # 启动入口（serve / once / retry / export / scan / strm / test）
│   ├── app/                 # 后端：config / emby / engine / mediainfo / syncer / strmgen / openlist / scheduler / state / web
│   ├── web/                 # 前端面板（HTML / CSS / JS，无框架，四页面）
│   └── README.md            # 完整使用文档（安装、配置、路径、常见问题）
├── fpk/embyprewarm/         # 飞牛 OS fpk 打包源目录（manifest / cmd / config / wizard / app / ui）
├── tools/
│   ├── build_fpk.py         # 纯 Python 打包脚本（无需 fnpack）
│   └── make_icons.py        # 图标生成
└── embyprewarm-1.9.1.fpk    # 当前版本安装包
```

## 快速开始

> **下载最新安装包**：[Releases · v1.9.1](https://github.com/l9970509/emby-strm-prewarmer/releases/latest)

### 飞牛 OS（推荐）

应用中心「手动安装」选择 `embyprewarm-1.9.1.fpk`，或在 SSH 中：

```bash
sudo appcenter-cli install-fpk embyprewarm-1.9.1.fpk
```

装完点桌面图标即可在飞牛 Web 桌面内打开面板（路径 `/app/embyprewarm`）。
记得在「应用中心 → 设置 → 授权目录」添加影视目录读写权限，否则备份无法写入。

### 其他 Linux（systemd）

```bash
cd emby-prewarmer
sudo bash install.sh              # 默认 /opt/emby-prewarmer，端口 8787
```

浏览器打开 `http://NAS的IP:8787`，右上角齿轮填写 Emby 地址 / API Token / 媒体根目录 / 路径映射。

详细配置说明见 [emby-prewarmer/README.md](emby-prewarmer/README.md)。

## STRM 生成与全自动闭环

面板第四个页面「STRM 生成」可以把 OpenList（AList 兼容）网盘目录同步成本地 `.strm`：

1. 填 OpenList 地址 + 长期令牌（或账号密码）、STRM 前缀（`http://IP:5244/d` 或 rclone 挂载根），
   点「测试 OpenList 连接」确认可用
2. 点「添加目录」添加生成任务：网盘目录可以直接**从 OpenList 目录树里浏览选择**；「本地根目录」
   填镜像根，strm 会落在 `根目录 + 网盘完整路径` 下（例如网盘 `/2dland/动画`、根 `/vol2/…/strm`，
   实际输出 `/vol2/…/strm/2dland/动画/…`），目录结构与网盘完全一致，多个任务可共用同一个根
3. 每个任务可单独：启用/停用、设置**每天多个定时时刻**、选择默认「增量」或「全量」模式、
   是否清理网盘已删除的 strm
4. 每个任务卡片上有「增量生成」「全量生成」按钮，点开后先看明细预览（新增/更新/跳过/删除），
   确认后才写盘；顶部「全部生成」按各任务自己的模式一起跑

两种模式的区别（都只作用于该任务自己的目录）：

| 模式 | 对比方式 | 适合场景 |
| --- | --- | --- |
| 增量 | 逐个对比网盘文件与本地镜像目录：本地没有的生成（新增）；已有的会**校验 strm 里的链接**，一致就跳过，不一致（换过前缀/换过源）才更新。BOM、CRLF、编码大小写（%e7/%E7）、%20 与 +、中文是否编码这些等价写法不算差异 | 日常定时跑，只补新片、自动纠正旧链接 |
| 全量 | 不看本地是否已存在，整目录重写一遍 | 大批量改前缀、需要彻底重建 |

> 附件（nfo/字幕/海报）在任何模式下都只补缺失文件，不重复下载。
> 从旧版本升级：本地目录含义变为「镜像根」，首次运行会把旧布局（直接落在根目录下的 strm）
> 自动搬移到 `根目录/网盘完整路径` 的新结构里，日志里会显示「迁移」数量。

生成后**不主动预热**——等 Emby 扫描到新 strm 并发出入库通知（Webhooks `library.new`），
本工具收到通知再触发增量预热，形成闭环：

```
网盘更新 → 生成 STRM → Emby 入库通知 → 自动增量预热 → 导出 mediainfo.json → 自动同步备份目录 B
```

全链路都在本工具内完成，每一步都有独立日志页面与变动通知（只推有实际变动的任务）。

## 自行打包 fpk

```bash
python3 tools/build_fpk.py        # 输出 embyprewarm-<manifest 版本号>.fpk
```

修改源码后需同步到 `fpk/embyprewarm/app/prewarmer/` 再打包（打包脚本读取该目录）。

## 说明

- 本项目与 Emby、StrmTool、飞牛 OS 官方均无关联，仅为个人自用工具的开源分享
- 媒体信息来自 Emby 自身的探测结果，工具不伪造数据、不修改影片文件
- 请勿将包含真实 API Token 的 `config.json` 提交到公开仓库（已在 `.gitignore` 中排除）
