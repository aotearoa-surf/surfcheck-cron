# -*- coding: utf-8 -*-
"""Emails Che when a GitHub workflow fails. Used by the `if: failure()` step in
every workflow (both repos). Env: SMTP_PASSWORD (required), WF_NAME, RUN_URL."""
import os, ssl, smtplib
from email.mime.text import MIMEText

wf = os.environ.get("WF_NAME", "unknown workflow")
url = os.environ.get("RUN_URL", "")
host = os.environ.get("SMTP_HOST", "mail.surfcheck.nz")
user = os.environ.get("SMTP_USER", "noreply@surfcheck.nz")
to = os.environ.get("NOTIFY_TO", "surf@aotearoasurf.co.nz")

msg = MIMEText(f"GitHub workflow FAILED: {wf}\n\nRun logs: {url}\n", "plain", "utf-8")
msg["Subject"], msg["From"], msg["To"] = f"⚠ SurfCheck workflow failed: {wf}", user, to
s = smtplib.SMTP_SSL(host, 465, timeout=30, context=ssl.create_default_context())
s.login(user, os.environ["SMTP_PASSWORD"])
s.sendmail(user, [to], msg.as_string())
s.quit()
print("failure email sent for", wf)
