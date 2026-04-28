"""
web.py — Web-Frontend für den latex-email-daemon.

Drei Tabs:
  Formular  → Brief ausfüllen, Sofortvorschau (HTML), PDF herunterladen
  Vorlagen  → .tex-Vorlagen im Browser bearbeiten, anlegen, löschen
  Verlauf   → localStorage-Protokoll aller erzeugten Dokumente

Starten:
    python web.py

Umgebungsvariablen (dieselbe .env wie der Daemon):
    LATEX_TEMPLATE_DIR   Verzeichnis mit *.tex-Vorlagen   (Standard: <hier>/templates/)
    LATEX_TEMPLATE_FILE  Einzelne Fallback-Vorlage         (Standard: <hier>/template.tex)
    WEB_HOST             Bind-Adresse                      (Standard: 0.0.0.0)
    WEB_PORT             Port                              (Standard: 5000)
    WEB_DEBUG            "1" für Flask-Debug-Modus
"""

import os
import re
import logging

from flask import Flask, request, Response, jsonify, render_template_string
from dotenv import load_dotenv

from pdf_utils import (
    html_to_latex,
    compile_pdf,
    sanitize_filename,
    REQUIRED_PLACEHOLDERS,
    plain_to_latex_lines,
    plain_to_latex_body,
)

# ── Konfiguration ─────────────────────────────────────────────────────────────

load_dotenv()

_HERE = os.path.dirname(os.path.abspath(__file__))

TEMPLATE_DIR      = os.getenv("LATEX_TEMPLATE_DIR",  os.path.join(_HERE, "templates"))
FALLBACK_TEMPLATE = os.getenv("LATEX_TEMPLATE_FILE", os.path.join(_HERE, "template.tex"))
WEB_HOST          = os.getenv("WEB_HOST",  "0.0.0.0")
WEB_PORT          = int(os.getenv("WEB_PORT", 5000))
WEB_DEBUG         = os.getenv("WEB_DEBUG", "0") == "1"

# Schreibverzeichnis — neue/bearbeitete Vorlagen werden hier gespeichert
TEMPLATE_WRITE_DIR = os.path.abspath(TEMPLATE_DIR)
os.makedirs(TEMPLATE_WRITE_DIR, exist_ok=True)

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("web")


# ── Vorlagen-Suche ────────────────────────────────────────────────────────────

