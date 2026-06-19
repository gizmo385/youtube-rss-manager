from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User

_LOCAL_SUB = "local-user"


def _get_or_create_local_user(db: Session) -> User:
    """Return a fixed local user, creating it on first use (local mode only)."""
    user = db.execute(
        select(User).where(User.oidc_sub == _LOCAL_SUB)
    ).scalar_one_or_none()
    if user is None:
        user = User(
            oidc_sub=_LOCAL_SUB,
            email=get_settings().local_user_email,
            display_name="Local User",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    if get_settings().local_mode:
        return _get_or_create_local_user(db)
    user_id = request.session.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, user_id)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="User not found")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    if get_settings().local_mode:
        return _get_or_create_local_user(db)
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    return db.get(User, user_id)
