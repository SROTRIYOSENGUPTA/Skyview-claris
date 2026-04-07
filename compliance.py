"""
SkyView Investment Advisors LLC
Claris Multi-Persona Platform — Compliance & Policy Engine

Enforces SEC Marketing Rule 206(4)-1 compliance across all AI-generated
responses. Operates as both a system prompt injector (pre-processing) and
an output filter (post-processing) with full audit trail.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from models import ComplianceLog, Conversation

logger = logging.getLogger("skyview.compliance")


# ─────────────────────────────────────────────────────────────────────────────
# PROHIBITED LANGUAGE PATTERNS (SEC Marketing Rule 206(4)-1)
# ─────────────────────────────────────────────────────────────────────────────

PROHIBITED_PATTERNS = [
    # Guarantees and absolute promises
    (r"\bguaranteed?\b", "prohibited_language", "high",
     "Cannot guarantee investment outcomes"),
    (r"\brisk[\s-]*free\b", "prohibited_language", "high",
     "Cannot describe investments as risk-free"),
    (r"\bno[\s-]*risk\b", "prohibited_language", "high",
     "Cannot claim no risk"),
    (r"\bsafe\s+investment\b", "prohibited_language", "medium",
     "Avoid characterizing investments as 'safe'"),
    (r"\bcannot\s+lose\b", "prohibited_language", "high",
     "Cannot claim investments cannot lose value"),
    (r"\bsure\s+thing\b", "prohibited_language", "high",
     "Cannot characterize investments as sure things"),

    # Superlatives in investment context
    (r"\bbest\s+(investment|fund|strategy|return)", "prohibited_language", "medium",
     "Avoid superlatives when describing investment products"),
    (r"\btop[\s-]*(performing|rated)\b", "prohibited_language", "medium",
     "Avoid superlative rankings without proper context"),
    (r"\bnumber\s*one\s+(fund|strategy|manager)\b", "prohibited_language", "medium",
     "Avoid unsubstantiated ranking claims"),

    # Promissory language
    (r"\bwill\s+(generate|produce|deliver|earn|return)\s+\d", "prohibited_language", "high",
     "Cannot promise specific return figures"),
    (r"\bexpect(?:ed)?\s+return\s+of\s+\d+%", "prohibited_language", "medium",
     "Projected returns require proper disclaimers"),

    # Misleading performance claims
    (r"\balways\s+(outperform|beat|exceed)", "prohibited_language", "high",
     "Cannot claim consistent outperformance"),
    (r"\bnever\s+(lost|lose|underperform)", "prohibited_language", "high",
     "Cannot claim zero losses"),
]

# Patterns that require a disclaimer if detected
DISCLAIMER_TRIGGERS = [
    (r"\bpast\s+performance\b", "Past performance does not guarantee future results."),
    (r"\bhistorical\s+(return|performance|data)\b",
     "Past performance does not guarantee future results. Historical data is provided for reference only."),
    (r"\b\d+%\s+(return|gain|yield|annualized)\b",
     "Past performance does not guarantee future results. Returns shown are not indicative of future performance."),
    (r"\bbacktest", "Backtested results do not represent actual trading and may not reflect the impact of material economic factors. Past performance does not guarantee future results."),
]

# PII patterns to catch
PII_PATTERNS = [
    (r"\b\d{3}[-.]?\d{2}[-.]?\d{4}\b", "pii_detected", "critical",
     "Possible SSN detected in output"),
    (r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "pii_detected", "critical",
     "Possible credit card number detected"),
    (r"\b[A-Z]{2}\d{2}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{4}[\s]?\d{2}\b",
     "pii_detected", "critical", "Possible IBAN detected"),
]

# Standard disclaimers
INVESTMENT_DISCLAIMER = (
    "\n\n---\n*For investment decisions specific to your situation, "
    "please consult a licensed SkyView investment professional. "
    "Past performance does not guarantee future results.*"
)

GENERAL_DISCLAIMER = (
    "\n\n— SkyView Investment Advisors LLC"
)


# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE DIRECTIVES (Layer 4 — injected into system prompt)
# ─────────────────────────────────────────────────────────────────────────────

COMPLIANCE_DIRECTIVES = """
══════════════════════════════════════════════════════
LAYER 4 — COMPLIANCE & POLICY DIRECTIVES
══════════════════════════════════════════════════════

MANDATORY COMPLIANCE RULES (SEC Marketing Rule 206(4)-1):
You MUST follow these rules in EVERY response. Violations may result in
regulatory action against SkyView Investment Advisors.

1. NEVER guarantee or promise specific investment returns.
2. NEVER use words like "guaranteed", "risk-free", "safe investment",
   "cannot lose", "sure thing" in investment context.
3. NEVER use superlatives ("best fund", "top performing", "#1 strategy")
   without proper third-party attribution and time period.
4. ALWAYS include "Past performance does not guarantee future results"
   when discussing historical performance data.
