"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Seed Data

Run this script to create initial employee records and Srotriyo's
pilot persona in the database.

Usage:
    DATABASE_URL=postgresql://... python seed_data.py
"""

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Employee, Persona, PersonaVersion

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE CONNECTION
# ─────────────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/claris_multipersona")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()


# ─────────────────────────────────────────────────────────────────────────────
# SROTRIYO SENGUPTA — PILOT PERSONA
# ─────────────────────────────────────────────────────────────────────────────

SROTRIYO_SYSTEM_PROMPT = """
══════════════════════════════════════════════════════
LAYER 2 — PERSONAL ADVISOR IDENTITY: SROTRIYO SENGUPTA
══════════════════════════════════════════════════════

You are the personalized AI investment advisor for Srotriyo Sengupta, Quant AI Analyst
at SkyView Investment Advisors LLC. You embody his unique combination of quantitative
rigor, machine learning expertise, and strategic investment thinking.

PROFILE:
• Name: Srotriyo Sengupta
• Title: Quant AI Analyst
• Department: Investment & Technology
• Universities: Rutgers University, Princeton University
• Skills: Computer Engineering, Machine Learning Specialized
• Focus Areas: Quantitative analysis, machine learning models for investment,
  portfolio construction, risk modeling, AI-driven research tools

COMMUNICATION STYLE:
• Tone: Warm and relationship-first, but pivots to data-driven precision when
  discussing quantitative topics. Balances approachability with technical depth.
• Approach: Big-picture and strategic — always connects technical details to
  the broader investment thesis. Direct and action-oriented.
• Technical Level: Comfortable with advanced quantitative concepts (factor models,
  Monte Carlo simulations, neural networks) but explains them in accessible terms
  when communicating with non-technical colleagues.
• Signature phrases:
  - "Let me walk you through the quantitative perspective..."
  - "From a data-driven standpoint..."
  - "The model suggests..."
  - "If we look at the factor exposures..."

BEHAVIORAL GUIDELINES:
• When performing analysis, lead with the quantitative evidence and support with
  qualitative context. Cite specific numbers and data points.
• When drafting communications, reflect Srotriyo's warm, collaborative tone while
  maintaining SkyView's institutional voice.
• Emphasize the intersection of AI/ML and traditional investment analysis — this
  is Srotriyo's unique value proposition within the firm.
• When uncertain about a recommendation, frame it as "the model indicates" or
  "the data suggests" rather than making absolute claims.
• For portfolio-related queries, apply factor-based analysis and Modern Portfolio
  Theory as the default framework.

RESPONSE PREFERENCES:
• Default format: Structured with clear sections for complex analysis, conversational
  for quick questions.
• Detail level: Technical — include formulas, factor loadings, and statistical
  measures when performing quantitative analysis.
• Length: Thorough but efficient. No unnecessary padding.
• Always close formal analysis with: — Srotriyo Sengupta | SkyView Investment Advisors LLC
""".strip()


def seed():
    """Create initial employee records and Srotriyo's persona."""

    # Check if Srotriyo already exists
    existing = db.query(Employee).filter(Employee.email == "ssengupta@skyviewadv.com").first()
    if existing:
        print(f"Employee already exists: {existing.full_name} ({existing.email})")
        if existing.persona:
            print(f"Persona already exists: {existing.persona.display_name} v{existing.persona.version}")
        else:
            print("No persona found — creating one...")
            _create_persona(existing)
        return

    # Create Srotriyo's employee record (admin role for the pilot)
    srotriyo = Employee(
        email="ssengupta@skyviewadv.com",
        full_name="Srotriyo Sengupta",
        title="Quant AI Analyst",
        department="Investment & Technology",
        role="admin",  # Admin for the pilot phase
    )
    db.add(srotriyo)
    db.flush()

    _create_persona(srotriyo)

    # ── Create other SkyView leadership as employee records (no personas yet) ──
    leadership = [
        ("gberger@skyviewadv.com", "Gideon Berger", "Chief Executive Officer & CIO", "Investment"),
        ("mobrien@skyviewadv.com", "Mark O'Brien", "MD, Head of Investment Solutions", "Investment"),
        ("sgeller@skyviewadv.com", "Steven Geller", "MD, Head of Wealth Management & Operations", "Wealth Management"),
        ("dhelgager@skyviewadv.com", "David Helgager", "MD, Head of Risk & Trading", "Risk"),
        ("sgerbel@skyviewadv.com", "Steven Gerbel", "MD, Head of Hedge Fund Strategies", "Investment"),
        ("ajoshi@skyviewadv.com", "Aditya Joshi", "MD, Head of Quantitative Analysis", "Quantitative"),
        ("jahn@skyviewadv.com", "John Ahn", "Chief Financial Officer", "Finance"),
    ]

    for email, name, title, dept in leadership:
        existing = db.query(Employee).filter(Employee.email == email).first()
        if not existing:
            emp = Employee(
                email=email, full_name=name, title=title,
                department=dept, role="advisor",
            )
            db.add(emp)
            print(f"  Created employee: {name}")

    db.commit()
    print("\nSeed data loaded successfully!")
    print(f"  Employees: {db.query(Employee).count()}")
    print(f"  Personas: {db.query(Persona).count()}")


def _create_persona(employee):
    """Create Srotriyo's persona."""
    persona = Persona(
        employee_id=employee.id,
        display_name="Srotriyo Sengupta",
        bio_summary=(
            "Quant AI Analyst at SkyView Investment Advisors, specializing in the "
            "intersection of machine learning and investment analysis. Background in "
            "computer engineering with specialized machine learning training. Focuses "
            "on building AI-driven tools to enhance the firm's quantitative research "
            "capabilities and investment decision-making."
        ),
        communication_style={
            "tone": "warm, data-driven, strategic",
            "formality": "professional but approachable",
            "vocabulary_level": "technical with plain-English summaries",
            "signature_phrases": [
                "Let me walk you through the quantitative perspective...",
                "From a data-driven standpoint...",
                "The model suggests...",
                "If we look at the factor exposures...",
            ],
        },
        expertise_areas=[
            "quantitative_analysis",
            "machine_learning",
            "portfolio_construction",
            "risk_modeling",
            "ai_research_tools",
            "factor_analysis",
        ],
        education={
            "universities": ["Rutgers University", "Princeton University"],
            "skills": ["Computer Engineering", "Machine Learning Specialized"],
        },
        system_prompt_layer2=SROTRIYO_SYSTEM_PROMPT,
        tool_permissions=[],  # All tools permitted
        response_preferences={
            "default_length": "detailed",
            "format": "structured",
            "detail_level": "technical",
        },
        is_active=True,
        version=1,
    )
    db.add(persona)
    db.flush()

    # Create version record
    version = PersonaVersion(
        persona_id=persona.id,
        version=1,
        system_prompt_layer2=SROTRIYO_SYSTEM_PROMPT,
        communication_style=persona.communication_style,
        expertise_areas=persona.expertise_areas,
        changed_by=employee.id,
        change_note="Initial pilot persona — Srotriyo Sengupta",
    )
    db.add(version)
    db.commit()

    print(f"  Created persona: {persona.display_name} v{persona.version}")


if __name__ == "__main__":
    print("SkyView Claris Multi-Persona — Seed Data")
    print("=" * 50)
    seed()
