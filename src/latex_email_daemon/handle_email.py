import sys
import os
import json
import subprocess
import smtplib
import re
from email.message import EmailMessage
from dotenv import load_dotenv
from bs4 import BeautifulSoup, NavigableString

# ---- Load environment variables ----
load_dotenv()

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SMTP_SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SMTP_SENDER_PASSWORD")
PDF_DIR = os.getenv("PDF_DIR", "pdfs")
LATEX_TEMPLATE_FILE = os.getenv("LATEX_TEMPLATE_FILE", "template.tex")
EMAIL_BODY_TEXT = os.getenv("EMAIL_BODY_TEXT", "Im Anhang befindet sich die gewünschte PDF.\n\nDies ist eine automatisch generierte Email. Beep. Boop.")

# ---- Sanity check ----
if not SENDER_EMAIL or not SENDER_PASSWORD:
    print("⚠️ SENDER_EMAIL or SENDER_PASSWORD not set in environment")
    sys.exit(1)

os.makedirs(PDF_DIR, exist_ok=True)

# ---- LaTeX escaping ----
# Compile regex pattern once for performance
LATEX_ESCAPE_PATTERN = re.compile(r'[\\%$#_{}~^&]')
LATEX_REPLACEMENTS = {
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
    """Escape special LaTeX characters in text."""
    if not text:
        return ""
    return LATEX_ESCAPE_PATTERN.sub(lambda m: LATEX_REPLACEMENTS[m.group()], text)

# ---- Filename sanitization ----
def sanitize_filename(text: str, max_length: int = 50) -> str:
    """
    Convert text to a safe ASCII-only filename.
    Handles German umlauts and removes all non-ASCII characters.
    """
    # German umlaut replacements
    replacements = {
        'ä': 'ae', 'ö': 'oe', 'ü': 'ue',
        'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue',
        'ß': 'ss'
    }

    # Replace umlauts
    for umlaut, replacement in replacements.items():
        text = text.replace(umlaut, replacement)

    # Remove or replace any remaining non-ASCII characters
    text = text.encode('ascii', 'ignore').decode('ascii')

    # Replace unsafe filename characters with underscore (only allow a-z, A-Z, 0-9, dash, underscore)
    text = re.sub(r'[^a-zA-Z0-9_-]+', '_', text)

    # Remove leading/trailing underscores and truncate
    text = text.strip('_')[:max_length]

    return text if text else "email"  # Fallback if everything gets stripped

# ---- HTML to LaTeX conversion ----
def html_to_latex(html_content: str) -> str:
    """
    Convert HTML content to LaTeX, preserving formatting.
    Handles: bold, italic, underline, links, lists, line breaks.
    """
    soup = BeautifulSoup(html_content, 'html.parser')

    def process_element(element):
        """Recursively process HTML elements and convert to LaTeX."""
        if isinstance(element, NavigableString):
            # Text node - escape LaTeX special chars
            return latex_escape(str(element))

        tag = element.name

        # Process children
        children_latex = ''.join(process_element(child) for child in element.children)

        # Convert based on tag type
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
            # Process list items
            items = []
            for li in element.find_all('li', recursive=False):
                item_content = ''.join(process_element(child) for child in li.children)
                items.append(f'\\item {item_content}')
            return '\\begin{itemize}\n' + '\n'.join(items) + '\n\\end{itemize}'
        elif tag == 'ol':
            # Process list items
            items = []
            for li in element.find_all('li', recursive=False):
                item_content = ''.join(process_element(child) for child in li.children)
                items.append(f'\\item {item_content}')
            return '\\begin{enumerate}\n' + '\n'.join(items) + '\n\\end{enumerate}'
        elif tag == 'p':
            # Paragraph - add double newline for separation
            return children_latex + '\n\n'
        elif tag == 'li':
            # List items are handled by ul/ol, just return content
            return children_latex
        else:
            # Unknown tag - just return the processed children content
            return children_latex

    result = process_element(soup)
    return result.strip()

# ---- Paragraph splitting (for plain text) ----
def split_paragraphs(text: str):
    """
    Split text into first, second, third, and rest paragraphs with LaTeX escaping.

    Robustly handles:
    - Multiple consecutive empty lines (treated as single paragraph break)
    - Whitespace-only lines (spaces, tabs, etc. - treated as empty)
    - Mixed line endings (\n, \r\n, \r)
    - Leading/trailing empty lines (ignored)
    """
    if not text or not text.strip():
        return "", "", "", ""

    lines = text.splitlines()
    paragraphs = []
    current = []

    for line in lines:
        # Strip whitespace to check if line is empty
        # This handles spaces, tabs, and other whitespace
        if line.strip() == "":
            # Only create a paragraph if we have content
            # This prevents multiple empty lines from creating empty paragraphs
            if current:
                paragraphs.append("\n".join(current))
                current = []
        else:
            current.append(line)

    # Don't forget the last paragraph if it exists
    if current:
        paragraphs.append("\n".join(current))

    # Handle case where we have no paragraphs at all
    if not paragraphs:
        return "", "", "", ""

    first = paragraphs[0] if len(paragraphs) > 0 else ""
    second = paragraphs[1] if len(paragraphs) > 1 else ""
    third = paragraphs[2] if len(paragraphs) > 2 else ""
    rest = "\n\n".join(paragraphs[3:]) if len(paragraphs) > 3 else ""

    # Escape for LaTeX, then replace newlines with LaTeX line breaks in first three paragraphs
    # BUT: only add line breaks if the content is not empty
    first = latex_escape(first).replace("\n", r"\\") if first else ""
    second = latex_escape(second).replace("\n", r"\\") if second else ""
    third = latex_escape(third).replace("\n", r"\\") if third else ""
    rest = latex_escape(rest)

    return first, second, third, rest

# ---- Paragraph splitting (for HTML/LaTeX that's already processed) ----
def split_latex_paragraphs(latex_text: str):
    """
    Split already-converted LaTeX text into first, second, third, and rest paragraphs.
    Does NOT escape since the text is already in LaTeX format.
    """
    if not latex_text or not latex_text.strip():
        return "", "", "", ""

    # Split on double newlines (paragraph breaks)
    paragraphs = [p.strip() for p in latex_text.split('\n\n') if p.strip()]

    if not paragraphs:
        return "", "", "", ""

    first = paragraphs[0] if len(paragraphs) > 0 else ""
    second = paragraphs[1] if len(paragraphs) > 1 else ""
    third = paragraphs[2] if len(paragraphs) > 2 else ""
    rest = "\n\n".join(paragraphs[3:]) if len(paragraphs) > 3 else ""

    # Replace single newlines with LaTeX line breaks in first three paragraphs
    # BUT: only add line breaks if the content is not empty
    first = first.replace("\n", r"\\") if first else ""
    second = second.replace("\n", r"\\") if second else ""
    third = third.replace("\n", r"\\") if third else ""

    return first, second, third, rest

# ---- Load email JSON ----
if len(sys.argv) < 2:
    print("Usage: python handle_email.py <email_json_file>")
    sys.exit(1)

json_file = sys.argv[1]

try:
    with open(json_file, "r", encoding="utf-8") as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError) as e:
    print(f"⚠️ Failed to load JSON file: {e}")
    sys.exit(1)

