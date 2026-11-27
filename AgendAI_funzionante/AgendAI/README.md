# Agenda: calendario personale con AI

Legge la posta Gmail, trova gli impegni (anche nei PDF e negli screenshot, allegati o scelti a mano) con Gemini e li mette
in un calendario tuo, installabile sul telefono come app.

## Come funziona
- **Posta**: ogni 30 minuti l'app legge la casella via IMAP (senza segnare le mail come lette).
  Le newsletter vengono ignorate, a meno che abbiano un PDF allegato.
- **AI**: Gemini riceve testo, PDF e immagini (anche scansioni e screenshot) e restituisce titolo, data, luogo e riassunto.
- **Revisione**: se l'AI è sicura (soglia `MIN_CONFIDENCE`) l'impegno entra subito in calendario.
  Altrimenti, e per i PDF scelti a mano, va in "Da confermare".
- **Periodi di più giorni**: un impegno che copre più giorni (per esempio “consigli di classe dal 1° gennaio al 1° febbraio”) va sempre in "Da confermare". Il pulsante “Scegli i giorni” permette di tenere solo quelli che servono, ognuno diventa un impegno.
- **Manuale**: il pulsante + permette di inserire un impegno o analizzare PDF e screenshot (su computer anche incollandoli con Ctrl+V).

