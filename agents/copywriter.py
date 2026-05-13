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
You are an expert B2B sales copywriter for YotamCo — the high-performance,
open-source NoSQL database built on Competitor. YotamCo is dramatically
faster and cheaper than Competitor, with no vendor lock-in.

Your task: write hyper-personalized outreach messages targeting engineers and
technical leaders who currently use Competitor. Messages must feel human, specific,
and respectful — NOT spammy.

YotamCo key advantages:
- 10x lower latency than Competitor at comparable workloads
- Open-source (Apache 2.0) — no enterprise licensing fees
- Drop-in standard-CQL compatible — easy migration path
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
   - Mention YotamCo's relevance to their pain angle: {pain_angle}
   - End with a soft hook, not a hard CTA
   - Must feel like it was written by a fellow engineer

2. FOLLOW-UP EMAIL:
   Subject line: specific, curiosity-driven, no clickbait
   Body (3–4 short paragraphs):
   - Para 1: acknowledge their specific technical context
   - Para 2: one concrete YotamCo advantage tied to their pain angle
   - Para 3: a relevant proof point (real YotamCo customer if applicable)
   - Para 4: low-pressure CTA (15-min call, or just a question)
   Sign with: "— Yotam Oppenheimer, Solutions Engineer @ YotamCo"

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
            f"Hi {l.first_name} — I reached out a while back about YotamCo's latency advantages "
            f"for {l.company_name}. Curious if anything has shifted with your Competitor setup since then — "
            f"happy to share what we've seen recently."
        ),
        "email_subject": lambda l: (
            f"Following up — new latency benchmarks relevant to {l.company_name.split()[0]}"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"I reached out a few months ago about YotamCo as an alternative to Competitor for "
            f"{l.company_name}'s real-time workloads. Following up with something concrete.\n\n"
            f"Since then, we published new benchmarks showing YotamCo sustaining sub-millisecond p99 "
            f"latency at 2M ops/sec on commodity hardware — the kind of numbers that typically require "
            f"Competitor clusters 3x the size.\n\n"
            f"A few teams in {l.company_industry.lower()} have made the switch in the last quarter. "
            f"One reduced their node count by 60% while improving tail latency. Happy to share the "
            f"case study if useful.\n\n"
            f"Worth a 15-minute catch-up to see if the timing is better now?\n\n"
            f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
        ),
    },
    "cost": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — following up on my earlier note about switching {l.company_name} "
            f"from Competitor to YotamCo. We've since put together a cost model for teams at your scale — "
            f"the numbers are pretty striking. Worth a look?"
        ),
        "email_subject": lambda l: (
            f"Updated cost model for {l.company_name.split()[0]} — Competitor vs YotamCo"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"I reached out previously about the licensing overhead that comes with Competitor. "
            f"Since then I've put together a more detailed cost comparison tailored to companies at "
            f"{l.company_name}'s scale (~{l.company_employees} employees).\n\n"
            f"The short version: teams migrating from Competitor to YotamCo typically recover the "
            f"migration cost within the first renewal cycle — sometimes faster if they can right-size "
            f"the cluster at the same time.\n\n"
            f"I'd be happy to walk through the model with you. It's a 20-minute conversation and "
            f"you'd leave with a concrete number to bring to your next budget review.\n\n"
            f"Is this worth 20 minutes of your time?\n\n"
            f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
        ),
    },
    "lock_in": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — checking back in after my earlier note on Competitor lock-in risk "
            f"for {l.company_name}. We've since helped more teams migrate to YotamCo with zero downtime — "
            f"happy to share what the process looks like now."
        ),
        "email_subject": lambda l: (
            f"Migration playbook update — relevant to {l.company_name.split()[0]}'s stack"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"A few months ago I flagged the growing lock-in risk around Competitor's proprietary "
            f"APIs. Since then, two more teams in {l.company_industry.lower()} have completed "
            f"migrations to YotamCo — both with zero application rewrites and zero downtime.\n\n"
            f"We've also updated our migration playbook based on those runs. It now covers the "
            f"trickier edge cases around CompetitorAPI and Competitor-specific CQL extensions that tend to "
            f"catch teams off guard.\n\n"
            f"I thought of {l.company_name} specifically because your stack looked like a "
            f"straightforward migration candidate. Happy to do a quick compatibility assessment — "
            f"no commitment, just a technical read on what a move would involve.\n\n"
            f"Interested?\n\n"
            f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
        ),
    },
    "scalability": {
        "linkedin_invite": lambda l: (
            f"Hi {l.first_name} — following up on my earlier note about YotamCo's throughput "
            f"advantages for {l.company_name}'s data volume. We've added new reference architectures "
            f"for {l.company_industry.lower()} workloads — happy to share."
        ),
        "email_subject": lambda l: (
            f"New {l.company_industry.lower()} reference architecture — relevant to {l.company_name.split()[0]}"
        ),
        "email_body": lambda l: (
            f"Hi {l.first_name},\n\n"
            f"I reached out a while back about the scaling challenges that come with Competitor at "
            f"{l.company_name}'s data volumes. Wanted to follow up with something more concrete.\n\n"
            f"We've since published a reference architecture specifically for "
            f"{l.company_industry.lower()} workloads — covering shard-per-core tuning, compaction "
            f"strategies, and cluster sizing for high-velocity time-series data. It's based on "
            f"production deployments handling 5M+ writes/sec.\n\n"
            f"Given what {l.company_name} is managing, I think there are a few quick wins in there "
            f"worth talking through — even if you're not actively evaluating alternatives right now.\n\n"
            f"Want me to send the doc over, or set up a 20-minute architecture review?\n\n"
            f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
        ),
    },
}

