# Movie Downloader Assist · 影视下载助手

轻量 Python/FastAPI 应用：聚合搜索种子资源，提取与校验磁力链接，按哈希去重并评分，通过 qBittorrent WebUI API 添加下载任务。前端使用原生 HTML/CSS/JavaScript，无构建步骤。

## Windows 快速开始

1. 解压到一个固定目录；安装 Python 3.10+，安装时启用 Python Launcher 或加入 PATH。另行安装 qBittorrent，并启用其 WebUI。
2. 双击 **setup.bat**：创建 `.venv`、安装指定版本依赖、仅在配置缺失时生成 `config.json`。安装需要联网，失败会保留错误信息，修复后可重试；不会覆盖已有配置或数据库。
3. 用文本编辑器打开 `config.json`，填写 `qbittorrent` 地址、端口、用户名、密码；检查 `download.temp_root` 和 `complete_root`。这些路径属于 qBittorrent 所在机器。JSON 中 Windows 路径写成 `D:\\Downloads` 或 `D:/Downloads`。
4. 双击 **run.bat**：检查依赖、配置和端口，必要时备份旧数据库，再启动并自动打开浏览器。保持窗口打开；按 **Ctrl+C** 正常停止服务。首次缺少虚拟环境时会先执行安装，但仍须填写配置后才能启动。
5. 粘贴一行一个关键词 → **解析为新列表** → **检索当前列表** → 展开搜索引擎 → 点击资源名称添加下载。解析只保存列表；搜索不会自动下载。已有任务显示在关键词下方，备用资源可以继续添加。

默认示例地址是 http://127.0.0.1:5000；已有配置使用自己的 `server.port`，启动窗口会显示实际地址。无需手动启动前端、安装 Node.js 或执行构建。

高级参数：`run.bat --check` 仅检查本地依赖、配置、端口和数据库版本，不启动服务、不升级数据库、不连接下载器；`run.bat --no-browser` 启动但不自动打开浏览器。重新运行 `setup.bat` 可以修复依赖。下载器离线时仍可维护方案、检索资源，实际添加下载需要 WebUI 连接正常。

## 目录树

```text
movie-downloader-assist/
├── setup.bat                   # 首次安装和依赖修复；保留已有配置
├── run.bat                     # 一键启动；窗口显示日志与错误
├── launch.py                   # 配置/端口检查、旧库备份、浏览器与日志
├── main.py                     # FastAPI 启动入口和统一 HTTP 接口
├── batch.py                    # 搜索队列、批量任务、固定关键词检索
├── storage.py                  # SQLite 方案、历史、下载关联和迁移
├── downloads.py                # qBittorrent 提交、查重、状态同步和操作
├── delivery.py                 # 完成文件核实、跨批次副本、整理常驻协程
├── report.py                   # report.txt 内容与原子保存
├── config.example.json         # 发布配置样例，不含实际账号
├── config.json                 # 本机账号和路径；安装生成，勿发布
├── requirements.txt            # 本次验证使用的直接依赖版本
├── README.md                   # 使用、配置、故障排查与发布说明
├── search_engine/              # 所有搜索插件；统一接口
│   ├── AGENTS.md               # 后续 AI 新增插件必须阅读
│   ├── search_template.py      # 搜索源模板：修改 URL 和解析规则
│   ├── base.py                 # 引擎与结果模型
│   ├── registry.py             # 插件发现与契约校验
│   ├── manager.py              # 并发、隔离、去重和评分
│   ├── magnets.py              # 磁链提取、解码和校验
│   ├── search_*.py             # 各站点解析插件
│   └── __init__.py             # 统一模块入口
├── static/
│   ├── index.html              # 批次工作台页面
│   ├── workbench.css           # 紧凑 Win98 控件与手机布局
│   └── workbench.js            # 表格、折叠、方案操作和实时刷新
├── scripts/
│   └── package_release.ps1     # 白名单打包，生成 ZIP 与 SHA256
├── tests/                      # 模拟搜索、下载器、启动、历史与文件回归
├── data/                       # 运行生成，包含历史，升级时保留
│   ├── app.db                  # SQLite 数据库及运行时 WAL/SHM
│   └── backups/                # run.bat 在旧库升级前生成的 SQLite 快照
├── logs/                       # run.bat 生成；app.log 轮转，排错用
├── dist/                       # 打包生成的发布 ZIP 和校验文件
└── .venv/                      # 本机 Python 环境；不随发布包分发
```

