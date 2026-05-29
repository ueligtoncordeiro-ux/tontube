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

# Resolve ffmpeg: prefer system PATH, fallback to imageio-ffmpeg bundle
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


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

        available_heights = set()
        for f in info.get("formats", []):
            h = f.get("height")
            vcodec = f.get("vcodec", "none")
            if h and vcodec not in ("none", None) and f.get("url"):
                available_heights.add(h)

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
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        if "unavailable" in msg.lower() or "private" in msg.lower():
            raise HTTPException(400, "Vídeo indisponível ou privado.")
        raise HTTPException(400, "Não foi possível analisar este vídeo. Verifique o link.")


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
        state = {"pct": 0, "speed": 0}
        done_event = threading.Event()
        error_holder: list = [None]

        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
                dl = d.get("downloaded_bytes", 0)
                if total > 0:
                    state["pct"] = min(int(dl / total * 88), 88)
                if d.get("speed"):
                    state["speed"] = d["speed"]
            elif d["status"] == "finished":
                state["pct"] = 95

        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
            "outtmpl": output_template,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
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
            yield f"data: {json.dumps({'progress': state['pct'], 'speed': state['speed']})}\n\n"
            await asyncio.sleep(0.5)

        if error_holder[0]:
            yield f"data: {json.dumps({'error': 'Falha no download. Tente outro formato.'})}\n\n"
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
