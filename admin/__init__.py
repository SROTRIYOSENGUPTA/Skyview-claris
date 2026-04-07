"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Admin Blueprint

Provides persona management, knowledge content upload, compliance dashboard,
and analytics for platform administrators.
"""

import json
import logging
import os
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Blueprint, render_template, request, jsonify, redirect, url_for,
    flash, session, current_app,
)
from sqlalchemy import func, desc
from sqlalchemy.orm import Session as DBSession

from models import (
    Employee, Persona, PersonaVersion, KnowledgeDocument,
    KnowledgeChunk, Conversation, ComplianceLog,
)

logger = logging.getLogger("skyview.admin")

admin_bp = Blueprint(
    "admin",
    __name__,
    template_folder="../templates/admin",
    url_prefix="/admin",
)


# ─────────────────────────────────────────────────────────────────────────────
# AUTH DECORATOR
# ─────────────────────────────────────────────────────────────────────────────

def admin_required(f):
    """Require admin role for access."""
    @wraps(f)
    def decorated(*args, **kwargs):
        db = current_app.extensions.get("db_session")
        if not db:
            return jsonify({"error": "Database not configured"}), 503

        employee_id = session.get("employee_id")
        if not employee_id:
            return redirect(url_for("persona_login"))

        employee = db.query(Employee).filter(
            Employee.id == employee_id,
            Employee.is_active == True,
        ).first()

        if not employee or not employee.is_admin:
            return jsonify({"error": "Admin access required"}), 403

        return f(*args, db=db, admin=employee, **kwargs)
    return decorated


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/")
@admin_required
def dashboard(db: DBSession, admin: Employee):
    """Admin dashboard overview."""
    stats = {
        "total_employees": db.query(Employee).filter(Employee.is_active == True).count(),
        "active_personas": db.query(Persona).filter(Persona.is_active == True).count(),
        "total_conversations": db.query(Conversation).count(),
        "knowledge_documents": db.query(KnowledgeDocument).filter(
            KnowledgeDocument.is_active == True
        ).count(),
        "pending_documents": db.query(KnowledgeDocument).filter(
            KnowledgeDocument.status == "pending"
        ).count(),
        "compliance_flags_30d": db.query(ComplianceLog).filter(
            ComplianceLog.created_at >= func.now() - func.cast("30 days", type_=None)
        ).count() if False else 0,  # Simplified for MVP
    }

    recent_conversations = db.query(Conversation).order_by(
        desc(Conversation.started_at)
    ).limit(10).all()

    return render_template(
        "dashboard.html",
        stats=stats,
        recent_conversations=recent_conversations,
        admin=admin,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PERSONA MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/personas")
@admin_required
def list_personas(db: DBSession, admin: Employee):
    """List all personas."""
    personas = db.query(Persona).join(Employee).order_by(Employee.full_name).all()
    return render_template("personas.html", personas=personas, admin=admin)


@admin_bp.route("/personas/new", methods=["GET", "POST"])
@admin_required
def create_persona(db: DBSession, admin: Employee):
    """Create a new persona."""
    if request.method == "GET":
        employees = db.query(Employee).filter(
            Employee.is_active == True,
            ~Employee.id.in_(
                db.query(Persona.employee_id).filter(Persona.employee_id.isnot(None))
            ),
        ).order_by(Employee.full_name).all()
        return render_template("persona_form.html", employees=employees, admin=admin)

    # POST: Create persona
    data = request.form
    employee_id = data.get("employee_id")
    if not employee_id:
        return jsonify({"error": "Employee ID required"}), 400

    employee = db.query(Employee).filter(Employee.id == employee_id).first()
    if not employee:
        return jsonify({"error": "Employee not found"}), 404

    # Parse JSON fields
    communication_style = {}
    try:
        communication_style = json.loads(data.get("communication_style", "{}"))
    except json.JSONDecodeError:
        pass

    expertise_areas = []
    try:
        expertise_areas = json.loads(data.get("expertise_areas", "[]"))
    except json.JSONDecodeError:
        raw = data.get("expertise_areas", "")
        expertise_areas = [a.strip() for a in raw.split(",") if a.strip()]

    education = {}
    try:
        education = json.loads(data.get("education", "{}"))
    except json.JSONDecodeError:
        pass

    tool_permissions = []
    try:
        tool_permissions = json.loads(data.get("tool_permissions", "[]"))
    except json.JSONDecodeError:
        pass

    persona = Persona(
        employee_id=employee_id,
        display_name=data.get("display_name", employee.full_name),
        bio_summary=data.get("bio_summary", ""),
        communication_style=communication_style,
        expertise_areas=expertise_areas,
        education=education,
        system_prompt_layer2=data.get("system_prompt_layer2", ""),
        tool_permissions=tool_permissions,
        response_preferences=json.loads(data.get("response_preferences", "{}")),
        is_active=True,
        version=1,
    )
    db.add(persona)

    # Create initial version record
    version = PersonaVersion(
        persona_id=persona.id,
        version=1,
        system_prompt_layer2=persona.system_prompt_layer2,
        communication_style=persona.communication_style,
        expertise_areas=persona.expertise_areas,
        changed_by=admin.id,
        change_note="Initial persona creation",
    )
    db.add(version)
    db.commit()

    logger.info(f"Persona created: {persona.display_name} by {admin.full_name}")
    return redirect(url_for("admin.list_personas"))


@admin_bp.route("/personas/<persona_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_persona(persona_id, db: DBSession, admin: Employee):
    """Edit an existing persona."""
    persona = db.query(Persona).filter(Persona.id == persona_id).first()
    if not persona:
        return jsonify({"error": "Persona not found"}), 404

    if request.method == "GET":
        return render_template("persona_form.html", persona=persona, admin=admin)

    # POST: Update persona
    data = request.form
    old_prompt = persona.system_prompt_layer2

    persona.display_name = data.get("display_name", persona.display_name)
    persona.bio_summary = data.get("bio_summary", persona.bio_summary)
    persona.system_prompt_layer2 = data.get("system_prompt_layer2", persona.system_prompt_layer2)

    try:
        persona.communication_style = json.loads(data.get("communication_style", "{}"))
    except json.JSONDecodeError:
        pass

    try:
        raw_expertise = data.get("expertise_areas", "[]")
        persona.expertise_areas = json.loads(raw_expertise)
    except json.JSONDecodeError:
        persona.expertise_areas = [a.strip() for a in raw_expertise.split(",") if a.strip()]

    try:
        persona.education = json.loads(data.get("education", "{}"))
    except json.JSONDecodeError:
        pass

    try:
        persona.tool_permissions = json.loads(data.get("tool_permissions", "[]"))
    except json.JSONDecodeError:
        pass

    try:
        persona.response_preferences = json.loads(data.get("response_preferences", "{}"))
    except json.JSONDecodeError:
        pass

    # Bump version if prompt changed
    if persona.system_prompt_layer2 != old_prompt:
        persona.version += 1
        version = PersonaVersion(
            persona_id=persona.id,
            version=persona.version,
            system_prompt_layer2=persona.system_prompt_layer2,
            communication_style=persona.communication_style,
            expertise_areas=persona.expertise_areas,
            changed_by=admin.id,
            change_note=data.get("change_note", "Updated via admin"),
        )
        db.add(version)

    db.commit()
    logger.info(f"Persona updated: {persona.display_name} v{persona.version} by {admin.full_name}")
    return redirect(url_for("admin.list_personas"))


@admin_bp.route("/personas/<persona_id>/toggle", methods=["POST"])
@admin_required
def toggle_persona(persona_id, db: DBSession, admin: Employee):
    """Activate/deactivate a persona."""
    persona = db.query(Persona).filter(Persona.id == persona_id).first()
    if not persona:
        return jsonify({"error": "Persona not found"}), 404

    persona.is_active = not persona.is_active
    db.commit()

    status = "activated" if persona.is_active else "deactivated"
    logger.info(f"Persona {status}: {persona.display_name} by {admin.full_name}")
    return jsonify({"status": status, "persona": persona.display_name})


@admin_bp.route("/personas/<persona_id>/test", methods=["GET"])
@admin_required
def test_persona(persona_id, db: DBSession, admin: Employee):
    """Test mode: chat with a persona without affecting production data."""
    persona = db.query(Persona).filter(Persona.id == persona_id).first()
    if not persona:
        return jsonify({"error": "Persona not found"}), 404
    return render_template("persona_test.html", persona=persona, admin=admin)


# ─────────────────────────────────────────────────────────────────────────────
# KNOWLEDGE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/knowledge")
@admin_required
def list_documents(db: DBSession, admin: Employee):
    """List all knowledge documents."""
    documents = db.query(KnowledgeDocument).order_by(
        desc(KnowledgeDocument.created_at)
    ).all()
    return render_template("knowledge.html", documents=documents, admin=admin)


@admin_bp.route("/knowledge/upload", methods=["POST"])
@admin_required
def upload_document(db: DBSession, admin: Employee):
    """Upload a new document for the knowledge base."""
    from knowledge import extract_text_from_file, ingest_document

    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    title = request.form.get("title", file.filename)
    category = request.form.get("category", "general")

    # Save uploaded file temporarily
    upload_dir = os.path.join(current_app.instance_path, "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, file.filename)
    file.save(file_path)

    try:
        file_ext = file.filename.rsplit(".", 1)[-1].lower()
        content_text = extract_text_from_file(file_path, file_ext)

        doc = ingest_document(
            db=db,
            title=title,
            content_text=content_text,
            category=category,
            source_file=file.filename,
            uploaded_by=str(admin.id),
        )

        logger.info(f"Document uploaded: {title} by {admin.full_name}")
        return jsonify({
            "status": "uploaded",
            "document_id": str(doc.id),
            "title": doc.title,
            "chunks": len(doc.chunks),
        })

    except Exception as e:
        logger.error(f"Document upload failed: {e}")
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500
    finally:
        # Clean up temp file
        if os.path.exists(file_path):
            os.remove(file_path)


@admin_bp.route("/knowledge/<doc_id>/approve", methods=["POST"])
@admin_required
def approve_document(doc_id, db: DBSession, admin: Employee):
    """Approve a pending document for use in RAG."""
    doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()
    if not doc:
        return jsonify({"error": "Document not found"}), 404

    doc.status = "approved"
    doc.approved_by = admin.id
    doc.approved_at = datetime.now(timezone.utc)
    db.commit()

    logger.info(f"Document approved: {doc.title} by {admin.full_name}")
    return jsonify({"status": "approved", "title": doc.title})


@admin_bp.route("/knowledge/<doc_id>/reject", methods=["POST"])
@admin_required
def reject_document(doc_id, db: DBSession, admin: Employee):
    """Reject a pending document."""
    doc = db.query(KnowledgeDocument).filter(KnowledgeDocument.id == doc_id).first()
    if not doc:
        return jsonify({"error": "Document not found"}), 404

    doc.status = "rejected"
    db.commit()

    logger.info(f"Document rejected: {doc.title} by {admin.full_name}")
    return jsonify({"status": "rejected", "title": doc.title})


# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/compliance")
@admin_required
def compliance_dashboard(db: DBSession, admin: Employee):
    """Compliance overview and flag browser."""
    from compliance import get_compliance_summary

    summary = get_compliance_summary(db, days=30)
    recent_flags = db.query(ComplianceLog).order_by(
        desc(ComplianceLog.created_at)
    ).limit(50).all()

    return render_template(
        "compliance.html",
        summary=summary,
        recent_flags=recent_flags,
        admin=admin,
    )


@admin_bp.route("/compliance/<flag_id>/resolve", methods=["POST"])
@admin_required
def resolve_flag(flag_id, db: DBSession, admin: Employee):
    """Mark a compliance flag as resolved."""
    flag = db.query(ComplianceLog).filter(ComplianceLog.id == flag_id).first()
    if not flag:
        return jsonify({"error": "Flag not found"}), 404

    flag.resolved = True
    flag.resolved_by = admin.id
    flag.resolved_at = datetime.now(timezone.utc)
    db.commit()

    return jsonify({"status": "resolved"})


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATIONS
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/conversations")
@admin_required
def list_conversations(db: DBSession, admin: Employee):
    """Browse all conversations for audit purposes."""
    page = request.args.get("page", 1, type=int)
    per_page = 25

    conversations = db.query(Conversation).order_by(
        desc(Conversation.started_at)
    ).offset((page - 1) * per_page).limit(per_page).all()

    total = db.query(Conversation).count()

    return render_template(
        "conversations.html",
        conversations=conversations,
        page=page,
        per_page=per_page,
        total=total,
        admin=admin,
    )


@admin_bp.route("/conversations/<conv_id>")
@admin_required
def view_conversation(conv_id, db: DBSession, admin: Employee):
    """View a single conversation with full message history."""
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        return jsonify({"error": "Conversation not found"}), 404

    flags = db.query(ComplianceLog).filter(
        ComplianceLog.conversation_id == conv_id
    ).order_by(ComplianceLog.created_at).all()

    return render_template(
        "conversation_detail.html",
        conversation=conv,
        flags=flags,
        admin=admin,
    )


# ─────────────────────────────────────────────────────────────────────────────
# EMPLOYEE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/employees")
@admin_required
def list_employees(db: DBSession, admin: Employee):
    """List all employees."""
    employees = db.query(Employee).order_by(Employee.full_name).all()
    return render_template("employees.html", employees=employees, admin=admin)


@admin_bp.route("/employees/new", methods=["POST"])
@admin_required
def create_employee(db: DBSession, admin: Employee):
    """Create a new employee record."""
    data = request.get_json() or request.form

    employee = Employee(
        email=data.get("email"),
        full_name=data.get("full_name"),
        title=data.get("title"),
        department=data.get("department"),
        role=data.get("role", "advisor"),
    )
    db.add(employee)
    db.commit()

    logger.info(f"Employee created: {employee.full_name} by {admin.full_name}")
    return jsonify({"status": "created", "employee_id": str(employee.id)})


# ─────────────────────────────────────────────────────────────────────────────
# API ENDPOINTS (for HTMX / frontend)
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/api/stats")
@admin_required
def api_stats(db: DBSession, admin: Employee):
    """Return dashboard stats as JSON."""
    return jsonify({
        "employees": db.query(Employee).filter(Employee.is_active == True).count(),
        "personas": db.query(Persona).filter(Persona.is_active == True).count(),
        "conversations": db.query(Conversation).count(),
        "documents": db.query(KnowledgeDocument).filter(
            KnowledgeDocument.is_active == True
        ).count(),
    })