`data/`、`logs/`、`dist/`、`.venv/` 都不是源码目录。下载文件保存在配置指定的下载器目录内，不必放在项目目录。

## 项目分析

```text
多行关键词 → /api/jobs 保存方案 → 整批或行内搜索 → 全局有限队列 → EngineManager
                        ├─ 现有 JSON API / 蜜柑 RSS 插件
                        └─ generic_sites 网页搜索/详情页 → 通用提取器
                      → 磁力校验和哈希规范化 → 去重/评分 → 每行最多三个候选

网页源码/文本/JSON/RSS → /api/extract → 通用提取器 → 持久化候选
候选下载按钮 → /api/download → 查重并提交 → qBittorrent → 下载目录
SQLite 方案/快照/下载关联 ← 定时同步 qBittorrent 进度与完成时间
已完成文件 → 核实/复制到本批次目录 → report.txt
```

| 文件 | 职责 |
| --- | --- |
| `setup.bat` / `run.bat` / `launch.py` | Windows 安装、配置/端口检查、备份与一键启动 |
| `main.py` | 配置加载、REST API、静态页面；在线程池中调用同步下载器 |
| `batch.py` | 全局搜索队列、同批重复词合并、标题相关性筛选及最多三个推荐 |
| `storage.py` | SQLite 固定方案、关键词行、查询快照、多资源关联、历史迁移和删除限制 |
| `downloads.py` | 下载查重、提交确认、WebUI 会话和周期状态同步 |
| `delivery.py` | 已完成文件核对、跨批次复制、独立副本删除及整理/报告协程 |
| `report.py` | UTF-8 批次报告与原子保存 |
| `search_engine/base.py` | 引擎基类、TorrentResult、分辨率/大小解析、磁力构建 |
| `search_engine/manager.py` | 并发调度、失败/超时隔离、哈希规范化、合并来源、评分 |
| `search_engine/registry.py` | 启动时自动发现插件，校验接口版本和唯一 ID |
| `search_engine/search_template.py` | 复制后修改 URL、正则和引擎 ID 的站点模板 |
| `search_engine/AGENTS.md` | 后续 AI 新增搜索源必须阅读的插件接口规范 |
| `search_engine/search_thepiratebay.py`、`search_bitsearch.py`、`search_solidtorrents.py` | 现有 JSON API 适配器 |
| `search_engine/search_mikan.py` | 蜜柑 RSS 适配器，复用统一提取器 |
| `search_engine/magnets.py` | 正则候选提取、解码、BTIH/BTMH 校验和去重 |
| `search_engine/search_generic.py` | 配置驱动的 HTML 搜索/详情页适配器 |
| `static/index.html` | 批量解析、方案侧栏、批次工作台及编辑对话框 |
| `static/workbench.css` | Win98 控件与紧凑表格，手机抽屉和横向滚动 |
| `static/workbench.js` | 方案切换、关键词维护、下载操作、两层折叠和局部轮询更新 |
| `config.example.json` | 部署配置和额外网站模板 |
| `tests/test_pipeline.py` | 模拟网页和下载器的离线回归测试 |
| `tests/test_jobs.py` | 方案维护、查询版本、多资源关联、目录整理与报告回归测试 |

评分综合考虑做种数、分辨率、编码/音轨、关键词和来源权重。枪版等标题会扣分，并非彻底过滤；推荐无法保证内容、字幕或实际可下载性。网页源和蜜柑 RSS 不提供实时做种统计，显示“做种未知”，不授予“最优匹配”标记。

配置在启动时读取，修改后需重启；插件不是运行时热插拔。HANDOVER.md 是历史记录，当前行为以代码和本文为准。

## 安装和启动

推荐使用顶部的 Windows 快速开始。手动安装需要 Python 3.10+ 和启用 WebUI 的 qBittorrent：

