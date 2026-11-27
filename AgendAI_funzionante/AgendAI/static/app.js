'use strict';

const $ = (s) => document.querySelector(s);
const pad = (n) => String(n).padStart(2, '0');
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parseYmd = (s) => { const [y, m, d] = s.slice(0, 10).split('-').map(Number); return new Date(y, m - 1, d); };
const fmtLong = new Intl.DateTimeFormat('it-IT', { weekday: 'long', day: 'numeric', month: 'long' });
const fmtShort = new Intl.DateTimeFormat('it-IT', { day: 'numeric', month: 'short' });
const fmtMonth = new Intl.DateTimeFormat('it-IT', { month: 'long', year: 'numeric' });
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

const now = new Date();
const state = {
  confirmed: [], pending: [], status: null, view: 'cal',
  cursor: new Date(now.getFullYear(), now.getMonth(), 1),
  selected: ymd(now), editing: null,
};

// Crea elementi DOM senza mai usare innerHTML: titoli e riassunti arrivano da mail e PDF.
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  kids.flat().forEach((c) => c != null && n.append(c));
  return n;
}

async function api(path, { method = 'GET', body } = {}) {
  const init = { method, credentials: 'same-origin', headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body) { init.body = JSON.stringify(body); init.headers['Content-Type'] = 'application/json'; }
  const res = await fetch(path, init);
  if (res.status === 401) { location.href = '/login'; throw new Error('Sessione scaduta.'); }
  if (!res.ok) {
    let msg = 'Qualcosa è andato storto. Riprova.';
    try { const j = await res.json(); msg = typeof j.detail === 'string' ? j.detail : 'Controlla i campi inseriti.'; } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}

// ---------- logica dei dati ----------
const lastDay = (ev) => (ev.end_at || ev.start_at).slice(0, 10);
const occursOn = (ev, day) => ev.start_at.slice(0, 10) <= day && day <= lastDay(ev);
const isMulti = (ev) => !!ev.end_at && lastDay(ev) > ev.start_at.slice(0, 10);
const spanDays = (ev) => Math.round((parseYmd(lastDay(ev)) - parseYmd(ev.start_at)) / 86400000) + 1;
const byTime = (a, b) => (b.all_day - a.all_day) || a.start_at.localeCompare(b.start_at);

function whenLabel(ev, day) {
  if (ev.all_day) return 'Tutto il giorno';
  const s = ev.start_at, e = ev.end_at;
  if (day === s.slice(0, 10)) return e && e.slice(0, 10) === day ? `${s.slice(11, 16)}–${e.slice(11, 16)}` : s.slice(11, 16);
  if (e && day === e.slice(0, 10)) return `fino alle ${e.slice(11, 16)}`;
  return 'Tutto il giorno';
}

// ---------- disegno ----------
function sourceLabel(ev) {
  if (ev.source === 'mail') return `Mail: ${ev.source_ref}`;
  if (ev.source === 'pdf') return `PDF: ${ev.source_ref}`;
  if (ev.source === 'image') return `Immagine: ${ev.source_ref}`;
  return null;
}

function eventCard(ev, day, review = false) {
  const pickable = review && isMulti(ev) && spanDays(ev) <= 93;
  const minConf = state.status ? state.status.min_confidence : 0.7;
  const src = sourceLabel(ev);
  const when = el('div', { class: 'when' });
  if (review) when.append(el('div', { text: fmtShort.format(parseYmd(ev.start_at)) }));
  when.append(el('div', { text: whenLabel(ev, day) }));

  const body = el('div', {},
    el('h4', { text: ev.title }),
    ev.location ? el('p', { text: ev.location }) : null,
    ev.summary ? el('p', { class: 'sum', text: ev.summary }) : null,
    src ? el('p', { text: src }) : null,
    pickable ? el('p', { text: `Periodo di ${plural(spanDays(ev), 'giorno', 'giorni')}, fino al ${fmtShort.format(parseYmd(lastDay(ev)))}: scegli i giorni che ti servono.` }) : null,
    review && ev.confidence < minConf ? el('p', { class: 'warn', text: 'Poco sicuro: controlla la data' }) : null,
    review ? el('div', { class: 'review' },
      el('button', { class: 'btn primary', type: 'button', text: 'Conferma', onclick: () => confirmEvent(ev) }),
      pickable ? el('button', { class: 'btn', type: 'button', text: 'Scegli i giorni', onclick: () => openDays(ev) }) : null,
      el('button', { class: 'btn', type: 'button', text: 'Modifica', onclick: () => openDialog(ev) }),
      el('button', { class: 'btn danger', type: 'button', text: 'Scarta', onclick: () => discardEvent(ev) }),
    ) : null,
  );
  const card = el('article', { class: `ev ${ev.source}` }, when, body);
  if (!review) {
    card.setAttribute('tabindex', '0');
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Modifica ${ev.title}`);
    card.addEventListener('click', () => openDialog(ev));
    card.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDialog(ev); } });
  }
  return card;
}

function renderHeader() {
  $('#todayLabel').textContent = fmtLong.format(new Date());
  const s = state.status, line = $('#statusLine');
  line.classList.remove('err');
  if (!s) { line.textContent = ''; return; }
  if (!s.mail_enabled) line.textContent = 'Posta non collegata: puoi inserire gli impegni a mano.';
  else if (s.last_error) { line.textContent = `Controllo posta non riuscito: ${s.last_error}`; line.classList.add('err'); }
  else if (s.last_check) line.textContent = `Posta controllata alle ${s.last_check.slice(11, 16)}. ${s.last_result}.`;
  else line.textContent = 'Primo controllo della posta in arrivo.';
  const badge = $('#badge');
  badge.hidden = state.pending.length === 0;
  badge.textContent = state.pending.length;
}

function renderMonth() {
  const y = state.cursor.getFullYear(), m = state.cursor.getMonth();
  $('#monthTitle').textContent = fmtMonth.format(state.cursor);
  const offset = (new Date(y, m, 1).getDay() + 6) % 7;      // la settimana parte da lunedì
  const weeks = Math.ceil((offset + new Date(y, m + 1, 0).getDate()) / 7);
  const today = ymd(new Date());
  const grid = $('#grid'); grid.replaceChildren();
  for (let i = 0; i < weeks * 7; i++) {
    const d = new Date(y, m, 1 - offset + i), key = ymd(d);
    const evs = state.confirmed.filter((ev) => occursOn(ev, key));
    const cls = ['day', d.getMonth() !== m ? 'out' : '', key === today ? 'today' : '', key === state.selected ? 'sel' : ''];
    grid.append(el('button', {
      class: cls.filter(Boolean).join(' '), type: 'button',
      'aria-label': `${fmtLong.format(d)}, ${plural(evs.length, 'impegno', 'impegni')}`,
      'aria-pressed': String(key === state.selected),
      onclick: () => {
        state.selected = key;
        if (d.getMonth() !== m) state.cursor = new Date(d.getFullYear(), d.getMonth(), 1);
        renderMonth();
      },
    }, el('span', { class: 'n', text: d.getDate() }),
       el('span', { class: 'dots' }, evs.slice(0, 3).map((ev) => el('span', { class: `dot ${ev.source}` })))));
  }
  const list = $('#dayList'); list.replaceChildren();
  const dayEvents = state.confirmed.filter((ev) => occursOn(ev, state.selected)).sort(byTime);
  list.append(el('h3', { class: 'daytitle', text: fmtLong.format(parseYmd(state.selected)) }));
  if (!dayEvents.length) list.append(el('p', { class: 'empty', text: 'Nessun impegno questo giorno. Tocca + per aggiungerne uno.' }));
  dayEvents.forEach((ev) => list.append(eventCard(ev, state.selected)));
}

function renderNext() {
  const list = $('#nextList'); list.replaceChildren();
  const today = ymd(new Date()), limit = new Date(); limit.setDate(limit.getDate() + 90);
  const groups = new Map();
  state.confirmed
    .filter((ev) => lastDay(ev) >= today && ev.start_at.slice(0, 10) <= ymd(limit))
    .sort((a, b) => a.start_at.localeCompare(b.start_at) || byTime(a, b))
    .forEach((ev) => {
      const day = ev.start_at.slice(0, 10) < today ? today : ev.start_at.slice(0, 10);
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day).push(ev);
    });
  if (!groups.size) { list.append(el('p', { class: 'empty', text: 'Niente in programma nei prossimi 90 giorni.' })); return; }
  const tomorrow = new Date(); tomorrow.setDate(tomorrow.getDate() + 1);
  for (const [day, evs] of groups) {
    const title = day === today ? 'Oggi' : day === ymd(tomorrow) ? 'Domani' : fmtLong.format(parseYmd(day));
    list.append(el('h3', { class: 'daytitle', text: title }));
    evs.sort(byTime).forEach((ev) => list.append(eventCard(ev, day)));
  }
}

function renderPending() {
  const list = $('#pendingList'); list.replaceChildren();
  if (!state.pending.length) {
    list.append(el('p', { class: 'empty', text: 'Tutto controllato. Qui arrivano gli impegni di cui l’AI non è sicura e quelli trovati nei PDF e negli screenshot che analizzi.' }));
    return;
  }
  state.pending.forEach((ev) => list.append(eventCard(ev, ev.start_at.slice(0, 10), true)));
}

const renderAll = () => { renderHeader(); renderMonth(); renderNext(); renderPending(); };

function showView(name) {
  state.view = name;
  for (const v of ['cal', 'next', 'pending']) $(`#view-${v}`).hidden = v !== name;
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.dataset.view === name ? b.setAttribute('aria-current', 'page') : b.removeAttribute('aria-current'));
}

async function load() {
  const [confirmed, pending, status] = await Promise.all([
    api('/api/events?status=confirmed'), api('/api/events?status=pending'), api('/api/status'),
  ]);
  Object.assign(state, { confirmed, pending, status });
  renderAll();
}

// ---------- azioni ----------
async function confirmEvent(ev) {
  try { await api(`/api/events/${ev.id}`, { method: 'PATCH', body: { status: 'confirmed' } }); await load(); toast('Impegno confermato.'); }
  catch (e) { toast(e.message); }
}
async function discardEvent(ev) {
  try { await api(`/api/events/${ev.id}`, { method: 'DELETE' }); await load(); toast('Impegno scartato.'); }
  catch (e) { toast(e.message); }
}

const dlg = $('#dlg');
function openDialog(ev = null) {
  state.editing = ev;
  $('#dlgTitle').textContent = ev ? 'Modifica impegno' : 'Nuovo impegno';
  $('#btnSave').textContent = !ev ? 'Salva impegno' : ev.status === 'pending' ? 'Salva e conferma' : 'Salva modifiche';
  $('#btnDelete').hidden = !ev;
  $('#pdfBox').hidden = !!ev;
  $('#formMsg').textContent = '';
  $('#f-title').value = ev ? ev.title : '';
  $('#f-date').value = ev ? ev.start_at.slice(0, 10) : state.selected;
  $('#f-time').value = ev && !ev.all_day ? ev.start_at.slice(11, 16) : '';
  const endDay = ev && ev.end_at ? ev.end_at.slice(0, 10) : '';
  $('#f-end-date').value = ev && endDay !== ev.start_at.slice(0, 10) ? endDay : '';
  $('#f-end-time').value = ev && !ev.all_day && ev.end_at ? ev.end_at.slice(11, 16) : '';
  $('#f-location').value = ev ? ev.location : '';
  $('#f-summary').value = ev ? ev.summary : '';
  $('#f-pdf').value = '';
  $('#pdfMsg').textContent = 'Gli impegni trovati arrivano in “Da confermare” per un controllo. Su computer puoi incollare uno screenshot con Ctrl+V.';
  dlg.showModal();
  $('#f-title').focus();
}

$('#form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const date = $('#f-date').value, time = $('#f-time').value;
  const endDate = $('#f-end-date').value, endTime = $('#f-end-time').value;
  const allDay = !time;
  let end = '';
  if (allDay) end = endDate;
  else if (endDate || endTime) end = `${endDate || date}T${endTime || time}`;
  const body = {
    title: $('#f-title').value, start_at: allDay ? date : `${date}T${time}`, end_at: end,
    all_day: allDay, location: $('#f-location').value, summary: $('#f-summary').value,
  };
  try {
    if (state.editing) {
      if (state.editing.status === 'pending') body.status = 'confirmed';
      await api(`/api/events/${state.editing.id}`, { method: 'PATCH', body });
    } else {
      await api('/api/events', { method: 'POST', body });
    }
    state.selected = date;
    state.cursor = new Date(parseYmd(date).getFullYear(), parseYmd(date).getMonth(), 1);
    dlg.close(); await load(); toast('Impegno salvato.');
  } catch (err) { $('#formMsg').textContent = err.message; }
});

