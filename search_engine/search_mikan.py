"""
蜜柑计划 (Mikan Project) 动漫专属 RSS 搜索插件
专攻日漫新番、剧场版、中文字幕组 (如 喵萌奶茶屋、极影字幕社、轻之国度等)
"""

import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote
from .base import BaseEngine, TorrentResult, parse_quality, format_size, web_url
from .magnets import extract_magnets

logger = logging.getLogger(__name__)

class MikanEngine(BaseEngine):
    name = "mikan"
    display_name = "蜜柑计划"
    description = "国内优质动漫番剧发布站，各大字幕组新番直发"

    async def search(self, query: str, client) -> list[TorrentResult]:
        results: list[TorrentResult] = []
        try:
            url = f"https://mikanani.me/RSS/Search?searchstr={quote(query)}"
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=self.timeout
            )
            resp.raise_for_status()

            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            for item in items:
                title_elem = item.find("title")
                title = title_elem.text.strip() if title_elem is not None and title_elem.text else ""
                if not title:
                    continue

                # 提取磁力哈希 (在 enclosure url 或 torrent/link 中)
                enclosure = item.find("enclosure")
                size_bytes = int(enclosure.attrib.get("length", 0)) if enclosure is not None and enclosure.attrib.get("length") else 0

                # 从链接提取 40 位哈希: e.g. /Download/.../4bd0f6ef8a1a55b38b7a4d4f7b10458cfa8b8d3f.torrent
                links = extract_magnets(ET.tostring(item, encoding="unicode"), allow_hashes=True)
                if not links:
                    continue
                link = links[0]
                info_hash = link.info_hash

                results.append(
                    TorrentResult(
                        source=self.display_name,
                        title=title,
                        magnet=link.magnet,
                        info_hash=info_hash,
                        size_bytes=size_bytes,
                        size=format_size(size_bytes),
                        seeders=0,
                        seeders_known=False,
                        quality=parse_quality(title),
                        page_url=web_url(item.findtext("link"), str(resp.url)) or str(resp.url),
                        page_kind="detail" if web_url(item.findtext("link"), str(resp.url)) else "search_response",
                        published_at=item.findtext("pubDate") or None,
                    )
                )
        except Exception as e:
            logger.warning(f"[{self.display_name}] 搜索异常: {e}")
            raise
        return results


ENGINE_CLASS = MikanEngine
DEFAULT_ENABLED = True
