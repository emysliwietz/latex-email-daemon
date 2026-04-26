"""
web.py — Web-Frontend für den latex-email-daemon.

Startet ein Browser-Dashboard, das den E-Mail-Workflow ersetzt:
Felder ausfüllen → PDF live vorschauen → herunterladen.

Starten:
    python web.py

Umgebungsvariablen (dieselbe .env wie der Daemon):
    LATEX_TEMPLATE_DIR   Verzeichnis mit *.tex-Vorlagen   (Standard: templates/)
    LATEX_TEMPLATE_FILE  Einzelne Fallback-Vorlage         (Standard: template.tex)
    WEB_HOST             Bind-Adresse                      (Standard: 0.0.0.0)
    WEB_PORT             Port                              (Standard: 5000)
    WEB_DEBUG            Auf "1" setzen für Flask-Debug-Modus
"""

import os
import glob
import logging

from flask import Flask, request, Response, jsonify, render_template_string
from dotenv import load_dotenv

from pdf_utils import (
    html_to_latex,
    compile_pdf,
    sanitize_filename,
    REQUIRED_PLACEHOLDERS,
)

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

load_dotenv()

# Verzeichnis dieser Datei — als Ausgangspunkt für die Vorlagensuche
_HERE = os.path.dirname(os.path.abspath(__file__))

TEMPLATE_DIR      = os.getenv("LATEX_TEMPLATE_DIR",  os.path.join(_HERE, "templates"))
FALLBACK_TEMPLATE = os.getenv("LATEX_TEMPLATE_FILE", os.path.join(_HERE, "template.tex"))
WEB_HOST          = os.getenv("WEB_HOST",  "0.0.0.0")
WEB_PORT          = int(os.getenv("WEB_PORT", 5000))
WEB_DEBUG         = os.getenv("WEB_DEBUG", "0") == "1"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")


# ---------------------------------------------------------------------------
# Vorlagen-Suche
# ---------------------------------------------------------------------------

