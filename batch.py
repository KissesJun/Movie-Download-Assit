"""有限工作线程式 asyncio 队列：所有批次共享上限，结果立即持久化。"""

import asyncio
import logging
from search_engine.base import matches_query

logger = logging.getLogger(__name__)


def recommendations(results, query):
    accepted = []
    identities = set()
    for item in sorted(results, key=lambda result: result.score, reverse=True):
        if item.info_hash not in identities and matches_query(item.title, query):
            identities.add(item.info_hash)
            accepted.append(item.to_dict())
        if len(accepted) == 3:
            break
    return accepted


class BatchService:
    def __init__(self, storage, engine_manager, config):
        self.storage = storage
        self.manager = engine_manager
        self.max_lines = max(1, int(config.get("max_lines", 1000)))
        self.concurrency = max(1, min(int(config.get("concurrency", 3)), 20))
        self.max_pending = max(self.concurrency, int(config.get("max_pending", 2000)))
        self.queue = asyncio.Queue()
        self.workers = []
        self.pending = 0
        self.submit_lock = asyncio.Lock()

    async def start(self):
        self.workers = [asyncio.create_task(self.worker()) for _ in range(self.concurrency)]

    async def stop(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        # 等待线程池中的短写事务完成后再标记中断。
        await asyncio.to_thread(self.storage.recover)

    async def db_call(self, method, *args):
        work = asyncio.create_task(asyncio.to_thread(method, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            await work
            raise

    async def submit(self, text, source="all"):
        self.manager.validate_source(source)
        lines = [(number, line) for number, line in enumerate(text.splitlines(), 1)
                 if line.strip().lstrip("\ufeff")]
        if not lines:
            raise ValueError("请输入至少一个关键词")
        if len(lines) > self.max_lines:
            raise ValueError(f"一次最多 {self.max_lines} 行，输入未被截断")
        async with self.submit_lock:
            unique_count = len({line.strip().lstrip("\ufeff").casefold() for _, line in lines})
            if self.pending + unique_count > self.max_pending:
                raise OverflowError("搜索队列已满，请等待当前批次完成")
            batch_id = await asyncio.to_thread(self.storage.create_batch, lines, source)
            rows = await asyncio.to_thread(self.storage.input_rows, batch_id)
            groups = {}
            for row in rows:
                group = groups.setdefault(row["query"].casefold(), {"query": row["query"], "ids": []})
                group["ids"].append(row["id"])
            self.pending += len(groups)
            for group in groups.values():
                self.queue.put_nowait((group, source))
            return batch_id

    @staticmethod
    def parse_lines(text):
        lines = [line for line in text.splitlines() if line.strip().lstrip("\ufeff")]
        if not lines:
            raise ValueError("请输入至少一个关键词")
        if any(len(line.strip().lstrip("\ufeff")) > 1000 for line in lines):
            raise ValueError("每行关键词最多 1000 字符")
        return lines

    async def submit_job(self, job_id, item_id=None, source=None):
        """搜索快照挂到固定工作行；限流与旧接口共用同一队列。"""
        async with self.submit_lock:
            job, items = await asyncio.to_thread(self.storage.search_items, job_id, item_id)
            selected_source = source if source is not None else job["source"]
            self.manager.validate_source(selected_source)
            groups = {}
            for item in items:
                group = groups.setdefault(item["query"].casefold(), {"query": item["query"], "items": []})
                group["items"].append(item)
            if self.pending + len(groups) > self.max_pending:
                raise OverflowError("搜索队列已满，请等待当前检索完成")
            lines = [(item["ordinal"], item["query"]) for item in items]
            job_rows = [(item["id"], item["revision"]) for item in items]
            batch_id = await asyncio.to_thread(self.storage.create_batch, lines, selected_source, "search", job_rows)
            rows = await asyncio.to_thread(self.storage.input_rows, batch_id)
            self.pending += len(groups)
            for group in groups.values():
                ids = {item["id"] for item in group["items"]}
                self.queue.put_nowait(({"query": group["query"], "ids": [row["id"] for row in rows if row["job_item_id"] in ids]}, selected_source))
            return batch_id

    async def worker(self):
        while True:
            group, source = await self.queue.get()
            try:
                await self.db_call(self.storage.start_rows, group["ids"])
                report = await self.manager.search_report(group["query"], source)
                selected = recommendations(report["results"], group["query"])
                successful = any(item["state"] == "ok" for item in report["sources"])
                state = "success" if selected else ("empty" if successful else "failed")
                error = "" if successful else "没有启用的来源或所有搜索来源均失败"
                await self.db_call(self.storage.finish_rows, group["ids"], selected,
                                        report["sources"], state, error)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("批量搜索失败")
                await self.db_call(self.storage.finish_rows, group["ids"], [], [], "failed", "搜索处理失败")
            finally:
                self.pending -= 1
                self.queue.task_done()