def _is_valid_template(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        return all(p in content for p in REQUIRED_PLACEHOLDERS)
    except OSError:
        return False


def _collect_tex_files(*search_roots: str) -> list[str]:
    """
    Sucht rekursiv nach .tex-Dateien.  Dedupliziert nach absolutem Pfad UND
    nach Dateiname (erster Treffer pro Name gewinnt — TEMPLATE_DIR hat Vorrang).
    """
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    result: list[str] = []

    for root in search_roots:
        if not root:
            continue
        if os.path.isfile(root) and root.endswith(".tex"):
            abs_path = os.path.abspath(root)
            name     = os.path.basename(abs_path)
            if abs_path not in seen_paths and name not in seen_names and _is_valid_template(abs_path):
                seen_paths.add(abs_path)
                seen_names.add(name)
                result.append(abs_path)
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fname in sorted(files):
                if not fname.endswith(".tex"):
                    continue
                abs_path = os.path.abspath(os.path.join(dirpath, fname))
                name     = fname
                if abs_path not in seen_paths and name not in seen_names and _is_valid_template(abs_path):
                    seen_paths.add(abs_path)
                    seen_names.add(name)
                    result.append(abs_path)
    return result


def list_templates() -> list[dict]:
    paths = _collect_tex_files(TEMPLATE_DIR, FALLBACK_TEMPLATE, _HERE, os.getcwd())
    return [
        {
            "name":     os.path.basename(p),
            "path":     p,
            "writable": os.path.abspath(os.path.dirname(p)) == TEMPLATE_WRITE_DIR,
        }
        for p in paths
    ]


def resolve_template(name: str | None) -> str:
    templates = list_templates()
    if not templates:
        raise ValueError("Keine LaTeX-Vorlagen gefunden.")
    if name:
        for t in templates:
            if t["name"] == name:
                return t["path"]
        raise ValueError(f"Unbekannte Vorlage: {name!r}")
    return templates[0]["path"]


def _safe_write_path(name: str) -> str:
    """Gibt einen sicheren Pfad innerhalb von TEMPLATE_WRITE_DIR zurück."""
    name = os.path.basename(name)
    if not name:
        raise ValueError("Leerer Dateiname")
    if not name.endswith(".tex"):
        name += ".tex"
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', name):
        raise ValueError(f"Ungültiger Dateiname: {name!r}")
    path = os.path.abspath(os.path.join(TEMPLATE_WRITE_DIR, name))
    if not path.startswith(TEMPLATE_WRITE_DIR + os.sep):
        raise ValueError("Pfad außerhalb des Vorlagenverzeichnisses")
    return path


# ── API-Routen ────────────────────────────────────────────────────────────────

@app.get("/api/templates")
def api_templates():
    return jsonify(list_templates())


@app.get("/api/templates/<name>")
def api_template_get(name):
    try:
        path = resolve_template(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({
        "name":     name,
        "content":  content,
        "writable": os.path.abspath(os.path.dirname(path)) == TEMPLATE_WRITE_DIR,
    })


@app.put("/api/templates/<name>")
def api_template_put(name):
    data    = request.get_json(force=True, silent=True) or {}
    content = data.get("content", "")
    try:
        path = _safe_write_path(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    log.info("Vorlage gespeichert: %s", path)
    return jsonify({"name": os.path.basename(path), "ok": True})


@app.delete("/api/templates/<name>")
def api_template_delete(name):
    try:
        path = _safe_write_path(name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "Vorlage nicht im Schreibverzeichnis — schreibgeschützt"}), 403
    try:
        os.remove(path)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    log.info("Vorlage gelöscht: %s", path)
    return jsonify({"ok": True})


@app.post("/api/compile")
def api_compile():
    data = request.get_json(force=True, silent=True) or {}

    subject       = data.get("subject",          "").strip()
    raw_first     = data.get("first_paragraph",  "")
    raw_second    = data.get("second_paragraph", "")
    raw_third     = data.get("third_paragraph",  "")
    raw_body      = data.get("body",             "")
    body_is_html  = bool(data.get("body_is_html", False))
    template_name = data.get("template") or None

    # Zeilenumbrüche korrekt in LaTeX übersetzen
    first_paragraph  = plain_to_latex_lines(raw_first)
    second_paragraph = plain_to_latex_lines(raw_second)
    third_paragraph  = plain_to_latex_lines(raw_third)
    body             = html_to_latex(raw_body) if body_is_html else plain_to_latex_body(raw_body)

    try:
        template_path = resolve_template(template_name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    log.info("Kompiliere PDF | Vorlage=%s Betreff=%r", template_path, subject)

    try:
        pdf_bytes = compile_pdf(
            template_file    = template_path,
            subject          = subject,
            first_paragraph  = first_paragraph,
            second_paragraph = second_paragraph,
            third_paragraph  = third_paragraph,
            body             = body,
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
            "Content-Length":      str(len(pdf_bytes)),
        },
    )


@app.post("/api/download")
def api_download():
    resp = api_compile()
    if isinstance(resp, Response) and resp.status_code == 200:
        data      = request.get_json(force=True, silent=True) or {}
        subject   = data.get("subject", "dokument").strip()
        safe_name = sanitize_filename(subject) if subject else "dokument"
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
    return resp


# ── Dashboard-HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>LaTeX PDF Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;500;600&family=DM+Mono:wght@300;400;500&family=Manrope:wght@300;400;500;600&family=Lora:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
/* ── Design-Tokens ───────────────────────────────────────────────────────── */
:root {
  --ink:      #0f0e0c;
  --paper:    #f5f0e8;
  --mist:     #e8e2d6;
  --dust:     #c9bfad;
  --copper:   #b87c4c;
  --copper-d: #c98a5a;
  --charcoal: #2a2520;
  --panel:    #201c18;
  --border:   #3a342e;
  --ash:      #6b6258;
  --error:    #c0392b;
  --ok:       #4fa84f;

  --sidebar-w: 460px;
  --radius:    3px;
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
  font-size: 15px;
  line-height: 1.6;
  overflow: hidden;
}

/* ── Root layout ─────────────────────────────────────────────────────────── */
#root { display: flex; height: 100vh; width: 100vw; }

/* ── Sidebar ─────────────────────────────────────────────────────────────── */
#sidebar {
  width: var(--sidebar-w);
  flex-shrink: 0;
  background: var(--charcoal);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  height: 100vh;
}

/* ── Header ──────────────────────────────────────────────────────────────── */
#sidebar-header {
  flex-shrink: 0;
  padding: 22px 26px 16px;
  border-bottom: 1px solid var(--border);
}

.wordmark {
  font-family: var(--serif);
  font-size: 22px;
  font-weight: 500;
  color: var(--paper);
  display: flex;
  align-items: center;
  gap: 9px;
  letter-spacing: 0.01em;
}
.wordmark-dot {
  width: 8px; height: 8px;
  background: var(--copper);
  border-radius: 50%;
}
.wordmark-sub {
  font-family: var(--mono);
  font-size: 9.5px;
  color: var(--ash);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-top: 3px;
}

/* ── Tab-Leiste ──────────────────────────────────────────────────────────── */
#tab-bar {
  flex-shrink: 0;
  display: flex;
  border-bottom: 1px solid var(--border);
}

.tab-btn {
  flex: 1;
  padding: 11px 4px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--ash);
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.tab-btn:hover { color: var(--dust); }
.tab-btn.active {
  color: var(--copper);
  border-bottom-color: var(--copper);
}
.tab-btn svg { opacity: 0.7; }
.tab-btn.active svg { opacity: 1; }

/* ── Tab-Panels ──────────────────────────────────────────────────────────── */
#tab-panels {
  flex: 1;
  min-height: 0;
  position: relative;
  overflow: hidden;
}

.tab-panel {
  position: absolute;
  inset: 0;
  display: none;
  flex-direction: column;
  overflow: hidden;
}
.tab-panel.active { display: flex; }

/* ── Formular-Tab: Felder oben ───────────────────────────────────────────── */
#form-top {
  flex-shrink: 0;
  padding: 16px 26px 0;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}
