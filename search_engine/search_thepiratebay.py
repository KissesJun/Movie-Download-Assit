"""
The Pirate Bay (海盗湾官方抗封锁 API 插件)
专注于海外高清影视、蓝光原盘、经典大片
"""

import logging
from .base import BaseEngine, TorrentResult, parse_quality, format_size, build_magnet, source_fields

logger = logging.getLogger(__name__)

class ThePirateBayEngine(BaseEngine):
    name = "thepiratebay"
    display_name = "The Pirate Bay"
    description = "海盗湾官方 API (apibay.org)，欧美高清大片与原盘首选"

    async def search(self, query: str, client) -> list[TorrentResult]:
        results: list[TorrentResult] = []
        try:
            resp = await client.get(
                "https://apibay.org/q.php",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("搜索响应格式已改变")
            if isinstance(data, list):
                for t in data:
                    if t.get("id") == "0":
                        continue
                    seeders = int(t.get("seeders", 0))
                    if seeders <= 0:
                        continue
                    info_hash = str(t.get("info_hash", "")).strip().lower()
                    if not info_hash:
                        continue
                    title = str(t.get("name", "")).strip()
                    size_bytes = int(t.get("size", 0))
                    leechers = int(t.get("leechers", 0))

                    results.append(
                        TorrentResult(
                            source=self.display_name,
                            title=title,
                            magnet=build_magnet(info_hash, title),
                            info_hash=info_hash,
                            size_bytes=size_bytes,
                            size=format_size(size_bytes),
                            seeders=seeders,
                            leechers=leechers,
                            quality=parse_quality(title),
                            **source_fields(t, str(resp.url)),
                        )
                    )
        except Exception as e:
            logger.warning(f"[{self.display_name}] 搜索异常: {e}")
            raise
        return results


ENGINE_CLASS = ThePirateBayEngine
DEFAULT_ENABLED = True
