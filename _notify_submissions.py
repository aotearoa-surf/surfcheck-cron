# -*- coding: utf-8 -*-
"""Email Che a digest of new community submissions.

Runs as the final step of the 3-hourly forecast workflow. Finds submissions
where notified_at is null, sends ONE digest email via Gmail SMTP (app
password), then stamps notified_at so nothing is sent twice.

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY, GMAIL_USER, GMAIL_APP_PASSWORD,
     NOTIFY_TO (defaults to surf@aotearoasurf.co.nz)
"""
import io, sys, os, smtplib, requests
from email.mime.text import MIMEText
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
# Sent from the site's own hosting mailbox so Gmail does NOT treat the digest
# as self-sent mail (which silently skips the Inbox - found 2026-06-13).
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.surfcheck.nz")
SMTP_USER = os.environ.get("SMTP_USER", "noreply@surfcheck.nz")
SMTP_PASS = os.environ["SMTP_PASSWORD"]
NOTIFY_TO = os.environ.get("NOTIFY_TO", "surf@aotearoasurf.co.nz")

TYPE_LABEL = {
    "error": "ERROR REPORT", "update": "UPDATE SUGGESTION", "media": "PHOTO/VIDEO",
    "webcam": "WEBCAM SUBMISSION", "feedback": "FEEDBACK", "idea": "IDEA", "broken": "BROKEN",
}


def fetch_new():
    r = requests.get(
        f"{URL}/rest/v1/submissions",
        headers=H,
        params={"notified_at": "is.null", "order": "id.asc", "limit": 50,
                "select": "id,created_at,type,page_type,entity_id,page_url,message,cam_url,video_url,image_path,name,email"},
        timeout=30)
    r.raise_for_status()
    return r.json()


def fmt(s):
    lines = [f"#{s['id']} · {TYPE_LABEL.get(s['type'], s['type'].upper())} · {s['created_at'][:16].replace('T', ' ')} UTC"]
    where = s.get("page_type") or "?"
    if s.get("entity_id"):
        where += f": {s['entity_id']}"
    lines.append(f"  Where: {where}  ({s.get('page_url') or 'no url'})")
    if s.get("message"):
        lines.append(f"  Message: {s['message']}")
    if s.get("cam_url"):
        lines.append(f"  Webcam link: {s['cam_url']}")
    if s.get("video_url"):
        lines.append(f"  Video link: {s['video_url']}")
    if s.get("image_path"):
        lines.append(f"  Photo: (private bucket) {s['image_path']} - view via _submissions.py show {s['id']}")
    who = s.get("name") or "Anonymous"
    if s.get("email"):
        who += f" <{s['email']}> (wants a reply)"
    lines.append(f"  From: {who}")
    return "\n".join(lines)


def main():
    subs = fetch_new()
    if not subs:
        print("no new submissions", flush=True)
        return

    n = len(subs)
    body = (f"{n} new submission{'s' if n != 1 else ''} on SurfCheck.nz\n"
            + "=" * 50 + "\n\n"
            + "\n\n".join(fmt(s) for s in subs)
            + "\n\n" + "=" * 50
            + "\nTriage: python _submissions.py list   (in the site repo)\n")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"SurfCheck: {n} new submission{'s' if n != 1 else ''}" \
                     + (" incl WEBCAM" if any(s["type"] == "webcam" for s in subs) else "")
    msg["From"] = f"SurfCheck <{SMTP_USER}>"
    msg["To"] = NOTIFY_TO

    s = smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30)
    s.login(SMTP_USER, SMTP_PASS)
    s.sendmail(SMTP_USER, [NOTIFY_TO], msg.as_string())
    s.quit()
    print(f"emailed digest of {n} submission(s) to {NOTIFY_TO}", flush=True)

    now = datetime.now(timezone.utc).isoformat()
    ids = ",".join(str(s["id"]) for s in subs)
    r = requests.patch(f"{URL}/rest/v1/submissions?id=in.({ids})",
                       headers=H, json={"notified_at": now}, timeout=30)
    r.raise_for_status()
    print(f"stamped notified_at on {n} row(s)", flush=True)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    main()