$('#btnCancel').addEventListener('click', () => dlg.close());
$('#btnDelete').addEventListener('click', async () => {
  if (!state.editing || !confirm('Eliminare questo impegno?')) return;
  try { await api(`/api/events/${state.editing.id}`, { method: 'DELETE' }); dlg.close(); await load(); toast('Impegno eliminato.'); }
  catch (e) { $('#formMsg').textContent = e.message; }
});

// ---- OCR nel browser (Tesseract.js): lo screenshot si legge sul telefono/computer, senza caricarlo
// e senza spendere token. Il lettore (circa 15 MB) si scarica la prima volta e poi resta in cache.
let ocrWorkerPromise = null;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src; el.onload = resolve; el.onerror = () => reject(new Error('script non raggiungibile'));
    document.head.appendChild(el);
  });
}
async function getOcrWorker(onStatus) {
  if (!ocrWorkerPromise) {
    ocrWorkerPromise = (async () => {
      if (!window.Tesseract) {
        onStatus('Scarico il lettore di testo (solo la prima volta)…');
        await loadScript('https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js');
      }
      return window.Tesseract.createWorker('ita', 1, {
        logger: (m) => { if (m.status === 'recognizing text') onStatus(`Leggo il testo… ${Math.round(m.progress * 100)}%`); },
      });
    })().catch((e) => { ocrWorkerPromise = null; throw e; });
  }
  return ocrWorkerPromise;
}
async function ocrInBrowser(file, onStatus) {
  const worker = await getOcrWorker(onStatus);
  const { data } = await worker.recognize(file);
  return (data.text || '').trim();
}

