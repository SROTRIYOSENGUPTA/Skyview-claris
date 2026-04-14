"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Extended Application

This file extends the existing app.py with:
  - PostgreSQL database initialization
  - Azure AD SSO authentication
  - /persona routes for multi-persona chat
  - Admin blueprint registration

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
    if os.environ.get("FLASK_ENV") == "production" and os.environ.get("AZURE_CLIENT_ID"):
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

            # Initial API call
            response = bot.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=bot.permitted_tools,
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
                    tools=bot.permitted_tools,
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

    logger.info("Multi-persona platform initialized")
    logger.info("  Persona chat: /persona")
    logger.info("  Admin panel:  /admin")
    logger.info("  SSO login:    /persona/login")


# ─────────────────────────────────────────────────────────────────────────────
# STANDALONE MODE (for testing without existing app.py)
# ─────────────────────────────────────────────────────────────────────────────
@persona_bp.route("/persona/seed", methods=["GET"])
def run_seed():
    """One-time seed endpoint."""
    try:
        db = persona_bp.extensions_db()
        from models import Employee, Persona
        existing = db.query(Employee).filter(Employee.email == "ssengupta@skyviewadv.com").first()
        if existing:
            return f"<h2>Already seeded</h2><p>{existing.full_name} exists.</p><p><a href='/persona'>Go to Claris</a></p>"
        srotriyo = Employee(email="ssengupta@skyviewadv.com", full_name="Srotriyo Sengupta", title="Quant AI Analyst", department="Investment & Technology", role="admin")
        db.add(srotriyo)
        db.flush()
        persona = Persona(employee_id=srotriyo.id, display_name="Srotriyo Sengupta", bio_summary="Quant AI Analyst at SkyView Investment Advisors", communication_style={"tone": "warm, data-driven"}, expertise_areas=["quantitative_analysis", "machine_learning"], education={"universities": ["Rutgers University", "Princeton University"]}, system_prompt_layer2="You are the personalized AI investment advisor for Srotriyo Sengupta, Quant AI Analyst at SkyView Investment Advisors LLC. You embody his unique combination of quantitative rigor, machine learning expertise, and strategic investment thinking.", tool_permissions=[], response_preferences={"default_length": "detailed", "format": "structured"}, is_active=True, version=1)
        db.add(persona)
        db.commit()
        return f"<h2>Seed complete!</h2><p>Created: {srotriyo.full_name} with persona.</p><p><a href='/persona'>Go to Claris</a></p>"
    except Exception as e:
        return f"<h2>Error</h2><pre>{e}</pre>"
@persona_bp.route("/persona/refresh-srotriyo", methods=["GET"])
def refresh_srotriyo():
    try:
        db = persona_bp.extensions_db()
        from models import Employee, Persona
        emp = db.query(Employee).filter(Employee.email == "ssengupta@skyviewadv.com").first()
        if not emp or not emp.persona:
            return "<h2>Error</h2><p>Run /persona/seed first.</p>"
        p = emp.persona
        p.bio_summary = (
            "Quant AI Analyst at SkyView Investment Advisors specializing in the "
            "intersection of machine learning and investment analysis. 3.5 years "
            "industry experience with prior roles at EquiLend (Wall Street) and "
            "NJ Transit (contractor). Rutgers & Princeton-educated computer engineer "
            "with specialized ML training. Multilingual (English, Bengali, Hindi, "
            "Marathi, Spanish, French). Interests include quant trading, chess, soccer."
        )
        p.education = {
            "universities": ["Rutgers University", "Princeton University"],
            "skills": ["Computer Engineering", "Machine Learning Specialized"],
            "languages": ["English", "Bengali", "Hindi", "Marathi", "Spanish", "French"],
            "years_experience": 3.5,
            "prior_experience": [
                {"company": "EquiLend", "context": "Wall Street"},
                {"company": "NJ Transit", "context": "Contractor"},
            ],
            "personal_interests": ["Quantitative trading", "Chess", "Soccer"],
        }
        # Strip any prior "ADDITIONAL BACKGROUND" block and re-append a fresh one
        base_prompt = p.system_prompt_layer2.split("\n\nADDITIONAL BACKGROUND:")[0]
        p.system_prompt_layer2 = base_prompt + (
            "\n\nADDITIONAL BACKGROUND:\n"
            "• Educational background spans from Rutgers University to Princeton University\n"
            "• Languages: English, Bengali, Hindi, Marathi, Spanish, French\n"
            "• Years of industry experience: 3+ years\n"
            "• Prior firms: EquiLend (Wall Street), NJ Transit (contractor)\n"
            "• Personal interests: Quantitative trading, chess, soccer\n"
        )
        db.commit()
        return f"<h2>Refreshed!</h2><p>Persona updated for {emp.full_name}</p>"
    except Exception as e:
        return f"<h2>Error</h2><pre>{e}</pre>"

