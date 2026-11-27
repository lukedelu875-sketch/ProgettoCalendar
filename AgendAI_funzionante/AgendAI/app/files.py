"""Tipi di file che il programma sa leggere: PDF e immagini (screenshot, foto)."""
import os

ALLOWED = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}
MIMES = set(ALLOWED.values())


def mime_for(filename: str, content_type: str = ""):
    """Restituisce il tipo del file dal nome (o, in mancanza, dal tipo dichiarato), oppure None."""
    ext = os.path.splitext((filename or "").lower())[1]
    if ext in ALLOWED:
        return ALLOWED[ext]
    ct = (content_type or "").lower().split(";")[0].strip()
    return ct if ct in MIMES else None