subject_raw = data.get("subject", "No Subject")
subject_safe = sanitize_filename(subject_raw)

# Prefer HTML over text to preserve formatting
raw_body = data.get("html") or data.get("text") or ""
raw_body = raw_body.strip()
if not raw_body:
    raw_body = "No body content"

# Determine if we're processing HTML or plain text
is_html = bool(data.get("html"))

if is_html:
    # Convert HTML to LaTeX
    latex_body = html_to_latex(raw_body)
    first_paragraph, second_paragraph, third_paragraph, rest_body = split_latex_paragraphs(latex_body)
else:
    # Plain text - use original logic with escaping
    first_paragraph, second_paragraph, third_paragraph, rest_body = split_paragraphs(raw_body)

from_email = [email for _, email in data.get("from", [])]
cc_emails = [email for _, email in data.get("cc", [])] if data.get("cc") else []
bcc_emails = [email for _, email in data.get("bcc", [])] if data.get("bcc") else []
all_recipients = list(set(from_email + cc_emails + bcc_emails))

if not all_recipients:
    print("⚠️ No recipients found in email")
    sys.exit(1)

# ---- Load LaTeX template ----
try:
    with open(LATEX_TEMPLATE_FILE, "r", encoding="utf-8") as f:
        latex_template = f.read()
except FileNotFoundError:
    print(f"⚠️ LaTeX template not found: {LATEX_TEMPLATE_FILE}")
    sys.exit(1)

# Validate template has required placeholders
required_placeholders = ["{{SUBJECT}}", "{{FIRST_PARAGRAPH}}", "{{SECOND_PARAGRAPH}}", "{{THIRD_PARAGRAPH}}", "{{BODY}}"]
missing = [p for p in required_placeholders if p not in latex_template]
if missing:
    print(f"⚠️ Template missing placeholders: {missing}")
    sys.exit(1)

