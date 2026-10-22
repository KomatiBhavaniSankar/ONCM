"""Master Admin: manage who receives admin alerts, test the alert sender, and watch the alert log."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import alerts as al
from app.core import get_db, master_user, rate_limit, settings
from app.models import AlertLog, AlertRecipient, User
from app.services import _pool, audit, row

router = APIRouter(prefix="/api/master")


def _cats(cats):
    try:
        return al.clean_categories(cats)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _get(db: Session, rid: int) -> AlertRecipient:
    r = db.get(AlertRecipient, rid)
    if not r:
        raise HTTPException(404, "Recipient not found")
    return r


@router.get("/alert-settings")
def alert_settings(u: User = Depends(master_user), db: Session = Depends(get_db)):
    """Never returns the SMTP password - only whether the sender is ready."""
    return {"sender": al.sender_address() or None, "smtp_host": settings.alert_smtp_host or None,
            "smtp_configured": al.smtp_configured(), "categories": al.CATEGORIES,
            "recipients": db.query(AlertRecipient).count(),
            "failed": db.query(AlertLog).filter_by(status="failed").count()}


@router.get("/alert-recipients")
def list_recipients(u: User = Depends(master_user), db: Session = Depends(get_db)):
    return [row(r) for r in db.query(AlertRecipient).order_by(AlertRecipient.id)]


class RecipientIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=200)
    categories: Optional[list[str]] = None      # None = every category
    is_active: bool = True


@router.post("/alert-recipients", status_code=201)
def add_recipient(body: RecipientIn, u: User = Depends(master_user), db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.query(AlertRecipient).filter(func.lower(AlertRecipient.email) == email).first():
        raise HTTPException(409, "This address already receives alerts")
    r = AlertRecipient(email=email, name=body.name.strip() or email.split("@")[0], categories=_cats(body.categories),
                       is_active=body.is_active, created_by=u.id)
    db.add(r)
    db.flush()
    audit(db, u, "alert_recipient.added", "alert_recipient", r.id, {"email": email, "categories": r.categories})
    db.commit()
    return row(r)


class RecipientPatch(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    categories: Optional[list[str]] = None
    all_categories: bool = False                # true = reset to "every category"
    is_active: Optional[bool] = None


@router.patch("/alert-recipients/{rid}")
def patch_recipient(rid: int, body: RecipientPatch, u: User = Depends(master_user), db: Session = Depends(get_db)):
    r = _get(db, rid)
    changes = {}
    if body.name is not None:
        r.name = body.name.strip() or r.email.split("@")[0]
        changes["name"] = r.name
    if body.all_categories:
        r.categories, changes["categories"] = None, "all"
    elif body.categories is not None:
        r.categories = _cats(body.categories)
        changes["categories"] = r.categories
    if body.is_active is not None:
        r.is_active = body.is_active
        changes["is_active"] = r.is_active
    audit(db, u, "alert_recipient.updated", "alert_recipient", r.id, {"email": r.email, **changes})
    db.commit()
    return row(r)


@router.delete("/alert-recipients/{rid}")
def delete_recipient(rid: int, u: User = Depends(master_user), db: Session = Depends(get_db)):
    r = _get(db, rid)
    audit(db, u, "alert_recipient.removed", "alert_recipient", r.id, {"email": r.email})
    db.delete(r)
    db.commit()
    return {"deleted": True}


@router.post("/alert-recipients/{rid}/test")
def test_recipient(rid: int, request: Request, u: User = Depends(master_user), db: Session = Depends(get_db)):
    """Sends a real test email now and reports the real SMTP result (so a wrong app password is obvious)."""
    rate_limit(f"alert-test:{u.id}", 10, 300)
    r = _get(db, rid)
    ok, detail = al.send_test(db, r.email)
    audit(db, u, "alert_recipient.test", "alert_recipient", r.id, {"email": r.email, "ok": ok})
    db.commit()
    return {"ok": ok, "detail": detail, "smtp_configured": al.smtp_configured()}


@router.get("/alert-log")
def alert_log(status: str = "", category: str = "", limit: int = 200, u: User = Depends(master_user), db: Session = Depends(get_db)):
    q = db.query(AlertLog)
    if status:
        q = q.filter(AlertLog.status == status)
    if category:
        q = q.filter(AlertLog.category == category)
    return [row(a, exclude=("body",)) for a in q.order_by(AlertLog.id.desc()).limit(min(limit, 500)).all()]


@router.post("/alert-log/{aid}/retry")
def retry_alert(aid: int, u: User = Depends(master_user), db: Session = Depends(get_db)):
    a = db.get(AlertLog, aid)
    if not a:
        raise HTTPException(404, "Not found")
    if a.status == "sent":
        raise HTTPException(409, "Already sent")
    if a.status == "logged":
        a.status = "queued"       # SMTP was not configured when it was logged; allow sending now that it may be
        db.commit()
    _pool.submit(al.deliver_alert, aid)
    return {"queued": True}
