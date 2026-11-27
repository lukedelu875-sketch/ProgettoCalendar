"""Lettura dei file SENZA Gemini: OCR sul tuo computer + estrazione a regole.

Serve quando la quota di Gemini è finita (o non c'è la chiave). Tre pezzi:
  1. OCR delle immagini (Tesseract oppure RapidOCR, il primo che trovi installato);
  2. testo dei PDF (PyMuPDF; le pagine scansionate passano dall'OCR);
  3. un estrattore a regole che riconosce date e orari scritti in italiano.

Niente di tutto questo manda dati fuori dal computer. L'estrattore a regole è molto meno
bravo dell'AI: i suoi impegni hanno sempre confidenza bassa e finiscono in "Da confermare".
"""
import io
import logging
import os
import re
import time
from datetime import date, datetime

log = logging.getLogger("calendario.fallback")

MAX_PDF_PAGES = 20
MAX_EVENTS = 30
OCR_ENGINE = os.getenv("OCR_ENGINE", "auto").strip().lower()      # auto | tesseract | rapidocr
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()             # es. C:\Program Files\Tesseract-OCR\tesseract.exe


class OCRUnavailable(Exception):
    """Nessun motore OCR installato (o non funziona)."""


# ---------------------------------------------------------------- OCR ----
_rapid = None
_rapid_failed = False


def _tesseract_lang():
    import pytesseract
    langs = set(pytesseract.get_languages(config=""))
    if "ita" in langs:
        return "ita+eng" if "eng" in langs else "ita"
    return "eng" if "eng" in langs else None


def _tesseract_ready() -> str:
    """Restituisce '' se Tesseract funziona, altrimenti il motivo."""
    try:
        import pytesseract
    except ImportError:
        return "manca il pacchetto Python pytesseract"
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return "il programma Tesseract non è installato (o non è nel PATH: vedi TESSERACT_CMD)"
    return ""


def _rapid_engine():
    """Crea (una volta sola) il motore RapidOCR. Prova prima il modello 'latin' (italiano)."""
    global _rapid, _rapid_failed
    if _rapid is not None or _rapid_failed:
        return _rapid
    try:
        try:  # rapidocr >= 3
            from rapidocr import LangRec, RapidOCR
            try:
                _rapid = RapidOCR(params={"Rec.lang_type": LangRec.LATIN})
            except Exception:
                log.info("RapidOCR: modello latino non selezionabile, uso quello predefinito")
                _rapid = RapidOCR()
        except ImportError:  # vecchio pacchetto rapidocr-onnxruntime
            from rapidocr_onnxruntime import RapidOCR
            _rapid = RapidOCR()
    except Exception as e:
        log.warning("RapidOCR non disponibile: %s", e)
        _rapid_failed = True
    return _rapid


def _rapid_text(img) -> str:
    import numpy as np
    engine = _rapid_engine()
    if engine is None:
        raise OCRUnavailable("RapidOCR non riesce a partire: controlla l'installazione o usa Tesseract.")
    out = engine(np.array(img.convert("RGB")))
    if hasattr(out, "txts"):                       # rapidocr >= 3
        return "\n".join(out.txts or [])
    result = out[0] if isinstance(out, tuple) else out   # vecchio formato: (lista, tempi)
    return "\n".join(r[1] for r in (result or []))


_status_cache = (0.0, None)


def ocr_status() -> dict:
    """Quale OCR è utilizzabile adesso (per mostrarlo nell'app). Il risultato resta valido 30 secondi."""
    global _status_cache
    at, cached = _status_cache
    if cached is not None and time.time() - at < 30:
        return cached
    result = _ocr_status_uncached()
    _status_cache = (time.time(), result)
    return result


def _ocr_status_uncached() -> dict:
    problems = []
    if OCR_ENGINE in ("auto", "tesseract"):
        why = _tesseract_ready()
        if not why:
            lang = _tesseract_lang()
            return {"available": True, "engine": "tesseract",
                    "italian": bool(lang and "ita" in lang), "problems": []}
        problems.append(f"Tesseract: {why}")
    if OCR_ENGINE in ("auto", "rapidocr"):
        # solo un controllo di presenza: creare il motore scarica i modelli, lo facciamo alla prima lettura vera
        import importlib.util
        if importlib.util.find_spec("onnxruntime") and (
                importlib.util.find_spec("rapidocr") or importlib.util.find_spec("rapidocr_onnxruntime")):
            return {"available": True, "engine": "rapidocr", "italian": True, "problems": []}
        problems.append("RapidOCR: non installato")
    return {"available": False, "engine": "", "italian": False, "problems": problems}


