"""
Movie Downloader Assist - MVP Backend
搜索来源: The Pirate Bay API + BitSearch API (均抗封锁，支持中英文)
下载: qBittorrent Web API
"""

import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import quote

import httpx
import qbittorrentapi
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── 初始化 ──────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

config = json.loads(Path("config.json").read_text(encoding="utf-8"))
app = FastAPI(title="Movie Downloader Assist")

# 常用 Tracker 提速
TRACKERS = (
    "&tr=udp://open.demonii.com:1337/announce"
    "&tr=udp://tracker.openbittorrent.com:80"
    "&tr=udp://tracker.opentrackr.org:1337/announce"
)

# ── 数据模型 ────────────────────────────────────
class DownloadRequest(BaseModel):
    magnet: str
    title: str = ""

# ── 搜索接口 ────────────────────────────────────
@app.get("/api/search")
async def search(q: str):
    """
    搜索种子资源
    并行请求 apibay 和 bitsearch，合并结果
    """
    results = []
    
    async def fetch_apibay(client):
        try:
            resp = await client.get(
                "https://apibay.org/q.php",
                params={"q": q},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = resp.json()
            if isinstance(data, list):
                for t in data:
                    if t.get('id') == '0':
                        continue
                    seeders = int(t.get('seeders', 0))
                    if seeders == 0:
                        continue
                    title = t.get('name', '')
                    magnet = f"magnet:?xt=urn:btih:{t.get('info_hash')}&dn={quote(title)}{TRACKERS}"
                    results.append({
                        "source": "TPB",
                        "title": title,
                        "quality": _parse_quality(title),
                        "seeds": seeders,
                        "size": _format_size(int(t.get('size', 0))),
                        "magnet": magnet,
                    })
        except Exception as e:
            logger.warning(f"Apibay 搜索失败: {e}")

    async def fetch_bitsearch(client):
        try:
            resp = await client.get(
                "https://bitsearch.to/api/v1/search",
                params={"q": q},
                headers={"User-Agent": "Mozilla/5.0"}
            )
            data = resp.json()
            for t in data.get('results', []):
                seeders = int(t.get('seeders', 0))
                if seeders == 0:
                    continue
                title = t.get('title', '')
                magnet = f"magnet:?xt=urn:btih:{t.get('infohash')}&dn={quote(title)}{TRACKERS}"
                results.append({
                    "source": "BitSearch",
                    "title": title,
                    "quality": _parse_quality(title),
                    "seeds": seeders,
                    "size": _format_size(int(t.get('size', 0))),
                    "magnet": magnet,
                })
        except Exception as e:
            logger.warning(f"BitSearch 搜索失败: {e}")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        # 并发请求两个 API
        await asyncio.gather(
            fetch_apibay(client),
            fetch_bitsearch(client)
        )

    # 去重 (基于 magnet 中的 infohash)
    seen_hashes = set()
    unique_results = []
    for r in results:
        # 提取 urn:btih: 后的 hash
        hash_start = r['magnet'].find('urn:btih:') + 9
        hash_val = r['magnet'][hash_start:hash_start+40].lower()
        if hash_val not in seen_hashes:
            seen_hashes.add(hash_val)
            unique_results.append(r)

    # 按做种数排序，最多返回 40 条
    unique_results.sort(key=lambda x: x["seeds"], reverse=True)
    unique_results = unique_results[:40]
    
    return {"results": unique_results, "total": len(unique_results)}


# ── 下载接口 ────────────────────────────────────
@app.post("/api/download")
async def download(req: DownloadRequest):
    if not req.magnet:
        raise HTTPException(status_code=400, detail="magnet 不能为空")

    qbt_cfg = config["qbittorrent"]
    try:
        client = qbittorrentapi.Client(
            host=qbt_cfg["host"],
            port=qbt_cfg["port"],
            username=qbt_cfg["username"],
            password=qbt_cfg["password"],
        )
        client.auth_log_in()
        client.torrents_add(
            urls=req.magnet,
            save_path=config["download"]["save_path"],
        )
        logger.info(f"已添加下载: {req.title}")
        return {"ok": True, "message": f"已添加到 qBittorrent: {req.title}"}
    except Exception as e:
        logger.error(f"添加下载失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── 工具函数 ────────────────────────────────────
def _parse_quality(title: str) -> str:
    title = title.lower()
    if any(q in title for q in ("2160p", "4k")): return "4K"
    if "1080p" in title: return "1080p"
    if "720p" in title: return "720p"
    return "未知"

def _format_size(size_bytes: int) -> str:
    if not size_bytes: return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024: return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

app.mount("/", StaticFiles(directory="static", html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    cfg = config["server"]
    uvicorn.run("main:app", host=cfg["host"], port=cfg["port"], reload=False)
