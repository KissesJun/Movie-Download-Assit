import asyncio
import base64
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, parse_qs, urlsplit

import httpx
from fastapi.testclient import TestClient

from search_engine.search_generic import GenericHTMLEngine
from search_engine.manager import EngineManager
from search_engine.magnets import extract_magnets, normalize_magnet
from main import create_app, config
from support import settings_for

HASH = "0123456789abcdef0123456789abcdef01234567"
URI = "magnet:?xt=urn:btih:" + HASH


class ExtractionTests(unittest.TestCase):
    def test_encodings_and_dedup(self):
        b32 = base64.b32encode(bytes.fromhex(HASH)).decode()
        values = [
            f'<a href="{URI}&amp;dn=%E4%B8%AD%E6%96%87&amp;tr=udp%3A%2F%2Ftest%3A80">下载</a>',
            quote(quote(URI, safe=""), safe=""),
            URI.replace(":", r"\u003a").replace("?", r"\x3f"),
            "MAGNET:?XT=URN:BTIH:" + b32,
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(extract_magnets(value)[0].info_hash, HASH)
        links = extract_magnets("\n".join(values))
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].title, "中文")
        params = parse_qs(urlsplit(links[0].magnet).query)
        self.assertEqual(params["tr"], ["udp://test:80"])

    def test_invalid_and_context_hashes(self):
        self.assertEqual(extract_magnets(HASH, allow_hashes=True), [])
        self.assertEqual(extract_magnets("info_hash: " + HASH), [])
        self.assertEqual(extract_magnets("特征码：" + HASH, allow_hashes=True)[0].info_hash, HASH)
        self.assertEqual(extract_magnets("https://test/" + HASH + ".torrent", allow_hashes=True)[0].info_hash, HASH)
        for uri in [URI + "f", "magnet:?dn=test", "https://test/", URI + "&xt=urn:btih:" + "a" * 40]:
            with self.assertRaises(ValueError):
                normalize_magnet(uri)

    def test_v2_and_hybrid(self):
        xt = "urn:btmh:1220" + "a" * 64
        self.assertEqual(normalize_magnet("magnet:?xt=" + xt).info_hash, "btmh:1220" + "a" * 64)
        self.assertEqual(normalize_magnet(URI + "&xt=" + xt).info_hash, HASH)
        self.assertIn("btmh", normalize_magnet(URI + "&xt=" + xt).magnet)


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_search_detail_and_downloadable_results(self):
        requested = []

        def handler(req):
            requested.append(req.url)
            if req.url.path == "/search":
                self.assertEqual(req.url.params["q"], "中文 1080p")
                return httpx.Response(200, text='<article><h2>中文 1080p</h2><a href="/detail">详情</a><a href="https://other.test/detail">外站</a></article>')
            return httpx.Response(200, text=f'<a href="{URI}">磁力</a>')

        cfg = {"id": "fixture", "search_url": "https://site.test/search?q={query}",
               "result_selector": "article", "title_selector": "h2",
               "detail_link_selector": "a", "max_detail_pages": 1}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            results = await GenericHTMLEngine(cfg).search("中文 1080p", client)
        self.assertEqual(len(requested), 2)
        self.assertEqual(results[0].info_hash, HASH)
        self.assertEqual(results[0].quality, "1080p")
        self.assertFalse(results[0].seeders_known)

    async def test_manager_dedup_and_timeout_isolation(self):
        cfg = {"engines": {key: {"enabled": False} for key in ("mikan", "bitsearch", "solidtorrents", "thepiratebay")},
               "search": {"timeout_seconds": 0.05}}
        manager = EngineManager(cfg)
        engine = GenericHTMLEngine({"id": "fixture", "search_url": "https://site.test/?q={query}"})
        from search_engine.search_generic import results_from_text

        async def successful(query, client):
            return results_from_text(URI, "甲") + results_from_text(URI.upper(), "乙")

        async def stalled(query, client):
            await asyncio.sleep(5)

        other = GenericHTMLEngine({"id": "slow", "search_url": "https://site.test/?q={query}"})
        engine.search, other.search = successful, stalled
        manager.engines = {"fixture": engine, "slow": other}
        results = await manager.search("中文")
        self.assertEqual(len(results), 1)
        self.assertIn("甲", results[0].source)
        self.assertIn("乙", results[0].source)
        self.assertFalse(results[0].is_best)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patcher = patch("downloads.qbittorrentapi.Client")
        self.client_cls = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.client_cls.return_value.torrents_info.return_value = []
        self.client = TestClient(create_app(settings_for(self.directory.name), db_path=Path(self.directory.name) / "test.db"))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_extract_to_download_and_rejection(self):
        extracted = self.client.post("/api/extract", json={"text": URI, "title": "中文"}).json()
        result = extracted["results"][0]
        qbt = self.client_cls.return_value
        qbt.torrents_add.return_value = "Ok."
        response = self.client.post("/api/download", json={"candidate_id": result["id"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(qbt.torrents_add.call_args.kwargs["urls"], result["magnet"])
        qbt.torrents_add.return_value = "Fails."
        self.assertEqual(self.client.post("/api/download", json={"magnet": URI.replace(HASH, "b" * 40)}).status_code, 502)

    def test_invalid_input_never_reaches_downloader(self):
        self.assertEqual(self.client.post("/api/download", json={"magnet": "bad"}).status_code, 400)
        self.assertEqual(self.client.post("/api/download", json={"magnet": URI + "\n" + URI}).status_code, 400)
        self.client_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
