"""
搜索引擎插件基类与数据结构规范
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import re
import math
import unicodedata
import httpx
from urllib.parse import quote, urlsplit
from .magnets import normalize_btih


def matches_query(title: str, query: str) -> bool:
    """标题匹配供聚合排序和最终推荐共用，避免截断前后标准不同。"""
    def normalize(text):
        return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text).casefold())
    words = [normalize(word) for word in query.split() if normalize(word)]
    return bool(words) and all(word in normalize(title) for word in words)

# 常用高质量公共 Tracker（用于补全磁力链接，大幅提升握手做种连接速度）
PUBLIC_TRACKERS = (
    "&tr=udp://open.demonii.com:1337/announce"
    "&tr=udp://tracker.openbittorrent.com:80"
    "&tr=udp://tracker.opentrackr.org:1337/announce"
    "&tr=udp://p4p.arenabg.com:1337"
    "&tr=udp://tracker.coppersurfer.tk:6969"
    "&tr=udp://tracker.leechers-paradise.org:6969"
)

@dataclass
class TorrentResult:
    source: str               # 来源引擎名称 (例如: TPB, BitSearch, SolidTorrents, Mikan)
    title: str                # 种子/作品标题
    magnet: str               # 完整磁力链接 (magnet:?xt=urn:btih:...)
    info_hash: str            # 标准 BTIH，纯 v2 为 btmh:1220...；由 normalize_magnet 生成
    size_bytes: int           # 文件大小 (字节)
    size: str                 # 格式化后的大小 (如 "2.4 GB")
    seeders: int              # 做种数 (可用健康度)
    leechers: int = 0         # 下载数
    quality: str = "未知"     # 分辨率标签 (4K, 1080p, 720p, 等)
    score: float = 0.0        # 综合推荐打分 (由优化引擎计算)
    is_best: bool = False     # 是否为算法推荐的最优源
    seeders_known: bool = True
    page_url: str | None = None
    page_kind: str = "detail"
    hotness: float | None = None
    updated_at: str | None = None
    published_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "magnet": self.magnet,
            "info_hash": self.info_hash,
            "size_bytes": self.size_bytes,
            "size": self.size,
            "seeds": self.seeders,
            "leechers": self.leechers,
            "quality": self.quality,
            "score": round(self.score, 2),
            "is_best": self.is_best,
            "seeders_known": self.seeders_known,
            "page_url": self.page_url,
            "page_kind": self.page_kind,
            "hotness": self.hotness,
            "updated_at": self.updated_at,
            "published_at": self.published_at,
        }


def web_url(value, base=""):
    """仅保留可供浏览器打开的 HTTP(S) 页面地址。"""
    from urllib.parse import urljoin
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        url = urljoin(base, value.strip())
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError:
        return None
    if parts.scheme in ("http", "https") and hostname and not parts.username and not parts.password:
        return url
    return None


def source_fields(item, fallback_url=None):
    """保留来源实际返回的详情地址与时间；不将发布时间冒充更新时间。"""
    page = web_url(item.get("detail_url") or item.get("url") or item.get("page_url"), fallback_url or "")
    try:
        hotness = float(item["hotness"])
        hotness = hotness if math.isfinite(hotness) and hotness >= 0 else None
    except (KeyError, TypeError, ValueError):
        hotness = None
    return {"page_url": page or web_url(fallback_url), "page_kind": "detail" if page else "search_response", "hotness": hotness,
            "updated_at": str(item.get("updated_at") or item.get("updated_time") or "") or None,
            "published_at": str(item.get("published_at") or item.get("added") or "") or None}

def parse_quality(title: str) -> str:
    """从标题中解析分辨率规格"""
    t = title.lower()
    if any(q in t for q in ("2160p", "4k", "uhd")):
        return "4K"
    if any(q in t for q in ("1080p", "fhd")):
        return "1080p"
    if any(q in t for q in ("720p", "hd")):
        return "720p"
    if any(q in t for q in ("480p", "sd")):
        return "480p"
    return "未知"

def format_size(size_bytes: int) -> str:
    """友好格式化文件字节大小"""
    if not size_bytes or size_bytes <= 0:
        return "未知大小"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

def build_magnet(info_hash: str, title: str) -> str:
    """根据 hash 与标题构建标准化磁力链接"""
    clean_hash = normalize_btih(info_hash)
    return f"magnet:?xt=urn:btih:{clean_hash}&dn={quote(title.strip())}{PUBLIC_TRACKERS}"

class BaseEngine(ABC):
    """所有搜索引擎扩展插件的抽象基类"""
    name: str = "Base"
    display_name: str = "基础引擎"
    description: str = ""
    plugin_api_version: int = 1

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.enabled = self.config.get("enabled", True)
        self.weight = float(self.config.get("weight", 1.0))
        self.timeout = float(self.config.get("timeout", 10.0))
        self.max_results = max(1, min(int(self.config.get("max_results", 10)), 100))

    @abstractmethod
    async def search(self, query: str, client: httpx.AsyncClient) -> list[TorrentResult]:
        """
        执行异步搜索
        :param query: 搜索词
        :param client: httpx.AsyncClient 实例
        :return: TorrentResult 列表
        """
        pass
