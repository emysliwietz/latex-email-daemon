"""
pdf_utils.py — Shared PDF compilation helpers.

Extracted from handle_email.py so both the email daemon and the web
front-end can reuse LaTeX escaping, HTML→LaTeX conversion, paragraph
splitting, and pdflatex invocation without duplicating code.
"""

import os
import re
import subprocess
import tempfile
from typing import Tuple

from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------------------
# LaTeX character escaping
# ---------------------------------------------------------------------------

_LATEX_ESCAPE_PATTERN = re.compile(r'[\\%$#_{}~^&]')
_LATEX_REPLACEMENTS = {
    '\\': r'\textbackslash{}',
    '%': r'\%',
    '$': r'\$',
    '#': r'\#',
    '_': r'\_',
    '{': r'\{',
    '}': r'\}',
    '~': r'\textasciitilde{}',
    '^': r'\^{}',
    '&': r'\&',
}


def latex_escape(text: str) -> str:
    """Escape special LaTeX characters in plain text."""
    if not text:
        return ""
    return _LATEX_ESCAPE_PATTERN.sub(lambda m: _LATEX_REPLACEMENTS[m.group()], text)


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------

def sanitize_filename(text: str, max_length: int = 50) -> str:
    """Convert arbitrary text to a safe ASCII-only filename."""
    replacements = {
        'ä': 'ae', 'ö': 'oe', 'ü': 'ue',
        'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue',
        'ß': 'ss',
    }
    for umlaut, replacement in replacements.items():
        text = text.replace(umlaut, replacement)
    text = text.encode('ascii', 'ignore').decode('ascii')
    text = re.sub(r'[^a-zA-Z0-9_-]+', '_', text)
    text = text.strip('_')[:max_length]
    return text if text else "document"


# ---------------------------------------------------------------------------
# HTML → LaTeX conversion
# ---------------------------------------------------------------------------