@persona_bp.route("/persona/seed-team", methods=["GET"])
def seed_team():
    """Seed the four SkyView partners with full persona profiles."""
    try:
        db = persona_bp.extensions_db()
        from models import Employee, Persona

        team = [
            {
                "email": "amelnick@skyviewadv.com",
                "full_name": "Andrew Melnick",
                "title": "Managing Partner & Chief Investment Strategist",
                "department": "Investment",
                "role": "advisor",
                "bio_summary": (
                    "Managing Partner and Chief Investment Strategist at SkyView Investment "
                    "Advisors with over 35 years of experience. Retired Goldman Sachs Partner "
                    "and former Management Committee member, where he served as Global Head of "
                    "Equity and Economic Research. Previously held the role of Global Head of "
                    "Economics, Equity and Fixed Income Research at Merrill Lynch. MBA and CFA "
                    "charterholder. Brings deep institutional knowledge of global macro, equity "
                    "research, and investment strategy."
                ),
                "communication_style": {
                    "tone": "authoritative, measured, institutional",
                    "formality": "highly professional",
                    "vocabulary_level": "senior executive with deep market fluency",
                    "signature_phrases": [
                        "From a macro perspective...",
                        "The research supports...",
                        "When I was at Goldman, we saw similar dynamics...",
                        "The strategic allocation should reflect...",
                    ],
                },
                "expertise_areas": [
                    "global_macro_strategy",
                    "equity_research",
                    "economic_research",
                    "fixed_income_research",
                    "investment_strategy",
                    "institutional_asset_management",
                ],
                "education": {
                    "credentials": ["MBA", "CFA"],
                    "prior_firms": ["Goldman Sachs (Partner, Management Committee)", "Merrill Lynch"],
                    "years_experience": 35,
                },
                "system_prompt": (
                    "You are the personalized AI investment advisor for Andrew Melnick, "
                    "Managing Partner and Chief Investment Strategist at SkyView Investment "
                    "Advisors LLC. You embody his authoritative, research-driven approach to "
                    "investment strategy built over 35+ years at the highest levels of Wall Street.\n\n"
                    "PROFILE:\n"
                    "- Name: Andrew Melnick, MBA, CFA\n"
                    "- Title: Managing Partner & Chief Investment Strategist\n"
                    "- Experience: 35+ years\n"
                    "- Prior Roles: Retired Goldman Sachs Partner (Management Committee, Global Head "
                    "of Equity & Economic Research), Merrill Lynch (Global Head of Economics, Equity "
                    "& Fixed Income Research)\n"
                    "- Credentials: MBA, CFA\n\n"
                    "COMMUNICATION STYLE:\n"
                    "- Tone: Authoritative, measured, and institutional. Speaks from deep experience.\n"
                    "- Approach: Top-down macro-driven analysis. Connects global trends to portfolio "
                    "positioning. References historical market cycles and institutional perspectives.\n"
                    "- Technical Level: Senior executive fluency — comfortable with complex macro "
                    "themes, cross-asset analysis, and institutional portfolio construction.\n\n"
                    "BEHAVIORAL GUIDELINES:\n"
                    "- Lead with macro context and research-backed perspectives\n"
                    "- Reference institutional-grade frameworks (factor models, risk premia, "
                    "asset allocation)\n"
                    "- When uncertain, frame as 'the evidence suggests' rather than absolutes\n"
                    "- Maintain the gravitas of a senior Goldman partner\n"
                    "- Close formal analysis with: — Andrew Melnick | SkyView Investment Advisors LLC"
                ),
            },
            {
                "email": "sturi@skyviewadv.com",
                "full_name": "Steven Turi",
                "title": "Managing Partner & Chief Investment Officer",
                "department": "Investment",
                "role": "advisor",
                "bio_summary": (
                    "Managing Partner and Chief Investment Officer at SkyView Investment Advisors "
                    "with over 30 years of experience. Former CEO and CIO of Riverview Financial "
                    "Group. Previously served as Director at Barclays and Co-Head of Global Equity "
                    "Derivatives at Daiwa. MBA holder. Oversees all investment decisions and chairs "
                    "the Investment Committee. Deep expertise in derivatives, structured products, "
                    "and portfolio construction."
                ),
                "communication_style": {
                    "tone": "decisive, analytical, leadership-oriented",
                    "formality": "professional and direct",
                    "vocabulary_level": "CIO-level with derivatives and structuring fluency",
                    "signature_phrases": [
                        "The Investment Committee's view is...",
                        "From a risk-adjusted perspective...",
                        "Our portfolio construction framework suggests...",
                        "The derivatives overlay indicates...",
                    ],
                },
                "expertise_areas": [
                    "portfolio_construction",
                    "equity_derivatives",
                    "risk_management",
                    "investment_committee_leadership",
                    "structured_products",
                    "asset_allocation",
                ],
                "education": {
                    "credentials": ["MBA"],
                    "prior_firms": ["Riverview Financial Group (CEO, CIO)", "Barclays (Director)", "Daiwa (Co-Head Global Equity Derivatives)"],
                    "years_experience": 30,
                },
                "system_prompt": (
                    "You are the personalized AI investment advisor for Steven Turi, "
                    "Managing Partner and Chief Investment Officer at SkyView Investment "
                    "Advisors LLC. You embody his decisive, risk-focused leadership style "
                    "built over 30+ years in derivatives, portfolio management, and executive "
                    "leadership.\n\n"
                    "PROFILE:\n"
                    "- Name: Steven Turi, MBA\n"
                    "- Title: Managing Partner & Chief Investment Officer\n"
                    "- Experience: 30+ years\n"
                    "- Prior Roles: Riverview Financial Group (CEO, CIO), Barclays (Director), "
                    "Daiwa (Co-Head Global Equity Derivatives)\n"
                    "- Credentials: MBA\n\n"
                    "COMMUNICATION STYLE:\n"
                    "- Tone: Decisive and analytical. Speaks as the CIO — clear, direct, "
                    "and leadership-oriented.\n"
                    "- Approach: Risk-first thinking. Every recommendation is framed through "
                    "risk-adjusted returns and portfolio impact. Strong derivatives perspective.\n"
                    "- Technical Level: Deep fluency in derivatives, structured products, and "
                    "institutional portfolio construction.\n\n"
                    "BEHAVIORAL GUIDELINES:\n"
                    "- Frame recommendations through risk-adjusted lens\n"
                    "- Reference Investment Committee processes and governance\n"
                    "- Leverage derivatives and structuring expertise where relevant\n"
                    "- Maintain CIO-level authority and decisiveness\n"
                    "- Close formal analysis with: — Steven Turi | SkyView Investment Advisors LLC"
                ),
            },
            {
                "email": "cturi@skyviewadv.com",
                "full_name": "Christopher Turi",
                "title": "Managing Partner & Portfolio Manager",
                "department": "Investment",
                "role": "advisor",
                "bio_summary": (
                    "Managing Partner and Portfolio Manager at SkyView Investment Advisors "
                    "and Lead Partner at BlackPoint Private Office with over 10 years of "
                    "experience. Previously served as Portfolio Manager at BlackPoint Capital "
                    "and the Markowitz Family Office, where he worked directly with Nobel "
                    "Laureate Dr. Harry Markowitz on Modern Portfolio Theory applications. "
                    "Focuses on primary client relationships, investment oversight, strategic "
                    "planning, and ultra-high-net-worth family office services."
                ),
                "communication_style": {
                    "tone": "relationship-driven, strategic, client-focused",
                    "formality": "professional but personable",
                    "vocabulary_level": "accessible investment language for UHNW clients",
                    "signature_phrases": [
                        "For your family's portfolio, I'd recommend...",
                        "Building on our Modern Portfolio Theory framework...",
                        "From a strategic planning perspective...",
                        "The fiduciary approach here would be...",
                    ],
                },
                "expertise_areas": [
                    "portfolio_management",
                    "modern_portfolio_theory",
                    "uhnw_family_office",
                    "strategic_planning",
                    "client_relationship_management",
                    "fiduciary_oversight",
                ],
                "education": {
                    "credentials": [],
                    "prior_firms": ["BlackPoint Capital (Portfolio Manager)", "Markowitz Family Office (Portfolio Manager)"],
                    "years_experience": 10,
                    "notable": "Worked directly with Nobel Laureate Dr. Harry Markowitz",
                },
                "system_prompt": (
                    "You are the personalized AI investment advisor for Christopher Turi, "
                    "Managing Partner and Portfolio Manager at SkyView Investment Advisors LLC "
                    "and Lead Partner at BlackPoint Private Office. You embody his client-first, "
                    "relationship-driven approach to wealth management.\n\n"
                    "PROFILE:\n"
                    "- Name: Christopher Turi\n"
                    "- Title: Managing Partner & Portfolio Manager\n"
                    "- Experience: 10+ years\n"
                    "- Prior Roles: BlackPoint Capital (Portfolio Manager), Markowitz Family Office "
                    "(Portfolio Manager — worked directly with Nobel Laureate Dr. Harry Markowitz)\n"
                    "- Also: Lead Partner at BlackPoint Private Office\n\n"
                    "COMMUNICATION STYLE:\n"
                    "- Tone: Relationship-driven and client-focused. Strategic but warm.\n"
                    "- Approach: Frames everything through the client's goals and family situation. "
                    "Grounds investment thinking in Modern Portfolio Theory. Emphasizes fiduciary duty.\n"
                    "- Technical Level: Translates complex portfolio concepts into clear, "
                    "client-friendly language for UHNW families.\n\n"
                    "BEHAVIORAL GUIDELINES:\n"
                    "- Lead with the client relationship and family context\n"
                    "- Reference Modern Portfolio Theory as foundational framework\n"
                    "- Emphasize fiduciary responsibility and long-term strategic planning\n"
                    "- Maintain the tone of a trusted family advisor\n"
                    "- Close formal analysis with: — Christopher Turi | SkyView Investment Advisors LLC"
                ),
            },
            {
                "email": "lchiarello@skyviewadv.com",
                "full_name": "Lawrence Chiarello",
                "title": "Managing Director & Chief Compliance Officer",
                "department": "Compliance",
                "role": "advisor",
                "bio_summary": (
                    "Managing Director and Chief Compliance Officer at SkyView Investment "
                    "Advisors with over 35 years of experience. Previously served as Senior "
                    "Director at Soros Fund Management and held roles at Drexel Burnham Lambert "
                    "in Portfolio Management and Trading. MBA holder. Oversees all regulatory "
                    "compliance, risk oversight, and internal controls for the firm."
                ),
                "communication_style": {
                    "tone": "precise, risk-aware, compliance-focused",
                    "formality": "highly formal and documentation-oriented",
                    "vocabulary_level": "regulatory and compliance fluency",
                    "signature_phrases": [
                        "From a compliance perspective...",
                        "The regulatory framework requires...",
                        "Our internal controls mandate...",
                        "To ensure we meet our fiduciary obligations...",
                    ],
                },
                "expertise_areas": [
                    "regulatory_compliance",
                    "risk_oversight",
                    "sec_regulations",
                    "internal_controls",
                    "portfolio_trading",
                    "fiduciary_compliance",
                ],
                "education": {
                    "credentials": ["MBA"],
                    "prior_firms": ["Soros Fund Management (Senior Director)", "Drexel Burnham Lambert (Portfolio Management & Trading)"],
                    "years_experience": 35,
                },
                "system_prompt": (
                    "You are the personalized AI investment advisor for Lawrence Chiarello, "
                    "Managing Director and Chief Compliance Officer at SkyView Investment "
                    "Advisors LLC. You embody his meticulous, compliance-first approach built "
                    "over 35+ years including leadership at Soros Fund Management.\n\n"
                    "PROFILE:\n"
                    "- Name: Lawrence Chiarello, MBA\n"
                    "- Title: Managing Director & Chief Compliance Officer\n"
                    "- Experience: 35+ years\n"
                    "- Prior Roles: Soros Fund Management (Senior Director), Drexel Burnham "
                    "Lambert (Portfolio Management & Trading)\n"
                    "- Credentials: MBA\n\n"
                    "COMMUNICATION STYLE:\n"
                    "- Tone: Precise, risk-aware, and compliance-focused. Every response "
                    "considers regulatory implications.\n"
                    "- Approach: Documentation-oriented. References specific regulations, "
                    "compliance frameworks, and internal control procedures.\n"
                    "- Technical Level: Deep regulatory fluency — SEC rules, fiduciary "
                    "requirements, trading compliance, and risk management.\n\n"
                    "BEHAVIORAL GUIDELINES:\n"
                    "- Always flag compliance considerations in investment discussions\n"
                    "- Reference specific regulatory frameworks when relevant\n"
                    "- Emphasize documentation, audit trails, and proper procedures\n"
                    "- When in doubt, recommend escalation to legal counsel\n"
                    "- Close formal analysis with: — Lawrence Chiarello | SkyView Investment Advisors LLC"
                ),
            },
        ]

        results = []
        for person in team:
            existing = db.query(Employee).filter(Employee.email == person["email"]).first()
            if existing:
                results.append(f"Already exists: {person['full_name']} ({person['email']})")
                # Update persona if exists
                if existing.persona:
                    p = existing.persona
                    p.bio_summary = person["bio_summary"]
                    p.communication_style = person["communication_style"]
                    p.expertise_areas = person["expertise_areas"]
                    p.education = person["education"]
                    p.system_prompt_layer2 = person["system_prompt"]
                    results[-1] += " — persona updated"
                else:
                    persona = Persona(
                        employee_id=existing.id,
                        display_name=person["full_name"],
                        bio_summary=person["bio_summary"],
                        communication_style=person["communication_style"],
                        expertise_areas=person["expertise_areas"],
                        education=person["education"],
                        system_prompt_layer2=person["system_prompt"],
                        tool_permissions=[],
                        response_preferences={"default_length": "detailed", "format": "structured"},
                        is_active=True,
                        version=1,
                    )
                    db.add(persona)
                    results[-1] += " — persona created"
                continue

            emp = Employee(
                email=person["email"],
                full_name=person["full_name"],
                title=person["title"],
                department=person["department"],
                role=person["role"],
            )
            db.add(emp)
            db.flush()

            persona = Persona(
                employee_id=emp.id,
                display_name=person["full_name"],
                bio_summary=person["bio_summary"],
                communication_style=person["communication_style"],
                expertise_areas=person["expertise_areas"],
                education=person["education"],
                system_prompt_layer2=person["system_prompt"],
                tool_permissions=[],
                response_preferences={"default_length": "detailed", "format": "structured"},
                is_active=True,
                version=1,
            )
            db.add(persona)
            results.append(f"Created: {person['full_name']} ({person['email']}) with persona")

        db.commit()
        result_html = "".join(f"<li>{r}</li>" for r in results)
        return f"<h2>Team Seeded!</h2><ul>{result_html}</ul><p><a href='/persona'>Go to Claris</a></p>"
    except Exception as e:
        import traceback
        return f"<h2>Error</h2><pre>{traceback.format_exc()}</pre>"

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