// mode: 'auto' = Gemini (con poco testo) e, se non va, AI di riserva / regole · 'local' = niente AI
async function analyzeFiles(mode) {
  const files = [...$('#f-pdf').files], msg = $('#pdfMsg');
  const btns = [$('#btnPdf'), $('#btnOcr')];
  if (!files.length) { msg.textContent = 'Scegli prima un PDF o uno screenshot.'; return; }
  btns.forEach((b) => { b.disabled = true; });
  try {
    // 1) gli screenshot si leggono nel browser; i PDF e le immagini illeggibili vanno al server
    const texts = [], upload = [];
    for (const f of files) {
      const isImage = f.type.startsWith('image/') && !/heic|heif/i.test(f.type + f.name);
      if (!isImage) { upload.push(f); continue; }
      try {
        msg.textContent = `Leggo ${f.name}…`;
        const t = await ocrInBrowser(f, (m) => { msg.textContent = m; });
        if (t.length >= 15) texts.push(`[${f.name}]\n${t}`); else upload.push(f);
      } catch (e) { upload.push(f); }  // lettore non scaricabile o immagine strana: ci pensa il server
    }
    const totals = { found: 0, added: 0, notes: [] };
    const add = (r) => { totals.found += r.found; totals.added += r.added; if (r.note && r.engine !== 'gemini') totals.notes.push(r.note); };
    if (texts.length) {
      msg.textContent = mode === 'local' ? 'Cerco date e orari…' : 'Cerco gli impegni nel testo…';
      add(await api('/api/text', { method: 'POST', body: { text: texts.join('\n\n'), name: files.map((f) => f.name).join(', '), mode } }));
    }
    if (upload.length) {
      msg.textContent = mode === 'local' ? 'Leggo i file con l’OCR del computer…' : 'Gemini sta leggendo i file…';
      const fd = new FormData(); upload.forEach((f) => fd.append('files', f)); fd.append('mode', mode);
      add(await api('/api/file', { method: 'POST', body: fd }));
    }
    dlg.close(); await load();
    const how = totals.notes.length ? ` ${[...new Set(totals.notes)].join(' ')}` : '';
    if (totals.added) { toast(`Trovati ${plural(totals.added, 'impegno', 'impegni')}. Controlla e conferma.${how}`); showView('pending'); }
    else toast((totals.found ? 'Gli impegni trovati sono già in agenda.' : 'Nessun impegno trovato nei file.') + how);
  } catch (e) {
    msg.textContent = e.message;
    if (mode === 'auto') msg.textContent += ' Puoi provare “Solo OCR (senza AI)”.';
  } finally { btns.forEach((b) => { b.disabled = false; }); }
}
$('#btnPdf').addEventListener('click', () => analyzeFiles('auto'));
$('#btnOcr').addEventListener('click', () => analyzeFiles('local'));

