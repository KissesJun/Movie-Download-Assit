"""
搜索引擎管理器与最优资源智能匹配算法
支持启动时发现插件、权重调整、去重及多维度评分推荐
"""

import asyncio
import logging
import math
import re
from typing import Dict, List
import httpx

from .base import BaseEngine, TorrentResult, matches_query, web_url
from .search_generic import GenericHTMLEngine
from .magnets import normalize_magnet
from .registry import AVAILABLE_ENGINES, ENGINE_PLUGINS

logger = logging.getLogger(__name__)

class EngineManager:
    """管理所有引擎插件生命周期与聚合搜索调度"""

    def __init__(self, config: dict):
        self.config = config
        self.engines_config = config.get("engines", {})
        self.search_config = config.get("search", {})
        self.timeout = float(self.search_config.get("timeout_seconds", 12.0))
        self.max_results = int(self.search_config.get("max_results", 40))
        self.max_results_per_engine = max(1, min(int(self.search_config.get("max_results_per_engine", 10)), 100))
        self.per_source_concurrency = max(1, int(self.search_config.get("per_source_concurrency", 2)))
        self._source_limits = {}

        # 实例化引擎
        self.engines: Dict[str, BaseEngine] = {}
        self._init_engines()

    def _init_engines(self):
        """根据配置加载启用的引擎"""
        for key, plugin in ENGINE_PLUGINS.items():
            engine_cls = plugin.engine_class
            cfg = self.engines_config.get(key, {})
            if cfg.get("enabled", plugin.default_enabled):
                try:
                    self.engines[key] = engine_cls({**cfg, "max_results": self.max_results_per_engine})
                except Exception:
                    logger.exception("搜索插件初始化失败: %s", key)
                    continue
                logger.info(f"加载搜索引擎插件: [{engine_cls.display_name}] (权重: {cfg.get('weight', 1.0)})")
        for cfg in self.config.get("generic_sites", []):
            if not cfg.get("enabled", True):
                continue
            key = cfg.get("id", "")
            if not key or key == "all" or key in self.engines or key in AVAILABLE_ENGINES:
                raise ValueError(f"通用搜索源 id 缺失或重复: {key}")
            self.engines[key] = GenericHTMLEngine({**cfg, "max_results": self.max_results_per_engine})

    def get_available_sources(self) -> list[dict]:
        """获取当前启用的所有引擎列表 (供前端筛选使用)"""
        sources = [{"id": "all", "name": "全部聚合引擎"}]
        for key, engine in self.engines.items():
            sources.append({
                "id": key,
                "name": engine.display_name,
                "desc": engine.description
            })
        return sources

    def calculate_score(self, item: TorrentResult, query: str, engine_weight: float) -> float:
        """
        资源质量多维度智能打分算法
        自动寻找画质最好、做种最健康、且最匹配搜索词的资源
        """
        score = 0.0

        # 1. 种子做种健康度评分 (采用对数函数，避免数千做种产生过大偏差，但确保健康节点有足够权重)
        # seeders: 1 -> 0, 10 -> 20, 100 -> 40, 1000 -> 60
        seeds = max(item.seeders, 0)
        score += math.log10(max(seeds, 1) + 1) * 25.0

        # 2. 画质与分辨率偏好打分
        quality_scores = {
            "4K": 40.0,
            "1080p": 25.0,
            "720p": 10.0,
            "480p": 0.0,
            "未知": 5.0
        }
        score += quality_scores.get(item.quality, 5.0)

        # 3. 压制质量与无损音频加分
        title_lower = item.title.lower()
        if any(codec in title_lower for codec in ("x265", "hevc", "265")):
            score += 8.0   # 高压缩比高质量现代编码
        if any(audio in title_lower for audio in ("atmos", "truehd", "dts-hd", "flac")):
            score += 5.0   # 顶级音轨

        # 4. 严厉惩罚低画质/抢版/枪版垃圾资源
        if re.search(r"(?<![a-z0-9])(?:camrip|hdcam|hd[ ._-]?ts|telesync|tc)(?![a-z0-9])|枪版", title_lower):
            score -= 100.0

        # 5. 关键词强匹配加分
        query_words = [w for w in re.split(r"\s+", query.strip().lower()) if w]
        if query_words and all(w in title_lower for w in query_words):
            score += 20.0  # 标题完全命中所有搜索词

        # 6. 引擎本身设定的权威权重
        score *= max(engine_weight, 0.1)

        return max(score, 0.0)

    async def search(self, query: str, source_filter: str = "all") -> list[TorrentResult]:
        return (await self.search_report(query, source_filter))["results"]

    def validate_source(self, source_filter):
        if source_filter == "all":
            return
        selected = source_filter.split(",")
        if not selected or any(key not in self.engines for key in selected):
            raise ValueError("搜索来源不存在或未启用，请至少选择一个来源")

    async def _search_engine(self, engine, query, client):
        limit = self._source_limits.setdefault(engine.name, asyncio.Semaphore(self.per_source_concurrency))
        async with limit:
            return await asyncio.wait_for(engine.search(query, client), timeout=self.timeout)

    async def search_report(self, query: str, source_filter: str = "all") -> dict:
        """
        执行多引擎并发查询、去重合并并选出最优结果
        """
        self.validate_source(source_filter)
        if not query.strip():
            return {"results": [], "sources": []}

        # 确定需要运行的目标引擎
        target_engines: List[BaseEngine] = []
        if source_filter != "all":
            target_engines = [self.engines[key] for key in dict.fromkeys(source_filter.split(","))]
        else:
            target_engines = list(self.engines.values())

        if not target_engines:
            return {"results": [], "sources": []}

        # 并发执行各引擎检索
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            tasks = [self._search_engine(engine, query, client)
                     for engine in target_engines]
            engine_outputs = await asyncio.gather(*tasks, return_exceptions=True)

        all_results: List[TorrentResult] = []
        diagnostics = []
        for i, res in enumerate(engine_outputs):
            eng = target_engines[i]
            if isinstance(res, Exception):
                logger.error("引擎 [%s] 运行报错: %s", eng.display_name, type(res).__name__)
                diagnostics.append({"id": eng.name, "name": eng.display_name, "state": "failed",
                                    "error": type(res).__name__, "count": 0, "results": []})
            elif isinstance(res, list):
                source_items = {}
                for item in res:
                    if not isinstance(item, TorrentResult):
                        continue
                    try:
                        link = normalize_magnet(item.magnet)
                    except ValueError:
                        logger.warning("[%s] 忽略无效磁力链接", eng.display_name)
                        continue
                    item.magnet, item.info_hash = link.magnet, link.info_hash
                    item.page_url = web_url(item.page_url)
                    # 计算推荐分
                    item.score = self.calculate_score(item, query, eng.weight)
                    existing = source_items.get(item.info_hash)
                    if existing is None or (matches_query(item.title, query), item.score) > (matches_query(existing.title, query), existing.score):
                        if existing:
                            item.source = ", ".join(dict.fromkeys((existing.source + ", " + item.source).split(", ")))
                        source_items[item.info_hash] = item
                    elif item.source != existing.source:
                        existing.source = ", ".join(dict.fromkeys((existing.source + ", " + item.source).split(", ")))
                kept = sorted(source_items.values(), key=lambda item: (matches_query(item.title, query), item.score), reverse=True)[:self.max_results_per_engine]
                diagnostics.append({"id": eng.name, "name": eng.display_name, "state": "ok",
                                    "error": "", "count": len(kept), "available_count": len(source_items),
                                    "limit": self.max_results_per_engine, "results": [item.to_dict() for item in kept]})
                all_results.extend(kept)
            else:
                diagnostics.append({"id": eng.name, "name": eng.display_name, "state": "failed",
                                    "error": "InvalidPluginResponse", "count": 0, "results": []})

        if not all_results:
            return {"results": [], "sources": diagnostics}

        # 根据 info_hash 进行唯一性去重 (保留做种数更多或得分更高的项)
        dedup_map: Dict[str, TorrentResult] = {}
        for item in all_results:
            key = item.info_hash.lower()
            if key in dedup_map:
                existing = dedup_map[key]
                # 合并来源说明，保留更高健康度的项
                item_rank = (matches_query(item.title, query), item.score, item.seeders)
                existing_rank = (matches_query(existing.title, query), existing.score, existing.seeders)
                if item_rank > existing_rank:
                    item.source = f"{existing.source}, {item.source}"
                    dedup_map[key] = item
                else:
                    existing.source = f"{existing.source}, {item.source}"
            else:
                dedup_map[key] = item

        deduped_results = list(dedup_map.values())

        # 按打分进行最优降序排列
        # 先保留相关标题，再应用全局上限。否则其他源的高分无关结果
        # 会挤掉单源中可用的条目，最终被应用层全部过滤成空结果。
        deduped_results.sort(key=lambda x: (matches_query(x.title, query), x.score), reverse=True)

        # 标记总分第一且健康度良好的条目为 "最优推荐"
        if deduped_results and deduped_results[0].seeders_known and deduped_results[0].seeders > 0:
            deduped_results[0].is_best = True

        return {"results": deduped_results[:self.max_results], "sources": diagnostics}
