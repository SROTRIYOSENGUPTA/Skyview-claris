"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Microsoft 365 / Graph Integration

Self-contained module that wires Microsoft Graph (delegated auth) into the
Claris persona chatbot. Each advisor connects their own M365 account via
OAuth consent; Claris stores the refresh token in Postgres and uses it to
read mail, calendar, OneDrive and SharePoint on the advisor's behalf.

Public surface:
  - register_msgraph_routes(blueprint)   # mounts /auth/microsoft/*
  - MSGRAPH_TOOLS                        # tool definitions for Anthropic API
  - execute_msgraph_tool(name, args, employee_id, db)  # dispatcher
  - is_connected(db, employee_id)        # UI helper

Env vars required on Render:
  MSGRAPH_CLIENT_ID
  MSGRAPH_TENANT_ID
  MSGRAPH_CLIENT_SECRET
  MSGRAPH_REDIRECT_URI  (optional; defaults below)
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from flask import Blueprint, redirect, request, session, url_for
from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID

from models import Base, Employee

logger = logging.getLogger("skyview.msgraph")


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTHORIZE_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
TOKEN_URL_TMPL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

SCOPES = [
    "User.Read",
    "Mail.Read",
    "Calendars.Read",
    "Files.Read",
    "Sites.Read.All",
    "offline_access",
]

DEFAULT_REDIRECT = "https://skyview-claris.onrender.com/auth/microsoft/callback"


def _cfg():
    cid = os.environ.get("MSGRAPH_CLIENT_ID")
    tid = os.environ.get("MSGRAPH_TENANT_ID")
    sec = os.environ.get("MSGRAPH_CLIENT_SECRET")
    red = os.environ.get("MSGRAPH_REDIRECT_URI", DEFAULT_REDIRECT)
    if not (cid and tid and sec):
        raise RuntimeError(
            "MSGRAPH_CLIENT_ID / MSGRAPH_TENANT_ID / MSGRAPH_CLIENT_SECRET "
            "env vars are not set — M365 integration disabled."
        )
    return cid, tid, sec, red


# ─────────────────────────────────────────────────────────────────────────────
# TOKEN STORAGE MODEL  (add to Alembic or let init_database() create_all it)
# ─────────────────────────────────────────────────────────────────────────────

def _utcnow():
    return datetime.now(timezone.utc)


class MSGraphToken(Base):
    """Per-employee OAuth tokens for Microsoft Graph (delegated auth)."""
    __tablename__ = "msgraph_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id = Column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    access_token = Column(Text, nullable=False)
    refresh_token = Column(Text, nullable=False)
    token_type = Column(String(32), default="Bearer", nullable=False)
    scope = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    account_upn = Column(String(255), nullable=True)  # user@skyviewadv.com
    display_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    def __repr__(self) -> str:
        return f"<MSGraphToken emp={self.employee_id} exp={self.expires_at}>"


# ─────────────────────────────────────────────────────────────────────────────
# OAUTH FLOW
# ─────────────────────────────────────────────────────────────────────────────

def _build_authorize_url(state: str) -> str:
    cid, tid, _sec, red = _cfg()
    params = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": red,
        "response_mode": "query",
        "scope": " ".join(SCOPES),
        "state": state,
        "prompt": "select_account",
    }
    return AUTHORIZE_URL_TMPL.format(tenant=tid) + "?" + urlencode(params)