// su computer: incolla uno screenshot con Ctrl+V mentre la finestra è aperta
dlg.addEventListener('paste', (e) => {
  const imgs = [...((e.clipboardData && e.clipboardData.files) || [])].filter((f) => f.type.startsWith('image/'));
  if (!imgs.length || state.editing) return;
  e.preventDefault();
  const dt = new DataTransfer(); imgs.forEach((f) => dt.items.add(f));
  $('#f-pdf').files = dt.files;
  $('#pdfMsg').textContent = `Screenshot incollato (${plural(imgs.length, 'immagine', 'immagini')}). Premi “Analizza con AI” o “Solo OCR”.`;
});

// ---------- scelta dei giorni di un periodo ----------
const dlgD = $('#dlgDays');
const fmtMonthShort = new Intl.DateTimeFormat('it-IT', { month: 'short' });
const daysState = { ev: null, first: '', last: '', picked: new Set() };

function rangeKeys() {
  const keys = [];
  for (let d = parseYmd(daysState.first); ymd(d) <= daysState.last; d.setDate(d.getDate() + 1)) keys.push(ymd(d));
  return keys;
}

function updateDaysUi() {
  const n = daysState.picked.size;
  $('#daysCount').textContent = n ? plural(n, 'giorno scelto', 'giorni scelti') + '.' : 'Nessun giorno scelto.';
  $('#btnDaysSave').disabled = n === 0;
}

