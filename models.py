"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Database Models

SQLAlchemy ORM models for employee personas, knowledge documents,
conversations, and compliance audit trails.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Text, Boolean, Integer, DateTime, ForeignKey,
    Enum as SAEnum, Index, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column
from pgvector.sqlalchemy import Vector


# ─────────────────────────────────────────────────────────────────────────────
# BASE
# ─────────────────────────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# EMPLOYEE / USER
# ─────────────────────────────────────────────────────────────────────────────

class Employee(Base):
    __tablename__ = "employees"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    full_name = Column(String(255), nullable=False)
    title = Column(String(255), nullable=True)
    department = Column(String(100), nullable=True)
    role = Column(
        SAEnum("admin", "advisor", "viewer", name="employee_role"),
        nullable=False,
        default="advisor",
    )
    azure_ad_oid = Column(String(255), nullable=True, unique=True)  # Azure AD Object ID
    is_active = Column(Boolean, default=True, nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    persona = relationship("Persona", back_populates="employee", uselist=False, lazy="joined")
    conversations = relationship("Conversation", back_populates="employee", lazy="dynamic")

    def __repr__(self):
        return f"<Employee {self.full_name} ({self.email})>"

    @property
    def is_admin(self):
        return self.role == "admin"


# ─────────────────────────────────────────────────────────────────────────────
# PERSONA
# ─────────────────────────────────────────────────────────────────────────────

class Persona(Base):
    __tablename__ = "personas"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id = Column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    display_name = Column(String(255), nullable=False)
    bio_summary = Column(Text, nullable=True)

    # Structured persona data (JSONB for flexible schema)
    communication_style = Column(JSONB, nullable=True, default=dict)
    # Example: {"tone": "warm, data-driven", "formality": "professional",
    #           "vocabulary_level": "technical", "signature_phrases": [...]}

    expertise_areas = Column(JSONB, nullable=True, default=list)
    # Example: ["quantitative_analysis", "machine_learning", "portfolio_construction"]

    education = Column(JSONB, nullable=True, default=dict)
    # Example: {"universities": ["Rutgers University", "Princeton University"],
    #           "skills": ["Computer Engineering", "Machine Learning Specialized"]}

    system_prompt_layer2 = Column(Text, nullable=False)
    # The full Layer 2 persona prompt text, loaded at runtime

    tool_permissions = Column(JSONB, nullable=True, default=list)
    # Which tools this persona can invoke. Empty = all tools.
    # Example: ["analyze_portfolio", "research_investment_strategy"]

    response_preferences = Column(JSONB, nullable=True, default=dict)
    # Example: {"default_length": "detailed", "format": "structured",
    #           "detail_level": "technical"}

    is_active = Column(Boolean, default=True, nullable=False)
    version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    employee = relationship("Employee", back_populates="persona")

    def __repr__(self):
        return f"<Persona {self.display_name} v{self.version}>"


# ─────────────────────────────────────────────────────────────────────────────
# PERSONA VERSION HISTORY (audit trail)
# ─────────────────────────────────────────────────────────────────────────────

class PersonaVersion(Base):
    __tablename__ = "persona_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("personas.id", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(Integer, nullable=False)
    system_prompt_layer2 = Column(Text, nullable=False)
    communication_style = Column(JSONB, nullable=True)
    expertise_areas = Column(JSONB, nullable=True)
    changed_by = Column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True)
    change_note = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("persona_id", "version", name="uq_persona_version"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# KNOWLEDGE DOCUMENT
# ─────────────────────────────────────────────────────────────────────────────

class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    category = Column(
        SAEnum("theia", "camp", "sma", "compliance", "market", "general",
               name="doc_category"),
        nullable=False,
        default="general",
    )
    content_text = Column(Text, nullable=False)
    source_file = Column(String(500), nullable=True)
    file_size_bytes = Column(Integer, nullable=True)

    # Approval workflow
    status = Column(
        SAEnum("pending", "approved", "rejected", "expired", name="doc_status"),
        nullable=False,
        default="pending",
    )
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    chunks = relationship("KnowledgeChunk", back_populates="document", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<KnowledgeDocument '{self.title}' [{self.category}]>"


# ─────────────────────────────────────────────────────────────────────────────
# KNOWLEDGE CHUNK (for RAG embeddings)
# ─────────────────────────────────────────────────────────────────────────────

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index = Column(Integer, nullable=False)  # Position within document
    chunk_text = Column(Text, nullable=False)
    token_count = Column(Integer, nullable=True)
    embedding = Column(Vector(1536), nullable=True)  # pgvector 1536-dim (voyage-3 / ada-002)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # Relationships
    document = relationship("KnowledgeDocument", back_populates="chunks")

    __table_args__ = (
        Index("ix_knowledge_chunks_embedding", "embedding",
              postgresql_using="ivfflat",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    def __repr__(self):
        return f"<KnowledgeChunk doc={self.document_id} idx={self.chunk_index}>"


# ─────────────────────────────────────────────────────────────────────────────
# CONVERSATION
# ─────────────────────────────────────────────────────────────────────────────

class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    employee_id = Column(
        UUID(as_uuid=True),
        ForeignKey("employees.id", ondelete="SET NULL"),
        nullable=True,
    )
    persona_id = Column(
        UUID(as_uuid=True),
        ForeignKey("personas.id", ondelete="SET NULL"),
        nullable=True,
    )
    title = Column(String(500), nullable=True)  # Auto-generated from first message
    messages = Column(JSONB, nullable=False, default=list)
    tools_invoked = Column(JSONB, nullable=True, default=list)
    compliance_flags = Column(JSONB, nullable=True, default=list)
    response_mode = Column(
        SAEnum("informational", "guided_advisory", "escalation",
               name="response_mode"),
        nullable=True,
        default="informational",
    )
    message_count = Column(Integer, default=0, nullable=False)
    total_tokens = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Relationships
    employee = relationship("Employee", back_populates="conversations")

    def __repr__(self):
        return f"<Conversation {self.id} ({self.message_count} msgs)>"


# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE AUDIT LOG
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceLog(Base):
    __tablename__ = "compliance_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    employee_id = Column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True)
    flag_type = Column(
        SAEnum("prohibited_language", "missing_disclaimer", "pii_detected",
               "hallucination_suspect", "tone_deviation", "escalation_triggered",
               name="compliance_flag_type"),
        nullable=False,
    )
    severity = Column(
        SAEnum("low", "medium", "high", "critical", name="flag_severity"),
        nullable=False,
        default="medium",
    )
    description = Column(Text, nullable=False)
    original_text = Column(Text, nullable=True)  # The flagged text
    corrected_text = Column(Text, nullable=True)  # Auto-corrected version (if applicable)
    resolved = Column(Boolean, default=False, nullable=False)
    resolved_by = Column(UUID(as_uuid=True), ForeignKey("employees.id"), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        Index("ix_compliance_logs_conversation", "conversation_id"),
        Index("ix_compliance_logs_severity", "severity"),
        Index("ix_compliance_logs_created", "created_at"),
    )

    def __repr__(self):
        return f"<ComplianceLog [{self.flag_type}] {self.severity}>"
