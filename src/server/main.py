"""domain-agent SaaS 백엔드."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server import analyzer, compressor, memory_store, reporter
from server.auth import (
    admin_setup_done, create_admin_token, create_member_token, generate_key,
    get_admin, get_api_key, get_member_ctx, get_team, hash_key, hash_password, verify_password,
    MemberContext,
)
from server.db import (
    AdminUser, APIKey, AuditLog, ProviderSettings, Report, Team,
    TeamMember, TokenUsage, WeeklySchedule, create_tables, engine, get_session,
)

DASHBOARD_HTML = Path(__file__).parent / "templates" / "dashboard.html"
ADMIN_HTML = Path(__file__).parent / "templates" / "admin.html"
MEMBER_HTML = Path(__file__).parent / "templates" / "member.html"

app = FastAPI(title="domain-agent server", version="0.2.0")
scheduler = BackgroundScheduler(timezone="Asia/Seoul")


# ── 시작/종료 ─────────────────────────────────────────────

@app.on_event("startup")
def startup():
    create_tables()
    _load_provider_settings()
    _reload_schedules()
    scheduler.start()


@app.on_event("shutdown")
def shutdown():
    scheduler.shutdown(wait=False)


def _load_provider_settings():
    """DB에 저장된 프로바이더 설정을 환경변수로 적용."""
    with Session(engine) as session:
        s = session.query(ProviderSettings).filter_by(id="default").first()
        if s:
            os.environ["LLM_PROVIDER"] = s.provider
            if s.openai_api_key:
                os.environ["OPENAI_API_KEY"] = s.openai_api_key
            if s.openai_model:
                os.environ["OPENAI_MODEL"] = s.openai_model
            if s.anthropic_api_key:
                os.environ["ANTHROPIC_API_KEY"] = s.anthropic_api_key
            if s.anthropic_model:
                os.environ["ANTHROPIC_MODEL"] = s.anthropic_model


# ── 감사 로그 미들웨어 ────────────────────────────────────

@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    response = await call_next(request)

    path = request.url.path
    skip_paths = {"/health", "/dashboard", "/admin", "/admin/setup/status"}
    if path in skip_paths or path.startswith("/static"):
        return response

    method = request.method
    status_code = response.status_code
    ip = request.client.host if request.client else ""

    auth = request.headers.get("Authorization", "")
    team_id, team_name, member_name = "", "", ""

    if auth.startswith("Bearer da_"):
        raw_key = auth.split(" ", 1)[1]
        key_hash = hash_key(raw_key)
        try:
            with Session(engine) as session:
                row = session.query(APIKey).filter_by(key_hash=key_hash).first()
                if row and row.team:
                    team_id = row.team.id
                    team_name = row.team.name
                    member_name = row.label
        except Exception:
            pass

    try:
        with Session(engine) as session:
            log = AuditLog(
                team_id=team_id, team_name=team_name, member_name=member_name,
                method=method, endpoint=path,
                status_code=status_code, ip_address=ip,
            )
            session.add(log)
            session.commit()
    except Exception:
        pass

    return response


# ── 스케줄러 ─────────────────────────────────────────────

def _reload_schedules():
    scheduler.remove_all_jobs()
    with Session(engine) as session:
        schedules = session.query(WeeklySchedule).filter_by(enabled=True).all()
        for s in schedules:
            _register_job(s.team_id, s.day_of_week, s.hour, s.minute)


def _register_job(team_id: str, dow: int, hour: int, minute: int):
    job_id = f"weekly_{team_id}"
    scheduler.add_job(
        _run_weekly_for_team,
        CronTrigger(day_of_week=dow, hour=hour, minute=minute),
        args=[team_id],
        id=job_id,
        replace_existing=True,
    )


def _run_weekly_for_team(team_id: str):
    with Session(engine) as session:
        try:
            result = reporter.generate_weekly(session, team_id)
            print(f"[weekly] team={team_id} {result['period']} 생성 완료 ({result['new_count']}개 신규)")
        except Exception as e:
            print(f"[weekly] team={team_id} 실패: {e}")


# ── 요청 모델 ─────────────────────────────────────────────

class CaptureRequest(BaseModel):
    platform: str = "unknown"
    source: str = "chat"
    member: Optional[str] = None
    messages: Optional[list[dict]] = None
    transcript: Optional[str] = None


class MemoryRequest(BaseModel):
    type: str
    description: str
    content: str
    confidence: float = 0.7
    tags: list[str] = []


class ScheduleRequest(BaseModel):
    day_of_week: int
    hour: int
    minute: int = 0
    enabled: bool = True


# ── 관리자 초기 설정 ──────────────────────────────────────

class SetupRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class ProviderRequest(BaseModel):
    provider: str  # "openai" | "anthropic"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"


class TeamUpdateRequest(BaseModel):
    name: Optional[str] = None
    memory_limit: Optional[int] = None
    enabled: Optional[bool] = None
    regenerate_join_code: bool = False


class MemberUpdateRequest(BaseModel):
    enabled: Optional[bool] = None


# ── 관리자 페이지 ─────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse(ADMIN_HTML.read_text(encoding="utf-8"))


@app.get("/admin/setup/status")
def setup_status(session: Session = Depends(get_session)):
    return {"setup_done": admin_setup_done(session)}


@app.post("/admin/setup")
def admin_setup(req: SetupRequest, session: Session = Depends(get_session)):
    if admin_setup_done(session):
        raise HTTPException(400, "이미 관리자가 설정되었습니다")
    if len(req.password) < 8:
        raise HTTPException(400, "비밀번호는 8자 이상이어야 합니다")
    user = AdminUser(username=req.username, password_hash=hash_password(req.password))
    session.add(user)
    session.commit()
    return {"ok": True, "token": create_admin_token(req.username)}


@app.post("/admin/login")
def admin_login(req: LoginRequest, session: Session = Depends(get_session)):
    user = session.query(AdminUser).filter_by(username=req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "아이디 또는 비밀번호가 올바르지 않습니다")
    return {"token": create_admin_token(req.username)}


# ── 관리자 API: 프로바이더 설정 ───────────────────────────

@app.get("/admin/provider")
def get_provider(
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    s = session.query(ProviderSettings).filter_by(id="default").first()
    if not s:
        return {
            "provider": os.getenv("LLM_PROVIDER", "openai"),
            "openai_model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "anthropic_model": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "openai_key_set": bool(os.getenv("OPENAI_API_KEY")),
            "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        }
    return {
        "provider": s.provider,
        "openai_model": s.openai_model,
        "anthropic_model": s.anthropic_model,
        "openai_key_set": bool(s.openai_api_key),
        "anthropic_key_set": bool(s.anthropic_api_key),
        "updated_at": s.updated_at.isoformat(),
    }


@app.put("/admin/provider")
def update_provider(
    req: ProviderRequest,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    s = session.query(ProviderSettings).filter_by(id="default").first()
    if not s:
        s = ProviderSettings(id="default")
        session.add(s)

    s.provider = req.provider
    if req.openai_api_key:
        s.openai_api_key = req.openai_api_key
    s.openai_model = req.openai_model
    if req.anthropic_api_key:
        s.anthropic_api_key = req.anthropic_api_key
    s.anthropic_model = req.anthropic_model
    session.commit()

    # 즉시 환경변수 반영
    os.environ["LLM_PROVIDER"] = req.provider
    if req.openai_api_key:
        os.environ["OPENAI_API_KEY"] = req.openai_api_key
    os.environ["OPENAI_MODEL"] = req.openai_model
    if req.anthropic_api_key:
        os.environ["ANTHROPIC_API_KEY"] = req.anthropic_api_key
    os.environ["ANTHROPIC_MODEL"] = req.anthropic_model

    return {"ok": True, "provider": req.provider}


# ── 관리자 API: 팀 관리 ───────────────────────────────────

@app.get("/admin/teams")
def admin_list_teams(
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    teams = session.query(Team).all()
    result = []
    for t in teams:
        member_count = session.query(TeamMember).filter_by(team_id=t.id).count()
        memory_count = session.query(Report).filter_by(team_id=t.id).count()
        result.append({
            "id": t.id,
            "name": t.name,
            "join_code": t.join_code,
            "memory_limit": t.memory_limit,
            "enabled": t.enabled,
            "member_count": member_count,
            "created_at": t.created_at.isoformat(),
        })
    return {"teams": result}


@app.put("/admin/team/{team_id}")
def admin_update_team(
    team_id: str,
    req: TeamUpdateRequest,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    team = session.query(Team).filter_by(id=team_id).first()
    if not team:
        raise HTTPException(404, "팀을 찾을 수 없습니다")
    if req.name is not None:
        team.name = req.name
    if req.memory_limit is not None:
        team.memory_limit = req.memory_limit
    if req.enabled is not None:
        team.enabled = req.enabled
    if req.regenerate_join_code:
        team.join_code = secrets.token_urlsafe(6)
    session.commit()
    return {"ok": True, "join_code": team.join_code}


@app.delete("/admin/team/{team_id}")
def admin_delete_team(
    team_id: str,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    team = session.query(Team).filter_by(id=team_id).first()
    if not team:
        raise HTTPException(404, "팀을 찾을 수 없습니다")
    team.enabled = False
    session.commit()
    return {"ok": True}


# ── 관리자 API: 팀원 관리 ─────────────────────────────────

@app.get("/admin/team/{team_id}/members")
def admin_list_members(
    team_id: str,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    members = session.query(TeamMember).filter_by(team_id=team_id).all()
    result = []
    for m in members:
        api_key = session.query(APIKey).filter_by(id=m.api_key_id).first() if m.api_key_id else None
        result.append({
            "id": m.id,
            "name": m.name,
            "email": m.email,
            "enabled": m.enabled,
            "last_active": m.last_active.isoformat() if m.last_active else None,
            "api_key_enabled": api_key.enabled if api_key else False,
            "api_key_last_used": api_key.last_used_at.isoformat() if api_key and api_key.last_used_at else None,
            "created_at": m.created_at.isoformat(),
        })
    return {"members": result}


@app.put("/admin/member/{member_id}")
def admin_update_member(
    member_id: str,
    req: MemberUpdateRequest,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    member = session.query(TeamMember).filter_by(id=member_id).first()
    if not member:
        raise HTTPException(404, "팀원을 찾을 수 없습니다")
    if req.enabled is not None:
        member.enabled = req.enabled
        if member.api_key_id:
            api_key = session.query(APIKey).filter_by(id=member.api_key_id).first()
            if api_key:
                api_key.enabled = req.enabled
    session.commit()
    return {"ok": True}


@app.delete("/admin/member/{member_id}")
def admin_delete_member(
    member_id: str,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    member = session.query(TeamMember).filter_by(id=member_id).first()
    if not member:
        raise HTTPException(404, "팀원을 찾을 수 없습니다")
    member.enabled = False
    if member.api_key_id:
        api_key = session.query(APIKey).filter_by(id=member.api_key_id).first()
        if api_key:
            api_key.enabled = False
    session.commit()
    return {"ok": True}


# ── 관리자 API: API 키 관리 ───────────────────────────────

@app.get("/admin/apikeys")
def admin_list_apikeys(
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    keys = session.query(APIKey).all()
    return {"api_keys": [
        {
            "id": k.id,
            "team_id": k.team_id,
            "team_name": k.team.name if k.team else "",
            "label": k.label,
            "enabled": k.enabled,
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "created_at": k.created_at.isoformat(),
        }
        for k in keys
    ]}


@app.delete("/admin/apikey/{key_id}")
def admin_revoke_apikey(
    key_id: str,
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    key = session.query(APIKey).filter_by(id=key_id).first()
    if not key:
        raise HTTPException(404, "API 키를 찾을 수 없습니다")
    key.enabled = False
    session.commit()
    return {"ok": True}


# ── 관리자 API: 통계 ──────────────────────────────────────

@app.get("/admin/stats")
def admin_stats(
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    from server.db import Memory
    total_teams = session.query(Team).filter_by(enabled=True).count()
    total_members = session.query(TeamMember).filter_by(enabled=True).count()
    total_memories = session.query(Memory).count()
    total_reports = session.query(Report).count()

    # 토큰 사용량 합계
    from sqlalchemy import func
    token_sum = session.query(
        func.sum(TokenUsage.prompt_tokens + TokenUsage.completion_tokens)
    ).scalar() or 0

    return {
        "total_teams": total_teams,
        "total_members": total_members,
        "total_memories": total_memories,
        "total_reports": total_reports,
        "total_tokens": token_sum,
    }


# ── 관리자 API: 토큰 사용량 (V2) ─────────────────────────

@app.get("/admin/tokens")
def admin_tokens(
    days: int = Query(default=30, le=90),
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # 팀별 합계
    by_team = session.query(
        TokenUsage.team_name,
        func.sum(TokenUsage.prompt_tokens).label("prompt"),
        func.sum(TokenUsage.completion_tokens).label("completion"),
        func.count().label("calls"),
    ).filter(TokenUsage.created_at >= since).group_by(TokenUsage.team_name).all()

    # 오퍼레이션별 합계
    by_op = session.query(
        TokenUsage.operation,
        func.sum(TokenUsage.prompt_tokens + TokenUsage.completion_tokens).label("total"),
        func.count().label("calls"),
    ).filter(TokenUsage.created_at >= since).group_by(TokenUsage.operation).all()

    # 일별 추이 (최근 30일)
    daily = session.query(
        func.strftime("%Y-%m-%d", TokenUsage.created_at).label("day"),
        func.sum(TokenUsage.prompt_tokens + TokenUsage.completion_tokens).label("total"),
    ).filter(TokenUsage.created_at >= since).group_by("day").order_by("day").all()

    # 프로바이더별
    by_provider = session.query(
        TokenUsage.provider,
        func.sum(TokenUsage.prompt_tokens + TokenUsage.completion_tokens).label("total"),
    ).filter(TokenUsage.created_at >= since).group_by(TokenUsage.provider).all()

    return {
        "period_days": days,
        "by_team": [{"team": r.team_name, "prompt": r.prompt, "completion": r.completion, "calls": r.calls} for r in by_team],
        "by_operation": [{"operation": r.operation, "total": r.total, "calls": r.calls} for r in by_op],
        "by_provider": [{"provider": r.provider, "total": r.total} for r in by_provider],
        "daily": [{"day": r.day, "total": r.total} for r in daily],
    }


# ── 관리자 API: 감사 로그 (V2) ───────────────────────────

@app.get("/admin/audit")
def admin_audit(
    limit: int = Query(default=100, le=500),
    team_id: Optional[str] = Query(default=None),
    admin=Depends(get_admin),
    session: Session = Depends(get_session),
):
    q = session.query(AuditLog)
    if team_id:
        q = q.filter_by(team_id=team_id)
    logs = q.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return {"logs": [
        {
            "id": l.id,
            "team_name": l.team_name,
            "member_name": l.member_name,
            "method": l.method,
            "endpoint": l.endpoint,
            "status_code": l.status_code,
            "ip_address": l.ip_address,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]}


# ── 팀/API 키 관리 (기존) ─────────────────────────────────

@app.post("/admin/member")
def create_member(
    name: str,
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    raw_key = generate_key()
    api_key = APIKey(team_id=team.id, key_hash=hash_key(raw_key), label=name)
    session.add(api_key)
    session.flush()

    member = TeamMember(team_id=team.id, name=name, api_key_id=api_key.id)
    session.add(member)
    session.commit()

    return {"member": name, "api_key": raw_key}


# ── 대시보드 ──────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))


# ── 공개 API ─────────────────────────────────────────────

@app.get("/api/teams")
def list_teams(session: Session = Depends(get_session)):
    teams = session.query(Team).filter_by(enabled=True).all()
    return {"teams": [
        {"id": t.id, "name": t.name,
         "member_count": session.query(TeamMember).filter_by(team_id=t.id).count()}
        for t in teams
    ]}


class JoinRequest(BaseModel):
    team_id: str
    join_code: str
    name: str
    email: str = ""


@app.post("/api/join")
def join_team(req: JoinRequest, session: Session = Depends(get_session)):
    team = session.query(Team).filter_by(id=req.team_id, enabled=True).first()
    if not team:
        raise HTTPException(404, "팀을 찾을 수 없습니다")
    if team.join_code and team.join_code != req.join_code:
        raise HTTPException(403, "초대 코드가 올바르지 않습니다")

    existing = session.query(TeamMember).filter_by(team_id=team.id, email=req.email).first()
    if existing and req.email:
        old_key = session.query(APIKey).filter_by(id=existing.api_key_id).first()
        if old_key:
            raw_key = generate_key()
            old_key.key_hash = hash_key(raw_key)
            old_key.enabled = True
            existing.enabled = True
            session.commit()
            return {"team": team.name, "member": req.name, "api_key": raw_key, "rejoined": True}

    raw_key = generate_key()
    api_key = APIKey(team_id=team.id, key_hash=hash_key(raw_key), label=req.name)
    session.add(api_key)
    session.flush()

    member = TeamMember(team_id=team.id, name=req.name, email=req.email, api_key_id=api_key.id)
    session.add(member)
    session.commit()

    return {"team": team.name, "member": req.name, "api_key": raw_key}


@app.get("/api/team/info")
def team_info(team: Team = Depends(get_team), session: Session = Depends(get_session)):
    count = session.query(TeamMember).filter_by(team_id=team.id).count()
    return {"id": team.id, "name": team.name, "member_count": count}


@app.post("/admin/team")
def create_team(name: str, join_code: str = "", session: Session = Depends(get_session)):
    code = join_code or secrets.token_urlsafe(6)
    team = Team(name=name, join_code=code)
    session.add(team)
    session.flush()

    raw_key = generate_key()
    api_key = APIKey(team_id=team.id, key_hash=hash_key(raw_key), label="admin")
    session.add(api_key)
    session.commit()

    return {"team_id": team.id, "team_name": name, "join_code": code,
            "admin_api_key": raw_key,
            "dashboard_url": f"http://localhost:8000/dashboard?key={raw_key}"}


# ── 캡처 ─────────────────────────────────────────────────

def _process_capture(team_id: str, team_name: str, req: CaptureRequest):
    if req.transcript:
        text = req.transcript
    elif req.messages:
        lines = [f"[{m.get('role','?')}] {m.get('content','')}" for m in req.messages]
        text = "\n\n".join(lines)
    else:
        return

    ctx = {"team_id": team_id, "team_name": team_name, "member_name": req.member or ""}
    items = analyzer.analyze(text, platform=req.platform, ctx=ctx)
    with Session(engine) as session:
        for item in items:
            try:
                memory_store.save(
                    session, team_id,
                    item["type"], item["description"], item["content"],
                    float(item.get("confidence", 0.7)),
                    list(item.get("tags") or []),
                    platform=req.platform,
                    captured_by=req.member or "",
                )
            except Exception:
                pass


@app.post("/api/capture")
def capture(
    req: CaptureRequest,
    background_tasks: BackgroundTasks,
    team: Team = Depends(get_team),
):
    background_tasks.add_task(_process_capture, team.id, team.name, req)
    return {"status": "queued", "platform": req.platform}


# ── 도메인 조회 ──────────────────────────────────────────

@app.get("/api/domain")
def get_domain_summary(
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    return reporter.domain_summary(session, team.id)


@app.get("/api/domain/members")
def get_members(
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    from collections import Counter
    mems = memory_store.list_all(session, team.id)
    counter = Counter(m.captured_by or "미설정" for m in mems)
    return {
        "members": [
            {"name": name, "memory_count": count}
            for name, count in counter.most_common()
        ]
    }


@app.get("/api/team/roster")
def get_team_roster(
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    members = session.query(TeamMember).filter_by(team_id=team.id, enabled=True).all()
    return {
        "team_name": team.name,
        "members": [{"name": m.name, "email": m.email} for m in members],
    }


@app.get("/api/context/recent")
def get_recent_context(
    days: int = Query(default=7, le=30),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
    api_key: APIKey = Depends(get_api_key),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    member_name = api_key.label or None

    q = session.query(Memory).filter(
        Memory.team_id == team.id,
        Memory.created_at >= cutoff,
    )
    if member_name:
        q = q.filter(Memory.captured_by == member_name)
    mems = q.order_by(Memory.created_at.desc()).limit(20).all()
    return {"memories": [memory_store.to_dict(m) for m in mems], "member": member_name, "days": days}


@app.get("/api/domain/member/{member_name}")
def get_member_domain(
    member_name: str,
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    all_mems = memory_store.list_all(session, team.id)
    mems = [m for m in all_mems if (m.captured_by or "미설정") == member_name]
    if not mems:
        return {"member": member_name, "total": 0, "groups": {}}
    return {
        "member": member_name,
        **reporter.domain_summary_from_memories(mems),
    }


# ── 컨텍스트 ─────────────────────────────────────────────

@app.get("/api/context")
def get_context(
    q: str = Query(default=""),
    limit: int = Query(default=10, le=50),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    mems = memory_store.query(session, team.id, q, limit=limit)
    return {"memories": [memory_store.to_dict(m) for m in mems]}


@app.get("/api/context/brief")
def get_context_brief(
    max_chars: int = Query(default=500, le=1500),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    mems = memory_store.list_all(session, team.id)
    dicts = [memory_store.to_dict(m) for m in mems]
    ctx = {"team_id": team.id, "team_name": team.name}
    brief = compressor.get_brief(team.id, dicts, max_chars=max_chars, ctx=ctx)
    return {"brief": brief, "memory_count": len(mems)}


# ── 메모리 CRUD ──────────────────────────────────────────

@app.post("/api/memory")
def save_memory(
    req: MemoryRequest,
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    mem = memory_store.save(
        session, team.id,
        req.type, req.description, req.content,
        req.confidence, req.tags,
    )
    return memory_store.to_dict(mem)


@app.get("/api/memory")
def list_memories(
    type: Optional[str] = Query(default=None),
    tag: Optional[list[str]] = Query(default=None),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    mems = memory_store.list_all(session, team.id, mem_type=type, tags=tag)
    return {"memories": [memory_store.to_dict(m) for m in mems]}


@app.delete("/api/memory/{memory_id}")
def delete_memory(
    memory_id: str,
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    ok = memory_store.delete(session, team.id, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": memory_id}


# ── 리포트 ───────────────────────────────────────────────

@app.get("/api/report/daily")
def get_daily_report(
    date: Optional[str] = Query(default=None),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    from datetime import date as _date
    target = _date.fromisoformat(date) if date else None
    return reporter.generate_daily(session, team.id, target)


@app.get("/api/report/weekly")
def get_weekly_report(
    week: Optional[str] = Query(default=None),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    return reporter.generate_weekly(session, team.id, week)


@app.get("/api/reports")
def list_reports(
    report_type: Optional[str] = Query(default=None),
    limit: int = Query(default=10),
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    q = session.query(Report).filter_by(team_id=team.id)
    if report_type:
        q = q.filter_by(report_type=report_type)
    reports = q.order_by(Report.created_at.desc()).limit(limit).all()
    return {"reports": [
        {"id": r.id, "type": r.report_type, "period": r.period,
         "new_count": r.new_memory_count, "created_at": r.created_at.isoformat(),
         "preview": r.content[:200] + "..." if len(r.content) > 200 else r.content}
        for r in reports
    ]}


# ── 위클리 스케줄 설정 ───────────────────────────────────

@app.post("/api/schedule/weekly")
def set_weekly_schedule(
    req: ScheduleRequest,
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    DAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
    sched = session.query(WeeklySchedule).filter_by(team_id=team.id).first()
    if sched:
        sched.day_of_week = req.day_of_week
        sched.hour = req.hour
        sched.minute = req.minute
        sched.enabled = req.enabled
    else:
        sched = WeeklySchedule(
            team_id=team.id,
            day_of_week=req.day_of_week,
            hour=req.hour,
            minute=req.minute,
            enabled=req.enabled,
        )
        session.add(sched)
    session.commit()

    if req.enabled:
        _register_job(team.id, req.day_of_week, req.hour, req.minute)
    else:
        job_id = f"weekly_{team.id}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    day_str = DAY_KR[req.day_of_week]
    return {
        "status": "ok",
        "schedule": f"매주 {day_str}요일 {req.hour:02d}:{req.minute:02d}",
        "enabled": req.enabled,
    }


@app.get("/api/schedule/weekly")
def get_weekly_schedule(
    team: Team = Depends(get_team),
    session: Session = Depends(get_session),
):
    DAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
    sched = session.query(WeeklySchedule).filter_by(team_id=team.id).first()
    if not sched:
        return {"configured": False}
    return {
        "configured": True,
        "day_of_week": sched.day_of_week,
        "day_kr": DAY_KR[sched.day_of_week],
        "hour": sched.hour,
        "minute": sched.minute,
        "enabled": sched.enabled,
        "schedule": f"매주 {DAY_KR[sched.day_of_week]}요일 {sched.hour:02d}:{sched.minute:02d}",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ── 멤버 대시보드 ─────────────────────────────────────────

@app.get("/member", response_class=HTMLResponse)
def member_page():
    return HTMLResponse(MEMBER_HTML.read_text(encoding="utf-8"))


class MemberLoginRequest(BaseModel):
    name: str
    email: str
    team_id: Optional[str] = None


@app.post("/member/login")
def member_login(req: MemberLoginRequest, session: Session = Depends(get_session)):
    """이름+이메일로 멤버 로그인 → JWT 발급."""
    # 이메일로 우선 조회
    query = session.query(TeamMember).filter_by(enabled=True)
    if req.team_id:
        query = query.filter_by(team_id=req.team_id)

    member = None
    if req.email:
        member = query.filter_by(email=req.email).first()
    if not member:
        member = query.filter_by(name=req.name).first()

    if not member:
        raise HTTPException(404, "등록된 팀원을 찾을 수 없습니다. 이름과 이메일을 확인하거나 관리자에게 문의하세요.")

    team = session.query(Team).filter_by(id=member.team_id, enabled=True).first()
    if not team:
        raise HTTPException(403, "소속 팀이 비활성화되었습니다.")

    from server.db import _now
    member.last_active = _now()
    session.commit()

    token = create_member_token(member.id, team.id, member.name)
    return {
        "token": token,
        "member_name": member.name,
        "team_name": team.name,
        "team_id": team.id,
    }


@app.get("/member/me")
def member_me(ctx: MemberContext = Depends(get_member_ctx)):
    return {
        "id": ctx.member.id,
        "name": ctx.member.name,
        "email": ctx.member.email,
        "team_id": ctx.team.id,
        "team_name": ctx.team.name,
        "last_active": ctx.member.last_active.isoformat() if ctx.member.last_active else None,
        "created_at": ctx.member.created_at.isoformat(),
    }


@app.get("/member/domain")
def member_domain(
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """내가 캡처한 도메인 지식."""
    all_mems = memory_store.list_all(session, ctx.team.id)
    mems = [m for m in all_mems if (m.captured_by or "") == ctx.member.name]
    return {
        "member": ctx.member.name,
        "team": ctx.team.name,
        **reporter.domain_summary_from_memories(mems),
    }


@app.get("/member/team-domain")
def member_team_domain(
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """팀 전체 도메인 요약."""
    return reporter.domain_summary(session, ctx.team.id)


@app.get("/member/memories")
def member_memories(
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    all_mems = memory_store.list_all(session, ctx.team.id)
    mems = [m for m in all_mems if (m.captured_by or "") == ctx.member.name]
    return {"memories": [memory_store.to_dict(m) for m in mems]}


class MemoAddRequest(BaseModel):
    description: str
    content: str = ""
    tags: list[str] = []
    mem_type: str = "fact"


@app.post("/member/memo")
def member_add_memo(
    req: MemoAddRequest,
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """직접 메모 추가."""
    mem = memory_store.save(
        session, ctx.team.id,
        mem_type=req.mem_type,
        description=req.description,
        content=req.content or req.description,
        confidence=1.0,
        tags=req.tags,
        platform="manual",
        captured_by=ctx.member.name,
    )
    return {"ok": True, "id": mem.id}


@app.get("/member/team-knowledge")
def member_team_knowledge(
    q: str = Query(default=""),
    tag: str = Query(default=""),
    limit: int = Query(default=50, le=200),
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """팀 전체 지식 검색."""
    if q:
        mems = memory_store.query(session, ctx.team.id, q, limit=limit)
    else:
        mems = memory_store.list_all(session, ctx.team.id)
        if tag:
            mems = [m for m in mems if tag in (json.loads(m.tags or "[]"))]
        mems = mems[:limit]
    all_tags = set()
    for m in memory_store.list_all(session, ctx.team.id):
        all_tags.update(json.loads(m.tags or "[]"))
    return {
        "memories": [memory_store.to_dict(m) for m in mems],
        "total": len(mems),
        "all_tags": sorted(all_tags),
    }


@app.get("/member/team-members")
def member_team_members(
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """팀원 기여 현황."""
    from collections import Counter
    all_mems = memory_store.list_all(session, ctx.team.id)
    counter = Counter(m.captured_by or "미설정" for m in all_mems)
    return {
        "members": [
            {"name": name, "memory_count": count, "is_me": name == ctx.member.name}
            for name, count in counter.most_common()
        ]
    }


@app.post("/member/report/weekly")
def member_generate_weekly(
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """오늘 기준 7일치 개인 위클리 리포트 생성."""
    result = reporter.generate_member_weekly(session, ctx.team.id, ctx.member.name)
    return result


@app.get("/member/reports")
def member_reports(
    limit: int = Query(default=5),
    ctx: MemberContext = Depends(get_member_ctx),
    session: Session = Depends(get_session),
):
    """팀 리포트 목록."""
    reports = (
        session.query(Report)
        .filter_by(team_id=ctx.team.id)
        .order_by(Report.created_at.desc())
        .limit(limit)
        .all()
    )
    return {"reports": [
        {"id": r.id, "type": r.report_type, "period": r.period,
         "new_count": r.new_memory_count, "created_at": r.created_at.isoformat(),
         "content": r.content}
        for r in reports
    ]}
