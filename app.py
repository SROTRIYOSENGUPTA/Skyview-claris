"""
SkyView Investment Advisors LLC
Claris — Production Flask Server v4.0

Security hardening:
  - Rate limiting  (Flask-Limiter)
  - Security headers (Flask-Talisman: HSTS, CSP, X-Frame-Options, etc.)
  - Input validation: message length, file size, attachment count
  - No stack traces exposed to clients
  - Secure session cookies (HttpOnly, SameSite, Secure in production)
  - Single gunicorn worker + threads (avoids shared-memory session issues)

Roles:
  GET  /          → client-facing experience (clean Q&A portal)
  GET  /advisor   → internal advisor tool (full capabilities)

Endpoints:
  GET  /health            — Health check (load-balancer probe)
  GET  /session           — Session metadata (JSON)
  POST /chat              — Blocking chat (fallback)
  POST /chat/stream       — Streaming SSE chat (primary)
  POST /set-client-type   — Update client profile
  POST /reset             — Reset conversation
  POST /save              — Save conversation JSON
"""

import os
import uuid
import logging
from datetime import timedelta

from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman

from chatbot import SkyViewChatbot, MODEL, MAX_TOKENS, build_content_blocks, execute_tool, TOOLS

# ── App setup ──────────────────────────────────────────────────────────────────
app = Flask(__name__)

IS_PRODUCTION = os.environ.get("FLASK_ENV", "development") == "production"

app.config.update(
    SECRET_KEY                 = os.environ.get("FLASK_SECRET_KEY", os.urandom(32)),
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8),
    SESSION_COOKIE_HTTPONLY    = True,
    SESSION_COOKIE_SAMESITE    = "Lax",
    SESSION_COOKIE_SECURE      = IS_PRODUCTION,   # HTTPS-only in prod
    MAX_CONTENT_LENGTH         = 20 * 1024 * 1024, # 20 MB max request body
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("skyview.app")

# ── Rate limiting ──────────────────────────────────────────────────────────────
limiter = Limiter(
    key_func        = get_remote_address,
    app             = app,
    default_limits  = ["300 per hour", "60 per minute"],
    storage_uri     = "memory://",
)

# ── Security headers ───────────────────────────────────────────────────────────
# CSP allows CDN fonts, marked.js, and SkyView image assets; blocks everything else.
CSP = {
    "default-src": "'self'",
    "script-src": [
        "'self'",
        "https://cdnjs.cloudflare.com",
        "'unsafe-inline'",           # needed for inline JS in the single-file template
    ],
    "style-src": [
        "'self'",
        "https://fonts.googleapis.com",
        "'unsafe-inline'",
    ],
    "font-src": [
        "'self'",
        "https://fonts.gstatic.com",
    ],
    "img-src": [
        "'self'",
        "data:",
        "https://skyviewadv.com",
        "https://*.skyviewadv.com",
    ],
    "connect-src": "'self'",
    "frame-ancestors": "'none'",
    "object-src": "'none'",
}

Talisman(
    app,
    force_https              = IS_PRODUCTION,
    strict_transport_security= IS_PRODUCTION,
    content_security_policy  = CSP,
    x_content_type_options   = True,
    x_xss_protection         = True,
    referrer_policy          = "strict-origin-when-cross-origin",
)

# ── Validation constants ───────────────────────────────────────────────────────
MAX_MESSAGE_LEN   = 10_000   # characters
MAX_ATTACHMENTS   = 5
MAX_ATTACH_BYTES  = 10 * 1024 * 1024   # 10 MB per file (base64 decoded)
ALLOWED_ROLES     = {"advisor", "client"}

# ── Session store (single-worker; use Redis for multi-worker scale-out) ────────
_sessions: dict[str, SkyViewChatbot] = {}


def _get_bot(role: str = "client") -> SkyViewChatbot:
    session.permanent = True
    sid = session.get("sid")
    if not sid or sid not in _sessions:
        sid = str(uuid.uuid4())[:8]
        session["sid"] = sid
        _sessions[sid] = SkyViewChatbot(session_id=sid, role=role)
        logger.info(f"New session: {sid} | role={role}")
    return _sessions[sid]


