from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import yt_dlp
from typing import Optional
import asyncio
import json
import threading
import uuid
import time
from pathlib import Path

app = FastAPI(title="TonTube")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)


def _find_ffmpeg() -> Optional[str]:
    import shutil
    if path := shutil.which("ffmpeg"):
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


FFMPEG_PATH = _find_ffmpeg()

# Clients tentados em paralelo — retorna o primeiro que responder
PLAYER_CLIENTS = [["android_vr"], ["android"], ["android", "web"]]


class AnalyzeRequest(BaseModel):
    url: str


def cleanup_old_files():
    cutoff = time.time() - 3600
    for f in DOWNLOADS_DIR.iterdir():
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except Exception:
            pass


def _try_extract(url: str, clients: list) -> dict:
    """Executa em thread separada — tenta um client específico."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 12,
        "extractor_args": {"youtube": {"player_client": clients}},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


async def _extract_parallel(url: str) -> dict:
    """Dispara todos os clients em paralelo e retorna o primeiro que funcionar."""
    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    async def worker(clients):
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, _try_extract, url, clients),
                timeout=15.0,
            )
            await queue.put(("ok", result))
        except Exception as e:
            await queue.put(("err", e))

    tasks = [asyncio.create_task(worker(c)) for c in PLAYER_CLIENTS]
    errors = []

    try:
        for _ in range(len(PLAYER_CLIENTS)):
            status, value = await asyncio.wait_for(queue.get(), timeout=18.0)
            if status == "ok":
                return value
            errors.append(value)
    finally:
        for t in tasks:
            t.cancel()

    last = errors[-1] if errors else Exception("Todos os clients falharam")
    raise last


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        info = await _extract_parallel(req.url)
    except Exception as e:
        msg = str(e).lower()
        if "private" in msg or "unavailable" in msg or "removed" in msg:
            raise HTTPException(400, "Vídeo privado ou indisponível.")
        if "sign in" in msg or "login" in msg:
            raise HTTPException(400, "Este vídeo requer login no YouTube.")
        raise HTTPException(400, "Não foi possível analisar. Verifique o link e tente novamente.")

    available_heights = set()
    for f in info.get("formats", []):
        h = f.get("height")
        if h and f.get("vcodec", "none") not in ("none", None) and f.get("url"):
            available_heights.add(h)

    # Se nenhum formato com URL, mostra todas as resoluções padrão mesmo assim
    if not available_heights:
        available_heights = {360, 480, 720, 1080}

    formats = []
    height_meta = {
        2160: ("4K Ultra HD", None),
        1440: ("2K QHD", None),
        1080: ("1080p Full HD", "HD"),
        720: ("720p HD", "HD"),
        480: ("480p", None),
        360: ("360p", None),
    }
    for height, (label, badge) in height_meta.items():
        if any(ah >= height for ah in available_heights):
            formats.append({
                "id": f"video_{height}",
                "label": label,
                "type": "video",
                "ext": "mp4",
                "format_string": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]",
                "badge": badge,
                "popular": height == 1080,
            })

    formats += [
        {
            "id": "audio_mp3",
            "label": "MP3",
            "sublabel": "320 kbps",
            "type": "audio",
            "ext": "mp3",
            "format_string": "bestaudio/best",
            "badge": "Pop",
            "popular": False,
        },
        {
            "id": "audio_m4a",
            "label": "M4A",
            "sublabel": "Alta qualidade",
            "type": "audio",
            "ext": "m4a",
            "format_string": "bestaudio/best",
            "badge": None,
            "popular": False,
        },
    ]

    duration = info.get("duration", 0) or 0
    duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else ""

    return {
        "title": info.get("title", "Vídeo"),
        "thumbnail": info.get("thumbnail", ""),
        "duration": duration_str,
        "channel": info.get("uploader", ""),
        "view_count": info.get("view_count", 0),
        "formats": formats,
    }


@app.get("/api/download")
async def download(
    url: str = Query(...),
    format_string: str = Query(...),
    ext: str = Query(...),
    media_type: str = Query(...),
):
    cleanup_old_files()
    job_id = str(uuid.uuid4())
    output_template = str(DOWNLOADS_DIR / f"{job_id}.%(ext)s")

    async def stream():
        state = {"pct": 0, "speed": 0, "phase": "baixando"}
        done_event = threading.Event()
        error_holder: list = [None]

        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                dl = d.get("downloaded_bytes", 0)
                if total > 0:
                    state["pct"] = min(int(dl / total * 85), 85)
                if d.get("speed"):
                    state["speed"] = d["speed"]
                state["phase"] = "baixando"
            elif d["status"] == "finished":
                state["pct"] = 90
                state["phase"] = "convertendo"

        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
            "outtmpl": output_template,
            "socket_timeout": 30,
            "extractor_args": {"youtube": {"player_client": ["android", "android_vr"]}},
            **({"ffmpeg_location": FFMPEG_PATH} if FFMPEG_PATH else {}),
        }

        if media_type == "audio":
            opts["format"] = "bestaudio/best"
            codec = "mp3" if ext == "mp3" else "m4a"
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": "320" if ext == "mp3" else "0",
            }]
        else:
            opts["format"] = format_string
            opts["merge_output_format"] = "mp4"

        def run():
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                error_holder[0] = str(e)
            finally:
                done_event.set()

        threading.Thread(target=run, daemon=True).start()

        while not done_event.is_set():
            yield f"data: {json.dumps({'progress': state['pct'], 'speed': state['speed'], 'phase': state['phase']})}\n\n"
            await asyncio.sleep(0.5)

        if error_holder[0]:
            err = error_holder[0]
            msg = "Formato indisponível. Tente qualidade menor." if "requested format" in err.lower() else "Falha no download. Tente novamente."
            yield f"data: {json.dumps({'error': msg})}\n\n"
            return

        file_path = next((f for f in DOWNLOADS_DIR.iterdir() if f.stem == job_id), None)
        if not file_path:
            yield f"data: {json.dumps({'error': 'Arquivo não gerado. Tente novamente.'})}\n\n"
            return

        yield f"data: {json.dumps({'progress': 100, 'done': True, 'file_id': job_id})}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/file/{file_id}")
async def get_file(file_id: str):
    f = next((f for f in DOWNLOADS_DIR.iterdir() if f.stem == file_id), None)
    if not f:
        raise HTTPException(404, "Arquivo expirado ou não encontrado.")
    return FileResponse(f, filename=f.name, media_type="application/octet-stream")


@app.get("/health")
async def health():
    return {"status": "ok", "ffmpeg": bool(FFMPEG_PATH)}


app.mount("/", StaticFiles(directory="static", html=True), name="static")
