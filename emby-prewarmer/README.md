# Emby 媒体信息预热工具

为 Emby 中的 **STRM 媒体**提前获取真实 MediaInfo，消除首次播放前 ffprobe 探测的长时间等待。

原理与 [emby-strm-prewarmer](https://github.com/ado-uta/emby-strm-prewarmer) 一致：  
通过 Emby API 主动调用 `PlaybackInfo`，让 Emby 真正去读一次网盘/302 直链媒体文件，  
把分辨率、编码、码率、音轨等**真实**信息写入 Emby 自己的媒体库数据库。

- ✅ 不下载完整视频
- ✅ 不修改 STRM / NFO，不注入虚假媒体信息
- ✅ 自动跳过已预热影片，只做增量
- ✅ 同名 STRM 换源自动识别（内容指纹）
- ✅ 预热完成后自动导出 `{filename}-mediainfo.json` 备份（兼容 [StrmTool](https://github.com/jinlin-teck/StrmTool) 格式，可反向恢复）
- ✅ 备份可自动同步到独立备份目录（对比增量、结构一致、可选图片字幕）
- ✅ PushPlus / Webhook 完成通知（只在有实际变动时推送，不打扰）
- ✅ 纯 Python 标准库，**零第三方依赖**，飞牛 OS 开箱即跑

## 功能

| 功能       | 说明                                                              |
| -------- | --------------------------------------------------------------- |
| 一键预热     | 面板大按钮 / 命令行 `python3 run.py once`，全量增量预热                        |
| 定时预热     | 固定间隔（如每 3 小时）或每天固定时刻，**支持一天多个执行时刻**（如 03:30、12:00、22:00）        |
| 新片入库监听   | 按入库时间增量查询 Emby，检测到新影片自动立即预热（可配检测间隔）                             |
| 入库通知唤醒   | 接收 Emby「Webhooks」插件的 library.new 通知（参考 qmediasync），收到即触发检测，秒级响应 |
| 失败循环预热   | 运行内多轮重试 + 失败队列持久化 + 后台按指数退避持续重试 + 单项手动预热                        |
| 媒体信息备份   | 预热成功后自动在 .strm 同目录导出 `{filename}-mediainfo.json`；另有全库补导出模式      |
| 备份同步     | 把备份的 mediainfo.json 同步到独立备份目录，先对比后复制、支持图片字幕、手动/预热后/定时三种触发        |
| Web 管理面板 | 三页面布局（预热 / 备份同步 / 任务记录），状态统计、双通道实时日志、进度条、失败队列管理、在线改配置           |
| 扫描预览     | 只列出待预热项目，不实际执行（dry-run）                                         |
| 完成通知     | PushPlus（微信）与通用 Webhook，推送开始时间/时长/条目明细/成功失败数；均提供发送测试按钮          |

## Web 管理面板

面板按功能分为三个页面，通过顶部导航切换：

- **预热**：统计卡片、任务控制、预热日志、失败队列、定时与循环状态；「预热设置」折叠面板收纳全部预热相关配置
- **备份同步**：同步状态与上次结果、立即同步（先展示同步明细确认后才执行）、同步日志；「同步设置」折叠面板
- **任务记录**：全部任务历史（开始时间/类型/条目/成功失败/备份/时长）

右上角抽屉只保留**公共设置**：Emby 连接、媒体库范围（roots / 路径映射）、消息通知（PushPlus / Webhook，含测试按钮）、面板端口与访问密码。

## 媒体信息备份（mediainfo.json）

参考 [StrmTool](https://github.com/jinlin-teck/StrmTool) 的备份方案实现：

- **自动导出**：预热成功后，自动在 `.strm` 同目录写出同名 `{filename}-mediainfo.json`  
  （如 `Movie.strm` → `Movie-mediainfo.json`）。默认已有备份时跳过；**STRM 换源后  
  信息已变化，会自动覆盖旧备份**。
- **兼容格式**：JSON 结构与 StrmTool 完全一致（`MediaSourceInfo / Chapters /
  ZeroFingerprintConfidence / EmbeddedImage / AudioMetadata`，PascalCase），  
  以后可以直接用 Emby 插件 StrmTool 的「恢复 STRM 媒体信息」任务读取还原，  
  也能迁移到装了 StrmTool 的服务器上使用。
- **全库补备份**：面板「导出备份」按钮 / 命令行 `python3 run.py export`，  
  为媒体库里已有 MediaInfo 但还没生成 json 的旧影片批量补备份（已有备份自动跳过）。
- **前置条件**：需要在「公共设置 → 媒体库范围 → 路径映射」里配置  
  `Emby路径 = 本机路径`，否则工具不知道备份该写到哪个目录（会提示 skipped）。
- **安全写法**：先写 `.tmp` 再原子改名，不会出现半截文件；`Id/ItemId/Path` 脱敏为  
  null，外挂字幕只保留文件名；如果备份缺 `RunTimeTicks/Container/Size`（网盘没返回  
  文件大小时可能发生），日志会给出字段提示。

## 备份同步（同步到独立备份目录）

把预热导出的 `*-mediainfo.json` 按相同目录结构同步到指定备份目录 B（如网盘挂载目录、
其他机器可访问的目录），方便日后恢复或迁移：

- **对比增量**：B 中已存在的文件跳过（不覆盖），不存在的才复制；重复执行无副作用
- **同步内容**：默认只同步 `*-mediainfo.json`，可勾选一并同步**图片**（poster/fanart 等）与**字幕**
- **来源 A**：默认自动使用「路径映射」后的媒体库本机路径；也可在同步设置里手动指定一个或多个来源目录（每行一个）
- **三种触发方式**：
  - 手动：「立即同步」按钮 —— 先展示**同步明细预览**（复制/跳过列表 + 文件大小），
    确认后才执行；没有可复制的文件时确认按钮禁用
  - 预热后：每次预热任务结束后自动同步（可开关）
  - 定时：每天固定时刻同步一次（可开关）
- **通知一致**：同步完成同样推送 PushPlus / Webhook，且**只在实际有复制（变动）时推送**，
  两边完全一致时不打扰
- **日志独立**：同步日志与预热日志分开展示在各自页面

## 消息通知（PushPlus / Webhook）

预热与备份同步任务结束时推送（开始时间、持续时长、条目明细、成功/失败数量）。
**空扫描、纯导出备份、没有复制的同步不会推送**——只在有实际变动时通知。

- **PushPlus**：在 [pushplus.plus](https://www.pushplus.plus/) 微信扫码获取 Token 填入
  （公共设置 → 消息通知），支持「PushPlus 测试」按钮直接发一条验证
- **Webhook**：填一个接收 URL（POST JSON，payload 含 summary + details 明细），
  支持「Webhook 测试」按钮
- Token / 地址支持修改：留空表示保持原值不变

## 在飞牛 OS 上安装

### 方式一：fpk 应用包（推荐，飞牛原生）

使用应用中心「手动安装」选择 `embyprewarm-1.6.3.fpk`，或在 SSH 里执行：

```bash
sudo appcenter-cli install-fpk embyprewarm-1.6.3.fpk
```

fpk 版本的特点：

- **接入飞牛统一网关**：点桌面图标直接在飞牛 Web 桌面内打开面板（iframe），  
  复用 fnOS 域名与登录态，路径为 `/app/embyprewarm`，不再暴露 `IP:端口` 新标签页  
  （应用监听 Unix Socket `target/app.sock`；若 fnOS 版本过旧不支持网关，会自动回退  
  `http://NAS的IP:8799` 端口模式并在日志中说明）
- 经网关访问时登录态由 fnOS 校验并转发 `X-Trim-Userid` 等身份头，应用只信任  
  来自本机 Unix Socket 的请求；直连端口仍走面板自身的访问密码
- 安装向导里可直接填写 **Emby 地址 / API 密钥 / 媒体根目录 / 路径映射**，装完即用  
  （之后也可以在「应用设置」里修改这些值，留空表示不修改；保存后自动重启生效）
- 配置、状态、日志分别存放在应用的 `etc / var / var/logs` 目录，升级与卸载都会保留数据
- 以 **root 权限运行**（install_type=root），可读写已授权目录，mediainfo.json 备份不会再  
  出现 Permission denied
- 安装前会自动检查 python3（缺失时尝试用 apt 安装，失败会给出明确提示）
- 需要在「应用中心 → Emby 媒体信息预热 → 设置 → 授权目录」中添加影视目录（可读写），  
  否则预热正常但 mediainfo.json 备份与备份同步无法写入影片目录

命令行方式（SSH）：

```bash
# 一次性预热 / 只重试失败 / 全库补备份
python3 /var/apps/embyprewarm/target/prewarmer/run.py once \
  --config /var/apps/embyprewarm/etc/config.json \
  --data-dir /var/apps/embyprewarm/var \
  --log-dir /var/apps/embyprewarm/var/logs
```

#### 入库通知（Webhook）配置

在 Emby「Webhooks」插件（控制台 → 高级 → Webhooks）里添加一个**通用 Webhook**：

- URL：`http://NAS的IP:8799/hook/emby?api_key=你的令牌`（令牌在预热页「预热设置 → 入库通知」里设置，可留空）
- 事件：勾选 **新媒体已添加（library.new）**
- 内容类型：application/json

收到通知后立即触发一次新片检测（连续多条通知会自动合并），比轮询间隔响应更快。  
端口 8799 只对 Webhook 开放，面板本体在 fpk 模式下仍只通过飞牛统一网关访问。

### 方式二：脚本安装（systemd）

**1. 上传程序**

把 `emby-prewarmer` 整个文件夹上传到飞牛 OS（例如放到 `/vol1/1000/apps/`），  
可以用飞牛文件管理器上传压缩包后解压，或通过 SSH 的 scp 传输。

**2. SSH 进入系统执行安装**

在飞牛 OS 打开 SSH（设置 → 远程/终端），然后：

```bash
cd /vol1/1000/apps/emby-prewarmer     # 换成你的实际目录
sudo bash install.sh                  # 默认装到 /opt/emby-prewarmer，端口 8787
```

安装脚本会：检查 Python3 → 复制程序 → 生成默认配置 → 注册 systemd 服务并开机自启。

自定义参数：

```bash
sudo bash install.sh --port 9000                    # 指定面板端口
sudo bash install.sh --dir /vol1/1000/apps/prewarm  # 指定安装目录
```

**3. 打开面板配置**

浏览器访问 `http://NAS的IP:8787`，点右上角齿轮「设置」图标打开公共设置抽屉：

1. **服务器地址**：`http://127.0.0.1:8096`（Emby 与本工具同机）；带不带 `/emby` 前缀都可以
2. **API Token**：Emby 控制台 → 高级 → API 密钥，新建一个
3. **媒体根目录**：填 **Emby 内部看到的路径**（见下方说明）
4. **路径映射**：`Emby路径 = fnOS宿主机路径`（用于换源检测，建议填写）
5. 点「测试连接」确认 → 「保存配置」→ 回主页点「一键预热」

> 建议给面板设置一个访问密码（设置 → 面板 → 访问密码）。

## 关于路径（重要）

工具需要填两层路径：

- **roots（媒体根目录）**：必须是 **Emby 容器里**看到的路径。  
  例如 Docker 映射 `/vol1/1000/media:/data/media`，就填 `/data/media/`。  
  留空表示处理 Emby 里全部媒体库。
- **path_mappings（路径映射）**：`Emby路径 = 本机路径`，让工具能读到 STRM 文件内容。  
  上例填 `/data/media/ = /vol1/1000/media/`。  
  不填也能正常预热，只是无法识别"同名换源"。

## 定时预热

在预热页「预热设置 → 定时预热」中开启：

- **固定间隔**：如 180 = 每 3 小时检查一次（最小 5 分钟）
- **每天定时**：可添加**多个执行时刻**（如 03:30、12:00、22:00），到点各做一次增量预热

定时任务只做增量：已有完整 MediaInfo 的项目自动跳过，不会重复探测。  
修改间隔建议：STRM 更新频繁 → 60；每天少量新增 → 180~360；低配机器 → 360 以上。

## 失败循环预热

三层保障，全部可在预热页「预热设置 → 失败循环预热」调整：

1. **单项重试**：每个项目在预热时失败后，间隔数秒再试 `max_retries` 次
2. **运行内循环**：一轮全部跑完后，失败项目等待 `round_delay_seconds` 秒再整体重试 `rounds` 轮
3. **后台持续循环**：失败进入持久化队列，之后每隔 `auto_retry_interval_seconds`  
   自动重试一遍，重试间隔随失败次数指数放大（上限 6 小时）；  
   链接彻底失效的项目可在失败队列里「重新排队」或「移除」

`max_attempts = 0` 表示不限次数，一直循环直到成功。

## 命令行用法（可选）

```bash
cd /opt/emby-prewarmer
python3 run.py serve    # 启动面板（systemd 服务就是跑的这个）
python3 run.py once     # 命令行执行一次全量增量预热
python3 run.py retry    # 只重试失败队列
python3 run.py export   # 为已有媒体信息的旧影片批量补导出备份
python3 run.py scan     # 只扫描统计，列出待预热项目
python3 run.py test     # 测试 Emby 连接
```

## 常用运维命令

```bash
systemctl status  emby-prewarmer     # 服务状态
systemctl restart emby-prewarmer     # 重启（改完 config.json 后执行）
journalctl -u emby-prewarmer -f      # 实时日志
tail -f /opt/emby-prewarmer/logs/prewarmer.log
```

## 目录结构

```
emby-prewarmer/
├── run.py                  # 启动入口（面板 / CLI）
├── app/
│   ├── config.py           # 配置读写与校验
│   ├── emby.py             # Emby API 客户端（纯标准库）
│   ├── engine.py           # 预热引擎：扫描/分类/并发预热/失败循环/通知
│   ├── mediainfo.py        # mediainfo.json 备份导出（StrmTool 兼容格式）
│   ├── scheduler.py        # 定时调度器（预热/重试/入库监听/定时同步）
│   ├── syncer.py           # 备份同步：plan 预览/对比复制/明细收集
│   ├── state.py            # 失败队列/指纹/历史/日志总线（预热与同步双通道）
│   ├── bootstrap.py        # 安装向导配置接入（roots 保护）
│   └── web.py              # HTTP 服务与 REST API（含同步预览/测试通知接口）
├── web/                    # 前端面板（原生 HTML/JS，三页面 + 统一网关适配）
├── install.sh / uninstall.sh
├── emby-prewarmer.service  # systemd 单元模板
├── config.example.json
└── data/ logs/             # 运行后生成：状态与日志
```

## 常见问题 

**运行后显示 0 个待处理？** 说明媒体库都已完整。用「扫描预览」确认 roots 是否写对。

**401 / 403？** API Token 错误或失效；检查是否多了空格。

**PlaybackInfo 成功但 MediaInfo 仍不完整？** 通常是 STRM 里的直链失效、302 服务  
没返回真实链接、网盘文件被删、或 URL 中的 `#` 未编码成 `%23`。这类项目会进入  
失败队列反复重试，修复链接源后自动恢复。

**会占用很多资源吗？** 平时只有一个几十 MB 的 Python 进程 + 每 5 秒一次的调度  
心跳；预热时主要是等 Emby/网盘响应，`concurrency` 保持 1~2 即可。

**如何卸载？**

```bash
sudo bash uninstall.sh           # 保留配置与日志
sudo bash uninstall.sh --purge   # 连安装目录一起删除
```

## 安全建议

- 不要把真实 Token 提交到公开仓库或分享给他人
- 面板暴露到公网时务必设置访问密码，或只用内网访问
- 首次使用先「扫描预览」，再用「单次上限」小批量试跑