function renderDays(focusKey) {
  const { first, last, picked } = daysState;
  const start = parseYmd(first), end = parseYmd(last);
  const gridStart = new Date(start.getFullYear(), start.getMonth(), start.getDate() - ((start.getDay() + 6) % 7));
  const gridEnd = new Date(end.getFullYear(), end.getMonth(), end.getDate() + (6 - ((end.getDay() + 6) % 7)));
  const grid = $('#daysGrid'); grid.replaceChildren();
  for (let d = new Date(gridStart); d <= gridEnd; d.setDate(d.getDate() + 1)) {
    const day = new Date(d), key = ymd(day);
    if (key < first || key > last) { grid.append(el('span', { class: 'day' })); continue; }   // fuori dal periodo
    const monthLabel = key === first || day.getDate() === 1 ? fmtMonthShort.format(day) : '';
    grid.append(el('button', {
      class: `day avail${picked.has(key) ? ' pick' : ''}`, type: 'button', 'data-key': key,
      'aria-pressed': String(picked.has(key)), 'aria-label': fmtLong.format(day),
      onclick: () => { picked.has(key) ? picked.delete(key) : picked.add(key); renderDays(key); },
    }, el('span', { class: 'n', text: day.getDate() }), el('span', { class: 'mo', text: monthLabel })));
  }
  if (focusKey) { const b = grid.querySelector(`[data-key="${focusKey}"]`); if (b) b.focus(); }
  updateDaysUi();
}

function openDays(ev) {
  Object.assign(daysState, { ev, first: ev.start_at.slice(0, 10), last: lastDay(ev), picked: new Set() });
  $('#daysInfo').textContent = `${ev.title}, dal ${fmtShort.format(parseYmd(daysState.first))} al ${fmtShort.format(parseYmd(daysState.last))}. Tocca i giorni che ti servono: ognuno diventa un impegno.`;
  $('#daysMsg').textContent = '';
  renderDays();
  dlgD.showModal();
}

