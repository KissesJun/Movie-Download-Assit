import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from downloads import DownloadError, DownloadService, add_response_state
from storage import Storage
from main import config
from search_engine.search_generic import results_from_text


HASH = "a" * 40
URI = "magnet:?xt=urn:btih:" + HASH


class DownloadTrackingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Storage(Path(self.directory.name) / "test.db")
        self.store.initialize()
        self.service = DownloadService(self.store, config)
        self.qbt = MagicMock()
        self.qbt.torrents_info.return_value = []
        self.qbt.torrents_add.return_value = "Ok."
        self.service.client = self.qbt

    def candidate(self, uri=URI, keyword="中文"):
        batch = self.store.create_batch([(1, keyword)])
        row = self.store.input_rows(batch)[0]
        self.store.finish_rows([row["id"]], [item.to_dict() for item in results_from_text(uri, "模拟源", keyword)])
        return self.store.get_batch(batch)["rows"][0]["candidates"][0]["id"]

    def torrent(self, progress=.5, state="downloading", completion=-1):
        return {"hash": HASH, "magnet_uri": URI, "progress": progress, "state": state,
                "added_on": 1000, "completion_on": completion}

    def test_concurrent_duplicate_clicks_submit_once_and_share_record(self):
        one, two = self.candidate(), self.candidate(keyword="另一个关键词")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            tasks = list(executor.map(self.service.submit, [one, two]))
        self.assertEqual(tasks[0]["id"], tasks[1]["id"])
        self.qbt.torrents_add.assert_called_once()
        self.assertEqual(self.store.candidate(one)["download_id"], self.store.candidate(two)["download_id"])

    def test_completion_persists_after_removal_and_connection_failure(self):
        task = self.service.submit(self.candidate())
        self.qbt.torrents_info.return_value = [self.torrent()]
        self.assertTrue(self.service.sync_once())
        self.assertEqual(self.store.download(task["id"])["progress"], .5)
        self.qbt.torrents_info.return_value = [self.torrent(1, "uploading", 2000)]
        self.service.sync_once()
        self.service.sync_once()
        completed = self.store.download(task["id"])
        self.assertEqual(completed["completion_on"], 2000)
        self.assertEqual(sum(event["type"] == "completed" for event in completed["events"]), 1)
        self.qbt.torrents_info.return_value = []
        self.service.sync_once()
        self.assertEqual(self.store.download(task["id"])["state"], "missing")
        self.qbt.torrents_info.side_effect = ConnectionError("offline")
        self.assertFalse(self.service.sync_once())
        self.assertEqual(self.store.download(task["id"])["completion_on"], 2000)
        self.assertTrue(self.store.download(task["id"])["sync_error"])

    def test_uncertain_submission_is_never_automatically_retried(self):
        candidate = self.candidate()
        self.qbt.torrents_add.side_effect = TimeoutError("lost response")
        with self.assertRaises(DownloadError):
            self.service.submit(candidate)
        self.service.client = self.qbt
        again = self.service.submit(candidate)
        self.assertEqual(again["submission_state"], "uncertain")
        self.qbt.torrents_add.assert_called_once()
        self.qbt.torrents_info.return_value = [self.torrent()]
        self.service.sync_once()
        self.assertEqual(self.store.download(again["id"])["submission_state"], "confirmed")

    def test_existing_task_and_two_candidates_on_same_input_row(self):
        batch = self.store.create_batch([(1, "中文")])
        row = self.store.input_rows(batch)[0]
        other_uri = URI.replace(HASH, "b" * 40)
        items = results_from_text(URI + "\n" + other_uri, "来源", "中文")
        self.store.finish_rows([row["id"]], [item.to_dict() for item in items])
        candidates = self.store.get_batch(batch)["rows"][0]["candidates"]
        self.qbt.torrents_info.return_value = [self.torrent()]
        one = self.service.submit(candidates[0]["id"])
        self.qbt.torrents_add.assert_not_called()
        two = self.service.submit(candidates[1]["id"])
        self.assertNotEqual(one["id"], two["id"])
        saved = self.store.get_batch(batch)["rows"][0]["candidates"]
        self.assertTrue(all(item["download"] for item in saved))

    def test_v2_identity_uses_magnet_when_webui_hash_is_short(self):
        uri = "magnet:?xt=urn:btmh:1220" + "c" * 64
        candidate = self.candidate(uri)
        self.qbt.torrents_info.return_value = [{"hash": "c" * 40, "magnet_uri": uri,
                                                "progress": .2, "state": "downloading"}]
        task = self.service.submit(candidate)
        self.assertEqual(task["submission_state"], "confirmed")
        self.qbt.torrents_add.assert_not_called()

    def test_first_observed_completion_is_separate_from_actual_timestamp(self):
        task = self.service.submit(self.candidate())
        self.qbt.torrents_info.return_value = [self.torrent(1, "uploading")]
        self.service.sync_once()
        saved = self.store.download(task["id"])
        self.assertIsNone(saved["completion_on"])
        self.assertIsNotNone(saved["completed_observed_at"])

    def test_new_api_json_success_and_pending_are_accepted(self):
        from qbittorrentapi.torrents import TorrentsAddedMetadata
        for index, response in enumerate((
            TorrentsAddedMetadata({'success_count': 1, 'failure_count': 0,
                                  'pending_count': 0, 'added_torrent_ids': [HASH]}),
            {'success_count': 0, 'failure_count': 0, 'pending_count': 1, 'added_torrent_ids': []},
            '{"success_count":1,"failure_count":0,"pending_count":0}',
        )):
            with self.subTest(response=response):
                candidate = self.candidate(URI.replace(HASH, "bcd"[index] * 40))
                self.qbt.torrents_add.return_value = response
                task = self.service.submit(candidate)
                self.assertEqual(task['submission_state'], 'accepted')
                self.assertEqual(task['error'], '')

    def test_unknown_response_checks_list_and_never_automatically_resubmits(self):
        candidate = self.candidate()
        self.qbt.torrents_add.return_value = ''
        with self.assertRaises(DownloadError):
            self.service.submit(candidate)
        task = self.service.submit(candidate)
        self.assertEqual(task['submission_state'], 'uncertain')
        self.qbt.torrents_add.assert_called_once()
        self.qbt.torrents_info.return_value = [self.torrent()]
        self.service.sync_once()
        self.assertEqual(self.store.download(task['id'])['submission_state'], 'confirmed')

    def test_failed_record_recovers_only_when_torrent_really_exists(self):
        candidate = self.candidate()
        self.qbt.torrents_add.return_value = 'Fails.'
        with self.assertRaises(DownloadError):
            self.service.submit(candidate)
        task_id = self.store.candidate(candidate)['download_id']
        self.service.sync_once()
        self.assertEqual(self.store.download(task_id)['submission_state'], 'failed')
        self.qbt.torrents_info.return_value = [self.torrent()]
        self.service.sync_once()
        self.assertEqual(self.store.download(task_id)['submission_state'], 'confirmed')
        self.assertEqual(self.store.download(task_id)['error'], '')
        self.qbt.torrents_add.assert_called_once()

    def test_explicit_new_api_rejection_and_unknown_responses(self):
        self.assertEqual(add_response_state({'success_count': 0, 'pending_count': 0, 'failure_count': 1}), 'failed')
        for response in (None, '', {}, {'success_count': 'bad'}, '<html>Unexpected</html>'):
            self.assertEqual(add_response_state(response), 'uncertain')

    def test_unknown_response_is_confirmed_by_immediate_task_lookup(self):
        self.qbt.torrents_add.return_value = ''
        self.qbt.torrents_info.side_effect = [[], [self.torrent()]]
        task = self.service.submit(self.candidate())
        self.assertEqual(task['submission_state'], 'confirmed')
