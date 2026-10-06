"""SQLite 持久化：查询快照与下载记录，短事务，不依赖浏览器缓存。"""

import json
import sqlite3
import threading
import time
import uuid
import re
import ntpath
import posixpath
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from pathlib import Path


def new_id():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Storage:
    def __init__(self, path, settings=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.settings = settings or {}

    @contextmanager
    def connection(self):
        with self.lock:
            connection = sqlite3.connect(self.path, timeout=10)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            try:
                with connection:
                    yield connection
            finally:
                connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 3:
                raise RuntimeError("数据库版本高于当前程序支持版本")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                    source TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS input_rows (
                    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id),
                    line_number INTEGER NOT NULL, original_text TEXT NOT NULL,
                    query TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    started_at REAL, finished_at REAL, diagnostics TEXT NOT NULL DEFAULT '[]',
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS rows_batch ON input_rows(batch_id, line_number);
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY, downloader_key TEXT NOT NULL, identity TEXT NOT NULL,
                    magnet TEXT NOT NULL, title TEXT NOT NULL,
                    submission_state TEXT NOT NULL DEFAULT 'new', state TEXT NOT NULL DEFAULT 'unknown',
                    progress REAL NOT NULL DEFAULT 0, submitted_at REAL, confirmed_at REAL,
                    added_on REAL, completion_on REAL, completed_observed_at REAL,
                    last_sync REAL, qbt_hash TEXT NOT NULL DEFAULT '',
                    sync_error TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    events TEXT NOT NULL DEFAULT '[]', UNIQUE(downloader_key, identity)
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, row_id TEXT NOT NULL REFERENCES input_rows(id),
                    rank INTEGER NOT NULL, data TEXT NOT NULL, recommended INTEGER NOT NULL DEFAULT 1,
                    download_id TEXT REFERENCES downloads(id)
                );
                CREATE INDEX IF NOT EXISTS candidates_row ON candidates(row_id, rank);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(candidates)")}
            if "recommended" not in columns:
                db.execute("ALTER TABLE candidates ADD COLUMN recommended INTEGER NOT NULL DEFAULT 1")
            additions = {
                "batches": {"job_id": "TEXT"},
                "input_rows": {"job_item_id": "TEXT", "query_revision": "INTEGER NOT NULL DEFAULT 1"},
                "downloads": {"speed": "INTEGER NOT NULL DEFAULT 0", "eta": "INTEGER", "size": "INTEGER NOT NULL DEFAULT 0",
                              "save_path": "TEXT NOT NULL DEFAULT ''", "content_path": "TEXT NOT NULL DEFAULT ''",
                              "requested_save_path": "TEXT NOT NULL DEFAULT ''", "requested_temp_path": "TEXT NOT NULL DEFAULT ''"},
            }
            for table, fields in additions.items():
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for field, definition in fields.items():
                    if field not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {definition}")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS download_jobs (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, last_search_at REAL,
                    source TEXT NOT NULL DEFAULT 'all', temp_root TEXT NOT NULL DEFAULT '', complete_root TEXT NOT NULL DEFAULT '',
                    dir_name TEXT NOT NULL, ever_started INTEGER NOT NULL DEFAULT 0, deleted_at REAL,
                    request_key TEXT UNIQUE, report_dirty INTEGER NOT NULL DEFAULT 1,
                    report_error TEXT NOT NULL DEFAULT '', report_at REAL
                );
                CREATE TABLE IF NOT EXISTS job_items (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES download_jobs(id),
                    ordinal INTEGER NOT NULL, original_text TEXT NOT NULL, query TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1, latest_row_id TEXT REFERENCES input_rows(id),
                    adopted_link_id TEXT, deleted_at REAL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS items_job ON job_items(job_id, ordinal);
                CREATE TABLE IF NOT EXISTS job_item_downloads (
                    id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES job_items(id),
                    download_id TEXT NOT NULL REFERENCES downloads(id), candidate_id TEXT REFERENCES candidates(id),
                    query TEXT NOT NULL, revision INTEGER NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
                    file_state TEXT NOT NULL DEFAULT 'pending', target_path TEXT NOT NULL DEFAULT '',
                    file_error TEXT NOT NULL DEFAULT '', delivered_at REAL,
                    UNIQUE(item_id, download_id, revision)
                );
                CREATE INDEX IF NOT EXISTS links_download ON job_item_downloads(download_id);
                CREATE INDEX IF NOT EXISTS snapshots_item ON input_rows(job_item_id, finished_at);
            """)
            # 每个旧快照成为一个可维护方案，候选 ID 与真实下载 ID 保持不变。
            for batch in db.execute("SELECT * FROM batches WHERE job_id IS NULL").fetchall():
                job_id = batch["id"]
                self._insert_job(db, job_id, batch["created_at"], batch["source"], last_search_at=batch["created_at"])
                db.execute("UPDATE batches SET job_id=? WHERE id=?", (job_id, batch["id"]))
                for row in db.execute("SELECT * FROM input_rows WHERE batch_id=? ORDER BY line_number", (batch["id"],)).fetchall():
                    item_id = new_id()
                    db.execute("INSERT INTO job_items(id,job_id,ordinal,original_text,query,latest_row_id,created_at) VALUES(?,?,?,?,?,?,?)",
                               (item_id, job_id, row["line_number"], row["original_text"], row["query"], row["id"], batch["created_at"]))
                    db.execute("UPDATE input_rows SET job_item_id=? WHERE id=?", (item_id, row["id"]))
                    for candidate in db.execute("SELECT * FROM candidates WHERE row_id=? AND download_id IS NOT NULL", (row["id"],)).fetchall():
                        self._link_download(db, item_id, candidate, self.read_download(db, candidate["download_id"]))
            db.execute("PRAGMA user_version=3")

    def recover(self):
        """只标记中断，不在重启时自动重复搜索或重新提交下载。"""
        with self.connection() as db:
            db.execute("UPDATE input_rows SET state='interrupted', error=?, finished_at=? "
                       "WHERE state IN ('pending','searching')", ("服务停止导致搜索中断，请重新查询", time.time()))
            db.execute("UPDATE downloads SET submission_state='uncertain' WHERE submission_state='submitting'")

    def create_batch(self, lines, source="all", kind="search", job_rows=None):
        batch_id = new_id()
        with self.connection() as db:
            now = time.time()
            if job_rows is None:
                job_id = batch_id
                self._insert_job(db, job_id, now, source, last_search_at=now)
                job_rows = []
                for number, text in lines:
                    item_id = new_id()
                    db.execute("INSERT INTO job_items(id,job_id,ordinal,original_text,query,created_at) VALUES(?,?,?,?,?,?)",
                               (item_id, job_id, number, text, text.strip().lstrip("\ufeff"), now))
                    job_rows.append((item_id, 1))
            else:
                item = self._require_item(db, job_rows[0][0])
                job_id = item["job_id"]
            db.execute("INSERT INTO batches(id,created_at,source,kind,job_id) VALUES (?,?,?,?,?)", (batch_id, now, source, kind, job_id))
            for (number, text), (item_id, revision) in zip(lines, job_rows):
                item = self._require_item(db, item_id)
                if item["job_id"] != job_id or item["revision"] != revision:
                    raise ValueError("关键词已修改，请重新检索")
                row_id = new_id()
                db.execute("INSERT INTO input_rows(id,batch_id,line_number,original_text,query,job_item_id,query_revision) VALUES(?,?,?,?,?,?,?)",
                           (row_id, batch_id, number, text, text.strip().lstrip("\ufeff"), item_id, revision))
                db.execute("UPDATE job_items SET latest_row_id=? WHERE id=?", (row_id, item_id))
            db.execute("UPDATE download_jobs SET last_search_at=?,source=?,report_dirty=report_dirty+1 WHERE id=?", (now, source, job_id))
        return batch_id

    def input_rows(self, batch_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM input_rows WHERE batch_id=? ORDER BY line_number", (batch_id,))]

    def start_rows(self, row_ids):
        with self.connection() as db:
            db.executemany("UPDATE input_rows SET state='searching',started_at=? WHERE id=?",
                           [(time.time(), row_id) for row_id in row_ids])

    def finish_rows(self, row_ids, results, diagnostics=None, state=None, error=""):
        state = state or ("success" if results else "empty")
        with self.connection() as db:
            for row_id in row_ids:
                source_snapshots = []
                for source in diagnostics or []:
                    snapshot = {key: value for key, value in source.items() if key != "results"}
                    if "results" in source:
                        snapshot["candidate_ids"] = []
                        for rank, result in enumerate(source["results"], 1):
                            candidate_id = new_id()
                            db.execute("INSERT INTO candidates(id,row_id,rank,data,recommended) VALUES (?,?,?,?,0)",
                                       (candidate_id, row_id, rank, encode(result)))
                            snapshot["candidate_ids"].append(candidate_id)
                    source_snapshots.append(snapshot)
                db.execute("UPDATE input_rows SET state=?,finished_at=?,diagnostics=?,error=? WHERE id=?",
                           (state, time.time(), encode(source_snapshots), error, row_id))
                for rank, result in enumerate(results, 1):
                    db.execute("INSERT INTO candidates(id,row_id,rank,data) VALUES (?,?,?,?)",
                               (new_id(), row_id, rank, encode(result)))
                db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id=(SELECT job_id FROM job_items WHERE id=(SELECT job_item_id FROM input_rows WHERE id=?))", (row_id,))

    @staticmethod
    def summary(db, batch):
        counts = {row["state"]: row["n"] for row in db.execute(
            "SELECT state,COUNT(*) n FROM input_rows WHERE batch_id=? GROUP BY state", (batch["id"],))}
        total = sum(counts.values())
        done = total - counts.get("pending", 0) - counts.get("searching", 0)
        if done < total:
            state = "running"
        elif counts.get("interrupted"):
            state = "interrupted"
        elif counts.get("failed") == total:
            state = "failed"
        elif counts.get("failed"):
            state = "partial_failed"
        else:
            state = "completed"
        return {**dict(batch), "total": total, "done": done, "state": state, "counts": counts}

    def get_batch(self, batch_id, page=1, page_size=25):
        with self.connection() as db:
            batch = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if batch is None:
                return None
            result = self.summary(db, batch)
            rows = db.execute("SELECT * FROM input_rows WHERE batch_id=? ORDER BY line_number LIMIT ? OFFSET ?",
                              (batch_id, page_size, (page - 1) * page_size)).fetchall()
            result.update(page=page, page_size=page_size, rows=[])
            for stored in rows:
                row = dict(stored)
                row["diagnostics"] = [{key: value for key, value in source.items() if key != "candidate_ids"}
                                      for source in json.loads(row["diagnostics"])]
                row["candidates"] = []
                for candidate in db.execute("SELECT * FROM candidates WHERE row_id=? AND recommended=1 ORDER BY rank", (row["id"],)):
                    item = {**json.loads(candidate["data"]), "id": candidate["id"], "rank": candidate["rank"]}
                    item["download"] = self.read_download(db, candidate["download_id"]) if candidate["download_id"] else None
                    row["candidates"].append(item)
                row["downloads"] = [self.read_download(db, item[0]) for item in db.execute(
                    "SELECT DISTINCT download_id FROM candidates WHERE row_id=? AND download_id IS NOT NULL", (row["id"],))]
                result["rows"].append(row)
            return result

    def row_details(self, batch_id, row_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM input_rows WHERE id=? AND batch_id=?", (row_id, batch_id)).fetchone()
            if row is None:
                return None
            sources = json.loads(row["diagnostics"])
            available = any("candidate_ids" in source for source in sources)
            for source in sources:
                source["results"] = []
                for candidate_id in source.pop("candidate_ids", []):
                    candidate = db.execute("SELECT * FROM candidates WHERE id=? AND row_id=?", (candidate_id, row_id)).fetchone()
                    if candidate:
                        source["results"].append({**json.loads(candidate["data"]), "id": candidate["id"],
                                                  "download": self.read_download(db, candidate["download_id"]) if candidate["download_id"] else None})
            return {"row_id": row_id, "query": row["query"], "state": row["state"], "finished_at": row["finished_at"],
                    "snapshot_available": available, "sources": sources}

    def history(self, query="", page=1, page_size=20):
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        condition = "EXISTS (SELECT 1 FROM input_rows r WHERE r.batch_id=b.id AND r.original_text LIKE ? ESCAPE '\\')"
        with self.connection() as db:
            total = db.execute(f"SELECT COUNT(*) FROM batches b WHERE {condition}", (f"%{escaped}%",)).fetchone()[0]
            batches = db.execute(f"SELECT b.* FROM batches b WHERE {condition} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                                 (f"%{escaped}%", page_size, (page - 1) * page_size)).fetchall()
            return {"items": [self.summary(db, batch) for batch in batches], "total": total,
                    "page": page, "page_size": page_size}

    def candidate(self, candidate_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            return {**dict(row), "data": json.loads(row["data"])} if row else None

    def reserve_download(self, candidate_id, downloader_key):
        with self.connection() as db:
            candidate = db.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if candidate is None:
                raise KeyError("候选记录不存在")
            snapshot = db.execute("SELECT * FROM input_rows WHERE id=?", (candidate["row_id"],)).fetchone()
            item = self._require_item(db, snapshot["job_item_id"]) if snapshot["job_item_id"] else None
            if item and item["revision"] != snapshot["query_revision"]:
                raise ValueError("候选来自旧关键词，请重新搜索当前行")
            data = json.loads(candidate["data"])
            task_id = new_id()
            db.execute("INSERT OR IGNORE INTO downloads(id,downloader_key,identity,magnet,title) VALUES (?,?,?,?,?)",
                       (task_id, downloader_key, data["info_hash"], data["magnet"], data["title"]))
            task = db.execute("SELECT * FROM downloads WHERE downloader_key=? AND identity=?",
                              (downloader_key, data["info_hash"])).fetchone()
            # 推荐栏与该行各来源详情中的同一资源共用下载记录。
            for related in db.execute("SELECT id,data FROM candidates WHERE row_id=?", (candidate["row_id"],)).fetchall():
                if json.loads(related["data"]).get("info_hash") == data["info_hash"]:
                    db.execute("UPDATE candidates SET download_id=? WHERE id=?", (task["id"], related["id"]))
            if item:
                self._link_download(db, item["id"], candidate, dict(task))
            return self.read_download(db, task["id"])

    @staticmethod
    def read_download(db, task_id):
        task = db.execute("SELECT * FROM downloads WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return None
        result = dict(task)
        result["events"] = json.loads(result["events"])
        return result

    def download(self, task_id):
        with self.connection() as db:
            return self.read_download(db, task_id)

    def download_tasks(self, downloader_key):
        with self.connection() as db:
            return [self.read_download(db, row[0]) for row in db.execute(
                "SELECT id FROM downloads WHERE downloader_key=?", (downloader_key,))]

    def update_download(self, task_id, event=None, **changes):
        allowed = {"submission_state", "state", "progress", "submitted_at", "confirmed_at", "added_on",
                   "completion_on", "completed_observed_at", "last_sync", "qbt_hash", "sync_error", "error",
                   "speed", "eta", "size", "save_path", "content_path", "requested_save_path", "requested_temp_path"}
        if changes.keys() - allowed:
            raise ValueError("无效下载字段")
        with self.connection() as db:
            task = self.read_download(db, task_id)
            if task is None:
                raise KeyError(task_id)
            if event:
                events = task["events"] + [{"type": event, "at": time.time()}]
                changes["events"] = encode(events)
            if changes:
                db.execute("UPDATE downloads SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                           (*changes.values(), task_id))
                # 高频进度只留在状态表；报告仅在重要状态变化时刷新。
                if event or any(task.get(key) != value for key, value in changes.items() if key in {"state", "submission_state", "error", "sync_error", "content_path"}):
                    db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id IN (SELECT i.job_id FROM job_items i JOIN job_item_downloads l ON l.item_id=i.id WHERE l.download_id=?)", (task_id,))
            return self.read_download(db, task_id)

    def _insert_job(self, db, job_id, created, source, name=None, temp_root=None, complete_root=None, last_search_at=None, request_key=None):
        stamp = datetime.fromtimestamp(created, ZoneInfo(self.settings.get("display", {}).get("timezone", "Asia/Tokyo"))).strftime("%Y%m%d_%H%M%S")
        defaults = self.settings.get("download", {})
        if name is not None:
            name = name.strip()
            if not name or len(name) > 200:
                raise ValueError("方案名称长度须为 1 至 200 字符")
        complete = defaults.get("complete_root", defaults.get("save_path", "")) if complete_root is None else complete_root
        temporary = defaults.get("temp_root", self.path_join(complete, "_incomplete") if complete else "") if temp_root is None else temp_root
        for root in (temporary, complete):
            if root and not (Path(root).is_absolute() or ntpath.isabs(root)):
                raise ValueError("批次目录必须为下载器上的绝对路径")
        db.execute("INSERT INTO download_jobs(id,name,created_at,last_search_at,source,temp_root,complete_root,dir_name,request_key) VALUES(?,?,?,?,?,?,?,?,?)",
                   (job_id, name or f"检索方案_{stamp}", created, last_search_at, source, temporary, complete, stamp + "-" + job_id[:8], request_key))

    @staticmethod
    def _require_job(db, job_id):
        job = db.execute("SELECT * FROM download_jobs WHERE id=? AND deleted_at IS NULL", (job_id,)).fetchone()
        if job is None:
            raise KeyError("方案不存在或已删除")
        return job

    @classmethod
    def _require_item(cls, db, item_id, allow_deleted=False):
        item = db.execute("SELECT * FROM job_items WHERE id=?" + ("" if allow_deleted else " AND deleted_at IS NULL"), (item_id,)).fetchone()
        if item is None:
            raise KeyError("关键词行不存在或已移出")
        cls._require_job(db, item["job_id"])
        return item

    @staticmethod
    def path_join(root, *parts):
        # API 路径属于下载器机器，支持 Windows 与 POSIX 下载器。
        if "\\" in root or ntpath.splitdrive(root)[0]:
            return ntpath.join(root, *parts)
        return posixpath.join(root, *(part.replace("\\", "/") for part in parts))

    @classmethod
    def resource_paths(cls, job, item, identity):
        label = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", item["original_text"]).strip(" .")[:60] or "资源"
        folder = f"{item['ordinal']:04d}-{label}"
        safe_hash = identity.replace(":", "-")
        return (cls.path_join(job["temp_root"], job["dir_name"], folder, safe_hash) if job["temp_root"] else "",
                cls.path_join(job["complete_root"], job["dir_name"], folder, safe_hash) if job["complete_root"] else "")

    def _link_download(self, db, item_id, candidate, task):
        item = db.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
        job = self._require_job(db, item["job_id"])
        data = json.loads(candidate["data"])
        link_id = new_id()
        temporary, target = self.resource_paths(job, item, task["identity"])
        db.execute("INSERT OR IGNORE INTO job_item_downloads(id,item_id,download_id,candidate_id,query,revision,source,created_at,target_path) VALUES(?,?,?,?,?,?,?,?,?)",
                   (link_id, item_id, task["id"], candidate["id"], item["query"], item["revision"], data.get("source", ""), task.get("submitted_at") or time.time(), target))
        db.execute("UPDATE download_jobs SET ever_started=1,report_dirty=report_dirty+1 WHERE id=?", (job["id"],))
        if task["submission_state"] == "new" and not task.get("requested_save_path"):
            db.execute("UPDATE downloads SET requested_save_path=?,requested_temp_path=? WHERE id=?", (target, temporary, task["id"]))

    def create_job(self, lines, source="all", name=None, temp_root=None, complete_root=None, request_key=None, max_lines=1000):
        if not lines or len(lines) > max_lines:
            raise ValueError(f"清单必须包含 1 至 {max_lines} 行关键词")
        with self.connection() as db:
            if request_key:
                existing = db.execute("SELECT id,deleted_at FROM download_jobs WHERE request_key=?", (request_key,)).fetchone()
                if existing:
                    if existing["deleted_at"]:
                        raise ValueError("该创建请求对应的方案已删除，请重新解析")
                    return existing[0]
            job_id, now = new_id(), time.time()
            self._insert_job(db, job_id, now, source, name, temp_root, complete_root, request_key=request_key)
            for ordinal, text in enumerate(lines, 1):
                db.execute("INSERT INTO job_items(id,job_id,ordinal,original_text,query,created_at) VALUES(?,?,?,?,?,?)",
                           (new_id(), job_id, ordinal, text, text.strip().lstrip("\ufeff"), now))
            return job_id

    def append_items(self, job_id, lines, max_lines=1000):
        if not lines:
            raise ValueError("请输入关键词")
        with self.connection() as db:
            self._require_job(db, job_id)
            count = db.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND deleted_at IS NULL", (job_id,)).fetchone()[0]
            if count + len(lines) > max_lines:
                raise ValueError(f"整个方案最多 {max_lines} 行，未截断输入")
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0) FROM job_items WHERE job_id=?", (job_id,)).fetchone()[0]
            for offset, text in enumerate(lines, 1):
                db.execute("INSERT INTO job_items(id,job_id,ordinal,original_text,query,created_at) VALUES(?,?,?,?,?,?)",
                           (new_id(), job_id, ordinal + offset, text, text.strip().lstrip("\ufeff"), time.time()))
            db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id=?", (job_id,))

    def patch_job(self, job_id, **changes):
        if not changes or changes.keys() - {"name", "temp_root", "complete_root"}:
            raise ValueError("无效的方案字段")
        with self.connection() as db:
            job = self._require_job(db, job_id)
            if "name" in changes:
                changes["name"] = changes["name"].strip()
                if not changes["name"] or len(changes["name"]) > 200:
                    raise ValueError("方案名称长度须为 1 至 200 字符")
            if job["ever_started"] and changes.keys() & {"temp_root", "complete_root"}:
                raise ValueError("已经添加下载，批次目录不可修改")
            for key in ("temp_root", "complete_root"):
                if key in changes and (not changes[key] or not (Path(changes[key]).is_absolute() or ntpath.isabs(changes[key]))):
                    raise ValueError("目录必须为下载器上的绝对路径")
            db.execute("UPDATE download_jobs SET " + ",".join(f"{key}=?" for key in changes) + ",report_dirty=report_dirty+1 WHERE id=?", (*changes.values(), job_id))

    def delete_job(self, job_id):
        with self.connection() as db:
            job = self._require_job(db, job_id)
            if job["ever_started"]:
                raise ValueError("方案已有下载记录（含待确认），不能删除")
            db.execute("UPDATE download_jobs SET deleted_at=? WHERE id=?", (time.time(), job_id))

    def patch_item(self, item_id, query):
        query = query.strip().lstrip("\ufeff")
        if not query or len(query) > 1000 or "\n" in query or "\r" in query:
            raise ValueError("关键词须为 1 至 1000 字符的单行文字")
        with self.connection() as db:
            item = self._require_item(db, item_id)
            if item["query"] != query:
                db.execute("UPDATE job_items SET query=?,revision=revision+1,latest_row_id=NULL,adopted_link_id=NULL WHERE id=?", (query, item_id))
                db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id=?", (item["job_id"],))

    def delete_item(self, item_id):
        with self.connection() as db:
            item = self._require_item(db, item_id)
            db.execute("UPDATE job_items SET deleted_at=? WHERE id=?", (time.time(), item_id))
            db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id=?", (item["job_id"],))

    def search_items(self, job_id, item_id=None):
        with self.connection() as db:
            job = dict(self._require_job(db, job_id))
            items = [dict(row) for row in db.execute("SELECT * FROM job_items WHERE job_id=? AND deleted_at IS NULL ORDER BY ordinal", (job_id,))
                     if not item_id or row["id"] == item_id]
            if not items:
                raise KeyError("没有可检索的关键词行")
            for item in items:
                if item["latest_row_id"]:
                    snapshot = db.execute("SELECT state FROM input_rows WHERE id=?", (item["latest_row_id"],)).fetchone()
                    if snapshot and snapshot[0] in ("pending", "searching"):
                        raise ValueError("当前行已经在检索，请等待完成")
            return job, items

    @staticmethod
    def active_download(task):
        if task["state"] in {"removed", "missing", "error", "missingFiles", "pausedDL", "stoppedDL", "pausedUP", "stoppedUP"}:
            return False
        return (not task.get("completed_observed_at") or task["progress"] < 1) and task["submission_state"] not in {"new", "failed"}

    def _item_view(self, db, stored):
        item = dict(stored)
        snapshot = db.execute("SELECT * FROM input_rows WHERE id=?", (item["latest_row_id"],)).fetchone() if item["latest_row_id"] else None
        item.update(state=snapshot["state"] if snapshot else "unsearched", error=snapshot["error"] if snapshot else "",
                    searched_at=snapshot["finished_at"] if snapshot else None, candidates=[], downloads=[], diagnostics=[])
        links = db.execute("SELECT * FROM job_item_downloads WHERE item_id=? ORDER BY created_at", (item["id"],)).fetchall()
        for link in links:
            task = self.read_download(db, link["download_id"])
            item["downloads"].append({**dict(link), "task": task, "adopted": link["id"] == item["adopted_link_id"]})
        item["downloads"].sort(key=lambda link: not self.active_download(link["task"]))
        item["active"] = any(self.active_download(link["task"]) for link in item["downloads"])
        item["satisfied"] = any(link["adopted"] and link["file_state"] == "ready" for link in item["downloads"])
        if snapshot:
            item["diagnostics"] = [{key: val for key, val in source.items() if key != "candidate_ids"} for source in json.loads(snapshot["diagnostics"])]
            for candidate in db.execute("SELECT * FROM candidates WHERE row_id=? AND recommended=1 ORDER BY rank", (snapshot["id"],)):
                data = json.loads(candidate["data"])
                linked = next((link for link in item["downloads"] if link["task"]["identity"] == data.get("info_hash")), None)
                item["candidates"].append({**data, "id": candidate["id"], "download": linked["task"] if linked else None})
        return item

    def _job_view(self, db, stored, page=1, page_size=25, order="active", filter_by="all"):
        job = dict(stored)
        views = [self._item_view(db, row) for row in db.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY ordinal", (job["id"],))]
        valid = [item for item in views if not item["deleted_at"]]
        job.update(total=len(valid), satisfied=sum(item["satisfied"] for item in valid),
                   searching=sum(item["state"] in {"pending", "searching"} for item in valid),
                   active=sum(self.active_download(link["task"]) for item in views for link in item["downloads"]),
                   retained=[item for item in views if item["deleted_at"] and item["downloads"]])
        if order == "active":
            valid.sort(key=lambda item: not item["active"])
        if filter_by == "unsatisfied":
            valid = [item for item in valid if not item["satisfied"]]
        elif filter_by == "active":
            valid = [item for item in valid if item["active"]]
        elif filter_by == "satisfied":
            valid = [item for item in valid if item["satisfied"]]
        job.update(page=page, page_size=page_size, filtered_total=len(valid), items=valid[(page-1)*page_size:page*page_size])
        job["complete_path"] = self.path_join(job["complete_root"], job["dir_name"]) if job["complete_root"] else ""
        job["temp_path"] = self.path_join(job["temp_root"], job["dir_name"]) if job["temp_root"] else ""
        return job

    def get_job(self, job_id, page=1, page_size=25, order="active", filter_by="all"):
        with self.connection() as db:
            job = self._require_job(db, job_id)
            return self._job_view(db, job, page, page_size, order, filter_by)

    def list_jobs(self, query="", page=1, page_size=30):
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.connection() as db:
            condition = "deleted_at IS NULL AND name LIKE ? ESCAPE '\\'"
            params = (f"%{escaped}%",)
            total = db.execute("SELECT COUNT(*) FROM download_jobs WHERE " + condition, params).fetchone()[0]
            rows = db.execute("SELECT * FROM download_jobs WHERE " + condition + " ORDER BY (last_search_at IS NOT NULL) DESC,last_search_at DESC,created_at DESC,id LIMIT ? OFFSET ?", (*params, page_size, (page-1)*page_size)).fetchall()
            result = []
            for row in rows:
                job = dict(row)
                job["total"] = db.execute("SELECT COUNT(*) FROM job_items WHERE job_id=? AND deleted_at IS NULL", (job["id"],)).fetchone()[0]
                result.append(job)
            return {"items": result, "total": total, "page": page, "page_size": page_size}

    def item_history(self, item_id):
        with self.connection() as db:
            self._require_item(db, item_id, allow_deleted=True)
            return [dict(row) for row in db.execute("SELECT id,batch_id,query,query_revision,state,started_at,finished_at FROM input_rows WHERE job_item_id=? ORDER BY rowid DESC", (item_id,))]

    def item_details(self, item_id, snapshot_id=None):
        with self.connection() as db:
            item = self._require_item(db, item_id, allow_deleted=True)
            snapshot = db.execute("SELECT * FROM input_rows WHERE id=? AND job_item_id=?", (snapshot_id or item["latest_row_id"], item_id)).fetchone()
            if snapshot is None:
                return {"sources": [], "snapshot_available": False, "state": "unsearched"}
            result = self.row_details(snapshot["batch_id"], snapshot["id"])
            result["query_revision"] = snapshot["query_revision"]
            links = db.execute("SELECT d.* FROM downloads d JOIN job_item_downloads l ON l.download_id=d.id WHERE l.item_id=?", (item_id,)).fetchall()
            for source in result["sources"]:
                for candidate in source["results"]:
                    task = next((row for row in links if row["identity"] == candidate.get("info_hash")), None)
                    if task:
                        candidate["download"] = self.read_download(db, task["id"])
            return result

    def item_job_id(self, item_id):
        with self.connection() as db:
            return self._require_item(db, item_id)["job_id"]

    def validate_candidate(self, item_id, candidate_id):
        with self.connection() as db:
            item = self._require_item(db, item_id)
            row = db.execute("SELECT r.job_item_id,r.query_revision FROM candidates c JOIN input_rows r ON r.id=c.row_id WHERE c.id=?", (candidate_id,)).fetchone()
            if row is None or row["job_item_id"] != item_id:
                raise KeyError("候选不属于当前关键词行")
            if row["query_revision"] != item["revision"]:
                raise ValueError("候选来自旧关键词，请重新搜索")

    def delivery_links(self):
        with self.connection() as db:
            return [{**dict(link), "task": self.read_download(db, link["download_id"])} for link in db.execute(
                "SELECT l.*,i.ordinal,i.original_text,j.temp_root,j.complete_root,j.dir_name FROM job_item_downloads l JOIN job_items i ON i.id=l.item_id JOIN download_jobs j ON j.id=i.job_id JOIN downloads d ON d.id=l.download_id WHERE j.deleted_at IS NULL ORDER BY COALESCE(d.completion_on,d.completed_observed_at,1e20),l.created_at")]

    def update_delivery(self, link_id, state, target_path=None, error=""):
        with self.connection() as db:
            link = db.execute("SELECT * FROM job_item_downloads WHERE id=?", (link_id,)).fetchone()
            if link is None:
                raise KeyError(link_id)
            target = link["target_path"] if target_path is None else target_path
            if (link["file_state"], link["target_path"], link["file_error"]) != (state, target, error):
                db.execute("UPDATE job_item_downloads SET file_state=?,target_path=?,file_error=?,delivered_at=? WHERE id=?", (state, target, error, time.time() if state == "ready" else None, link_id))
                db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id=(SELECT job_id FROM job_items WHERE id=?)", (link["item_id"],))
            item = db.execute("SELECT * FROM job_items WHERE id=?", (link["item_id"],)).fetchone()
            if state == "removed" and item["adopted_link_id"] == link_id:
                db.execute("UPDATE job_items SET adopted_link_id=NULL WHERE id=?", (item["id"],))
            if state == "ready" and not item["adopted_link_id"] and item["revision"] == link["revision"] and not item["deleted_at"]:
                db.execute("UPDATE job_items SET adopted_link_id=? WHERE id=?", (link_id, item["id"]))

    def adopt_link(self, item_id, link_id):
        with self.connection() as db:
            item = self._require_item(db, item_id)
            link = db.execute("SELECT * FROM job_item_downloads WHERE id=? AND item_id=? AND file_state='ready'", (link_id, item_id)).fetchone()
            if link is None:
                raise ValueError("只能采用当前行已就绪的资源")
            db.execute("UPDATE job_items SET adopted_link_id=? WHERE id=?", (link_id, item_id))
            db.execute("UPDATE download_jobs SET report_dirty=report_dirty+1 WHERE id=?", (item["job_id"],))

    def download_references(self, task_id):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT DISTINCT j.id,j.name FROM download_jobs j JOIN job_items i ON i.job_id=j.id JOIN job_item_downloads l ON l.item_id=i.id WHERE l.download_id=? AND j.deleted_at IS NULL", (task_id,))]

    def delivery_link(self, link_id):
        with self.connection() as db:
            row = db.execute("SELECT l.*,i.ordinal,i.original_text,j.temp_root,j.complete_root,j.dir_name FROM job_item_downloads l JOIN job_items i ON i.id=l.item_id JOIN download_jobs j ON j.id=i.job_id WHERE l.id=? AND j.deleted_at IS NULL", (link_id,)).fetchone()
            return {**dict(row), "task": self.read_download(db, row["download_id"])} if row else None

    def dirty_jobs(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,report_dirty FROM download_jobs WHERE deleted_at IS NULL AND report_dirty>0")]

    def report_result(self, job_id, generation, error=""):
        with self.connection() as db:
            db.execute("UPDATE download_jobs SET report_error=?,report_at=? WHERE id=?", (error, None if error else time.time(), job_id))
            if not error:
                db.execute("UPDATE download_jobs SET report_dirty=0 WHERE id=? AND report_dirty=?", (job_id, generation))
