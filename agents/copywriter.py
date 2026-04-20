"""
agents/copywriter.py
--------------------
Agent 3 — CopywriterAgent

Responsibility:
  - For each qualified lead, generate a personalised LinkedIn invite and
    a follow-up email using the Claude API.
  - The pain_category detected by ResearcherAgent drives the copy angle,
    so each message is topically relevant to that lead's context.

Modes:
  LIVE  — Calls claude-sonnet-4-20250514 via the Anthropic API.
           Requires ANTHROPIC_API_KEY env var or --api-key CLI flag.
  MOCK  — Falls back to pre-written, pain-angle-specific templates.
           No API key needed; useful for demos and dry-run testing.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from models.lead import Lead
from utils.logging import make_logger


# ── Anthropic API config ──────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL      = "claude-sonnet-4-20250514"

# Resolved at import time; can be overridden by passing api_key= to __init__
_ENV_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert B2B sales copywriter for ScyllaDB — the high-performance,
open-source NoSQL database built on Apache Cassandra. ScyllaDB is dramatically
faster and cheaper than DataStax Enterprise / Astra DB, with no vendor lock-in.

Your task: write hyper-personalized outreach messages targeting engineers and
technical leaders who currently use DataStax. Messages must feel human, specific,
and respectful — NOT spammy.

ScyllaDB key advantages:
- 10x lower latency than DataStax at comparable workloads
- Open-source (Apache 2.0) — no enterprise licensing fees
- Drop-in Cassandra CQL compatible — easy migration path
- Handles millions of ops/sec on a fraction of the hardware
- Used by Discord, Comcast, Grab, and other high-scale companies

Tone: peer-to-peer, technically credible, brief, genuine.
Never use buzzwords like "leverage", "synergy", "game-changer", "revolutionize".
No exclamation marks in LinkedIn invites.
"""

# ── User prompt template ──────────────────────────────────────────────────────

USER_PROMPT_TEMPLATE = """\
Write outreach for this lead:

Name: {name}
Title: {title}
Company: {company_name} ({company_industry}, {company_employees} employees)
Technologies: {technologies}
Company context: {company_description}
Pain angle to focus on: {pain_angle}
LinkedIn URL: {linkedin_url}

Instructions:
1. LINKEDIN INVITE (STRICT: under 300 characters, plain text, no emojis):
   - Reference something specific about their company/stack
   - Mention ScyllaDB's relevance to their pain angle: {pain_angle}
   - End with a soft hook, not a hard CTA
   - Must feel like it was written by a fellow engineer

2. FOLLOW-UP EMAIL:
   Subject line: specific, curiosity-driven, no clickbait
   Body (3–4 short paragraphs):
   - Para 1: acknowledge their specific technical context
   - Para 2: one concrete ScyllaDB advantage tied to their pain angle
   - Para 3: a relevant proof point (real ScyllaDB customer if applicable)
   - Para 4: low-pressure CTA (15-min call, or just a question)
   Sign with: "— Alex Chen, Solutions Engineer @ ScyllaDB"

Respond ONLY with valid JSON (no markdown fences):
{{
  "linkedin_invite": "...",
  "email_subject": "...",
  "email_body": "..."
}}"""


# ── Mock copy templates (dry-run / no API key) ────────────────────────────────

