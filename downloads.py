"""下载提交与状态同步。一个锁保护 WebUI 会话及重复提交，不启动独立进程。"""

import asyncio
import json
import logging
import threading
import time
from urllib.parse import urlsplit
from collections.abc import Mapping

import qbittorrentapi

from search_engine.magnets import normalize_magnet

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def identities(torrent):
    values = set()
    for key in ("hash", "infohash_v1"):
        value = str(torrent.get(key) or "").lower()
        if len(value) == 40:
            values.add(value)
    v2 = str(torrent.get("infohash_v2") or "").lower()
    if len(v2) == 64:
        values.add("btmh:1220" + v2)
    magnet = torrent.get("magnet_uri")
    if isinstance(magnet, str):
        try:
            values.add(normalize_magnet(magnet).info_hash)
        except ValueError:
            pass
    return values


def positive_timestamp(value):
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


def add_response_state(response):
    """兼容旧版文本与 Web API 2.14+ JSON；未知响应不能当作明确失败。"""
    if isinstance(response, str):
        text = response.strip()
        if text.lower() == "ok.":
            return "accepted"
        if text.lower() == "fails.":
            return "failed"
        try:
            response = json.loads(text)
        except (ValueError, TypeError):
            return "uncertain"
    if isinstance(response, Mapping):
        try:
            success = int(response.get("success_count", 0))
            pending = int(response.get("pending_count", 0))
            failed = int(response.get("failure_count", 0))
        except (TypeError, ValueError):
            return "uncertain"
        if success > 0 or pending > 0:
            return "accepted"
        if failed > 0:
            return "failed"
    return "uncertain"