$('#btnDaysAll').addEventListener('click', () => { rangeKeys().forEach((k) => daysState.picked.add(k)); renderDays(); });
$('#btnDaysNone').addEventListener('click', () => { daysState.picked.clear(); renderDays(); });
$('#btnDaysCancel').addEventListener('click', () => dlgD.close());
$('#daysForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const dates = [...daysState.picked].sort();
  if (!dates.length) return;
  $('#btnDaysSave').disabled = true;
  try {
    const r = await api(`/api/events/${daysState.ev.id}/split`, { method: 'POST', body: { dates } });
    dlgD.close(); await load();
    toast(r.created ? `Aggiunti ${plural(r.created, 'giorno', 'giorni')} al calendario.` : 'Quei giorni erano già in agenda.');
  } catch (err) { $('#daysMsg').textContent = err.message; updateDaysUi(); }
});

$('#btnCheck').addEventListener('click', async () => {
  const btn = $('#btnCheck'); btn.disabled = true; btn.textContent = 'Controllo…';
  try {
    const r = await api('/api/mail/check', { method: 'POST' });
    if (r.running) toast('Un controllo è già in corso: aspetto che finisca.');
    // il controllo gira sul server: chiedo lo stato ogni 2 secondi (massimo 5 minuti)
    for (let i = 0; i < 150; i++) {
      await new Promise((ok) => setTimeout(ok, 2000));
      const st = await api('/api/status');
      if (!st.checking) {
        await load();
        if (st.last_error) toast(`Controllo non riuscito: ${st.last_error}`);
        else if (!st.mail_enabled) toast('Nessun account di posta: aggiungilo da Impostazioni.');
        else toast(`Posta controllata. ${st.last_result}.`);
        return;
      }
    }
    toast('Il controllo sta richiedendo più del solito: continua in background.');
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Controlla posta'; }
});

$('#prev').addEventListener('click', () => { state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth() - 1, 1); renderMonth(); });
$('#next').addEventListener('click', () => { state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth() + 1, 1); renderMonth(); });
$('#fab').addEventListener('click', () => openDialog());
document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => showView(b.dataset.view)));

// ---------- tema giorno / notte ----------
const THEME_KEY = 'agenda-theme';
const storedTheme = () => { try { return localStorage.getItem(THEME_KEY); } catch (_) { return null; } };
function applyTheme(theme, save) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').setAttribute('content', theme === 'dark' ? '#0F1724' : '#17243A');
  $('#btnTheme').setAttribute('aria-label', theme === 'dark' ? 'Passa al tema chiaro' : 'Passa al tema scuro');
  if (save) { try { localStorage.setItem(THEME_KEY, theme); } catch (_) {} }
}
$('#btnTheme').addEventListener('click', () =>
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark', true));
applyTheme(document.documentElement.dataset.theme || 'light', false);
// finché non scegli a mano, l'app segue il tema del telefono
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
  if (!storedTheme()) applyTheme(e.matches ? 'dark' : 'light', false);
});

// ---------- impostazioni: chiave Gemini e account di posta ----------
const dlgS = $('#dlgSettings');
const say = (sel, text, ok = false) => { const n = $(sel); n.textContent = text; n.className = ok ? 'msg ok' : 'msg'; };

function renderGemini() {
  const g = state.settings.gemini;
  $('#gStatus').textContent = g.configured
    ? `Chiave attiva (…${g.hint}), ${g.source === 'env' ? 'letta dal file .env' : 'salvata dal sito'}.`
    : 'Nessuna chiave: senza, l’AI non può leggere mail, PDF e screenshot.';
  $('#g-key').value = '';
  $('#g-key').placeholder = g.configured ? 'Incolla una nuova chiave per sostituirla' : 'Incolla qui la chiave';
  $('#g-model').value = g.model;
  $('#g-model').placeholder = g.default_model;
  $('#gRemove').hidden = g.source !== 'site';
}

const FB_PRESETS = {
  groq: { url: 'https://api.groq.com/openai/v1', model: 'es. llama-3.3-70b-versatile' },
  mistral: { url: 'https://api.mistral.ai/v1', model: 'es. mistral-small-latest' },
  openrouter: { url: 'https://openrouter.ai/api/v1', model: 'un modello con “:free” nel nome' },
  ollama: { url: 'http://localhost:11434/v1', model: 'il modello scaricato con ollama pull' },
  other: { url: '', model: 'nome del modello' },
};

