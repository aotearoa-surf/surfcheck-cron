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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NZ = ZoneInfo("Pacific/Auckland")


def nz_time(iso_utc):
    """ISO UTC timestamp -> NZ display, e.g. '12 Jun 2026 11:27 PM NZT'."""
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        return dt.astimezone(NZ).strftime("%d %b %Y %I:%M %p NZT")
    except Exception:
        return iso_utc

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
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.surfcheck.nz").strip()
SMTP_USER = os.environ.get("SMTP_USER", "noreply@surfcheck.nz").strip()
# .strip() the password: pasted GitHub secrets often carry a trailing newline,
# which makes SMTP AUTH fail (the verified mailbox password is clean, so a
# trailing \n on the secret was the cron's silent email failure). Same gotcha
# that hit the CF token + Supabase key.
SMTP_PASS = os.environ["SMTP_PASSWORD"].strip()
NOTIFY_TO = os.environ.get("NOTIFY_TO", "surf@aotearoasurf.co.nz").strip()

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
    lines = [f"#{s['id']} · {TYPE_LABEL.get(s['type'], s['type'].upper())} · {nz_time(s['created_at'])}"]
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
        if s.get("_signed_url"):
            lines.append(f"  Photo: attached below - full size (link valid 7 days): {s['_signed_url']}")
        else:
            lines.append(f"  Photo: {s['image_path']} (sign failed - view via _submissions.py show {s['id']})")
    who = s.get("name") or "Anonymous"
    if s.get("email"):
        who += f" <{s['email']}> (wants a reply)"
    lines.append(f"  From: {who}")
    return "\n".join(lines)


MAX_ATTACH = 5            # at most this many photo attachments per digest
MAX_ATTACH_BYTES = 3_000_000


def sign_and_fetch(path):
    """7-day signed URL + image bytes for an uploaded photo (service key)."""
    signed = None
    try:
        r = requests.post(f"{URL}/storage/v1/object/sign/submissions/{path}",
                          headers=H, json={"expiresIn": 604800}, timeout=30)
        if r.ok:
            signed = f"{URL}/storage/v1{r.json()['signedURL']}"
    except Exception:
        pass
    blob = None
    try:
        r = requests.get(f"{URL}/storage/v1/object/submissions/{path}", headers=H, timeout=60)
        if r.ok and len(r.content) <= MAX_ATTACH_BYTES:
            blob = r.content
    except Exception:
        pass
    return signed, blob


def main():
    subs = fetch_new()
    if not subs:
        print("no new submissions", flush=True)
        return

    # Photos: signed link in the body for all, attachment for the first few,
    # so Che can review media for publishing straight from the inbox.
    attachments = []
    for s in subs:
        if s.get("image_path"):
            signed, blob = sign_and_fetch(s["image_path"])
            s["_signed_url"] = signed
            if blob is not None and len(attachments) < MAX_ATTACH:
                attachments.append((f"submission-{s['id']}.jpg", blob))

    n = len(subs)
    body = (f"{n} new submission{'s' if n != 1 else ''} on SurfCheck.nz\n"
            + "=" * 50 + "\n\n"
            + "\n\n".join(fmt(s) for s in subs)
            + "\n\n" + "=" * 50
            + "\nTriage: python _submissions.py list   (in the site repo)\n")
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        for fname, blob in attachments:
            img = MIMEImage(blob, name=fname)
            img.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(img)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    # Unique subject per digest (id range + NZ timestamp) so Gmail never
    # threads digests into one conversation (Che 2026-06-13).
    nz_now = datetime.now(NZ).strftime("%d %b %I:%M %p")
    ids_label = f"#{subs[0]['id']}" if n == 1 else f"#{subs[0]['id']}-#{subs[-1]['id']}"
    msg["Subject"] = (f"SurfCheck: {n} new submission{'s' if n != 1 else ''} {ids_label}"
                      + (" incl WEBCAM" if any(s["type"] == "webcam" for s in subs) else "")
                      + f" · {nz_now}")
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
