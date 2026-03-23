"""
SkyView Investment Advisors LLC
Claris -  Core Chatbot Engine v3.0

Changes in v3:
  - Multi-modal attachment support (images, PDFs, text, CSV)
  - Refined system prompt: institutional tone, no filler phrases
  - Per-session isolation with full retry and history trimming
"""

import json
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import anthropic
from anthropic import APIConnectionError, APIStatusError, RateLimitError

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("skyview.chatbot")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────
MODEL             = "claude-sonnet-4-6"   # ~4x faster than Opus; switched for speed
MAX_TOKENS        = 1500
MAX_HISTORY_MSGS  = 20                    # trimmed from 40 — less context = faster
RETRY_ATTEMPTS    = 3
RETRY_BASE_DELAY  = 1.5
CONVERSATIONS_DIR = Path("conversations")

# ─────────────────────────────────────────────────────────────────────────────
# SYSTEM PROMPT — Institutional tone, no AI filler
# ─────────────────────────────────────────────────────────────────────────────
ADVISOR_SYSTEM_PROMPT = """You are Claris, the internal investment research and advisory assistant for SkyView Investment Advisors LLC.
You are operating in ADVISOR MODE — assisting SkyView professionals with research, analysis, client communications, and portfolio review.
Full tool access is available including portfolio analysis, communication drafting, and strategy research.

══════════════════════════════════════════════════════
ABOUT SKYVIEW INVESTMENT ADVISORS LLC
══════════════════════════════════════════════════════
SkyView is a private investment office established in 2009, catalyzing opportunities globally
across both public and private markets. The firm manages approximately $6.9 billion in assets
and is registered with the SEC as an investment adviser.

Core philosophy: Research Driven Approach — separating high-quality information from noise to
construct robust, sustainable investment solutions. As Einstein said: "Information alone is not
knowledge. The only source of knowledge is experience."

Team: Senior professionals averaging 30+ years of experience, including alumni of Goldman Sachs,
Merrill Lynch, Soros Fund Management, Riverview, and Daiwa Securities. The firm's advisory board
includes Dr. Harry Markowitz, Nobel Laureate in Economic Sciences and father of Modern Portfolio
Theory, who has consulted with SkyView since 1992.

══════════════════════════════════════════════════════
CLIENT SEGMENTS
══════════════════════════════════════════════════════
• Family Offices & Multi-Family Offices — customised multi-asset solutions, alternative access,
  open-architecture transparency, thorough risk/return analysis.
• Institutions — scalable infrastructure, boutique service without the conflicts of larger firms,
  disciplined investment process, complex traditional and alternative strategies.
• Wealth Managers — institutional-grade multi-asset solutions, business development support,
  investment education, freeing advisors to focus on client relationships.

══════════════════════════════════════════════════════
INVESTMENT UNIVERSE
══════════════════════════════════════════════════════
Traditional: Long/short equity, fixed income, global macro, commodities, mutual funds, ETPs.
Alternative: Statistical relative value, merger arbitrage, event-driven, convertible arbitrage,
asset arbitrage, distressed investing, private equity, asset-based finance, venture capital.

Investment process: Identify sources of risk and return at the portfolio, strategy, security,
and factor level. Open architecture — no proprietary product conflicts. Collaborative, transparent
client partnerships with disciplined institutional infrastructure.

══════════════════════════════════════════════════════
CAPABILITIES
══════════════════════════════════════════════════════
• Client Q&A: SkyView services, investment products, market education, account processes.
• Advisor Tools: Draft client communications, meeting prep, research, business development.
• Portfolio Review: Allocation analysis, risk decomposition, factor exposure, rebalancing,
  performance attribution. Apply Modern Portfolio Theory principles.
• Strategy Research: Deep analysis of any strategy in SkyView's investment universe.
• Document Analysis: Review uploaded documents, data files, and reports.

══════════════════════════════════════════════════════
RESPONSE STANDARDS — CRITICAL
══════════════════════════════════════════════════════
TONE & STYLE:
• Write as a senior investment professional, not as a chatbot or virtual assistant.
• Be direct, precise, and authoritative. Brevity is a virtue.
• Match the register to the audience — institutional for advisors, clear and educational for clients.
• Use structured formats (headers, tables) only when content genuinely warrants it.
  For straightforward questions, respond in professional prose.
• When reviewing attached documents or data, be specific and cite relevant figures.

DO NOT:
• Start responses with "Certainly!", "Of course!", "Great question!", "Absolutely!",
  "I'd be happy to", "I understand that", or any similar filler phrases.
• Use excessive bullet points for simple answers.
• Hedge every statement with unnecessary qualifiers.
• Sound like a customer service chatbot.
• Use emoji or informal language.

COMPLIANCE:
• Do not provide personalised investment recommendations (e.g., "buy X security").
• When giving investment-related guidance, include at the close: "For investment decisions
  specific to your situation, please consult a licensed SkyView investment professional."
• Do not guarantee returns or predict specific market outcomes.
• Do not reference or speculate about other clients' information.

Sign-off for formal communications and analysis: — SkyView Investment Advisors LLC
"""

