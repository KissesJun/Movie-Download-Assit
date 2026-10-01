# 🎬 Movie Downloader Assist - 项目交接文档

## 1. 项目概述
这是一个为离线下载机量身定制的"轻量级 AI 下载控制台"。
项目避开了日本宽带运营商（ISP）对主流盗版种子站的 DNS 劫持和 SNI 阻断，通过集成抗封锁的 API，实现了**全本地化、支持中英双语检索、一键推送到 qBittorrent** 的顺畅体验。

## 2. 核心架构与技术栈
* **运行环境**：Windows / Python 3.11+
* **后端引擎**：`FastAPI` + `httpx` (异步并发网络请求)
* **前端展示**：纯 HTML + CSS + JS (零构建工具，修改即生效)
* **下载引擎**：`qBittorrent Enhanced Edition v5.x` (通过 `qbittorrent-api` 进行 RPC 调用)

## 3. 工作原理（数据流）
1. 用户在 `http://localhost:5000` 搜索关键词（如 "星际穿越"）。
2. `main.py` 触发异步并发请求，同时查询两大抗封锁种子库：
   * **The Pirate Bay (apibay.org)**：偏向欧美原盘、高分电影。
   * **BitSearch.to**：全球 DHT 节点聚合，**对中文名称极其友好**，能搜到大量国内 BT/PT 资源。
3. 后端将两边的结果按 info_hash 去重，并按 `seeders` (做种数) 降序排列。
4. 前端渲染结果卡片，用户点击"下载"后，发送磁力链接到 `/api/download`。
5. 后端通过 WebUI API，将任务直接注入到 qBittorrent。

## 4. 目录结构
```text
E:\Codes\movie-downloader-assist\
├── main.py              # 核心后端逻辑 (搜索聚合 & qBit 交互)
├── config.json          # 配置文件 (端口、qBit密码、默认下载路径)
├── requirements.txt     # Python 依赖清单
├── .gitignore           # Git 忽略规则
└── static\
    └── index.html       # 唯一的前端 UI 文件
```

## 5. 常见问题排查 (Troubleshooting)

### Q1: 搜索报错 Timeout 或 0 个结果
* **原因**：可能是网络波动。目前代码里设定了 15 秒超时 (`timeout=15.0`) 以及自动跟随 301 跳转 (`follow_redirects=True`)。
* **解决**：重试即可。如果长时间持续失败，可考虑在 `main.py` 的 `httpx.AsyncClient` 中配置本地代理（如果你未来安装了 Clash 等翻墙软件）。

### Q2: 无法添加到 qBittorrent (HTTP 401 Unauthorized)
* **原因**：qBittorrent 5.x 版本在没有密码时会彻底锁死 WebUI。
* **解决**：我们已通过修改 `qBittorrent.ini` 强行植入了 `admin/admin` 作为账号密码。如果你在客户端里修改了密码，请同步更新本项目的 `config.json`。

### Q3: 想要改前端界面？
* **方法**：直接用文本编辑器打开 `static/index.html` 修改 HTML 或 CSS 保存即可。刷新浏览器页面立刻生效，无需重启 Python 服务器。

## 6. 后续扩展建议
1. **自动识别过滤**：可以接入 LLM API，对搜索结果的 title 进行二次筛选，把枪版 (CAM) 和无中文字幕的版本过滤掉。
2. **下载进度推送**：当前依赖 qBittorrent 自己的 WebUI 看进度，未来可以接入 Telegram Bot 做"下载完成自动发送手机通知"。