## 1. Prepara Google
1. Account Google → Sicurezza → attiva la **verifica in due passaggi**.
2. Vai su https://myaccount.google.com/apppasswords e crea una **password per le app** (16 caratteri).
3. In Gmail → Impostazioni → Inoltro e POP/IMAP controlla che IMAP sia attivo
   (se l'opzione non compare, è già attivo).
4. Crea una chiave su https://aistudio.google.com/apikey

## 2. Installa
Serve Python 3.10 o più recente.

```
cd calendario-ai
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # Windows: copy .env.example .env
```

Apri `.env` e compila `EMAIL_PASSWORD`, `GEMINI_API_KEY`, `APP_PASSWORD` (quella con cui entri nell'app)
e `SECRET_KEY` (una frase lunga a caso). Il file `.env` non va mai condiviso né caricato su GitHub.

## 3. Avvia
```
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Apri http://localhost:8000, entra con `APP_PASSWORD`, poi prova in ordine:
1. un impegno inserito a mano;
2. un PDF o uno screenshot (pulsante + → Analizza file);
3. "Controlla posta".

Non usare `--workers` maggiore di 1: ci sarebbero più controlli della posta in parallelo.

## 4. Usalo sul telefono
Per installare un'app web servono **HTTPS** oppure `localhost`. Per provarla subito dal PC:

```
cloudflared tunnel --url http://localhost:8000
```
(installa prima cloudflared da developers.cloudflare.com). Ti dà un indirizzo `https://...trycloudflare.com`
da aprire dal telefono. L'indirizzo cambia a ogni avvio: va bene per una dimostrazione. Per un indirizzo
fisso servono un tunnel con nome (serve un dominio) oppure un hosting sempre acceso (VPS, Fly.io con volume)
con HTTPS. Su hosting imposta anche `COOKIE_SECURE=1` e tieni la cartella `data/` su un disco persistente.

- **Android (Chrome)**: menu ⋮ → Installa app.
- **iPhone (Safari)**: Condividi → Aggiungi alla schermata Home.

## Cosa è cambiato (efficienza e comodità)
**Posta, ogni 30 minuti** (fisso; il pulsante "Controlla posta" la controlla subito, in background).
- Guarda solo le mail **nuove** dall'ultimo controllo (prima rileggeva ogni volta 7 giorni).
- Con Gmail esclude in partenza Promozioni, Social e Forum (si cambia con `GMAIL_SKIP_CATEGORIES` nel `.env`, vuoto = nessun filtro).
- Scarta senza scaricarle newsletter e messaggi automatici (a meno che abbiano un allegato).
- Non chiama l'AI se nella mail non c'è nessuna data, orario o parola da impegno, né per promemoria identici a uno già letto.
- All'AI manda solo le righe con date e orari (non la mail intera), con "pensiero" spento e un tetto alla risposta.
- I PDF col testo si leggono in locale: all'AI va solo testo, non il file.
- Se la quota finisce, le mail non lette restano in coda e si riprovano al giro dopo (prima, dopo 3 errori, venivano saltate per sempre).

**Screenshot e PDF**
- Gli **screenshot si leggono nel browser** (OCR Tesseract.js, lingua italiana): non servono installazioni e l'immagine non viene nemmeno caricata. La prima volta scarica il lettore (circa 15 MB, serve internet), poi resta in cache.
- **Analizza con AI**: mette il testo letto all'AI (poche centinaia di token). Se Gemini ha finito la quota passa all'AI di riserva (se l'hai configurata) e poi alla lettura a regole.
- **Solo OCR (senza AI)**: legge e cerca date e orari senza mandare nulla a nessun servizio. Capisce molto meno: tutto passa da "Da confermare".
- Se il lettore nel browser non parte (niente internet, foto HEIC dell'iPhone), il file va al server: prima OCR locale se installato, altrimenti a Gemini come immagine.

**Modello Gemini automatico.** `GEMINI_MODEL` nel `.env` può restare vuoto: il programma cerca un modello disponibile con la tua chiave (per il solo testo preferisce i "flash-lite", più economici). Se ne scrivi uno e non esiste, ne trova un altro da solo.

**Sicurezza.** Una `SECRET_KEY` corta (come 1234) nel `.env` viene ignorata e sostituita da una chiave forte salvata in `data/secret.key`: con una chiave corta si poteva entrare senza password. Cambia anche `APP_PASSWORD` (almeno 12 caratteri) se il sito è online. Il server ascolta solo su questo computer (il tunnel lo raggiunge lo stesso).

**Se qualcosa non funziona**: Impostazioni → "Controlla che tutto funzioni" (oppure `diagnosi.bat`) prova chiave, modello, posta e lettura dei file e dice cosa sistemare.

### AI di riserva (facoltativa)
Qualsiasi servizio compatibile con le API di OpenAI: Groq, Mistral, OpenRouter, oppure **Ollama sul tuo PC** (nessun limite, nessun dato fuori casa). Si imposta da Impostazioni. Riceve il *testo* già letto, quindi basta un modello di solo testo. Gli impegni che trova passano sempre da "Da confermare".

### OCR sul computer (facoltativo)
Serve solo se vuoi leggere i file anche quando il browser non può. Fai doppio clic su `installa-opzionali.bat` (installa i pacchetti uno alla volta) e, per gli screenshot, installa Tesseract per Windows con la lingua **Italian** (se non lo trova: `TESSERACT_CMD` nel `.env`). `OCR_ENGINE=tesseract` o `rapidocr` sceglie il motore.

### Chiave Gemini e account di posta sono due cose separate
La chiave Gemini (Google AI Studio) e l'account Gmail letto via IMAP non devono coincidere: la chiave può essere di qualsiasi account Google. La quota è del *progetto* Google, non della singola chiave: più chiavi dello stesso progetto condividono gli stessi limiti.

## Se qualcosa non va
- `Controllo posta non riuscito: ... AUTHENTICATIONFAILED`: password per le app errata, oppure verifica in
  due passaggi non attiva.
- Errore 404 sul modello: copia il nome aggiornato da https://aistudio.google.com e mettilo in `GEMINI_MODEL`.
- Errore 429: hai finito la quota di Gemini. Per i file ora c'è il ripiego (vedi sopra).
- Nessun impegno trovato: guarda la riga sotto la data (ultimo esito) e il terminale.

## Privacy
Con il piano gratuito di Gemini, Google può usare i contenuti inviati per migliorare i suoi prodotti
(controlla i termini aggiornati): se nelle mail ci sono dati personali di altre persone, valuta il piano a pagamento.
Tutti gli impegni restano nel file `data/calendario.db` sul tuo computer.

## Struttura
```
app/main.py     server, accesso e API
app/mailer.py   lettura Gmail (IMAP)
app/ai.py       estrazione impegni: Gemini, AI di riserva, catena di ripiego
app/fallback.py OCR sul computer, testo dei PDF, lettura a regole, riduzione del testo
app/diagnose.py controllo di chiave, modello, posta e lettura dei file
app/db.py       database SQLite
static/         interfaccia, manifest e service worker (PWA)
```

## Idee per andare oltre
Feed .ics per vedere gli impegni nel calendario del telefono, promemoria via Telegram,
accesso a Gmail con OAuth al posto della password per le app, regole per mittente (es. solo la scuola).