latex_content = latex_template \
    .replace("{{SUBJECT}}", latex_escape(subject_raw)) \
    .replace("{{FIRST_PARAGRAPH}}", first_paragraph) \
    .replace("{{SECOND_PARAGRAPH}}", second_paragraph) \
    .replace("{{THIRD_PARAGRAPH}}", third_paragraph) \
    .replace("{{BODY}}", rest_body)

# ---- Determine unique filenames ----
def unique_filename(base_name, ext):
    """Generate unique filename to avoid overwrites."""
    filename = f"{base_name}.{ext}"
    counter = 1
    while os.path.exists(os.path.join(PDF_DIR, filename)):
        filename = f"{base_name}_{counter}.{ext}"
        counter += 1
    return os.path.join(PDF_DIR, filename)

tex_file = unique_filename(subject_safe, "tex")
pdf_file = tex_file.replace(".tex", ".pdf")

# ---- Write LaTeX to file ----
try:
    with open(tex_file, "w", encoding="utf-8") as f:
        f.write(latex_content)
except IOError as e:
    print(f"⚠️ Failed to write TeX file: {e}")
    sys.exit(1)

# ---- Compile PDF ----
try:
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-output-directory", PDF_DIR, tex_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False  # Don't raise exception, we'll check manually
    )

    # pdflatex often returns 0 even with errors when using -interaction=nonstopmode
    # So we check if the PDF was actually created
    if not os.path.exists(pdf_file):
        print("⚠️ Failed to compile PDF - no output file created")
        print(f"\n--- pdflatex STDOUT ---")
        stdout_text = result.stdout.decode('utf-8', errors='replace')
        print(stdout_text)

        if result.stderr:
            print(f"\n--- pdflatex STDERR ---")
            print(result.stderr.decode('utf-8', errors='replace'))

        # Try to show the .log file for more details
        log_file = tex_file.replace('.tex', '.log')
        if os.path.exists(log_file):
            print(f"\n--- Last 50 lines of {log_file} ---")
            try:
                with open(log_file, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                    print(''.join(lines[-50:]))
            except Exception as e:
                print(f"Could not read log file: {e}")

        sys.exit(1)

    # Check for LaTeX errors even if PDF was created
    stdout_text = result.stdout.decode('utf-8', errors='replace')
    if '! LaTeX Error:' in stdout_text or '! Emergency stop' in stdout_text:
        print(f"⚠️ Warning: PDF created but LaTeX reported errors")
        print(f"\n--- Errors from pdflatex ---")
        # Extract error lines
        for line in stdout_text.split('\n'):
            if line.startswith('!') or 'Error' in line or 'Warning' in line:
                print(line)
        print()

    print(f"✅ PDF generated: {pdf_file}")

except subprocess.TimeoutExpired:
    print("⚠️ PDF compilation timed out")
    sys.exit(1)
except FileNotFoundError:
    print("⚠️ pdflatex not found. Is LaTeX installed?")
    sys.exit(1)
except Exception as e:
    print(f"⚠️ Unexpected error during PDF compilation: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# Verify PDF was created (double-check)
if not os.path.exists(pdf_file):
    print(f"⚠️ PDF file was not created: {pdf_file}")
    sys.exit(1)

# ---- Send email ----
msg = EmailMessage()
msg["Subject"] = f"PDF: {subject_raw}"
msg["From"] = SENDER_EMAIL
msg["To"] = ", ".join(all_recipients)
msg.set_content(EMAIL_BODY_TEXT)

try:
    with open(pdf_file, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=os.path.basename(pdf_file)
        )
except IOError as e:
    print(f"⚠️ Failed to read PDF file: {e}")
    sys.exit(1)

try:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.starttls()
        try:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
        except smtplib.SMTPAuthenticationError as e:
            print("⚠️ Authentication failed. Check SENDER_EMAIL / SENDER_PASSWORD or use App Password for Gmail.")
            print(f"Server response: {e}")
            sys.exit(1)
        server.send_message(msg)
    print(f"📤 Email sent to: {all_recipients}")

    # ---- Cleanup ----
    if os.path.exists(json_file):
        os.remove(json_file)
        print(f"🗑 Deleted JSON file: {json_file}")

    # Clean up LaTeX auxiliary files and the PDF
    for ext in [".aux", ".log", ".tex", ".pdf", ".out"]:
        f = tex_file.replace(".tex", ext)
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError as e:
                print(f"⚠️ Could not delete {f}: {e}")
    print(f"🗑 Cleaned up LaTeX files and PDF")

except smtplib.SMTPException as e:
    print(f"⚠️ SMTP error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"⚠️ Failed to send email: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