SECOND_TOUCH_TEMPLATES: dict[str, dict] = {
    "latency": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — I reached out a while back about ScyllaDB's latency advantages "
            f"for {l.company_name}. Curious if anything has shifted with your DataStax setup since then — "
            f"happy to share what we've seen recently."
        ),
        "email_subject": lambda l: (
            f"Following up — new latency benchmarks relevant to {l.company_name.split()[0]}"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"I reached out a few months ago about ScyllaDB as an alternative to DataStax for "
            f"{l.company_name}'s real-time workloads. Wanted to circle back with something concrete.\n\n"
            f"Since then, we published new benchmarks showing ScyllaDB sustaining sub-millisecond p99 "
            f"latency at 2M ops/sec on commodity hardware — the kind of numbers that typically require "
            f"DataStax clusters 3x the size.\n\n"
            f"A few teams in {l.company_industry.lower()} have made the switch in the last quarter. "
            f"One reduced their node count by 60% while improving tail latency. Happy to share the "
            f"case study if useful.\n\n"
            f"Worth a 15-minute catch-up to see if the timing is better now?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
    "cost": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — following up on my earlier note about {l.company_name}'s DataStax "
            f"licensing costs. We've since put together a cost model for teams at your scale — "
            f"the numbers are pretty striking. Worth a look?"
        ),
        "email_subject": lambda l: (
            f"Updated cost model for {l.company_name.split()[0]} — DataStax vs ScyllaDB"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"I reached out previously about the licensing overhead that comes with DataStax Enterprise. "
            f"Since then I've put together a more detailed cost comparison tailored to companies at "
            f"{l.company_name}'s scale (~{l.company_employees} employees).\n\n"
            f"The short version: teams migrating from DataStax to ScyllaDB typically recover the "
            f"migration cost within the first renewal cycle — sometimes faster if they can right-size "
            f"the cluster at the same time.\n\n"
            f"I'd be happy to walk through the model with you. It's a 20-minute conversation and "
            f"you'd leave with a concrete number to bring to your next budget review.\n\n"
            f"Is this worth 20 minutes of your time?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
    "lock_in": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — checking back in after my earlier note on DataStax lock-in risk "
            f"for {l.company_name}. We've helped a few more teams execute zero-downtime migrations "
            f"since then. Happy to share what the process looks like now."
        ),
        "email_subject": lambda l: (
            f"Migration playbook update — relevant to {l.company_name.split()[0]}'s stack"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"A few months ago I flagged the growing lock-in risk around DataStax's proprietary "
            f"APIs. Since then, two more teams in {l.company_industry.lower()} have completed "
            f"migrations to ScyllaDB — both with zero application rewrites and zero downtime.\n\n"
            f"We've also updated our migration playbook based on those runs. It now covers the "
            f"trickier edge cases around Stargate and Astra-specific CQL extensions that tend to "
            f"catch teams off guard.\n\n"
            f"I thought of {l.company_name} specifically because your stack looked like a "
            f"straightforward migration candidate. Happy to do a quick compatibility assessment — "
            f"no commitment, just a technical read on what a move would involve.\n\n"
            f"Interested?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
    "scalability": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — following up on my earlier note about ScyllaDB's throughput "
            f"advantages for {l.company_name}'s data volume. We've added new reference architectures "
            f"for {l.company_industry.lower()} workloads — happy to share."
        ),
        "email_subject": lambda l: (
            f"New {l.company_industry.lower()} reference architecture — relevant to {l.company_name.split()[0]}"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"I reached out a while back about the scaling challenges that come with DataStax at "
            f"{l.company_name}'s data volumes. Wanted to follow up with something more concrete.\n\n"
            f"We've since published a reference architecture specifically for "
            f"{l.company_industry.lower()} workloads — covering shard-per-core tuning, compaction "
            f"strategies, and cluster sizing for high-velocity time-series data. It's based on "
            f"production deployments handling 5M+ writes/sec.\n\n"
            f"Given what {l.company_name} is managing, I think there are a few quick wins in there "
            f"worth talking through — even if you're not actively evaluating alternatives right now.\n\n"
            f"Want me to send the doc over, or set up a 20-minute architecture review?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
}