CLIENT_SYSTEM_PROMPT = """You are Claris, SkyView Investment Advisors' client intelligence assistant.
You are operating in CLIENT PORTAL MODE — serving SkyView's clients and prospective clients.

══════════════════════════════════════════════════════
ABOUT SKYVIEW INVESTMENT ADVISORS LLC
══════════════════════════════════════════════════════
SkyView is a private investment office established in 2009, catalyzing opportunities globally
across both public and private markets. The firm manages approximately $6.9 billion in assets
and is registered with the SEC as an investment adviser.

Core philosophy: Research Driven Approach — separating high-quality information from noise to
construct robust, sustainable investment solutions.

Team: Senior professionals averaging 30+ years of experience, including alumni of Goldman Sachs,
Merrill Lynch, Soros Fund Management, and Daiwa Securities. The firm's advisory board
includes Dr. Harry Markowitz, Nobel Laureate in Economic Sciences and father of Modern Portfolio
Theory, who has consulted with SkyView since 1992.

══════════════════════════════════════════════════════
YOUR ROLE IN CLIENT PORTAL MODE
══════════════════════════════════════════════════════
• Answer questions about SkyView's investment philosophy, strategies, team, and services.
• Explain investment concepts clearly — avoid unnecessary jargon, but maintain professional depth.
• Discuss market conditions, economic themes, and investment considerations.
• Help clients understand portfolio concepts (asset allocation, diversification, risk management).
• For enquiries about becoming a client or account-specific questions, direct them to SkyView at:
  contact@skyviewadv.com | +1 (914) 307-7770

══════════════════════════════════════════════════════
RESPONSE STANDARDS — CRITICAL
══════════════════════════════════════════════════════
TONE & STYLE:
• Write as a senior investment professional speaking directly with a valued client.
• Be clear, precise, and reassuring. Educate without being condescending.
• Use structured formats (headers, tables) when content is complex enough to warrant it.
• For straightforward questions, respond in professional prose — no unnecessary lists.

DO NOT:
• Start responses with "Certainly!", "Of course!", "Great question!", "Absolutely!",
  "I'd be happy to", or any similar chatbot filler phrases.
• Sound like an automated customer service bot.
• Use emoji or overly casual language.
• Discuss other clients or proprietary internal information.

COMPLIANCE:
• Do not provide personalised investment recommendations (e.g., "buy X security").
• When giving investment-related guidance, include at the close: "For recommendations
  specific to your situation, please speak with a SkyView investment professional."
• Do not guarantee returns or predict specific market outcomes.

Sign-off for formal responses: — SkyView Investment Advisors LLC
"""

# Keep SYSTEM_PROMPT as an alias for backward compatibility
SYSTEM_PROMPT = ADVISOR_SYSTEM_PROMPT