5. NEVER provide personalized investment recommendations (e.g., "You should
   buy X security"). Frame analysis as considerations and options.
6. When presenting performance data, always specify the time period and
   benchmark comparison.
7. NEVER disclose client personal information (SSNs, account numbers, etc.)
8. If a query requires specific investment advice for a particular situation,
   recommend speaking with a SkyView investment professional.
9. Flag queries about restricted securities, material non-public information,
   or topics requiring human compliance review.

ESCALATION TRIGGERS — respond with referral to human advisor:
• Requests involving specific security recommendations for a client
• Questions about insider information or material non-public data
• Complaints or legal matters
• Requests to execute trades or move money
• Any topic you are uncertain about from a compliance perspective

RESPONSE SIGNING:
• End formal communications with: — SkyView Investment Advisors LLC
""".strip()


# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceEngine:
    """
    Pre- and post-processing compliance engine for AI advisor responses.
    """

    def __init__(self, db: Optional[DBSession] = None):
        self.db = db

    def get_directives(self) -> str:
        """Return the Layer 4 compliance directives for system prompt injection."""
        return COMPLIANCE_DIRECTIVES

    def check_response(
        self,
        response_text: str,
        conversation_id: str = None,
        employee_id: str = None,
    ) -> dict:
        """
        Run all compliance checks on a generated response.

        Returns:
            {
                "flags": [{"type": str, "severity": str, "description": str, "match": str}],
                "corrected_text": str or None,
                "disclaimers_added": [str],
            }
        """
        flags = []
        corrected_text = response_text
        disclaimers_added = []

        # 1. Check for prohibited language
        for pattern, flag_type, severity, description in PROHIBITED_PATTERNS:
            matches = re.findall(pattern, corrected_text, re.IGNORECASE)
            if matches:
                for match in matches:
                    flags.append({
                        "type": flag_type,
                        "severity": severity,
                        "description": description,
                        "match": match,
                    })
                    logger.warning(
                        f"Compliance flag [{severity}]: {description} — "
                        f"matched '{match}'"
                    )

        # 2. Check for PII
        for pattern, flag_type, severity, description in PII_PATTERNS:
            matches = re.findall(pattern, corrected_text)
            if matches:
                for match in matches:
                    flags.append({
                        "type": flag_type,
                        "severity": severity,
                        "description": description,
                        "match": "[REDACTED]",
                    })
                    # Redact PII from response
                    corrected_text = corrected_text.replace(match, "[REDACTED]")
                    logger.critical(f"PII detected and redacted: {description}")

        # 3. Check if disclaimers are needed
        has_investment_disclaimer = "past performance" in corrected_text.lower()
        for pattern, disclaimer in DISCLAIMER_TRIGGERS:
            if re.search(pattern, corrected_text, re.IGNORECASE):
                if not has_investment_disclaimer:
                    corrected_text += f"\n\n*{disclaimer}*"
                    disclaimers_added.append(disclaimer)
                    has_investment_disclaimer = True
                    flags.append({
                        "type": "missing_disclaimer",
                        "severity": "low",
                        "description": f"Auto-injected disclaimer: {disclaimer[:50]}...",
                        "match": "",
                    })

        # 4. Log compliance flags to database
        if flags and self.db and conversation_id:
            self._log_flags(flags, conversation_id, employee_id)

        result = {
            "flags": flags,
            "corrected_text": corrected_text if corrected_text != response_text else None,
            "disclaimers_added": disclaimers_added,
        }

        if flags:
            high_count = sum(1 for f in flags if f["severity"] in ("high", "critical"))
            logger.info(
                f"Compliance check: {len(flags)} flags "
                f"({high_count} high/critical)"
            )

        return result

    def should_escalate(self, user_message: str) -> bool:
        """
        Determine if a user query should be escalated to a human advisor.
        Returns True if the query matches escalation patterns.
        """
        escalation_patterns = [
            r"\b(buy|sell|trade)\s+(this|the|that)\s+(stock|security|bond|fund)\b",
            r"\b(insider|non[\s-]?public)\s+(information|data|knowledge)\b",
            r"\b(complaint|lawsuit|legal\s+action|sue)\b",
            r"\b(transfer|wire|move)\s+(money|funds|assets)\b",
            r"\brestricted\s+(security|securities|list)\b",
            r"\b(execute|place)\s+(a\s+)?(trade|order|transaction)\b",
        ]

        for pattern in escalation_patterns:
            if re.search(pattern, user_message, re.IGNORECASE):
                logger.info(f"Escalation triggered by pattern: {pattern}")
                return True

        return False

    def _log_flags(self, flags: list, conversation_id: str, employee_id: str = None):
        """Persist compliance flags to the database."""
        if not self.db:
            return

        try:
            for flag in flags:
                log = ComplianceLog(
                    conversation_id=conversation_id,
                    employee_id=employee_id,
                    flag_type=flag["type"],
                    severity=flag["severity"],
                    description=flag["description"],
                    original_text=flag.get("match", ""),
                )
                self.db.add(log)
            self.db.commit()
        except Exception as e:
            logger.error(f"Failed to log compliance flags: {e}")
            self.db.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def quick_compliance_check(text: str) -> list[dict]:
    """
    Run a quick compliance check without database logging.
    Returns list of flags.
    """
    engine = ComplianceEngine(db=None)
    result = engine.check_response(text)
    return result["flags"]


def get_compliance_summary(db: DBSession, days: int = 30) -> dict:
    """
    Generate a compliance summary for the admin dashboard.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    total = db.query(ComplianceLog).filter(
        ComplianceLog.created_at >= cutoff
    ).count()

    by_severity = {}
    for severity in ("low", "medium", "high", "critical"):
        count = db.query(ComplianceLog).filter(
            ComplianceLog.created_at >= cutoff,
            ComplianceLog.severity == severity,
        ).count()
        by_severity[severity] = count

    by_type = {}
    for flag_type in ("prohibited_language", "missing_disclaimer", "pii_detected",
                      "hallucination_suspect", "tone_deviation", "escalation_triggered"):
        count = db.query(ComplianceLog).filter(
            ComplianceLog.created_at >= cutoff,
            ComplianceLog.flag_type == flag_type,
        ).count()
        if count > 0:
            by_type[flag_type] = count

    unresolved = db.query(ComplianceLog).filter(
        ComplianceLog.resolved == False,
        ComplianceLog.severity.in_(["high", "critical"]),
    ).count()

    return {
        "period_days": days,
        "total_flags": total,
        "by_severity": by_severity,
        "by_type": by_type,
        "unresolved_high_critical": unresolved,
    }
