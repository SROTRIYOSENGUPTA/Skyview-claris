"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Persona Engine

Assembles the 3-layer system prompt (Firm + Persona + Workflow) for each
employee's AI advisor. Handles persona loading, prompt composition, and
tool permission filtering.
"""

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from models import Employee, Persona
from chatbot import TOOLS  # Reuse existing tool definitions

logger = logging.getLogger("skyview.persona_engine")


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 1: FIRM CONTEXT (shared across all personas)
# ─────────────────────────────────────────────────────────────────────────────

FIRM_CONTEXT_LAYER = """
══════════════════════════════════════════════════════
LAYER 1 — SKYVIEW INVESTMENT ADVISORS: FIRM CONTEXT
══════════════════════════════════════════════════════

You are an AI advisor within the SkyView Investment Advisors internal platform.
You are powered by the Claris Multi-Persona system. Each SkyView employee has a
personalized AI advisor that reflects their individual expertise and communication style.

ABOUT SKYVIEW:
SkyView Investment Advisors LLC is a private investment office established in 2009,
catalyzing opportunities globally across both public and private markets. The firm
manages approximately $10 billion in assets and is registered with the SEC as an
investment adviser.

CORE PHILOSOPHY: Research Driven Approach — separating high-quality information from
noise to construct robust, sustainable investment solutions. As Einstein said:
"Information alone is not knowledge. The only source of knowledge is experience."

LEADERSHIP:
• Gideon Berger — Chief Executive Officer & Chief Investment Officer
• Mark O'Brien — Managing Director, Head of Investment Solutions
• Steven Geller — Managing Director, Head of Wealth Management & Head of Operations
• David Helgager — Managing Director, Head of Risk & Head of Trading
• Steven Gerbel — Managing Director, Head of Hedge Fund Strategies
• Aditya Joshi — Managing Director, Head of Quantitative Analysis
• John Ahn — Chief Financial Officer

ADVISORY BOARD:
• Dr. Harry Markowitz — Nobel Laureate in Economic Sciences, Father of Modern Portfolio Theory
  (consulting with SkyView since 1992)

INVESTMENT FRAMEWORK — THEIA (Enhanced Investment Architecture):
SkyView's proprietary multi-factor framework designed to enhance a client's existing
portfolio through complementary strategies with low correlation, strong downside
protection, and alternative alpha generation.

CAMP (Customized Access Management Platform):
SkyView's platform providing access to alternative investment strategies typically
reserved for large institutional investors. Simplified subscription process with
institutional-grade due diligence.

SMA STRATEGIES (7 Separately Managed Accounts):
SkyView offers seven distinct SMA strategies spanning equity, fixed income, and
multi-asset approaches, each managed with disciplined institutional infrastructure.

CLIENT SEGMENTS:
• Family Offices & Multi-Family Offices
• Institutions
• Wealth Managers

CORE VALUES:
• Rigorous research and due diligence
• Open architecture — no proprietary product conflicts
• Collaborative, transparent client partnerships
• Disciplined institutional infrastructure
• Long-term relationship focus

══════════════════════════════════════════════════════
RESPONSE STYLE — MANDATORY, NON-NEGOTIABLE
══════════════════════════════════════════════════════

You are speaking as a senior investment advisor to a peer inside a
professional investment firm. Your register is that of an experienced
colleague — measured, direct, substantive. Not a chatbot, not a customer
service agent, not a marketer.

FORMATTING RULES:
• Never use tables, emoji flags, or decorative icons in conversational
  replies. Tables are permitted ONLY when the user explicitly requests
  tabular output (e.g. "compare these three funds in a table").
• No emojis. None. Not in headers, not as bullets, not as reactions.
• Avoid excessive bolding, headers, and bullet lists in short answers.
  Write in prose. Use lists only when the content is genuinely
  enumerative (three or more discrete items that don't read naturally
  as a sentence).
• No markdown ornamentation for flourish. Plain, clean prose.

VOICE RULES:
• Be direct and confident. State the answer first, then the reasoning
  if needed. Do not hedge with "Great question" or "That's interesting."
• Do NOT narrate your own capabilities, identity, or feelings. Do not
  say things like "Based on my profile…", "As your AI advisor…", "I have
  access to…". Demonstrate expertise by using it, not by describing it.
• Do NOT end responses with "Is there anything else…", "Let me know if
  you have more questions", "Happy to help with…", or similar chatbot
  closers. End when the substance ends. Let the conversation breathe.
• Do NOT use phrases like "Great point", "Absolutely", "I'd be happy to".
  Get to the content.