```powershell
cd E:\Codes\movie-downloader-assist
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

仅首次配置缺失时复制样例；已有配置请直接编辑，保留自己的账号和路径：

```powershell
if (-not (Test-Path -LiteralPath config.json)) { Copy-Item config.example.json config.json }
```

| 配置 | 用途 |
| --- | --- |
| server.host / port | 监听地址/端口；样例默认 127.0.0.1:5000 |
| qbittorrent.host / port | qBittorrent WebUI 地址/端口 |
| qbittorrent.username / password | 实际凭据，替换样例的 CHANGE_ME |
| download.save_path | 完成根目录未指定时的兼容默认值 |
| download.temp_root / complete_root | 新批次的暂存根目录与完成根目录 |
| download.delivery_interval_seconds | 文件核对、整理和报告周期，默认 10 秒 |
| download.path_mappings | 远程下载器目录到应用可访问共享目录的映射 |
| download.webui_url | 页面中可点击的 WebUI 地址，远程访问请填写浏览器能访问的地址 |
| download.sync_interval_seconds | 后端同步下载状态的基础间隔，默认 10 秒；失败时退避至最多 120 秒 |
| batch.max_lines / concurrency / max_pending | 单批非空行上限 1000、全局搜索并发 3、待处理查询上限 2000 |
| display.timezone | 页面时间显示时区，默认 Asia/Tokyo，可设置 Asia/Shanghai |
| engines.*.enabled / weight / timeout | 内置引擎开关、权重、单次请求超时 |
| generic_sites | 额外网页搜索源 |
| search.timeout_seconds | 每个引擎整次搜索预算，包括详情页请求 |
| search.max_results | 去重排序后返回的结果上限 |
| search.per_source_concurrency | 同一来源的全局并发上限，默认 2 |
| search.max_results_per_engine | 每个引擎保存和展示的不同资源上限，默认 10，可设 1–100；修改后重启 |

在 qBittorrent 设置中启用 WebUI，设置自己的用户名和密码，然后启动：

```powershell
.\.venv\Scripts\python.exe main.py
```

按 `server.port` 打开页面，样例配置使用 http://localhost:5000。API 文档在对应地址的 `/docs`。配置和静态目录相对 main.py 定位。直接运行 main.py 不包含 launch.py 的端口预检、升级备份和文件日志，日常使用建议运行 run.bat。

本应用没有登录鉴权；监听 0.0.0.0 时，可访问端口的设备能够查询历史并提交下载，应部署在可信网络中。WebUI 快捷链接由 download.webui_url 配置。

## 批次工作台与方案维护

界面借鉴 Windows 98 下载管理器：浅灰底色、深蓝标题栏、方形凸起按钮、相连的表格框线，以及清晰的现代中文字体。桌面资源行约 28–30px、按钮约 24–28px；手机使用约 44px 的触控按钮，保留浏览器缩放、同样的表格结构和表格内横向滚动，左侧方案列表收进抽屉。

1. 在顶部粘贴一行一个关键词的文本，勾选搜索引擎。点击“解析为新列表”创建并保存方案；名称默认为 `检索方案_YYYYMMDD_HHMMSS`。解析不搜索、不下载。空行跳过，原文和重复输入保留，每行最多 1000 字符。
2. “追加到当前列表”把文本添加到当前方案末尾；旁边显示目标名称。整个方案受 `batch.max_lines` 限制，超限直接提示，不截断。工作区也可以新增、编辑或删除关键词行。
3. 点击“检索当前列表”或行内“搜索”。每次结果单独保存成快照，并挂回固定关键词行。搜索后自动展开外层详情总览，各引擎默认折叠，点击标题展开 `num / 名字 / 热度 / 更新时间 / 详情` 表格。
4. 每行最多三个推荐；每个引擎最多保留 `search.max_results_per_engine` 个结果，默认 10。点击候选名称加入 qBittorrent，详情列的“原始页面”只打开来源地址。名字列最多占结果表的 40%，完整名称与资源信息放入悬浮提示；手机以换行与第二行信息补充。长磁链不出现在提示里，推荐旁的“磁链”按钮复制完整磁力地址。
5. 已添加的所有资源直接显示在关键词下方，包括备用、暂停、失败、完成和待确认任务。进行中的关键词和资源优先，同状态保持原始顺序；百分比和速度变化不重新排序。页面轮询只更新变化的单元格，折叠和滚动位置保留。
6. 已添加的同哈希共用真实下载任务；点击“已添加”只核实，不重复提交。提交时间、下载器加入时间、进度、速度和完成时间保存在服务端。没有实际完成时间时标注“发现”；连接离线或任务从下载器消失后，历史完成信息继续保留。
7. 左侧方案窗口支持打开、名称查找、改名和删除。曾有下载关联（含待确认）的方案不能删除，后端也验证。打开方案后自动定位工作区，顶部输入离开视野，“返回批量输入”可返回。右侧标题栏支持上一批、下一批切换；左侧分页可以访问全部历史。
8. 首页默认打开最近检索的方案；改名、打开不会改变最近检索时间。没有检索记录时打开最近创建的方案。`/?job=固定ID` 可以收藏、刷新或分享给能访问此服务的人；原来的 `?batch=...` 地址也可以打开迁移的方案。

修改关键词保留原文、旧快照与已有下载，但旧资源不自动满足新关键词；若只是调整搜索措辞，可以手动“采用”已有完整资源。正在检索时编辑关键词，迟到的旧结果只进入历史，不覆盖新词。行内“记录”可查看以前的检索结果；旧关键词候选不能直接添加至新关键词。

删除已有下载的行表示移出目标，任务和文件仍留在当前方案的“保留任务”区。删除行不会向下载器发送删除请求，也不会解除方案的删除限制。空目标显示“无目标条目”，不宣称 0/0 已完成。

### 批次目录、备用资源和报告

`download.temp_root` 和 `download.complete_root` 是新方案的默认根目录。未配置完成根目录时沿用 `download.save_path`；未配置暂存根目录时使用完成根目录下的 `_incomplete`。顶部“新方案目录设置”只修改当前页面后续新建方案使用的目录，长期默认值请修改配置；工作区“批次目录”允许在首次下载前修改当前方案目录。

每批次使用创建时间与固定短 ID；资源按原始序号、原文和哈希分开存放。改名不会移动目录。开始下载后锁定目录。暂存和最终保存路径逐任务提交到下载器，正常完成移动由 qBittorrent 管理。

```text
D:/Downloads/_incomplete/20261006_145533-a12f3456/
  0001-关键词/hash-A/                 # 未完成或备用版本
