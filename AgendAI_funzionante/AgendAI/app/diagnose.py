"""Controllo di tutto quello che serve al programma, con messaggi chiari.

Si usa dal sito (Impostazioni → "Controlla che tutto funzioni") o dal terminale:
    python -m app.diagnose
Ogni voce ha un esito: ok=True (va), ok=False (da sistemare) o ok=None (avviso)."""
import importlib.util

from . import ai, config, db, fallback, mailer


def _item(name: str, ok, detail: str, fix: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail, "fix": fix}


def run(deep: bool = True) -> list[dict]:
    """deep=True fa anche una piccola richiesta vera a Gemini e un accesso vero alla posta."""
    out = []

    # --- sicurezza (conta soprattutto se il sito è online)
    out.append(_item("Password del sito", len(config.APP_PASSWORD) >= 10 or None,
                     f"{len(config.APP_PASSWORD)} caratteri.",
                     "" if len(config.APP_PASSWORD) >= 10 else "È corta: se il sito è online chiunque può provare a indovinarla. Metti almeno 12 caratteri in APP_PASSWORD nel file .env."))

    # --- database
    try:
        n = db.count_pending()
        out.append(_item("Archivio degli impegni", True, f"Funziona ({n} da confermare)."))
    except Exception as e:
        out.append(_item("Archivio degli impegni", False, str(e)[:200], "Controlla che la cartella data sia scrivibile."))

    # --- Gemini
    if not ai.has_key():
        out.append(_item("Gemini", False, "Manca la chiave.", "Incollala in Impostazioni (o in GEMINI_API_KEY nel file .env)."))
    elif deep:
        try:
            events = ai._gemini_extract("Riunione di prova il 12 gennaio 2027 alle 15:30 in aula magna.", [])
            model = ai._good_model.get("text", "?")
            out.append(_item("Gemini", True, f"Risponde (modello «{model}»; impegni trovati nella prova: {len(events)})."))
        except ai.QuotaError:
            out.append(_item("Gemini", None, "La chiave è valida ma la quota è finita per ora.",
                             "Riprova più tardi, oppure configura l'AI di riserva in Impostazioni."))
        except ai.AIError as e:
            out.append(_item("Gemini", False, str(e)[:250], "Controlla la chiave su aistudio.google.com/apikey."))
        except Exception as e:
            out.append(_item("Gemini", False, str(e)[:250], "Controlla la chiave su aistudio.google.com/apikey."))
    else:
        out.append(_item("Gemini", True, "Chiave presente."))

    # --- AI di riserva
    if ai.has_fallback_ai():
        fb = ai.fallback_settings()
        if deep:
            try:
                warn = ai.check_fallback(fb["url"], fb["key"], fb["model"])
                out.append(_item("AI di riserva", True if not warn else None, warn or f"Risponde ({fb['model']})."))
            except ai.AIError as e:
                out.append(_item("AI di riserva", False, str(e), "Correggi chiave o modello in Impostazioni."))
        else:
            out.append(_item("AI di riserva", True, f"Configurata ({fb['model']})."))
    else:
        out.append(_item("AI di riserva", None, "Non configurata (facoltativa).",
                         "Serve solo se Gemini finisce la quota."))

    # --- lettura dei file senza AI
    ocr = fallback.ocr_status()
    pdf_ok = bool(importlib.util.find_spec("pypdf") or importlib.util.find_spec("fitz"))
    out.append(_item("Testo dei PDF in locale", True if pdf_ok else None,
                     "Pronto." if pdf_ok else "Manca pypdf: i PDF vanno a Gemini come file (più token).",
                     "" if pdf_ok else "Esegui: pip install pypdf"))
    out.append(_item("OCR sul computer (facoltativo)", True if ocr["available"] else None,
                     f"Pronto ({ocr['engine']})." if ocr["available"] else
                     "Non installato: gli screenshot si leggono comunque, direttamente nel browser.",
                     ""))

    # --- posta
    accounts = db.list_accounts(enabled_only=True)
    if not accounts:
        out.append(_item("Posta", None, "Nessun account collegato.", "Aggiungilo in Impostazioni."))
    for acc in accounts:
        label = acc["label"] or acc["email"]
        if not deep:
            out.append(_item(f"Posta: {label}", True if not acc["last_error"] else False,
                             acc["last_error"] or "Ultimo controllo senza errori."))
            continue
        try:
            mailer.test_login(acc["email"], acc["password"], acc["imap_server"])
            out.append(_item(f"Posta: {label}", True, "Accesso riuscito."))
        except ValueError as e:
            out.append(_item(f"Posta: {label}", False, str(e),
                             "Serve una «Password per le app» di Google (con la verifica in due passaggi attiva) e IMAP abilitato in Gmail."))

    out.append(_item("Controllo automatico", True, f"Ogni {config.CHECK_EVERY_MINUTES} minuti, solo mail nuove."))
    return out


if __name__ == "__main__":
    db.init()
    for it in run():
        mark = {True: "[ OK ]", False: "[ !! ]", None: "[ .. ]"}[it["ok"]]
        print(f"{mark} {it['name']}: {it['detail']}")
        if it["fix"] and it["ok"] is not True:
            print(f"        -> {it['fix']}")
