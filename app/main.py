import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from sqlalchemy import inspect, text
from fastapi.staticfiles import StaticFiles

from app.core import SessionLocal, engine, hash_password, settings
from app.models import AccessRequest, AlertLog, Base, Document, Notification, Task, User
from app import alerts
from app.routers import admin, master, participant, public
from app.routers import alerts as alerts_router
from app.services import _pool, deliver, notify, shared_mailbox, storage

log = logging.getLogger("oncm")


def add_missing_columns():
    """Tiny forward-only migration: adds any new model column that an existing database table does not have yet
    (so upgrading the app never needs a manual ALTER TABLE). Safe to run on every start."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}'))
                log.warning("Added column %s.%s", table.name, col.name)


def seed_master():
    db = SessionLocal()
    try:
        if not db.query(User).filter_by(role="master_admin").first():
            db.add(User(email=settings.master_admin_email.lower(), name=settings.master_admin_name, role="master_admin",
                        password_hash=hash_password(settings.master_admin_password), can_share=True, can_export=True))
            db.commit()
            log.warning("Created Master Admin %s - change the password after first login", settings.master_admin_email)
    finally:
        db.close()


def worker_tick():
    """Deadline reminders (REMINDER_DAYS before due) + retry of failed emails (max 3 attempts)."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        days = [int(x) for x in settings.reminder_days.split(",") if x.strip().isdigit()]
        for t in db.query(Task).filter(Task.status != "done", Task.due_date != None).all():  # noqa: E711
            due = t.due_date if t.due_date.tzinfo else t.due_date.replace(tzinfo=timezone.utc)
            left = (due.date() - now.date()).days
            if left in days:
                owner = db.get(User, t.owner_id) if t.owner_id else None
                notify(db, "deadline_reminder", owner.email if owner else shared_mailbox(),
                       f"Deadline reminder: {t.title}", f"“{t.title}” is due in {left} day(s).",
                       f"remind:{t.id}:{left}:{now:%Y%m%d}")
                if t.organization_id:
                    from app.models import Organization
                    org = db.get(Organization, t.organization_id)
                    if org:
                        notify(db, "deadline_reminder", org.contact_email, f"Deadline reminder: {t.title}",
                               f"“{t.title}” is due in {left} day(s).", f"remind-org:{t.id}:{left}:{now:%Y%m%d}")
        db.commit()
        review_reminders(db)
        for n in db.query(Notification).filter(Notification.status == "failed", Notification.attempts < 3).all():
            _pool.submit(deliver, n.id)
        for a in db.query(AlertLog).filter(AlertLog.status == "failed", AlertLog.attempts < 3).all():
            _pool.submit(alerts.deliver_alert, a.id)
    finally:
        db.close()


def review_reminders(db):
    """Reminder when a document sits under review longer than REVIEW_REMINDER_DAYS (one email per day)."""
    now = datetime.now(timezone.utc)
    for d in db.query(Document).filter_by(status="under_review").all():
        upd = d.updated_at if d.updated_at.tzinfo else d.updated_at.replace(tzinfo=timezone.utc)
        rev = db.get(User, d.assigned_reviewer_id) if d.assigned_reviewer_id else None
        if rev and (now - upd).days >= settings.review_reminder_days:
            notify(db, "review_reminder", rev.email, f"Review pending: {d.title}",
                   "This document is still waiting for your review. Open the approval inbox in the Workbench.",
                   f"review-remind:{d.id}:{now:%Y%m%d}")
    db.commit()


def poll_mailbox():
    """Mail-in channel: an email to the shared mailbox that mentions a document code (ONCM-RES-2026-V1-0012)
    becomes a pending access request. Works with any IMAP mailbox; disabled when IMAP_HOST is empty."""
    if not settings.imap_host:
        return
    import email
    import imaplib
    import re
    from email.header import decode_header, make_header
    from email.utils import parseaddr
    from fastapi import HTTPException
    from app.routers.admin import find_request_only_doc
    M = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    db = SessionLocal()
    try:
        M.login(settings.imap_user, settings.imap_password)
        M.select(settings.imap_folder)
        _, data = M.search(None, "UNSEEN")
        for num in data[0].split():
            _, msg = M.fetch(num, "(RFC822)")
            m = email.message_from_bytes(msg[0][1])
            subject = str(make_header(decode_header(m.get("Subject", ""))))
            body = ""
            for part in m.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_payload(decode=True).decode(errors="ignore")[:1500]
            hit = re.search(r"ONCM-[A-Z]{2,3}-\d{4}-V\d+-\d+", (subject + " " + body).upper())
            name, addr = parseaddr(m.get("From", ""))
            if not hit or not addr:
                continue
            try:
                d = find_request_only_doc(db, hit.group(0))
            except HTTPException:
                continue
            if db.query(AccessRequest).filter_by(document_id=d.id, requester_email=addr.lower(), status="pending").first():
                continue
            db.add(AccessRequest(document_id=d.id, requester_name=name or addr, requester_email=addr.lower(),
                                 purpose=(body.strip() or subject)[:1000], source="email"))
            db.commit()
    finally:
        db.close()
        try:
            M.logout()
        except Exception:
            pass


def loop():
    while True:
        for fn in (worker_tick, poll_mailbox):
            try:
                fn()
            except Exception:  # never kill the worker thread
                log.exception("worker error in %s", fn.__name__)
        time.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    add_missing_columns()
    storage()  # fail fast on bad storage config
    seed_master()
    alerts.seed_recipients()
    threading.Thread(target=loop, daemon=True).start()
    yield


app = FastAPI(title=settings.app_name, version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                         "Referrer-Policy": "same-origin", "Cache-Control": "no-store" if request.url.path.startswith("/api") else "no-cache"})
    return resp


for r in (public.router, participant.router, admin.router, master.router, alerts_router.router):
    app.include_router(r)

app.mount("/", StaticFiles(directory=Path(__file__).resolve().parent.parent / "static", html=True), name="static")