#form-top::-webkit-scrollbar { width: 3px; }
#form-top::-webkit-scrollbar-thumb { background: var(--border); }

/* ── Formular-Tab: Brieftext wächst ─────────────────────────────────────── */
#body-wrapper {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0 26px;
  gap: 5px;
}

#body-wrapper label {
  flex-shrink: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--dust);
  letter-spacing: 0.03em;
}

#f-body {
  flex: 1;
  min-height: 0;
  width: 100%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--paper);
  font-family: var(--sans);
  font-size: 14.5px;
  padding: 10px 12px;
  outline: none;
  resize: none;
  line-height: 1.6;
  transition: border-color 0.15s, box-shadow 0.15s;
}
#f-body::placeholder { color: #4a4038; font-style: italic; }
#f-body:focus { border-color: var(--copper); box-shadow: 0 0 0 3px rgba(184,124,76,0.12); }

/* ── Formular-Tab: Aktionen ──────────────────────────────────────────────── */
#form-actions {
  flex-shrink: 0;
  padding: 10px 26px 18px;
  display: flex;
  gap: 8px;
}

/* ── Formular-Felder (allgemein) ─────────────────────────────────────────── */
.section-label {
  font-family: var(--mono);
  font-size: 9px;
  font-weight: 500;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ash);
  margin-bottom: 9px;
  margin-top: 20px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-label::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.section-label:first-child { margin-top: 0; }

.field { margin-bottom: 11px; }

.field label {
  display: block;
  font-size: 12px;
  font-weight: 500;
  color: var(--dust);
  margin-bottom: 5px;
  letter-spacing: 0.03em;
}
.field label .pt {
  color: var(--ash);
  font-weight: 300;
  font-family: var(--mono);
  font-size: 10px;
}

.field input,
.field textarea,
.field select {
  width: 100%;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--paper);
  font-family: var(--sans);
  font-size: 14.5px;
  padding: 9px 12px;
  outline: none;
  resize: none;
  line-height: 1.5;
  -webkit-appearance: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.field input::placeholder,
.field textarea::placeholder { color: #4a4038; font-style: italic; }
.field input:focus,
.field textarea:focus,
.field select:focus { border-color: var(--copper); box-shadow: 0 0 0 3px rgba(184,124,76,0.12); }
.field textarea { min-height: 66px; }
.field-hint { font-size: 10.5px; color: var(--ash); margin-top: 3px; font-family: var(--mono); }

.field select {
  cursor: pointer;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b6258' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 12px center;
  padding-right: 34px;
}

/* ── Buttons ─────────────────────────────────────────────────────────────── */
.btn {
  flex: 1;
  padding: 11px 14px;
  border-radius: var(--radius);
  border: none;
  font-family: var(--sans);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  cursor: pointer;
  transition: background 0.15s, transform 0.08s, opacity 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  white-space: nowrap;
}
.btn:active { transform: scale(0.97); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }

.btn-primary { background: var(--copper); color: #fff; }
.btn-primary:hover:not(:disabled) { background: var(--copper-d); }

.btn-ghost {
  background: #2a2520;
  color: var(--dust);
  border: 1px solid var(--border);
}
.btn-ghost:hover:not(:disabled) { background: #332e28; border-color: var(--copper); color: var(--paper); }

.btn-danger { background: rgba(192,57,43,0.15); color: #e57575; border: 1px solid rgba(192,57,43,0.3); }
.btn-danger:hover:not(:disabled) { background: rgba(192,57,43,0.25); }

.btn-sm { flex: 0 0 auto; padding: 7px 12px; font-size: 11px; }

/* ── Vorlagen-Tab ────────────────────────────────────────────────────────── */
#tab-vorlagen {
  gap: 0;
}

#tpl-controls {
  flex-shrink: 0;
  padding: 14px 26px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-bottom: 1px solid var(--border);
}

#tpl-controls .row {
  display: flex;
  gap: 6px;
  align-items: center;
}

#tpl-select-editor {
  flex: 1;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--paper);
  font-family: var(--sans);
  font-size: 13.5px;
  padding: 8px 12px;
  outline: none;
  cursor: pointer;
  -webkit-appearance: none;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' fill='none'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b6258' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  padding-right: 28px;
  transition: border-color 0.15s;
}
#tpl-select-editor:focus { border-color: var(--copper); }

#tpl-readonly-note {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ash);
  padding: 5px 0;
  display: none;
}
#tpl-readonly-note.visible { display: block; }

#tpl-editor-wrapper {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  padding: 0;
}

#tpl-editor {
  flex: 1;
  min-height: 0;
  width: 100%;
  background: #181410;
  border: none;
  color: #c8c0b4;
  font-family: var(--mono);
  font-size: 12.5px;
  padding: 16px 20px;
  outline: none;
  resize: none;
  line-height: 1.7;
  tab-size: 2;
}
#tpl-editor:focus { background: #1a1612; }

#tpl-save-bar {
  flex-shrink: 0;
  padding: 10px 26px 16px;
  display: flex;
  gap: 8px;
  align-items: center;
  border-top: 1px solid var(--border);
}

#tpl-save-status {
  flex: 1;
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--ash);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
#tpl-save-status.ok    { color: #6dbb6d; }
#tpl-save-status.error { color: #e57575; }