def html_to_latex(html_content: str) -> str:
    """Convert HTML content to LaTeX, preserving bold/italic/links/lists."""
    soup = BeautifulSoup(html_content, 'html.parser')

    def process_element(element, parent_tag=None):
        if isinstance(element, NavigableString):
            text = str(element)
            return latex_escape(text) if text else ""

        tag = element.name
        children_latex = ''.join(process_element(child, tag) for child in element.children)

        if tag in ['strong', 'b']:
            return f'\\textbf{{{children_latex}}}'
        elif tag in ['em', 'i']:
            return f'\\textit{{{children_latex}}}'
        elif tag == 'u':
            return f'\\uline{{{children_latex}}}'
        elif tag == 'a':
            href = element.get('href', '')
            return f'\\href{{{latex_escape(href)}}}{{{children_latex}}}'
        elif tag == 'br':
            return '\\\\'
        elif tag == 'ul':
            items = []
            for li in element.find_all('li', recursive=False):
                item_content = ''.join(process_element(child, 'ul') for child in li.children)
                items.append(f'\\item {item_content}')
            return '\\begin{itemize}\n' + '\n'.join(items) + '\n\\end{itemize}'
        elif tag == 'ol':
            items = []
            for li in element.find_all('li', recursive=False):
                item_content = ''.join(process_element(child, 'ol') for child in li.children)
                items.append(f'\\item {item_content}')
            return '\\begin{enumerate}\n' + '\n'.join(items) + '\n\\end{enumerate}'
        elif tag == 'p':
            return children_latex + '\n\n' if children_latex.strip() else ''
        elif tag == 'div':
            if children_latex.strip() in ['', '\\\\']:
                return '\n\n'
            return children_latex + '\n\n' if children_latex.strip() else ''
        elif tag == 'li':
            return children_latex
        else:
            return children_latex

    result = process_element(soup)
    result = re.sub(r'\\\\(\\\\)+', r'\n\n', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()


# ---------------------------------------------------------------------------
# Paragraph splitting
# ---------------------------------------------------------------------------

def split_paragraphs(text: str) -> Tuple[str, str, str, str]:
    """Split plain text into (first, second, third, rest) with LaTeX escaping."""
    if not text or not text.strip():
        return "", "", "", ""

    lines = text.splitlines()
    paragraphs = []
    current = []

    for line in lines:
        if line.strip() == "":
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append("\n".join(current))

    if not paragraphs:
        return "", "", "", ""

    first  = paragraphs[0] if len(paragraphs) > 0 else ""
    second = paragraphs[1] if len(paragraphs) > 1 else ""
    third  = paragraphs[2] if len(paragraphs) > 2 else ""
    rest   = "\n\n".join(paragraphs[3:]) if len(paragraphs) > 3 else ""

    first  = latex_escape(first).replace("\n", r"\\")  if first  else ""
    second = latex_escape(second).replace("\n", r"\\") if second else ""
    third  = latex_escape(third).replace("\n", r"\\")  if third  else ""
    rest   = latex_escape(rest)

    return first, second, third, rest


def split_latex_paragraphs(latex_text: str) -> Tuple[str, str, str, str]:
    """Split already-converted LaTeX text into (first, second, third, rest)."""
    if not latex_text or not latex_text.strip():
        return "", "", "", ""

    paragraphs = [p.strip() for p in latex_text.split('\n\n') if p.strip()]

    if not paragraphs:
        return "", "", "", ""

    first  = paragraphs[0] if len(paragraphs) > 0 else ""
    second = paragraphs[1] if len(paragraphs) > 1 else ""
    third  = paragraphs[2] if len(paragraphs) > 2 else ""
    rest   = "\n\n".join(paragraphs[3:]) if len(paragraphs) > 3 else ""

    first  = first.replace("\n", r"\\")  if first  else ""
    second = second.replace("\n", r"\\") if second else ""
    third  = third.replace("\n", r"\\")  if third  else ""

    return first, second, third, rest


# ---------------------------------------------------------------------------
# Core PDF compilation
# ---------------------------------------------------------------------------

REQUIRED_PLACEHOLDERS = [
    "{{SUBJECT}}",
    "{{FIRST_PARAGRAPH}}",
    "{{SECOND_PARAGRAPH}}",
    "{{THIRD_PARAGRAPH}}",
    "{{BODY}}",
]


def compile_pdf(
    template_file: str,
    subject: str,
    first_paragraph: str,
    second_paragraph: str,
    third_paragraph: str,
    body: str,
    output_dir: str | None = None,
    base_name: str | None = None,
) -> bytes:
    """
    Fill *template_file* with the supplied field values, compile with
    pdflatex, and return the raw PDF bytes.

    Raises RuntimeError on template/compilation errors.
    All intermediate files are cleaned up automatically.
    """
    # ---- load template ----
    try:
        with open(template_file, "r", encoding="utf-8") as f:
            latex_template = f.read()
    except FileNotFoundError:
        raise RuntimeError(f"LaTeX template not found: {template_file}")

    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in latex_template]
    if missing:
        raise RuntimeError(f"Template missing placeholders: {missing}")

    latex_content = (
        latex_template
        .replace("{{SUBJECT}}", latex_escape(subject))
        .replace("{{FIRST_PARAGRAPH}}", first_paragraph)
        .replace("{{SECOND_PARAGRAPH}}", second_paragraph)
        .replace("{{THIRD_PARAGRAPH}}", third_paragraph)
        .replace("{{BODY}}", body)
    )

    # ---- compile in a temp directory ----
    with tempfile.TemporaryDirectory() as tmpdir:
        tex_path = os.path.join(tmpdir, "document.tex")
        pdf_path = os.path.join(tmpdir, "document.pdf")

        with open(tex_path, "w", encoding="utf-8") as f:
            f.write(latex_content)

        try:
            result = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-output-directory", tmpdir, tex_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        except FileNotFoundError:
            raise RuntimeError("pdflatex not found — is LaTeX installed?")
        except subprocess.TimeoutExpired:
            raise RuntimeError("pdflatex timed out after 30 seconds")

        if not os.path.exists(pdf_path):
            stdout = result.stdout.decode("utf-8", errors="replace")
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise RuntimeError(
                f"PDF compilation failed (exit {result.returncode}).\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )

        with open(pdf_path, "rb") as f:
            return f.read()