D:/Downloads/20261006_145533-a12f3456/
  report.txt                         # 当前批次报告
  0001-关键词/hash-B/资源文件或目录    # 完成并采用的版本
  0001-关键词/hash-C/资源文件或目录    # 额外完成版本，继续保留
```

每行至少有一个文件就绪的版本才计为“已满足”。先完成并就绪的版本默认采用，也可手动切换。多个未完成资源的百分比不相加。慢速时可以搜索并选择另一资源并行下载；应用不会自动换源、暂停备用或清理多余文件。

跨方案复用同哈希不会迁走旧文件。整理协程在文件完成并可访问后，把资源复制到新方案目录；复制中断保留暂存副本并可重试，错误时不计为就绪，也不覆盖不同的已有文件。独立副本可以通过“删副本”手动删除，原下载与其他方案保持；删除后不会自动复制回来。

资源行的“移除”默认从 qBittorrent 移除任务并保留文件。可另选删除原下载文件；应用保留操作记录。被多个方案共用的真实任务禁止单方案移除或删除，暂停/恢复会先显示影响范围。整批目录不提供自动递归清空。

`report.txt` 会随关键状态变化保存到完成目录，也可点击“导出报告”随时下载当前报告。报告包含名称、创建/检索时间、原文、当前关键词、来源、各版本状态、采用资源、文件位置、移出清单的任务和最后同步时间。部分完成也可导出；磁盘保存失败保留上一份报告并在页面显示错误，浏览器仍可下载数据库当前报告。

**路径属于 qBittorrent 所在机器。** 应用需要访问对应文件才能核实完成、整理副本和写报告。仅能访问远程 WebUI 时可查询下载状态和导出浏览器报告，但不能声称交付文件已就绪。可用共享目录映射，例如：

```json
"path_mappings": [
  {"remote": "/downloads", "local": "Z:\\Downloads"}
]
```

请让应用进程对共享目录有读写权限。映射按目录边界匹配，不把 `/downloads-other` 当作 `/downloads`。

### 持久化与升级

数据库为 `data/app.db`。首次启动自动升级到版本 3，把旧搜索/提取批次迁移成可维护方案，保留原始行、候选 ID、下载 ID、事件和时间。旧快照没有各引擎详情时明确提示缺失，不编造结果。升级前请停止旧服务并备份整个 `data/` 目录；run.bat 也会在升级旧库前用 SQLite backup API 保存快照到 `data/backups/`（包含 WAL 中的已提交数据），备份失败停止启动。旧程序不支持升级后的数据库。

浏览器只保留输入草稿和当前会话的分页/折叠状态。方案、查询记录、下载关联及完成状态都在服务端；关闭页面不丢数据。服务停止后下载器自身仍继续运行；服务重启核对下载，不自动重发提交，也不自动重跑中断搜索。

沿用一个 FastAPI 进程、有限搜索协程和下载同步协程，新增一个文件整理协程。报告随变化生成，不单开进程；不需要 Redis、Celery 或 WebSocket，保持 Uvicorn `workers=1`。`download.sync_interval_seconds` 控制下载器同步，`download.delivery_interval_seconds` 控制整理/报告周期，页面前台约每 4 秒轮询。

## 通用磁力匹配方式

磁力格式不取决于网站语言。中文资源站、字幕组页面、论坛和聚合站通常把同一种 URI 放在 href、data-magnet、复制按钮、脚本、JSON 或 RSS 中。通用流程是：**有界解码 → 正则候选提取 → 查询参数解析 → 哈希校验 → 去重**。

| 形式 | 支持方式 |
| --- | --- |
| 普通磁力、HTML 属性、页面文本 | 不区分大小写提取 URI |
| `&amp;` / 数字 HTML 实体 | html.unescape 后提取 |
| `magnet%3A%3Fxt%3D...` / 双重 URL 编码 | 最多四轮解码与扫描 |
| JS/JSON `\u003a`、`\x3f`、`\/` | 定向解码字符转义，保留中文 |
| 40 位十六进制 BTIH | 校验后统一小写 |
| 32 位 Base32 BTIH | 转为同一份 40 位十六进制身份 |
| BT v2 / 混合种子 | 支持 urn:btmh:1220 加 64 位十六进制 SHA-256，保留混合链接两个 xt |
| info_hash、种子哈希、磁力哈希、特征码 | 开启 allow_hashes 后构造磁力 |
| 40位哈希.torrent 地址 | 开启 allow_hashes 后构造磁力 |

核心正则（Python）：

```python
import re

MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"'`\\\[\]{}，。；、）]+", re.I)
BTIH_RE = re.compile(r"urn:btih:([0-9a-f]{40}|[a-z2-7]{32})\Z", re.I)
BTMH_RE = re.compile(r"urn:btmh:(1220[0-9a-f]{64})\Z", re.I)
```

第一条只捕获候选 URI；后两条完整校验解析出来的 xt 值，所以参数顺序变化、dn 在 xt 前、多个 tracker 均可处理。不要对整页无条件匹配任意 40 位摘要，那可能只是文件摘要或脚本标识。

建议直接复用实现：

```python
from search_engine.magnets import extract_magnets

for link in extract_magnets(html_or_text, allow_hashes=False):
    print(link.info_hash, link.title, link.magnet)
```

保留已有 dn、tr 等参数并重新编码；纯哈希构造不额外添加 tracker，可由下载器使用 DHT。普通裸哈希默认不识别。格式依据 [BitTorrent BEP 9](https://www.bittorrent.org/beps/bep_0009.html)。

### 方式一：在其他网站搜索后粘贴提取

切换“磁力提取”，粘贴网页源码、文本、JSON 或 RSS，点击“提取并保存”，然后点击“加入 qBittorrent”下载。复制可见文字可能不包含 href，此时应复制网页源码或磁力地址。

API 示例：

```powershell
$body = @{
  text = Get-Content .\search-result.html -Raw -Encoding utf8
  title = "我的搜索结果"
  allow_hashes = $false
} | ConvertTo-Json
$data = Invoke-RestMethod http://localhost:5000/api/extract -Method Post -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$data.results
```

返回去重后的 results、total 和 batch_id，结果持久化但不自动下载。text 最长 2,000,000 字符；title 补充没有 dn 的链接标题。接口只解析提交的内容，不主动访问其中的 URL。

### 方式二：接入自动搜索管线

把网站加入 config.json 的 generic_sites，重启后自动出现在来源列表并参与聚合搜索：

```json
{
  "id": "my_chinese_site",
  "name": "我的中文资源站",
  "enabled": true,
  "search_url": "https://example.com/search?q={query}",
  "result_selector": "article",
  "title_selector": "h2",
  "detail_link_selector": "h2 a",
  "max_detail_pages": 5,
  "allow_hashes": false,
  "weight": 1.0,
  "timeout": 8
}
```

- 替换示例域名、搜索路径和 CSS 选择器。id 必须唯一，不能用 all 或内置引擎标识。
- search_url 必须含 {query}，关键词自动 URL 编码，支持中文 GET 搜索。
- result_selector 划分结果，例如 `#topic_list tbody tr`、`article`、`.search-result`；省略则扫描整个响应。
- title_selector 在每条结果内部寻找标题；优先使用磁力 dn，其次选择器文本，最后搜索词。
- 搜索页已包含磁力时省略 detail_link_selector；否则抓取匹配的同源 HTTP(S) 详情页，不跟随详情重定向。
- max_detail_pages 默认 5、上限 20；一次最多处理 100 个结果块。
- allow_hashes 默认 false，确认有明确哈希标记或哈希命名的种子链接后再开启。