/* ── Verlauf-Tab ─────────────────────────────────────────────────────────── */
#tab-verlauf {
  overflow: hidden;
}

#verlauf-header {
  flex-shrink: 0;
  padding: 12px 26px 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}
#verlauf-header span {
  flex: 1;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--ash);
}

#verlauf-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px 26px 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  scrollbar-width: thin;
  scrollbar-color: var(--border) transparent;
}
#verlauf-list::-webkit-scrollbar { width: 3px; }
#verlauf-list::-webkit-scrollbar-thumb { background: var(--border); }

.verlauf-entry {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 11px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.verlauf-entry:hover { border-color: var(--copper); background: #252018; }

.verlauf-subject {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--paper);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.verlauf-meta {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--ash);
  display: flex;
  gap: 10px;
}
.verlauf-preview {
  font-size: 11.5px;
  color: #5a5048;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-top: 2px;
}

.verlauf-empty {
  text-align: center;
  padding: 40px 0;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--ash);
  opacity: 0.6;
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
  height: 48px;
  background: #191512;
  border-bottom: 1px solid #2a2520;
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 14px;
}
.toolbar-title {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ash);
  flex: 1;
}

#preview-mode-badge {
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  padding: 3px 9px;
  border-radius: 99px;
  background: rgba(184,124,76,0.1);
  color: var(--copper);
  border: 1px solid rgba(184,124,76,0.25);
}
#preview-mode-badge.pdf { background: rgba(80,160,80,0.1); color: #6dbb6d; border-color: rgba(80,160,80,0.25); }

#status-pill {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.06em;
  padding: 3px 10px;
  border-radius: 99px;
  background: #2a2520;
  color: var(--ash);
  transition: all 0.2s;
}
#status-pill.compiling { background: rgba(184,124,76,0.15); color: var(--copper); }
#status-pill.ready     { background: rgba(80,160,80,0.15);  color: #6dbb6d; }
#status-pill.error     { background: rgba(192,57,43,0.15);  color: #e57575; }

#preview-area { flex: 1; overflow: hidden; position: relative; }

#preview-frame, #pdf-frame {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: none;
}
#pdf-frame { display: none; background: #525659; }