• Keep responses concise. A one-line answer is better than a paragraph
  of throat-clearing. Avoid over-explaining. Do not restate the question.
• Write at a peer-to-peer register. Assume the user understands basic
  finance, portfolio theory, and the firm's terminology. Don't lecture.
• If a question is personal or off-topic (e.g. about languages spoken,
  biographical detail), answer it in one or two sentences and stop.
  Do not pivot back to investment topics unsolicited.

EXAMPLES OF WHAT NOT TO DO:
✗ "Based on my profile, I speak 6 languages: 🇺🇸 English..."
✗ "Is there anything else you'd like to know?"
✗ "Great question! Let me break this down for you."
✗ Decorative tables for 3-6 line answers.

EXAMPLES OF WHAT TO DO INSTEAD:
✓ "English, Bengali, Hindi, Marathi, Spanish, and French. Handy when
   a French research note crosses the desk or a Madrid-based family
   office calls."
✓ "Equities look fully priced into a cooling earnings cycle. I'd keep
   duration short and lean into quality and free-cash-flow screens."
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 2: PERSONA (per-employee, loaded from database)
# ─────────────────────────────────────────────────────────────────────────────

def build_layer2_prompt(persona: Persona) -> str:
    """
    Construct the Layer 2 persona prompt from the database record.
    If a full system_prompt_layer2 is stored, use it directly.
    Otherwise, generate from structured fields.
    """
    if persona.system_prompt_layer2:
        return persona.system_prompt_layer2

    # Fallback: build from structured data
    parts = [
        f"\n{'═' * 54}",
        f"LAYER 2 — PERSONAL ADVISOR IDENTITY: {persona.display_name.upper()}",
        f"{'═' * 54}\n",
        f"You are the personalized AI advisor for {persona.display_name}.",
    ]

    if persona.employee and persona.employee.title:
        parts.append(f"Their role at SkyView: {persona.employee.title}")

    if persona.bio_summary:
        parts.append(f"\nBACKGROUND:\n{persona.bio_summary}")

    if persona.communication_style:
        style = persona.communication_style
        parts.append("\nCOMMUNICATION STYLE:")
        if style.get("tone"):
            parts.append(f"• Tone: {style['tone']}")
        if style.get("formality"):
            parts.append(f"• Formality: {style['formality']}")
        if style.get("vocabulary_level"):
            parts.append(f"• Vocabulary: {style['vocabulary_level']}")
        if style.get("signature_phrases"):
            phrases = ", ".join(f'"{p}"' for p in style["signature_phrases"])
            parts.append(f"• Signature phrases: {phrases}")

    if persona.expertise_areas:
        areas = ", ".join(persona.expertise_areas)
        parts.append(f"\nEXPERTISE AREAS: {areas}")

    if persona.education:
        edu = persona.education
        if edu.get("universities"):
            parts.append(f"EDUCATION: {', '.join(edu['universities'])}")
        if edu.get("skills"):
            parts.append(f"SPECIALIZED SKILLS: {', '.join(edu['skills'])}")

    if persona.response_preferences:
        prefs = persona.response_preferences
        parts.append("\nRESPONSE PREFERENCES:")
        if prefs.get("default_length"):
            parts.append(f"• Default length: {prefs['default_length']}")
        if prefs.get("format"):
            parts.append(f"• Preferred format: {prefs['format']}")
        if prefs.get("detail_level"):
            parts.append(f"• Detail level: {prefs['detail_level']}")

    parts.append(
        "\nAdapt all responses to match this persona's communication style and expertise. "
        "When drafting communications, use their voice and tone. When performing analysis, "
        "emphasize their areas of specialization."
    )

    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# LAYER 3: WORKFLOW INSTRUCTION (per-request, dynamic)
# ─────────────────────────────────────────────────────────────────────────────

WORKFLOW_TEMPLATES = {
    "draft_email": (
        "The user is drafting a client communication. Use their persona voice. "
        "Maintain SkyView's institutional tone. Include appropriate disclaimers."
    ),
    "portfolio_analysis": (
        "The user is performing portfolio analysis. Apply Modern Portfolio Theory "
        "principles. Be quantitative and precise. Cite specific figures."
    ),
    "market_research": (
        "The user is researching market conditions. Separate signal from noise per "
        "SkyView's philosophy. Reference relevant macroeconomic indicators."
    ),
    "general": (
        "Respond to the user's query using their persona's communication style and "
        "expertise. Draw on firm knowledge when relevant."
    ),
}


