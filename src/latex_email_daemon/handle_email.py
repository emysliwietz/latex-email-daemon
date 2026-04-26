import sys
import os
import json
import smtplib
from email.message import EmailMessage
from dotenv import load_dotenv

# ---- Import shared PDF utilities ----
from pdf_utils import (
    latex_escape,
    sanitize_filename,
    html_to_latex,
    split_paragraphs,
    split_latex_paragraphs,
    compile_pdf,
)

# ---- Load environment variables ----
load_dotenv()

SMTP_SERVER         = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT           = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL        = os.getenv("SMTP_SENDER_EMAIL")
SENDER_PASSWORD     = os.getenv("SMTP_SENDER_PASSWORD")
PDF_DIR             = os.getenv("PDF_DIR", "pdfs")
LATEX_TEMPLATE_FILE = os.getenv("LATEX_TEMPLATE_FILE", "template.tex")
EMAIL_BODY_TEXT     = os.getenv(
    "EMAIL_BODY_TEXT",
    "Im Anhang befindet sich die gewünschte PDF.\n\nDies ist eine automatisch generierte Email. Beep. Boop.",
)

if not SENDER_EMAIL or not SENDER_PASSWORD:
    print("⚠️ SENDER_EMAIL or SENDER_PASSWORD not set in environment")
    sys.exit(1)

os.makedirs(PDF_DIR, exist_ok=True)

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

subject_raw  = data.get("subject", "No Subject")
subject_safe = sanitize_filename(subject_raw)

raw_body = data.get("html") or data.get("text") or ""
raw_body = raw_body.strip()
if not raw_body:
    raw_body = "No body content"

is_html = bool(data.get("html"))

if is_html:
    latex_body = html_to_latex(raw_body)
    first_paragraph, second_paragraph, third_paragraph, rest_body = split_latex_paragraphs(latex_body)
else:
    first_paragraph, second_paragraph, third_paragraph, rest_body = split_paragraphs(raw_body)

from_email = [email for _, email in data.get("from", [])]
cc_emails  = [email for _, email in data.get("cc",   [])] if data.get("cc")  else []
bcc_emails = [email for _, email in data.get("bcc",  [])] if data.get("bcc") else []
all_recipients = list(set(from_email + cc_emails + bcc_emails))

if not all_recipients:
    print("⚠️ No recipients found in email")
    sys.exit(1)

try:
    pdf_bytes = compile_pdf(
        template_file=LATEX_TEMPLATE_FILE,
        subject=subject_raw,
        first_paragraph=first_paragraph,
        second_paragraph=second_paragraph,
        third_paragraph=third_paragraph,
        body=rest_body,
    )
except RuntimeError as e:
    print(f"⚠️ {e}")
    sys.exit(1)

def unique_filename(base_name, ext):
    filename = f"{base_name}.{ext}"
    counter = 1
    while os.path.exists(os.path.join(PDF_DIR, filename)):
        filename = f"{base_name}_{counter}.{ext}"
        counter += 1
    return os.path.join(PDF_DIR, filename)

pdf_file = unique_filename(subject_safe, "pdf")

try:
    with open(pdf_file, "wb") as f:
        f.write(pdf_bytes)
    print(f"✅ PDF generated: {pdf_file}")
except IOError as e:
    print(f"⚠️ Failed to write PDF: {e}")
    sys.exit(1)

msg = EmailMessage()
msg["Subject"] = f"PDF: {subject_raw}"
msg["From"]    = SENDER_EMAIL
msg["To"]      = ", ".join(all_recipients)
msg.set_content(EMAIL_BODY_TEXT)
msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf",
                   filename=os.path.basename(pdf_file))

try:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.starttls()
        try:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
        except smtplib.SMTPAuthenticationError as e:
            print("⚠️ Authentication failed.")
            print(f"Server response: {e}")
            sys.exit(1)
        server.send_message(msg)
    print(f"📤 Email sent to: {all_recipients}")

    if os.path.exists(json_file):
        os.remove(json_file)
        print(f"🗑 Deleted JSON file: {json_file}")
    if os.path.exists(pdf_file):
        os.remove(pdf_file)
    print("🗑 Cleaned up PDF")

except smtplib.SMTPException as e:
    print(f"⚠️ SMTP error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"⚠️ Failed to send email: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