function renderFallback() {
  const f = state.settings.fallback, o = state.settings.ocr;
  $('#fStatus').textContent = f.configured
    ? `Attiva: ${f.model}${f.has_key ? ` (chiave …${f.hint})` : ''}, ${f.source === 'env' ? 'letta dal file .env' : 'salvata dal sito'}.`
    : 'Non configurata: se Gemini finisce la quota, per i file resta solo l’OCR.';
  $('#fb-preset').value = f.preset || '';
  $('#fb-url').value = f.base_url; $('#fb-model').value = f.model;
  $('#fb-key').value = ''; $('#fb-key').placeholder = f.has_key ? 'Incolla una nuova chiave per sostituirla' : '';
  $('#fbRemove').hidden = f.source !== 'site';
  $('#ocrStatus').textContent = o.available
    ? `OCR sul computer: pronto (${o.engine}${o.italian ? '' : ', senza il pacchetto italiano: leggerà peggio le lettere accentate'}).`
    : `OCR sul computer: non pronto. ${o.problems.join(' · ')} (vedi il README, sezione “Quando Gemini finisce la quota”).`;
}

$('#fb-preset').addEventListener('change', () => {
  const p = FB_PRESETS[$('#fb-preset').value];
  if (!p) return;
  if (p.url) $('#fb-url').value = p.url;
  $('#fb-model').placeholder = p.model;
});

function accountRow(a) {
  const checked = a.last_check ? `${fmtShort.format(parseYmd(a.last_check))}, ${a.last_check.slice(11, 16)}` : null;
  return el('div', { class: `acct${a.enabled ? '' : ' off'}` },
    el('div', { class: 'acct-info' },
      el('strong', { text: a.label || a.email }),
      el('p', { text: a.label ? `${a.email} · ${a.imap_server}` : a.imap_server }),
      !a.enabled ? el('p', { text: 'In pausa: non viene letto.' })
        : a.last_error ? el('p', { class: 'err', text: a.last_error })
        : checked ? el('p', { text: `Ultimo controllo: ${checked}` }) : null,
    ),
    el('div', { class: 'acct-actions' },
      el('button', { class: 'btn small', type: 'button', text: a.enabled ? 'Pausa' : 'Riattiva', onclick: () => patchAccount(a, { enabled: !a.enabled }) }),
      el('button', { class: 'btn small danger', type: 'button', text: 'Rimuovi', onclick: () => removeAccount(a) }),
    ));
}

function renderAccounts() {
  const list = $('#acctList'); list.replaceChildren();
  const accounts = state.settings.accounts;
  if (!accounts.length) list.append(el('p', { class: 'hint', text: 'Nessun account collegato: l’agenda non legge la posta.' }));
  accounts.forEach((a) => list.append(accountRow(a)));
}

async function openSettings() {
  say('#gMsg', ''); say('#fbMsg', ''); say('#aMsg', ''); say('#aFormMsg', '');
  dlgS.showModal();
  try {
    state.settings = await api('/api/settings');
    renderGemini(); renderFallback(); renderAccounts();
  } catch (e) { say('#aMsg', e.message); }
}

async function patchAccount(a, body) {
  say('#aMsg', '');
  try {
    state.settings.accounts = (await api(`/api/accounts/${a.id}`, { method: 'PATCH', body })).accounts;
    renderAccounts();
  } catch (e) { say('#aMsg', e.message); }
}

async function removeAccount(a) {
  if (!confirm(`Rimuovere ${a.label || a.email}? Gli impegni già trovati restano in agenda.`)) return;
  say('#aMsg', '');
  try {
    state.settings.accounts = (await api(`/api/accounts/${a.id}`, { method: 'DELETE' })).accounts;
    renderAccounts();
  } catch (e) { say('#aMsg', e.message); }
}

$('#btnSettings').addEventListener('click', openSettings);
$('#sClose').addEventListener('click', () => dlgS.close());
dlgS.addEventListener('close', () => load().catch(() => {}));

