"""HTTP routes for the YouTube-to-shorts flow: sources, moments, previews and clip jobs."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .. import config
from ..analyze import service
from ..analyze.schemas import AnalyzeOptions
from ..errors import IngestError
from ..ingest import upload as upload_mod
from ..ingest.guard import canonical_url, source_id_for, youtube_video_id
from ..ingest.source import Source
from ..jobs.runner import JobRunner
from ..pipeline import clips as clips_mod
from ..pipeline import source as source_pipeline

MODEL_HEADER = "x-gemini-model"
KEY_HELP = (
    "Add GEMINI_API_KEY=your-key to the .env file next to docker-compose.yml, then restart "
    "Snipmint (docker compose up -d). Get a key at https://aistudio.google.com/apikey"
)
_CLIP_FILE = re.compile(r"^(clip_\d{2}\.(mp4|srt|ass)|titles\.json|clips\.zip)$")


def gemini_model_from(request: Request) -> str | None:
    model = (request.headers.get(MODEL_HEADER) or "").strip()
    return model if re.fullmatch(r"[A-Za-z0-9._-]{3,80}", model) else None


def gemini_key_from(request: Request) -> str | None:  # noqa: ARG001 - kept for one call shape
    """The Gemini key comes only from the server's environment (.env), never from the browser,
    so it is never typed into a page, stored in browser storage or sent over the network."""
    return (os.environ.get("GEMINI_API_KEY") or "").strip() or None


def ai_status() -> dict[str, bool]:
    avail = service.available_providers()
    return {
        "gemini_installed": service._gemini_installed(),
        "gemini_server_key": bool((os.environ.get("GEMINI_API_KEY") or "").strip()),
        "local_llm": avail["local_llm"],
    }


class SourceRequest(BaseModel):
    url: str
    analysis: AnalyzeOptions = Field(default_factory=AnalyzeOptions)


class SourceCreated(BaseModel):
    source_id: str
    task_id: str


class TaskCreated(BaseModel):
    task_id: str


class CheckKeyResponse(BaseModel):
    ok: bool
    error: str | None = None


def _source(source_id: str) -> Source:
    src = Source.existing(source_id)
    if src is None:
        raise HTTPException(404, "That video isn't known here. Paste the link again.")
    return src


def source_state(src: Source) -> dict[str, Any]:
    meta = src.meta
    status = src.status
    latest = service.latest(src)
    return {
        "source_id": src.id,
        "meta": meta.model_dump() if meta else None,
        "vertical": bool(meta and meta.vertical),
        "status": status.model_dump(),
        "has_video": src.video.is_file(),
        "has_transcript": src.file("transcript.json").is_file(),
        "analysis": latest.model_dump() if latest else None,
    }


def register(app: FastAPI, runner: JobRunner) -> None:
    @app.post("/api/sources", response_model=SourceCreated, tags=["sources"])
    def create_source(req: SourceRequest, request: Request) -> SourceCreated:
        vid = youtube_video_id(req.url)  # raises IngestError -> 400 with a friendly message
        src = Source(source_id_for(vid))
        return _start_prepare(src, canonical_url(vid), req.analysis, request)

    def _start_prepare(src: Source, url: str, analysis: AnalyzeOptions, request: Request) -> SourceCreated:
        key, model = gemini_key_from(request), gemini_model_from(request)

        def run(progress, cancel):
            return source_pipeline.prepare_source(src, url, analysis, gemini_key=key, gemini_model=model,
                                                  progress=progress, cancel=cancel)  # fmt: skip

        job = runner.submit_task("source", run)
        src.update_status(lambda st: setattr(st, "task_id", job.id))
        return SourceCreated(source_id=src.id, task_id=job.id)

    @app.post("/api/sources/upload", response_model=SourceCreated, tags=["sources"])
    async def upload_source(request: Request, file: UploadFile = File(...),
                            analysis: str = Form("{}")) -> SourceCreated:  # fmt: skip
        """A video file from this computer instead of a YouTube link."""
        try:
            opts = AnalyzeOptions.model_validate(json.loads(analysis or "{}"))
        except ValueError as exc:
            raise HTTPException(422, f"Bad analysis options: {exc}") from exc
        limit = upload_mod.max_upload_bytes()
        tmp = config.sources_dir() / f".upload-{secrets.token_hex(6)}{Path(file.filename or '').suffix.lower()}"
        size = 0
        try:
            with tmp.open("wb") as out:
                while chunk := await file.read(4 * 1024 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise IngestError(f"That file is larger than {limit // (1024 * 1024)} MB (MAX_UPLOAD_MB).")
                    out.write(chunk)
            src = await asyncio.to_thread(upload_mod.import_file, tmp, file.filename or "video.mp4")
        finally:
            tmp.unlink(missing_ok=True)
        meta = src.meta
        limit_min = source_pipeline.max_source_minutes()
        if meta and meta.duration > limit_min * 60:
            raise IngestError(f"This video is {meta.duration / 60:.0f} minutes long; the limit is "
                              f"{limit_min:.0f} minutes (MAX_SOURCE_MINUTES).")  # fmt: skip
        return _start_prepare(src, meta.url if meta else "", opts, request)

    @app.post("/api/sources/{source_id}/prepare", response_model=SourceCreated, tags=["sources"])
    def prepare_again(source_id: str, opts: AnalyzeOptions, request: Request) -> SourceCreated:
        """Continue or retry preparing a source (finished steps are kept)."""
        src = _source(source_id)
        meta = src.meta
        url = meta.url if meta else ""
        if not url and src.id.startswith("yt-"):
            url = canonical_url(src.id[3:])
        if not url:
            raise HTTPException(409, "Upload the file again.")
        return _start_prepare(src, url, opts, request)

    @app.get("/api/sources/{source_id}", tags=["sources"])
    def get_source(source_id: str) -> dict[str, Any]:
        src = _source(source_id)
        state = source_state(src)
        task = runner.get(state["status"].get("task_id") or "")
        state["task"] = task.public() if task else None
        return state

    @app.post("/api/sources/{source_id}/analyze", response_model=TaskCreated, tags=["sources"])
    def analyze_source(source_id: str, opts: AnalyzeOptions, request: Request) -> TaskCreated:
        src = _source(source_id)
        if not src.file("transcript.json").is_file():
            raise HTTPException(409, "This video is still being prepared.")
        key, model = gemini_key_from(request), gemini_model_from(request)

        def run(progress, cancel):
            return source_pipeline.reanalyze(src, opts, gemini_key=key, gemini_model=model, progress=progress)

        job = runner.submit_task("analyze", run)
        return TaskCreated(task_id=job.id)

    @app.get("/api/sources/{source_id}/video", tags=["sources"])
    def source_video(source_id: str) -> FileResponse:
        src = _source(source_id)
        if not src.video.is_file():
            raise HTTPException(404, "The video hasn't been downloaded yet.")
        return FileResponse(src.video, media_type="video/mp4")

    @app.post("/api/clips/preview", tags=["clips"])
    def clip_preview(req: clips_mod.ClipPreviewRequest) -> Response:
        return Response(clips_mod.preview_clip_frame(req), media_type="image/jpeg")

    @app.post("/api/clips", tags=["clips"])
    def create_clips(req: clips_mod.ClipsRequest) -> dict[str, str]:
        src = _source(req.source_id)
        meta = src.meta
        if not src.video.is_file() and (meta is None or meta.url.startswith("file:")):
            raise HTTPException(409, "The video was removed to save space. Upload it again.")
        # a link whose video was removed after the last clips is downloaded again inside the job
        from ..captions.styles import resolve_style

        resolve_style(req.captions.preset, req.captions.overrides, req.captions.font)  # fail fast

        def run(ws, progress, cancel):
            return clips_mod.make_clips(req, ws, progress=progress, cancel=cancel)

        job = runner.submit_in_workspace("clips", run, inputs=req.model_dump())
        return {"job_id": job.id}

    @app.get("/api/clips/{job_id}/files/{name}", tags=["clips"])
    def clip_file(job_id: str, name: str, download: bool = False) -> FileResponse:
        if not _CLIP_FILE.match(name):
            raise HTTPException(404, "No such file.")
        folder = runner.workspace_for(job_id)
        if folder is None:
            raise HTTPException(404, "That job doesn't exist.")
        path = folder / name
        if name == "clips.zip":
            path = clips_mod.build_zip(folder)
        if not path.is_file():
            raise HTTPException(404, "That file isn't available (yet).")
        media = {"mp4": "video/mp4", "srt": "application/x-subrip", "ass": "text/plain",
                 "json": "application/json", "zip": "application/zip"}[name.rsplit(".", 1)[1]]  # fmt: skip
        return FileResponse(path, media_type=media, filename=name if download else None)

    @app.post("/api/ai/check-key", response_model=CheckKeyResponse, tags=["ai"])
    def check_key(request: Request) -> CheckKeyResponse:
        """Test the server's own key (from .env)."""
        key = gemini_key_from(request)
        if not key:
            return CheckKeyResponse(ok=False, error=f"No Gemini key on the server. {KEY_HELP}")
        try:
            from ..analyze.providers import gemini
        except ImportError:
            return CheckKeyResponse(ok=False, error="Gemini support isn't installed (pip install \"snipmint[gemini]\").")
        ok, error = gemini.check_key(key)
        return CheckKeyResponse(ok=ok, error=error)
