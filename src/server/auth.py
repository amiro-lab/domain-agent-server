"""API 키 인증 + 관리자 JWT 인증."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
import bcrypt
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from server.db import APIKey, AdminUser, Team, TeamMember, get_session

bearer = HTTPBearer(auto_error=False)
admin_bearer = HTTPBearer(auto_error=True)
member_bearer = HTTPBearer(auto_error=True)

JWT_SECRET = os.getenv("JWT_SECRET", secrets.token_hex(32))
JWT_ALGO = "HS256"
JWT_EXPIRE_HOURS = 24


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_key() -> str:
    return "da_" + secrets.token_urlsafe(32)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def create_admin_token(username: str) -> str:
    payload = {
        "sub": username,
        "role": "admin",
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def get_team(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    session: Session = Depends(get_session),
) -> Team:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증 필요")
    raw_key = credentials.credentials
    key_hash = hash_key(raw_key)
    row = session.query(APIKey).filter_by(key_hash=key_hash).first()
    if not row or not row.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if not row.team.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀이 비활성화되었습니다")

    # last_used_at 갱신
    from server.db import _now
    row.last_used_at = _now()
    session.commit()

    return row.team


def get_api_key(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    session: Session = Depends(get_session),
) -> APIKey:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증 필요")
    raw_key = credentials.credentials
    key_hash = hash_key(raw_key)
    row = session.query(APIKey).filter_by(key_hash=key_hash).first()
    if not row or not row.enabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return row


def get_admin(
    credentials: HTTPAuthorizationCredentials = Security(admin_bearer),
    session: Session = Depends(get_session),
) -> AdminUser:
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
        if payload.get("role") != "admin":
            raise ValueError
        username = payload["sub"]
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="관리자 인증 필요")

    user = session.query(AdminUser).filter_by(username=username).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="관리자를 찾을 수 없습니다")
    return user


def create_member_token(member_id: str, team_id: str, name: str) -> str:
    payload = {
        "sub": member_id,
        "role": "member",
        "team_id": team_id,
        "name": name,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS * 7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


class MemberContext:
    def __init__(self, member: TeamMember, team: Team):
        self.member = member
        self.team = team


def get_member_ctx(
    credentials: HTTPAuthorizationCredentials = Security(member_bearer),
    session: Session = Depends(get_session),
) -> MemberContext:
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
        if payload.get("role") != "member":
            raise ValueError
        member_id = payload["sub"]
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="멤버 인증 필요")

    member = session.query(TeamMember).filter_by(id=member_id, enabled=True).first()
    if not member:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="팀원을 찾을 수 없습니다")
    team = session.query(Team).filter_by(id=member.team_id, enabled=True).first()
    if not team:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="팀이 비활성화되었습니다")

    return MemberContext(member=member, team=team)


def admin_setup_done(session: Session) -> bool:
    return session.query(AdminUser).count() > 0