$('#geminiForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#gSave'); btn.disabled = true; say('#gMsg', 'Provo la chiave…', true);
  try {
    const r = await api('/api/settings/gemini', { method: 'PUT', body: { api_key: $('#g-key').value, model: $('#g-model').value } });
    state.settings.gemini = r.gemini; renderGemini();
    say('#gMsg', r.warning || 'Chiave verificata e salvata.', !r.warning);
  } catch (err) { say('#gMsg', err.message); }
  finally { btn.disabled = false; }
});

$('#btnDiag').addEventListener('click', async () => {
  const btn = $('#btnDiag'), list = $('#diagList');
  btn.disabled = true; list.innerHTML = ''; say('#diagMsg', 'Controllo in corso… può servire qualche secondo.', true);
  try {
    const r = await api('/api/diagnose', { method: 'POST' });
    for (const c of r.checks) {
      const li = document.createElement('li');
      li.className = c.ok === true ? 'ok' : c.ok === false ? 'ko' : 'warn';
      const b = document.createElement('b'); b.textContent = `${c.ok === true ? '✓' : c.ok === false ? '✗' : '•'} ${c.name}`;
      const d = document.createElement('span'); d.textContent = c.detail;
      li.append(b, d);
      if (c.fix && c.ok !== true) { const f = document.createElement('small'); f.textContent = c.fix; li.append(f); }
      list.append(li);
    }
    const bad = r.checks.filter((c) => c.ok === false).length;
    say('#diagMsg', bad ? `${plural(bad, 'cosa da sistemare', 'cose da sistemare')}.` : 'Tutto a posto.', !bad);
  } catch (e) { say('#diagMsg', e.message); }
  finally { btn.disabled = false; }
});

$('#fallbackForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#fbSave'); btn.disabled = true; say('#fbMsg', 'Provo il servizio…', true);
  try {
    const r = await api('/api/settings/fallback', { method: 'PUT', body: {
      preset: $('#fb-preset').value, base_url: $('#fb-url').value,
      model: $('#fb-model').value, api_key: $('#fb-key').value,
    } });
    state.settings.fallback = r.fallback; renderFallback();
    say('#fbMsg', r.warning || 'Servizio verificato e salvato.', !r.warning);
  } catch (err) { say('#fbMsg', err.message); }
  finally { btn.disabled = false; }
});

$('#fbRemove').addEventListener('click', async () => {
  if (!confirm('Rimuovere l’AI di riserva salvata dal sito?')) return;
  try {
    const r = await api('/api/settings/fallback', { method: 'DELETE' });
    state.settings.fallback = r.fallback; renderFallback();
    say('#fbMsg', 'AI di riserva rimossa.', true);
  } catch (err) { say('#fbMsg', err.message); }
});

$('#gRemove').addEventListener('click', async () => {
  if (!confirm('Rimuovere la chiave salvata dal sito?')) return;
  try {
    const r = await api('/api/settings/gemini-key', { method: 'DELETE' });
    state.settings.gemini = r.gemini; renderGemini();
    say('#gMsg', r.gemini.configured ? 'Chiave rimossa: ora vale quella del file .env.' : 'Chiave rimossa.', true);
  } catch (err) { say('#gMsg', err.message); }
});

$('#acctForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('#aSave'); btn.disabled = true; say('#aFormMsg', 'Provo ad accedere…', true);
  try {
    const r = await api('/api/accounts', { method: 'POST', body: {
      label: $('#a-label').value, email: $('#a-email').value,
      password: $('#a-pass').value, imap_server: $('#a-server').value,
    } });
    state.settings.accounts = r.accounts; renderAccounts();
    e.target.reset(); $('#addBox').open = false; say('#aFormMsg', '');
    say('#aMsg', 'Account aggiunto. Premi “Controlla posta” per leggerlo subito: guarda gli ultimi giorni.', true);
  } catch (err) { say('#aFormMsg', err.message); }
  finally { btn.disabled = false; }
});

// aggiorna quando torni sull'app e ogni minuto
document.addEventListener('visibilitychange', () => { if (!document.hidden) load().catch(() => {}); });
setInterval(() => { if (!document.hidden && !dlg.open) load().catch(() => {}); }, 60000);

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

showView('cal');
load().catch((e) => toast(e.message));
