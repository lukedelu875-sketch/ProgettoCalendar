"""Estrazione degli impegni con Gemini (Google AI Studio)."""
import json
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from pydantic import BaseModel

from . import config, db

log = logging.getLogger("calendario.ai")

GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MAX_TEXT_CHARS = 20_000
NO_KEY_MESSAGE = "Manca la chiave Gemini: inseriscila in Impostazioni."


class AIError(Exception):
    """L'AI non è raggiungibile o ha rifiutato la richiesta."""


class QuotaError(AIError):
    """La quota (gratuita o a pagamento) del servizio AI è finita: riprovare più tardi."""


class ExtractedEvent(BaseModel):
    title: str
    start_at: str
    end_at: str
    all_day: bool
    location: str
    summary: str
    confidence: float


class Extraction(BaseModel):
    events: list[ExtractedEvent]


_client = None
_client_key = None


def api_key() -> str:
    """La chiave salvata dal sito vince su quella del file .env."""
    return db.get_setting("gemini_api_key") or config.GEMINI_API_KEY


def model_name() -> str:
    return db.get_setting("gemini_model") or config.GEMINI_MODEL


def has_key() -> bool:
    return bool(api_key())


def _get_client():
    global _client, _client_key
    key = api_key()
    if not key:
        raise AIError(NO_KEY_MESSAGE)
    if _client is None or key != _client_key:  # chiave cambiata dal sito: nuovo client
        _client = genai.Client(api_key=key)
        _client_key = key
    return _client


def check_key(key: str, model: str) -> str:
    """Prova una chiave con una richiesta minima.

    Restituisce "" se funziona, un avviso se non si è potuto verificare (rete, quota),
    e solleva AIError se Google la rifiuta.
    """
    try:
        genai.Client(api_key=key).models.generate_content(
            model=model,
            contents="Rispondi solo: ok",
            config=types.GenerateContentConfig(max_output_tokens=16),
        )
        return ""
    except Exception as e:
        code = getattr(e, "code", None)
        if code in (400, 401, 403):
            raise AIError("Google non accetta questa chiave: controlla di averla copiata per intero.")
        if code == 404 or _is_unavailable(e):
            return f"Chiave valida, ma il modello «{model}» non c'è: ne userò uno disponibile in automatico."
        return f"Salvata, ma non sono riuscito a verificarla adesso ({str(e)[:150]})."


def _is_quota(e: Exception) -> bool:
    return getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in str(e)


def _is_unavailable(e: Exception) -> bool:
    """Questo modello non esiste o non è disponibile per questa chiave: ne proviamo un altro."""
    code, msg = getattr(e, "code", None), str(e)
    if code == 404:
        return True
    if code == 429 and re.search(r"limit:\s*0\b", msg):   # piano gratuito senza accesso a questo modello
        return True
    return code == 400 and ("not supported" in msg or "not found" in msg.lower())


_good_model: dict = {}      # {"text": nome, "vision": nome}: l'ultimo modello che ha funzionato
_good_for_key = None
_thinking_ok: dict = {}     # nome modello -> se accetta "pensiero spento"
_EXCLUDE = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "native", "learnlm", "gemma")