#spinner-overlay {
  position: absolute;
  inset: 0;
  background: rgba(15,14,12,0.7);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 10;
}
.spinner {
  width: 32px; height: 32px;
  border: 2px solid #3a342e;
  border-top-color: var(--copper);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Fehler-Toast ────────────────────────────────────────────────────────── */
#error-toast {
  position: fixed;
  bottom: 22px; left: 50%;
  transform: translateX(-50%) translateY(90px);
  background: #3a1a18;
  border: 1px solid var(--error);
  border-radius: 4px;
  padding: 12px 20px;
  max-width: 560px;
  width: calc(100vw - 48px);
  font-family: var(--mono);
  font-size: 11px;
  color: #e57575;
  line-height: 1.55;
  z-index: 200;
  transition: transform 0.3s cubic-bezier(.16,1,.3,1);
  white-space: pre-wrap;
  word-break: break-word;
}
#error-toast.visible { transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div id="root">

  <!-- ══ Sidebar ══════════════════════════════════════════════════════════ -->
  <aside id="sidebar">

    <div id="sidebar-header">
      <div class="wordmark"><span class="wordmark-dot"></span>LaTeX PDF Studio</div>
      <div class="wordmark-sub">latex-email-daemon · Web-Oberfläche</div>
    </div>

    <!-- Tab-Leiste -->
    <div id="tab-bar">
      <button class="tab-btn active" onclick="switchTab('formular')" id="tbtn-formular">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
        Formular
      </button>
      <button class="tab-btn" onclick="switchTab('vorlagen')" id="tbtn-vorlagen">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>
        Vorlagen
      </button>
      <button class="tab-btn" onclick="switchTab('verlauf')" id="tbtn-verlauf">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        Verlauf
      </button>
    </div>

    <!-- Tab-Panels -->
    <div id="tab-panels">

      <!-- ── Formular ── -->
      <div class="tab-panel active" id="tab-formular">

        <div id="form-top">

          <div class="section-label">Vorlage</div>
          <div class="field">
            <label for="f-template">Vorlagendatei</label>
            <select id="f-template" onchange="onFieldChange()">
              <option value="">Wird geladen…</option>
            </select>
          </div>

          <div class="section-label">Dokument</div>
          <div class="field">
            <label for="f-subject">Betreff <span class="pt">· {{SUBJECT}}</span></label>
            <input id="f-subject" type="text" placeholder="z. B. Kündigung Mietvertrag" oninput="onFieldChange()"/>
          </div>

          <div class="section-label">Abschnitte</div>
          <div class="field">
            <label for="f-first">Empfänger <span class="pt">· {{FIRST_PARAGRAPH}}</span></label>
            <textarea id="f-first" rows="3" placeholder="Kal Meier&#10;Winkelstraße 3&#10;12345 Musterstadt" oninput="onFieldChange()"></textarea>
            <div class="field-hint">Eine Adresszeile pro Textzeile</div>
          </div>
          <div class="field">
            <label for="f-second">Datum <span class="pt">· {{SECOND_PARAGRAPH}}</span></label>
            <textarea id="f-second" rows="2" placeholder="Köln, 28. April 2026" oninput="onFieldChange()"></textarea>
          </div>
          <div class="field">
            <label for="f-third">Anrede <span class="pt">· {{THIRD_PARAGRAPH}}</span></label>
            <textarea id="f-third" rows="2" placeholder="Sehr geehrte Damen und Herren," oninput="onFieldChange()"></textarea>
          </div>

          <div class="section-label">Inhalt</div>
        </div><!-- /form-top -->

        <!-- Brieftext wächst auf verbleibenden Platz -->
        <div id="body-wrapper">
          <label for="f-body">Brieftext <span class="pt" style="font-family:var(--mono);font-size:10px;color:var(--ash);font-weight:300">· {{BODY}}</span></label>
          <textarea id="f-body" placeholder="Haupttext des Briefes…&#10;&#10;Absätze durch eine Leerzeile trennen." oninput="onFieldChange()"></textarea>
        </div>

        <div id="form-actions">
          <button class="btn btn-primary" id="btn-pdf" onclick="compilePdf(false)">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            PDF-Vorschau
          </button>
          <button class="btn btn-ghost" id="btn-dl" onclick="compilePdf(true)">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Herunterladen
          </button>
        </div>

      </div><!-- /tab-formular -->

      <!-- ── Vorlagen ── -->
      <div class="tab-panel" id="tab-vorlagen">

        <div id="tpl-controls">
          <div class="row">
            <select id="tpl-select-editor" onchange="loadTemplateContent(this.value)">
              <option value="">Wird geladen…</option>
            </select>
            <button class="btn btn-ghost btn-sm" onclick="newTemplate()">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
              Neu
            </button>
            <button class="btn btn-danger btn-sm" id="tpl-del-btn" onclick="deleteTemplate()">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg>
              Löschen
            </button>
          </div>
          <div id="tpl-readonly-note">⚠ Schreibgeschützt — Änderungen werden als neue Datei in templates/ gespeichert</div>
        </div>

        <div id="tpl-editor-wrapper">
          <textarea id="tpl-editor" spellcheck="false" placeholder="\documentclass{letter}&#10;% Vorlage hier eingeben…"></textarea>
        </div>

        <div id="tpl-save-bar">
          <div id="tpl-save-status">Ungespeichert</div>
          <button class="btn btn-primary btn-sm" onclick="saveTemplate()" style="flex:0 0 auto;padding:8px 16px;">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
            Speichern
          </button>
        </div>

      </div><!-- /tab-vorlagen -->

      <!-- ── Verlauf ── -->
      <div class="tab-panel" id="tab-verlauf">

        <div id="verlauf-header">
          <span>Verlauf</span>
          <button class="btn btn-danger btn-sm" onclick="clearHistory()">Alles löschen</button>
        </div>

        <div id="verlauf-list">
          <div class="verlauf-empty">Noch keine Dokumente erstellt.</div>
        </div>

      </div><!-- /tab-verlauf -->

    </div><!-- /tab-panels -->
  </aside><!-- /sidebar -->

  <!-- ══ Vorschau ══════════════════════════════════════════════════════════ -->
  <main id="preview-pane">
    <div id="preview-toolbar">
      <span class="toolbar-title">Vorschau</span>
      <span id="preview-mode-badge">Sofortvorschau</span>
      <span id="status-pill">bereit</span>
    </div>
    <div id="preview-area">
      <!-- Sofortvorschau (HTML-Brief) -->
      <iframe id="preview-frame" title="Sofortvorschau" src="about:blank"></iframe>
      <!-- Echtes PDF (nach Kompilierung) -->
      <iframe id="pdf-frame"     title="PDF-Vorschau"></iframe>
      <div id="spinner-overlay"><div class="spinner"></div></div>
    </div>
  </main>

</div><!-- /root -->
<div id="error-toast"></div>

<script>
'use strict';

/* ══════════════════════════════════════════════════════════════════════════
   Zustand
══════════════════════════════════════════════════════════════════════════ */
const S = {
  activeTab:       'formular',
  previewMode:     'instant',   // 'instant' | 'pdf'
  isCompiling:     false,
  instantTimer:    null,
  currentPdfUrl:   null,
  currentInstUrl:  null,
  templateWritable: true,
  templateList:    [],
};

/* ══════════════════════════════════════════════════════════════════════════
   Hilfsfunktionen
══════════════════════════════════════════════════════════════════════════ */
const $  = id => document.getElementById(id);
const fv = id => $(id)?.value ?? '';

function escHtml(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function setStatus(cls, text) {
  const p = $('status-pill');
  p.className   = cls;
  p.textContent = text;
}

let _toastTimer = null;
function showError(msg) {
  const t = $('error-toast');
  t.textContent = msg;
  t.classList.add('visible');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('visible'), 10000);
}

/* ══════════════════════════════════════════════════════════════════════════
   Tabs
══════════════════════════════════════════════════════════════════════════ */
function switchTab(name) {
  S.activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  $(`tbtn-${name}`).classList.add('active');
  $(`tab-${name}`).classList.add('active');

  if (name === 'verlauf') renderHistory();
  if (name === 'vorlagen' && !$('tpl-select-editor').value) loadTemplateList(true);
}