MOCK_COPY_TEMPLATES: dict[str, dict] = {
    "latency": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — saw {l.company_name} is running DataStax for real-time workloads. "
            f"We've seen teams cut p99 latency by 3–5x migrating to ScyllaDB. "
            f"Happy to share what that looked like. Worth a quick chat?"
        ),
        "email_subject": lambda l: (
            f"How {l.company_name.split()[0]} could shave 4ms off every {l.company_industry.lower()} query"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"Your work at {l.company_name} on {l.company_industry.lower()} infrastructure caught my "
            f"attention — anyone running DataStax at your scale knows the latency tax that comes with "
            f"the JVM GC pauses and the enterprise licensing overhead.\n\n"
            f"ScyllaDB is a drop-in Cassandra replacement (same CQL, same drivers) written in C++ — "
            f"no GC, no stop-the-world pauses. Teams in similar real-time workloads typically see "
            f"3–5x lower p99 latency on the same hardware footprint.\n\n"
            f"Discord moved their message store to ScyllaDB and cut their node count from 177 Cassandra "
            f"nodes to 72 ScyllaDB nodes while handling higher throughput. Similar story for Grab and Comcast.\n\n"
            f"Would a 15-minute call make sense to walk through what a migration would look like for "
            f"{l.company_name}'s stack?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
    "cost": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — {l.company_name}'s DataStax Enterprise footprint must come with a "
            f"painful renewal conversation every year. ScyllaDB is Apache 2.0 open-source — same "
            f"Cassandra CQL, no license fees. Worth 15 minutes?"
        ),
        "email_subject": lambda l: (
            f"What {l.company_name.split()[0]} is paying DataStax vs what it could cost"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"DataStax Enterprise licensing costs scale brutally as your data grows — and the value "
            f"proposition gets murkier once you're past the early adoption phase.\n\n"
            f"ScyllaDB is Apache 2.0 open-source. No per-node fees, no enterprise license renewals. "
            f"You get the same Cassandra CQL compatibility, so your existing drivers and tooling keep "
            f"working. Teams typically cut their database infrastructure spend by 40–60% in the first year.\n\n"
            f"We've helped several {l.company_industry} companies make this switch without a rewrite — "
            f"just a rolling migration using ScyllaDB's Cassandra-compatible interface.\n\n"
            f"I put together a quick cost comparison model for companies at {l.company_name}'s scale "
            f"(~{l.company_employees} employees). Happy to share it — want me to send it over?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
    "lock_in": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — noticed {l.company_name} is on DataStax. Proprietary APIs are a quiet "
            f"tax that compounds over time. ScyllaDB is open-source, Cassandra-compatible, and actively "
            f"developed. Happy to walk through what switching looks like."
        ),
        "email_subject": lambda l: (
            f"Escaping DataStax lock-in without rewriting {l.company_name.split()[0]}'s data layer"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"DataStax has been quietly expanding proprietary APIs — Stargate, Astra's Vector Search, "
            f"CQL extensions — which makes migration progressively harder over time. It's a well-worn playbook.\n\n"
            f"ScyllaDB is Apache 2.0 and implements standard CQL with no proprietary extensions you'd be "
            f"locked into. The migration path from DataStax Enterprise or Astra is well-documented — most "
            f"teams do it as a rolling replacement with zero downtime.\n\n"
            f"We recently helped a fintech company migrate 8TB of DataStax data to ScyllaDB in under two "
            f"weeks. They kept the same Cassandra drivers and application code untouched.\n\n"
            f"I'm curious — how tied is {l.company_name}'s stack to DataStax-specific features right now?\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
    "scalability": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — managing billions of data points at {l.company_name} must put real "
            f"pressure on your DataStax cluster. ScyllaDB handles that scale on significantly fewer nodes. "
            f"Happy to compare architectures."
        ),
        "email_subject": lambda l: (
            f"{l.company_name.split()[0]}'s data scale deserves a database built for it"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"At the scale {l.company_name} is operating — {l.company_industry.lower()} workloads typically "
            f"mean millions of writes per second and billions of records — DataStax clusters tend to get "
            f"expensive and operationally complex fast.\n\n"
            f"ScyllaDB's architecture (userspace I/O, shard-per-core design) means it saturates hardware "
            f"far more efficiently than DataStax's JVM-based stack. Comcast handles 1 million events/sec on "
            f"a 6-node ScyllaDB cluster that previously required 30 Cassandra nodes.\n\n"
            f"For IoT and high-throughput data pipelines specifically, we've seen 10x throughput improvements "
            f"on identical hardware — which translates directly to infrastructure cost and operational headcount.\n\n"
            f"Would it be useful to do a quick architecture review of your current DataStax setup? "
            f"Even 30 minutes often surfaces some quick wins.\n\n"
            f"— Alex Chen, Solutions Engineer @ ScyllaDB"
        ),
    },
}


# ── Agent ─────────────────────────────────────────────────────────────────────