config.example.json 包含禁用的动漫花园 GET 搜索模板和中文论坛详情页模板。动漫花园模板尚未完成实时连通性及选择器验证，启用前请检查实际页面；论坛模板需填写自己的站点。蜜柑已有 RSS 插件。其他中文发布站也可复用提取器，但普通 .torrent 地址无法仅凭网址计算 info hash。

### 方式三：特殊站点插件

需要 POST、登录或特定 JSON/RSS 结构的站点，复制 `search_engine/search_template.py` 为 `search_engine/search_<source_id>.py`，修改 URL 和解析正则，或继承 BaseEngine，实现 async search(query, client)，复用 extract_magnets 或 results_from_text 生成 TorrentResult，并声明 `ENGINE_CLASS = MySourceEngine`。启动时自动发现，无需修改 main.py、前端或手工注册表。新插件默认关闭，在 config.json 的 engines.<name> 显式配置 enabled=true，重启后生效。

完整字段、异常、并发和测试契约见 [search_engine/AGENTS.md](search_engine/AGENTS.md)。插件只负责检索，不拥有下载状态、数据库或常驻进程。

httpx 不执行 JavaScript。动态页面可以先在浏览器取得最终源码或接口响应，再提交 /api/extract。验证码、加密脚本、简繁转换、自动分页及网页做种/尺寸统计解析尚未实现。

## API 与下载

