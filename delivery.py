"""单个后台协程整理完成资源与报告；不移动旧批次，不自动清理文件。"""

import asyncio
import logging
import os
import shutil
import threading
from pathlib import Path

from report import render_report, save_report

logger = logging.getLogger(__name__)


class DeliveryService:
    def __init__(self, storage, settings):
        self.storage = storage
        self.settings = settings
        self.options = settings.get("download", {})
        self.interval = max(2, float(self.options.get("delivery_interval_seconds", 10)))
        self.lock = threading.RLock()
        self.task = None

    def local_path(self, path):
        if not path:
            raise ValueError("未配置文件路径")
        normalized = path.replace("\\", "/").rstrip("/")
        for mapping in self.options.get("path_mappings", []):
            remote = mapping["remote"].replace("\\", "/").rstrip("/")
            # 盘符路径忽略大小写；POSIX 路径仍区分大小写。
            left, right = (normalized.casefold(), remote.casefold()) if ":" in remote else (normalized, remote)
            if left == right or left.startswith(right + "/"):
                root = Path(mapping["local"]).resolve()
                result = root.joinpath(normalized[len(remote):].lstrip("/")).resolve()
                if result != root and root not in result.parents:
                    raise ValueError("映射路径越过本地共享目录")
                return result
        result = Path(path)
        if not result.is_absolute():
            raise ValueError("此下载器路径无法在应用机器访问，请配置 path_mappings")
        return result.resolve()

    @staticmethod
    def manifest(source):
        if source.is_symlink():
            raise ValueError("资源包含符号链接，不能自动整理")
        files = [source] if source.is_file() else list(source.rglob("*"))
        if any(file.is_symlink() for file in files):
            raise ValueError("资源包含符号链接，不能自动整理")
        return [(file, file.stat()) for file in files if file.is_file()]

    @classmethod
    def copy_complete(cls, source, destination, link_id, expected_size=0):
        if source == destination:
            return
        if source in destination.parents or destination in source.parents:
            raise ValueError("整理源与目标互相包含，不能自动复制")
        manifest = cls.manifest(source)
        total = sum(stat.st_size for _, stat in manifest)
        if not manifest or (expected_size and total < expected_size):
            raise ValueError("资源文件不完整，等待下载器完成移动或检查")
        if destination.exists():
            for file, stat in manifest:
                target = destination if source.is_file() else destination / file.relative_to(source)
                if not target.is_file() or (target.stat().st_size, target.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                    raise ValueError("目标已有不同文件，请手动检查；系统不会覆盖")
            if not source.is_file() and sum(stat.st_size for _, stat in cls.manifest(destination)) != total:
                raise ValueError("目标包含额外内容，请手动检查")
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name(".partial-" + link_id)
        if source.is_dir():
            stage.mkdir(exist_ok=True)
        for file, stat in manifest:
            target = stage if source.is_file() else stage / file.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or (target.stat().st_size, target.stat().st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                shutil.copy2(file, target)
            current = file.stat()
            if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError("整理期间源文件改变，请等待下载器稳定")
        os.replace(stage, destination)

    def _deliver(self, link):
        task = link["task"]
        if link["file_state"] == "removed":
            return  # 手动删除的交付副本不能被后台重新复制。
        if link["file_state"] == "ready":
            if task["progress"] < 1 and self.local_path(link["target_path"]) == self.local_path(task["content_path"]):
                self.storage.update_delivery(link["id"], "pending", error="原下载正在重新检查或下载")
                return
            if self.local_path(link["target_path"]).exists():
                return
            self.storage.update_delivery(link["id"], "failed", error="已就绪的文件被移除，请检查或改选版本")
            return
        if not task["completed_observed_at"] or task["progress"] < 1:
            return
        if task["state"] in {"moving", "checkingDL", "checkingUP", "checkingResumeData", "downloading", "forcedDL"}:
            return
        source = self.local_path(task["content_path"])
        if not source.exists():
            raise ValueError("完成文件暂不可访问，请检查共享目录或下载器移动状态")
        expected_size = task.get("size", 0)
        files = self.manifest(source)
        if not files or (expected_size and sum(stat.st_size for _, stat in files) < expected_size):
            raise ValueError("文件大小未达到下载器资源大小，不能计为就绪")
        _, target_root = self.storage.resource_paths(link, link, task["identity"])
        root = self.local_path(target_root)
        if source == root or root in source.parents:
            destination = source
        else:
            destination = root / source.name
            self.storage.update_delivery(link["id"], "copying")
            self.copy_complete(source, destination, link["id"], expected_size)
        # 数据库存下载器路径，报告和前端不把本地映射混同远程目录。
        delivered_path = target_root if source == root else self.storage.path_join(target_root, source.name)
        if source != root and root in source.parents:
            delivered_path = self.storage.path_join(target_root, str(source.relative_to(root)))
        self.storage.update_delivery(link["id"], "ready", delivered_path)

    def remove_copy(self, link_id):
        """只删除当前批次已整理的独立副本；真实下载文件走下载器删除接口。"""
        with self.lock:
            link = self.storage.delivery_link(link_id)
            if not link:
                raise KeyError("资源关联不存在")
            if link["file_state"] not in {"ready", "removed"}:
                raise ValueError("只允许手动删除已就绪的独立副本")
            target = self.local_path(link["target_path"])
            job_root = self.local_path(self.storage.path_join(link["complete_root"], link["dir_name"]))
            original = self.local_path(link["task"]["content_path"])
            if target == job_root or job_root not in target.parents:
                raise ValueError("目标不在当前批次完成目录内，拒绝删除")
            if target == original or target in original.parents or original in target.parents:
                raise ValueError("此位置是真实下载内容，请使用下载器的移除任务操作")
            for other in self.storage.delivery_links():
                if other["id"] == link_id or other["file_state"] != "ready":
                    continue
                path = self.local_path(other["target_path"])
                if path == target or path in target.parents or target in path.parents:
                    raise ValueError("此副本也被其他条目或查询版本引用，不能单独删除")
            self.storage.update_delivery(link_id, "removed")
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            except OSError as exc:
                self.storage.update_delivery(link_id, "removed", error="删除未完成，请手动检查：" + str(exc))
                raise

    def sync_once(self):
        with self.lock:
            for link in self.storage.delivery_links():
                try:
                    self._deliver(link)
                except Exception as exc:
                    self.storage.update_delivery(link["id"], "failed", error=str(exc))
            for entry in self.storage.dirty_jobs():
                self.write_report(entry["id"], entry["report_dirty"])

    def write_report(self, job_id, generation=None):
        with self.lock:
            try:
                job = self.storage.get_job(job_id, page_size=100000, order="original")
                report = render_report(job, self.settings.get("display", {}).get("timezone", "Asia/Tokyo"))
                save_report(self.local_path(self.storage.path_join(job["complete_path"], "report.txt")), report)
                self.storage.report_result(job_id, generation if generation is not None else job["report_dirty"])
                return report
            except KeyError:
                return None
            except Exception as exc:
                self.storage.report_result(job_id, generation or 0, str(exc))
                return None

    async def start(self):
        self.task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            await asyncio.sleep(self.interval)
            work = asyncio.create_task(asyncio.to_thread(self.sync_once))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                await work
                raise

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
