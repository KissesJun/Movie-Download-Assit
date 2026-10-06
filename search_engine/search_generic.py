"""配置驱动的 HTML 搜索源；CSS 选择器只负责划分结果，磁力提取统一处理。"""

import logging
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import BaseEngine, TorrentResult, format_size, parse_quality
from .magnets import extract_magnets

logger = logging.getLogger(__name__)


def results_from_text(text: str, source: str, title: str = "", *, allow_hashes: bool = False):
    return [TorrentResult(
        source=source, title=link.title or title or link.info_hash,
        magnet=link.magnet, info_hash=link.info_hash,
        size_bytes=0, size=format_size(0), seeders=0,
        quality=parse_quality(link.title or title), seeders_known=False,
    ) for link in extract_magnets(text, allow_hashes=allow_hashes)]


class GenericHTMLEngine(BaseEngine):
    description = "可配置网页搜索源，使用统一磁力提取器"

    def __init__(self, config):
        super().__init__(config)
        self.name = config["id"]
        self.display_name = config.get("name", self.name)
        template = config["search_url"]
        if "{query}" not in template or urlsplit(template).scheme not in ("http", "https"):
            raise ValueError("search_url 必须是含 {query} 的 HTTP(S) URL")

    async def search(self, query, client):
        url = self.config["search_url"].replace("{query}", quote(query, safe=""))
        response = await client.get(url, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        selector = self.config.get("result_selector")
        blocks = soup.select(selector) if selector else [soup]
        results = []
        visited = set()
        detail_limit = min(max(int(self.config.get("max_detail_pages", 5)), 0), 20, self.max_results)
        allow_hashes = self.config.get("allow_hashes", False)
        for block in blocks[:100]:
            title_node = block.select_one(self.config["title_selector"]) if self.config.get("title_selector") else None
            title = title_node.get_text(" ", strip=True) if title_node else query
            block_results = results_from_text(str(block), self.display_name, title, allow_hashes=allow_hashes)
            for item in block_results:
                item.page_url, item.page_kind = str(response.url), "search"
            results.extend(block_results)
            detail_selector = self.config.get("detail_link_selector")
            if not detail_selector:
                continue
            for anchor in block.select(detail_selector):
                detail_url = urljoin(str(response.url), anchor.get("href", ""))
                # 只访问同源详情页；配置的搜索站点必须可信。
                if (urlsplit(detail_url).scheme not in ("http", "https")
                        or (urlsplit(detail_url).scheme, urlsplit(detail_url).netloc)
                        != (urlsplit(str(response.url)).scheme, urlsplit(str(response.url)).netloc)):
                    continue
                if detail_url in visited or len(visited) >= detail_limit:
                    continue
                visited.add(detail_url)
                for item in block_results:
                    item.page_url, item.page_kind = detail_url, "detail"
                try:
                    detail = await client.get(detail_url, timeout=self.timeout, follow_redirects=False)
                    detail.raise_for_status()
                    detail_results = results_from_text(detail.text, self.display_name, title, allow_hashes=allow_hashes)
                    for item in detail_results:
                        item.page_url, item.page_kind = detail_url, "detail"
                    results.extend(detail_results)
                except Exception as exc:
                    logger.warning("[%s] 详情页请求失败: %s", self.display_name, exc)
        return results
