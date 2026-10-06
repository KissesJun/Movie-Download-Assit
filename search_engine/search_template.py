"""复制本文件为 search_<站点ID>.py，修改 name、search_url 和 result_pattern。

这是虚构 HTML 示例，不对应真实站点；发现器跳过 search_template 本身。
复杂 API/RSS 可覆盖 search()，但必须返回相同的 list[TorrentResult]。
"""

import html
import re
from urllib.parse import quote

import httpx

from .base import BaseEngine, TorrentResult, format_size, parse_quality, web_url, source_fields
from .magnets import extract_magnets


class TemplateEngine(BaseEngine):
    name = "template"  # 改为站点唯一 ID，例如 my_site
    display_name = "搜索模板"  # 改为显示名称
    description = "通过 URL 和正则解析 HTML 搜索结果"

    # {query} 会被替换为经过 URL 编码的关键词；也可在配置中覆盖 search_url。
    search_url = "https://example.com/search?q={query}"

    # 对应虚构结构：<article><h2>标题</h2><a href="磁力">下载</a></article>
    # 必需命名组：title、magnet。可选：seeders、size_bytes（字节）、
    # detail_url（可为相对路径）、updated_at、published_at、hotness。
    # 正则只负责切分条目，磁力解码、校验和哈希规范化共用 extract_magnets。
    result_pattern = re.compile(
        r'<article\b[^>]*>\s*<h2\b[^>]*>(?P<title>.*?)</h2>.*?'
        r'<a\b[^>]*href=[\"\'](?P<magnet>[^\"\']+)[\"\'][^>]*>.*?</article>',
        re.I | re.S,
    )

    async def search(self, query: str, client: httpx.AsyncClient) -> list[TorrentResult]:
        template = self.config.get("search_url", self.search_url)
        if "{query}" not in template:
            raise ValueError("search_url 必须包含 {query}")
        response = await client.get(
            template.replace("{query}", quote(query, safe="")), timeout=self.timeout
        )
        response.raise_for_status()
        results = []
        for match in self.result_pattern.finditer(response.text):
            values = match.groupdict()
            title = html.unescape(re.sub(r"<[^>]+>", "", values["title"])).strip()
            if not title:
                continue
            try:
                raw_seeds = values.get("seeders")
                seeds_known = raw_seeds is not None and bool(raw_seeds.strip())
                seeders = max(int(raw_seeds), 0) if seeds_known else 0
                size_bytes = max(int(values.get("size_bytes") or 0), 0)
            except (TypeError, ValueError):
                continue  # 仅跳过畸形条目，不吞掉整次网络错误。
            for link in extract_magnets(values["magnet"]):
                result_title = link.title or title
                results.append(TorrentResult(
                    source=self.display_name, title=result_title,
                    magnet=link.magnet, info_hash=link.info_hash,
                    size_bytes=size_bytes, size=format_size(size_bytes),
                    seeders=seeders, seeders_known=seeds_known,
                    quality=parse_quality(result_title),
                    page_url=web_url(values.get("detail_url"), str(response.url)) or str(response.url),
                    page_kind="detail" if web_url(values.get("detail_url"), str(response.url)) else "search",
                    updated_at=values.get("updated_at") or None,
                    published_at=values.get("published_at") or None,
                    hotness=source_fields(values).get("hotness"),
                ))
        return results


ENGINE_CLASS = TemplateEngine
DEFAULT_ENABLED = False  # 新插件在 config.json 的 engines.<name> 中显式启用。
