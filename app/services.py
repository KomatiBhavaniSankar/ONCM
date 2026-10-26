"""Storage (local/S3), email queue, audit, workflow rules and shared helpers."""
import hashlib
import re
import smtplib
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import alerts
from app.core import SessionLocal, make_token, settings
from datetime import timedelta
from app.models import (AccessRequest, AuditLog, ChangeLog, ChecklistItem, Document, DocumentGrant, DocumentReview,
                        DocumentShare, DocumentVersion, Notification, Organization, ShareAttachment, Task, User, Workstream)

# =============================================================== constants
CATEGORIES = ["01 Onboarding documents", "02 Agreements", "03 Implementation", "04 Meetings and decisions"]
CATEGORY_SLUG = {CATEGORIES[0]: "onboarding", CATEGORIES[1]: "agreements",
                 CATEGORIES[2]: "implementation", CATEGORIES[3]: "meetings-decisions",
                 "Shared resource": "shared"}
ACTOR_TYPES = ["Project Developer", "VVB", "Obligated Entity", "ACVA", "TSP"]
STAGES = ["joined", "documents_in_progress", "agreement_signed", "implementing", "live"]

CHECKLIST = {
    "_common": ["Organization registration form", "Authorized signatory details", "Data protection acknowledgement",
                "Signed participation agreement"],
    "Project Developer": ["Project description", "Evidence of project ownership"],
    "VVB": ["Accreditation certificate"],
    "ACVA": ["Accreditation certificate"],
    "Obligated Entity": ["Registration certificate"],
    "TSP": ["Technical interface contact", "Service description"],
}

# Knowledge-repository categories (code used in the document code ONCM-<CODE>-<YEAR>-V<version>-<serial>)
REPO_CATEGORIES = {
    "CM": "Carbon Markets", "POL": "Policies/ Rules/ Notifications", "RES": "Research",
    "PPD": "Pilot project documents", "CDP": "Common documents for pilot", "AGR": "Agreements with external stakeholders"}
# Access levels (1-6 in the ONCM access classification)
ACCESS_LEVELS = {
    "internal": "1 Internal - authorised ONCM team",
    "pilot_shared": "2 Pilot shared - all authorised pilot participants",
    "role_specific": "3 Role-specific - selected stakeholder categories",
    "restricted": "4 Restricted - specifically authorised individuals or groups",
    "public": "5 Public - anyone",
    "request_only": "6 Request-only - approved requests only"}
# which levels each category may use (from the Access and Sharing table)
_ALL = list(ACCESS_LEVELS)
ALLOWED_LEVELS = {"CM": _ALL, "POL": _ALL, "RES": _ALL,
                  "PPD": ["internal", "role_specific", "restricted"],
                  "CDP": ["internal", "pilot_shared"],
                  "AGR": ["internal", "restricted"]}
DEFAULT_LEVEL = {"CDP": "pilot_shared", "AGR": "restricted", "PPD": "role_specific"}
FOLDER_TO_CODE = {CATEGORIES[0]: "PPD", CATEGORIES[1]: "AGR", CATEGORIES[2]: "PPD", CATEGORIES[3]: "PPD"}
ADMIN_DEFAULT_REPO = ["CM", "POL", "RES", "PPD", "CDP"]  # AGR needs an explicit grant (authorised personnel only)
# repository stage shown on every document
STAGE_GROUP = {"draft": "draft", "submitted": "internal_review", "under_review": "internal_review",
               "changes_requested": "internal_review", "internally_approved": "approved",
               "shared_externally": "published", "rejected": "closed", "superseded": "archive", "archived": "archive"}

# status -> allowed next statuses (enforced server-side)
TRANSITIONS = {
    "draft": {"submitted", "under_review", "archived"},
    "submitted": {"under_review", "superseded", "archived"},
    "under_review": {"changes_requested", "internally_approved", "rejected", "archived"},
    "changes_requested": {"submitted", "draft", "superseded", "archived"},
    "rejected": {"submitted", "superseded", "archived"},
    "internally_approved": {"shared_externally", "under_review", "superseded", "archived"},
    "shared_externally": {"internally_approved", "superseded", "archived"},
    "superseded": {"archived"},
    "archived": {"draft"},
}


def check_transition(cur: str, new: str):
    if new not in TRANSITIONS.get(cur, set()):
        raise HTTPException(409, f"Invalid status change: {cur} -> {new}")