# ─────────────────────────────────────────────────────────────────────────────
# TOOL DEFINITIONS
# ─────────────────────────────────────────────────────────────────────────────
TOOLS = [
    {
        "name": "analyze_portfolio",
        "description": (
            "Analyse a client portfolio using Modern Portfolio Theory principles. "
            "Accepts holdings, allocations, performance data, and client context. "
            "Returns structured analysis: risk/return decomposition, concentration flags, "
            "factor exposures, and rebalancing recommendations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "portfolio_data": {
                    "type": "string",
                    "description": "Holdings, weights (%), asset classes, and performance figures."
                },
                "client_type": {
                    "type": "string",
                    "enum": ["family_office", "institution", "wealth_manager", "end_client"],
                },
                "investment_objective": {"type": "string"},
                "risk_tolerance": {
                    "type": "string",
                    "enum": ["conservative", "moderate", "moderately_aggressive", "aggressive"],
                },
                "time_horizon": {"type": "string"}
            },
            "required": ["portfolio_data"]
        }
    },
    {
        "name": "research_investment_strategy",
        "description": "Deep research on a specific strategy or asset class in SkyView's investment universe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "strategy": {"type": "string"},
                "depth": {"type": "string", "enum": ["overview", "detailed", "technical"]},
                "audience": {"type": "string", "enum": ["client", "advisor", "institutional"]}
            },
            "required": ["strategy"]
        }
    },
    {
        "name": "analyze_market_environment",
        "description": "Research and contextualise current market conditions, macro themes, or economic topics.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "context": {"type": "string"}
            },
            "required": ["topic"]
        }
    },
    {
        "name": "draft_client_communication",
        "description": "Draft professional client-facing or internal communications in SkyView's institutional voice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "communication_type": {
                    "type": "string",
                    "enum": ["client_email", "quarterly_review", "meeting_summary",
                             "onboarding_letter", "investment_proposal", "market_update",
                             "risk_notification", "business_development"]
                },
                "key_points": {"type": "string"},
                "client_name": {"type": "string"},
                "client_type": {
                    "type": "string",
                    "enum": ["family_office", "institution", "wealth_manager"]
                },
                "tone": {"type": "string", "enum": ["formal", "professional", "conversational"]}
            },
            "required": ["communication_type", "key_points"]
        }
    },
    {
        "name": "assess_risk_factors",
        "description": "Structured risk assessment at portfolio, strategy, security, and factor level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "risk_dimensions": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            "required": ["subject"]
        }
    },
    {
        "name": "analyze_document",
        "description": (
            "Analyse a document, report, data file, or spreadsheet that has been attached. "
            "Extract key data, summarise findings, identify risks or opportunities, "
            "and provide actionable observations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_type": {
                    "type": "string",
                    "enum": ["portfolio_report", "financial_statement", "research_report",
                             "data_file", "contract", "presentation", "other"]
                },
                "analysis_focus": {
                    "type": "string",
                    "description": "What specifically to analyse or extract from the document."
                }
            },
            "required": ["document_type"]
        }
    }
]


# ─────────────────────────────────────────────────────────────────────────────
# TOOL EXECUTION
# ─────────────────────────────────────────────────────────────────────────────
def execute_tool(tool_name: str, tool_input: dict) -> str:
    logger.info(f"Tool: {tool_name} | keys: {list(tool_input.keys())}")

    instructions = {
        "analyze_portfolio": (
            "Using the portfolio data and client context provided, perform a thorough analysis applying "
            "Modern Portfolio Theory principles as pioneered by Dr. Harry Markowitz. Structure the output as: "
            "1) Executive Summary (3–4 sentences), "
            "2) Asset Allocation Assessment (vs. typical benchmarks for this client type), "
            "3) Risk Analysis (concentration, factor exposures, liquidity, tail risk), "
            "4) Return Attribution where data permits, "
            "5) Rebalancing Recommendations with rationale. "
            "Write as a senior portfolio analyst at an institutional investment office. "
            "Close with the standard investment disclaimer."
        ),
        "research_investment_strategy": (
            "Provide a rigorous research summary of this strategy as SkyView would present it. "
            "Cover: mechanics and return generation, historical risk/return profile, "
            "role within a multi-asset portfolio, key risks and mitigants, "
            "market environments where the strategy performs best and worst, "
            "and SkyView's open-architecture perspective. "
            "Tailor depth and language to the specified audience."
        ),
        "analyze_market_environment": (
            "Provide a research-driven market analysis consistent with SkyView's philosophy of "
            "separating signal from noise. Structure: "
            "1) Current Environment — key data and trends, "
            "2) Implications for SkyView's asset classes, "
            "3) Risk Considerations, "
            "4) Opportunities worth monitoring. "
            "Reference relevant macroeconomic indicators and historical parallels. "
            "Maintain institutional tone. Close with investment disclaimer."
        ),
        "draft_client_communication": (
            "Draft a complete, polished communication in SkyView's institutional voice: "
            "authoritative, collaborative, transparent, and client-centric. "
            "Structure appropriately for the communication type. "
            "For client-facing documents include the investment disclaimer at the foot. "
            "Sign as [Advisor Name] | SkyView Investment Advisors LLC."
        ),
        "assess_risk_factors": (
            "Conduct a structured risk assessment using SkyView's framework: identify risk sources "
            "at the portfolio, strategy, security, and factor level. For each risk dimension: "
            "identify the risk, assess severity (Low/Medium/High/Critical), explain the mechanism, "
            "suggest mitigation. Conclude with an overall risk summary and priority actions."
        ),
        "analyze_document": (
            "Analyse the attached document thoroughly. Extract key data points, summarise the main findings, "
            "identify any risks, opportunities, or anomalies, and provide specific, actionable observations. "
            "Be precise — cite specific figures, dates, and sections where relevant. "
            "Structure the analysis clearly with an executive summary followed by detailed findings."
        ),
    }

    payload = {**tool_input, "instruction": instructions.get(tool_name, "Analyse and respond.")}
    return json.dumps(payload)