/* ══════════════════════════════════════════════════════════════════════════
   Sofortvorschau (HTML-Brief, kein pdflatex)
══════════════════════════════════════════════════════════════════════════ */
function generateLetterHtml() {
  const subject  = fv('f-subject');
  const first    = fv('f-first');
  const second   = fv('f-second');
  const third    = fv('f-third');
  const body     = fv('f-body');

  const ph = s => s ? escHtml(s) : '';

  return `<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<link href="https://fonts.googleapis.com/css2?family=Lora:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{background:#606060;min-height:100%;font-family:'Lora','Times New Roman',serif}
body{display:flex;justify-content:center;align-items:flex-start;padding:28px 20px;min-height:100vh}
.page{
  background:#fff;width:100%;max-width:740px;
  min-height:calc(740px * 1.414);
  padding:64px 72px 80px;
  box-shadow:0 6px 40px rgba(0,0,0,.55);
  font-size:11.5pt;line-height:1.55;color:#111;
  position:relative;
}
.addr-window{margin-bottom:36px}
.return-addr{
  font-family:sans-serif;font-size:7.5pt;color:#aaa;
  border-bottom:.5px solid #ddd;padding-bottom:5px;margin-bottom:9px;
  letter-spacing:.04em;
}
.recipient{font-size:11pt;white-space:pre-line;min-height:5em;line-height:1.6}
.date-block{text-align:right;margin-bottom:28px;white-space:pre-line;font-size:11pt}
.subject-line{font-weight:700;font-size:11.5pt;margin-bottom:18px}
.salutation{margin-bottom:18px;white-space:pre-line}
.body-text{white-space:pre-wrap;text-align:justify;line-height:1.65}
.hint{color:#ccc;font-style:italic;font-size:9pt;font-family:sans-serif}
.stamp{
  position:absolute;bottom:20px;right:24px;
  font-family:sans-serif;font-size:7.5pt;color:#ddd;font-style:italic;
}
</style>
</head>
<body>
<div class="page">
  <div class="addr-window">
    <div class="return-addr">Sofortvorschau · LaTeX PDF Studio</div>
    <div class="recipient">${ph(first) || '<span class="hint">Empfänger…</span>'}</div>
  </div>
  <div class="date-block">${ph(second) || '<span class="hint">Datum…</span>'}</div>
  <div class="subject-line">${ph(subject) || '<span class="hint">Betreff…</span>'}</div>
  <div class="salutation">${ph(third) || '<span class="hint">Anrede…</span>'}</div>
  <div class="body-text">${ph(body) || '<span class="hint">Brieftext…</span>'}</div>
  <div class="stamp">Sofortvorschau · kein echtes PDF</div>
</div>
</body></html>`;
}

function renderInstantPreview() {
  const html = generateLetterHtml();
  const blob = new Blob([html], {type:'text/html'});

  if (S.currentInstUrl) URL.revokeObjectURL(S.currentInstUrl);
  S.currentInstUrl = URL.createObjectURL(blob);

  $('preview-frame').src = S.currentInstUrl;

  // Falls gerade PDF angezeigt → nichts überschreiben
  if (S.previewMode === 'instant') {
    $('preview-frame').style.display = 'block';
    $('pdf-frame').style.display     = 'none';
    $('preview-mode-badge').textContent = 'Sofortvorschau';
    $('preview-mode-badge').classList.remove('pdf');
    setStatus('', 'bereit');
  }
}

function onFieldChange() {
  // Wenn gerade PDF angezeigt → zurück zur Sofortvorschau
  if (S.previewMode === 'pdf') {
    S.previewMode = 'instant';
    $('pdf-frame').style.display     = 'none';
    $('preview-frame').style.display = 'block';
    $('preview-mode-badge').textContent = 'Sofortvorschau';
    $('preview-mode-badge').classList.remove('pdf');
  }
  clearTimeout(S.instantTimer);
  S.instantTimer = setTimeout(renderInstantPreview, 120);
}

/* ══════════════════════════════════════════════════════════════════════════
   Echte PDF-Kompilierung
══════════════════════════════════════════════════════════════════════════ */
function buildPayload() {
  return {
    template:          fv('f-template') || null,
    subject:           fv('f-subject'),
    first_paragraph:   fv('f-first'),
    second_paragraph:  fv('f-second'),
    third_paragraph:   fv('f-third'),
    body:              fv('f-body'),
    body_is_html:      false,
  };
}

