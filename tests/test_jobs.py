import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from batch import BatchService
from delivery import DeliveryService
from downloads import DownloadService
from main import create_app
from report import render_report
from storage import Storage
from support import settings_for
from test_search_details import fixture_manager, item as resource


class JobAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = settings_for(self.directory.name)
        self.settings["batch"]["max_lines"] = 4
        self.qbt = MagicMock()
        self.qbt.torrents_info.return_value = []
        self.qbt.torrents_add.return_value = "Ok."
        patcher = patch("downloads.qbittorrentapi.Client", return_value=self.qbt)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app(self.settings, Path(self.directory.name)/"db.sqlite", fixture_manager())
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, text="中文\n中文\n", **options):
        response = self.client.post("/api/jobs", json={"text": text, **options})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def get(self, job):
        return self.client.get(f"/api/jobs/{job}").json()

    def search(self, job):
        response = self.client.post(f"/api/jobs/{job}/search", json={"sources": ["first", "empty"]})
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(200):
            saved = self.get(job)
            if not saved["searching"]:
                return saved
            time.sleep(.01)
        self.fail("搜索未完成")

    def test_parse_append_limits_and_idempotent_create(self):
        job = self.create("  中文  \n\n中文", request_key="same")
        self.assertEqual(self.create("  中文  \n\n中文", request_key="same"), job)
        saved = self.get(job)
        self.assertEqual(saved["items"][0]["original_text"], "  中文  ")
        self.assertEqual(saved["total"], 2)
        self.assertIsNone(saved["last_search_at"])
        self.assertEqual(saved["items"][0]["state"], "unsearched")
        self.assertEqual(self.client.post(f"/api/jobs/{job}/items", json={"text":"第三\n第四"}).status_code, 201)
        self.assertEqual(self.client.post(f"/api/jobs/{job}/items", json={"text":"第五"}).status_code, 400)
        self.assertEqual(self.get(job)["total"], 4)
        self.assertEqual(self.client.post("/api/jobs", json={"text":"中文", "sources":[]}).status_code, 400)

    def test_latest_search_rename_and_unstarted_delete(self):
        one = self.create("中文")
        self.search(one)
        two = self.create("新列表")
        before = self.get(one)
        self.assertEqual(self.client.get("/api/jobs").json()["items"][0]["id"], one)
        self.assertEqual(self.client.patch(f"/api/jobs/{one}", json={"name":"新名字"}).status_code, 200)
        after = self.get(one)
        self.assertEqual(before["dir_name"], after["dir_name"])
        self.assertEqual(before["last_search_at"], after["last_search_at"])
        self.assertEqual(self.client.delete(f"/api/jobs/{two}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{two}").status_code, 404)

    def test_requery_keeps_downloads_edit_rejects_old_candidate_and_delete_retains(self):
        job = self.create("中文")
        first = self.search(job)["items"][0]
        candidate = first["candidates"][0]
        response = self.client.post(f"/api/job-items/{first['id']}/downloads", json={"candidate_id":candidate["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        again = self.search(job)["items"][0]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(again["downloads"]), 1)
        self.client.post(f"/api/job-items/{first['id']}/downloads", json={"candidate_id":again["candidates"][0]["id"]})
        self.qbt.torrents_add.assert_called_once()
        self.assertEqual(len(self.client.get(f"/api/job-items/{first['id']}/history").json()), 2)
        self.assertEqual(self.client.patch(f"/api/job-items/{first['id']}", json={"query":"新的关键词"}).status_code, 200)
        edited = self.get(job)["items"][0]
        self.assertEqual(edited["state"], "unsearched")
        self.assertEqual(edited["downloads"][0]["query"], "中文")
        self.assertEqual(self.client.post(f"/api/job-items/{first['id']}/downloads", json={"candidate_id":candidate["id"]}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/jobs/{job}").status_code, 400)
        self.assertEqual(self.client.patch(f"/api/jobs/{job}", json={"complete_root":str(Path(self.directory.name)/"else")}).status_code, 400)
        self.assertEqual(self.client.delete(f"/api/job-items/{first['id']}").status_code, 200)
        deleted = self.get(job)
        self.assertEqual(deleted["total"], 0)
        self.assertEqual(len(deleted["retained"][0]["downloads"]), 1)
        self.assertEqual(len(self.client.get(f"/api/job-items/{first['id']}/history").json()), 2)
        detail = self.client.get(f"/api/job-items/{first['id']}/details", params={"snapshot_id":first["latest_row_id"]})
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["sources"])
        self.assertEqual(self.client.post(f"/api/job-items/{first['id']}/downloads", json={"candidate_id":candidate["id"]}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/jobs/{job}").status_code, 400)
        report = self.client.get(f"/api/jobs/{job}/report")
        self.assertEqual(report.status_code, 200)
        self.assertIn("已移出清单", report.text)
        self.assertIn("无目标条目", report.text)

    def test_candidate_ownership_and_active_sort_before_pagination(self):
        job = self.create("中文\n中文")
        saved = self.search(job)
        first, second = saved["items"]
        candidate = second["candidates"][0]
        self.assertEqual(self.client.post(f"/api/job-items/{first['id']}/downloads", json={"candidate_id":candidate["id"]}).status_code, 404)
        response = self.client.post(f"/api/job-items/{second['id']}/downloads", json={"candidate_id":candidate["id"]})
        task = response.json()["task"]
        self.app.state.storage.update_download(task["id"], submission_state="confirmed", state="downloading", progress=.5)
        sorted_page = self.client.get(f"/api/jobs/{job}?page_size=1").json()
        self.assertEqual(sorted_page["items"][0]["id"], second["id"])
        original = self.client.get(f"/api/jobs/{job}?page_size=1&order=original").json()
        self.assertEqual(original["items"][0]["id"], first["id"])


class JobRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_and_remove_while_searching_does_not_publish_old_keyword(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(Path(directory)/"db", settings_for(directory))
            store.initialize()
            job = store.create_job(["中文"])
            current = store.get_job(job)["items"][0]
            manager = fixture_manager()
            entered, release = asyncio.Event(), asyncio.Event()
            original = manager.search_report
            async def delayed(query, source):
                entered.set()
                await release.wait()
                return await original(query, source)
            manager.search_report = delayed
            service = BatchService(store, manager, {})
            await service.start()
            try:
                await service.submit_job(job)
                await entered.wait()
                with self.assertRaises(ValueError):
                    await service.submit_job(job)
                store.patch_item(current["id"], "新词")
                release.set()
                await service.queue.join()
                item = store.get_job(job)["items"][0]
                self.assertEqual(item["query"], "新词")
                self.assertEqual(item["state"], "unsearched")
                self.assertEqual(len(store.item_history(item["id"])), 1)
                store.delete_item(item["id"])
                self.assertEqual(store.get_job(job)["total"], 0)
            finally:
                release.set()
                await service.stop()


class FileDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = settings_for(self.root)
        self.store = Storage(self.root/"db", self.settings)
        self.store.initialize()
        self.delivery = DeliveryService(self.store, self.settings)
        self.downloader = DownloadService(self.store, self.settings)
        self.qbt = MagicMock()
        self.qbt.torrents_info.return_value = []
        self.qbt.torrents_add.return_value = "Ok."
        self.downloader.client = self.qbt

    def attach(self, identity=1):
        job = self.store.create_job(["中文"])
        current = self.store.get_job(job)["items"][0]
        batch = self.store.create_batch([(1,"中文")], job_rows=[(current["id"],1)])
        row = self.store.input_rows(batch)[0]
        self.store.finish_rows([row["id"]], [resource(identity).to_dict()])
        candidate = self.store.get_job(job)["items"][0]["candidates"][0]
        task = self.downloader.submit(candidate["id"])
        return job, current["id"], task

    def complete(self, task, size=4):
        source = self.root/"original"/"movie.bin"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"data")
        self.store.update_download(task["id"], submission_state="confirmed", state="uploading", progress=1,
                                   completion_on=1000, completed_observed_at=1001, size=size,
                                   content_path=str(source), save_path=str(source.parent))
        return source

    def test_cross_batch_reuse_copies_and_report_survives_restart(self):
        one, item_id, task = self.attach()
        source = self.complete(task)
        two, _, same_task = self.attach()
        self.assertEqual(task["id"], same_task["id"])
        self.qbt.torrents_add.assert_called_once()
        self.delivery.sync_once()
        for job_id in (one,two):
            job = self.store.get_job(job_id)
            self.assertEqual(job["satisfied"], 1)
            link = job["items"][0]["downloads"][0]
            self.assertEqual(Path(link["target_path"]).read_bytes(), b"data")
            self.assertTrue((Path(job["complete_path"])/"report.txt").is_file())
        self.assertTrue(source.exists())
        reopened = Storage(self.root/"db", self.settings)
        reopened.initialize()
        self.assertEqual(reopened.get_job(one)["satisfied"], 1)
        refs = reopened.download_references(task["id"])
        self.assertEqual(len(refs), 2)
        with self.assertRaises(ValueError):
            self.downloader.control(task["id"], "remove", True)
        self.store.patch_item(item_id, "另一部")
        self.assertEqual(self.store.get_job(one)["satisfied"], 0)

    def test_incomplete_file_does_not_satisfy_and_retry_can_recover(self):
        job, _, task = self.attach()
        source = self.complete(task, size=10)
        self.delivery.sync_once()
        first = self.store.get_job(job)
        self.assertEqual(first["satisfied"], 0)
        self.assertEqual(first["items"][0]["downloads"][0]["file_state"], "failed")
        source.write_bytes(b"0123456789")
        self.delivery.sync_once()
        self.assertEqual(self.store.get_job(job)["satisfied"], 1)

    def test_multi_version_first_actual_completion_adopted_not_progress_sum(self):
        job, item_id, first = self.attach(1)
        current = self.store.get_job(job)["items"][0]
        batch = self.store.create_batch([(1,"中文")], job_rows=[(item_id,1)])
        row = self.store.input_rows(batch)[0]
        self.store.finish_rows([row["id"]], [resource(2).to_dict()])
        second = self.downloader.submit(self.store.get_job(job)["items"][0]["candidates"][0]["id"])
        self.store.update_download(first["id"], progress=.5)
        self.store.update_download(second["id"], progress=.5)
        self.delivery.sync_once()
        self.assertEqual(self.store.get_job(job)["satisfied"], 0)
        self.complete(first)
        self.complete(second)
        self.store.update_download(second["id"], completion_on=900)
        self.delivery.sync_once()
        saved = self.store.get_job(job)
        adopted = next(link for link in saved["items"][0]["downloads"] if link["adopted"])
        self.assertEqual(adopted["download_id"], second["id"])
        self.assertEqual(len(saved["items"][0]["downloads"]), 2)

    def test_mapping_boundary_and_copy_collision_are_rejected(self):
        self.assertEqual(Storage.path_join("/downloads", "批次", "file"), "/downloads/批次/file")
        self.assertEqual(Storage.path_join("D:\\Downloads", "批次", "file"), "D:\\Downloads\\批次\\file")
        self.delivery.options["path_mappings"] = [{"remote":"/remote","local":str(self.root)}]
        self.assertEqual(self.delivery.local_path("/remote/movie"), self.root/"movie")
        with self.assertRaises(ValueError):
            self.delivery.local_path("/remote/../outside")
        source, destination = self.root/"source", self.root/"destination"
        source.write_bytes(b"data")
        destination.write_bytes(b"different")
        with self.assertRaises(ValueError):
            self.delivery.copy_complete(source,destination,"link")
        self.assertEqual(destination.read_bytes(),b"different")

    def test_manual_copy_removal_preserves_original_and_is_not_recreated(self):
        job, _, task = self.attach()
        source = self.complete(task)
        self.delivery.sync_once()
        link = self.store.get_job(job)["items"][0]["downloads"][0]
        target = Path(link["target_path"])
        self.delivery.remove_copy(link["id"])
        self.assertFalse(target.exists())
        self.assertTrue(source.exists())
        self.delivery.sync_once()
        self.assertFalse(target.exists())
        self.assertEqual(self.store.get_job(job)["satisfied"], 0)

    def test_deletion_rejects_original_content_and_batch_root(self):
        job, _, task = self.attach()
        source = self.complete(task)
        self.delivery.sync_once()
        saved = self.store.get_job(job)
        link = saved["items"][0]["downloads"][0]
        self.store.update_delivery(link["id"], "ready", str(source))
        with self.assertRaises(ValueError):
            self.delivery.remove_copy(link["id"])
        self.store.update_delivery(link["id"], "ready", saved["complete_path"])
        with self.assertRaises(ValueError):
            self.delivery.remove_copy(link["id"])
        self.assertTrue(source.exists())

    def test_report_failure_preserves_previous_report_and_completion_history(self):
        job, _, task = self.attach()
        source = self.complete(task)
        self.delivery.sync_once()
        saved = self.store.get_job(job)
        report_path = Path(saved["complete_path"])/"report.txt"
        old = report_path.read_text(encoding="utf-8")
        self.store.patch_job(job,name="新的名字")
        with patch("report.os.replace",side_effect=OSError("disk full")):
            self.delivery.write_report(job)
        self.assertEqual(report_path.read_text(encoding="utf-8"),old)
        self.assertTrue(self.store.get_job(job)["report_error"])
        source.unlink()
        self.store.update_download(task["id"],state="missing")
        self.delivery.sync_once()
        self.assertEqual(self.store.get_job(job)["satisfied"],1)
        self.assertIn("新的名字",render_report(self.store.get_job(job)))
