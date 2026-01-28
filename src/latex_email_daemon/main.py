import os
import time
import signal
import sys
import json
import subprocess
from datetime import datetime
from typing import List

from imapclient import IMAPClient
import pyzmail
from dotenv import load_dotenv

# ---- Load .env ----
load_dotenv()

# ---- Config ----
IMAP_SERVER = os.getenv("IMAP_SERVER", "imap.gmail.com")
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
TARGET_ADDRESS = os.getenv("TARGET_ADDRESS")
ALLOWED_SENDER_DOMAIN = os.getenv("ALLOWED_SENDER_DOMAIN", "")
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", 300))
STATE_FILE = os.getenv("STATE_FILE", "last_seen_uid.txt")
JSON_DIR = os.getenv("JSON_DIR", "emails")
FETCH_BATCH_SIZE = 100  # messages per batch

required_vars = [IMAP_SERVER, EMAIL_ACCOUNT, EMAIL_PASSWORD, TARGET_ADDRESS]
if not all(required_vars):
    raise RuntimeError("Missing required environment variables")

os.makedirs(JSON_DIR, exist_ok=True)

shutdown_requested = False

# ---- graceful shutdown ----
def request_shutdown(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    print(f"\n🛑 Shutdown requested (signal {signum}). Finishing current work…")

signal.signal(signal.SIGINT, request_shutdown)
signal.signal(signal.SIGTERM, request_shutdown)

# ---- state persistence ----
def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            value = f.read().strip()
            return int(value) if value else 0
    except Exception:
        return 0

def save_state(last_uid):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(last_uid))
    os.replace(tmp, STATE_FILE)

# ---- helpers ----
def connect() -> IMAPClient:
    print("🔌 Connecting to Gmail…")
    server = IMAPClient(IMAP_SERVER, ssl=True)
    server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
    server.select_folder("INBOX")
    print("📬 Connected to INBOX")
    return server

def chunked(items: List[int], size: int):
    for i in range(0, len(items), size):
        yield items[i:i+size]

def decode_payload(part, charset="utf-8"):
    """Safely decode email part payload."""
    if not part:
        return None
    try:
        payload = part.get_payload()
        if isinstance(payload, bytes):
            return payload.decode(charset, errors="replace")
        return str(payload)
    except Exception as e:
        print(f"⚠️ Failed to decode payload: {e}")
        return None

def save_email_json(full_msg, uid):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{uid}.json"
    path = os.path.join(JSON_DIR, filename)

    # Decode text and HTML parts properly
    body_text = decode_payload(
        full_msg.text_part,
        full_msg.text_part.charset or "utf-8"
    ) if full_msg.text_part else None

    body_html = decode_payload(
        full_msg.html_part,
        full_msg.html_part.charset or "utf-8"
    ) if full_msg.html_part else None

    data = {
        "uid": uid,
        "subject": full_msg.get_subject(),
        "from": full_msg.get_addresses("from"),
        "to": full_msg.get_addresses("to"),
        "cc": full_msg.get_addresses("cc") or [],
        "bcc": full_msg.get_addresses("bcc") or [],
        "text": body_text,
        "html": body_html
    }

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return path, body_text, body_html

