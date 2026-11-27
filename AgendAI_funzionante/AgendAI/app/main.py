import hmac
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import ai, config, db, diagnose, fallback, mailer
from .files import mime_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
STATIC = Path(__file__).resolve().parent.parent / "static"
scheduler = BackgroundScheduler(timezone=config.TIMEZONE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.APP_PASSWORD:
        raise RuntimeError("Imposta APP_PASSWORD nel file .env: protegge l'accesso al calendario.")
    db.init()
    # Il controllo parte sempre: se non ci sono account collegati non fa nulla,
    # e appena ne aggiungi uno dal sito viene letto al giro successivo.
    first_run = datetime.now(ZoneInfo(config.TIMEZONE)) + timedelta(seconds=10)
    scheduler.add_job(mailer.run_check, "interval", minutes=config.CHECK_EVERY_MINUTES,
                      next_run_time=first_run, max_instances=1, coalesce=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Agenda", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=60 * 60 * 24 * 30,
                   same_site="lax", https_only=config.COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------- accesso ----------
_attempts: dict[str, list[float]] = {}


def require_login(request: Request):
    if not request.session.get("ok"):
        raise HTTPException(401, "Accedi per continuare.")


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
def login(request: Request, password: str = Form("")):
    ip = request.client.host if request.client else "?"
    now = time.time()
    recent = [t for t in _attempts.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        return RedirectResponse("/login?e=2", status_code=303)
    if hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode()):
        request.session["ok"] = True
        _attempts.pop(ip, None)
        return RedirectResponse("/", status_code=303)
    _attempts[ip] = recent + [now]
    return RedirectResponse("/login?e=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/")
def home(request: Request):
    if not request.session.get("ok"):
        return RedirectResponse("/login")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


# ---------- impegni ----------
class EventIn(BaseModel):
    title: str
    start_at: str
    end_at: str = ""
    all_day: bool = False
    location: str = ""
    summary: str = ""


class EventPatch(BaseModel):
    title: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    all_day: bool | None = None
    location: str | None = None
    summary: str | None = None
    status: str | None = None


@app.get("/api/events", dependencies=[Depends(require_login)])
def list_events(status: str = "confirmed"):
    if status not in ("confirmed", "pending"):
        raise HTTPException(422, "Stato non valido.")
    return db.list_events(status)


@app.post("/api/events", status_code=201, dependencies=[Depends(require_login)])
def create_event(body: EventIn):
    try:
        ev = db.clean_event(body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": db.add_event(ev, source="manual", dedupe=False)}


@app.patch("/api/events/{event_id}", dependencies=[Depends(require_login)])
def patch_event(event_id: int, body: EventPatch):
    current = db.get_event(event_id)
    if not current:
        raise HTTPException(404, "Impegno non trovato.")
    changes = body.model_dump(exclude_none=True)
    status = changes.pop("status", current["status"])
    if status not in ("confirmed", "pending"):
        raise HTTPException(422, "Stato non valido.")
    try:
        ev = db.clean_event({**current, **changes})
    except ValueError as e:
        raise HTTPException(422, str(e))
    db.update_event(event_id, ev, status)
    return {"ok": True}


@app.delete("/api/events/{event_id}", status_code=204, dependencies=[Depends(require_login)])
def delete_event(event_id: int):
    if not db.delete_event(event_id):
        raise HTTPException(404, "Impegno non trovato.")
    return Response(status_code=204)


class SplitIn(BaseModel):
    dates: list[str]


@app.post("/api/events/{event_id}/split", dependencies=[Depends(require_login)])
def split_event(event_id: int, body: SplitIn):
    try:
        result = db.split_event(event_id, body.dates)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if result is None:
        raise HTTPException(404, "Impegno non trovato.")
    return {"created": result[0], "skipped": result[1]}


# ---------- PDF e immagini scelti a mano ----------
MAX_FILES = 6
MAX_TOTAL_MB = 18


@app.post("/api/file", dependencies=[Depends(require_login)])
async def analyze_files(files: list[UploadFile] = File(...), mode: str = Form("auto")):
    # auto  = Gemini, e se non va l'AI di riserva e poi l'OCR sul computer
    # local = solo OCR e regole sul computer: nessun dato esce (pulsante "Solo OCR")
    if mode not in ("auto", "local"):
        raise HTTPException(422, "Modalità non valida.")
    if not files or len(files) > MAX_FILES:
        raise HTTPException(422, f"Scegli da 1 a {MAX_FILES} file.")
    limit = config.MAX_PDF_MB * 1024 * 1024
    loaded, total = [], 0
    for f in files:
        name = f.filename or "file"
        mime = mime_for(name, f.content_type or "")
        if not mime:
            raise HTTPException(422, f"\u201c{name}\u201d: formato non supportato. Usa PDF, PNG, JPG, WEBP o HEIC.")
        data = await f.read()
        if len(data) > limit:
            raise HTTPException(422, f"\u201c{name}\u201d supera i {config.MAX_PDF_MB} MB.")
        total += len(data)
        loaded.append((name, data, mime))
    if total > MAX_TOTAL_MB * 1024 * 1024:
        raise HTTPException(422, f"I file insieme superano i {MAX_TOTAL_MB} MB: caricane meno per volta.")
    try:
        found, info = await run_in_threadpool(
            lambda: ai.extract_events_ex("", loaded, mode=mode, allow_rules=True))
    except ai.AIError as e:
        raise HTTPException(502, str(e))

    source = "pdf" if any(m == "application/pdf" for _, _, m in loaded) else "image"
    ref = loaded[0][0] + (f" e altri {len(loaded) - 1}" if len(loaded) > 1 else "")
    return _save_found(found, info, source, ref)


def _save_found(found: list[dict], info: dict, source: str, ref: str) -> dict:
    added = 0
    for raw in found:
        try:
            ev = db.clean_event(raw)
        except ValueError:
            continue
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        # i file scelti a mano passano sempre dalla revisione: l'utente conferma
        if db.add_event(ev, source=source, source_ref=ref, confidence=confidence, status="pending"):
            added += 1
    return {"found": len(found), "added": added, "engine": info["engine"], "note": info["note"]}


class TextIn(BaseModel):
    text: str
    name: str = "screenshot"
    mode: str = "auto"


@app.post("/api/text", dependencies=[Depends(require_login)])
async def analyze_text(body: TextIn):
    """Testo già letto dal browser con l'OCR (lo screenshot non viene nemmeno caricato)."""
    if body.mode not in ("auto", "local"):
        raise HTTPException(422, "Modalità non valida.")
    text = body.text.strip()[:60_000]
    if len(text) < 8:
        raise HTTPException(422, "Nel file non ho letto abbastanza testo.")
    try:
        found, info = await run_in_threadpool(
            lambda: ai.extract_events_ex(text, [], mode=body.mode, allow_rules=True))
    except ai.AIError as e:
        raise HTTPException(502, str(e))
    return _save_found(found, info, "image", body.name[:120] or "screenshot")


# ---------- impostazioni: chiave Gemini e account di posta ----------
def _gemini_view() -> dict:
    stored = db.get_setting("gemini_api_key")
    key = stored or config.GEMINI_API_KEY
    return {
        "configured": bool(key),
        "hint": key[-4:] if key else "",  # la chiave intera non lascia mai il server
        "source": "site" if stored else ("env" if key else ""),
        "model": db.get_setting("gemini_model"),
        "default_model": config.GEMINI_MODEL,
    }


def _fallback_view() -> dict:
    fb = ai.fallback_settings()
    return {
        "configured": ai.has_fallback_ai(),
        "preset": db.get_setting("fallback_preset"),
        "base_url": fb["url"],
        "model": fb["model"],
        "hint": fb["key"][-4:] if fb["key"] else "",  # la chiave intera non lascia mai il server
        "has_key": bool(fb["key"]),
        "source": "site" if db.get_setting("fallback_url") else ("env" if fb["url"] else ""),
    }


def _accounts_view() -> list[dict]:
    return [
        {
            "id": a["id"], "label": a["label"], "email": a["email"],
            "imap_server": a["imap_server"], "enabled": bool(a["enabled"]),
            "last_check": a["last_check"], "last_error": a["last_error"],
        }
        for a in db.list_accounts()
    ]


class GeminiIn(BaseModel):
    api_key: str = ""   # vuoto = tieni quella attuale
    model: str = ""     # vuoto = modello predefinito


class FallbackIn(BaseModel):
    preset: str = ""
    base_url: str
    model: str
    api_key: str = ""   # vuota = tieni quella attuale (o nessuna, per Ollama)


class AccountIn(BaseModel):
    label: str = ""
    email: str
    password: str
    imap_server: str = "imap.gmail.com"


class AccountPatch(BaseModel):
    label: str | None = None
    password: str | None = None
    imap_server: str | None = None
    enabled: bool | None = None


_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


@app.get("/api/settings", dependencies=[Depends(require_login)])
def get_settings():
    return {"gemini": _gemini_view(), "fallback": _fallback_view(), "accounts": _accounts_view(),
            "ocr": fallback.ocr_status()}


@app.put("/api/settings/fallback", dependencies=[Depends(require_login)])
async def save_fallback(body: FallbackIn):
    url = body.base_url.strip().rstrip("/")
    model = body.model.strip()
    key = re.sub(r"\s", "", body.api_key) or ai.fallback_settings()["key"]
    if not re.fullmatch(r"https?://[^\s]{3,200}", url):
        raise HTTPException(422, "L'indirizzo deve iniziare con http:// o https://")
    if not re.fullmatch(r"[A-Za-z0-9._:/@+-]{2,100}", model):
        raise HTTPException(422, "Il nome del modello non è valido.")
    try:
        warning = await run_in_threadpool(ai.check_fallback, url, key, model)
    except ai.AIError as e:
        raise HTTPException(422, str(e))
    db.set_setting("fallback_url", url)
    db.set_setting("fallback_model", model)
    db.set_setting("fallback_preset", body.preset.strip()[:30])
    if key:
        db.set_setting("fallback_key", key)
    else:
        db.delete_setting("fallback_key")
    return {"fallback": _fallback_view(), "warning": warning}


@app.delete("/api/settings/fallback", dependencies=[Depends(require_login)])
def remove_fallback():
    for k in ("fallback_url", "fallback_key", "fallback_model", "fallback_preset"):
        db.delete_setting(k)
    return {"fallback": _fallback_view()}


@app.put("/api/settings/gemini", dependencies=[Depends(require_login)])
async def save_gemini(body: GeminiIn):
    key = re.sub(r"\s", "", body.api_key)
    model = body.model.strip()
    if model and not re.fullmatch(r"[A-Za-z0-9._/-]{2,80}", model):
        raise HTTPException(422, "Il nome del modello non è valido.")
    if not key and not ai.api_key():
        raise HTTPException(422, "Incolla la chiave API di Gemini.")
    # prima proviamo la chiave: una chiave sbagliata non viene salvata
    try:
        warning = await run_in_threadpool(ai.check_key, key or ai.api_key(), model or config.GEMINI_MODEL)
    except ai.AIError as e:
        raise HTTPException(422, str(e))
    if key:
        db.set_setting("gemini_api_key", key)
    if model:
        db.set_setting("gemini_model", model)
    else:
        db.delete_setting("gemini_model")
    return {"gemini": _gemini_view(), "warning": warning}


@app.delete("/api/settings/gemini-key", dependencies=[Depends(require_login)])
def remove_gemini_key():
    db.delete_setting("gemini_api_key")
    return {"gemini": _gemini_view()}


@app.post("/api/accounts", status_code=201, dependencies=[Depends(require_login)])
async def add_account(body: AccountIn):
    address = body.email.strip().lower()
    password = re.sub(r"\s", "", body.password)  # Google mostra la password per app a gruppi di 4
    server = body.imap_server.strip().lower() or "imap.gmail.com"
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise HTTPException(422, "L'indirizzo email non è valido.")
    if not password:
        raise HTTPException(422, "Inserisci la password per app.")
    if not _HOST.match(server):
        raise HTTPException(422, "Il nome del server non è valido.")
    try:
        await run_in_threadpool(mailer.test_login, address, password, server)
    except ValueError as e:
        raise HTTPException(422, str(e))
    try:
        db.add_account(body.label.strip()[:60], address, password, server)
    except sqlite3.IntegrityError:
        raise HTTPException(422, "Questo indirizzo è già collegato.")
    return {"accounts": _accounts_view()}


@app.patch("/api/accounts/{account_id}", dependencies=[Depends(require_login)])
async def patch_account(account_id: int, body: AccountPatch):
    current = db.get_account(account_id)
    if not current:
        raise HTTPException(404, "Account non trovato.")
    changes = body.model_dump(exclude_none=True)
    if "label" in changes:
        changes["label"] = changes["label"].strip()[:60]
    if "password" in changes:
        changes["password"] = re.sub(r"\s", "", changes["password"])
        if not changes["password"]:
            changes.pop("password")
    if "imap_server" in changes:
        changes["imap_server"] = changes["imap_server"].strip().lower()
        if not _HOST.match(changes["imap_server"]):
            raise HTTPException(422, "Il nome del server non è valido.")
    if "enabled" in changes:
        changes["enabled"] = 1 if changes["enabled"] else 0
    if "password" in changes or "imap_server" in changes:  # credenziali cambiate: le proviamo
        try:
            await run_in_threadpool(
                mailer.test_login, current["email"],
                changes.get("password", current["password"]),
                changes.get("imap_server", current["imap_server"]),
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        changes["last_error"] = ""
    db.update_account(account_id, changes)
    return {"accounts": _accounts_view()}


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_login)])
def remove_account(account_id: int):
    if not db.delete_account(account_id):
        raise HTTPException(404, "Account non trovato.")
    return {"accounts": _accounts_view()}


# ---------- posta e stato ----------
@app.post("/api/mail/check", dependencies=[Depends(require_login)])
def check_mail_now():
    return mailer.start_check()   # parte in background: la pagina chiede lo stato ogni pochi secondi


@app.get("/api/status", dependencies=[Depends(require_login)])
def status():
    return {
        "pending": db.count_pending(),
        "last_check": db.get_meta("last_check"),
        "last_result": db.get_meta("last_result"),
        "last_error": db.get_meta("last_error"),
        "mail_enabled": bool(db.list_accounts(enabled_only=True)),
        "checking": mailer.is_running(),
        "check_every": config.CHECK_EVERY_MINUTES,
        "min_confidence": config.MIN_CONFIDENCE,
    }


@app.post("/api/diagnose", dependencies=[Depends(require_login)])
def run_diagnose():
    """Prova una per una le cose che servono (chiave, modelli, posta, OCR) e dice cosa sistemare."""
    return {"checks": diagnose.run(deep=True)}