# ATTACHMENT PROCESSING

SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
SUPPORTED_DOC_TYPES   = {"application/pdf"}
SUPPORTED_TEXT_TYPES  = {"text/plain", "text/csv", "text/markdown", "application/json"}

def build_content_blocks(user_message: str, attachments: list = None) -> list:
    """
    Build a list of Anthropic content blocks from a text message + optional attachments.

    Each attachment dict:
      { "name": str, "media_type": str, "data": str (base64 or plain text) }

    Supported:
      Images (JPEG, PNG, GIF, WEBP)  → native image blocks
      PDFs                           → native document blocks
      Text / CSV / JSON / Markdown   → embedded as text blocks
    """
    blocks = []

    if attachments:
        for att in attachments:
            name       = att.get("name", "file")
            media_type = att.get("media_type", "")
            data       = att.get("data", "")

            if media_type in SUPPORTED_IMAGE_TYPES:
                blocks.append({
                    "type": "image",
                    "source": {
                        "type":       "base64",
                        "media_type": media_type,
                        "data":       data,
                    }
                })
                blocks.append({
                    "type": "text",
                    "text": f"[Image attached: {name}]"
                })

            elif media_type in SUPPORTED_DOC_TYPES:
                blocks.append({
                    "type": "document",
                    "source": {
                        "type":       "base64",
                        "media_type": "application/pdf",
                        "data":       data,
                    }
                })
                blocks.append({
                    "type": "text",
                    "text": f"[PDF attached: {name}]"
                })

            elif media_type in SUPPORTED_TEXT_TYPES or media_type.startswith("text/"):
                # data is plain text (not base64) for text files
                blocks.append({
                    "type": "text",
                    "text": f"[File attached: {name}]\n\n{data}"
                })

            else:
                # Unknown type — include as text note
                blocks.append({
                    "type": "text",
                    "text": f"[Unsupported file type: {name} ({media_type}) — please re-upload as PDF, image, or text file.]"
                })

    # The user's actual message always goes last
    if user_message:
        blocks.append({"type": "text", "text": user_message})

    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# SKYVIEW CHATBOT