def list_models() -> list[str]:
    """Modelli Gemini 'flash' che questa chiave può usare, dal più adatto in giù (stabili prima delle anteprime)."""
    found = []
    for m in _get_client().models.list():
        name = (m.name or "").replace("models/", "")
        actions = getattr(m, "supported_actions", None)
        if actions and "generateContent" not in actions:
            continue
        mt = re.match(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?(?:-(.+))?$", name)
        if not mt or any(x in name for x in _EXCLUDE):
            continue
        preview = bool(mt.group(3) and re.search(r"preview|exp", mt.group(3)))
        found.append((name, float(mt.group(1)), bool(mt.group(2)), preview))
    # più nuovo prima, ma le versioni stabili battono le anteprime
    found.sort(key=lambda x: (x[3], -x[1], len(x[0])))
    return [f[0] for f in found]


def _ranked_models(kind: str) -> list[str]:
    names = list_models()
    lite = [n for n in names if "-lite" in n]
    flash = [n for n in names if "-lite" not in n]
    # solo testo: il modello "lite" costa meno e ha limiti giornalieri più larghi; per le immagini serve il "flash"
    return (lite + flash) if kind == "text" else (flash + lite)


def _make_cfg(system: str, thinking_off: bool):
    kw = dict(system_instruction=system, response_mime_type="application/json",
              response_schema=Extraction, temperature=0.1, max_output_tokens=4096)
    if thinking_off and hasattr(types, "ThinkingConfig"):
        kw["thinking_config"] = types.ThinkingConfig(thinking_budget=0)   # niente token spesi a "ragionare"
    return types.GenerateContentConfig(**kw)


def _call_model(model: str, contents, system: str):
    client = _get_client()
    if _thinking_ok.get(model, True):
        try:
            return client.models.generate_content(model=model, contents=contents, config=_make_cfg(system, True))
        except Exception as e:
            if getattr(e, "code", None) == 400 and "thinking" in str(e).lower():
                _thinking_ok[model] = False   # questo modello non permette di spegnerlo: riprova senza
            else:
                raise
    return client.models.generate_content(model=model, contents=contents, config=_make_cfg(system, False))


def _generate(contents, system: str, kind: str):
    """Chiama Gemini. Se il modello non c'è o non è disponibile, passa a uno che funziona."""
    global _good_for_key
    if _good_for_key != api_key():
        _good_model.clear(); _good_for_key = api_key()
    candidates = [_good_model[kind]] if kind in _good_model else [m for m in [model_name()] if m]
    tried, discovered = [], False
    while True:
        pending = [m for m in candidates if m not in tried]
        if not pending:
            if discovered:
                break
            discovered = True
            try:
                candidates += _ranked_models(kind)[:4]
            except Exception as e:
                raise AIError(f"Non riesco a leggere l'elenco dei modelli Gemini: {str(e)[:150]}")
            continue
        model = pending[0]
        tried.append(model)
        try:
            resp = _call_model(model, contents, system)
            _good_model[kind] = model
            return resp
        except Exception as e:
            if _is_unavailable(e):
                log.info("Modello %s non disponibile (%s): provo un altro", model, str(e)[:100])
                continue
            raise
    raise AIError("Nessun modello Gemini è disponibile con questa chiave"
                  + (f" (provati: {', '.join(tried)})." if tried else "."))


def _system_prompt() -> str:
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    return f"""Sei l'assistente di un'agenda personale. Oggi è {GIORNI[now.weekday()]} {now:%d/%m/%Y}, \
ore {now:%H:%M} (fuso {config.TIMEZONE}).

Estrai dal contenuto SOLO gli impegni con una data concreta: appuntamenti, riunioni, eventi, \
uscite didattiche, verifiche, scadenze (iscrizioni, pagamenti, consegne), viaggi, visite.
Ignora pubblicità, promozioni, newsletter generiche, avvisi di sicurezza e messaggi senza una data.
Il contenuto può essere testo, un PDF o un'immagine (screenshot di chat o mail, foto di avvisi, locandine, inviti): leggi anche il testo scritto nelle immagini e considera tutti i file ricevuti insieme.

Per ogni impegno restituisci:
- title: titolo breve e chiaro (max 60 caratteri). Per le scadenze inizia con "Scadenza: ".
- start_at: "YYYY-MM-DDTHH:MM" se c'è un orario, altrimenti "YYYY-MM-DD".
- end_at: fine nello stesso formato, oppure stringa vuota se non indicata.
- all_day: true se non c'è un orario preciso.
- location: luogo, oppure stringa vuota.
- summary: da 1 a 3 frasi con le informazioni davvero utili (cosa portare, costi, a chi è rivolto, \
come iscriversi, contatti importanti).
- confidence: da 0 a 1, quanto sei sicuro che sia un impegno reale con la data giusta. \
Usa meno di 0.7 se la data è ambigua o dedotta.

Se una data non ha l'anno, usa la prima occorrenza futura rispetto a oggi.
Se il documento è una circolare con più date, crea un impegno per ciascuna.
Non coprire un periodo con un unico impegno lungo se dentro ci sono appuntamenti separati: se il testo elenca date precise (per esempio consigli di classe il 12/01, il 19/01 e il 26/01) crea un impegno per ciascuna data. Usa un solo impegno con inizio e fine su più giorni soltanto per eventi davvero continui (vacanze, gite di più giorni) oppure quando il testo indica solo un periodo senza dire in quali giorni.
Se non ci sono impegni, restituisci una lista vuota. Non inventare informazioni."""


def _gemini_extract(text: str = "", files: list[tuple[str, bytes, str]] = ()) -> list[dict]:
    """Impegni trovati nel testo e/o nei file (PDF e immagini) da Gemini.

    files è una lista di (nome, contenuto, tipo), per esempio ("foto.png", b"...", "image/png")."""
    parts = []
    if text.strip():
        parts.append(types.Part.from_text(text=text[:MAX_TEXT_CHARS]))
    for name, data, mime in files:
        parts.append(types.Part.from_text(text=f"File allegato: {name}"))
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    if not parts:
        return []

    system = _system_prompt()
    kind = "vision" if files else "text"
    last_error = None
    for attempt in range(3):
        try:
            resp = _generate(parts, system, kind)
            if not resp.text:  # risposta bloccata o vuota
                return []
            return [e.model_dump() for e in Extraction.model_validate_json(resp.text).events]
        except AIError:
            raise
        except Exception as e:  # errori di rete, quota...
            last_error = e
            code = getattr(e, "code", None)
            if code in (400, 401, 403):  # inutile riprovare
                break
            if _is_quota(e):
                if attempt >= 1:  # un secondo tentativo per il limite al minuto, poi basta
                    raise QuotaError("La quota di Gemini è finita per ora.")
                time.sleep(8)
                continue
            time.sleep(3 * (attempt + 1))
    raise AIError(f"Errore Gemini: {last_error}")


# ---------------------------------------------------------------------------
# AI di riserva (API compatibile con OpenAI) e catena di ripiego
# ---------------------------------------------------------------------------
def fallback_settings() -> dict:
    """Impostazioni della seconda AI: quelle salvate dal sito vincono su quelle del file .env."""
    return {
        "url": (db.get_setting("fallback_url") or config.FALLBACK_AI_URL).rstrip("/"),
        "key": db.get_setting("fallback_key") or config.FALLBACK_AI_KEY,
        "model": db.get_setting("fallback_model") or config.FALLBACK_AI_MODEL,
    }


def has_fallback_ai() -> bool:
    fb = fallback_settings()
    return bool(fb["url"] and fb["model"])


def has_any_engine() -> bool:
    return has_key() or has_fallback_ai()


_JSON_SHAPE = (
    'Rispondi SOLO con un oggetto JSON valido, senza testo prima o dopo e senza ```, fatto così: '
    '{"events":[{"title":"","start_at":"","end_at":"","all_day":false,"location":"","summary":"","confidence":0.0}]}. '
    "Il contenuto che ricevi è testo estratto da PDF o immagini con un OCR: può avere errori di lettura "
    "(cifre sbagliate, parole spezzate), quindi abbassa la confidence quando una data o un'ora non è chiara."
)


def _chat(fb: dict, system: str, user: str, *, json_mode: bool = True, max_tokens: int = 2000) -> str:
    """Una richiesta a /chat/completions. Restituisce il testo della risposta."""
    import httpx
    headers = {"Content-Type": "application/json"}
    if fb["key"]:
        headers["Authorization"] = f"Bearer {fb['key']}"
    body = {
        "model": fb["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    try:
        r = httpx.post(fb["url"] + "/chat/completions", headers=headers, json=body, timeout=90)
    except httpx.HTTPError as e:
        raise AIError(f"AI di riserva non raggiungibile: {str(e)[:150]}")
    if r.status_code == 400 and json_mode:  # alcuni servizi non conoscono response_format: riprova senza
        return _chat(fb, system, user, json_mode=False, max_tokens=max_tokens)
    if r.status_code == 429:
        raise QuotaError("Anche l'AI di riserva ha finito la quota per ora.")
    if r.status_code in (401, 403):
        raise AIError("L'AI di riserva non accetta la chiave: controllala in Impostazioni.")
    if r.status_code == 404:
        raise AIError(f"L'AI di riserva non trova il modello «{fb['model']}»: controlla il nome in Impostazioni.")
    if r.status_code >= 400:
        raise AIError(f"AI di riserva: errore {r.status_code} ({r.text[:150]})")
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError):
        raise AIError("L'AI di riserva ha dato una risposta che non capisco.")


def _parse_events_json(content: str) -> list[dict]:
    """Legge il JSON anche se il modello ha aggiunto testo o ```."""
    content = re.sub(r"```(?:json)?", "", content).strip()
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise AIError("L'AI di riserva non ha risposto in JSON.")
    try:
        data = json.loads(content[start:end + 1])
    except ValueError:
        raise AIError("L'AI di riserva ha risposto con un JSON rovinato.")
    items = data.get("events", []) if isinstance(data, dict) else data
    cap = max(0.0, config.MIN_CONFIDENCE - 0.01)  # i modelli di riserva sono meno affidabili: sempre in revisione
    events = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        start_at = str(it.get("start_at") or "").strip()
        events.append({
            "title": str(it.get("title") or "").strip(),
            "start_at": start_at,
            "end_at": str(it.get("end_at") or "").strip(),
            "all_day": bool(it.get("all_day")) or "T" not in start_at.replace(" ", "T"),
            "location": str(it.get("location") or "").strip(),
            "summary": str(it.get("summary") or "").strip(),
            "confidence": min(cap, max(0.0, float(it.get("confidence") or 0.5))),
        })
    return events[:40]


def check_fallback(url: str, key: str, model: str) -> str:
    """Prova l'AI di riserva con una richiesta minima. Come check_key: \"\" se va, avviso se non verificabile."""
    try:
        _chat({"url": url.rstrip("/"), "key": key, "model": model}, "Rispondi solo: ok", "ok",
              json_mode=False, max_tokens=8)
        return ""
    except QuotaError:
        return "Salvata, ma adesso il servizio risponde che la quota è finita."
    except AIError as e:
        msg = str(e)
        if "non accetta la chiave" in msg or "non trova il modello" in msg:
            raise
        return f"Salvata, ma non sono riuscito a verificarla adesso ({msg[:150]})."


def _fallback_extract(fb: dict, text: str) -> list[dict]:
    content = _chat(fb, _system_prompt() + "\n\n" + _JSON_SHAPE, text[:MAX_TEXT_CHARS])
    return _parse_events_json(content)


def extract_events_ex(text: str = "", files: list[tuple[str, bytes, str]] = (), *,
                      mode: str = "auto", allow_rules: bool = False) -> tuple[list[dict], dict]:
    """Come extract_events, ma con la catena di ripiego. Restituisce (impegni, info).

    Per spendere pochi token:
      - i PDF col testo e (se c'è l'OCR) gli screenshot si leggono IN LOCALE e all'AI va solo testo;
      - del testo si tengono le righe con date, orari e parole da impegno (condense);
      - un file binario va a Gemini solo se non si riesce a leggerlo in locale.
    Catena: 1) Gemini → 2) AI di riserva (se configurata) → 3) lettura a regole (solo se allow_rules).
    Con mode="local" si usano SOLO OCR e regole: nulla esce dal computer.
    info = {"engine": "gemini" | "ai2" | "ocr" | "rules", "note": "..."}.
    """
    from . import fallback
    files = list(files)
    notes: list[str] = []
    error: AIError | None = None
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()

    # --- 0) lettura locale dei file (zero token)
    local_text, unread = "", files
    if files and mode != "local":
        local_text, unread = fallback.local_file_text(files)
    base_text = "\n\n".join(t for t in (text.strip(), local_text.strip()) if t)
    condensed = fallback.condense(base_text, 3500, today) if base_text else ""

    # --- 1) Gemini
    if mode != "local":
        if has_key():
            try:
                if condensed or unread:
                    return _gemini_extract(condensed, unread), {"engine": "gemini", "note": ""}
                return [], {"engine": "gemini", "note": ""}
            except AIError as e:
                error = e
                notes.append("Gemini ha finito la quota." if isinstance(e, QuotaError) else f"Gemini: {e}")
        else:
            error = AIError(NO_KEY_MESSAGE)

    fb = fallback_settings()
    use_ai2 = bool(fb["url"] and fb["model"]) and mode != "local"
    if not use_ai2 and not allow_rules:
        raise error or AIError("Nessun servizio AI configurato.")

    # --- 2) testo completo per le alternative: serve anche la lettura (OCR) dei file rimasti
    ocr_text = ""
    to_read = files if mode == "local" else unread
    if to_read:
        try:
            ocr_text, ocr_notes = fallback.extract_text(to_read)
            notes.extend(ocr_notes)
        except fallback.OCRUnavailable as e:
            why = f"{error} " if error else ""
            raise AIError(f"{why}Per leggere i file senza Gemini serve l'OCR. {e}")
    full_text = "\n\n".join(t for t in (text.strip(), local_text.strip(), ocr_text.strip()) if t)
    if mode == "local":
        full_text = "\n\n".join(t for t in (text.strip(), ocr_text.strip()) if t)
    if not full_text:
        if files:
            raise AIError("Non ho trovato testo leggibile nei file.")
        return [], {"engine": "rules", "note": " ".join(notes)}

    if use_ai2:
        try:
            events = _fallback_extract(fb, fallback.condense(full_text, 3500, today))
            notes.append(f"Ho usato l'AI di riserva ({fb['model']}): controlla gli impegni.")
            return events, {"engine": "ai2", "note": " ".join(notes)}
        except AIError as e:
            error = e
            notes.append(str(e))
    if allow_rules:
        name = files[0][0] if files else ""
        events = fallback.events_from_text(full_text, today, name)
        notes.append("Ho cercato date e orari senza AI: controlla bene gli impegni.")
        return events, {"engine": "ocr" if files else "rules", "note": " ".join(notes)}
    raise error or AIError("Nessun servizio AI ha risposto.")


def extract_events(text: str = "", files: list[tuple[str, bytes, str]] = ()) -> list[dict]:
    """Per la posta: Gemini e, se c'è, l'AI di riserva. Niente lettura a regole (riempirebbe l'agenda di falsi impegni)."""
    return extract_events_ex(text, files)[0]
