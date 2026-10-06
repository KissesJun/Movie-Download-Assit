import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from batch import BatchService, recommendations
from main import create_app, config
from search_engine.base import TorrentResult
from search_engine.manager import EngineManager
from search_engine.search_generic import GenericHTMLEngine
from storage import Storage
from support import settings_for


def result(title="中文 1080p", identity="a" * 40, score=50):
    return TorrentResult("模拟来源", title, "magnet:?xt=urn:btih:" + identity,
                         identity, 0, "未知大小", 0, score=score, seeders_known=False)


class FakeManager:
    def __init__(self):
        self.calls = []
        self.running = 0
        self.peak = 0

    def validate_source(self, source):
        if source != "all":
            raise ValueError("无此来源")

    async def search_report(self, query, source):
        self.calls.append(query)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(.01)
            if query == "失败":
                return {"results": [], "sources": [{"id": "fixture", "name": "模拟来源", "state": "failed", "error": "TimeoutError"}]}
            return {"results": [result(query + " 1080p"), result("完全无关", "b" * 40, 100)],
                    "sources": [{"id": "fixture", "name": "模拟来源", "state": "ok", "error": ""}]}
        finally:
            self.running -= 1

    def get_available_sources(self):
        return [{"id": "all", "name": "全部"}]


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Storage(Path(self.directory.name) / "test.db")
        self.store.initialize()
        self.manager = FakeManager()
        self.service = BatchService(self.store, self.manager, {"concurrency": 2, "max_lines": 10})
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.stop()
        self.directory.cleanup()

    async def test_order_duplicates_global_concurrency_and_history(self):
        first = await self.service.submit("  中文  \n\n第二个\n中文\n失败")
        second = await self.service.submit("第三个\n第四个")
        await self.service.queue.join()
        saved = self.store.get_batch(first)
        self.assertEqual([row["line_number"] for row in saved["rows"]], [1, 3, 4, 5])
        self.assertEqual(saved["rows"][0]["original_text"], "  中文  ")
        self.assertEqual(self.manager.calls.count("中文"), 1)
        self.assertLessEqual(self.manager.peak, 2)
        self.assertEqual(saved["rows"][3]["state"], "failed")
        self.assertEqual(len(saved["rows"][0]["candidates"]), 1)
        self.assertEqual(self.store.history("中文")["total"], 1)
        self.assertEqual(self.store.get_batch(second)["state"], "completed")

    async def test_limits_and_pagination(self):
        with self.assertRaises(ValueError):
            await self.service.submit(" \n ")
        with self.assertRaises(ValueError):
            await self.service.submit("\n".join(str(index) for index in range(11)))
        with self.assertRaises(ValueError):
            await self.service.submit("中文", "missing")
        batch_id = await self.service.submit("一\n二\n三")
        await self.service.queue.join()
        page = self.store.get_batch(batch_id, 2, 2)
        self.assertEqual(len(page["rows"]), 1)
        self.assertEqual(page["rows"][0]["query"], "三")

    async def test_stop_marks_unfinished_records_interrupted(self):
        self.manager.search_report = self.stalled
        batch_id = await self.service.submit("中文\n其他")
        await asyncio.sleep(.02)
        await self.service.stop()
        self.assertEqual(self.store.get_batch(batch_id)["state"], "interrupted")
        self.assertTrue(all(row["state"] == "interrupted" for row in self.store.input_rows(batch_id)))

    async def stalled(self, query, source):
        await asyncio.sleep(10)


class RecommendationTests(unittest.TestCase):
    def test_unrelated_titles_and_duplicate_hashes_do_not_fill_top_three(self):
        selected = recommendations([result("无关", score=100), result("中文 1080p"),
                                    result("中文 4K"), result("中文 第二版", "b" * 40)], "中文")
        self.assertEqual(len(selected), 2)

    def test_quality_penalty_does_not_match_substring_in_normal_word(self):
        manager = EngineManager({"engines": {key: {"enabled": False} for key in ("mikan", "bitsearch", "solidtorrents", "thepiratebay")}})
        normal = result("Watchmen 1080p")
        cam = result("Watchmen TC 1080p")
        self.assertGreater(manager.calculate_score(normal, "Watchmen", 1), manager.calculate_score(cam, "Watchmen", 1))


class AggregateTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = EngineManager({"engines": {key: {"enabled": False} for key in
                                 ("mikan", "bitsearch", "solidtorrents", "thepiratebay")},
                                 "search": {"max_results": 1}})
        first = GenericHTMLEngine({"id": "first", "name": "来源一", "search_url": "https://example.test/?q={query}"})
        second = GenericHTMLEngine({"id": "second", "name": "来源二", "search_url": "https://example.test/?q={query}"})
        manager.engines = {"first": first, "second": second}
        return manager, first, second

    async def test_aggregate_preserves_matching_title_before_limit_and_dedup(self):
        for same_hash in (False, True):
            with self.subTest(same_hash=same_hash):
                manager, first, second = self.manager()
                async def relevant(query, client):
                    return [result("中文 720p")]
                async def unrelated(query, client):
                    item = result("不相关的高分标题 4K", "a" * 40 if same_hash else "b" * 40)
                    item.seeders = 10000
                    item.quality = "4K"
                    return [item]
                first.search, second.search = relevant, unrelated
                single = recommendations(await manager.search("中文", "first"), "中文")
                aggregate = recommendations(await manager.search("中文", "all"), "中文")
                self.assertEqual(len(single), 1)
                self.assertEqual(len(aggregate), 1)
                self.assertEqual(single[0]["info_hash"], aggregate[0]["info_hash"])

    async def test_multi_selection_calls_only_selected_sources(self):
        manager, first, second = self.manager()
        calls = []
        async def one(query, client):
            calls.append("first")
            return [result()]
        async def two(query, client):
            calls.append("second")
            return []
        first.search, second.search = one, two
        await manager.search("中文", "second")
        self.assertEqual(calls, ["second"])
        calls.clear()
        report = await manager.search_report("中文", "second,first,second")
        self.assertCountEqual(calls, ["first", "second"])
        self.assertEqual(len(report["sources"]), 2)
        for selection in ("", "first,unknown", "all,first"):
            with self.assertRaises(ValueError):
                manager.validate_source(selection)


class HistoryAPITests(unittest.TestCase):
    def test_refresh_history_and_requery_create_new_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "history.db"
            manager = FakeManager()
            with TestClient(create_app(settings_for(directory), database, manager)) as client:
                response = client.get("/api/search", params={"q": "中文"})
                self.assertEqual(response.status_code, 200)
                batch_id = response.json()["batch_id"]
                first = client.get("/api/batches/" + batch_id).json()
                self.assertEqual(first["rows"][0]["original_text"], "中文")
                again = client.post("/api/batches/" + batch_id + "/retry").json()["id"]
                self.assertNotEqual(batch_id, again)
                self.assertEqual(client.get("/api/history?q=中文").json()["total"], 2)
                self.assertEqual(client.get("/api/batches/missing").status_code, 404)
                self.assertEqual(client.post("/api/batches", json={"text": "中文", "source": "bad"}).status_code, 400)
                self.assertEqual(client.post("/api/batches", json={"text": "中文", "sources": []}).status_code, 400)
            with TestClient(create_app(settings_for(directory), database, FakeManager())) as client:
                persisted = client.get("/api/batches/" + batch_id).json()
                self.assertEqual(persisted["rows"][0]["candidates"][0]["id"], first["rows"][0]["candidates"][0]["id"])
