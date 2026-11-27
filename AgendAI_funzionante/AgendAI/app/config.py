import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
_log = logging.getLogger("calendario.config")


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


EMAIL_ACCOUNT = env("EMAIL_ACCOUNT")
EMAIL_PASSWORD = env("EMAIL_PASSWORD").replace(" ", "")  # Google la mostra a gruppi di 4
IMAP_SERVER = env("IMAP_SERVER", "imap.gmail.com")

GEMINI_API_KEY = env("GEMINI_API_KEY")
# Vuoto = scelta automatica. Se scrivi un modello, viene provato per primo; se non esiste o non è
# disponibile con la tua chiave, il programma ne cerca da solo uno che funziona.
GEMINI_MODEL = env("GEMINI_MODEL")

APP_PASSWORD = env("APP_PASSWORD")
COOKIE_SECURE = env("COOKIE_SECURE", "0") == "1"


def _secret_key() -> str:
    """Chiave che firma i cookie di accesso. Una chiave corta (come 1234) si può indovinare e
    permetterebbe a chiunque di entrare senza password: la ignoriamo e ne teniamo una forte nel
    file data/secret.key, uguale a ogni riavvio."""
    given = env("SECRET_KEY")
    if len(given) >= 24:
        return given
    if given:
        _log.warning("SECRET_KEY nel file .env è troppo corta (meno di 24 caratteri): la ignoro.")
    path = Path(env("DB_PATH", "data/calendario.db")).parent / "secret.key"
    try:
        if path.exists() and len(path.read_text().strip()) >= 32:
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        return path.read_text().strip()
    except OSError:
        return secrets.token_hex(32)


SECRET_KEY = _secret_key()
if len(APP_PASSWORD) < 10:
    _log.warning("APP_PASSWORD è corta: se il sito è online chiunque può provare a indovinarla. "
                 "Usa almeno 12 caratteri.")

# Controllo automatico della posta: ogni 30 minuti (MAIL_CHECK_MINUTES solo se serve cambiarlo).
CHECK_EVERY_MINUTES = max(5, int(env("MAIL_CHECK_MINUTES", "30")))
LOOKBACK_DAYS = int(env("LOOKBACK_DAYS", "7"))   # solo al primo controllo di una casella
MIN_CONFIDENCE = float(env("MIN_CONFIDENCE", "0.7"))
TIMEZONE = env("TIMEZONE", "Europe/Rome")

DB_PATH = env("DB_PATH", "data/calendario.db")
MAX_PDF_MB = int(env("MAX_PDF_MB", "15"))

# AI di riserva (qualsiasi servizio compatibile con le API di OpenAI: Groq, Mistral, OpenRouter, Ollama...).
# Si può impostare anche dal sito, in Impostazioni.
FALLBACK_AI_URL = env("FALLBACK_AI_URL")      # es. https://api.groq.com/openai/v1
FALLBACK_AI_KEY = env("FALLBACK_AI_KEY")      # vuota per Ollama in locale
FALLBACK_AI_MODEL = env("FALLBACK_AI_MODEL")  # nome del modello, come lo scrive il provider