def _check_api_key() -> bool:
    """Return True if the Anthropic API key is configured."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _validate_chat_input(msg: str, attachments: list) -> str | None:
    """Return an error string if input is invalid, else None."""
    if len(msg) > MAX_MESSAGE_LEN:
        return f"Message too long (max {MAX_MESSAGE_LEN:,} characters)."
    if len(attachments) > MAX_ATTACHMENTS:
        return f"Too many attachments (max {MAX_ATTACHMENTS})."
    for att in attachments:
        data = att.get("data", "")
        # base64 → ~75 % of original; rough size check
        if len(data) * 0.75 > MAX_ATTACH_BYTES:
            return f"File '{att.get('name', 'file')}' exceeds the 10 MB limit."
    return None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index_client():
    """Client-facing portal — clean Q&A experience."""
    # Don't eagerly create the chatbot session on page load;
    # it will be created lazily on the first chat request.
    return render_template("index.html", role="client")


@app.route("/advisor")
def index_advisor():
    """
    Internal advisor tool — full capabilities.
    In production, place this behind your VPN or IP allowlist.
    """
    return render_template("index.html", role="advisor")


@app.route("/health")
@limiter.exempt
def health():
    api_key_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return jsonify({
        "status":           "ok",
        "service":          "SkyView Claris v4.1",
        "sessions":         len(_sessions),
        "api_key_loaded":   api_key_ok,
    })


@app.route("/session", methods=["GET"])
def get_session():
    if not _check_api_key():
        return jsonify({"error": "Service is not configured."}), 503
    return jsonify(_get_bot().get_summary())


@app.route("/set-client-type", methods=["POST"])
@limiter.limit("20 per minute")
def set_client_type():
    if not _check_api_key():
        return jsonify({"error": "Service is not configured."}), 503
    data = request.get_json() or {}
    ct   = data.get("client_type", "general")
    if ct not in {"family_office", "institution", "wealth_manager", "general"}:
        return jsonify({"error": "Invalid client_type"}), 400
    _get_bot().set_client_type(ct)
    return jsonify({"status": "ok", "client_type": ct})


@app.route("/chat", methods=["POST"])
@limiter.limit("30 per minute")
def chat():
    """Blocking fallback endpoint (used when EventSource is unavailable)."""
    if not _check_api_key():
        return jsonify({"error": "Service is not configured. Please contact SkyView support."}), 503

    data        = request.get_json() or {}
    msg         = data.get("message", "").strip()
    attachments = data.get("attachments", [])

    if not msg and not attachments:
        return jsonify({"error": "Message or attachment required."}), 400
    if not msg:
        msg = "Please analyse the attached file(s)."

    err = _validate_chat_input(msg, attachments)
    if err:
        return jsonify({"error": err}), 400

    bot = _get_bot()
    try:
        result = bot.chat(msg, attachments or None)
        return jsonify({
            "response":    result["text"],
            "session_id":  result["session_id"],
            "tokens_used": result["tokens_used"],
            "tools_used":  result["tools_used"],
        })
    except Exception as e:
        logger.exception("Error in /chat")
        return jsonify({"error": "An internal error occurred. Please try again."}), 500


@app.route("/chat/stream", methods=["POST"])
@limiter.limit("30 per minute")
def chat_stream():
    """
    Primary chat endpoint — Server-Sent Events streaming.
    Supports attachments and the full tool-use agentic loop.
    Text tokens are flushed to the browser as soon as they arrive.
    """
    import json as _json

    if not _check_api_key():
        return jsonify({"error": "Service is not configured. Please contact SkyView support."}), 503

    data        = request.get_json() or {}
    msg         = data.get("message", "").strip()
    attachments = data.get("attachments", [])

    if not msg and not attachments:
        return jsonify({"error": "Message or attachment required."}), 400
    if not msg:
        msg = "Please analyse the attached file(s)."

    err = _validate_chat_input(msg, attachments)
    if err:
        return jsonify({"error": err}), 400

    bot = _get_bot()

    def generate():
        try:
            bot._trim_history()
            bot.message_count += 1

            content = build_content_blocks(msg, attachments or None)
            bot.history.append({"role": "user", "content": content})

            # ── Tool-use agentic loop (non-streaming until tools complete) ────
            tools_used = []
            response = bot.client.messages.create(
                model      = MODEL,
                max_tokens = MAX_TOKENS,
                system     = bot._build_system_prompt(),
                tools      = TOOLS,
                messages   = bot.history,
            )

            while response.stop_reason == "tool_use":
                bot.history.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tools_used.append(block.name)
                        yield f"data: {_json.dumps({'tool': block.name})}\n\n"
                        result = execute_tool(block.name, block.input)
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,
                            "content":     result,
                        })
                bot.history.append({"role": "user", "content": tool_results})
                response = bot.client.messages.create(
                    model      = MODEL,
                    max_tokens = MAX_TOKENS,
                    system     = bot._build_system_prompt(),
                    tools      = TOOLS,
                    messages   = bot.history,
                )

            # ── Stream the final text to the browser in small chunks ──────────
            final_text = "".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            bot.history.append({"role": "assistant", "content": final_text})

            CHUNK = 4
            for i in range(0, len(final_text), CHUNK):
                yield f"data: {_json.dumps({'token': final_text[i:i+CHUNK]})}\n\n"

            yield f"data: {_json.dumps({'done': True, 'tools_used': tools_used})}\n\n"

        except Exception as e:
            logger.exception("Stream error")
            yield f"data: {_json.dumps({'error': 'An internal error occurred. Please try again.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype = "text/event-stream",
        headers  = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/reset", methods=["POST"])
@limiter.limit("10 per minute")
def reset():
    if not _check_api_key():
        return jsonify({"error": "Service is not configured."}), 503
    bot = _get_bot()
    bot.reset()
    return jsonify({"status": "reset", "session_id": bot.session_id})


@app.route("/save", methods=["POST"])
@limiter.limit("10 per minute")
def save():
    try:
        path = _get_bot().save()
        return jsonify({"status": "saved", "file": path})
    except Exception as e:
        logger.exception("Save error")
        return jsonify({"error": "Could not save conversation."}), 500


# ── Error handlers (never expose stack traces) ─────────────────────────────────

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Request too large. Maximum upload size is 20 MB."}), 413

@app.errorhandler(429)
def rate_limited(e):
    return jsonify({"error": "Too many requests. Please wait a moment and try again."}), 429

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "An internal error occurred."}), 500


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("\n⚠️  Set your API key first:")
        print("   Windows:  $env:ANTHROPIC_API_KEY = 'sk-ant-...'")
        print("   Mac/Linux: export ANTHROPIC_API_KEY='sk-ant-...'\n")

    print("\n" + "═" * 60)
    print("  SKYVIEW INVESTMENT ADVISORS LLC — Claris v4.0")
    print("  Client portal  → http://127.0.0.1:5000/")
    print("  Advisor tool   → http://127.0.0.1:5000/advisor")
    print("═" * 60 + "\n")

    # Dev only — production runs via gunicorn (see Procfile)
    app.run(debug=False, port=5000, host="127.0.0.1")
