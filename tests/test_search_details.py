import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from batch import BatchService
from downloads import DownloadService
from main import config, create_app
from search_engine.base import TorrentResult, web_url
from search_engine.manager import EngineManager
from search_engine.search_generic import GenericHTMLEngine
from storage import Storage
from support import settings_for


def item(index, title='中文'):
    identity = f'{index:040x}'
    return TorrentResult('引擎一', f'{title} {index} 1080p', 'magnet:?xt=urn:btih:'+identity,
                         identity, 1000, '1000 B', index, page_url=f'https://example.test/detail/{index}',
                         hotness=30, updated_at='2026/10/06')


def fixture_manager(limit=2):
    settings = {'engines': {key: {'enabled': False} for key in
                           ('mikan', 'bitsearch', 'solidtorrents', 'thepiratebay')},
                'search': {'max_results_per_engine': limit}}
    manager = EngineManager(settings)
    engines = {key: GenericHTMLEngine({'id': key, 'name': name, 'search_url': 'https://example.test/?q={query}'})
               for key, name in [('first', '引擎一'), ('empty', '引擎二'), ('broken', '引擎三')]}
    async def success(query, client):
        return [item(index) for index in range(1, 8)]
    async def empty(query, client):
        return []
    async def broken(query, client):
        raise TimeoutError('fixture')
    engines['first'].search, engines['empty'].search, engines['broken'].search = success, empty, broken
    manager.engines = engines
    return manager


class SearchDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_per_engine_limit_empty_failure_and_metadata(self):
        report = await fixture_manager(2).search_report('中文')
        source, empty, failed = report['sources']
        self.assertEqual(source['count'], 2)
        self.assertEqual(source['available_count'], 7)
        self.assertEqual(len(source['results']), 2)
        self.assertEqual(source['results'][0]['updated_at'], '2026/10/06')
        self.assertEqual(source['results'][0]['hotness'], 30)
        self.assertEqual(empty['count'], 0)
        self.assertEqual(empty['state'], 'ok')
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(len((await fixture_manager(4).search_report('中文'))['sources'][0]['results']), 4)

    async def test_snapshots_survive_restart_and_detail_download_is_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'history.db'
            store = Storage(path)
            store.initialize()
            service = BatchService(store, fixture_manager(5), {})
            await service.start()
            try:
                batch_id = await service.submit('中文')
                await service.queue.join()
            finally:
                await service.stop()
            saved = store.get_batch(batch_id)
            row_id = saved['rows'][0]['id']
            self.assertEqual(len(saved['rows'][0]['candidates']), 3)
            detail = store.row_details(batch_id, row_id)
            self.assertTrue(detail['snapshot_available'])
            self.assertEqual(len(detail['sources'][0]['results']), 5)
            other = detail['sources'][0]['results'][4]  # 推荐栏以外的结果。
            downloader = DownloadService(store, config)
            downloader.client = MagicMock()
            downloader.client.torrents_info.return_value = []
            downloader.client.torrents_add.return_value = 'Ok.'
            task = downloader.submit(other['id'])
            self.assertEqual(len(store.get_batch(batch_id)['rows'][0]['downloads']), 1)
            self.assertEqual(store.row_details(batch_id, row_id)['sources'][0]['results'][4]['download']['id'], task['id'])
            top = store.get_batch(batch_id)['rows'][0]['candidates'][0]
            detail_top = next(result for result in detail['sources'][0]['results'] if result['info_hash']==top['info_hash'])
            top_task = downloader.submit(detail_top['id'])
            updated = store.get_batch(batch_id)['rows'][0]['candidates'][0]
            self.assertEqual(updated['download']['id'], top_task['id'])
            reopened = Storage(path)
            reopened.initialize()
            self.assertEqual(reopened.row_details(batch_id, row_id)['sources'][0]['results'][4]['id'], other['id'])
            with patch('downloads.qbittorrentapi.Client', return_value=downloader.client), TestClient(create_app(settings_for(directory), path, fixture_manager())) as client:
                response = client.get(f'/api/batches/{batch_id}/rows/{row_id}/details')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['sources'][0]['results'][4]['id'], other['id'])
                self.assertEqual(client.get(f'/api/batches/wrong/rows/{row_id}/details').status_code, 404)


class DetailsCompatibilityTests(unittest.TestCase):
    def test_legacy_database_keeps_candidates_and_adds_default_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'legacy.db'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE candidates(id TEXT PRIMARY KEY,row_id TEXT,rank INTEGER,data TEXT,download_id TEXT)')
                db.execute('INSERT INTO candidates VALUES(?,?,?,?,?)', ('old', 'row', 1, json.dumps(item(1).to_dict()), None))
                db.execute('PRAGMA user_version=1')
            store = Storage(path)
            store.initialize()
            store.initialize()
            self.assertEqual(store.candidate('old')['recommended'], 1)
            self.assertEqual(store.candidate('old')['data']['info_hash'], item(1).info_hash)
            with store.connection() as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)

    def test_old_history_has_no_fabricated_details_and_invalid_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(Path(directory)/'test.db')
            store.initialize()
            batch_id = store.create_batch([(1, '中文')])
            row_id = store.input_rows(batch_id)[0]['id']
            store.finish_rows([row_id], [item(1).to_dict()], [{'id': 'old', 'name': '旧引擎', 'state':'ok','count': 3}])
            self.assertFalse(store.row_details(batch_id, row_id)['snapshot_available'])
        for value in ('javascript:alert(1)', 'https://user:password@example.test/', 'http://[bad'):
            self.assertIsNone(web_url(value))