class CopywriterAgent:
    """
    Generates personalised LinkedIn invite + follow-up email per qualified lead.

    Set api_key (or ANTHROPIC_API_KEY env var) to use the Claude API.
    Omit both for dry-run / demo mode with pain-angle mock templates.
    """

    log = make_logger("CopywriterAgent")

    RECONTACT_AFTER_DAYS = 180  # 6 months

    def __init__(self, api_key: str = "", db_path: str = "") -> None:
        self.api_key  = api_key or _ENV_API_KEY
        self.use_mock = not bool(self.api_key)
        self.db_path  = Path(db_path) if db_path else None
        if self.use_mock:
            self.log.info(
                "   ℹ️  No ANTHROPIC_API_KEY — using mock copy templates (dry-run mode)"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, leads: list[Lead]) -> list[Lead]:
        qualified = [l for l in leads if not l.disqualified]
        self.log.info(f"▶  Writing copy for {len(qualified)} qualified leads...")
        for lead in qualified:
            self._write_copy(lead)
        return leads

    # ── Private helpers ───────────────────────────────────────────────────────

    def _prior_contact_status(self, lead_id: str) -> str:
        """
        Check the DB for prior outreach to this lead.
        Returns "cold", "second_touch", or "skipped".
        """
        if not self.db_path or not self.db_path.exists():
            return "cold"
        try:
            with sqlite3.connect(self.db_path) as con:
                row = con.execute(
                    "SELECT processed_at FROM leads WHERE id = ? ORDER BY processed_at DESC LIMIT 1",
                    (lead_id,),
                ).fetchone()
        except sqlite3.OperationalError:
            return "cold"

        if row is None:
            return "cold"

        last_contact = datetime.fromisoformat(row[0])
        if last_contact.tzinfo is None:
            last_contact = last_contact.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last_contact
        if age.days < self.RECONTACT_AFTER_DAYS:
            return "skipped"
        return "second_touch"

    def _write_copy(self, lead: Lead) -> None:
        status = self._prior_contact_status(lead.id)
        lead.message_type = status

        if status == "skipped":
            self.log.info(
                f"   ⏭️  Skipping {lead.name} ({lead.company_name}) — contacted less than "
                f"{self.RECONTACT_AFTER_DAYS} days ago"
            )
            return

        if status == "second_touch":
            self.log.info(f"   🔁  [SECOND TOUCH] {lead.name} ({lead.company_name})")
            if self.use_mock:
                self._write_mock_copy(lead, templates=SECOND_TOUCH_TEMPLATES)
            else:
                self._write_live_copy(lead, second_touch=True)
        else:
            if self.use_mock:
                self._write_mock_copy(lead, templates=MOCK_COPY_TEMPLATES)
            else:
                self._write_live_copy(lead, second_touch=False)

    def _write_mock_copy(self, lead: Lead, templates: dict) -> None:
        self.log.info(f"   ✍️  [MOCK] {lead.name} ({lead.company_name})")
        cat  = lead.pain_category if lead.pain_category in templates else "latency"
        tmpl = templates[cat]
        lead.linkedin_invite          = tmpl["linkedin_invite"](lead)
        lead.follow_up_email_subject  = tmpl["email_subject"](lead)
        lead.follow_up_email_body     = tmpl["email_body"](lead)

    def _write_live_copy(self, lead: Lead, second_touch: bool = False) -> None:
        self.log.info(f"   ✍️  [LIVE] {lead.name} ({lead.company_name})")
        second_touch_note = (
            "\n\nIMPORTANT: This is a SECOND TOUCH — the lead was contacted 6+ months ago. "
            "Acknowledge the prior outreach briefly, offer something new (a benchmark, case study, "
            "or updated insight), and keep the tone warmer and less cold-intro."
            if second_touch else ""
        )
        prompt = USER_PROMPT_TEMPLATE.format(
            name=lead.name,
            title=lead.title,
            company_name=lead.company_name,
            company_industry=lead.company_industry,
            company_employees=lead.company_employees,
            technologies=", ".join(lead.company_technologies[:6]),
            company_description=lead.company_description,
            pain_angle=lead.pain_angle,
            linkedin_url=lead.linkedin_url,
        ) + second_touch_note
        try:
            response = httpx.post(
                ANTHROPIC_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 1000,
                    "system": SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            response.raise_for_status()
            raw = response.json()["content"][0]["text"].strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)

            lead.linkedin_invite         = parsed.get("linkedin_invite", "")
            lead.follow_up_email_subject = parsed.get("email_subject", "")
            lead.follow_up_email_body    = parsed.get("email_body", "")

        except Exception as exc:
            self.log.error(f"   ❌ Copy generation failed for {lead.name}: {exc}")
            lead.linkedin_invite         = "[GENERATION FAILED]"
            lead.follow_up_email_subject = "[GENERATION FAILED]"
            lead.follow_up_email_body    = str(exc)
