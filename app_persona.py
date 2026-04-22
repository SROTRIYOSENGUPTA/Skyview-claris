"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Extended Application
This file extends the existing app.py with:
  - PostgreSQL database initialization
  - Azure AD SSO authentication
  - /persona routes for multi-persona chat
  - Admin blueprint registration
  - Microsoft 365 / Microsoft Graph integration (mail, calendar, files)
INTEGRATION INSTRUCTIONS:
  Import and call `init_multipersona(app)` from your existing app.py
  after the Flask app is created. See the bottom of this file for
  the exact code to add.
"""
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from flask import (
    Flask, Blueprint, render_template, request, jsonify, session,
    Response, stream_with_context, redirect, url_for,
)
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Base, Employee, Persona, Conversation
from persona_engine import PersonaChatbot, load_persona_by_email
from knowledge import KnowledgeRetriever
from compliance import ComplianceEngine
from chatbot import MODEL, MAX_TOKENS, build_content_blocks, execute_tool

# Microsoft 365 / Graph integration
from msgraph import (
    register_msgraph_routes, MSGRAPH_TOOLS, execute_msgraph_tool,
)

logger = logging.getLogger("skyview.persona_app")
# ─────────────────────────────────────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────────────────────────────────────
def init_database(app: Flask):
    """Initialize PostgreSQL connection and create tables."""
    database_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://localhost:5432/claris_multipersona"
    )
    # Render.com uses postgres:// but SQLAlchemy needs postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    engine = create_engine(
        database_url,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        echo=False,
    )
    # Create all tables (use Alembic migrations in production)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    db_session = scoped_session(session_factory)
    # Store on app for access in routes
    app.extensions["db_engine"] = engine
    app.extensions["db_session"] = db_session
    @app.teardown_appcontext
    def shutdown_session(exception=None):
        db_session.remove()
    logger.info("Database initialized successfully")
    return db_session
# ─────────────────────────────────────────────────────────────────────────────
# AZURE AD SSO (via MSAL)
# ─────────────────────────────────────────────────────────────────────────────
class EmployeeUser(UserMixin):
    """Flask-Login user wrapper for Employee model."""
    def __init__(self, employee: Employee):
        self.employee = employee
        self.id = str(employee.id)
        self.email = employee.email
        self.name = employee.full_name
        self.is_admin_user = employee.is_admin
def init_auth(app: Flask, db_session):
    """Initialize Flask-Login and Azure AD SSO."""
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "persona.persona_login"
    @login_manager.user_loader
    def load_user(user_id):
        db = db_session()
        employee = db.query(Employee).filter(Employee.id == user_id).first()
        if employee:
            return EmployeeUser(employee)
        return None
    # Azure AD configuration
    app.config["AZURE_CLIENT_ID"] = os.environ.get("AZURE_CLIENT_ID", "")
    app.config["AZURE_CLIENT_SECRET"] = os.environ.get("AZURE_CLIENT_SECRET", "")
    app.config["AZURE_TENANT_ID"] = os.environ.get("AZURE_TENANT_ID", "")
    app.config["AZURE_REDIRECT_URI"] = os.environ.get(
        "AZURE_REDIRECT_URI",
        "http://localhost:5000/auth/callback"
    )
    logger.info("Authentication initialized")
# ─────────────────────────────────────────────────────────────────────────────
# PERSONA BLUEPRINT
# ─────────────────────────────────────────────────────────────────────────────
persona_bp = Blueprint("persona", __name__, template_folder="templates")
# In-memory session store for persona chatbots (same pattern as existing Claris)
_persona_sessions: dict[str, PersonaChatbot] = {}
@persona_bp.route("/persona/login", methods=["GET"])
def persona_login():
    """Render login page or redirect to Azure AD."""
    azure_client_id = os.environ.get("AZURE_CLIENT_ID")
    if azure_client_id:
        # Redirect to Azure AD
        tenant_id = os.environ.get("AZURE_TENANT_ID", "common")
        redirect_uri = os.environ.get("AZURE_REDIRECT_URI", "http://localhost:5000/auth/callback")
        auth_url = (
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize"
            f"?client_id={azure_client_id}"
            f"&response_type=code"
            f"&redirect_uri={redirect_uri}"
            f"&scope=openid+profile+email"
            f"&response_mode=query"
        )
        return redirect(auth_url)
    else:
        # Dev mode: show email login form
        return render_template("persona_login.html")
@persona_bp.route("/persona/login/dev", methods=["POST"])
def persona_login_dev():
    """Dev-mode login by email (no SSO). Disable in production."""
    if os.environ.get("FLASK_ENV") == "production":
        return jsonify({"error": "Dev login disabled in production"}), 403
    data = request.get_json(silent=True) or request.form
    email = data.get("email", "").strip().lower()
    if not email:
        return jsonify({"error": "Email required"}), 400
    db = persona_bp.extensions_db()
    employee = db.query(Employee).filter(
        Employee.email == email,
        Employee.is_active == True,
    ).first()
    if not employee:
        return jsonify({"error": "Employee not found. Contact your admin."}), 404
    # Login
    login_user(EmployeeUser(employee))
    employee.last_login_at = datetime.now(timezone.utc)
    db.commit()
    session["employee_id"] = str(employee.id)
    session["employee_email"] = employee.email
    session["employee_name"] = employee.full_name
    logger.info(f"Dev login: {employee.full_name} ({employee.email})")
    return redirect(url_for("persona.persona_chat"))
@persona_bp.route("/auth/callback")
def auth_callback():
    """Azure AD OAuth callback."""
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "No auth code received"}), 400
    try:
        import msal
        client_id = os.environ.get("AZURE_CLIENT_ID")
        client_secret = os.environ.get("AZURE_CLIENT_SECRET")
        tenant_id = os.environ.get("AZURE_TENANT_ID", "common")
        redirect_uri = os.environ.get("AZURE_REDIRECT_URI")
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        msal_app = msal.ConfidentialClientApplication(
            client_id,
            authority=authority,
            client_credential=client_secret,
        )
        result = msal_app.acquire_token_by_authorization_code(
            code,
            scopes=["openid", "profile", "email"],
            redirect_uri=redirect_uri,
        )
        if "error" in result:
            logger.error(f"Azure AD error: {result.get('error_description')}")
            return jsonify({"error": "Authentication failed"}), 401
        # Extract user info from ID token claims
        claims = result.get("id_token_claims", {})
        email = claims.get("preferred_username", claims.get("email", "")).lower()
        name = claims.get("name", "")
        oid = claims.get("oid", "")
        if not email:
            return jsonify({"error": "Could not determine email from SSO"}), 400
        # Find or create employee
        db = persona_bp.extensions_db()
        employee = db.query(Employee).filter(Employee.email == email).first()
        if not employee:
            # Auto-create employee record (they can set up persona later)
            employee = Employee(
                email=email,
                full_name=name,
                azure_ad_oid=oid,
                role="advisor",
            )
            db.add(employee)
            db.commit()
            logger.info(f"Auto-created employee from SSO: {name} ({email})")
        # Update Azure AD OID if not set
        if not employee.azure_ad_oid and oid:
            employee.azure_ad_oid = oid
        employee.last_login_at = datetime.now(timezone.utc)
        db.commit()
        # Login
        login_user(EmployeeUser(employee))
        session["employee_id"] = str(employee.id)
        session["employee_email"] = employee.email
        session["employee_name"] = employee.full_name
        logger.info(f"SSO login: {employee.full_name} ({employee.email})")
        return redirect(url_for("persona.persona_chat"))
    except ImportError:
        logger.error("msal not installed. Run: pip install msal")
        return jsonify({"error": "SSO not configured. Install msal package."}), 500
    except Exception as e:
        logger.error(f"SSO callback error: {e}")
        return jsonify({"error": "Authentication failed"}), 500
@persona_bp.route("/persona/logout")
def persona_logout():
    """Logout and clear session."""
    logout_user()
    session.clear()
    return redirect(url_for("persona.persona_login"))
@persona_bp.route("/persona")
@login_required
def persona_chat():
    """Main persona chat interface."""
    db = persona_bp.extensions_db()
    employee_id = session.get("employee_id")
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return redirect(url_for("persona.persona_login"))
    persona = employee.persona
    has_persona = persona is not None and persona.is_active
    # Load recent conversations
    recent_convos = db.query(Conversation).filter(
        Conversation.employee_id == employee_id,
        Conversation.is_active == True,
    ).order_by(Conversation.updated_at.desc()).limit(10).all()
    return render_template(
        "persona.html",
        employee=employee,
        persona=persona,
        has_persona=has_persona,
        recent_conversations=recent_convos,
    )
@persona_bp.route("/persona/terminal")
@login_required
def persona_terminal():
    """Render the Bloomberg-style Market Terminal for the logged-in advisor."""
    db = persona_bp.extensions_db()
    employee_id = session.get("employee_id")
    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return redirect(url_for("persona.persona_login"))
    return render_template("market_terminal_v4.html", employee=employee)
@persona_bp.route("/persona/chat/stream", methods=["POST"])
@login_required
def persona_chat_stream():
    """
    Persona-aware streaming chat endpoint.
    Same SSE pattern as existing /chat/stream but routes through the
    persona engine, knowledge layer, and compliance engine.
    """
    db = persona_bp.extensions_db()
    employee_id = session.get("employee_id")
    # Load persona
    persona = load_persona_by_email(db, session.get("employee_email", ""))
    if not persona:
        return jsonify({"error": "No active persona found. Contact your admin."}), 404
    data = request.get_json() or {}
    msg = data.get("message", "").strip()
    attachments = data.get("attachments", [])
    conversation_id = data.get("conversation_id")
    if not msg and not attachments:
        return jsonify({"error": "Message or attachment required."}), 400
    if not msg:
        msg = "Please analyse the attached file(s)."
    # Get or create chatbot session
    session_key = f"{employee_id}:{conversation_id or 'default'}"
    if session_key not in _persona_sessions:
        # Initialize with knowledge retriever and compliance engine
        knowledge_retriever = KnowledgeRetriever(db)
        compliance_engine = ComplianceEngine(db)
        _persona_sessions[session_key] = PersonaChatbot(
            persona=persona,
            session_id=conversation_id or str(uuid.uuid4())[:8],
            knowledge_retriever=knowledge_retriever,
            compliance_engine=compliance_engine,
        )
    bot = _persona_sessions[session_key]
    def generate():
        try:
            import json as _json
            # Check for escalation
            compliance = ComplianceEngine(db)
            if compliance.should_escalate(msg):
                yield f"data: {_json.dumps({'escalation': True})}\n\n"
            # Build system prompt
            bot._trim_history()
            bot.message_count += 1
            content = build_content_blocks(msg, attachments or None)
            bot.history.append({"role": "user", "content": content})
            system_prompt = bot.get_system_prompt(msg)
            tools_used = []
            # Merge M365 tools into the tool list for this request
            _tools = (bot.permitted_tools or []) + MSGRAPH_TOOLS
            # Initial API call
            response = bot.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=_tools,
                messages=bot.history,
            )
            # Tool-use agentic loop
            while response.stop_reason == "tool_use":
                bot.history.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tools_used.append(block.name)
                        yield f"data: {_json.dumps({'tool': block.name})}\n\n"
                        if block.name.startswith("msgraph_"):
                            result_data = execute_msgraph_tool(
                                block.name, block.input,
                                db=persona_bp.extensions_db(),
                                employee_id=session.get("employee_id"),
                            )
                            result = _json.dumps(result_data)
                        else:
                            result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                bot.history.append({"role": "user", "content": tool_results})
                response = bot.client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=_tools,
                    messages=bot.history,
                )
            # Extract final text
            final_text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            # Post-processing compliance check
            check_result = compliance.check_response(
                final_text,
                conversation_id=conversation_id,
                employee_id=employee_id,
            )
            if check_result.get("corrected_text"):
                final_text = check_result["corrected_text"]
            bot.history.append({"role": "assistant", "content": final_text})
            # Stream to browser in small chunks
            CHUNK = 4
            for i in range(0, len(final_text), CHUNK):
                yield f"data: {_json.dumps({'token': final_text[i:i+CHUNK]})}\n\n"
            # Send completion with metadata
            yield f"data: {_json.dumps({'done': True, 'tools_used': tools_used, 'compliance_flags': len(check_result.get('flags', []))})}\n\n"
            # Persist conversation to database
            _save_conversation(db, employee_id, persona.id, bot, conversation_id)
        except Exception as e:
            logger.exception("Persona stream error")
            yield f"data: {json.dumps({'error': 'An internal error occurred. Please try again.'})}\n\n"
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
@persona_bp.route("/persona/reset", methods=["POST"])
@login_required
def persona_reset():
    """Reset the current persona chat session."""
    employee_id = session.get("employee_id")
    session_key = f"{employee_id}:default"
    if session_key in _persona_sessions:
        _persona_sessions[session_key].reset()
    return jsonify({"status": "reset"})
@persona_bp.route("/persona/session", methods=["GET"])
@login_required
def persona_session_info():
    """Get current persona session info."""
    employee_id = session.get("employee_id")
    session_key = f"{employee_id}:default"
    if session_key in _persona_sessions:
        return jsonify(_persona_sessions[session_key].get_summary())
    return jsonify({"status": "no_active_session"})
# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────
def _save_conversation(db, employee_id, persona_id, bot, conversation_id=None):
    """Save or update conversation in the database."""
    try:
        if conversation_id:
            conv = db.query(Conversation).filter(
                Conversation.id == conversation_id
            ).first()
        else:
            conv = None
        if not conv:
            conv = Conversation(
                employee_id=employee_id,
                persona_id=persona_id,
                title=_generate_title(bot.history),
                messages=bot.history[-2:],  # Last exchange
                tools_invoked=bot.tools_invoked,
                compliance_flags=bot.compliance_flags,
                message_count=bot.message_count,
                total_tokens=bot.total_tokens,
            )
            db.add(conv)
        else:
            conv.messages = bot.history[-20:]  # Keep last 20
            conv.tools_invoked = bot.tools_invoked
            conv.compliance_flags = bot.compliance_flags
            conv.message_count = bot.message_count
            conv.total_tokens = bot.total_tokens
            conv.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to save conversation: {e}")
        db.rollback()
def _generate_title(history: list) -> str:
    """Generate a short title from the first user message."""
    for msg in history:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block["text"][:80]
                        return text + ("..." if len(block["text"]) > 80 else "")
            elif isinstance(content, str):
                return content[:80] + ("..." if len(content) > 80 else "")
    return "New Conversation"
# ─────────────────────────────────────────────────────────────────────────────
# INIT FUNCTION — call this from your existing app.py
# ─────────────────────────────────────────────────────────────────────────────
def init_multipersona(app: Flask):
    """
    Initialize the multi-persona platform on an existing Flask app.
    Call this in your app.py after creating the Flask app:
        from app_persona import init_multipersona
        init_multipersona(app)
    """
    # Initialize database
    db_session = init_database(app)
    # Initialize auth
    init_auth(app, db_session)
    # Helper to access DB from blueprint
    def get_db():
        return db_session()
    persona_bp.extensions_db = get_db
    # Register blueprints
    app.register_blueprint(persona_bp)
    from admin import admin_bp
    admin_bp.extensions_db = get_db
    app.register_blueprint(admin_bp)
    # Mount Microsoft 365 OAuth + Graph routes
    register_msgraph_routes(
        app,
        get_db_session=get_db,
        get_current_employee_id=lambda: session.get("employee_id"),
    )
    logger.info("Multi-persona platform initialized")
    logger.info("  Persona chat: /persona")
    logger.info("  Admin panel:  /admin")
    logger.info("  SSO login:    /persona/login")
    logger.info("  M365 connect: /auth/microsoft/start")
# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE MODE (for testing without existing app.py)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))
    init_multipersona(app)
    print("\n" + "=" * 60)
    print("  SKYVIEW — Claris Multi-Persona Platform")
    print("  Persona chat → http://127.0.0.1:5000/persona")
    print("  Admin panel  → http://127.0.0.1:5000/admin")
    print("=" * 60 + "\n")
    app.run(debug=True, port=5000, host="127.0.0.1")