def _is_valid_template(path: str) -> bool:
    """Prüft, ob die Datei alle nötigen Platzhalter enthält."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return all(p in content for p in REQUIRED_PLACEHOLDERS)
    except OSError:
        return False


def _collect_tex_files(*search_roots: str) -> list[str]:
    """
    Sucht rekursiv nach .tex-Dateien in den angegebenen Verzeichnissen
    und gibt nur solche zurück, die alle Pflicht-Platzhalter enthalten.
    Ergebnisse werden dedupliziert (nach absolutem Pfad).
    """
    seen: set[str] = set()
    result: list[str] = []

    for root in search_roots:
        if not root:
            continue
        if os.path.isfile(root) and root.endswith(".tex"):
            # Einzelne Datei statt Verzeichnis angegeben
            abs_path = os.path.abspath(root)
            if abs_path not in seen and _is_valid_template(abs_path):
                seen.add(abs_path)
                result.append(abs_path)
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fname in sorted(files):
                if not fname.endswith(".tex"):
                    continue
                abs_path = os.path.abspath(os.path.join(dirpath, fname))
                if abs_path not in seen and _is_valid_template(abs_path):
                    seen.add(abs_path)
                    result.append(abs_path)

    return result


def list_templates() -> list[dict]:
    """
    Gibt [{name, path}] für jede gefundene Vorlage zurück.

    Suchreihenfolge:
      1. LATEX_TEMPLATE_DIR  (Docker-Volume oder konfiguriertes Verzeichnis)
      2. LATEX_TEMPLATE_FILE (einzelne Fallback-Datei)
      3. Verzeichnisbaum ab dem Speicherort dieser Datei (_HERE)
      4. Arbeitsverzeichnis (falls abweichend)
    """
    paths = _collect_tex_files(
        TEMPLATE_DIR,
        FALLBACK_TEMPLATE,
        _HERE,
        os.getcwd(),
    )
    return [{"name": os.path.basename(p), "path": p} for p in paths]


def resolve_template(name: str | None) -> str:
    """Löst einen Vorlagennamen in einen Dateipfad auf. Wirft ValueError bei Fehler."""
    templates = list_templates()
    if not templates:
        raise ValueError(
            "Keine LaTeX-Vorlagen gefunden. "
            "Bitte ein Template-Volume einbinden oder LATEX_TEMPLATE_FILE setzen."
        )
    if name:
        for t in templates:
            if t["name"] == name:
                return t["path"]
        raise ValueError(f"Unbekannte Vorlage: {name!r}")
    return templates[0]["path"]


# ---------------------------------------------------------------------------
# API-Routen
# ---------------------------------------------------------------------------

@app.get("/api/templates")
def api_templates():
    """Gibt die Liste der verfügbaren Vorlagen zurück."""
    return jsonify(list_templates())


@app.post("/api/compile")
def api_compile():
    """
    Kompiliert ein PDF aus den Formularfeldern und gibt die PDF-Bytes zurück.

    Erwartet JSON-Body:
        {
            "template":          "<dateiname.tex>",   // optional
            "subject":           "…",
            "first_paragraph":   "…",
            "second_paragraph":  "…",
            "third_paragraph":   "…",
            "body":              "…",
            "body_is_html":      false                // optional
        }

    Gibt: application/pdf zurück (oder JSON-Fehler mit passendem Status)
    """
    data = request.get_json(force=True, silent=True) or {}

    subject          = data.get("subject",          "").strip()
    first_paragraph  = data.get("first_paragraph",  "").strip()
    second_paragraph = data.get("second_paragraph", "").strip()
    third_paragraph  = data.get("third_paragraph",  "").strip()
    raw_body         = data.get("body",              "").strip()
    body_is_html     = bool(data.get("body_is_html", False))
    template_name    = data.get("template") or None

    body = html_to_latex(raw_body) if (body_is_html and raw_body) else raw_body

    try:
        template_path = resolve_template(template_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info("Kompiliere PDF | Vorlage=%s Betreff=%r", template_path, subject)

    try:
        pdf_bytes = compile_pdf(
            template_file=template_path,
            subject=subject,
            first_paragraph=first_paragraph,
            second_paragraph=second_paragraph,
            third_paragraph=third_paragraph,
            body=body,
        )
    except RuntimeError as e:
        log.error("Kompilierfehler: %s", e)
        return jsonify({"error": str(e)}), 500

    safe_name = sanitize_filename(subject) if subject else "dokument"

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}.pdf"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@app.post("/api/download")
def api_download():
    """Wie /api/compile, erzwingt aber einen Datei-Download."""
    resp = api_compile()
    if isinstance(resp, Response) and resp.status_code == 200:
        data = request.get_json(force=True, silent=True) or {}
        subject = data.get("subject", "dokument").strip()
        safe_name = sanitize_filename(subject) if subject else "dokument"
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
    return resp


# ---------------------------------------------------------------------------
# Dashboard-HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>LaTeX PDF Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400&family=DM+Mono:wght@300;400;500&family=Manrope:wght@300;400;500;600&display=swap" rel="stylesheet">

<style>
:root {
  --ink:      #0f0e0c;
  --paper:    #f5f0e8;
  --mist:     #e8e2d6;
  --dust:     #c9bfad;
  --copper:   #b87c4c;
  --charcoal: #2a2520;
  --ash:      #6b6258;
  --error:    #c0392b;

  --form-w: 460px;
  --radius: 3px;
  --mono:   'DM Mono', monospace;
  --serif:  'Cormorant Garamond', Georgia, serif;
  --sans:   'Manrope', system-ui, sans-serif;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  background: var(--ink);
  color: var(--paper);
  font-family: var(--sans);
  font-size: 15.5px;
  line-height: 1.6;
  overflow: hidden;
}

/* ── Layout ──────────────────────────────────────────────────────────────── */
#root {
  display: flex;
  height: 100vh;
  width: 100vw;
}

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
#sidebar {
  width: var(--form-w);
  flex-shrink: 0;
  background: var(--charcoal);
  border-right: 1px solid #3a342e;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

#sidebar-header {
  padding: 28px 28px 0;
  flex-shrink: 0;
}

.wordmark {
  font-family: var(--serif);
  font-size: 24px;
  font-weight: 500;
  letter-spacing: 0.01em;
  color: var(--paper);
  display: flex;
  align-items: center;
  gap: 10px;
}

.wordmark-dot {
  width: 9px; height: 9px;
  background: var(--copper);
  border-radius: 50%;
  flex-shrink: 0;
}

.wordmark-sub {
  font-family: var(--mono);
  font-size: 10.5px;
  font-weight: 300;
  color: var(--ash);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-top: 4px;
}

#form-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 24px 28px 28px;
  scrollbar-width: thin;
  scrollbar-color: #3a342e transparent;
}

#form-scroll::-webkit-scrollbar { width: 4px; }
#form-scroll::-webkit-scrollbar-track { background: transparent; }
#form-scroll::-webkit-scrollbar-thumb { background: #3a342e; border-radius: 2px; }

/* ── Formularelemente ────────────────────────────────────────────────────── */
.section-label {
  font-family: var(--mono);
  font-size: 9.5px;
  font-weight: 500;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ash);
  margin-bottom: 10px;
  margin-top: 26px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.section-label::after {
  content: '';
  flex: 1;
  height: 1px;
  background: #3a342e;
}

.section-label:first-child { margin-top: 0; }

.field { margin-bottom: 15px; }

.field label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--dust);
  margin-bottom: 5px;
  letter-spacing: 0.03em;
}

.field label .placeholder-tag {
  color: var(--ash);
  font-weight: 300;
  font-family: var(--mono);
  font-size: 10.5px;
}

.field input,
.field textarea,
.field select {
  width: 100%;
  background: #1e1a16;
  border: 1px solid #3a342e;
  border-radius: var(--radius);
  color: var(--paper);
  font-family: var(--sans);
  font-size: 14.5px;
  padding: 9px 12px;
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
  resize: none;
  line-height: 1.5;
  -webkit-appearance: none;
}

.field input::placeholder,
.field textarea::placeholder {
  color: #4a4038;
  font-style: italic;
}

.field input:focus,
.field textarea:focus,
.field select:focus {
  border-color: var(--copper);
  box-shadow: 0 0 0 3px rgba(184,124,76,0.12);
}

.field textarea          { min-height: 82px; }
.field textarea.tall     { min-height: 140px; }

.field select {
  cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b6258' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 12px center;
  padding-right: 34px;
}

.field-hint {
  font-size: 11px;
  color: var(--ash);
  margin-top: 4px;
  font-family: var(--mono);
}

/* ── Schaltflächen ───────────────────────────────────────────────────────── */
#actions {
  display: flex;
  gap: 8px;
  margin-top: 22px;
}

.btn {
  flex: 1;
  padding: 12px 16px;
  border-radius: var(--radius);
  border: none;
  font-family: var(--sans);
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
  transition: background 0.15s, transform 0.08s, opacity 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
}

.btn:active { transform: scale(0.97); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }

.btn-primary {
  background: var(--copper);
  color: #fff;
}
.btn-primary:hover:not(:disabled) { background: #c98a5a; }

.btn-ghost {
  background: #2a2520;
  color: var(--dust);
  border: 1px solid #3a342e;
}
.btn-ghost:hover:not(:disabled) {
  background: #332e28;
  border-color: var(--copper);
  color: var(--paper);
}

/* ── Vorschau-Bereich ────────────────────────────────────────────────────── */
#preview-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  background: var(--ink);
  overflow: hidden;
}

#preview-toolbar {
  flex-shrink: 0;
  height: 50px;
  background: #191512;
  border-bottom: 1px solid #2a2520;
  display: flex;
  align-items: center;
  padding: 0 22px;
  gap: 18px;
}

.toolbar-title {
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ash);
  flex: 1;
}

#status-pill {
  font-family: var(--mono);
  font-size: 10.5px;
  letter-spacing: 0.06em;
  padding: 3px 11px;
  border-radius: 99px;
  background: #2a2520;
  color: var(--ash);
  transition: all 0.2s;
}
#status-pill.compiling { background: rgba(184,124,76,0.15); color: var(--copper); }
#status-pill.ready     { background: rgba(80,160,80,0.15);  color: #6dbb6d; }
#status-pill.error     { background: rgba(192,57,43,0.15);  color: #e57575; }

#auto-preview-toggle {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--ash);
  cursor: pointer;
  user-select: none;
}

#auto-preview-toggle input[type=checkbox] {
  width: 14px; height: 14px;
  accent-color: var(--copper);
  cursor: pointer;
}

#preview-area {
  flex: 1;
  overflow: hidden;
  position: relative;
}

#preview-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  pointer-events: none;
}

.placeholder-icon {
  font-family: var(--serif);
  font-size: 88px;
  opacity: 0.06;
  line-height: 1;
}

.placeholder-text {
  font-family: var(--mono);
  font-size: 12px;
  letter-spacing: 0.1em;
  color: var(--ash);
  opacity: 0.5;
}

#pdf-frame {
  width: 100%;
  height: 100%;
  border: none;
  display: none;
  background: #333;
}

/* ── Ladeindikator ───────────────────────────────────────────────────────── */
#spinner-overlay {
  position: absolute;
  inset: 0;
  background: rgba(15,14,12,0.65);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 10;
}

.spinner {
  width: 34px; height: 34px;
  border: 2px solid #3a342e;
  border-top-color: var(--copper);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin { to { transform: rotate(360deg); } }

/* ── Fehler-Hinweis ──────────────────────────────────────────────────────── */
#error-toast {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(90px);
  background: #3a1a18;
  border: 1px solid var(--error);
  border-radius: 4px;
  padding: 13px 22px;
  max-width: 540px;
  width: calc(100vw - 48px);
  font-family: var(--mono);
  font-size: 11.5px;
  color: #e57575;
  line-height: 1.55;
  z-index: 100;
  transition: transform 0.3s cubic-bezier(.16,1,.3,1);
  white-space: pre-wrap;
  word-break: break-word;
}

#error-toast.visible { transform: translateX(-50%) translateY(0); }

/* ── Responsiv ───────────────────────────────────────────────────────────── */
@media (max-width: 920px) {
  #root { flex-direction: column; overflow-y: auto; }
  #sidebar { width: 100%; height: auto; overflow: visible; }
  #form-scroll { overflow: visible; }
  body, html { overflow: auto; }
  #preview-pane { min-height: 60vh; }
}
</style>
</head>
<body>

<div id="root">

  <!-- ── Linke Leiste: Formular ── -->
  <aside id="sidebar">
    <div id="sidebar-header">
      <div class="wordmark">
        <span class="wordmark-dot"></span>
        LaTeX PDF Studio
      </div>
      <div class="wordmark-sub">latex-email-daemon · Web-Oberfläche</div>
    </div>

    <div id="form-scroll">

      <div class="section-label">Vorlage</div>

      <div class="field">
        <label for="template-select">Vorlagendatei</label>
        <select id="template-select">
          <option value="">Wird geladen…</option>
        </select>
      </div>

      <div class="section-label">Dokument</div>

      <div class="field">
        <label for="f-subject">
          Betreff / Titel
          <span class="placeholder-tag">· {{SUBJECT}}</span>
        </label>
        <input id="f-subject" type="text" placeholder="z. B. Kündigung Mietvertrag"/>
      </div>

      <div class="section-label">Abschnitte</div>

      <div class="field">
        <label for="f-first">
          Adresse
          <span class="placeholder-tag">· {{FIRST_PARAGRAPH}}</span>
        </label>
        <textarea id="f-first" rows="3"
          placeholder="Empfänger&#10;Musterstraße 12&#10;12345 Musterstadt"></textarea>
        <div class="field-hint">Empfängeranschrift</div>
      </div>

      <div class="field">
        <label for="f-second">
          Datum
          <span class="placeholder-tag">· {{SECOND_PARAGRAPH}}</span>
        </label>
        <textarea id="f-second" rows="2"
          placeholder="Köln, 26. April 2026"></textarea>
        <div class="field-hint">Ort und Datum</div>
      </div>

      <div class="field">
        <label for="f-third">
          Anrede
          <span class="placeholder-tag">· {{THIRD_PARAGRAPH}}</span>
        </label>
        <textarea id="f-third" rows="2"
          placeholder="Sehr geehrte Damen und Herren,"></textarea>
        <div class="field-hint">Briefliche Anrede</div>
      </div>

      <div class="section-label">Inhalt</div>

      <div class="field">
        <label for="f-body">
          Brieftext
          <span class="placeholder-tag">· {{BODY}}</span>
        </label>
        <textarea id="f-body" class="tall"
          placeholder="Haupttext des Briefes…&#10;&#10;Absätze durch eine Leerzeile trennen."></textarea>
        <div class="field-hint">Absätze durch Leerzeile trennen</div>
      </div>

      <div id="actions">
        <button class="btn btn-primary" id="btn-preview" onclick="triggerCompile(false)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
            <circle cx="12" cy="12" r="3"/>
          </svg>
          Vorschau
        </button>
        <button class="btn btn-ghost" id="btn-download" onclick="triggerCompile(true)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          Herunterladen
        </button>
      </div>

    </div><!-- /form-scroll -->
  </aside>

  <!-- ── Rechts: Vorschau ── -->
  <main id="preview-pane">
    <div id="preview-toolbar">
      <span class="toolbar-title">PDF-Vorschau</span>
      <label id="auto-preview-toggle">
        <input type="checkbox" id="auto-preview-cb" checked/>
        Auto-Vorschau
      </label>
      <span id="status-pill">bereit</span>
    </div>

    <div id="preview-area">
      <div id="preview-placeholder">
        <div class="placeholder-icon">&#9998;</div>
        <div class="placeholder-text">Felder ausfüllen und Vorschau klicken</div>
      </div>
      <iframe id="pdf-frame" title="PDF-Vorschau"></iframe>
      <div id="spinner-overlay"><div class="spinner"></div></div>
    </div>
  </main>

</div><!-- /root -->

<div id="error-toast"></div>

<script>
/* ── Zustand ──────────────────────────────────────────────────────────────── */
let compileTimer  = null;
let currentObjUrl = null;
let isCompiling   = false;

const DEBOUNCE_MS = 1400;

/* ── DOM ──────────────────────────────────────────────────────────────────── */
const statusPill    = document.getElementById('status-pill');
const spinner       = document.getElementById('spinner-overlay');
const pdfFrame      = document.getElementById('pdf-frame');
const placeholder   = document.getElementById('preview-placeholder');
const errorToast    = document.getElementById('error-toast');
const autoPreviewCb = document.getElementById('auto-preview-cb');
const templateSel   = document.getElementById('template-select');

const fields = ['f-subject','f-first','f-second','f-third','f-body'];

/* ── Vorlagen laden ───────────────────────────────────────────────────────── */
async function loadTemplates() {
  try {
    const res  = await fetch('/api/templates');
    const list = await res.json();

    templateSel.innerHTML = '';

    if (list.length === 0) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '⚠ Keine Vorlagen gefunden';
      templateSel.appendChild(opt);
      return;
    }

    list.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.name;
      opt.textContent = t.name;
      templateSel.appendChild(opt);
    });
  } catch(e) {
    templateSel.innerHTML = '<option value="">Fehler beim Laden der Vorlagen</option>';
  }
}

/* ── Status-Anzeige ───────────────────────────────────────────────────────── */
function setStatus(state, label) {
  statusPill.className   = state;
  statusPill.textContent = label;
}

/* ── Fehler-Toast ─────────────────────────────────────────────────────────── */
let toastTimer = null;
function showError(msg) {
  errorToast.textContent = msg;
  errorToast.classList.add('visible');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => errorToast.classList.remove('visible'), 9000);
}

/* ── Anfrage zusammenstellen ──────────────────────────────────────────────── */
function buildPayload() {
  return {
    template:          templateSel.value || null,
    subject:           document.getElementById('f-subject').value,
    first_paragraph:   document.getElementById('f-first').value,
    second_paragraph:  document.getElementById('f-second').value,
    third_paragraph:   document.getElementById('f-third').value,
    body:              document.getElementById('f-body').value,
    body_is_html:      false,
  };
}

/* ── Kompilieren & anzeigen ───────────────────────────────────────────────── */
async function triggerCompile(forDownload = false) {
  if (isCompiling) return;
  isCompiling = true;
  clearTimeout(compileTimer);

  setStatus('compiling', 'kompiliert…');
  spinner.style.display = 'flex';
  document.getElementById('btn-preview').disabled  = true;
  document.getElementById('btn-download').disabled = true;

  const endpoint = forDownload ? '/api/download' : '/api/compile';

  try {
    const res = await fetch(endpoint, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(buildPayload()),
    });

    if (!res.ok) {
      let errMsg = `HTTP ${res.status}`;
      try { const j = await res.json(); errMsg = j.error || errMsg; } catch(_) {}
      throw new Error(errMsg);
    }

    const blob = await res.blob();

    if (forDownload) {
      const subject = document.getElementById('f-subject').value.trim() || 'dokument';
      const safe    = subject.replace(/[^a-zA-Z0-9_\-]/g, '_').substring(0, 50);
      const url     = URL.createObjectURL(blob);
      const a       = document.createElement('a');
      a.href = url; a.download = safe + '.pdf';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      setStatus('ready', 'heruntergeladen ✓');
    } else {
      if (currentObjUrl) URL.revokeObjectURL(currentObjUrl);
      currentObjUrl = URL.createObjectURL(blob);
      pdfFrame.src  = currentObjUrl;
      pdfFrame.style.display    = 'block';
      placeholder.style.display = 'none';
      setStatus('ready', 'fertig ✓');
    }

  } catch(e) {
    setStatus('error', 'Fehler');
    showError('Kompilierfehler:\n' + e.message);
  } finally {
    spinner.style.display = 'none';
    isCompiling = false;
    document.getElementById('btn-preview').disabled  = false;
    document.getElementById('btn-download').disabled = false;
  }
}

/* ── Auto-Vorschau mit Entprellung ────────────────────────────────────────── */
function scheduleAutoPreview() {
  if (!autoPreviewCb.checked) return;
  clearTimeout(compileTimer);
  setStatus('compiling', 'warte…');
  compileTimer = setTimeout(() => triggerCompile(false), DEBOUNCE_MS);
}

fields.forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('input', scheduleAutoPreview);
});

templateSel.addEventListener('change', scheduleAutoPreview);

/* ── Start ────────────────────────────────────────────────────────────────── */
loadTemplates();
</script>
</body>
</html>
"""


@app.get("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    templates = list_templates()
    if templates:
        log.info("Gefundene Vorlagen (%d):", len(templates))
        for t in templates:
            log.info("  • %s  (%s)", t["name"], t["path"])
    else:
        log.warning(
            "Keine Vorlagen gefunden! Bitte ein Template-Volume einbinden "
            "oder LATEX_TEMPLATE_FILE / LATEX_TEMPLATE_DIR setzen."
        )

    log.info("Starte Web-Dashboard auf http://%s:%d", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=WEB_DEBUG)
