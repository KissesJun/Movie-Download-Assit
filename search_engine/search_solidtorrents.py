"""
SolidTorrents 开源去中心化种子搜索插件
提供抗审查的 API 节点，速度快，无广告
"""

import logging
from .base import BaseEngine, TorrentResult, parse_quality, format_size, build_magnet, source_fields

logger = logging.getLogger(__name__)

class SolidTorrentsEngine(BaseEngine):
    name = "solidtorrents"
    display_name = "SolidTorrents"
    description = "去中心化种子引擎，更新迅速，多语言支持良好"

    async def search(self, query: str, client) -> list[TorrentResult]:
        results: list[TorrentResult] = []
        try:
            resp = await client.get(
                "https://solidtorrents.to/api/v1/search",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ValueError("搜索响应格式已改变")
            for t in data.get("results", []):
                seeders = int(t.get("seeders", 0))
                if seeders <= 0:
                    continue
                info_hash = str(t.get("infohash", "")).strip().lower()
                if not info_hash:
                    continue
                title = str(t.get("title", "")).strip()
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


ENGINE_CLASS = SolidTorrentsEngine
DEFAULT_ENABLED = True