def utcnow():
    return datetime.now(timezone.utc)


def iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def row(o, exclude=()):
    return {c.name: iso(getattr(o, c.name)) for c in o.__table__.columns if c.name not in exclude}


def user_out(u: User):
    return row(u, exclude=("password_hash",))


# =============================================================== audit
def audit(db: Session, actor: User | None, action: str, resource: str, rid, detail: dict | None = None):
    db.add(AuditLog(actor_id=actor.id if actor else None, actor_email=actor.email if actor else None,
                    action=action, resource=resource, resource_id=str(rid), detail=detail or {}))


# =============================================================== storage
class LocalStorage:
    def __init__(self):
        self.base = Path(settings.local_storage_path).resolve()
        self.base.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        p = (self.base / key).resolve()
        if self.base not in p.parents:
            raise HTTPException(400, "Invalid storage key")
        return p

    def save(self, key: str, data: bytes):
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def delete(self, key: str):
        p = self.path(key)
        if p.exists():
            p.unlink()

    def download_url(self, key: str, filename: str) -> str:
        token = make_token("file", typ="file", minutes=5, key=key, name=filename)
        return f"/api/files/local?token={token}"


class S3Storage:
    def __init__(self):
        import boto3
        from botocore.config import Config
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3", region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
            endpoint_url=settings.s3_endpoint_url or None,
            config=Config(signature_version="s3v4"))

    def save(self, key: str, data: bytes):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def download_url(self, key: str, filename: str) -> str:
        safe = filename.replace('"', "")
        return self.client.generate_presigned_url(
            "get_object", ExpiresIn=300,
            Params={"Bucket": self.bucket, "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{safe}"'})


_storage = None


def storage():
    """Switch with STORAGE_BACKEND=local|s3 - no code changes needed."""
    global _storage
    if _storage is None:
        if settings.storage_backend == "s3":
            if not settings.s3_bucket:
                raise RuntimeError("STORAGE_BACKEND=s3 but S3_BUCKET is empty")
            _storage = S3Storage()
        else:
            _storage = LocalStorage()
    return _storage


# =============================================================== file validation
MAGIC = {"pdf": [b"%PDF"], "docx": [b"PK"], "xlsx": [b"PK"], "doc": [b"\xd0\xcf\x11\xe0"],
         "xls": [b"\xd0\xcf\x11\xe0"], "png": [b"\x89PNG"], "jpg": [b"\xff\xd8"], "jpeg": [b"\xff\xd8"]}


def scan_for_malware(data: bytes) -> bool:
    """Hook for ClamAV / S3 malware scanning. Return False to reject the file."""
    return True


async def read_validated(file: UploadFile):
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", (file.filename or "file").split("/")[-1].split("\\")[-1])[:150]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in settings.allowed_extensions.split(","):
        raise HTTPException(400, f"File type .{ext} is not allowed. Allowed: {settings.allowed_extensions}")
    limit = settings.max_upload_mb * 1024 * 1024
    data = b""
    while chunk := await file.read(1024 * 1024):
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
    if not data:
        raise HTTPException(400, "File is empty")
    if ext in MAGIC and not any(data.startswith(m) for m in MAGIC[ext]):
        raise HTTPException(400, "File content does not match its extension")
    if ext in ("csv", "txt") and b"\x00" in data[:4096]:
        raise HTTPException(400, "File content does not match its extension")
    if not scan_for_malware(data):
        raise HTTPException(400, "File failed the security scan")
    return data, name, hashlib.sha256(data).hexdigest()


def storage_key(doc: Document, version: int, filename: str) -> str:
    if doc.organization_id:
        base = f"organizations/{doc.organization_id}/{CATEGORY_SLUG.get(doc.category, 'other')}"
    else:
        base = f"repository/{(doc.repo_category or 'misc').lower()}"
    return f"{base}/{doc.id}/v{version}_{uuid.uuid4().hex[:8]}_{filename}"


async def add_version(db: Session, doc: Document, file: UploadFile, uploader: User) -> DocumentVersion:
    data, name, checksum = await read_validated(file)
    if doc.organization_id is not None:
        dup = (db.query(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id)
               .filter(Document.organization_id == doc.organization_id, DocumentVersion.checksum == checksum).first())
        if dup:
            raise HTTPException(409, "An identical file was already uploaded for this organization")
    last = db.query(DocumentVersion).filter_by(document_id=doc.id).count()
    v = last + 1
    key = storage_key(doc, v, name)
    storage().save(key, data)
    ver = DocumentVersion(document_id=doc.id, version=v, filename=name, storage_key=key,
                          checksum=checksum, size=len(data), uploader_id=uploader.id)
    db.add(ver)
    doc.current_version = v
    return ver


# =============================================================== email queue
_pool = ThreadPoolExecutor(max_workers=3)


def _build_link(path=""):
    return settings.app_base_url.rstrip("/") + "/" + path


def notify(db: Session, event_name: str, recipient: str | None, subject: str, body: str, key: str):
    """Queue an email. It is only SENT after the surrounding DB transaction commits (see after_commit below).
    Idempotency key prevents duplicate emails. Never put internal notes or attachments in here."""
    if not recipient:
        return
    if db.query(Notification.id).filter_by(idempotency_key=key).first():
        return
    n = Notification(event=event_name, recipient=recipient, subject=subject[:250],
                     body=body + "\n\n-- ONCM Workbench (do not reply with confidential documents)", idempotency_key=key)
    db.add(n)
    db.flush()
    db.info.setdefault("pending_mail", []).append(n.id)
    alerts.mirror_email(db, event_name, recipient, subject, body, key)   # copy for subscribed admins (see app/alerts.py)


@event.listens_for(Session, "after_commit")
def _after_commit(session):
    for nid in session.info.pop("pending_mail", []):
        _pool.submit(deliver, nid)
    for aid in session.info.pop("pending_alerts", []):      # admin alerts (separate sender, see app/alerts.py)
        _pool.submit(alerts.deliver_alert, aid)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session):
    session.info.pop("pending_mail", None)
    session.info.pop("pending_alerts", None)


def deliver(nid: int):
    db = SessionLocal()
    try:
        n = db.get(Notification, nid)
        if not n or n.status in ("sent", "logged"):
            return
        n.attempts = (n.attempts or 0) + 1
        if not settings.smtp_host:
            n.status, n.last_error = "logged", "SMTP not configured - email recorded but not sent"
        else:
            try:
                m = EmailMessage()
                m["From"], m["To"], m["Subject"] = settings.mail_from, n.recipient, n.subject
                m["Reply-To"] = settings.mail_reply_to
                m.set_content(n.body)
                with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as s:
                    if settings.smtp_use_tls:
                        s.starttls()
                    if settings.smtp_user:
                        s.login(settings.smtp_user, settings.smtp_password)
                    s.send_message(m)
                n.status, n.sent_at, n.last_error = "sent", utcnow(), None
            except Exception as e:  # retry later by the background worker (max 3 attempts)
                n.status, n.last_error = "failed", str(e)[:500]
        db.commit()
    finally:
        db.close()


def shared_mailbox():
    return settings.oncm_shared_mailbox


# =============================================================== workflow helpers
def make_checklist(db: Session, org: Organization):
    for t in CHECKLIST["_common"] + CHECKLIST.get(org.actor_type, []):
        db.add(ChecklistItem(organization_id=org.id, title=t))


def invite_link(user: User) -> str:
    return _build_link("?invite=" + make_token(user.id, typ="invite", minutes=60 * 24 * 7))


def org_contact_emails(db: Session, org_id: int):
    org = db.get(Organization, org_id)
    return [org.contact_email] if org and org.contact_email else []


def active_shares(db: Session, doc_id: int):
    now_ = utcnow()
    out = []
    for s in db.query(DocumentShare).filter_by(document_id=doc_id, status="active").all():
        exp = s.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is None or exp > now_:
            out.append(s)
    return out


def _live_shares(db: Session):
    now_ = utcnow()
    out = []
    for sh in db.query(DocumentShare).filter_by(status="active").order_by(DocumentShare.id.desc()).all():
        exp = sh.expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is None or exp > now_:
            out.append(sh)
    return out


def has_grant(db: Session, share: DocumentShare, user: User | None, org: Organization | None, source=None) -> bool:
    now_ = utcnow()
    q = db.query(DocumentGrant).filter(DocumentGrant.share_id == share.id)
    if source:
        q = q.filter(DocumentGrant.source == source)
    for g in q.all():
        exp = g.expires_at.replace(tzinfo=timezone.utc) if g.expires_at is not None and g.expires_at.tzinfo is None else g.expires_at
        if exp is not None and exp < now_:
            continue
        if (org and g.organization_id == org.id) or (user and g.user_id == user.id):
            return True
    return False


def participant_resources(db: Session, user: User, org: Organization):
    """(document, share) pairs this participant may open. Levels: pilot_shared, role_specific, restricted, public,
    and request_only (only after an approved request). Internal documents never appear."""
    res, seen = [], set()
    for sh in _live_shares(db):
        d = db.get(Document, sh.document_id)
        if not d or d.status != "shared_externally" or d.id in seen:
            continue
        a = sh.audience
        ok = (a in ("pilot_shared", "public")
              or (a == "role_specific" and org.actor_type in (sh.actor_types or []))
              or (a == "restricted" and has_grant(db, sh, user, org))
              or (a == "request_only" and has_grant(db, sh, user, None, source="request")))
        if ok:
            res.append((d, sh))
            seen.add(d.id)
    return res


def catalogue_docs(db: Session, level: str):
    """Documents currently published at a level (used for the public catalogue / request-only list)."""
    out = []
    for sh in _live_shares(db):
        d = db.get(Document, sh.document_id)
        if d and d.status == "shared_externally" and sh.audience == level:
            out.append((d, sh))
    return out


def attachment_key(doc: Document, filename: str) -> str:
    base = f"organizations/{doc.organization_id}" if doc.organization_id else f"repository/{(doc.repo_category or 'misc').lower()}"
    return f"{base}/{doc.id}/attachments/{uuid.uuid4().hex[:10]}_{filename}"


def attachment_row(a, with_share=False):
    out = {"id": a.id, "filename": a.filename, "size": a.size, "created_at": iso(a.created_at)}
    if with_share:
        out.update(share_id=a.share_id, published=a.share_id is not None)
    return out


def share_attachments(db: Session, share: DocumentShare):
    return [attachment_row(a) for a in db.query(ShareAttachment).filter_by(share_id=share.id).order_by(ShareAttachment.id)]


def version_row(v: DocumentVersion):
    return {"id": v.id, "version": v.version, "filename": v.filename, "size": v.size,
            "created_at": iso(v.created_at), "uploader_id": v.uploader_id}


# =============================================================== repository: codes, permissions, maker-checker
def doc_code(d: Document):
    """ONCM - Category - Year - Version - Serial number"""
    return f"ONCM-{d.repo_category}-{d.year}-V{d.current_version}-{d.serial:04d}" if d.serial else None


def new_document(db: Session, code: str, **kw) -> Document:
    year = utcnow().year
    for _ in range(5):
        try:
            with db.begin_nested():
                mx = db.query(func.max(Document.serial)).filter_by(repo_category=code, year=year).scalar() or 0
                d = Document(repo_category=code, year=year, serial=mx + 1, **kw)
                db.add(d)
                db.flush()
            return d
        except IntegrityError:
            continue
    raise HTTPException(503, "Could not allocate a document code. Please retry.")


def admin_repo_codes(u: User):
    return list(REPO_CATEGORIES) if u.role == "master_admin" else (u.repo_access if u.repo_access is not None else ADMIN_DEFAULT_REPO)


def can_see_doc(u: User, d: Document, assigned_org_ids=None) -> bool:
    """Internal access 'according to assigned permissions'."""
    if u.role == "master_admin":
        return True
    if u.id in (d.assigned_reviewer_id, d.approver_id, d.created_by):
        return True
    if d.repo_category not in admin_repo_codes(u):
        return False
    if d.organization_id:
        return assigned_org_ids is not None and d.organization_id in assigned_org_ids
    return True


def validate_classification(code: str, level: str, actor_types):
    if level not in ACCESS_LEVELS:
        raise HTTPException(400, "Unknown access level")
    if level not in ALLOWED_LEVELS.get(code, []):
        raise HTTPException(400, f"{REPO_CATEGORIES.get(code, code)} documents cannot use the '{level}' access level")
    if level == "role_specific":
        bad = [a for a in (actor_types or []) if a not in ACTOR_TYPES]
        if not actor_types or bad:
            raise HTTPException(400, "Choose the intended stakeholder categories (PD, VVB, Obligated Entity, ACVA, TSP)")


def latest_maker_id(db: Session, d: Document):
    v = db.query(DocumentVersion).filter_by(document_id=d.id, version=d.current_version).first()
    return v.uploader_id if v else d.created_by


def log_change(db: Session, user: User, title: str, decision: str = "", doc: Document | None = None,
               supersedes: Document | None = None, ws_id: int | None = None):
    db.add(ChangeLog(title=title[:250], decision=decision, document_id=doc.id if doc else None,
                     supersedes_document_id=supersedes.id if supersedes else None,
                     workstream_id=ws_id if ws_id else (doc.workstream_id if doc else None), recorded_by=user.id))


def doc_owners(db: Session, d: Document):
    """Admins to notify about a document: maker, reviewer, approver, workstream owner (no participants)."""
    ids = {d.created_by, d.assigned_reviewer_id, d.approver_id}
    if d.workstream_id:
        ws = db.get(Workstream, d.workstream_id)
        ids.add(ws.owner_id if ws else None)
    return [u for u in db.query(User).filter(User.id.in_([i for i in ids if i]), User.role != "participant", User.is_active == True).all()]  # noqa: E712


def notify_doc_owners(db: Session, d: Document, event_name: str, subject: str, body: str, suffix: str, team=False):
    emails = {u.email for u in doc_owners(db, d)}
    if team:
        emails.add(shared_mailbox())
    for em in emails:
        notify(db, event_name, em, subject, body, f"{event_name}:{d.id}:{suffix}:{em}")


def ensure_review_task(db: Session, d: Document, reviewer_id: int | None, creator: User):
    """Review tasks are tied to the document so reminders and the shared task view follow the workflow."""
    if not reviewer_id:
        return
    open_t = db.query(Task).filter(Task.document_id == d.id, Task.status != "done", Task.title.like("Review:%")).first()
    due = d.due_date or (utcnow() + timedelta(days=5))
    if open_t:
        open_t.owner_id, open_t.due_date = reviewer_id, due
    else:
        db.add(Task(title=f"Review: {d.title}", description=f"Document {doc_code(d)}", document_id=d.id, workstream_id=d.workstream_id,
                    organization_id=d.organization_id, owner_id=reviewer_id, due_date=due, priority="medium", created_by=creator.id))


def close_review_tasks(db: Session, d: Document):
    for t in db.query(Task).filter(Task.document_id == d.id, Task.status != "done", Task.title.like("Review:%")).all():
        t.status, t.completed_at = "done", utcnow()


def doc_out(db: Session, d: Document, for_participant=False):
    org = db.get(Organization, d.organization_id) if d.organization_id else None
    out = {"id": d.id, "code": doc_code(d), "title": d.title, "category": d.category, "repo_category": d.repo_category,
           "repo_category_name": REPO_CATEGORIES.get(d.repo_category), "doc_type": d.doc_type, "status": d.status,
           "stage": STAGE_GROUP.get(d.status), "current_version": d.current_version, "organization_id": d.organization_id,
           "organization_name": org.name if org else "ONCM repository", "origin": d.origin,
           "due_date": iso(d.due_date), "updated_at": iso(d.updated_at), "created_at": iso(d.created_at)}
    if for_participant:
        last = (db.query(DocumentReview).filter(DocumentReview.document_id == d.id, DocumentReview.feedback != "")
                .filter(DocumentReview.decision.in_(["changes", "reject"])).order_by(DocumentReview.id.desc()).first())
        out["feedback"] = last.feedback if last and d.status in ("changes_requested", "rejected") else None
    else:
        rev = db.get(User, d.assigned_reviewer_id) if d.assigned_reviewer_id else None
        apr = db.get(User, d.approver_id) if d.approver_id else None
        mk = db.get(User, latest_maker_id(db, d)) if d.created_by else None
        ws = db.get(Workstream, d.workstream_id) if d.workstream_id else None
        out.update(reviewer_name=rev.name if rev else None, assigned_reviewer_id=d.assigned_reviewer_id,
                   approver_id=d.approver_id, approver_name=apr.name if apr else None,
                   maker_name=mk.name if mk else None, access_level=d.access_level,
                   target_actor_types=d.target_actor_types or [], workstream_id=d.workstream_id,
                   workstream_name=ws.name if ws else None, superseded_by_id=d.superseded_by_id,
                   classification_confirmed=bool(d.classification_confirmed_at), share_count=len(active_shares(db, d.id)))
    return out
