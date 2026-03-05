# latex-email-daemon

A small daemon that watches an IMAP inbox and turns incoming emails into formatted PDFs using a LaTeX template. Send it an email, get a compiled PDF back.

The intended use case is generating formal letters: you write the content, the daemon slots it into your template and mails you the result as an attachment.

## How it works

1. The daemon connects to your inbox via IMAP IDLE and waits for new messages addressed to `TARGET_ADDRESS`.
2. When one arrives (optionally filtered by sender domain), the email body is parsed — HTML formatting is preserved and converted to LaTeX equivalents.
3. The body is split into paragraphs and injected into your `template.tex` via placeholders.
4. `pdflatex` compiles the result, and the PDF is sent back to the original sender via SMTP.
5. All intermediate files are cleaned up on success.

## Template

The template is a standard `.tex` file with five placeholders:

| Placeholder | Filled with |
|---|---|
| `{{SUBJECT}}` | Email subject line |
| `{{FIRST_PARAGRAPH}}` | First paragraph of the body |
| `{{SECOND_PARAGRAPH}}` | Second paragraph |
| `{{THIRD_PARAGRAPH}}` | Third paragraph |
| `{{BODY}}` | All remaining paragraphs |

For a formal letter (e.g. using KOMA-Script's `scrlttr2`), a natural mapping is:
- `{{FIRST_PARAGRAPH}}` → recipient address
- `{{SECOND_PARAGRAPH}}` → date
- `{{THIRD_PARAGRAPH}}` → opening salutation
- `{{BODY}}` → letter body

The template is not included in this repo and is expected to be provided via a Docker volume (see below).

## Setup

### Environment variables

**IMAP (receiving)**

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMAP_SERVER` | no | `imap.gmail.com` | IMAP server hostname |
| `EMAIL_ACCOUNT` | **yes** | — | Email address to log in as |
| `EMAIL_PASSWORD` | **yes** | — | Password or app password for the account |
| `TARGET_ADDRESS` | **yes** | — | Only process emails sent to this address |
| `ALLOWED_SENDER_DOMAIN` | no | _(any)_ | If set, ignore emails from other domains |
| `IDLE_TIMEOUT` | no | `300` | Seconds before IDLE connection is refreshed |
| `STATE_FILE` | no | `last_seen_uid.txt` | Path to file storing the last processed UID |
| `JSON_DIR` | no | `emails` | Directory for temporary email JSON files |

**SMTP (sending)**

| Variable | Required | Default | Description |
|---|---|---|---|
| `SMTP_SERVER` | no | `smtp.gmail.com` | SMTP server hostname |
| `SMTP_PORT` | no | `587` | SMTP port (STARTTLS) |
| `SMTP_SENDER_EMAIL` | **yes** | — | From address for outgoing emails |
| `SMTP_SENDER_PASSWORD` | **yes** | — | Password or app password for SMTP |
| `EMAIL_BODY_TEXT` | no | _(German default)_ | Body text of the reply email |

**LaTeX**

| Variable | Required | Default | Description |
|---|---|---|---|
| `LATEX_TEMPLATE_FILE` | no | `template.tex` | Path to your LaTeX template |
| `PDF_DIR` | no | `pdfs` | Directory for compiled PDFs (cleaned up after sending) |

For Gmail you'll need to use an [App Password](https://support.google.com/accounts/answer/185833) rather than your account password.

### Docker

```bash
docker compose up -d
```

The compose file expects two named volumes:
- `daemon-data` — mounted at `/app/src/latex_email_daemon/data`, stores the UID state file
- `template-data` — mounted at `/app/src/latex_email_daemon/templates`, where you put your `template.tex`

Put your template in the `template-data` volume before starting the container.

### Running locally

```bash
pip install poetry
poetry install
cp .env.example .env  # fill in your values
cd src/latex_email_daemon
python main.py
```

You'll need `pdflatex` installed and on your `PATH` (`texlive-latex-base` and friends).

## Notes

- On first start with no state file, the daemon skips all existing emails and only processes new ones from that point on.
- If PDF compilation or sending fails, the JSON file is intentionally left in `JSON_DIR` for debugging.
- The daemon handles SIGINT/SIGTERM gracefully and finishes the current email before exiting.
