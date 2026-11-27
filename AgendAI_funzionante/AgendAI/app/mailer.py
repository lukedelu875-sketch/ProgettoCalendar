"""Lettura della posta via IMAP (uno o più account) e creazione degli impegni."""
import email
import hashlib
import imaplib
import logging
import re
import threading
from datetime import datetime, timedelta
from email.policy import default as default_policy
from html import unescape

from . import ai, config, db, fallback
from .files import mime_for

log = logging.getLogger("calendario.mail")
_lock = threading.Lock()
_MESI = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(d: datetime) -> str:
    return f"{d.day:02d}-{_MESI[d.month - 1]}-{d.year}"  # i mesi IMAP sono sempre in inglese


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _body_text(msg) -> str:
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
        if body is None:
            return ""
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = _html_to_text(content)
        return content.strip()
    except Exception:
        return ""


MIN_IMAGE_BYTES = 20_000   # sotto questa soglia sono quasi sempre loghi e icone
MAX_IMAGES_PER_MAIL = 4


def _attachments(msg) -> list[tuple[str, bytes, str]]:
    """PDF e immagini allegati (non le immagini incorporate nel testo, come i loghi)."""
    limit = config.MAX_PDF_MB * 1024 * 1024
    found, images = [], 0
    for part in msg.iter_attachments():
        name = part.get_filename() or "allegato"
        mime = mime_for(name, part.get_content_type())
        if not mime:
            continue
        data = part.get_payload(decode=True)
        if not data or len(data) > limit:
            continue
        if mime.startswith("image/"):
            if len(data) < MIN_IMAGE_BYTES or images >= MAX_IMAGES_PER_MAIL:
                continue
            images += 1
        found.append((name, data, mime))
    return found