async function compilePdf(forDownload = false) {
  if (S.isCompiling) return;
  S.isCompiling = true;

  setStatus('compiling', 'kompiliert…');
  $('spinner-overlay').style.display = 'flex';
  $('btn-pdf').disabled = true;
  $('btn-dl').disabled  = true;

  const endpoint = forDownload ? '/api/download' : '/api/compile';
  const payload  = buildPayload();

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });

    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).error || msg; } catch(_) {}
      throw new Error(msg);
    }

    const blob = await res.blob();

    if (forDownload) {
      const safe = (fv('f-subject') || 'dokument').replace(/[^a-zA-Z0-9_\-]/g,'_').slice(0,50);
      const url  = URL.createObjectURL(blob);
      const a    = Object.assign(document.createElement('a'), {href:url, download: safe+'.pdf'});
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 15000);
      setStatus('ready', 'heruntergeladen ✓');
      addToHistory(payload);
    } else {
      if (S.currentPdfUrl) URL.revokeObjectURL(S.currentPdfUrl);
      S.currentPdfUrl = URL.createObjectURL(blob);
      S.previewMode   = 'pdf';
      $('pdf-frame').src            = S.currentPdfUrl;
      $('pdf-frame').style.display  = 'block';
      $('preview-frame').style.display = 'none';
      $('preview-mode-badge').textContent = 'PDF ✓';
      $('preview-mode-badge').classList.add('pdf');
      setStatus('ready', 'fertig ✓');
      addToHistory(payload);
    }
  } catch(e) {
    setStatus('error', 'Fehler');
    showError('Kompilierfehler:\n' + e.message);
  } finally {
    $('spinner-overlay').style.display = 'none';
    S.isCompiling = false;
    $('btn-pdf').disabled = false;
    $('btn-dl').disabled  = false;
  }
}

/* ══════════════════════════════════════════════════════════════════════════
   Vorlagen laden (für beide Dropdowns synchron)
══════════════════════════════════════════════════════════════════════════ */
async function loadTemplateList(selectFirst = false) {
  try {
    const res  = await fetch('/api/templates');
    S.templateList = await res.json();
  } catch(e) {
    S.templateList = [];
  }

  const list = S.templateList;

  // Formular-Dropdown
  const fSel = $('f-template');
  const prev = fSel.value;
  fSel.innerHTML = '';
  if (list.length === 0) {
    fSel.innerHTML = '<option value="">⚠ Keine Vorlagen gefunden</option>';
  } else {
    list.forEach(t => {
      const o = document.createElement('option');
      o.value = t.name; o.textContent = t.name;
      if (t.name === prev) o.selected = true;
      fSel.appendChild(o);
    });
  }

  // Editor-Dropdown
  const eSel = $('tpl-select-editor');
  const eprev = eSel.value;
  eSel.innerHTML = '';
  if (list.length === 0) {
    eSel.innerHTML = '<option value="">Keine Vorlagen</option>';
  } else {
    list.forEach(t => {
      const o = document.createElement('option');
      o.value = t.name; o.textContent = t.name + (t.writable ? '' : ' 🔒');
      if (t.name === eprev || (selectFirst && !eprev)) o.selected = true;
      eSel.appendChild(o);
    });
    if (selectFirst || !eprev) loadTemplateContent(eSel.value);
  }
}

async function loadTemplateContent(name) {
  if (!name) return;
  $('tpl-editor').value = 'Wird geladen…';
  $('tpl-save-status').textContent = '';
  $('tpl-save-status').className   = '';

  try {
    const res  = await fetch(`/api/templates/${encodeURIComponent(name)}`);
    const data = await res.json();
    $('tpl-editor').value = data.content || '';
    S.templateWritable    = data.writable;
    $('tpl-editor').readOnly = false;

    const note = $('tpl-readonly-note');
    if (!data.writable) {
      note.classList.add('visible');
      $('tpl-del-btn').disabled = true;
    } else {
      note.classList.remove('visible');
      $('tpl-del-btn').disabled = false;
    }
    $('tpl-save-status').textContent = 'Gespeichert';
    $('tpl-save-status').className   = 'ok';
  } catch(e) {
    $('tpl-editor').value = '// Fehler beim Laden';
    $('tpl-save-status').textContent = 'Ladefehler';
    $('tpl-save-status').className   = 'error';
  }
}