| 接口 | 功能 |
| --- | --- |
| GET /api/sources | 可用来源列表 |
| GET /api/status | WebUI 同步健康状态、显示时区、单批上限 |
| POST /api/jobs | 解析并保存固定方案；text、sources、可选目录与 request_key |
| GET /api/jobs?q=名称 | 最近检索优先的方案列表，支持分页 |
| GET /api/jobs/{id} | 当前方案汇总与分页行，order=active/original，filter_by 可筛选 |
| PATCH /api/jobs/{id}；DELETE /api/jobs/{id} | 改名、首次下载前改目录、受限删除 |
| POST /api/jobs/{id}/items | 追加 text 中的关键词 |
| PATCH /api/job-items/{id}；DELETE /api/job-items/{id} | 修改 query 或移出清单，保留已有下载 |
| POST /api/jobs/{id}/search；POST /api/job-items/{id}/search | 整批/单行检索，保留稳定工作行 |
| GET /api/job-items/{id}/details；GET /api/job-items/{id}/history | 各引擎详情与查询历史；details 可指定 snapshot_id |
| POST /api/job-items/{id}/downloads；POST /api/job-items/{id}/adopt | 添加 candidate_id；采用已就绪的 link_id |
| POST /api/downloads/{id}/control | action 为 pause/resume/remove/sync；显式选择 delete_files/affect_shared |
| GET /api/downloads/{id}/references | 查询共享任务关联的方案范围 |
| POST /api/deliveries/{id}/remove | 删除当前批次的独立交付副本，保留原下载 |
| GET /api/jobs/{id}/report | 下载当前批次 report.txt，并尝试保存到完成目录 |
| POST /api/batches | 提交 text 和 sources（引擎 ID 列表）；兼容 source，202 返回批次 id |
| GET /api/batches/{id}?page=1&page_size=25 | 查询进度、原文、候选和下载记录 |
| GET /api/batches/{id}/rows/{row_id}/details | 读取这一行按引擎分组的结果快照，包含候选 ID 和下载状态 |
| POST /api/batches/{id}/retry?row_id=... | 重查整批或指定行，生成新批次 |
| GET /api/history?q=关键词&page=1 | 查询持久化历史 |
| GET /api/search?q=关键词&source=all | 兼容单次查询；进入同一队列、保存历史、最多三个推荐 |
| POST /api/extract | 从 text 提取，可选 title、allow_hashes |
| POST /api/download | 用 candidate_id 提交已保存的候选；兼容 magnet、title |

提取结果可直接接入下载：

```python
import httpx

data = httpx.post("http://localhost:5000/api/extract", json={"text": page_text}).json()
item = data["results"][0]
response = httpx.post("http://localhost:5000/api/download", json={
    "candidate_id": item["id"]
})
response.raise_for_status()
```

400 表示输入或来源无效，401 表示登录失败，404 表示记录不存在，429 表示队列已满，502 表示 qBittorrent 拒绝任务，503 表示连接失败或提交结果待核实。成功返回 task；请求已接受与任务已确认分别记录，不保证下载完成。提交超时时不会自动重发，后续通过 WebUI 列表核实，避免重复添加。参考 [qBittorrent WebUI API](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29)。