def process_new_messages(server: IMAPClient, last_seen_uid: int) -> int:
    search_range = f"{last_seen_uid + 1}:*"
    uids = server.search(["UID", search_range])
    if not uids:
        print("📭 No new messages")
        return last_seen_uid

    total = len(uids)
    print(f"📥 Found {total} new message(s)")

    for batch_num, uid_batch in enumerate(chunked(sorted(uids), FETCH_BATCH_SIZE), start=1):
        if shutdown_requested:
            print("🛑 Shutdown detected before batch fetch")
            break
        print(f"📦 Fetching batch {batch_num} ({len(uid_batch)} messages)")

        # Fetch full message for body
        messages = server.fetch(uid_batch, ["RFC822"])

        for uid in uid_batch:
            if shutdown_requested:
                print("🛑 Shutdown detected mid-batch")
                return last_seen_uid

            try:
                full_msg = pyzmail.PyzMessage.factory(messages[uid][b"RFC822"])
            except Exception as e:
                print(f"⚠️ Failed to parse message UID {uid}: {e}")
                last_seen_uid = max(last_seen_uid, uid)
                save_state(last_seen_uid)
                continue

            to_emails = [email.lower() for _, email in full_msg.get_addresses("to")]

            # Check target address
            if TARGET_ADDRESS.lower() not in to_emails:
                last_seen_uid = max(last_seen_uid, uid)
                save_state(last_seen_uid)
                continue

            # Check allowed sender domain
            from_emails = [email for _, email in full_msg.get_addresses("from")]
            if ALLOWED_SENDER_DOMAIN:
                if not any(email.lower().endswith(f"@{ALLOWED_SENDER_DOMAIN.lower()}") for email in from_emails):
                    print(f"❌ Ignored email from disallowed domain: {from_emails}")
                    last_seen_uid = max(last_seen_uid, uid)
                    save_state(last_seen_uid)
                    continue

            print(f"\n📨 MATCHING EMAIL UID {uid}")
            print("From:", from_emails)
            print("Subject:", full_msg.get_subject())

            # Save JSON and call handler
            try:
                json_path, body_text, body_html = save_email_json(full_msg, uid)

                # Print snippet from the already-decoded body
                if body_text:
                    snippet = body_text[:100].replace('\n', ' ')
                    print(f"Body snippet: {snippet}")
                elif body_html:
                    snippet = body_html[:100].replace('\n', ' ')
                    print(f"Body snippet: <HTML> {snippet}")
                else:
                    print("Body snippet: <empty>")

                print(f"💾 Email saved as JSON: {json_path}")

                # Use subprocess.run with timeout for better control
                result = subprocess.run(
                    [sys.executable, "handle_email.py", json_path],
                    timeout=120,  # 2 minute timeout for PDF generation
                    capture_output=True,
                    text=True
                )

                if result.returncode == 0:
                    print(f"⚡ Successfully processed UID {uid}")
                    if result.stdout:
                        print(f"Handler output: {result.stdout}")
                else:
                    print(f"⚠️ Handler failed with code {result.returncode}")
                    if result.stderr:
                        print(f"Handler error: {result.stderr}")
                    # Don't delete JSON on failure for debugging

            except subprocess.TimeoutExpired:
                print(f"⚠️ Handler timed out for UID {uid}")
            except Exception as e:
                print(f"⚠️ Failed to save/invoke handler: {e}")
                import traceback
                traceback.print_exc()

            last_seen_uid = max(last_seen_uid, uid)
            save_state(last_seen_uid)

    print(f"💾 last_seen_uid saved = {last_seen_uid}")
    return last_seen_uid

# ---- main loop ----
def watch_inbox():
    last_seen_uid = load_state()
    print(f"🔁 Starting from last_seen_uid = {last_seen_uid}")

    # Initial run skip only if last_seen_uid is 0
    if last_seen_uid == 0:
        try:
            with connect() as server:
                print("⏩ Initial run detected. Skipping old emails…")
                all_uids = server.search(["ALL"])
                if all_uids:
                    last_seen_uid = max(all_uids)
                    save_state(last_seen_uid)
                    print(f"⏩ Skipped {len(all_uids)} old message(s). Starting from UID {last_seen_uid}")
        except Exception as e:
            print("⚠️ Could not connect for initial UID fetch:", e)
            time.sleep(5)

    while not shutdown_requested:
        try:
            with connect() as server:
                last_seen_uid = process_new_messages(server, last_seen_uid)

                while not shutdown_requested:
                    print(f"😴 Entering IDLE (timeout={IDLE_TIMEOUT}s)")
                    server.idle()
                    responses = server.idle_check(timeout=IDLE_TIMEOUT)
                    server.idle_done()

                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    if responses:
                        print(f"\n🔔 {now} | Server updates: {responses}")
                        last_seen_uid = process_new_messages(server, last_seen_uid)
                    else:
                        print(f"\n⏰ {now} | IDLE timeout, refreshing")

        except Exception as e:
            if shutdown_requested:
                break
            print("\n⚠️ Connection error. Reconnecting…")
            print("Reason:", e)
            import traceback
            traceback.print_exc()
            time.sleep(10)

    print("✅ Clean shutdown complete")
    save_state(last_seen_uid)
    sys.exit(0)

if __name__ == "__main__":
    watch_inbox()