def detect_workflow(user_message: str) -> str:
    """
    Simple keyword-based workflow detection.
    Returns the workflow key for Layer 3 prompt selection.
    """
    msg_lower = user_message.lower()

    if any(kw in msg_lower for kw in ["draft", "email", "write", "letter", "communication"]):
        return "draft_email"
    elif any(kw in msg_lower for kw in ["portfolio", "allocation", "holdings", "rebalance"]):
        return "portfolio_analysis"
    elif any(kw in msg_lower for kw in ["market", "economy", "macro", "sector", "outlook"]):
        return "market_research"
    return "general"


# ─────────────────────────────────────────────────────────────────────────────
# PROMPT ASSEMBLY
# ─────────────────────────────────────────────────────────────────────────────

def assemble_system_prompt(
    persona: Persona,
    user_message: str = "",
    knowledge_context: str = "",
    compliance_directives: str = "",
) -> str:
    """
    Assemble the complete multi-layer system prompt.

    Order:
      1. Compliance / Policy directives (L4) — always first
      2. Firm Context (L1) — SkyView identity
      3. Persona (L2) — individual personality
      4. Knowledge Context (L3) — RAG-retrieved content
      5. Workflow Instruction — task-specific guidance
    """
    layers = []

    # L4: Compliance (injected first so the LLM treats it as highest-priority)
    if compliance_directives:
        layers.append(compliance_directives)

    # L1: Firm Context
    layers.append(FIRM_CONTEXT_LAYER)

    # L2: Persona
    layers.append(build_layer2_prompt(persona))

    # L3: Knowledge Context (RAG results)
    if knowledge_context:
        layers.append(
            f"\n{'═' * 54}\n"
            f"LAYER 3 — FIRM KNOWLEDGE CONTEXT\n"
            f"{'═' * 54}\n"
            f"The following information has been retrieved from SkyView's approved "
            f"content library. Use it to ground your responses. Cite sources when referencing "
            f"specific data.\n\n{knowledge_context}"
        )

    # Workflow instruction
    workflow = detect_workflow(user_message)
    workflow_text = WORKFLOW_TEMPLATES.get(workflow, WORKFLOW_TEMPLATES["general"])
    layers.append(
        f"\n{'═' * 54}\n"
        f"ACTIVE WORKFLOW: {workflow.upper().replace('_', ' ')}\n"
        f"{'═' * 54}\n"
        f"{workflow_text}"
    )

    return "\n\n".join(layers)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL FILTERING
# ─────────────────────────────────────────────────────────────────────────────

def get_permitted_tools(persona: Persona) -> list:
    """
    Return the list of tool definitions permitted for this persona.
    If tool_permissions is empty or None, all tools are permitted.
    """
    if not persona.tool_permissions:
        return TOOLS

    permitted_names = set(persona.tool_permissions)
    return [t for t in TOOLS if t["name"] in permitted_names]


# ─────────────────────────────────────────────────────────────────────────────
# PERSONA LOADING
# ─────────────────────────────────────────────────────────────────────────────

def load_persona(db: DBSession, employee_id: str) -> Optional[Persona]:
    """Load an active persona for a given employee ID."""
    employee = db.query(Employee).filter(
        Employee.id == employee_id,
        Employee.is_active == True,
    ).first()

    if not employee:
        logger.warning(f"Employee {employee_id} not found or inactive")
        return None

    if not employee.persona or not employee.persona.is_active:
        logger.warning(f"No active persona for employee {employee_id}")
        return None

    return employee.persona


def load_persona_by_email(db: DBSession, email: str) -> Optional[Persona]:
    """Load an active persona by employee email (used after SSO login)."""
    employee = db.query(Employee).filter(
        Employee.email == email,
        Employee.is_active == True,
    ).first()

    if not employee:
        logger.warning(f"Employee with email {email} not found or inactive")
        return None

    if not employee.persona or not employee.persona.is_active:
        logger.warning(f"No active persona for {email}")
        return None

    return employee.persona


# ─────────────────────────────────────────────────────────────────────────────
# PERSONA CHATBOT (extends SkyViewChatbot pattern)
# ─────────────────────────────────────────────────────────────────────────────

import os
import time
import uuid as _uuid
from datetime import datetime

import anthropic
from anthropic import APIConnectionError, APIStatusError, RateLimitError
from chatbot import (
    MODEL, MAX_TOKENS, MAX_HISTORY_MSGS, RETRY_ATTEMPTS, RETRY_BASE_DELAY,
    build_content_blocks, execute_tool, _strip_binary_content,
)


