"""
models/lead.py
--------------
Core data model shared by every agent in the pipeline.
Only import Lead and the pain-category dicts from here — no agent logic lives here.
"""

from dataclasses import dataclass, field


# ── Pain-category taxonomy ────────────────────────────────────────────────────

PAIN_CATEGORIES: dict[str, str] = {
    "latency":     "real-time latency & performance",
    "cost":        "infrastructure cost & operational overhead",
    "lock_in":     "vendor lock-in & proprietary API risk",
    "scalability": "scaling bottlenecks at high data volume",
}

PAIN_KEYWORDS: dict[str, list[str]] = {
    "latency":     ["real-time", "millisecond", "latency", "fast", "instant",
                    "leaderboard", "live", "streaming"],
    "cost":        ["cost", "overhead", "budget", "spend", "pricing",
                    "license", "enterprise fee"],
    "lock_in":     ["datastax", "proprietary", "vendor", "migration",
                    "open-source", "cassandra"],
    "scalability": ["scale", "billion", "sensor", "iot", "massive",
                    "throughput", "volume", "high-velocity"],
}


# ── Lead dataclass ────────────────────────────────────────────────────────────

@dataclass
class Lead:
    # ── Identity ──────────────────────────────────────────────────────────────
    id: str
    first_name: str
    last_name: str
    name: str
    title: str
    email: str
    linkedin_url: str
    email_status: str           # "verified" | "guessed" | "unknown" | "none"

    # ── Company ───────────────────────────────────────────────────────────────
    company_name: str
    company_domain: str
    company_industry: str
    company_employees: int
    company_technologies: list[str]
    company_description: str
    company_signal_score: int   # DataStax signal score from Apollo (0-100)

    # ── Qualification (set by QualifierAgent) ─────────────────────────────────
    apollo_reachable: bool = True   # False when Apollo had no email/phone at search time
    qualification_score: int = 0
    qualification_reason: str = ""
    pain_category: str = ""     # key from PAIN_CATEGORIES
    pain_angle: str = ""        # human-readable pain description
    disqualified: bool = False
    disqualify_reason: str = ""

    # ── Generated copy (set by CopywriterAgent) ───────────────────────────────
    linkedin_invite: str = ""
    follow_up_email_subject: str = ""
    follow_up_email_body: str = ""
    message_type: str = "cold"   # "cold" | "second_touch" | "skipped"

    # ── QA result (set by QAAgent) ────────────────────────────────────────────
    qa_passed: bool = False
    qa_issues: list[str] = field(default_factory=list)
    copy_variants: list[dict] = field(default_factory=list)   # transient — not persisted
    qa_selected_variant: int = 0                              # 1-indexed; 0 = no selection

    # ── Outreach status (set by SenderAgent, updated externally) ─────────────
    status: str = "pending"              # pending | linkedin_sent | email_sent | response_received
    status_updated_at: str = ""

    # ── Pipeline metadata ─────────────────────────────────────────────────────
    run_id: str = ""
    processed_at: str = ""
