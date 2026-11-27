import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL DEFAULT '',
    all_day     INTEGER NOT NULL DEFAULT 0,
    location    TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'manual',
    source_ref  TEXT NOT NULL DEFAULT '',
    confidence  REAL NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'confirmed',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message_failures (
    message_id TEXT PRIMARY KEY,
    count      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mail_accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL DEFAULT '',
    email       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password    TEXT NOT NULL,
    imap_server TEXT NOT NULL DEFAULT 'imap.gmail.com',
    enabled     INTEGER NOT NULL DEFAULT 1,
    last_check  TEXT NOT NULL DEFAULT '',
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
"""

FIELDS = ("title", "summary", "start_at", "end_at", "all_day", "location")
ACCOUNT_FIELDS = ("label", "password", "imap_server", "enabled", "last_error")


@contextmanager
def db():
    folder = os.path.dirname(config.DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA)
        # Una sola volta: l'account scritto nel file .env diventa il primo account del sito.
        seeded = con.execute("SELECT 1 FROM meta WHERE key='env_account_seeded'").fetchone()
        if not seeded:
            if config.EMAIL_ACCOUNT and config.EMAIL_PASSWORD:
                con.execute(
                    """INSERT OR IGNORE INTO mail_accounts
                       (label, email, password, imap_server, created_at) VALUES (?,?,?,?,?)""",
                    ("", config.EMAIL_ACCOUNT.lower(), config.EMAIL_PASSWORD, config.IMAP_SERVER,
                     datetime.now().isoformat(timespec="seconds")),
                )
            con.execute("INSERT OR REPLACE INTO meta VALUES ('env_account_seeded', '1')")


def _norm(value: str, all_day: bool) -> str:
    value = (value or "").strip().replace(" ", "T")
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value[:16] if "T" in value else value[:10])
    except ValueError:
        raise ValueError(f"Data non valida: {value}")
    return dt.strftime("%Y-%m-%d") if all_day else dt.strftime("%Y-%m-%dT%H:%M")


def clean_event(raw: dict) -> dict:
    """Controlla e normalizza un impegno (da form, da AI o da PDF)."""
    title = (raw.get("title") or "").strip()[:120]
    if not title:
        raise ValueError("Il titolo è obbligatorio.")
    start = (raw.get("start_at") or "").strip().replace(" ", "T")
    if not start:
        raise ValueError("La data di inizio è obbligatoria.")
    all_day = bool(raw.get("all_day")) or "T" not in start
    start_n = _norm(start, all_day)
    end_n = _norm(raw.get("end_at") or "", all_day)
    if end_n and (end_n <= start_n):
        end_n = ""
    return {
        "title": title,
        "summary": (raw.get("summary") or "").strip()[:2000],
        "start_at": start_n,
        "end_at": end_n,
        "all_day": 1 if all_day else 0,
        "location": (raw.get("location") or "").strip()[:200],
    }


def add_event(ev: dict, *, source="manual", source_ref="", confidence=1.0,
              status="confirmed", dedupe=True):
    """Salva un impegno già pulito. Restituisce l'id, oppure None se era un doppione."""
    with db() as con:
        if dedupe:
            dup = con.execute(
                "SELECT id FROM events WHERE lower(title) = lower(?) AND start_at = ?",
                (ev["title"], ev["start_at"]),
            ).fetchone()
            if dup:
                return None
        cur = con.execute(
            """INSERT INTO events (title, summary, start_at, end_at, all_day, location,
                                   source, source_ref, confidence, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ev["title"], ev["summary"], ev["start_at"], ev["end_at"], ev["all_day"],
             ev["location"], source, source_ref[:300], float(confidence), status,
             datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def list_events(status: str = "confirmed"):
    with db() as con:
        rows = con.execute(
            "SELECT * FROM events WHERE status = ? ORDER BY start_at", (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_event(event_id: int):
    with db() as con:
        row = con.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def update_event(event_id: int, ev: dict, status: str):
    with db() as con:
        con.execute(
            """UPDATE events SET title=?, summary=?, start_at=?, end_at=?, all_day=?,
                                 location=?, status=? WHERE id=?""",
            (ev["title"], ev["summary"], ev["start_at"], ev["end_at"], ev["all_day"],
             ev["location"], status, event_id),
        )


def delete_event(event_id: int) -> bool:
    with db() as con:
        return con.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0


def split_event(event_id: int, dates: list[str]):
    """Sostituisce un impegno di più giorni con un impegno per ciascun giorno scelto.

    Restituisce (creati, già presenti), oppure None se l'impegno non esiste.
    """
    ev = get_event(event_id)
    if ev is None:
        return None
    first, last = ev["start_at"][:10], (ev["end_at"] or ev["start_at"])[:10]
    chosen = sorted(set(dates))
    if not chosen:
        raise ValueError("Scegli almeno un giorno.")
    if len(chosen) > 93:
        raise ValueError("Troppi giorni: scegline al massimo 93.")
    for d in chosen:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Data non valida: {d}")
        if not (first <= d <= last):
            raise ValueError("Hai scelto un giorno fuori dal periodo dell'impegno.")
    timed = not ev["all_day"]
    start_time = ev["start_at"][11:16] if timed else ""
    end_time = ev["end_at"][11:16] if timed and "T" in ev["end_at"] else ""
    created = skipped = 0
    for d in chosen:
        raw = {
            "title": ev["title"], "summary": ev["summary"], "location": ev["location"],
            "all_day": not timed,
            "start_at": f"{d}T{start_time}" if timed else d,
            "end_at": f"{d}T{end_time}" if end_time else "",
        }
        if add_event(clean_event(raw), source=ev["source"], source_ref=ev["source_ref"],
                     confidence=ev["confidence"], status="confirmed"):
            created += 1
        else:
            skipped += 1
    delete_event(event_id)
    return created, skipped


def count_pending() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM events WHERE status='pending'").fetchone()[0]


def is_processed(message_id: str) -> bool:
    with db() as con:
        return con.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
        ).fetchone() is not None


def mark_processed(message_id: str):
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO processed_messages VALUES (?, ?)",
            (message_id, datetime.now().isoformat(timespec="seconds")),
        )


def bump_failure(message_id: str) -> int:
    with db() as con:
        con.execute(
            """INSERT INTO message_failures VALUES (?, 1)
               ON CONFLICT(message_id) DO UPDATE SET count = count + 1""",
            (message_id,),
        )
        return con.execute(
            "SELECT count FROM message_failures WHERE message_id = ?", (message_id,)
        ).fetchone()[0]


def set_meta(key: str, value: str):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def get_meta(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


# ---------- impostazioni (chiave Gemini, modello) ----------
def get_setting(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))


def delete_setting(key: str):
    with db() as con:
        con.execute("DELETE FROM settings WHERE key = ?", (key,))


# ---------- account di posta ----------
def list_accounts(enabled_only: bool = False):
    sql = "SELECT * FROM mail_accounts"
    if enabled_only:
        sql += " WHERE enabled = 1"
    with db() as con:
        rows = con.execute(sql + " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_account(account_id: int):
    with db() as con:
        row = con.execute("SELECT * FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def add_account(label: str, email: str, password: str, imap_server: str) -> int:
    """Solleva sqlite3.IntegrityError se l'indirizzo è già collegato."""
    with db() as con:
        cur = con.execute(
            """INSERT INTO mail_accounts (label, email, password, imap_server, created_at)
               VALUES (?,?,?,?,?)""",
            (label, email, password, imap_server, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def update_account(account_id: int, changes: dict) -> bool:
    changes = {k: v for k, v in changes.items() if k in ACCOUNT_FIELDS}
    if not changes:
        return True
    cols = ", ".join(f"{k} = ?" for k in changes)
    with db() as con:
        return con.execute(
            f"UPDATE mail_accounts SET {cols} WHERE id = ?", (*changes.values(), account_id)
        ).rowcount > 0


def set_account_status(account_id: int, error: str = ""):
    with db() as con:
        con.execute(
            "UPDATE mail_accounts SET last_check = ?, last_error = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), error[:300], account_id),
        )


def delete_account(account_id: int) -> bool:
    with db() as con:
        return con.execute("DELETE FROM mail_accounts WHERE id = ?", (account_id,)).rowcount > 0
