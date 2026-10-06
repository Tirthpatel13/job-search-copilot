"""FastAPI web app: server-rendered pages (Jinja) plus a few JSON endpoints.

Run with `uvicorn jobcopilot.web.main:create_app --factory` (or `jobcopilot serve`).

Server-rendered HTML keeps the frontend dependency-free and fast on phones;
a manifest and service worker make it an installable PWA.
"""

from __future__ import annotations

import base64
import secrets
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings
from ..db import Database
from ..llm import ClaudeLLM, LLMError, StructuredLLM
from ..models import Status
from ..pipeline import RUN_LOCK, run_pipeline
from ..profile import ProfileError, load_profile, save_resume_upload
from ..scheduler import PeriodicFetcher
from ..scoring import score_job
from ..tailoring import answer_questions, draft_cover_letter, tailor_resume

HERE = Path(__file__).parent
STATIC = HERE / "static"


def create_app(
    settings: Settings | None = None,
    llm_factory: Callable[[Settings], StructuredLLM] | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings and a fake LLM factory."""
    settings = settings or Settings.from_env()
    db = Database(settings.database_path)
    make_llm = llm_factory or ClaudeLLM

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        fetcher = None
        if settings.schedule_fetch_hours > 0:
            fetcher = PeriodicFetcher(settings, db, settings.schedule_fetch_hours)
            fetcher.start()
        yield
        if fetcher:
            fetcher.stop()

    app = FastAPI(title="Job Search Copilot", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["statuses"] = [s.value for s in Status]

    # ---- optional password (HTTP Basic) for internet-exposed deployments ------

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if not settings.app_password or request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                pw = ""
            if secrets.compare_digest(pw, settings.app_password):
                return await call_next(request)
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="jobs"'})

    # ---- helpers ----------------------------------------------------------------

    def llm_or_error() -> StructuredLLM:
        try:
            return make_llm(settings)
        except LLMError as exc:
            raise HTTPException(400, str(exc)) from exc

    def job_or_404(job_id: str):
        job = db.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    def back(job_id: str, msg: str = "") -> RedirectResponse:
        url = f"/jobs/{job_id}" + (f"?msg={quote(msg)}" if msg else "")
        return RedirectResponse(url, status_code=303)

    def run_llm_action(job_id: str, action: Callable[[], Any], ok: str) -> RedirectResponse:
        try:
            action()
        except (LLMError, ProfileError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return back(job_id, f"Error: {detail}")
        return back(job_id, ok)

    # ---- pages --------------------------------------------------------------------

    @app.get("/")
    def index(
        request: Request,
        q: str = "",
        status: str = "",
        source: str = "",
        min_score: str = "",
        remote: str = "",
        sort: str = "score",
    ):
        min_score_int = int(min_score) if min_score.strip().isdigit() else None
        jobs = db.list_jobs(
            q=q,
            status=status,
            source=source,
            min_score=min_score_int,
            remote=bool(remote),
            sort=sort,
        )
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "jobs": jobs,
                "stats": db.stats(),
                "sources": db.sources(),
                "last_run": db.last_run(),
                "f": {
                    "q": q,
                    "status": status,
                    "source": source,
                    "min_score": min_score,
                    "remote": remote,
                    "sort": sort,
                },
                "claude_enabled": settings.claude_enabled,
            },
        )

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: str, msg: str = ""):
        job = job_or_404(job_id)
        return templates.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "msg": msg,
                "resume": db.latest_artifact(job_id, "resume"),
                "cover": db.latest_artifact(job_id, "cover_letter"),
                "answers": db.latest_artifact(job_id, "answers"),
                "claude_enabled": settings.claude_enabled,
            },
        )

    @app.post("/jobs/{job_id}/status")
    def set_status(job_id: str, status: str = Form(...)):
        job_or_404(job_id)
        try:
            db.set_status(job_id, Status(status))
        except ValueError as exc:
            raise HTTPException(400, "Unknown status") from exc
        return back(job_id)

    @app.post("/jobs/{job_id}/notes")
    def set_notes(job_id: str, notes: str = Form("")):
        job_or_404(job_id)
        db.set_notes(job_id, notes)
        return back(job_id, "Notes saved")

    @app.post("/jobs/{job_id}/score")
    def rescore(job_id: str):
        job = job_or_404(job_id)

        def action() -> None:
            result = score_job(llm_or_error(), load_profile(settings.profile_dir), job)
            db.save_score(job_id, result.score, result.reason, result.gaps, result.matched)

        return run_llm_action(job_id, action, "Scored")

    @app.post("/jobs/{job_id}/tailor")
    def tailor(job_id: str):
        job = job_or_404(job_id)
        return run_llm_action(
            job_id,
            lambda: tailor_resume(
                db, llm_or_error(), load_profile(settings.profile_dir), job, settings.output_dir
            ),
            "Tailored resume ready. Review it before using it.",
        )

    @app.post("/jobs/{job_id}/cover-letter")
    def cover_letter(job_id: str):
        job = job_or_404(job_id)
        return run_llm_action(
            job_id,
            lambda: draft_cover_letter(
                db, llm_or_error(), load_profile(settings.profile_dir), job, settings.output_dir
            ),
            "Cover letter drafted. Review it before using it.",
        )

    @app.post("/jobs/{job_id}/answers")
    def answers(job_id: str, questions: str = Form(...)):
        job = job_or_404(job_id)
        return run_llm_action(
            job_id,
            lambda: answer_questions(
                db,
                llm_or_error(),
                load_profile(settings.profile_dir),
                job,
                questions.splitlines(),
            ),
            "Answers drafted. Flagged items need your input.",
        )

    @app.get("/files/{job_id}/{filename}")
    def download(job_id: str, filename: str):
        base = (settings.output_dir / job_id).resolve()
        path = (base / filename).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(path, filename=filename)

    @app.get("/profile")
    def profile_page(request: Request, msg: str = ""):
        try:
            profile = load_profile(settings.profile_dir)
            error = ""
        except ProfileError as exc:
            profile, error = None, str(exc)
        return templates.TemplateResponse(
            request,
            "profile.html",
            {"profile": profile, "error": error, "msg": msg, "settings": settings},
        )

    @app.post("/profile/resume")
    async def upload_resume(file: UploadFile = File(...)):
        data = await file.read()
        try:
            save_resume_upload(settings.profile_dir, file.filename or "resume.txt", data)
        except ProfileError as exc:
            return RedirectResponse(f"/profile?msg={quote('Error: ' + str(exc))}", 303)
        except Exception:  # unparseable PDF/DOCX
            return RedirectResponse(f"/profile?msg={quote('Error: could not read that file')}", 303)
        return RedirectResponse("/profile?msg=Resume+uploaded", 303)

    # ---- fetch API ------------------------------------------------------------------

    @app.post("/api/fetch")
    def fetch_now():
        if RUN_LOCK.locked():
            return JSONResponse({"started": False, "running": True})

        def work() -> None:
            llm = None
            with suppress(LLMError):  # no API key: fetch still runs, scoring is skipped
                llm = make_llm(settings)
            run_pipeline(settings, db, llm=llm)

        threading.Thread(target=work, name="fetch-now", daemon=True).start()
        return JSONResponse({"started": True, "running": True})

    @app.get("/api/fetch/status")
    def fetch_status():
        return {"running": RUN_LOCK.locked(), "last_run": db.last_run()}

    # ---- PWA plumbing -----------------------------------------------------------

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so its scope covers the whole app.
        return FileResponse(STATIC / "sw.js", media_type="application/javascript")

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/offline")
    def offline(request: Request):
        return templates.TemplateResponse(request, "offline.html", {})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
