"""Admin alerts: a SEPARATE SMTP sender (for example a Gmail account) that emails a managed list of admin mailboxes
about what is happening in the Workbench: which organisation wants to join, join decisions, onboarding progress,
document activity, access requests, participant questions and reminders.

How it fits the existing code
-----------------------------
* `queue_alert()` writes one `alert_log` row per subscribed recipient. Like normal emails, nothing is SENT until the
  surrounding database transaction commits (see the after_commit hook in services.py), so a rolled-back action never
  produces an alert.
* `mirror_email()` is called from `services.notify()`: every other email the Workbench sends is also copied to the
  recipients who subscribed to that category ("all the other emails").
* Delivery uses the ALERT_SMTP_* settings, independent of the normal SMTP_* settings. If ALERT_SMTP_* is empty the alert is
  recorded as `logged` (same behaviour as the normal email queue).
* Recipients are managed at runtime (Admin alerts page / /api/master/alert-recipients). Add as many as you like.

Security rules baked in
-----------------------
* Secure links (invitation / download tokens) are NEVER copied to a third mailbox: some events are not mirrored at all and
  every mirrored body is scrubbed of token links.
* The SMTP password only lives in `.env`. It is never stored in the database, returned by the API or written to logs.
"""
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from sqlalchemy.orm import Session

from app.core import SessionLocal, settings
from app.models import AlertLog, AlertRecipient, now

log = logging.getLogger("oncm.alerts")

# ----------------------------------------------------------------------------- categories
CATEGORIES = {
    "joins": "Join requests and new organisations",
    "organisations": "Organisation progress, status and members",
    "documents": "Documents, reviews and publication",
    "access_requests": "Access requests",
    "support": "Questions from participants",
    "reminders": "Deadline and review reminders",
    "other": "Everything else",
}

EVENT_CATEGORY = {
    # joins
    "join_request_submitted": "joins", "join_request_received": "joins", "join_approved": "joins",
    "join_reject": "joins", "join_more_info": "joins", "org_activated": "joins",
    # organisations
    "org_live": "organisations", "org_live_internal": "organisations", "org_stage_changed": "organisations",
    "org_status_changed": "organisations", "org_assigned": "organisations", "member_invited": "organisations",
    "member_added": "organisations",
    # documents
    "document_uploaded": "documents", "document_in_review": "documents", "document_recommend": "documents",
    "document_approve": "documents", "document_changes": "documents", "document_reject": "documents",
    "changes_requested": "documents", "document_rejected": "documents", "document_shared": "documents",
    "document_published": "documents", "document_superseded": "documents", "submission_confirmed": "documents",
    # access requests
    "access_requested": "access_requests", "access_request_received": "access_requests",
    "access_approved": "access_requests", "access_rejected": "access_requests",
    "access_decision": "access_requests",
    # support
    "participant_question": "support",
    # reminders
    "deadline_reminder": "reminders", "review_reminder": "reminders", "missing_documents": "reminders",
}

# Emails that are NOT copied by mirror_email():
#  - they carry a password-setup / download link (must never reach a third mailbox), or
#  - they are acknowledgements to the applicant, or one email per recipient in a bulk send, and a single richer alert
#    is raised explicitly instead (join submitted/decided, organisation live, publication, access decisions).
SKIP_MIRROR = {
    "member_invited", "join_approved", "access_approved",                      # contain secure links
    "join_request_received", "submission_confirmed", "access_request_received",  # applicant acknowledgements
    "document_shared",                                                          # one per organisation on publish
    "join_request_submitted", "join_reject", "join_more_info",                  # replaced by rich join alerts
    "org_live", "org_live_internal",                                            # replaced by the stage-change alert
    "access_rejected",                                                          # replaced by the access-decision alert
}

_SECRET_LINK = re.compile(r"https?://\S*(?:token=|invite=)\S*", re.I)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def category_of(event: str) -> str:
    return EVENT_CATEGORY.get(event, "other")


def redact(text: str) -> str:
    return _SECRET_LINK.sub("[secure link removed]", text or "")


def render(headline: str, rows: list[tuple[str, object]] | None = None, note: str = "") -> str:
    """Plain-text body: a headline, 'Label: value' lines, and a link to the Workbench."""
    lines = [headline, ""]
    for k, v in rows or []:
        if v not in (None, ""):
            lines.append(f"{k}: {v}")
    if note:
        lines += ["", note]
    lines += ["", f"Open the Workbench: {settings.app_base_url.rstrip('/')}/"]
    return "\n".join(lines)


# ----------------------------------------------------------------------------- queueing
def queue_alert(db: Session, category: str, event: str, subject: str, body: str, key: str, exclude: str | None = None):
    """Queue one alert per active recipient subscribed to `category`. Sent after the DB commit.
    `key` makes it idempotent (the same event never alerts the same recipient twice). `exclude` skips one address
    (used so a recipient is not alerted about an email that was already addressed to them)."""
    subject = " ".join(f"[ONCM] {subject}".split())[:200]      # one line: no header injection from user-supplied names
    for r in db.query(AlertRecipient).filter_by(is_active=True).all():
        if r.categories is not None and category not in r.categories:
            continue
        if exclude and r.email.lower() == exclude.lower():
            continue
        k = f"alert:{key}:{r.email}"[:250]
        if db.query(AlertLog.id).filter_by(idempotency_key=k).first():
            continue
        a = AlertLog(recipient_email=r.email, category=category, event=event, subject=subject, body=body, idempotency_key=k)
        db.add(a)
        db.flush()
        db.info.setdefault("pending_alerts", []).append(a.id)