def _is_bulk(msg) -> bool:
    return msg.get("List-Unsubscribe") is not None or \
        str(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk")


def process_message(msg, message_id: str) -> int:
    """Analizza una mail e salva gli impegni trovati. Restituisce quanti ne ha aggiunti.

    Prima dell'AI ci sono tre filtri locali (zero token): newsletter senza allegati, mail senza
    nessun segnale di data o orario, e contenuti già visti (stesso oggetto e stesso testo)."""
    attachments = _attachments(msg)
    body = _body_text(msg)
    has_pdf = any(m == "application/pdf" for _, _, m in attachments)
    if _is_bulk(msg) and not has_pdf:
        return 0
    if not body and not attachments:
        return 0

    subject = str(msg.get("Subject", "(senza oggetto)"))
    sender = str(msg.get("From", ""))
    if not attachments and not fallback.has_date_signal(f"{subject}\n{body}"):
        return 0  # nessuna data, nessun orario, nessuna parola da impegno: nulla da chiedere all'AI
    digest = "h:" + hashlib.sha1(f"{subject}\n{body[:3000]}".encode("utf-8", "ignore")).hexdigest()[:24]
    if not attachments and db.is_processed(digest):
        return 0  # promemoria identico a uno già letto
    text = f"Mail ricevuta il {msg.get('Date', '')}\nDa: {sender}\nOggetto: {subject}\n\n{body}"

    added = 0
    for raw in ai.extract_events(text, attachments):
        try:
            ev = db.clean_event(raw)
        except ValueError as e:
            log.warning("Impegno scartato (%s): %s", e, raw)
            continue
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
        multi_day = bool(ev["end_at"]) and ev["end_at"][:10] > ev["start_at"][:10]
        # i periodi di più giorni passano sempre dalla revisione: lì si scelgono i giorni che servono
        status = "confirmed" if confidence >= config.MIN_CONFIDENCE and not multi_day else "pending"
        if db.add_event(ev, source="mail", source_ref=f"{subject} (da {sender})",
                        confidence=confidence, status=status):
            added += 1
    if not attachments:
        db.mark_processed(digest)
    return added


def describe_error(e: Exception) -> str:
    """Rende leggibili gli errori più comuni di IMAP."""
    text = str(e)
    if "AUTHENTICATIONFAILED" in text or "Invalid credentials" in text or "LOGIN failed" in text:
        return "Accesso rifiutato: controlla indirizzo e password per app."
    if isinstance(e, (OSError, TimeoutError)) and not isinstance(e, imaplib.IMAP4.error):
        return "Impossibile raggiungere il server di posta: controlla il nome del server e la connessione."
    return text[:200]


def test_login(address: str, password: str, server: str):
    """Prova ad accedere alla casella. Solleva ValueError con un messaggio comprensibile."""
    try:
        with imaplib.IMAP4_SSL(server, timeout=20) as imap:
            imap.login(address, password)
    except Exception as e:
        raise ValueError(describe_error(e))


_NOREPLY = re.compile(r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|newsletter|marketing|promo)", re.I)
_HEAD_FIELDS = "MESSAGE-ID FROM SUBJECT LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED CONTENT-TYPE"


def _gmail_filter(server: str) -> str:
    """Gmail permette di escludere in partenza Promozioni e Social: niente scaricamento, niente AI."""
    if not re.search(r"(gmail|googlemail)\.com$", server.lower()):
        return ""
    cats = [c.strip() for c in config.env("GMAIL_SKIP_CATEGORIES", "promotions,social,forums").split(",") if c.strip()]
    return " ".join(f"-category:{c}" for c in cats)


def _fetch_headers(imap, uids: list[int]) -> dict:
    """Intestazioni di molte mail con poche richieste: {uid: Message}."""
    out = {}
    for i in range(0, len(uids), 100):
        chunk = ",".join(str(u) for u in uids[i:i + 100])
        typ, data = imap.uid("FETCH", chunk, f"(UID BODY.PEEK[HEADER.FIELDS ({_HEAD_FIELDS})])")
        if typ != "OK" or not data:
            continue
        waiting = None   # intestazione letta ma con l'UID che arriva dopo, nella riga di chiusura
        for item in data:
            if isinstance(item, tuple):
                m = re.search(rb"UID (\d+)", item[0])
                if m:
                    out[int(m.group(1))] = email.message_from_bytes(item[1])
                    waiting = None
                else:
                    waiting = item[1]
            elif isinstance(item, (bytes, bytearray)) and waiting is not None:
                m = re.search(rb"UID (\d+)", item)
                if m:
                    out[int(m.group(1))] = email.message_from_bytes(waiting)
                waiting = None
    return out


def _skippable(head) -> bool:
    """Newsletter e messaggi automatici senza allegati: non si scaricano nemmeno."""
    bulk = head.get("List-Unsubscribe") is not None \
        or str(head.get("Precedence", "")).lower() in ("bulk", "list", "junk") \
        or str(head.get("Auto-Submitted", "no")).lower() not in ("no", "")
    if not bulk and _NOREPLY.search(str(head.get("From", ""))):
        bulk = True
    may_have_pdf = "multipart/mixed" in str(head.get("Content-Type", "")).lower()
    return bulk and not may_have_pdf


def _search_new(imap, acc: dict, validity: str) -> list[int]:
    """UID delle mail da guardare: quelle arrivate dopo l'ultimo controllo (o gli ultimi giorni, la prima volta)."""
    mark = db.get_meta(f"uidw:{acc['id']}")
    wm = 0
    if mark.startswith(f"{validity}:"):
        wm = int(mark.split(":")[1])
    criteria = ["UID", f"{wm + 1}:*"] if wm else \
        ["SINCE", _imap_date(datetime.now() - timedelta(days=config.LOOKBACK_DAYS))]
    gm = _gmail_filter(acc["imap_server"])
    attempts = [criteria + ["X-GM-RAW", f'"{gm}"']] if gm else []
    attempts.append(criteria)
    for crit in attempts:
        try:
            typ, data = imap.uid("SEARCH", None, *crit)
        except imaplib.IMAP4.error:
            continue
        if typ == "OK":
            return sorted(u for u in (int(x) for x in data[0].split()) if u > wm)
    return []


def _check_account(acc: dict, result: dict):
    """Legge le mail NUOVE di un account (non le segna come lette) e chiama l'AI solo se serve."""
    consecutive_errors = 0
    with imaplib.IMAP4_SSL(acc["imap_server"], timeout=60) as imap:
        imap.login(acc["email"], acc["password"])
        imap.select("INBOX", readonly=True)
        try:
            validity = imap.response("UIDVALIDITY")[1][0].decode()
        except Exception:
            validity = "0"
        uids = _search_new(imap, acc, validity)
        if not uids:
            return
        heads = _fetch_headers(imap, uids)
        pending_min = None   # la prima mail da riprovare al prossimo giro
        stopped = None
        for uid in uids:
            head = heads.get(uid)
            raw_id = (head.get("Message-ID") if head else "") or ""
            # senza Message-ID l'UID vale solo dentro questo account: lo prefissiamo
            message_id = raw_id.strip() or f"acc{acc['id']}-uid-{uid}"
            if db.is_processed(message_id):
                continue
            if head is not None and _skippable(head):
                db.mark_processed(message_id)
                result["skipped"] += 1
                continue
            try:
                _, full = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                msg = email.message_from_bytes(full[0][1], policy=default_policy)
                result["events"] += process_message(msg, message_id)
                result["messages"] += 1
                db.mark_processed(message_id)
                consecutive_errors = 0
            except ai.QuotaError as e:
                # Quota finita: NON è colpa della mail. Non la contiamo come tentativo fallito
                # (dopo 3 tentativi verrebbe saltata per sempre): ci fermiamo e riprova al prossimo giro.
                pending_min = uid if pending_min is None else min(pending_min, uid)
                stopped = f"{e} Le mail non lette restano in coda: riprovo al prossimo controllo."
                break
            except Exception as e:
                log.error("Errore sulla mail %s: %s", message_id, e)
                result["errors"] += 1
                consecutive_errors += 1
                if db.bump_failure(message_id) >= 3:  # dopo 3 tentativi la saltiamo
                    db.mark_processed(message_id)
                else:
                    pending_min = uid if pending_min is None else min(pending_min, uid)
                if consecutive_errors >= 3:  # probabile problema di quota/chiave: ci fermiamo
                    stopped = f"Controllo interrotto dopo errori ripetuti: {e}"
                    break
        # la prossima volta si ripartirà da qui: solo mail più nuove (o ancora da riprovare)
        new_wm = (pending_min - 1) if pending_min is not None else max(uids)
        db.set_meta(f"uidw:{acc['id']}", f"{validity}:{new_wm}")
        if stopped:
            raise RuntimeError(stopped)


def check_mail() -> dict:
    """Un giro di controllo su tutti gli account attivi."""
    accounts = db.list_accounts(enabled_only=True)
    result = {"messages": 0, "events": 0, "errors": 0, "skipped": 0, "accounts": len(accounts), "failed": []}
    if not accounts:
        return result
    # Senza chiave ogni mail fallirebbe e dopo 3 tentativi verrebbe saltata per sempre.
    if not ai.has_any_engine():
        raise RuntimeError(ai.NO_KEY_MESSAGE)

    for acc in accounts:
        name = acc["label"] or acc["email"]
        try:
            _check_account(acc, result)
            db.set_account_status(acc["id"], "")
        except Exception as e:
            log.exception("Controllo fallito per %s", name)
            message = describe_error(e)
            result["failed"].append(f"{name}: {message}")
            db.set_account_status(acc["id"], message)
    return result


def run_check() -> dict:
    """Esegue il controllo (se non ce n'è già uno in corso) e registra l'esito."""
    if not _lock.acquire(blocking=False):
        return {"running": True}
    record = True
    try:
        result = check_mail()
        if result["accounts"] == 0:  # niente da controllare: non sporchiamo lo stato
            record = False
            return result
        failed = "; ".join(result["failed"])
        db.set_meta("last_error", failed[:300])
        db.set_meta("last_result", f"{result['messages']} mail analizzate, {result['skipped']} scartate, "
                                   f"{result['events']} impegni nuovi")
        if failed:
            result["error"] = failed
        return result
    except Exception as e:
        log.exception("Controllo posta fallito")
        db.set_meta("last_error", str(e)[:300])
        return {"error": str(e)}
    finally:
        if record:
            db.set_meta("last_check", datetime.now().isoformat(timespec="seconds"))
        _lock.release()


def is_running() -> bool:
    return _lock.locked()


def start_check() -> dict:
    """Avvia il controllo in background (il pulsante non resta in attesa: la pagina chiede lo stato)."""
    if _lock.locked():
        return {"running": True}
    threading.Thread(target=run_check, name="controllo-posta", daemon=True).start()
    return {"started": True}