class DownloadService:
    def __init__(self, storage, config):
        self.storage = storage
        self.config = config
        self.qbt = config["qbittorrent"]
        host = str(self.qbt["host"])
        url = urlsplit(host if "://" in host else "http://" + host)
        self.downloader_key = f"{url.scheme}://{url.hostname}:{url.port or self.qbt['port']}{url.path.rstrip('/')}"
        self.public_url = config.get("download", {}).get("webui_url", self.downloader_key)
        self.interval = max(2, float(config.get("download", {}).get("sync_interval_seconds", 10)))
        self.client = None
        self.lock = threading.RLock()
        self.sync_task = None
        self.health = {"state": "not_checked", "last_sync": None, "error": ""}

    def _client(self):
        if self.client is None:
            client = qbittorrentapi.Client(
                host=self.qbt["host"], port=self.qbt["port"],
                username=self.qbt["username"], password=self.qbt["password"],
                REQUESTS_ARGS={"timeout": 8},
            )
            client.auth_log_in()
            self.client = client
        return self.client

    def _failure(self, exception):
        self.client = None
        message = "qBittorrent 登录失败" if isinstance(exception, qbittorrentapi.LoginFailed) else "无法连接或读取 qBittorrent，请检查 WebUI 配置"
        self.health = {**self.health, "state": "offline", "error": message}
        return message

    @staticmethod
    def _find(task, torrents):
        for torrent in torrents:
            if ((task["qbt_hash"] and str(torrent.get("hash", "")).lower() == task["qbt_hash"])
                    or task["identity"] in identities(torrent)):
                return torrent
        return None

    def _observe(self, task, torrent):
        now = time.time()
        progress = max(0.0, min(float(torrent.get("progress") or 0), 1.0))
        completion = positive_timestamp(torrent.get("completion_on"))
        state = str(torrent.get("state") or "unknown")
        # 不能将四舍五入显示的 100% 当作完成；校验过程中也不推断完成。
        complete = bool(completion) or (progress >= 1 and state in {
            "uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "forcedUP"
        })
        first_complete = complete and task["completed_observed_at"] is None
        confirmed = task["submission_state"] != "confirmed"
        updated = self.storage.update_download(
            task["id"], event="completed" if first_complete else ("confirmed" if confirmed else None),
            submission_state="confirmed", state=state, progress=progress,
            confirmed_at=task["confirmed_at"] or now,
            added_on=positive_timestamp(torrent.get("added_on")) or task["added_on"],
            completion_on=task["completion_on"] or completion,
            completed_observed_at=task["completed_observed_at"] or (now if complete else None),
            last_sync=now, qbt_hash=str(torrent.get("hash") or task["qbt_hash"]).lower(),
            error="", sync_error="",
            speed=max(0, int(torrent.get("dlspeed") or 0)), eta=int(torrent.get("eta") or 0) or None,
            size=max(0, int(torrent.get("total_size") or torrent.get("size") or 0)),
            save_path=str(torrent.get("save_path") or task.get("save_path") or ""),
            content_path=str(torrent.get("content_path") or task.get("content_path") or ""),
        )
        return updated

    def submit(self, candidate_id):
        """线程池调用。先查询再提交；不确定请求绝不自动重发。"""
        with self.lock:
            task = self.storage.reserve_download(candidate_id, self.downloader_key)
            try:
                client = self._client()
                torrents = list(client.torrents_info())
                self.health = {"state": "online", "last_sync": time.time(), "error": ""}
                if task["state"] == "removed":
                    return task
                if torrent := self._find(task, torrents):
                    return self._observe(task, torrent)
                if task["submission_state"] in ("accepted", "uncertain", "submitting", "confirmed"):
                    # 已确认后从列表消失，也保留历史，避免无意重新下载。
                    return self.storage.update_download(task["id"], state="missing" if task["submission_state"] == "confirmed" else "awaiting_confirmation",
                                                        last_sync=time.time(), sync_error="")
            except Exception as exc:
                message = self._failure(exc)
                self.storage.update_download(task["id"], sync_error=message)
                raise DownloadError(message, 401 if isinstance(exc, qbittorrentapi.LoginFailed) else 503) from exc

            task = self.storage.update_download(task["id"], event="submitted", submission_state="submitting",
                                                submitted_at=time.time(), state="awaiting_confirmation", error="")
            try:
                options = {"urls": task["magnet"], "save_path": task.get("requested_save_path") or self.config["download"]["save_path"], "use_auto_torrent_management": False}
                if task.get("requested_temp_path"):
                    options.update(download_path=task["requested_temp_path"], use_download_path=True)
                response = client.torrents_add(**options)
            except Exception as exc:
                message = self._failure(exc)
                self.storage.update_download(task["id"], event="uncertain", submission_state="uncertain", sync_error=message,
                                             error="提交结果待核实；不会自动重新提交")
                raise DownloadError("下载请求结果待核实，稍后查看同步状态，系统不会重复提交", 503) from exc
            submission = add_response_state(response)
            # API 接受请求与下载任务已存在分别确认；未知响应也先查任务列表。
            task = self.storage.update_download(task["id"], submission_state=submission,
                                               error="", sync_error="")
            try:
                torrents = list(client.torrents_info())
                if torrent := self._find(task, torrents):
                    return self._observe(task, torrent)
            except Exception as exc:
                self.storage.update_download(task["id"], sync_error=self._failure(exc))
                task = self.storage.download(task["id"])
            if submission == "failed":
                self.storage.update_download(task["id"], event="rejected", state="error",
                                             error="qBittorrent 未接受任务")
                raise DownloadError("qBittorrent 未接受下载任务")
            if submission == "uncertain":
                self.storage.update_download(task["id"], event="uncertain",
                                             error="返回格式无法确认，等待同步核实；不会自动重新提交")
                raise DownloadError("提交结果待核实，正在查询下载器任务状态，不会重复提交", 503)
            return task

    def sync_once(self):
        with self.lock:
            tasks = self.storage.download_tasks(self.downloader_key)
            if not tasks:
                return True  # 未有下载记录时不无意义地频繁登录。
            try:
                torrents = list(self._client().torrents_info())
                now = time.time()
                index = {}
                for torrent in torrents:
                    for identity in identities(torrent):
                        index[identity] = torrent
                    if torrent.get("hash"):
                        index[str(torrent["hash"]).lower()] = torrent
                for task in tasks:
                    if task["state"] == "removed":
                        continue
                    torrent = index.get(task["identity"]) or index.get(task["qbt_hash"])
                    if torrent:
                        self._observe(task, torrent)
                    elif task["submission_state"] not in ("new", "failed"):
                        self.storage.update_download(task["id"], state="missing" if task["submission_state"] == "confirmed" else "awaiting_confirmation",
                                                     last_sync=now, sync_error="")
                self.health = {"state": "online", "last_sync": now, "error": ""}
                return True
            except Exception as exc:
                message = self._failure(exc)
                logger.warning("下载状态同步失败: %s", type(exc).__name__)
                for task in tasks:
                    self.storage.update_download(task["id"], sync_error=message)
                return False

    def control(self, task_id, action, delete_files=False, affect_shared=False):
        if action not in {"pause", "resume", "remove", "sync"}:
            raise ValueError("无效的下载操作")
        with self.lock:
            task = self.storage.download(task_id)
            if task is None or task["downloader_key"] != self.downloader_key:
                raise KeyError("当前下载器没有此任务")
            refs = self.storage.download_references(task_id)
            if len(refs) > 1 and action == "remove":
                raise ValueError("此任务被多个方案引用，请保留共享任务；不能在单个方案中移除或删除它")
            if len(refs) > 1 and action in {"pause", "resume"} and not affect_shared:
                raise ValueError("此操作影响多个方案，须明确确认共享范围")
            try:
                client = self._client()
                torrent = self._find(task, list(client.torrents_info()))
                if not torrent:
                    if action == "sync":
                        self.sync_once()
                        return self.storage.download(task_id)
                    raise DownloadError("下载器中不存在此任务，保留历史记录", 404)
                if action == "sync":
                    return self._observe(task, torrent)
                torrent_hash = str(torrent["hash"])
                if action == "pause":
                    client.torrents_stop(torrent_hashes=torrent_hash)
                elif action == "resume":
                    client.torrents_start(torrent_hashes=torrent_hash)
                else:
                    client.torrents_delete(torrent_hashes=torrent_hash, delete_files=delete_files)
                    return self.storage.update_download(task_id, event="removed_with_files" if delete_files else "removed", state="removed", speed=0)
                self.storage.update_download(task_id, event=action)
                self.sync_once()
                return self.storage.download(task_id)
            except DownloadError:
                raise
            except Exception as exc:
                raise DownloadError(self._failure(exc), 503) from exc

    async def start(self):
        self.sync_task = asyncio.create_task(self._sync_loop())

    async def _sync_loop(self):
        delay = self.interval
        while True:
            # shield 保证退出时能等到同步写事务结束。
            work = asyncio.create_task(asyncio.to_thread(self.sync_once))
            try:
                success = await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise
            delay = self.interval if success else min(delay * 2, 120)
            await asyncio.sleep(delay)

    async def stop(self):
        if self.sync_task:
            self.sync_task.cancel()
            await asyncio.gather(self.sync_task, return_exceptions=True)