def mirror_email(db: Session, event: str, to: str, subject: str, body: str, key: str):
    """Copy of a normal system email for the admins who subscribed to its category."""
    if event in SKIP_MIRROR or event.startswith("alert"):
        return
    text = (f"A copy of an email the Workbench sent.\n\nTo: {to}\nEvent: {event}\nSubject: {subject}\n"
            f"{'-' * 40}\n{redact(body)}\n\nOpen the Workbench: {settings.app_base_url.rstrip('/')}/")
    queue_alert(db, category_of(event), event, subject, text, f"mirror:{key}", exclude=to)


# ----------------------------------------------------------------------------- delivery
def smtp_configured() -> bool:
    return bool(settings.alert_smtp_host and settings.alert_smtp_user and settings.alert_smtp_password)


def sender_address() -> str:
    return settings.alert_mail_from or settings.alert_smtp_user


def friendly_error(e: Exception) -> str:
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the login. For Gmail use a 16-character App Password "
                "(Google Account > Security > 2-Step Verification > App passwords), not the normal password.")
    return f"{type(e).__name__}: {e}"[:500]


def _send(to: str, subject: str, body: str):
    m = EmailMessage()
    m["From"] = formataddr((settings.alert_from_name, sender_address()))
    m["To"], m["Subject"] = to, subject
    m.set_content(body)
    host, port = settings.alert_smtp_host, settings.alert_smtp_port
    ssl = port == 465
    with (smtplib.SMTP_SSL(host, port, timeout=20) if ssl else smtplib.SMTP(host, port, timeout=20)) as s:
        if not ssl and settings.alert_smtp_use_tls:
            s.starttls()
        s.login(settings.alert_smtp_user, settings.alert_smtp_password.replace(" ", ""))  # Google shows app passwords with spaces
        s.send_message(m)


def _attempt(a: AlertLog):
    a.attempts = (a.attempts or 0) + 1
    if not smtp_configured():
        a.status, a.last_error = "logged", "Alert SMTP not configured - recorded but not sent"
        return
    try:
        _send(a.recipient_email, a.subject, a.body)
        a.status, a.sent_at, a.last_error = "sent", now(), None
    except Exception as e:  # retried by the background worker (max 3 attempts)
        a.status, a.last_error = "failed", friendly_error(e)
        log.warning("alert %s to %s failed: %s", a.id, a.recipient_email, a.last_error)


def deliver_alert(aid: int):
    db = SessionLocal()
    try:
        a = db.get(AlertLog, aid)
        if not a or a.status in ("sent", "logged"):
            return
        _attempt(a)
        db.commit()
    finally:
        db.close()


def send_test(db: Session, email: str) -> tuple[bool, str]:
    """Synchronous test email so the Master Admin sees the real SMTP result immediately."""
    a = AlertLog(recipient_email=email, category="other", event="alert_test", subject="[ONCM] Test alert",
                 body=render("This is a test alert from the ONCM Workbench.",
                             [("Sender", sender_address() or "(not configured)"), ("Sent to", email)],
                             "If you can read this, alert delivery works for this address."),
                 idempotency_key=f"alert:test:{email}:{now().timestamp()}")
    db.add(a)
    db.flush()
    _attempt(a)
    db.commit()
    ok = a.status == "sent"
    return ok, "Test email sent." if ok else (a.last_error or "Not sent")


# ----------------------------------------------------------------------------- recipients
def clean_categories(cats) -> list[str] | None:
    """None/absent = all categories. Unknown names are rejected by the caller (see ValueError)."""
    if cats is None:
        return None
    bad = [c for c in cats if c not in CATEGORIES]
    if bad:
        raise ValueError(f"Unknown alert categories: {', '.join(bad)}")
    if not cats:
        raise ValueError("Choose at least one category")
    return list(dict.fromkeys(cats))


def seed_recipients():
    """First start only: load ALERT_RECIPIENTS from .env. After that the list is managed in the UI, so a restart never
    re-adds a recipient that was removed on purpose."""
    if not settings.alert_recipients.strip():
        return
    db = SessionLocal()
    try:
        if db.query(AlertRecipient.id).first():
            return
        for e in {x.strip().lower() for x in settings.alert_recipients.replace(";", ",").split(",") if x.strip()}:
            if _EMAIL.match(e):
                db.add(AlertRecipient(email=e, name=e.split("@")[0], categories=None))
            else:
                log.warning("ALERT_RECIPIENTS: ignoring invalid address %r", e)
        db.commit()
        log.warning("Seeded %d alert recipient(s) from ALERT_RECIPIENTS", db.query(AlertRecipient).count())
    finally:
        db.close()