async function saveTemplate() {
  const name    = $('tpl-select-editor').value;
  const content = $('tpl-editor').value;
  if (!name) return;

  $('tpl-save-status').textContent = 'Speichert…';
  $('tpl-save-status').className   = '';

  try {
    const res  = await fetch(`/api/templates/${encodeURIComponent(name)}`, {
      method:  'PUT',
      headers: {'Content-Type':'application/json'},
      body:    JSON.stringify({content}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    $('tpl-save-status').textContent = '✓ Gespeichert';
    $('tpl-save-status').className   = 'ok';
    // Reload list in case a new copy was created in templates/
    await loadTemplateList();
    $('tpl-select-editor').value = data.name;
    // Editor-Note zurücksetzen
    $('tpl-readonly-note').classList.remove('visible');
    $('tpl-del-btn').disabled = false;
  } catch(e) {
    $('tpl-save-status').textContent = '✗ ' + e.message;
    $('tpl-save-status').className   = 'error';
  }
}

async function deleteTemplate() {
  const name = $('tpl-select-editor').value;
  if (!name) return;
  if (!confirm(`Vorlage "${name}" wirklich löschen?`)) return;

  try {
    const res  = await fetch(`/api/templates/${encodeURIComponent(name)}`, {method:'DELETE'});
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    $('tpl-editor').value = '';
    $('tpl-save-status').textContent = 'Gelöscht';
    $('tpl-save-status').className   = 'ok';
    await loadTemplateList(true);
  } catch(e) {
    showError('Löschen fehlgeschlagen:\n' + e.message);
  }
}

async function newTemplate() {
  const name = prompt('Name der neuen Vorlage (ohne .tex):');
  if (!name) return;
  const content = `% Neue Vorlage: ${name}
% Alle fünf Platzhalter müssen vorhanden sein:
% {{SUBJECT}}, {{FIRST_PARAGRAPH}}, {{SECOND_PARAGRAPH}}, {{THIRD_PARAGRAPH}}, {{BODY}}

\\documentclass[12pt]{letter}
\\usepackage[utf8]{inputenc}
\\usepackage[T1]{fontenc}
\\usepackage[ngerman]{babel}

\\begin{document}
\\begin{letter}{{{FIRST_PARAGRAPH}}}

\\date{{{SECOND_PARAGRAPH}}}
\\opening{{{THIRD_PARAGRAPH}}}

{{BODY}}

\\closing{Mit freundlichen Grüßen,}

\\end{letter}
\\end{document}
`;
  try {
    const fname = name.endsWith('.tex') ? name : name + '.tex';
    const res   = await fetch(`/api/templates/${encodeURIComponent(fname)}`, {
      method:  'PUT',
      headers: {'Content-Type':'application/json'},
      body:    JSON.stringify({content}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);
    await loadTemplateList();
    $('tpl-select-editor').value = data.name;
    loadTemplateContent(data.name);
  } catch(e) {
    showError('Anlegen fehlgeschlagen:\n' + e.message);
  }
}

// Markiere Editor als "ungespeichert" bei Änderungen
$('tpl-editor').addEventListener('input', () => {
  $('tpl-save-status').textContent = 'Ungespeichert';
  $('tpl-save-status').className   = '';
});

/* ══════════════════════════════════════════════════════════════════════════
   Verlauf (localStorage)
══════════════════════════════════════════════════════════════════════════ */
const HISTORY_KEY = 'led_history_v1';
const MAX_HISTORY = 60;

function loadHistoryData() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); }
  catch(_) { return []; }
}

function saveHistoryData(entries) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(entries)); } catch(_) {}
}

function addToHistory(payload) {
  const entries = loadHistoryData();
  entries.unshift({
    id:       Date.now().toString(36),
    subject:  payload.subject || '(kein Betreff)',
    template: payload.template || '—',
    ts:       new Date().toISOString(),
    fields: {
      first:  payload.first_paragraph  || '',
      second: payload.second_paragraph || '',
      third:  payload.third_paragraph  || '',
      body:   payload.body             || '',
    },
  });
  saveHistoryData(entries.slice(0, MAX_HISTORY));
}

function renderHistory() {
  const list    = $('verlauf-list');
  const entries = loadHistoryData();

  if (entries.length === 0) {
    list.innerHTML = '<div class="verlauf-empty">Noch keine Dokumente erstellt.</div>';
    return;
  }

  list.innerHTML = '';
  entries.forEach(e => {
    const dt = new Date(e.ts);
    const dateStr = dt.toLocaleDateString('de-DE', {day:'2-digit',month:'2-digit',year:'numeric'})
                  + ' ' + dt.toLocaleTimeString('de-DE', {hour:'2-digit',minute:'2-digit'});

    const div = document.createElement('div');
    div.className = 'verlauf-entry';
    div.innerHTML = `
      <div class="verlauf-subject">${escHtml(e.subject)}</div>
      <div class="verlauf-meta">
        <span>${escHtml(dateStr)}</span>
        <span>${escHtml(e.template)}</span>
      </div>
      <div class="verlauf-preview">${escHtml((e.fields.body || '').slice(0, 80))}</div>
    `;
    div.addEventListener('click', () => loadHistoryEntry(e));
    list.appendChild(div);
  });
}

function loadHistoryEntry(e) {
  $('f-subject').value = e.subject === '(kein Betreff)' ? '' : e.subject;
  $('f-first').value   = e.fields.first  || '';
  $('f-second').value  = e.fields.second || '';
  $('f-third').value   = e.fields.third  || '';
  $('f-body').value    = e.fields.body   || '';

  // Vorlage setzen falls vorhanden
  if (e.template && e.template !== '—') {
    const opt = [...$('f-template').options].find(o => o.value === e.template);
    if (opt) $('f-template').value = e.template;
  }

  switchTab('formular');
  renderInstantPreview();
}

function clearHistory() {
  if (!confirm('Verlauf wirklich komplett löschen?')) return;
  saveHistoryData([]);
  renderHistory();
}

/* ══════════════════════════════════════════════════════════════════════════
   Start
══════════════════════════════════════════════════════════════════════════ */
loadTemplateList();
renderInstantPreview();
</script>
</body>
</html>
"""

@app.get("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)

# ── Einstiegspunkt ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    templates = list_templates()
    if templates:
        log.info("Gefundene Vorlagen (%d):", len(templates))
        for t in templates:
            log.info("  • %s  (%s)%s", t["name"], t["path"], "" if t["writable"] else " [schreibgeschützt]")
    else:
        log.warning("Keine Vorlagen gefunden! LATEX_TEMPLATE_DIR oder LATEX_TEMPLATE_FILE setzen.")

    log.info("Starte Web-Dashboard auf http://%s:%d", WEB_HOST, WEB_PORT)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=WEB_DEBUG)