# ─────────────────────────────────────────────────────────────────────────────
class SkyViewChatbot:
    def __init__(self, session_id: str = None, client_type: str = "general", role: str = "client"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            logger.error("ANTHROPIC_API_KEY is not set in the environment")
            raise EnvironmentError("ANTHROPIC_API_KEY environment variable is not configured.")
        self.client        = anthropic.Anthropic(api_key=api_key)
        self.session_id    = session_id or str(uuid.uuid4())[:8]
        self.client_type   = client_type
        self.role          = role if role in ("advisor", "client") else "client"
        self.history       = []
        self.created_at    = datetime.utcnow().isoformat()
        self.message_count = 0
        logger.info(f"Session {self.session_id} created | role={self.role} | client_type={client_type}")

    # ── Public API ─────────────────────────────────────────────────────────

    def chat(self, user_message: str, attachments: list = None) -> dict:
        """
        Process a message (with optional file attachments) and return a response dict.
        attachments: list of { name, media_type, data (base64 or text) }
        """
        self.message_count += 1
        self._trim_history()

        content = build_content_blocks(user_message, attachments)
        self.history.append({"role": "user", "content": content})

        att_names = [a.get("name") for a in (attachments or [])]
        logger.info(f"[{self.session_id}] msg #{self.message_count} | attachments={att_names}")

        tools_used   = []
        total_tokens = 0
        response     = None

        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                response = self.client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=self._build_system_prompt(),
                    tools=TOOLS,
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
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     result,
                    })
            self.history.append({"role": "user", "content": tool_results})
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=self._build_system_prompt(),
                tools=TOOLS,
                messages=self.history,
            )
            total_tokens += response.usage.input_tokens + response.usage.output_tokens

        final_text = "".join(
            block.text for block in response.content if hasattr(block, "text")
        )
        self.history.append({"role": "assistant", "content": final_text})
        logger.info(f"[{self.session_id}] Done | tokens={total_tokens} | tools={tools_used}")

        return {
            "text":        final_text,
            "session_id":  self.session_id,
            "tokens_used": total_tokens,
            "tools_used":  tools_used,
        }

    def set_client_type(self, client_type: str):
        self.client_type = client_type
        logger.info(f"[{self.session_id}] Client type → {client_type}")

    def reset(self):
        self.history       = []
        self.message_count = 0
        logger.info(f"[{self.session_id}] Reset")

    def save(self) -> str:
        CONVERSATIONS_DIR.mkdir(exist_ok=True)
        ts       = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = CONVERSATIONS_DIR / f"session_{self.session_id}_{ts}.json"
        with open(filename, "w") as f:
            json.dump({
                "session_id":    self.session_id,
                "role":          self.role,
                "client_type":   self.client_type,
                "created_at":    self.created_at,
                "saved_at":      datetime.utcnow().isoformat(),
                "message_count": self.message_count,
                "history":       [
                    # Strip base64 image data from saved history to keep files small
                    _strip_binary_content(msg) for msg in self.history
                ],
            }, f, indent=2, default=str)
        logger.info(f"[{self.session_id}] Saved → {filename}")
        return str(filename)

    def get_summary(self) -> dict:
        return {
            "session_id":     self.session_id,
            "role":           self.role,
            "client_type":    self.client_type,
            "created_at":     self.created_at,
            "message_count":  self.message_count,
            "history_length": len(self.history),
        }

    def _build_system_prompt(self) -> str:
        if self.role == "advisor":
            base = ADVISOR_SYSTEM_PROMPT
            profile = {
                "family_office":  "\nACTIVE PROFILE — Family Office: Prioritise alternative access, "
                                  "long-term wealth preservation, customisation, and transparency.",
                "institution":    "\nACTIVE PROFILE — Institutional: Prioritise rigorous quantitative "
                                  "analysis, scalable infrastructure, and complex alternative strategies.",
                "wealth_manager": "\nACTIVE PROFILE — Wealth Manager: Prioritise multi-asset solutions, "
                                  "business development support, and client relationship enablement.",
            }
            return base + profile.get(self.client_type, "")
        else:
            return CLIENT_SYSTEM_PROMPT

    def _trim_history(self):
        if len(self.history) > MAX_HISTORY_MSGS:
            excess = len(self.history) - MAX_HISTORY_MSGS
            self.history = self.history[excess:]
            logger.info(f"[{self.session_id}] Trimmed {excess} messages")


# ─────────────────────────────────────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────────────────────────────────────
def _strip_binary_content(message: dict) -> dict:
    """Remove base64 image/PDF data from saved history to keep JSON files small."""
    if not isinstance(message.get("content"), list):
        return message
    cleaned = []
    for block in message["content"]:
        if isinstance(block, dict):
            if block.get("type") in ("image", "document"):
                cleaned.append({"type": block["type"], "source": {"type": "base64", "data": "[binary removed]"}})
            else:
                cleaned.append(block)
        else:
            cleaned.append(block)
    return {**message, "content": cleaned}