NUM_VARIANTS = 3  # number of copy variants generated per lead

# Each pain category has NUM_VARIANTS template dicts.
# Variant angles: [performance hook, technical detail, proof-point story]
MOCK_COPY_VARIANTS: dict[str, list[dict]] = {
    "latency": [
        {   # V1 — p99 performance hook
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — saw {l.company_name} is running Competitor for real-time workloads. "
                f"We've seen teams cut p99 latency by 3–5x migrating to YotamCo. "
                f"Happy to share what that looked like. Worth a quick chat?"
            ),
            "email_subject": lambda l: (
                f"How {l.company_name.split()[0]} could shave 4ms off every {l.company_industry.lower()} query"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Your work at {l.company_name} on {l.company_industry.lower()} infrastructure caught my "
                f"attention — anyone running Competitor at your scale knows the latency tax that comes with "
                f"the JVM GC pauses and the enterprise licensing overhead.\n\n"
                f"YotamCo is a drop-in legacy replacement (same CQL, same drivers) written in C++ — "
                f"no GC, no stop-the-world pauses. Teams in similar real-time workloads typically see "
                f"3–5x lower p99 latency on the same hardware footprint.\n\n"
                f"Discord moved their message store to YotamCo and cut their node count from 177 Competitor "
                f"nodes to 72 YotamCo nodes while handling higher throughput. Similar story for Grab and Comcast.\n\n"
                f"Would a 15-minute call make sense to walk through what a migration would look like for "
                f"{l.company_name}'s stack?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V2 — JVM GC technical angle
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — JVM GC pauses are the hidden latency tax every Competitor team pays. "
                f"YotamCo is C++ with no garbage collector — predictable sub-ms p99 at {l.company_name}'s scale. "
                f"Curious if GC spikes are a pain point for you."
            ),
            "email_subject": lambda l: (
                f"Eliminating JVM GC-induced latency spikes at {l.company_name.split()[0]}"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Every Competitor deployment running on the JVM carries the same risk: GC pauses of "
                f"50–200ms under load, showing up directly in p99 tail latency at the worst possible moment.\n\n"
                f"YotamCo is written in C++ with no garbage collector. Its shard-per-core model keeps "
                f"latency predictable under full load — teams typically see 3–5x improvement in tail latency "
                f"moving from Competitor to YotamCo on identical hardware.\n\n"
                f"A fintech company similar to {l.company_name} ran both clusters in parallel for 4 weeks. "
                f"Their p99 dropped from 18ms to 3ms. Happy to share the technical breakdown.\n\n"
                f"Worth a 20-minute call to walk through what this looks like for {l.company_name}'s workload?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V3 — Discord proof-point story
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — Discord replaced Competitor with YotamCo and cut their cluster "
                f"from 177 to 72 nodes while improving latency. Given {l.company_name}'s real-time "
                f"requirements, the architecture comparison is worth a look."
            ),
            "email_subject": lambda l: (
                f"What Discord's YotamCo migration means for {l.company_name.split()[0]}"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Discord's engineering post about replacing Competitor with YotamCo — 177 nodes to 72, "
                f"better read latency, higher throughput — is one of the cleaner public migration case "
                f"studies available. The performance difference comes from architecture: C++ instead of "
                f"JVM, shard-per-core, no GC.\n\n"
                f"For {l.company_name}, the math tends to work out similarly. If you're running Competitor "
                f"for {l.company_industry.lower()} workloads at scale, you're likely over-provisioned "
                f"relative to what YotamCo needs for the same SLA.\n\n"
                f"The migration path is a rolling node-by-node replacement — same CQL, same drivers, "
                f"no application rewrites, no bulk data exports.\n\n"
                f"Happy to do a quick architecture comparison — no commitment, just a technical read.\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
    ],
    "cost": [
        {   # V1 — license renewal hook
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — {l.company_name}'s Competitor footprint must come with a "
                f"painful renewal conversation every year. YotamCo is Apache 2.0 open-source — same "
                f"standard CQL, no license fees. Worth 15 minutes?"
            ),
            "email_subject": lambda l: (
                f"What {l.company_name.split()[0]} is paying Competitor vs what it could cost"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Competitor licensing costs scale brutally as your data grows — and the value "
                f"proposition gets murkier once you're past the early adoption phase.\n\n"
                f"YotamCo is Apache 2.0 open-source. No per-node fees, no enterprise license renewals. "
                f"You get the same standard CQL compatibility, so your existing drivers and tooling keep "
                f"working. Teams typically cut their database infrastructure spend by 40–60% in the first year.\n\n"
                f"We've helped several {l.company_industry} companies make this switch without a rewrite — "
                f"just a rolling migration using YotamCo's standard-compatible interface.\n\n"
                f"I put together a quick cost comparison model for companies at {l.company_name}'s scale "
                f"(~{l.company_employees} employees). Happy to share it — want me to send it over?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V2 — node reduction / hardware savings
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — Competitor licensing scales with node count. YotamCo is open-source "
                f"and typically needs 2–3x fewer nodes for the same workload at {l.company_name}. "
                f"The savings compound quickly."
            ),
            "email_subject": lambda l: (
                f"Running {l.company_name.split()[0]}'s workload on half the nodes"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Most Competitor deployments are over-provisioned — the JVM overhead means extra headroom "
                f"to absorb GC pauses and thread contention. YotamCo's C++ architecture saturates hardware "
                f"far more efficiently, typically translating to a 50–70% node reduction.\n\n"
                f"For {l.company_name} at ~{l.company_employees} employees, that's fewer nodes to patch, "
                f"monitor, and scale — operational simplicity that compounds over time.\n\n"
                f"And since YotamCo is Apache 2.0 open-source, you drop the per-node licensing cost on "
                f"top of the hardware savings. Teams in {l.company_industry.lower()} have seen total "
                f"database spend drop 50–60% in the first renewal cycle.\n\n"
                f"Would it be useful to run a quick sizing exercise for {l.company_name}'s workload?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V3 — total cost of ownership / OpEx
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — the real cost of Competitor isn't just the license, it's the "
                f"operational overhead. YotamCo's self-tuning architecture reduces DBA time significantly. "
                f"Relevant for {l.company_name}?"
            ),
            "email_subject": lambda l: (
                f"{l.company_name.split()[0]}'s Competitor total cost — there's a lower floor"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"When teams calculate Competitor costs, they focus on the license fee. But the operational "
                f"overhead is often bigger: tuning JVM heap settings, managing compaction, handling "
                f"GC-related incidents, and provisioning headroom for traffic spikes.\n\n"
                f"YotamCo's shard-per-core architecture is largely self-tuning — it adapts to workload "
                f"changes without manual intervention. Teams that migrate typically report a meaningful "
                f"reduction in database-related incidents and on-call burden.\n\n"
                f"Combined with the licensing savings (Apache 2.0 open-source), the total cost reduction "
                f"for {l.company_name}-scale deployments is usually 50–60% across hardware, licenses, "
                f"and engineering time.\n\n"
                f"Happy to walk through a TCO comparison tailored to {l.company_name}'s setup — "
                f"takes about 20 minutes.\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
    ],
    "lock_in": [
        {   # V1 — proprietary API risk
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — noticed {l.company_name} is on Competitor. Proprietary APIs are a "
                f"quiet tax that compounds over time. YotamCo is open-source, standard-compatible, and "
                f"actively developed. Happy to walk through what switching looks like."
            ),
            "email_subject": lambda l: (
                f"Escaping Competitor lock-in without rewriting {l.company_name.split()[0]}'s data layer"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Competitor has been quietly expanding proprietary APIs — CompetitorAPI, Competitor's Vector Search, "
                f"CQL extensions — which makes migration progressively harder over time.\n\n"
                f"YotamCo is Apache 2.0 and implements standard CQL with no proprietary extensions. "
                f"The migration path from Competitor or Astra is well-documented — most teams "
                f"do it as a rolling replacement with zero downtime.\n\n"
                f"We recently helped a fintech company migrate 8TB of Competitor data to YotamCo in under "
                f"two weeks. They kept the same existing drivers and application code untouched.\n\n"
                f"I'm curious — how tied is {l.company_name}'s stack to Competitor-specific features?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V2 — open-source freedom angle
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — YotamCo is Apache 2.0, standard CQL, no proprietary extensions. "
                f"{l.company_name} could migrate from Competitor with the same application code and drivers. "
                f"Worth a quick architecture chat?"
            ),
            "email_subject": lambda l: (
                f"Open-source path out of Competitor — no rewrite for {l.company_name.split()[0]}"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"When your database is Apache 2.0 and CQL-compatible, you're never at the mercy of an "
                f"enterprise vendor's pricing or roadmap decisions.\n\n"
                f"YotamCo is exactly that — fully open-source, wire-compatible, actively "
                f"developed. No proprietary query language, no special API to untangle later.\n\n"
                f"For {l.company_name}, the migration is a rolling swap at the driver level — no "
                f"application rewrites, no new data model. Teams in {l.company_industry.lower()} "
                f"typically complete the transition in 2–4 weeks.\n\n"
                f"Happy to share our migration runbook — it answers most of the 'how hard is this "
                f"really?' questions upfront.\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V3 — zero-downtime migration story
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — the main concern teams raise about leaving Competitor is migration "
                f"risk. YotamCo supports zero-downtime rolling migrations from Competitor. "
                f"Happy to walk through what that looks like for {l.company_name}."
            ),
            "email_subject": lambda l: (
                f"Zero-downtime Competitor exit — what it looks like for {l.company_name.split()[0]}"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"The most common objection to leaving Competitor isn't cost or performance — it's migration "
                f"risk. 'We can't afford downtime' is a reasonable concern for production "
                f"{l.company_industry.lower()} workloads.\n\n"
                f"YotamCo's Competitor compatibility makes this tractable: YotamCo nodes join the existing "
                f"ring as legacy replacements, data streams over via standard replication, and you cut "
                f"over application traffic incrementally. No bulk exports, no maintenance windows.\n\n"
                f"We've done this with teams running multi-TB clusters with zero downtime. The trickiest "
                f"part is usually Competitor-specific CQL extensions — and we have a compatibility checker "
                f"that flags those upfront.\n\n"
                f"Want me to run the compatibility check against {l.company_name}'s schema? "
                f"It takes about 10 minutes.\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
    ],
    "scalability": [
        {   # V1 — node count / cluster size
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — managing billions of data points at {l.company_name} must put real "
                f"pressure on your Competitor cluster. YotamCo handles that scale on significantly fewer "
                f"nodes. Happy to compare architectures."
            ),
            "email_subject": lambda l: (
                f"{l.company_name.split()[0]}'s data scale deserves a database built for it"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"At the scale {l.company_name} is operating — {l.company_industry.lower()} workloads "
                f"typically mean millions of writes per second and billions of records — Competitor clusters "
                f"tend to get expensive and operationally complex fast.\n\n"
                f"YotamCo's architecture (userspace I/O, shard-per-core design) saturates hardware far "
                f"more efficiently than Competitor's JVM-based stack. Comcast handles 1 million events/sec "
                f"on a 6-node YotamCo cluster that previously required 30 legacy nodes.\n\n"
                f"For high-throughput data pipelines specifically, we've seen 10x throughput improvements "
                f"on identical hardware — translating directly to infrastructure cost savings.\n\n"
                f"Would it be useful to do a quick architecture review of your current Competitor setup?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V2 — write throughput ceiling
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — at {l.company_name}'s data volume, Competitor clusters start "
                f"hitting their write throughput ceiling. YotamCo sustains millions of writes/sec per "
                f"node with predictable tail latency. Worth comparing?"
            ),
            "email_subject": lambda l: (
                f"Sustaining {l.company_name.split()[0]}'s write throughput without adding nodes"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Write-heavy {l.company_industry.lower()} workloads are where Competitor tends to struggle "
                f"most — the JVM write path isn't designed for sustained high-velocity ingestion without "
                f"careful heap tuning and frequent compaction management.\n\n"
                f"YotamCo's LSM implementation in C++ handles write-heavy workloads natively: no heap "
                f"pressure, no GC interference, automatic compaction that doesn't compete with write I/O. "
                f"Teams at {l.company_name}'s scale typically see 5–10x write throughput improvement on "
                f"identical hardware.\n\n"
                f"Grab processes over 1 million writes/sec on YotamCo with p99 under 10ms — a workload "
                f"profile similar to {l.company_industry.lower()} platforms at your scale.\n\n"
                f"Happy to share the architecture details — would a short call make sense?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
        {   # V3 — Comcast proof-point story
            "linkedin_invite": lambda l: (
                f"Hi {l.first_name} — Comcast runs 1M events/sec on 6 YotamCo nodes; the equivalent "
                f"legacy cluster needed 30. For {l.company_name}'s data scale, the hardware math "
                f"works out similarly. Happy to walk through it."
            ),
            "email_subject": lambda l: (
                f"The Comcast scaling story — applicable to {l.company_name.split()[0]}?"
            ),
            "email_body": lambda l: (
                f"Hi {l.first_name},\n\n"
                f"Comcast built their xFi gateway telemetry platform on YotamCo — 1 million events/sec, "
                f"6 nodes, sub-10ms p99. The comparable Competitor deployment needed 30 nodes. The "
                f"difference is architectural: shard-per-core eliminates coordination overhead that limits "
                f"Competitor's per-node throughput.\n\n"
                f"For {l.company_name} operating at {l.company_industry.lower()} scale, the math tends "
                f"to work out similarly — fewer nodes, lower cost, better tail latency under peak load. "
                f"And since YotamCo is Apache 2.0 open-source, you drop the Competitor licensing cost too.\n\n"
                f"The migration is a rolling cluster replacement — same CQL, no application rewrites. "
                f"Most teams complete it in 2–4 weeks.\n\n"
                f"Would it be worth doing a quick cluster sizing comparison for {l.company_name}'s workload?\n\n"
                f"— Yotam Oppenheimer, Solutions Engineer @ YotamCo"
            ),
        },
    ],
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

    def _prior_contact_status(self, lead_id: str) -> tuple[str, str, str]:
        """
        Check the DB for prior outreach to this lead.
        Returns (message_type, status_updated_at, db_status) where message_type is
        "cold", "second_touch", or "skipped" and db_status is the lead's current DB status.
        """
        if not self.db_path or not self.db_path.exists():
            return "cold", "", ""
        try:
            with sqlite3.connect(self.db_path) as con:
                row = con.execute(
                    "SELECT processed_at, status_updated_at, status, "
                    "follow_up_email_subject, follow_up_email_body "
                    "FROM leads WHERE id = ? ORDER BY processed_at DESC LIMIT 1",
                    (lead_id,),
                ).fetchone()
        except sqlite3.OperationalError:
            return "cold", "", "", "", ""

        if row is None:
            return "cold", "", "", "", ""

        last_contact = datetime.fromisoformat(row[0])
        if last_contact.tzinfo is None:
            last_contact = last_contact.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last_contact
        status_updated_at    = row[1] or ""
        db_status            = row[2] or ""
        followup_subject     = row[3] or ""
        followup_body        = row[4] or ""
        if age.days < self.RECONTACT_AFTER_DAYS:
            return "skipped", status_updated_at, db_status, followup_subject, followup_body
        return "second_touch", status_updated_at, db_status, followup_subject, followup_body

    def _write_copy(self, lead: Lead) -> None:
        message_type, status_updated_at, db_status, followup_subject, followup_body = \
            self._prior_contact_status(lead.id)
        lead.message_type = message_type

        if message_type == "skipped":
            lead.status_updated_at = status_updated_at
            # Restore persisted status and copy so reporter renders correct badge + content
            if db_status:
                lead.status = db_status
            if followup_subject:
                lead.follow_up_email_subject = followup_subject
            if followup_body:
                lead.follow_up_email_body = followup_body
            self.log.info(
                f"   ⏭️  Skipping {lead.name} ({lead.company_name}) — contacted less than "
                f"{self.RECONTACT_AFTER_DAYS} days ago"
            )
            return

        if message_type == "second_touch":
            self.log.info(f"   🔁  [SECOND TOUCH] {lead.name} ({lead.company_name})")
            if self.use_mock:
                variants = self._generate_mock_variants(lead, SECOND_TOUCH_TEMPLATES)
            else:
                variants = self._generate_live_variants(lead, second_touch=True)
        else:
            self.log.info(f"   ✍️  [{'MOCK' if self.use_mock else 'LIVE'}] {lead.name} ({lead.company_name})")
            if self.use_mock:
                variants = self._generate_mock_variants(lead, MOCK_COPY_VARIANTS)
            else:
                variants = self._generate_live_variants(lead, second_touch=False)

        self._apply_variants(lead, variants)
        self.log.info(f"      {len(variants)} variant(s) generated — QA will select the best")

    def _generate_mock_variants(self, lead: Lead, templates: dict) -> list[dict]:
        """Return a list of copy variant dicts from mock templates.

        templates can be:
          MOCK_COPY_VARIANTS  — dict[str, list[dict]]  (3 variants per category)
          SECOND_TOUCH_TEMPLATES — dict[str, dict]     (1 template per category)
        """
        cat   = lead.pain_category if lead.pain_category in templates else "latency"
        value = templates[cat]
        # Normalise to a list regardless of template structure
        tmpl_list = value if isinstance(value, list) else [value]

        return [
            {
                "linkedin_invite":         t["linkedin_invite"](lead),
                "follow_up_email_subject": t["email_subject"](lead),
                "follow_up_email_body":    t["email_body"](lead),
            }
            for t in tmpl_list
        ]

    def _call_claude(self, prompt: str) -> dict:
        """Single Claude API call; returns parsed copy dict."""
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
        return {
            "linkedin_invite":         parsed.get("linkedin_invite", ""),
            "follow_up_email_subject": parsed.get("email_subject", ""),
            "follow_up_email_body":    parsed.get("email_body", ""),
        }

    def _generate_live_variants(self, lead: Lead, second_touch: bool = False) -> list[dict]:
        """Generate NUM_VARIANTS copy variants via the Claude API."""
        second_touch_note = (
            "\n\nIMPORTANT: This is a SECOND TOUCH — the lead was contacted 6+ months ago. "
            "Acknowledge the prior outreach briefly, offer something new (a benchmark, case study, "
            "or updated insight), and keep the tone warmer and less cold-intro."
            if second_touch else ""
        )
        base_prompt = USER_PROMPT_TEMPLATE.format(
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

        angle_hints = [
            "",
            "\n\nFor this variant, lead with a specific technical detail (e.g. GC pauses, node count, "
            "or write throughput) rather than a general pitch.",
            "\n\nFor this variant, open with a concrete customer proof point (Discord, Comcast, or Grab) "
            "and frame everything around that story.",
        ]

        variants: list[dict] = []
        for i in range(NUM_VARIANTS):
            try:
                variant = self._call_claude(base_prompt + angle_hints[i % len(angle_hints)])
                variants.append(variant)
            except Exception as exc:
                self.log.warning(f"   ⚠️  Variant {i + 1} generation failed for {lead.name}: {exc}")
                if not variants:
                    variants.append({
                        "linkedin_invite":         "[GENERATION FAILED]",
                        "follow_up_email_subject": "[GENERATION FAILED]",
                        "follow_up_email_body":    str(exc),
                    })
        return variants

    def _apply_variants(self, lead: Lead, variants: list[dict]) -> None:
        """Store variants on the lead and set copy fields from variant 1 as default."""
        lead.copy_variants           = variants
        lead.linkedin_invite         = variants[0]["linkedin_invite"]
        lead.follow_up_email_subject = variants[0]["follow_up_email_subject"]
        lead.follow_up_email_body    = variants[0]["follow_up_email_body"]