def _open_image(data: bytes):
    from PIL import Image
    try:
        return Image.open(io.BytesIO(data))
    except Exception:
        try:  # le foto dell'iPhone (HEIC) servono questo pacchetto in più
            import pillow_heif
            pillow_heif.register_heif_opener()
            return Image.open(io.BytesIO(data))
        except Exception:
            raise OCRUnavailable("Non riesco ad aprire l'immagine (per i file HEIC installa pillow-heif).")


def ocr_image(data: bytes) -> str:
    """Testo contenuto in un'immagine. Solleva OCRUnavailable se non c'è nessun motore."""
    status = ocr_status()
    if not status["available"]:
        raise OCRUnavailable("Nessun OCR installato. " + " · ".join(status["problems"]))
    img = _open_image(data)
    if status["engine"] == "tesseract":
        import pytesseract
        # ingrandire un po' gli screenshot piccoli migliora molto la lettura
        if max(img.size) < 1400:
            factor = 1400 / max(img.size)
            img = img.resize((int(img.width * factor), int(img.height * factor)))
        return pytesseract.image_to_string(img.convert("L"), lang=_tesseract_lang(), config="--psm 6")
    return _rapid_text(img)


# ---------------------------------------------------------------- PDF ----
def pdf_text(data: bytes) -> tuple[str, int]:
    """Testo di un PDF. Restituisce (testo, pagine lette con OCR)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        try:
            from pypdf import PdfReader
        except ImportError:
            raise OCRUnavailable("Per leggere i PDF installa PyMuPDF (pip install pymupdf).")
        pages = PdfReader(io.BytesIO(data)).pages[:MAX_PDF_PAGES]
        text = "\n".join((p.extract_text() or "") for p in pages)
        if len(text.strip()) < 40:
            raise OCRUnavailable("Questo PDF è una scansione: per leggerlo installa PyMuPDF (pip install pymupdf).")
        return text, 0

    parts, ocr_pages = [], 0
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page in list(doc)[:MAX_PDF_PAGES]:
            text = page.get_text().strip()
            if len(text) < 40:  # pagina senza testo: è un'immagine, la leggiamo con l'OCR
                text = ocr_image(page.get_pixmap(dpi=170).tobytes("png"))
                ocr_pages += 1
            parts.append(text)
    return "\n\n".join(parts), ocr_pages


def extract_text(files) -> tuple[str, list[str]]:
    """Testo di tutti i file (nome, contenuto, tipo). Restituisce (testo, note per l'utente)."""
    chunks, notes = [], []
    for name, data, mime in files:
        try:
            if mime == "application/pdf":
                text, ocr_pages = pdf_text(data)
                if ocr_pages:
                    notes.append(f"{name}: {ocr_pages} pagine lette con OCR")
            else:
                text = ocr_image(data)
        except OCRUnavailable:
            raise
        except Exception as e:
            log.warning("Lettura fallita per %s: %s", name, e)
            notes.append(f"{name}: non sono riuscito a leggerlo")
            continue
        if text.strip():
            chunks.append(f"--- {name} ---\n{text.strip()}")
        else:
            notes.append(f"{name}: nessun testo riconosciuto")
    return "\n\n".join(chunks), notes


# ------------------------------------------------- estrattore a regole ----
_MONTHS = {
    "gennaio": 1, "gen": 1, "febbraio": 2, "feb": 2, "marzo": 3, "mar": 3, "aprile": 4, "apr": 4,
    "maggio": 5, "mag": 5, "giugno": 6, "giu": 6, "luglio": 7, "lug": 7, "agosto": 8, "ago": 8,
    "settembre": 9, "set": 9, "sett": 9, "ottobre": 10, "ott": 10, "novembre": 11, "nov": 11,
    "dicembre": 12, "dic": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_RE = r"(?:lun(?:ed[iì])?|mar(?:ted[iì])?|mer(?:coled[iì])?|gio(?:ved[iì])?|ven(?:erd[iì])?|sab(?:ato)?|dom(?:enica)?)"

# 12/01/2026, 12-01-26, 12/01, 12.01.2026 (con il punto serve l'anno: "10.30" è un orario)
_DATE_NUM = re.compile(r"(?<![\d/.:-])(\d{1,2})\s*([/\-])\s*(\d{1,2})(?:\s*\2\s*(\d{4}|\d{2}))?(?![\d/:])")
_DATE_DOT = re.compile(r"(?<![\d/.:-])(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?![\d/:.])")
# 12 gennaio 2026 / 12 gen / lunedì 12 gennaio / 1° febbraio
_DATE_TXT = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s*(?:°|º|o)?\s+({_MONTH_RE})\b\.?(?:\s*(?:del\s+)?(\d{{4}}))?", re.I)
_TIME = re.compile(
    r"(?:\b(?:ore|alle|dalle|h)\s*\.?\s*(\d{1,2})(?:\s*[:.,h]\s*(\d{2}))?(?!\d)|(?<![\d/.])(\d{1,2})[:.](\d{2})(?![\d/.]))",
    re.I)
_TIME_END = re.compile(
    r"(?:-|–|—|\balle?\b|\ba\b)\s*(?:ore\s*)?(\d{1,2})(?:\s*[:.,h]\s*(\d{2}))?(?!\d)", re.I)
_DEADLINE = re.compile(r"\b(scadenza|entro(?:\s+il)?|termine ultimo|non oltre)\b", re.I)
_NOISE = re.compile(r"\b(protocollo|prot\.|pubblicat[oa]|emess[oa]|data di emissione|firmat[oa])\b", re.I)
_PLACE = re.compile(
    r"\b(?:presso|luogo\s*:|dove\s*:|c/o|in\s+(?:via|viale|piazza|aula|sala|palestra)|aula|sala|palestra)\s+([^,;.\n]{3,70})",
    re.I)


def _mk_date(day: int, month: int, year_s, today: date):
    """Costruisce la data; senza anno prende la prima occorrenza non passata."""
    explicit = bool(year_s)
    year = int(year_s) if year_s else today.year
    if year < 100:
        year += 2000
    try:
        d = date(year, month, day)
    except ValueError:
        return None, explicit
    if not explicit and d < today:
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None, explicit
    return d, explicit


def _find_dates(line: str, today: date):
    """Date trovate nella riga: lista di (data, anno_esplicito, inizio, fine) in ordine di posizione."""
    found = []
    for m in _DATE_TXT.finditer(line):
        d, ex = _mk_date(int(m.group(1)), _MONTHS[m.group(2).lower()], m.group(3), today)
        if d:
            found.append((d, ex, m.start(), m.end()))
    for rx, dg, mg, yg in ((_DATE_DOT, 1, 2, 3), (_DATE_NUM, 1, 3, 4)):
        for m in rx.finditer(line):
            if any(s <= m.start() < e for _, _, s, e in found):
                continue
            day, month = int(m.group(dg)), int(m.group(mg))
            if not (1 <= day <= 31 and 1 <= month <= 12):
                continue
            d, ex = _mk_date(day, month, m.group(yg), today)
            if d:
                found.append((d, ex, m.start(), m.end()))
    return sorted(found, key=lambda x: x[2])


_WD_INDEX = {"lun": 0, "mar": 1, "mer": 2, "gio": 3, "ven": 4, "sab": 5, "dom": 6}
_WD_BEFORE = re.compile(rf"\b({_WEEKDAY_RE})\.?[\s,]*$", re.I)


def _weekday_mismatch(line: str, pos: int, d: date) -> bool:
    """True se davanti alla data c'è un giorno della settimana che non coincide (es. 'sabato 21 marzo')."""
    m = _WD_BEFORE.search(line[max(0, pos - 20):pos])
    return bool(m) and _WD_INDEX[m.group(1)[:3].lower()] != d.weekday()


def _find_time(text: str, skip_spans=()):
    """Primo orario (ed eventuale fine) nel testo, come ('HH:MM', 'HH:MM' oppure '')."""
    for m in _TIME.finditer(text):
        if any(s <= m.start() < e for s, e in skip_spans):
            continue
        h = int(m.group(1) or m.group(3))
        mi = int(m.group(2) or m.group(4) or 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            continue
        start = f"{h:02d}:{mi:02d}"
        end = ""
        tail = _TIME_END.match(text, m.end())
        if not tail:  # tollera uno spazio prima del trattino
            tail = _TIME_END.match(text, m.end() + (len(text[m.end():]) - len(text[m.end():].lstrip())))
        if tail:
            eh, em = int(tail.group(1)), int(tail.group(2) or 0)
            if 0 <= eh <= 23 and 0 <= em <= 59 and (eh, em) > (h, mi):
                end = f"{eh:02d}:{em:02d}"
        return start, end
    return "", ""


def _clean_title(line: str, spans) -> str:
    """Toglie dalla riga le date e gli orari e ripulisce il resto."""
    out, last = [], 0
    for s, e in sorted(spans):
        out.append(line[last:s])
        last = max(last, e)
    out.append(line[last:])
    t = " ".join(out)
    t = _TIME.sub(" ", t)
    t = re.sub(rf"\b{_WEEKDAY_RE}\b\.?", " ", t, flags=re.I)
    t = re.sub(r"\b(?:il|del|dal|al|di|e|ore|alle|dalle|giorno|entro)\b(?=\s*[:,;\-–]*\s*$)", " ", t, flags=re.I)
    t = re.sub(r"^[\s:;,\-–—•*·\d.)]+|[\s:;,\-–—•*·(]+$", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


def events_from_text(text: str, today: date | None = None, source_name: str = "") -> list[dict]:
    """Impegni trovati nel testo con sole regole. Confidenza sempre bassa (max 0.65)."""
    today = today or datetime.now().date()
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    events, seen = [], set()
    for i, line in enumerate(lines):
        if re.match(r"^--- .* ---$", line):
            continue
        dates = [d for d in _find_dates(line, today) if not _NOISE.search(line[:d[2]])]
        if not dates:
            continue
        spans = [(s, e) for _, _, s, e in dates]
        start_t, end_t = _find_time(line, spans)
        if not start_t and i + 1 < len(lines) and not _find_dates(lines[i + 1], today):
            start_t, end_t = _find_time(lines[i + 1])
        title = _clean_title(line, spans)
        prev = lines[i - 1] if i > 0 else ""
        if len(title) < 4 and prev and not _find_dates(prev, today) and not prev.startswith("---"):
            title = prev
        if len(title) < 4:
            title = f"Impegno da {source_name}" if source_name else "Impegno"
        if _DEADLINE.search(line) and not title.lower().startswith("scadenza"):
            title = re.sub(r"\s{2,}", " ", _DEADLINE.sub(" ", title)).strip(" ,;:-")
            title = f"Scadenza: {title}" if len(title) >= 4 else "Scadenza"
        title = title[:60].rstrip(" ,;:-")
        place = ""
        for ctx in (line, lines[i + 1] if i + 1 < len(lines) else ""):
            pm = _PLACE.search(ctx)
            if pm:
                place = pm.group(0).strip()[:80]
                break
        context = " ".join(lines[max(0, i - 1): i + 2])
        for d, explicit, d_start, _ in dates:
            key = (d, start_t, title.lower())
            if key in seen:
                continue
            seen.add(key)
            conf = 0.40 + (0.10 if start_t else 0) + (0.10 if explicit else 0) + (0.05 if len(title) >= 8 else 0)
            summary = context[:300]
            if _weekday_mismatch(line, d_start, d):
                conf -= 0.15
                summary = ("ATTENZIONE: il giorno della settimana scritto nel testo non coincide con la data. "
                           + summary)[:300]
            events.append({
                "title": title,
                "start_at": f"{d:%Y-%m-%d}T{start_t}" if start_t else f"{d:%Y-%m-%d}",
                "end_at": f"{d:%Y-%m-%d}T{end_t}" if (start_t and end_t) else "",
                "all_day": not start_t,
                "location": place,
                "summary": summary,
                "confidence": round(max(0.1, min(conf, 0.65)), 2),
            })
            if len(events) >= MAX_EVENTS:
                return events
    return events


# ------------------------------------------- risparmio di token (locale) ----
# Prima di spendere una chiamata AI: c'è almeno un segnale di data? E se sì, mandiamo solo
# le righe che contano invece dell'intera mail.
_RELATIVE = re.compile(
    r"\b(oggi|domani|dopodomani|stasera|stamattina|questa\s+(?:sera|settimana)|prossim[oa]|settimana\s+prossima|"
    r"lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica|lunedi|martedi|mercoledi|giovedi|venerdi)\b", re.I)
_EVENT_WORD = re.compile(
    r"\b(riunione|incontro|appuntamento|colloqui?o|consiglio|assemblea|verifica|compito|interrogazione|esame|visita|"
    r"uscita|gita|scadenza|entro|iscrizion[ei]|pagamento|saldo|consegna|evento|concerto|spettacolo|lezione|corso|"
    r"prenotazione|biglietto|volo|check-?in|partenza|arrivo|ritrovo|convocazion[ei]|circolare|meeting|webinar)\b", re.I)


def _has_time_or_date(line: str, today: date) -> bool:
    return bool(_find_dates(line, today)) or bool(_TIME.search(line))


def has_date_signal(text: str, today: date | None = None) -> bool:
    """True se il testo contiene qualcosa che somiglia a una data o a un orario."""
    today = today or datetime.now().date()
    if _find_dates(text, today) or _TIME.search(text):
        return True
    return bool(_RELATIVE.search(text) and _EVENT_WORD.search(text))


def condense(text: str, max_chars: int = 2500, today: date | None = None) -> str:
    """Tiene le righe con date, orari, parole da impegno e quelle subito intorno. Il resto (saluti,
    firme, avvertenze legali, pubblicità) non serve a capire gli impegni e costerebbe token."""
    today = today or datetime.now().date()
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if len(text) <= max_chars:
        return "\n".join(lines)
    keep = set()
    for i, ln in enumerate(lines):
        if _has_time_or_date(ln, today) or _RELATIVE.search(ln) or _EVENT_WORD.search(ln):
            keep.update(range(max(0, i - 1), min(len(lines), i + 2)))
    head = list(range(min(3, len(lines))))           # l'inizio dà sempre il contesto (oggetto, mittente)
    chosen = sorted(set(head) | keep) or head
    out, size = [], 0
    for i in chosen:
        piece = lines[i][:300]
        if size + len(piece) > max_chars:
            break
        out.append(piece)
        size += len(piece) + 1
    return "\n".join(out)


def local_file_text(files) -> tuple[str, list]:
    """Prova a leggere i file in locale, senza AI. Restituisce (testo, file non leggibili).
    Un PDF col testo e uno screenshot (se c'è l'OCR) si leggono qui; il resto va all'AI come file."""
    chunks, unread = [], []
    for item in files:
        name, data, mime = item
        try:
            if mime == "application/pdf":
                text, _ = pdf_text_light(data)
            else:
                text = ocr_image(data)
        except Exception:
            unread.append(item)
            continue
        if len(text.strip()) >= 30:
            chunks.append(f"[{name}]\n{text.strip()}")
        else:
            unread.append(item)
    return "\n\n".join(chunks), unread


def pdf_text_light(data: bytes) -> tuple[str, int]:
    """Solo il livello di testo del PDF (veloce, nessun OCR): per le scansioni solleva OCRUnavailable."""
    try:
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(data)).pages[:MAX_PDF_PAGES]
        text = "\n".join((p.extract_text() or "") for p in pages)
    except ImportError:
        try:
            import fitz
            with fitz.open(stream=data, filetype="pdf") as doc:
                text = "\n".join(p.get_text() for p in list(doc)[:MAX_PDF_PAGES])
        except ImportError:
            raise OCRUnavailable("Per leggere i PDF installa pypdf (pip install pypdf).")
    if len(text.strip()) < 40:
        raise OCRUnavailable("PDF senza testo (scansione).")
    return text, 0
