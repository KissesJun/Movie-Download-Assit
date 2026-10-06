# 搜索引擎开发规范

本文件适用于 search_engine/ 及子目录。所有站点解析文件放在此目录，统一继承 BaseEngine，统一返回 list[TorrentResult]。新增站点前阅读 base.py、search_template.py 和本规范。

## 最简目录

```text
search_engine/
├── AGENTS.md                 # 本规范；Windows 上 agents.md 指向同一文件
├── __init__.py               # 对外导出公共接口
├── base.py                   # BaseEngine、TorrentResult 和格式化辅助函数
├── magnets.py                # 共用磁力提取、解码和哈希校验
├── registry.py               # 自动发现 search_*.py 中声明的引擎
├── manager.py                # 并发调用、失败隔离、去重和排序
├── search_template.py        # 新站点模板，不参与自动加载
├── search_generic.py         # 现有配置驱动的 HTML 适配器
├── search_bitsearch.py       # BitSearch 解析
├── search_solidtorrents.py    # SolidTorrents 解析
├── search_thepiratebay.py     # The Pirate Bay 解析
└── search_mikan.py            # 蜜柑 RSS 解析
```

不要另建 plugins/ 或把站点解析放在 main.py、前端中。简单 HTML 站点复制模板，主要修改 URL 和正则；JSON/RSS 站点可自定义 search() 的解析部分。

## 新增步骤

1. 复制 search_template.py 为 search_<站点ID>.py。
2. 修改 name、display_name、description、search_url 和 result_pattern。
3. 正则用命名组 title、magnet；可选 seeders、size_bytes（字节整数）。不要把“2.4 GB”直接当字节整数。
4. 保留 ENGINE_CLASS = 对应引擎类和 DEFAULT_ENABLED = False。
5. 在 config.json 的 engines.<name> 显式启用，重启服务。
6. 添加模拟请求测试并更新 README。无需修改 main.py、前端或注册表。

模板只适配它注释中的虚构 HTML 结构，example.com 不是可用搜索接口。实际页面可能需要改正则或覆盖解析逻辑。不要用一条正则假定所有网站的 HTML 结构相同；磁力本身的处理共用 magnets.py。

```json
{
  "engines": {
    "my_site": {"enabled": true, "weight": 1.0, "timeout": 8.0}
  }
}
```

配置键仍为 engines，不随目录改名。站点配置只接收 engines.<name>；禁止自行读取 qBittorrent 配置或整份配置文件。

## 共用接口

```python
class MySiteEngine(BaseEngine):
    name = "my_site"
    display_name = "我的站点"
    description = "站点介绍"

    async def search(self, query: str, client: httpx.AsyncClient) -> list[TorrentResult]:
        ...

ENGINE_CLASS = MySiteEngine
DEFAULT_ENABLED = False
```

- name 为唯一小写 ID，匹配 [a-z][a-z0-9_]*，不能为 all，也不能与 generic_sites 重复。
- plugin_api_version 继承 BaseEngine 的 1 即可，暂不设计多版本兼容层。
- search 必须是 async；复用传入 client，不关闭它，不改全局 cookies/headers。
- 搜索词使用 params 或 quote(query, safe="") 编码，每次请求设置 timeout=self.timeout。
- 每次创建新结果对象；上层会修改评分及来源。实例可能被并发调用，不保存当前关键词或当前结果到 self。
- 插件只检索和解析；不添加下载、不写业务数据库、不创建常驻线程/进程/后台任务。
- 新插件默认关闭，四个现有插件默认开启以保留原行为。自动发现发生在启动时，不是热更新。

## 输出字段

必须返回 base.py 的 TorrentResult 对象列表，不返回原始字典、网页或普通种子 URL。

| 字段 | 输出要求 |
| --- | --- |
| source | 使用 self.display_name，来源合并由 manager 处理 |
| title | 真实完整标题，去除 HTML 标签 |
| magnet | 完整有效磁力，保留已有 dn/tr 等参数 |
| info_hash | normalize_magnet(magnet).info_hash 或 extract_magnets 的 info_hash |
| size_bytes | 非负整数，未知为 0 |
| size | format_size(size_bytes) |
| seeders | 非负整数，未知为 0，不估算或伪造 |
| seeders_known | 网站无可靠统计时必须为 False |
| leechers | 非负整数，未知为 0 |
| quality | parse_quality(title) |
| page_url / page_kind | 可选 HTTP(S) 资源页；无资源页时使用真实搜索页面（search）或 API 响应地址（search_response），未知为 None |
| hotness | 来源实际热度数字；未知为 None，不把做种数或推荐分当热度 |
| updated_at / published_at | 来源实际更新时间/发布时间字符串；未知为 None，不用当前查询时间填充 |
| score / is_best | 保持默认值，由 manager 计算 |

BTIH 十六进制/Base32、BT v2 和混合链接通过 magnets.py 统一处理，不自行截断或重写哈希。只有 BTIH 时可用 build_magnet；已有链接优先 normalize_magnet/extract_magnets。明确标记哈希或哈希命名 .torrent 时才开启 allow_hashes。普通 .torrent URL 无法仅凭网址推导 info hash。

对外 JSON 由 to_dict() 生成，seeders 映射为既有字段 seeds，不破坏前端兼容性。

## 错误与测试

- 正常无结果返回 []；个别畸形条目可跳过。
- response.raise_for_status() 检查 HTTP 错误；整次网络/登录/验证码/格式改变应抛异常，不吞掉后返回 []。
- 允许取消传播，不捕获 BaseException，不无限重试，不调用阻塞 requests/time.sleep。
- 详情页必须有抓取数量和超时限制；不无界递归爬取。
- registry 检查接口和唯一 ID。导入/初始化故障隔离，重复 ID 拒绝启动。
- 日志不含密码、cookie、Authorization 或含凭据的 URL。
- 使用 httpx.MockTransport 测试中文编码、结果字段、空结果、错误、磁力有效性和未知做种数。
- 运行 python -m unittest discover -s tests -v。模拟测试通过不等于真实网站已验证可用。

批量搜索、历史和下载同步属于应用层；插件接口保持上述统一结构，不额外实现一套队列、状态机或常驻服务。

manager.search_report() 返回 results 与 sources（来源状态、错误类型和结果数量），用于保存批次诊断；manager.search() 仍返回结果列表。新增插件继续使用原接口，不在插件中另造报告格式。

应用层 source 支持 all、单一引擎 ID 或逗号分隔的多个 ID。聚合先按 matches_query 标题相关性排序，再应用结果上限；同哈希优先保留相关标题并合并来源。插件仍接收单个关键词，不处理多选 UI。

search.max_results_per_engine（默认 10，上限 100）统一控制每引擎保留的不同哈希条数，注入实例 self.max_results。可在分页/详情请求时使用该预算，禁止无界抓取；上游接口未提供 limit 时可能返回更多原始条目，manager 负责最终排序和截断。不要改动搜索接口的函数签名。

manager.search_report().sources 中每个引擎包含独立 results 快照（聚合合并前）、count、available_count、limit 和成功/失败状态。应用层为这些条目保存候选 ID，供行内搜索详情展示和下载；旧历史不会自动补搜。网页及 RSS 应保存实际详情链接；禁止根据不明确的字段虚构资源页面。
