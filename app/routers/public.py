"""Public + auth endpoints: login, invites, join requests, public resources, local file delivery."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from datetime import datetime, timezone

from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import (current_user, decode_token, get_db, hash_password, make_token, rate_limit, settings,
                      verify_password)
from app import alerts
from app.models import AccessRequest, Document, DocumentShare, JoinRequest, Organization, User
from app.services import (ACTOR_TYPES, REPO_CATEGORIES, active_shares, audit, catalogue_docs, doc_code, doc_out, notify,
                          shared_mailbox, storage, user_out)
from app.models import DocumentVersion

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"status": "ok", "storage": settings.storage_backend, "email": "smtp" if settings.smtp_host else "log-only",
            "alerts": "smtp" if alerts.smtp_configured() else "log-only"}


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("login:" + (request.client.host if request.client else "?"), 10, 300)
    u = db.query(User).filter(func.lower(User.email) == body.email.lower()).first()
    if not u or not u.is_active or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    if u.role == "participant":
        org = db.get(Organization, u.organization_id)
        if not org or org.status != "active":
            raise HTTPException(403, "This organization's access is suspended. Contact ONCM.")
    audit(db, u, "auth.login", "user", u.id)
    db.commit()
    return {"token": make_token(u.id), "user": user_out(u)}


@router.get("/auth/me")
def me(u: User = Depends(current_user), db: Session = Depends(get_db)):
    out = user_out(u)
    if u.organization_id:
        o = db.get(Organization, u.organization_id)
        out["organization"] = {"id": o.id, "name": o.name, "actor_type": o.actor_type}
    out["two_person_rule"] = settings.two_person_rule
    return out


class InviteIn(BaseModel):
    token: str
    password: str = Field(min_length=10, max_length=128)


@router.post("/auth/accept-invite")
def accept_invite(body: InviteIn, db: Session = Depends(get_db)):
    p = decode_token(body.token, "invite")
    u = db.get(User, int(p["sub"]))
    if not u:
        raise HTTPException(404, "Invitation not found")
    first_time = u.password_hash is None
    u.password_hash, u.is_active = hash_password(body.password), True
    audit(db, u, "auth.invite_accepted", "user", u.id)
    if first_time and u.role == "participant" and u.organization_id:
        org = db.get(Organization, u.organization_id)
        if org:
            alerts.queue_alert(db, "joins", "org_activated", f"{u.name} ({org.name}) activated their account",
                               alerts.render(f"{u.name} accepted the invitation and can now sign in for {org.name}.",
                                             [("Organisation", org.name), ("Actor type", org.actor_type), ("User", f"{u.name} <{u.email}>")]),
                               f"activated:{u.id}")
    db.commit()
    return {"token": make_token(u.id), "user": user_out(u)}


class PwIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=128)


@router.post("/auth/change-password")
def change_password(body: PwIn, u: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(body.current_password, u.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    u.password_hash = hash_password(body.new_password)
    audit(db, u, "auth.password_changed", "user", u.id)
    db.commit()
    return {"ok": True}


class JoinIn(BaseModel):
    org_name: str = Field(min_length=2, max_length=200)
    actor_type: str
    contact_name: str = Field(min_length=2, max_length=200)
    contact_email: EmailStr
    intended_role: Optional[str] = ""
    evidence: Optional[str] = ""


@router.get("/meta")
def meta():
    return {"actor_types": ACTOR_TYPES}


@router.post("/join-requests", status_code=201)
def join(body: JoinIn, request: Request, db: Session = Depends(get_db)):
    rate_limit("join:" + (request.client.host if request.client else "?"), 5, 3600)
    if body.actor_type not in ACTOR_TYPES:
        raise HTTPException(400, "Unknown actor type")
    dup = db.query(Organization).filter(func.lower(Organization.name) == body.org_name.lower()).first() is not None
    jr = JoinRequest(org_name=body.org_name, actor_type=body.actor_type, contact_name=body.contact_name,
                     contact_email=body.contact_email.lower(), intended_role=body.intended_role,
                     evidence=body.evidence, duplicate_flag=dup)
    db.add(jr)
    db.flush()
    notify(db, "join_request_submitted", shared_mailbox(), f"New join request: {jr.org_name}",
           f"{jr.org_name} ({jr.actor_type}) has applied to join. Review it in the Workbench admin area.",
           f"join-submitted:{jr.id}")
    alerts.queue_alert(db, "joins", "join_request_submitted", f"New organisation wants to join: {jr.org_name}",
                       alerts.render(f"{jr.org_name} has asked to join ONCM and is waiting for verification.",
                                     [("Organisation", jr.org_name), ("Actor type", jr.actor_type),
                                      ("Contact", f"{jr.contact_name} <{jr.contact_email}>"), ("Intended role", jr.intended_role),
                                      ("Supporting information", (jr.evidence or "")[:800]),
                                      ("Possible duplicate", "YES - an organisation with this name already exists" if dup else None),
                                      ("Request number", jr.id)],
                                     "Review it under Join requests (Master Admin)."),
                       f"join-submitted:{jr.id}")
    notify(db, "join_request_received", jr.contact_email, "We received your request to join ONCM",
           "Thank you. The ONCM team will verify your request and contact you with the outcome.",
           f"join-received:{jr.id}")
    audit(db, None, "join_request.created", "join_request", jr.id, {"org": jr.org_name})
    db.commit()
    return {"id": jr.id, "message": "Request received. ONCM will contact you by email."}


# ---------- public knowledge repository (no login) ----------
@router.get("/public/resources")
def public_resources(db: Session = Depends(get_db)):
    """Level 5 (Public) documents, plus a metadata-only list of Level 6 (Request-only) documents."""
    pub = [dict(doc_out(db, d, True), access="public") for d, _ in catalogue_docs(db, "public")]
    req = [{"id": d.id, "code": doc_code(d), "title": d.title, "repo_category_name": REPO_CATEGORIES.get(d.repo_category), "access": "request_only"}
           for d, _ in catalogue_docs(db, "request_only")]
    return {"public": pub, "request_only": req}


@router.get("/public/resources/{doc_id}/download")
def public_download(doc_id: int, db: Session = Depends(get_db)):
    pair = next(((d, s) for d, s in catalogue_docs(db, "public") if d.id == doc_id), None)
    if not pair:
        raise HTTPException(404, "Not found")
    d, s = pair
    v = db.query(DocumentVersion).filter_by(document_id=d.id, version=s.version).first()
    return {"url": storage().download_url(v.storage_key, v.filename), "filename": v.filename}


class PublicRequestIn(BaseModel):
    document_id: int
    requester_name: str = Field(min_length=2, max_length=200)
    requester_email: EmailStr
    requester_org: Optional[str] = ""
    purpose: str = Field(min_length=5, max_length=1000)


@router.post("/public/access-requests", status_code=201)
def public_request(body: PublicRequestIn, request: Request, db: Session = Depends(get_db)):
    """Public access on request (the web form equivalent of emailing the shared ONCM mailbox)."""
    rate_limit("req:" + (request.client.host if request.client else "?"), 5, 3600)
    d = db.get(Document, body.document_id)
    if not d or d.status != "shared_externally" or not any(s.audience == "request_only" for s in active_shares(db, d.id)):
        raise HTTPException(404, "This document is not available on request")
    r = AccessRequest(document_id=d.id, requester_name=body.requester_name, requester_email=body.requester_email.lower(),
                      requester_org=body.requester_org, purpose=body.purpose, source="public_form")
    db.add(r)
    db.flush()
    notify(db, "access_requested", shared_mailbox(), f"Access request: {doc_code(d)}",
           f"{r.requester_name} <{r.requester_email}> ({r.requester_org or 'no organization'}) requests “{d.title}”.\nPurpose: {r.purpose}",
           f"access-req:{r.id}")
    notify(db, "access_request_received", r.requester_email, "We received your request",
           "ONCM will review your request and reply by email.", f"access-recv:{r.id}")
    audit(db, None, "access_request.created", "document", d.id, {"email": r.requester_email})
    db.commit()
    return {"message": "Request received. ONCM will reply by email."}


@router.get("/request-access/download")
def request_download(token: str, db: Session = Depends(get_db)):
    """Emailed link for an approved request. Valid until the approval expires; the document must still be request-only."""
    p = decode_token(token, "reqdl")
    r = db.get(AccessRequest, int(p["sub"]))
    exp = r.expires_at.replace(tzinfo=timezone.utc) if r and r.expires_at is not None and r.expires_at.tzinfo is None else (r.expires_at if r else None)
    if not r or r.status != "approved" or (exp and exp < datetime.now(timezone.utc)):
        raise HTTPException(403, "This link is no longer valid")
    pair = next(((d, s) for d, s in catalogue_docs(db, "request_only") if d.id == r.document_id), None)
    if not pair:
        raise HTTPException(404, "This document is no longer available")
    d, s = pair
    v = db.query(DocumentVersion).filter_by(document_id=d.id, version=s.version).first()
    audit(db, None, "request_access.download", "document", d.id, {"request": r.id})
    db.commit()
    return RedirectResponse(storage().download_url(v.storage_key, v.filename))


@router.get("/files/local")
def local_file(token: str):
    """Short-lived (5 min) signed link used only when STORAGE_BACKEND=local."""
    p = decode_token(token, "file")
    if settings.storage_backend != "local":
        raise HTTPException(404, "Not found")
    path = storage().path(p["key"])
    if not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path, filename=p["name"])
