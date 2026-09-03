from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from backend.ai_provider import LocalAIError, chat as llama_chat, context_length, model_name, save_context_length
from backend.automation import AutoApplyError, extract_report_jobs_html, import_report_jobs, run_job_applications
from backend.browser_service import BrowserConnectionError, gmail_messages, read_gmail_message, read_linkedin_profile
from backend.credential_service import delete_protected_text, protect_text, unprotect_text
from backend.linkedin_service import evaluate_linkedin
from backend.job_search_service import JobSearchError, discover_jobs
from backend.logging_service import initialize_logs, install_exception_hooks, log_ai_interaction, log_error
from backend.system_metrics import machine_metrics

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "autoapply.db"
STATIC = ROOT / "frontend"
VIDEO_DIR = ROOT / "data" / "site-videos"
JOB_SEARCH_PATH = ROOT / "BUSCAR_VAGAS.md"

install_exception_hooks()


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


SEED_SITES = [
    ("Micro1 Talent", "https://www.talent.micro1.ai/jobs"),
    ("Backend Brasil - Vagas", "https://github.com/backend-br/vagas/issues"),
    ("Combine Global Recruitment", "https://combineglobalrecruitment.na.teamtailor.com/"),
    ("Indeed Brasil", "https://br.indeed.com/?r=us"),
    ("Kickresume", "https://www.kickresume.com/dashboard/"),
    ("PowerToFly", "https://powertofly.com/jobs/"),
    ("We Work Remotely", "https://weworkremotely.com/"),
    ("Wellfound", "https://wellfound.com/"),
    ("Remotive", "https://remotive.com/"),
    ("FlexJobs", "https://www.flexjobs.com/homevariant/t17"),
    ("Working Nomads", "https://www.workingnomads.com/jobs"),
    ("NoDesk", "https://nodesk.co/"),
    ("LinkedIn", "https://www.linkedin.com/jobs/"),
]


class SiteInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    website_url: str = Field(min_length=8, max_length=1000)
    login_method: str = "username_password"
    enabled: bool = True

    @field_validator("website_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Use uma URL completa iniciada por http:// ou https://")
        return value

    @field_validator("login_method")
    @classmethod
    def valid_login(cls, value: str) -> str:
        if value not in {"google", "linkedin", "username_password"}:
            raise ValueError("Método de login inválido")
        return value


class ApplicationInput(BaseModel):
    match: str = Field(default="UNKNOWN", max_length=80)
    company: str = Field(default="UNKNOWN", max_length=240)
    role: str = Field(default="UNKNOWN", max_length=240)
    source: str = Field(default="UNKNOWN", max_length=160)
    link: str = Field(min_length=8, max_length=2000)
    report_status: str = Field(default="UNKNOWN", max_length=160)
    application_status: str = Field(default="pending", pattern="^(pending|running|applied)$")
    details: str = Field(default="", max_length=4000)


class RunInput(BaseModel):
    email_id: str | None = None


class IdsInput(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)


class ApplicationStatusInput(IdsInput):
    status: str = Field(pattern="^(pending|applied)$")


class CredentialInput(BaseModel):
    identifier: str = Field(min_length=1, max_length=320)
    password: str = Field(default="", max_length=1000)


class LlamaConfigInput(BaseModel):
    context_length: int


class LlamaMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant|system)$")
    content: str = Field(min_length=1, max_length=8000)