class PersonaChatbot:
    """
    A persona-aware chatbot that composes the multi-layer system prompt
    per-employee and routes through the compliance engine.
    """

    def __init__(
        self,
        persona: Persona,
        session_id: str = None,
        knowledge_retriever=None,
        compliance_engine=None,
    ):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not configured.")

        self.client = anthropic.Anthropic(api_key=api_key)
        self.persona = persona
        self.session_id = session_id or str(_uuid.uuid4())[:8]
        self.history = []
        self.message_count = 0
        self.total_tokens = 0
        self.tools_invoked = []
        self.compliance_flags = []
        self.created_at = datetime.utcnow().isoformat()

        # Optional pluggable components
        self._knowledge_retriever = knowledge_retriever
        self._compliance_engine = compliance_engine

        # Filtered tools for this persona
        self.permitted_tools = get_permitted_tools(persona)

        logger.info(
            f"PersonaChatbot created | session={self.session_id} "
            f"| persona={persona.display_name} "
            f"| tools={len(self.permitted_tools)}"
        )

    def get_system_prompt(self, user_message: str = "") -> str:
        """Build the complete system prompt for this interaction."""
        # Get RAG context if knowledge retriever is available
        knowledge_context = ""
        if self._knowledge_retriever and user_message:
            knowledge_context = self._knowledge_retriever.retrieve(user_message)

        # Get compliance directives
        compliance_directives = ""
        if self._compliance_engine:
            compliance_directives = self._compliance_engine.get_directives()

        return assemble_system_prompt(
            persona=self.persona,
            user_message=user_message,
            knowledge_context=knowledge_context,
            compliance_directives=compliance_directives,
        )

    def chat(self, user_message: str, attachments: list = None) -> dict:
        """Process a message and return a response dict."""
        self.message_count += 1
        self._trim_history()

        content = build_content_blocks(user_message, attachments)
        self.history.append({"role": "user", "content": content})

        system_prompt = self.get_system_prompt(user_message)
        tools_used = []
        total_tokens = 0

        # Initial API call
        response = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self.client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=self.permitted_tools,
                    messages=self.history,
                )
                total_tokens += response.usage.input_tokens + response.usage.output_tokens
                break
            except RateLimitError:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"[{self.session_id}] Rate limited — retrying in {delay}s")
                if attempt == RETRY_ATTEMPTS:
                    raise
                time.sleep(delay)
            except APIConnectionError as e:
                logger.error(f"[{self.session_id}] Connection error: {e}")
                if attempt == RETRY_ATTEMPTS:
                    raise
                time.sleep(RETRY_BASE_DELAY)
            except APIStatusError as e:
                logger.error(f"[{self.session_id}] API {e.status_code}: {e.message}")
                raise

        # Tool-use agentic loop
        while response.stop_reason == "tool_use":
            self.history.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tools_used.append(block.name)
                    result = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            self.history.append({"role": "user", "content": tool_results})
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                tools=self.permitted_tools,
                messages=self.history,
            )
            total_tokens += response.usage.input_tokens + response.usage.output_tokens

        # Extract final text
        final_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )

        # Post-processing compliance check
        if self._compliance_engine:
            check_result = self._compliance_engine.check_response(final_text)
            if check_result.get("flags"):
                self.compliance_flags.extend(check_result["flags"])
            if check_result.get("corrected_text"):
                final_text = check_result["corrected_text"]

        self.history.append({"role": "assistant", "content": final_text})
        self.total_tokens += total_tokens
        self.tools_invoked.extend(tools_used)

        logger.info(
            f"[{self.session_id}] Done | persona={self.persona.display_name} "
            f"| tokens={total_tokens} | tools={tools_used}"
        )

        return {
            "text": final_text,
            "session_id": self.session_id,
            "persona": self.persona.display_name,
            "tokens_used": total_tokens,
            "tools_used": tools_used,
            "compliance_flags": [f.get("type", "unknown") for f in self.compliance_flags[-5:]],
        }

    def reset(self):
        self.history = []
        self.message_count = 0
        self.total_tokens = 0
        self.tools_invoked = []
        self.compliance_flags = []
        logger.info(f"[{self.session_id}] Reset | persona={self.persona.display_name}")

    def _trim_history(self):
        if len(self.history) > MAX_HISTORY_MSGS:
            excess = len(self.history) - MAX_HISTORY_MSGS
            self.history = self.history[excess:]

    def get_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "persona": self.persona.display_name,
            "employee": self.persona.employee.full_name if self.persona.employee else None,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "tools_invoked": self.tools_invoked,
            "compliance_flags_count": len(self.compliance_flags),
            "created_at": self.created_at,
        }