def _exchange_code(code: str) -> dict:
    cid, tid, sec, red = _cfg()
    resp = requests.post(
        TOKEN_URL_TMPL.format(tenant=tid),
        data={
            "client_id": cid,
            "client_secret": sec,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": red,
            "scope": " ".join(SCOPES),
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _refresh_token(refresh: str) -> dict:
    cid, tid, sec, _red = _cfg()
    resp = requests.post(
        TOKEN_URL_TMPL.format(tenant=tid),
        data={
            "client_id": cid,
            "client_secret": sec,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "scope": " ".join(SCOPES),
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def _save_tokens(db, employee_id: uuid.UUID, tok: dict, upn: str = None,
                 display: str = None) -> MSGraphToken:
    row = db.query(MSGraphToken).filter(
        MSGraphToken.employee_id == employee_id
    ).first()
    expires = _utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)

    if row is None:
        row = MSGraphToken(
            employee_id=employee_id,
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token") or "",
            token_type=tok.get("token_type", "Bearer"),
            scope=tok.get("scope"),
            expires_at=expires,
            account_upn=upn,
            display_name=display,
        )
        db.add(row)
    else:
        row.access_token = tok["access_token"]
        if tok.get("refresh_token"):
            row.refresh_token = tok["refresh_token"]
        row.token_type = tok.get("token_type", "Bearer")
        row.scope = tok.get("scope") or row.scope
        row.expires_at = expires
        if upn:     row.account_upn = upn
        if display: row.display_name = display
    db.commit()
    return row


def _get_valid_token(db, employee_id: uuid.UUID) -> Optional[str]:
    """Return a currently-valid access token, refreshing if expired."""
    row = db.query(MSGraphToken).filter(
        MSGraphToken.employee_id == employee_id
    ).first()
    if not row:
        return None
    if row.expires_at > _utcnow() + timedelta(seconds=30):
        return row.access_token
    # Refresh
    try:
        tok = _refresh_token(row.refresh_token)
        _save_tokens(db, employee_id, tok, row.account_upn, row.display_name)
        return tok["access_token"]
    except Exception as e:
        logger.error("Token refresh failed for %s: %s", employee_id, e)
        return None


def is_connected(db, employee_id: uuid.UUID) -> dict:
    row = db.query(MSGraphToken).filter(
        MSGraphToken.employee_id == employee_id
    ).first()
    if not row:
        return {"connected": False}
    return {
        "connected": True,
        "upn": row.account_upn,
        "display_name": row.display_name,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# GRAPH HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _graph(access_token: str, path: str, params: dict = None) -> dict:
    url = path if path.startswith("http") else GRAPH_BASE + path
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    r = requests.get(url, headers=headers, params=params or {}, timeout=20)
    if r.status_code >= 400:
        logger.warning("Graph %s -> %d %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
    return r.json()


def _graph_post(access_token: str, path: str, body: dict) -> dict:
    r = requests.post(
        GRAPH_BASE + path,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=20,
    )
    if r.status_code >= 400:
        logger.warning("Graph POST %s -> %d %s", path, r.status_code, r.text[:400])
        r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# TOOL DEFINITIONS  (Anthropic tool schema)
# ─────────────────────────────────────────────────────────────────────────────

MSGRAPH_TOOLS = [
    {
        "name": "msgraph_search_email",
        "description": (
            "Search the logged-in advisor's Outlook mailbox. Use for questions "
            "like 'what did Mark say about Q3 allocations' or 'find the email "
            "from the family office about cash flow'. Returns sender, subject, "
            "received date, and a short body preview. Only searches the "
            "advisor's own mailbox — never other employees'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keyword or phrase to search for (KQL syntax supported).",
                },
                "top": {
                    "type": "integer",
                    "description": "Max emails to return (default 10, max 25).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "msgraph_get_calendar",
        "description": (
            "List the advisor's upcoming (or recent) calendar events. Use for "
            "questions like 'what meetings do I have this week' or 'when am I "
            "next meeting with the Helgager team'. Returns title, start/end, "
            "attendees, and location."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "How many days forward to look (default 7). Use negative for past.",
                },
                "top": {
                    "type": "integer",
                    "description": "Max events to return (default 15, max 50).",
                },
            },
        },
    },
    {
        "name": "msgraph_search_files",
        "description": (
            "Search across SharePoint, OneDrive, and Teams files that the "
            "advisor has access to. Use for questions like 'find the Q3 "
            "committee deck' or 'where's the investment policy statement'. "
            "Returns filename, parent folder/site, last-modified date, and a "
            "link to open it. The advisor only sees files they are already "
            "permitted to view."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to search across file names and contents.",
                },
                "top": {
                    "type": "integer",
                    "description": "Max files to return (default 10, max 25).",
                },
            },
            "required": ["query"],
        },
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# TOOL IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _not_connected_msg() -> str:
    return (
        "The advisor has not connected their Microsoft 365 account yet. "
        "Ask them to click 'Connect Microsoft 365' in the Claris sidebar and "
        "approve the one-time consent prompt, then retry."
    )


def _tool_search_email(access_token: str, args: dict) -> dict:
    q = (args.get("query") or "").strip()
    top = max(1, min(int(args.get("top") or 10), 25))
    if not q:
        return {"error": "query is required"}
    data = _graph(access_token, "/me/messages", params={
        "$search": f'"{q}"',
        "$top": top,
        "$select": "subject,from,receivedDateTime,bodyPreview,webLink,isRead",
    })
    items = []
    for m in data.get("value", [])[:top]:
        sender = (m.get("from") or {}).get("emailAddress") or {}
        items.append({
            "subject": m.get("subject"),
            "from": sender.get("name") or sender.get("address"),
            "from_address": sender.get("address"),
            "received": m.get("receivedDateTime"),
            "preview": (m.get("bodyPreview") or "")[:300],
            "read": m.get("isRead"),
            "link": m.get("webLink"),
        })
    return {"count": len(items), "emails": items}


def _tool_get_calendar(access_token: str, args: dict) -> dict:
    days_ahead = int(args.get("days_ahead") if args.get("days_ahead") is not None else 7)
    top = max(1, min(int(args.get("top") or 15), 50))
    now = _utcnow()
    if days_ahead >= 0:
        start, end = now, now + timedelta(days=days_ahead)
    else:
        start, end = now + timedelta(days=days_ahead), now
    data = _graph(access_token, "/me/calendarView", params={
        "startDateTime": start.isoformat().replace("+00:00", "Z"),
        "endDateTime":   end.isoformat().replace("+00:00", "Z"),
        "$orderby": "start/dateTime",
        "$top": top,
        "$select": "subject,start,end,attendees,location,organizer,bodyPreview,webLink",
    })
    events = []
    for e in data.get("value", [])[:top]:
        attendees = [
            (a.get("emailAddress") or {}).get("name") or (a.get("emailAddress") or {}).get("address")
            for a in (e.get("attendees") or [])[:8]
        ]
        events.append({
            "subject": e.get("subject"),
            "start": (e.get("start") or {}).get("dateTime"),
            "end": (e.get("end") or {}).get("dateTime"),
            "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get("name"),
            "location": (e.get("location") or {}).get("displayName"),
            "attendees": [a for a in attendees if a],
            "preview": (e.get("bodyPreview") or "")[:200],
            "link": e.get("webLink"),
        })
    return {"count": len(events), "events": events}


def _tool_search_files(access_token: str, args: dict) -> dict:
    q = (args.get("query") or "").strip()
    top = max(1, min(int(args.get("top") or 10), 25))
    if not q:
        return {"error": "query is required"}
    body = {
        "requests": [{
            "entityTypes": ["driveItem", "listItem"],
            "query": {"queryString": q},
            "from": 0,
            "size": top,
            "fields": [
                "name", "webUrl", "lastModifiedDateTime",
                "createdBy", "parentReference",
            ],
        }]
    }
    data = _graph_post(access_token, "/search/query", body)
    hits = []
    for container in data.get("value", []):
        for hitset in container.get("hitsContainers", []):
            for h in hitset.get("hits", [])[:top]:
                res = h.get("resource", {}) or {}
                parent = res.get("parentReference") or {}
                creator = ((res.get("createdBy") or {}).get("user") or {}).get("displayName")
                hits.append({
                    "name": res.get("name"),
                    "site": parent.get("siteId") or parent.get("driveId"),
                    "path": parent.get("path"),
                    "modified": res.get("lastModifiedDateTime"),
                    "created_by": creator,
                    "link": res.get("webUrl") or h.get("hitId"),
                    "summary": (h.get("summary") or "")[:300],
                })
    return {"count": len(hits), "files": hits[:top]}


_TOOL_DISPATCH = {
    "msgraph_search_email":  _tool_search_email,
    "msgraph_get_calendar":  _tool_get_calendar,
    "msgraph_search_files":  _tool_search_files,
}


def execute_msgraph_tool(name: str, args: dict, db, employee_id: uuid.UUID) -> dict:
    """Call from the persona chat tool-use loop.

    Returns a dict that will be JSON-serialized into the tool_result content.
    Callers should wrap in json.dumps() when passing back to Anthropic.
    """
    fn = _TOOL_DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool: {name}"}
    token = _get_valid_token(db, employee_id)
    if not token:
        return {"error": _not_connected_msg()}
    try:
        return fn(token, args or {})
    except requests.HTTPError as e:
        return {
            "error": f"Microsoft Graph returned {e.response.status_code}",
            "detail": (e.response.text or "")[:400],
        }
    except Exception as e:
        logger.exception("msgraph tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

msgraph_bp = Blueprint("msgraph", __name__)


def register_msgraph_routes(app_or_bp, get_db_session, get_current_employee_id):
    """Attach /auth/microsoft/* routes to an existing Flask app or blueprint.

    Args:
      app_or_bp: Flask app or Blueprint to register routes on.
      get_db_session: callable returning a SQLAlchemy Session.
      get_current_employee_id: callable returning the logged-in employee UUID
                               (or None if not logged in).
    """

    @app_or_bp.route("/auth/microsoft/start")
    def msgraph_start():
        emp_id = get_current_employee_id()
        if not emp_id:
            return redirect("/persona/login")
        state = secrets.token_urlsafe(24)
        session["msgraph_oauth_state"] = state
        session["msgraph_oauth_emp"] = str(emp_id)
        return redirect(_build_authorize_url(state))

    @app_or_bp.route("/auth/microsoft/callback")
    def msgraph_callback():
        err = request.args.get("error")
        if err:
            return f"<h3>Microsoft sign-in failed</h3><p>{err}: " \
                   f"{request.args.get('error_description','')}</p>" \
                   f'<p><a href="/persona">Back to Claris</a></p>', 400

        code = request.args.get("code")
        state = request.args.get("state")
        expected = session.pop("msgraph_oauth_state", None)
        emp_id_raw = session.pop("msgraph_oauth_emp", None)
        if not code or not state or state != expected or not emp_id_raw:
            return "Invalid OAuth state — please retry Connect Microsoft 365.", 400

        try:
            tok = _exchange_code(code)
        except requests.HTTPError as e:
            return f"<h3>Token exchange failed</h3><pre>{e.response.text[:800]}</pre>", 400

        # Fetch profile so we can show who's connected
        try:
            me = _graph(tok["access_token"], "/me")
            upn = me.get("userPrincipalName") or me.get("mail")
            display = me.get("displayName")
        except Exception:
            upn, display = None, None

        db = get_db_session()
        try:
            _save_tokens(db, uuid.UUID(emp_id_raw), tok, upn, display)
        finally:
            pass  # session is scoped elsewhere

        return redirect("/persona?msgraph=connected")

    @app_or_bp.route("/auth/microsoft/status")
    def msgraph_status():
        emp_id = get_current_employee_id()
        if not emp_id:
            return {"connected": False, "reason": "not_logged_in"}
        db = get_db_session()
        return is_connected(db, emp_id)

    @app_or_bp.route("/auth/microsoft/disconnect", methods=["POST"])
    def msgraph_disconnect():
        emp_id = get_current_employee_id()
        if not emp_id:
            return {"ok": False}, 401
        db = get_db_session()
        db.query(MSGraphToken).filter(MSGraphToken.employee_id == emp_id).delete()
        db.commit()
        return {"ok": True}

    return app_or_bp