class LlamaChatInput(BaseModel):
    messages: list[LlamaMessage] = Field(min_length=1, max_length=20)


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize() -> None:
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, website_url TEXT NOT NULL UNIQUE,
            login_method TEXT NOT NULL CHECK(login_method IN ('google','linkedin','username_password')),
            enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email_id TEXT, match TEXT NOT NULL DEFAULT 'UNKNOWN',
            company TEXT NOT NULL DEFAULT 'UNKNOWN', role TEXT NOT NULL DEFAULT 'UNKNOWN', source TEXT NOT NULL DEFAULT 'UNKNOWN',
            link TEXT NOT NULL UNIQUE, report_status TEXT NOT NULL DEFAULT 'UNKNOWN',
            application_status TEXT NOT NULL CHECK(application_status IN ('pending','running','applied')),
            details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS linkedin_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, profile_url TEXT NOT NULL, profile_name TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL, evaluation_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS training_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, application_id INTEGER, site TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '', file_name TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS discovered_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, match TEXT NOT NULL DEFAULT 'UNKNOWN', company TEXT NOT NULL DEFAULT 'UNKNOWN',
            role TEXT NOT NULL DEFAULT 'UNKNOWN', source TEXT NOT NULL DEFAULT 'LlamaIndex', link TEXT NOT NULL UNIQUE,
            report_status TEXT NOT NULL DEFAULT 'open', snippet TEXT NOT NULL DEFAULT '', discovered_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS site_credentials (
            site_id INTEGER PRIMARY KEY, identifier_encrypted TEXT NOT NULL, password_encrypted TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE)""")
        if db.execute("SELECT COUNT(*) FROM sites").fetchone()[0] == 0:
            now = datetime.now(UTC).isoformat()
            db.executemany(
                "INSERT INTO sites(title,website_url,login_method,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                [(title, url, "username_password", 1, now, now) for title, url in SEED_SITES],
            )
        db.execute("UPDATE sites SET login_method='linkedin' WHERE login_method='username_password' AND (lower(title) LIKE '%linkedin%' OR lower(website_url) LIKE '%linkedin.com%')")
        db.execute("UPDATE sites SET login_method='google' WHERE login_method='username_password' AND lower(title) LIKE '%gmail%'")


def row_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    if "enabled" in data:
        data["enabled"] = bool(data["enabled"])
    return data


def match_score(value: object) -> float:
    """Return the numeric part of a match label; unscored jobs sort last."""
    found = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return float(found.group().replace(",", ".")) if found else -1.0


def sort_jobs_by_match(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: match_score(row.get("match")), reverse=True)


def upsert_application(job: dict, email_id: str | None = None) -> dict:
    now = datetime.now(UTC).isoformat()
    payload = ApplicationInput(**job)
    with connect() as db:
        db.execute("""INSERT INTO applications(email_id,match,company,role,source,link,report_status,application_status,details,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(link) DO UPDATE SET email_id=excluded.email_id,match=excluded.match,
            company=excluded.company,role=excluded.role,source=excluded.source,report_status=excluded.report_status,
            application_status=excluded.application_status,details=excluded.details,updated_at=excluded.updated_at""",
            (email_id, payload.match, payload.company, payload.role, payload.source, payload.link, payload.report_status,
             payload.application_status, payload.details, now, now))
        return row_dict(db.execute("SELECT * FROM applications WHERE link=?", (payload.link,)).fetchone())


def insert_application_if_new(job: dict, email_id: str | None = None) -> dict:
    now = datetime.now(UTC).isoformat()
    payload = ApplicationInput(**job)
    with connect() as db:
        db.execute("""INSERT INTO applications(email_id,match,company,role,source,link,report_status,application_status,details,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(link) DO NOTHING""",
            (email_id, payload.match, payload.company, payload.role, payload.source, payload.link, payload.report_status,
             payload.application_status, payload.details, now, now))
        return row_dict(db.execute("SELECT * FROM applications WHERE link=?", (payload.link,)).fetchone())


def credential_for_url(url: str) -> dict | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    with connect() as db:
        rows = db.execute("""SELECT s.website_url,s.login_method,c.identifier_encrypted,c.password_encrypted
            FROM sites s JOIN site_credentials c ON c.site_id=s.id WHERE s.enabled=1""").fetchall()
    matches = [row for row in rows if host == urlparse(row["website_url"]).netloc.lower().removeprefix("www.") or host.endswith("." + urlparse(row["website_url"]).netloc.lower().removeprefix("www."))]
    if not matches:
        return None
    row = matches[0]
    return {"login_method": row["login_method"], "identifier": unprotect_text(row["identifier_encrypted"]), "password": unprotect_text(row["password_encrypted"])}


RUN_LOCK = threading.Lock()
RUN_STATE = {"status": "idle", "kind": None, "message": "Pronto para executar", "email_id": None, "current": 0, "total": 0, "applied": 0, "pending": 0, "started_at": None, "finished_at": None}


def set_run_state(**values) -> None:
    with RUN_LOCK:
        RUN_STATE.update(values)


def import_worker(email_id: str | None) -> None:
    try:
        result = import_report_jobs(email_id, insert_application_if_new, set_run_state)
        set_run_state(status="completed", message="Vagas cadastradas sem IA", finished_at=datetime.now(UTC).isoformat(), **result)
    except (AutoApplyError, BrowserConnectionError) as exc:
        log_error("autoapply_worker", exc, {"email_id": email_id})
        set_run_state(status="failed", message=str(exc), finished_at=datetime.now(UTC).isoformat())
    except Exception as exc:
        log_error("autoapply_worker_unexpected", exc, {"email_id": email_id})
        set_run_state(status="failed", message=f"Falha inesperada: {exc}", finished_at=datetime.now(UTC).isoformat())


def application_worker(ids: list[int]) -> None:
    try:
        placeholders = ",".join("?" for _ in ids)
        with connect() as db:
            rows = db.execute(f"SELECT * FROM applications WHERE id IN ({placeholders})", ids).fetchall()
        jobs_by_id = {row["id"]: row_dict(row) for row in rows}
        jobs = [jobs_by_id[item] for item in ids if item in jobs_by_id]
        if not jobs:
            raise AutoApplyError("Nenhuma vaga selecionada foi encontrada")
        result = run_job_applications(jobs, upsert_application, set_run_state, credential_for_url)
        set_run_state(status="completed", message="Aplicação com IA concluída", finished_at=datetime.now(UTC).isoformat(), **result)
    except (AutoApplyError, BrowserConnectionError) as exc:
        log_error("application_worker", exc, {"ids": ids})
        set_run_state(status="failed", message=str(exc), finished_at=datetime.now(UTC).isoformat())
    except Exception as exc:
        log_error("application_worker_unexpected", exc, {"ids": ids})
        set_run_state(status="failed", message=f"Falha inesperada: {exc}", finished_at=datetime.now(UTC).isoformat())


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_env()
    initialize_logs()
    initialize()
    warmup_messages = [
        {"role": "system", "content": "Answer in English only."},
        {"role": "user", "content": "Reply with exactly: READY"},
    ]
    try:
        warmup = llama_chat(warmup_messages, timeout=180)
        log_ai_interaction(
            "llama_startup_warmup",
            warmup["model"],
            json.dumps(warmup_messages, ensure_ascii=False),
            warmup["content"],
            {"context_length": warmup["context_length"], **warmup["metrics"]},
        )
    except Exception as exc:
        log_error("llama_startup_warmup", exc, {"context_length": context_length()})
    yield


app = FastAPI(title="AutoApply", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def unexpected_error_logger(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as exc:
        log_error("fastapi_request", exc, {"method": request.method, "path": request.url.path})
        raise


@app.get("/api/sites")
def list_sites() -> list[dict]:
    with connect() as db:
        return [row_dict(row) for row in db.execute("""SELECT s.*,
            EXISTS(SELECT 1 FROM site_credentials c WHERE c.site_id=s.id) AS credential_configured
            FROM sites s ORDER BY enabled DESC, title COLLATE NOCASE""")]


@app.get("/api/sites/{site_id}/credentials")
def get_site_credentials(site_id: int) -> dict:
    with connect() as db:
        site = db.execute("SELECT id,title,login_method FROM sites WHERE id=?", (site_id,)).fetchone()
        row = db.execute("SELECT identifier_encrypted FROM site_credentials WHERE site_id=?", (site_id,)).fetchone()
    if not site:
        raise HTTPException(404, "Site não encontrado")
    return {"site_id": site_id, "title": site["title"], "login_method": site["login_method"], "configured": bool(row), "identifier": unprotect_text(row["identifier_encrypted"]) if row else "", "password_set": bool(row)}


@app.put("/api/sites/{site_id}/credentials")
def save_site_credentials(site_id: int, payload: CredentialInput) -> dict:
    now = datetime.now(UTC).isoformat()
    with connect() as db:
        if not db.execute("SELECT 1 FROM sites WHERE id=?", (site_id,)).fetchone():
            raise HTTPException(404, "Site não encontrado")
        current = db.execute("SELECT password_encrypted FROM site_credentials WHERE site_id=?", (site_id,)).fetchone()
        if not payload.password and not current:
            raise HTTPException(422, "Informe a senha")
        password = protect_text(payload.password, f"site:{site_id}:password") if payload.password else current["password_encrypted"]
        db.execute("""INSERT INTO site_credentials(site_id,identifier_encrypted,password_encrypted,created_at,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET identifier_encrypted=excluded.identifier_encrypted,
            password_encrypted=excluded.password_encrypted,updated_at=excluded.updated_at""",
            (site_id, protect_text(payload.identifier, f"site:{site_id}:identifier"), password, now, now))
    return {"site_id": site_id, "configured": True}


@app.delete("/api/sites/{site_id}/credentials", status_code=204)
def delete_site_credentials(site_id: int) -> Response:
    with connect() as db:
        row = db.execute(
            "SELECT identifier_encrypted,password_encrypted FROM site_credentials WHERE site_id=?",
            (site_id,),
        ).fetchone()
        db.execute("DELETE FROM site_credentials WHERE site_id=?", (site_id,))
    if row:
        delete_protected_text(row["identifier_encrypted"])
        delete_protected_text(row["password_encrypted"])
    return Response(status_code=204)


@app.post("/api/sites", status_code=201)
def create_site(payload: SiteInput) -> dict:
    now = datetime.now(UTC).isoformat()
    try:
        with connect() as db:
            cursor = db.execute("INSERT INTO sites(title,website_url,login_method,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?)", (payload.title, payload.website_url, payload.login_method, int(payload.enabled), now, now))
            return row_dict(db.execute("SELECT * FROM sites WHERE id=?", (cursor.lastrowid,)).fetchone())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Este site já está cadastrado") from exc


@app.put("/api/sites/{site_id}")
def update_site(site_id: int, payload: SiteInput) -> dict:
    try:
        with connect() as db:
            result = db.execute("UPDATE sites SET title=?,website_url=?,login_method=?,enabled=?,updated_at=? WHERE id=?", (payload.title, payload.website_url, payload.login_method, int(payload.enabled), datetime.now(UTC).isoformat(), site_id))
            if result.rowcount == 0:
                raise HTTPException(404, "Site não encontrado")
            return row_dict(db.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "Este site já está cadastrado") from exc


@app.delete("/api/sites/{site_id}", status_code=204)
def delete_site(site_id: int) -> Response:
    with connect() as db:
        credentials = db.execute(
            "SELECT identifier_encrypted,password_encrypted FROM site_credentials WHERE site_id=?",
            (site_id,),
        ).fetchone()
        if db.execute("DELETE FROM sites WHERE id=?", (site_id,)).rowcount == 0:
            raise HTTPException(404, "Site não encontrado")
    if credentials:
        delete_protected_text(credentials["identifier_encrypted"])
        delete_protected_text(credentials["password_encrypted"])
    return Response(status_code=204)


@app.get("/api/applications")
def list_applications() -> list[dict]:
    with connect() as db:
        rows = [row_dict(row) for row in db.execute("SELECT * FROM applications ORDER BY updated_at DESC")]
    return sort_jobs_by_match(rows)


@app.post("/api/applications", status_code=201)
def create_application(payload: ApplicationInput) -> dict:
    return upsert_application(payload.model_dump())


@app.post("/api/applications/apply-ai", status_code=202)
def apply_applications_with_ai(payload: IdsInput) -> dict:
    ids = list(dict.fromkeys(payload.ids))
    with RUN_LOCK:
        if RUN_STATE["status"] == "running":
            raise HTTPException(409, "Já existe uma operação em execução")
        RUN_STATE.update({"status": "running", "kind": "apply", "message": "Preparando aplicação com IA...", "email_id": None, "current": 0, "total": len(ids), "applied": 0, "pending": 0, "started_at": datetime.now(UTC).isoformat(), "finished_at": None})
    threading.Thread(target=application_worker, args=(ids,), daemon=True, name="application-worker").start()
    return dict(RUN_STATE)


@app.patch("/api/applications/status")
def update_application_status(payload: ApplicationStatusInput) -> dict:
    ids = list(dict.fromkeys(payload.ids))
    placeholders = ",".join("?" for _ in ids)
    details = "Marcada manualmente como aplicada" if payload.status == "applied" else "Marcada manualmente como pendente"
    with connect() as db:
        result = db.execute(
            f"UPDATE applications SET application_status=?,details=?,updated_at=? WHERE id IN ({placeholders})",
            [payload.status, details, datetime.now(UTC).isoformat(), *ids],
        )
    return {"updated": result.rowcount}


@app.post("/api/applications/delete-batch")
def delete_applications(payload: IdsInput) -> dict:
    ids = list(dict.fromkeys(payload.ids))
    placeholders = ",".join("?" for _ in ids)
    with connect() as db:
        result = db.execute(f"DELETE FROM applications WHERE id IN ({placeholders})", ids)
    return {"deleted": result.rowcount}


@app.delete("/api/applications/{application_id}", status_code=204)
def delete_application(application_id: int) -> Response:
    with connect() as db:
        if db.execute("DELETE FROM applications WHERE id=?", (application_id,)).rowcount == 0:
            raise HTTPException(404, "Vaga não encontrada")
    return Response(status_code=204)


@app.get("/api/gmail/messages")
def list_gmail_messages() -> list[dict]:
    try:
        return gmail_messages()
    except BrowserConnectionError as exc:
        log_error("gmail_messages", exc)
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/gmail/messages/{email_id}/package")
def download_report_package(email_id: str) -> Response:
    try:
        from backend.application_package import package_zip
        jobs = extract_report_jobs_html(read_gmail_message(email_id))
        content = package_zip(jobs)
    except BrowserConnectionError as exc:
        raise HTTPException(503, str(exc)) from exc
    except AutoApplyError as exc:
        raise HTTPException(422, str(exc)) from exc
    filename = f"autoapply-package-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return Response(content=content, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/autoapply/run", status_code=202)
def start_autoapply(payload: RunInput) -> dict:
    with RUN_LOCK:
        if RUN_STATE["status"] == "running":
            raise HTTPException(409, "O AutoApply já está executando")
        RUN_STATE.update({"status": "running", "kind": "import", "message": "Lendo o HTML do AutoApply Report...", "email_id": payload.email_id, "current": 0, "total": 0, "applied": 0, "pending": 0, "started_at": datetime.now(UTC).isoformat(), "finished_at": None})
    threading.Thread(target=import_worker, args=(payload.email_id,), daemon=True, name="autoapply-import-worker").start()
    return dict(RUN_STATE)


@app.get("/api/autoapply/status")
def autoapply_status() -> dict:
    with RUN_LOCK:
        return dict(RUN_STATE)


@app.get("/api/training-videos")
def list_training_videos() -> list[dict]:
    with connect() as db:
        return [row_dict(row) for row in db.execute("SELECT * FROM training_videos ORDER BY created_at DESC")]


@app.post("/api/training-videos", status_code=201)
async def save_training_video(request: Request, application_id: int) -> dict:
    with connect() as db:
        application = db.execute("SELECT * FROM applications WHERE id=?", (application_id,)).fetchone()
    if not application:
        raise HTTPException(404, "Vaga não encontrada")
    mime_type = request.headers.get("content-type", "video/webm").split(";", 1)[0]
    if not mime_type.startswith("video/"):
        raise HTTPException(415, "Envie uma gravação de vídeo")
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    extension = ".mp4" if mime_type == "video/mp4" else ".webm"
    file_name = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:10]}{extension}"
    path = VIDEO_DIR / file_name
    size = 0
    try:
        with path.open("wb") as output:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 1_000_000_000:
                    raise HTTPException(413, "A gravação excede o limite de 1 GB")
                output.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if size == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "A gravação está vazia")
    site = urlparse(application["link"]).netloc
    now = datetime.now(UTC).isoformat()
    with connect() as db:
        cursor = db.execute("INSERT INTO training_videos(application_id,site,role,company,file_name,mime_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?)", (application_id, site, application["role"], application["company"], file_name, mime_type, size, now))
        return row_dict(db.execute("SELECT * FROM training_videos WHERE id=?", (cursor.lastrowid,)).fetchone())


@app.get("/api/training-videos/{video_id}/file")
def training_video_file(video_id: int) -> FileResponse:
    with connect() as db:
        row = db.execute("SELECT * FROM training_videos WHERE id=?", (video_id,)).fetchone()
    if not row or not (VIDEO_DIR / row["file_name"]).is_file():
        raise HTTPException(404, "Vídeo não encontrado")
    return FileResponse(VIDEO_DIR / row["file_name"], media_type=row["mime_type"], filename=row["file_name"])


@app.delete("/api/training-videos/{video_id}", status_code=204)
def delete_training_video(video_id: int) -> Response:
    with connect() as db:
        row = db.execute("SELECT file_name FROM training_videos WHERE id=?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vídeo não encontrado")
        db.execute("DELETE FROM training_videos WHERE id=?", (video_id,))
    (VIDEO_DIR / row["file_name"]).unlink(missing_ok=True)
    return Response(status_code=204)


@app.get("/api/discovery/jobs")
def list_discovered_jobs() -> list[dict]:
    with connect() as db:
        rows = db.execute("""SELECT d.* FROM discovered_jobs d
            WHERE NOT EXISTS (SELECT 1 FROM applications a WHERE a.link=d.link)
            ORDER BY d.discovered_at DESC""").fetchall()
    return sort_jobs_by_match([row_dict(row) for row in rows])


@app.post("/api/discovery/refresh")
def refresh_discovered_jobs() -> dict:
    with connect() as db:
        existing = {row[0] for row in db.execute("SELECT link FROM applications")}
        sources = [row[0] for row in db.execute("SELECT website_url FROM sites WHERE enabled=1 ORDER BY title")]
    try:
        jobs = discover_jobs(JOB_SEARCH_PATH, existing, sources)
    except JobSearchError as exc:
        raise HTTPException(503, str(exc)) from exc
    now = datetime.now(UTC).isoformat()
    with connect() as db:
        for job in jobs:
            db.execute("""INSERT INTO discovered_jobs(match,company,role,source,link,report_status,snippet,discovered_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(link) DO UPDATE SET match=excluded.match,company=excluded.company,
                role=excluded.role,source=excluded.source,report_status=excluded.report_status,snippet=excluded.snippet,
                discovered_at=excluded.discovered_at""", (job["match"], job["company"], job["role"], job["source"], job["link"], job["report_status"], job["snippet"], now))
    return {"found": len(jobs)}


@app.post("/api/discovery/register")
def register_discovered_jobs(payload: IdsInput) -> dict:
    ids = list(dict.fromkeys(payload.ids))
    placeholders = ",".join("?" for _ in ids)
    with connect() as db:
        rows = db.execute(f"SELECT * FROM discovered_jobs WHERE id IN ({placeholders})", ids).fetchall()
    for row in rows:
        insert_application_if_new({**row_dict(row), "application_status": "pending", "details": "Cadastrada pela busca LlamaIndex"})
    return {"registered": len(rows)}


@app.get("/api/linkedin/profile")
def linkedin_profile() -> dict:
    try:
        profile = read_linkedin_profile()
        return {key: profile[key] for key in ("connected", "url", "name", "headline")}
    except BrowserConnectionError as exc:
        log_error("linkedin_profile", exc)
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/linkedin/evaluate")
def linkedin_evaluate() -> dict:
    try:
        result = evaluate_linkedin()
    except BrowserConnectionError as exc:
        log_error("linkedin_evaluate_browser", exc)
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        log_error("linkedin_evaluate", exc)
        raise HTTPException(502, f"Não foi possível avaliar o LinkedIn: {exc}") from exc
    now = datetime.now(UTC).isoformat()
    with connect() as db:
        db.execute("INSERT INTO linkedin_evaluations(profile_url,profile_name,score,evaluation_json,created_at) VALUES(?,?,?,?,?)", (result["profile"]["url"], result["profile"]["name"], result["evaluation"]["score"], json.dumps(result, ensure_ascii=False), now))
    result["created_at"] = now
    return result


@app.get("/api/linkedin/evaluations/latest")
def latest_linkedin_evaluation() -> dict | None:
    with connect() as db:
        row = db.execute("SELECT evaluation_json,created_at FROM linkedin_evaluations ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    result = json.loads(row["evaluation_json"])
    result["created_at"] = row["created_at"]
    return result


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.35):
            return True
    except OSError:
        return False


@app.get("/api/status")
def status() -> dict:
    return {
        "status": "ok",
        "database": DB_PATH.exists(),
        "redis": port_open(6379),
        "chrome_automation": port_open(9222),
        "ai_provider": "ollama",
        "ollama": port_open(11434),
        "ai_model": model_name(),
        "gmail_account": os.getenv("GMAIL_ACCOUNT", ""),
    }


@app.get("/api/llama/status")
def llama_status() -> dict:
    try:
        resources = machine_metrics()
    except Exception as exc:
        log_error("llama_machine_metrics", exc)
        raise HTTPException(500, "Não foi possível ler as métricas da máquina") from exc
    return {
        "model": model_name(),
        "context_length": context_length(),
        "allowed_contexts": [512, 1024, 2048, 4096],
        "resources": resources,
    }


@app.put("/api/llama/config")
def update_llama_config(payload: LlamaConfigInput) -> dict:
    try:
        value = save_context_length(payload.context_length)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"model": model_name(), "context_length": value}


@app.post("/api/llama/chat")
def llama_chat_message(payload: LlamaChatInput) -> dict:
    messages = [message.model_dump() for message in payload.messages]
    try:
        result = llama_chat(messages)
    except LocalAIError as exc:
        log_error("llama_chat", exc, {"context_length": context_length(), "message_count": len(messages)})
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        log_error("llama_chat_unexpected", exc, {"context_length": context_length(), "message_count": len(messages)})
        raise HTTPException(500, f"Falha inesperada no chat local: {exc}") from exc
    log_ai_interaction(
        "llama_chat",
        result["model"],
        json.dumps(messages, ensure_ascii=False),
        result["content"],
        {"context_length": result["context_length"], **result["metrics"]},
    )
    return result


app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")


@app.get("/{path:path}")
def frontend(path: str = "") -> FileResponse:
    return FileResponse(STATIC / "dashboard.html")