添加接口兼容旧版 `Ok.`/`Fails.` 文本和 Web API 2.14+ 的 JSON（`success_count`、`pending_count`、`failure_count`）。未知响应先核对任务列表，没有确认则保留待核实状态；失败记录也会定时核对，只有下载器中确实存在该任务时才恢复为已确认。新版返回结构可参考 [qBittorrent 5.2.4 源码](https://github.com/qbittorrent/qBittorrent/blob/release-5.2.4/src/webui/api/torrentscontroller.cpp#L1169)。

## 验证与限制

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试覆盖编码、哈希校验、插件发现、跨源去重、详情页边界、超时隔离、批量顺序/重复词/相关性、历史跨重启恢复、并发下载查重、不确定提交确认和完成记录保留；也覆盖固定方案与关键词维护、查询版本隔离、共享下载删除限制、跨批次文件复制、路径边界及报告原子保存。使用模拟网页和模拟 qBittorrent，不提交真实下载。

- 公开站点域名、接口和结构可能变化，内置适配器不保证在你的网络可用，也不保证免代理直连。零结果先检查后端日志、站点响应和超时。
- 页面必须包含磁力或明确哈希。任意种子文件的 info hash 需下载并解析 bencode，目前未实现。
- 十六进制与 Base32 BTIH 统一去重；混合磁力以 BTIH 为身份，纯 v2 以 BTMH 为身份，目前不跨这两类推断重复资源。
- 现有 API 插件过滤零做种条目，网页源允许做种未知的结果。详情页受整次搜索预算限制，可能只返回部分结果。
- 只配置你信任的站点。下载路径必须属于 qBittorrent 机器，远程下载器不能使用本机磁盘路径。
- 项目未附带 LICENSE 文件，历史文档中的 MIT 字样不足以认定许可。

## 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 安装提示找不到 Python | 安装 Python 3.10+，启用 Launcher 或 PATH，然后重新打开 setup.bat。环境随电脑路径变化，不能把别人的 `.venv` 复制过来。 |
| 安装下载依赖失败 | 根据窗口中的 pip 错误检查网络，再运行 setup.bat；不要删除 data/ 或覆盖配置。 |
| 已有 `.venv` 损坏 | 停止服务，把 `.venv` 改名后重新安装。账号在 config.json，历史在 data/，不在虚拟环境。 |
| 启动提示配置错误 | 按提示检查 JSON 行列、用户名/密码、端口和绝对路径。不要把配置或密码贴到公开问题区。 |
| 端口占用 / WinError 10048 | 先打开已运行实例的地址；要换端口就修改 server.port，停止旧服务后重启。启动器不会结束其他进程。 |
| 浏览器没自动打开 | 查看启动窗口的页面地址，手动打开；Ctrl+F5 可刷新旧缓存。窗口不能提前关闭。 |
| 下载器离线 / 添加失败 | 查看页面下载器状态的悬浮提示和 logs/app.log；核对 WebUI 地址、端口、账号、密码及下载器网络访问设置。下载器必须单独运行。 |
| 引擎 0 条结果 / 检索失败 | 展开搜索详情，区分无匹配与引擎错误；尝试单个引擎并检查网络，修改每引擎条数后需要重启。 |
| 下载器显示完成，页面仍整理中 | 核对应用能否读取真实文件；远程机器需配置共享路径映射。只有文件核实通过才算批次已满足，浏览器报告仍可导出。 |
| 手机无法访问 | 两台设备处于可信局域网；server.host 改为 0.0.0.0 后重启，允许该端口通过防火墙。手机用电脑局域网 IP，不能用 localhost。此服务没有登录，不适合直接暴露公网。 |
| 升级、迁移电脑或回滚 | 停止服务，备份 data/ 和 config.json，覆盖源码时保留它们，重新安装依赖。回滚到旧代码必须搭配升级前备份，不能使用已升级数据库。 |

不要直接删除 `app.db-wal` / `app.db-shm`。完整目录备份应先正常停止服务；升级器生成的 `.db` 快照使用 SQLite backup API，可独立恢复。日志可能包含搜索词、文件路径及服务器地址，提交排错材料前先检查这些信息。

## 发布整理

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\package_release.ps1
```

输出 `dist/movie-downloader-assist-时间戳-ID.zip` 和 `.zip.sha256`。ZIP 使用明确的文件白名单，只包含源码、样例配置、脚本、文档与离线测试；排除本机 `config.json`、数据库、备份、日志、虚拟环境、测试预览和 Git 历史。历史 HANDOVER.md 已过时，不纳入发布包。SHA256 用于校验收到的文件是否一致，不代表签名或来源认证。

发布前检查：

- 运行测试，确认 setup.bat 安装后不覆盖配置，run.bat 正常启动、停止且端口冲突提示明确。
- 在新解压目录及目标 Python 版本上验证安装；当前开发机验证环境为 Python 3.14，最低版本声明为 3.10，尚未逐版本验收。
- 在目标网络实际验证搜索源，并用你有权下载的资源完成一次真实 qBittorrent 添加、下载、完成目录与报告核对；自动测试使用模拟下载器。
- 确定公开发布许可并添加 LICENSE；文件还不存在时，不宣称项目使用 MIT 或其他许可。
- 检查发布 ZIP 无私密配置，写清本次变更和已知限制，再提交源码、标记版本或上传试用包。

**Git 提醒：** 本机 `config.json` 已从当前 Git 索引移除并加入忽略规则，磁盘文件保留；发布 ZIP 也排除它。历史版本曾跟踪该文件，公开源码前仍应检查历史是否含真实凭据，必要时更换已暴露的密码。当前代码尚需正式提交，忽略规则不能清除旧提交中的文件。不要用整个项目目录直接压缩发布。
