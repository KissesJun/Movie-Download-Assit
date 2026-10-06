"""启动入口和 HTTP 接口；搜索、持久化和下载分别调用独立模块。"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from batch import BatchService
from downloads import DownloadError, DownloadService
from delivery import DeliveryService
from report import render_report
from storage import Storage
from search_engine import EngineManager
from search_engine.search_generic import results_from_text
from search_engine.magnets import normalize_magnet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


class BatchRequest(BaseModel):
    text: str = Field(max_length=500_000)
    source: str = "all"
    sources: list[str] | None = None


class DownloadRequest(BaseModel):
    candidate_id: str | None = None
    magnet: str = ""
    title: str = ""


class ExtractRequest(BaseModel):
    text: str = Field(max_length=2_000_000)
    title: str = ""
    allow_hashes: bool = False


class JobRequest(BatchRequest):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    temp_root: str | None = None
    complete_root: str | None = None
    request_key: str | None = Field(default=None, max_length=100)


class JobPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    temp_root: str | None = None
    complete_root: str | None = None


class ItemPatch(BaseModel):
    query: str = Field(min_length=1, max_length=1000)


class SearchRequest(BaseModel):
    sources: list[str] | None = None


class CandidateRequest(BaseModel):
    candidate_id: str


class AdoptRequest(BaseModel):
    link_id: str


class ControlRequest(BaseModel):
    action: str
    delete_files: bool = False
    affect_shared: bool = False


def create_app(settings=None, db_path=None, manager=None):
    settings = settings if settings is not None else config
    database_path = db_path or BASE_DIR / "data" / "app.db"
    timezone = settings.get("display", {}).get("timezone", "Asia/Tokyo")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("显示时区无效或缺少 tzdata，请安装 requirements.txt") from exc

    @asynccontextmanager
    async def lifespan(application):
        store = Storage(database_path, settings)
        await asyncio.to_thread(store.initialize)
        await asyncio.to_thread(store.recover)
        engine_manager = manager if manager is not None else EngineManager(settings)
        batcher = BatchService(store, engine_manager, settings.get("batch", {}))
        downloader = DownloadService(store, settings)
        delivery = DeliveryService(store, settings)
        application.state.storage = store
        application.state.batch = batcher
        application.state.downloader = downloader
        application.state.manager = engine_manager
        application.state.delivery = delivery
        await batcher.start()
        await downloader.start()
        await delivery.start()
        try:
            yield
        finally:
            await batcher.stop()
            await downloader.stop()
            await delivery.stop()
            await asyncio.to_thread(store.recover)

    application = FastAPI(title="Movie Downloader Assist", lifespan=lifespan)

    @application.exception_handler(KeyError)
    async def missing_record(request, exception):
        return JSONResponse(status_code=404, content={"detail": str(exception.args[0])})

    @application.exception_handler(ValueError)
    async def invalid_action(request, exception):
        return JSONResponse(status_code=400, content={"detail": str(exception)})

    @application.exception_handler(DownloadError)
    async def downloader_error(request, exception):
        return JSONResponse(status_code=exception.status_code, content={"detail": str(exception)})

    @application.get("/api/sources")
    async def sources(request: Request):
        return request.app.state.manager.get_available_sources()

    @application.get("/api/status")
    async def status(request: Request):
        service = request.app.state.downloader
        return {"qbittorrent": service.health, "webui_url": service.public_url,
                "timezone": timezone, "max_lines": request.app.state.batch.max_lines,
                "default_paths": {"temp_root": settings.get("download", {}).get("temp_root", ""),
                                  "complete_root": settings.get("download", {}).get("complete_root", settings.get("download", {}).get("save_path", ""))}}

    def source_selection(req):
        if req.sources is not None:
            if not req.sources:
                raise ValueError("请至少选择一个搜索引擎")
            return ",".join(dict.fromkeys(req.sources))
        return getattr(req, "source", "all")

    @application.post("/api/jobs", status_code=201)
    async def create_job(req: JobRequest, request: Request):
        service = request.app.state.batch
        source = source_selection(req)
        request.app.state.manager.validate_source(source)
        lines = service.parse_lines(req.text)
        job_id = await asyncio.to_thread(service.storage.create_job, lines, source, req.name, req.temp_root,
                                         req.complete_root, req.request_key, service.max_lines)
        return {"id": job_id}

    @application.get("/api/jobs")
    async def jobs(request: Request, q: str = Query("", max_length=200), page: int = Query(1, ge=1),
                   page_size: int = Query(30, ge=1, le=100)):
        return await asyncio.to_thread(request.app.state.storage.list_jobs, q, page, page_size)

    @application.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str, request: Request, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100),
                         order: str = Query("active", pattern="^(active|original)$"), filter_by: str = Query("all", pattern="^(all|active|satisfied|unsatisfied)$")):
        return await asyncio.to_thread(request.app.state.storage.get_job, job_id, page, page_size, order, filter_by)

    @application.patch("/api/jobs/{job_id}")
    async def patch_job(job_id: str, req: JobPatch, request: Request):
        changes = req.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise ValueError("方案字段不能为 null")
        await asyncio.to_thread(request.app.state.storage.patch_job, job_id, **changes)
        return {"ok": True}

    @application.delete("/api/jobs/{job_id}")
    async def delete_job(job_id: str, request: Request):
        await asyncio.to_thread(request.app.state.storage.delete_job, job_id)
        return {"ok": True}

    @application.post("/api/jobs/{job_id}/items", status_code=201)
    async def append_items(job_id: str, req: BatchRequest, request: Request):
        service = request.app.state.batch
        await asyncio.to_thread(service.storage.append_items, job_id, service.parse_lines(req.text), service.max_lines)
        return {"ok": True}

    @application.patch("/api/job-items/{item_id}")
    async def patch_item(item_id: str, req: ItemPatch, request: Request):
        await asyncio.to_thread(request.app.state.storage.patch_item, item_id, req.query)
        return {"ok": True}

    @application.delete("/api/job-items/{item_id}")
    async def delete_item(item_id: str, request: Request):
        await asyncio.to_thread(request.app.state.storage.delete_item, item_id)
        return {"ok": True}

    async def enqueue_job(request, job_id, item_id, req):
        try:
            batch_id = await asyncio.shield(request.app.state.batch.submit_job(job_id, item_id,
                                             source_selection(req) if req.sources is not None else None))
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from exc
        return {"id": batch_id, "job_id": job_id}

    @application.post("/api/jobs/{job_id}/search", status_code=202)
    async def search_job(job_id: str, req: SearchRequest, request: Request):
        return await enqueue_job(request, job_id, None, req)

    @application.post("/api/job-items/{item_id}/search", status_code=202)
    async def search_item(item_id: str, req: SearchRequest, request: Request):
        job_id = await asyncio.to_thread(request.app.state.storage.item_job_id, item_id)
        return await enqueue_job(request, job_id, item_id, req)

    @application.get("/api/job-items/{item_id}/details")
    async def item_details(item_id: str, request: Request, snapshot_id: str | None = None):
        return await asyncio.to_thread(request.app.state.storage.item_details, item_id, snapshot_id)

    @application.get("/api/job-items/{item_id}/history")
    async def item_history(item_id: str, request: Request):
        return await asyncio.to_thread(request.app.state.storage.item_history, item_id)

    @application.post("/api/job-items/{item_id}/downloads")
    async def item_download(item_id: str, req: CandidateRequest, request: Request):
        store = request.app.state.storage
        await asyncio.to_thread(store.validate_candidate, item_id, req.candidate_id)
        task = await asyncio.to_thread(request.app.state.downloader.submit, req.candidate_id)
        return {"ok": True, "task": task}

    @application.post("/api/job-items/{item_id}/adopt")
    async def adopt(item_id: str, req: AdoptRequest, request: Request):
        await asyncio.to_thread(request.app.state.storage.adopt_link, item_id, req.link_id)
        return {"ok": True}

    @application.get("/api/downloads/{task_id}/references")
    async def download_references(task_id: str, request: Request):
        if not await asyncio.to_thread(request.app.state.storage.download, task_id):
            raise KeyError("下载任务不存在")
        return await asyncio.to_thread(request.app.state.storage.download_references, task_id)

    @application.post("/api/downloads/{task_id}/control")
    async def control_download(task_id: str, req: ControlRequest, request: Request):
        task = await asyncio.to_thread(request.app.state.downloader.control, task_id, req.action, req.delete_files, req.affect_shared)
        return {"ok": True, "task": task}

    @application.get("/api/jobs/{job_id}/report")
    async def job_report(job_id: str, request: Request):
        # 无法访问远程磁盘也允许浏览器下载真实的当前数据库报告。
        job = await asyncio.to_thread(request.app.state.storage.get_job, job_id, 1, 100000, "original")
        text = render_report(job, timezone)
        await asyncio.to_thread(request.app.state.delivery.write_report, job_id)
        return Response(text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="report.txt"'})

    @application.post("/api/deliveries/{link_id}/remove")
    async def remove_copy(link_id: str, request: Request):
        try:
            await asyncio.to_thread(request.app.state.delivery.remove_copy, link_id)
        except OSError as exc:
            raise HTTPException(409, "副本删除未完成，请查看该资源的文件错误并手动检查") from exc
        return {"ok": True}

    @application.post("/api/batches", status_code=202)
    async def create_batch(req: BatchRequest, request: Request):
        try:
            if req.sources is not None and not req.sources:
                raise ValueError("请至少选择一个搜索引擎")
            source = ",".join(dict.fromkeys(req.sources)) if req.sources is not None else req.source
            batch_id = await asyncio.shield(request.app.state.batch.submit(req.text, source))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from exc
        return {"id": batch_id}

    @application.get("/api/batches/{batch_id}")
    async def batch_detail(batch_id: str, request: Request,
                           page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=100)):
        result = await asyncio.to_thread(request.app.state.storage.get_batch, batch_id, page, page_size)
        if result is None:
            raise HTTPException(404, "批次不存在")
        return result

    @application.get("/api/history")
    async def history(request: Request, q: str = Query("", max_length=200),
                      page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
        return await asyncio.to_thread(request.app.state.storage.history, q, page, page_size)

    @application.get("/api/batches/{batch_id}/rows/{row_id}/details")
    async def search_details(batch_id: str, row_id: str, request: Request):
        result = await asyncio.to_thread(request.app.state.storage.row_details, batch_id, row_id)
        if result is None:
            raise HTTPException(404, "查询行不存在")
        return result

    @application.post("/api/batches/{batch_id}/retry", status_code=202)
    async def retry_batch(batch_id: str, request: Request, row_id: str | None = None):
        saved = await asyncio.to_thread(request.app.state.storage.get_batch, batch_id)
        if saved is None:
            raise HTTPException(404, "批次不存在")
        if saved["kind"] != "search":
            raise HTTPException(400, "提取记录请重新粘贴提取")
        rows = await asyncio.to_thread(request.app.state.storage.input_rows, batch_id)
        if row_id:
            rows = [row for row in rows if row["id"] == row_id]
        if not rows:
            raise HTTPException(404, "输入行不存在")
        try:
            new_batch_id = await request.app.state.batch.submit("\n".join(row["original_text"] for row in rows), saved["source"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from exc
        return {"id": new_batch_id}

    @application.get("/api/search")
    async def search(request: Request, q: str = Query(max_length=1000), source: str = "all"):
        if not q.strip():
            return {"results": [], "total": 0}
        try:
            batch_id = await request.app.state.batch.submit(q.replace("\n", " ").replace("\r", " "), source)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OverflowError as exc:
            raise HTTPException(429, str(exc)) from exc
        # 兼容原单次查询接口；实际仍进入全局有限队列并保存历史。
        while True:
            result = await asyncio.to_thread(request.app.state.storage.get_batch, batch_id)
            if result["state"] != "running":
                row = result["rows"][0]
                return {"results": row["candidates"], "total": len(row["candidates"]),
                        "batch_id": batch_id, "sources": row["diagnostics"], "state": row["state"]}
            await asyncio.sleep(0.1)

    async def save_extraction(store, results, title):
        batch_id = await asyncio.to_thread(store.create_batch, [(1, title or "手动磁力提取")], "manual", "extract")
        rows = await asyncio.to_thread(store.input_rows, batch_id)
        await asyncio.to_thread(store.finish_rows, [rows[0]["id"]], [item.to_dict() for item in results])
        saved = await asyncio.to_thread(store.get_batch, batch_id)
        return batch_id, saved["rows"][0]["candidates"]

    @application.post("/api/extract")
    async def extract(req: ExtractRequest, request: Request):
        results = await asyncio.to_thread(results_from_text, req.text, "手动提取", req.title, allow_hashes=req.allow_hashes)
        batch_id, saved = await save_extraction(request.app.state.storage, results, req.title)
        return {"results": saved, "total": len(saved), "batch_id": batch_id}

    @application.post("/api/download")
    async def download(req: DownloadRequest, request: Request):
        store = request.app.state.storage
        candidate_id = req.candidate_id
        batch_id = None
        if not candidate_id:
            try:
                link = normalize_magnet(req.magnet)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            results = results_from_text(link.magnet, "手动下载", req.title)
            batch_id, saved = await save_extraction(store, results, req.title)
            candidate_id = saved[0]["id"]
        try:
            task = await asyncio.to_thread(request.app.state.downloader.submit, candidate_id)
        except KeyError as exc:
            raise HTTPException(404, "候选记录不存在") from exc
        except DownloadError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc
        return {"ok": True, "message": "已确认下载任务" if task["submission_state"] == "confirmed" else "请求已接受，等待任务确认",
                "task": task, "batch_id": batch_id}

    application.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
    return application


app = create_app()

if __name__ == "__main__":
    import uvicorn
    server = config.get("server", {"host": "127.0.0.1", "port": 5000})
    uvicorn.run(app, host=server["host"], port=server["port"], workers=1)
