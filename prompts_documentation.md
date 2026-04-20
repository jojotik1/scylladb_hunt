# ScyllaDB GTM Hunter — Prompts & Agents Documentation

> This document covers every prompt, agent, and decision logic used in the pipeline.
> Intended for reviewers who want to understand how the AI-driven personalization works.

---

## Pipeline Overview

```
Apollo (mock/live)
      │
      ▼
Agent 1 — ResearcherAgent      fetch leads + detect pain category
      │
      ▼
Agent 2 — QualifierAgent       score leads, hard-disqualify ineligible ones
      │
      ▼
Agent 3 — CopywriterAgent      generate LinkedIn invite + follow-up email
      │                        (Claude API in live mode, templates in mock mode)
      ▼
Agent 4 — QAAgent              deterministic quality gate
      │
      ▼
Agent 5 — SenderAgent          dry-run trigger — logs what would be sent
      │
      ▼
Agent 6 — ReporterAgent        persist to SQLite + render HTML report
```

---

## Agent 1 — ResearcherAgent

**File:** `agents/researcher.py`

No LLM involved. Pure logic:

1. Calls Apollo's `run_full_pipeline()` (mock or live).
2. Merges tech stacks from person and company records.
3. Detects the dominant **pain category** per lead via keyword scoring.

### Pain Category Detection

Each lead's company description, tech stack, and industry are concatenated and lowercased. Each pain category's keywords are scored by hit count. The highest-scoring category wins.

| Category | Keywords |
|---|---|
| `latency` | real-time, millisecond, latency, fast, instant, leaderboard, live, streaming |
| `cost` | cost, overhead, budget, spend, pricing, license, enterprise fee |
| `lock_in` | datastax, proprietary, vendor, migration, open-source, cassandra |
| `scalability` | scale, billion, sensor, iot, massive, throughput, volume, high-velocity |

The pain category drives which copy template or Claude prompt angle is used downstream.

---

## Agent 2 — QualifierAgent

**File:** `agents/qualifier.py`

No LLM involved. 20-point scoring rubric + hard disqualifiers.

### Hard Disqualifiers (instant reject, no score computed)

**Title contains:** `intern`, `junior`, `jr.`, `associate engineer`, `entry`

**Company name contains:** `datastax`, `astra db`, `apache cassandra project`

### Scoring Rubric

| Signal | Logic | Max pts |
|---|---|---|
| Seniority | CTO=6, VP=5, Head of=5, Director=4, Principal/Staff/Manager=3, Senior=1 | 6 |
| Company size | ≥1000→4, ≥500→3, ≥200→2, ≥50→1 | 4 |
| DataStax signal score | `min(4, signal_score // 25)` | 4 |
| Email status | verified=3, guessed=1 | 3 |
| LinkedIn URL present | +2 if present | 2 |
| DataStax/Cassandra tech detected | +1 if found in tech stack | 1 |

**Pass threshold: ≥ 12 points.** Leads below threshold are marked disqualified with their score and reason stored in the DB.

---

## Agent 3 — CopywriterAgent

**File:** `agents/copywriter.py`

**Model:** `claude-sonnet-4-20250514` (live mode)

Runs in two modes:

- **LIVE** — Calls the Anthropic API with a system prompt + per-lead user prompt. Requires `ANTHROPIC_API_KEY`.
- **MOCK** — Uses pre-written pain-angle templates. No API key needed.

Also checks the DB before generating copy:

- **< 6 months since last contact** → skip, no message generated (`message_type = skipped`)
- **≥ 6 months since last contact** → second-touch message (`message_type = second_touch`)
- **Never contacted** → cold outreach (`message_type = cold`)

---

### Live Mode — System Prompt

```
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
```

---

### Live Mode — User Prompt Template

```
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
{
  "linkedin_invite": "...",
  "email_subject": "...",
  "email_body": "..."
}
```

For **second-touch** calls, the following note is appended to the user prompt:

```
IMPORTANT: This is a SECOND TOUCH — the lead was contacted 6+ months ago.
Acknowledge the prior outreach briefly, offer something new (a benchmark, case study,
or updated insight), and keep the tone warmer and less cold-intro.
```

---

### Mock Mode — Cold Outreach Templates

Pre-written templates, one per pain category. Each is a Python lambda that fills in
lead-specific fields (`first_name`, `company_name`, `company_industry`, etc.).

#### latency

**LinkedIn Invite:**
> Hi {first_name} — saw {company_name} is running DataStax for real-time workloads. We've seen teams cut p99 latency by 3–5x migrating to ScyllaDB. Happy to share what that looked like. Worth a quick chat?

**Email Subject:**
> How {company} could shave 4ms off every {industry} query

**Email Body:**
> Hi {first_name},
>
> Your work at {company_name} on {industry} infrastructure caught my attention — anyone running DataStax at your scale knows the latency tax that comes with the JVM GC pauses and the enterprise licensing overhead.
>
> ScyllaDB is a drop-in Cassandra replacement (same CQL, same drivers) written in C++ — no GC, no stop-the-world pauses. Teams in similar real-time workloads typically see 3–5x lower p99 latency on the same hardware footprint.
>
> Discord moved their message store to ScyllaDB and cut their node count from 177 Cassandra nodes to 72 ScyllaDB nodes while handling higher throughput. Similar story for Grab and Comcast.
>
> Would a 15-minute call make sense to walk through what a migration would look like for {company_name}'s stack?
>
> — Alex Chen, Solutions Engineer @ ScyllaDB

---

#### cost

**LinkedIn Invite:**
> Hi {first_name} — {company_name}'s DataStax Enterprise footprint must come with a painful renewal conversation every year. ScyllaDB is Apache 2.0 open-source — same Cassandra CQL, no license fees. Worth 15 minutes?

**Email Subject:**
> What {company} is paying DataStax vs what it could cost

**Email Body:**
> Hi {first_name},
>
> DataStax Enterprise licensing costs scale brutally as your data grows — and the value proposition gets murkier once you're past the early adoption phase.
>
> ScyllaDB is Apache 2.0 open-source. No per-node fees, no enterprise license renewals. You get the same Cassandra CQL compatibility, so your existing drivers and tooling keep working. Teams typically cut their database infrastructure spend by 40–60% in the first year.
>
> We've helped several {industry} companies make this switch without a rewrite — just a rolling migration using ScyllaDB's Cassandra-compatible interface.
>
> I put together a quick cost comparison model for companies at {company_name}'s scale (~{employees} employees). Happy to share it — want me to send it over?
>
> — Alex Chen, Solutions Engineer @ ScyllaDB

---

#### lock_in

**LinkedIn Invite:**
> Hi {first_name} — noticed {company_name} is on DataStax. Proprietary APIs are a quiet tax that compounds over time. ScyllaDB is open-source, Cassandra-compatible, and actively developed. Happy to walk through what switching looks like.

**Email Subject:**
> Escaping DataStax lock-in without rewriting {company}'s data layer

**Email Body:**
> Hi {first_name},
>
> DataStax has been quietly expanding proprietary APIs — Stargate, Astra's Vector Search, CQL extensions — which makes migration progressively harder over time. It's a well-worn playbook.
>
> ScyllaDB is Apache 2.0 and implements standard CQL with no proprietary extensions you'd be locked into. The migration path from DataStax Enterprise or Astra is well-documented — most teams do it as a rolling replacement with zero downtime.
>
> We recently helped a fintech company migrate 8TB of DataStax data to ScyllaDB in under two weeks. They kept the same Cassandra drivers and application code untouched.
>
> I'm curious — how tied is {company_name}'s stack to DataStax-specific features right now?
>
> — Alex Chen, Solutions Engineer @ ScyllaDB

---

#### scalability

**LinkedIn Invite:**
> Hi {first_name} — managing billions of data points at {company_name} must put real pressure on your DataStax cluster. ScyllaDB handles that scale on significantly fewer nodes. Happy to compare architectures.

**Email Subject:**
> {company}'s data scale deserves a database built for it

**Email Body:**
> Hi {first_name},
>
> At the scale {company_name} is operating — {industry} workloads typically mean millions of writes per second and billions of records — DataStax clusters tend to get expensive and operationally complex fast.
>
> ScyllaDB's architecture (userspace I/O, shard-per-core design) means it saturates hardware far more efficiently than DataStax's JVM-based stack. Comcast handles 1 million events/sec on a 6-node ScyllaDB cluster that previously required 30 Cassandra nodes.
>
> For IoT and high-throughput data pipelines specifically, we've seen 10x throughput improvements on identical hardware — which translates directly to infrastructure cost and operational headcount.
>
> Would it be useful to do a quick architecture review of your current DataStax setup? Even 30 minutes often surfaces some quick wins.
>
> — Alex Chen, Solutions Engineer @ ScyllaDB

---

### Mock Mode — Second Touch Templates

Used when a lead was contacted ≥ 6 months ago. Same pain-category structure but acknowledges
prior outreach and offers something new (benchmark, case study, cost model, reference architecture).

#### latency — second touch

**LinkedIn Invite:**
> Hi {first_name} — I reached out a while back about ScyllaDB's latency advantages for {company_name}. Curious if anything has shifted with your DataStax setup since then — happy to share what we've seen recently.

**Email Subject:**
> Following up — new latency benchmarks relevant to {company}

**Email Body (summary):** References the prior outreach, leads with new benchmark data (sub-ms p99 at 2M ops/sec), offers a case study of a team that cut node count 60%.

---

#### cost — second touch

**LinkedIn Invite:**
> Hi {first_name} — following up on my earlier note about {company_name}'s DataStax licensing costs. We've since put together a cost model for teams at your scale — the numbers are pretty striking. Worth a look?

**Email Subject:**
> Updated cost model for {company} — DataStax vs ScyllaDB

**Email Body (summary):** References prior outreach, offers a tailored cost comparison model, positions a 20-min walkthrough as the CTA.

---

#### lock_in — second touch

**LinkedIn Invite:**
> Hi {first_name} — checking back in after my earlier note on DataStax lock-in risk for {company_name}. We've helped a few more teams execute zero-downtime migrations since then. Happy to share what the process looks like now.

**Email Subject:**
> Migration playbook update — relevant to {company}'s stack

**Email Body (summary):** References prior outreach, leads with two new completed migrations in the same industry, offers a compatibility assessment.

---

#### scalability — second touch

**LinkedIn Invite:**
> Hi {first_name} — following up on my earlier note about ScyllaDB's throughput advantages for {company_name}'s data volume. We've added new reference architectures for {industry} workloads — happy to share.

**Email Subject:**
> New {industry} reference architecture — relevant to {company}

**Email Body (summary):** References prior outreach, leads with a published reference architecture for the lead's industry, offers to send the doc or do a 20-min architecture review.

---

## Agent 4 — QAAgent

**File:** `agents/qa.py`

No LLM. Pure Python deterministic gate. A lead passes only if **all** checks pass.

| Check | Rule |
|---|---|
| LinkedIn invite length | Must be ≤ 300 characters |
| Generic phrases | Blocked in both invite and email body |
| Company name | Must appear in the LinkedIn invite |
| ScyllaDB mention | Invite must contain "scylladb" or "scylla" |
| Email subject | Must be ≥ 5 characters |
| Email body length | Must be ≥ 100 characters |

### Blocked Phrases

`i hope this finds you well`, `i wanted to reach out`, `touch base`, `synergy`,
`leverage`, `game-changer`, `revolutionize`, `quick call`, `pick your brain`,
`circle back`, `per my last email`, `as per`, `hope you're doing well`,
`i came across your profile`

---

## Agent 5 — SenderAgent

**File:** `agents/sender.py`

No LLM. Dry-run trigger — simulates the moment messages would be dispatched.

Only processes leads that are:
- Not disqualified
- QA passed
- Not skipped (message_type ≠ "skipped")

For each actionable lead:
1. Prints a structured dispatch block to stdout showing name, title, company, email, LinkedIn URL, invite, and email.
2. Sets `lead.status = "linkedin_sent"` and records `status_updated_at`.
3. Writes all triggered leads to `data/outreach_<run_id>.json`.

Status is never downgraded — a lead already at `response_received` will not be reset.

---

## Agent 6 — ReporterAgent

**File:** `agents/reporter.py`

No LLM. Persists data and renders the HTML report.

- **SQLite:** One row per lead (PK = `id`). Updated in place on each run. Status never downgraded via upsert logic.
- **HTML:** Self-contained dark-theme report with embedded fonts. Four sections: Qualified, Already Touched, Lacks Contact Data, Disqualified.
- **Filename:** `data/report_<YYYYMMDD_HHMMSS>.html` — timestamped so each run produces a new file.

---

## Development Session — Iteration Prompts

The following prompts were used during a live development session to evolve the system from its initial state. Each entry records the prompt, what was changed, and which files were affected.

---

### 1 — Test apollo.py changes and compare results with main.py

**Prompt:**
> i've changed the apollo.py file and add some modifications, i would like you to test the new apollo.py file and then try to run the main.py and explain the diffrences in the results

**What changed:** Identified and documented the differences between `apollo.py` (new) and `apollo_.py` (original). Key differences:
- New `REACHABILITY_CHANNELS` config and `classify_reachability()` function added
- `run_full_pipeline()` now classifies all people into `apollo_reachable` vs `linkedin_only` before credit-consuming enrichment
- LinkedIn-only leads are preserved with `_apollo_reachable=False` instead of being silently dropped
- Pipeline returns two new keys: `apollo_reachable_people`, `linkedin_only_people`
- Reachability flags (`_apollo_reachable`, `_outreach_channels`, `_outreach_note`) carried forward onto enriched records

**Files affected:** `apollo.py` (read + tested), `apollo_.py` (compared)

---

### 2 — Update README

**Prompt:**
> update the readme file

**What changed:**
- Corrected mock data count (5 → 10 companies/leads)
- Added "Apollo Connector — Reachability Classification" section documenting the 5 outreach channels, the behavioral change (no leads dropped), the 3 new person-dict fields, and the 2 new pipeline return keys
- Updated Agent 1 description to mention reachability classification

**Files affected:** `README.md`

---

### 3 — Trace the LinkedIn-only lead funnel

**Prompt:**
> how does the leads with no email/phones funnel goes now? truncate the db, then run the main.py and track those leads and then tell me what happandes to them

**What changed:** No code changes. Ran a full pipeline trace and identified a structural gap: the `_apollo_reachable` flag from `apollo.py` was lost at the `ResearcherAgent` boundary because the `Lead` dataclass didn't store it. As a result, LinkedIn-only leads flowed through all downstream agents identically to Apollo-reachable leads in mock mode (because mock `bulk_match` returns full data regardless of who was submitted).

**Files affected:** None (analysis only)

---

### 4 — Add missing-contact detection, `needs_enrichment` status, and `apollo_reachable` DB column

**Prompt:**
> alter the mockdata so there will be 1 lead without email and 1 lead without linkedin url. then apply the flag at the researcheragent, the qualifieragent score those leads as "lack of crutial data" and note if its a lack of email or linkedin then the edit a status in the db for lack of data. so we can try other enrichment options in the future.

**What changed:**
- `apollo_mock_data.json` — Daniel Rodriguez: email cleared, `email_status` → `"none"`. Sofia Bertini: `linkedin_url` cleared
- `models/lead.py` — Added `apollo_reachable: bool = True` field
- `agents/researcher.py` — `_build_lead()` now copies `person.get("_apollo_reachable", True)` into `lead.apollo_reachable`
- `agents/qualifier.py` — New `_check_contact_data()` runs before scoring: detects empty `email` or `linkedin_url`, sets `disqualified=True`, `disqualify_reason="Missing contact data: ..."`, `status="needs_enrichment"`
- `agents/reporter.py` — Added `apollo_reachable INTEGER DEFAULT 1` column to schema, added `ALTER TABLE` migration for existing DBs, updated INSERT to 30 columns

**Files affected:** `apollo_mock_data.json`, `models/lead.py`, `agents/researcher.py`, `agents/qualifier.py`, `agents/reporter.py`

---

### 5 — Verify the pipeline end-to-end

**Prompt:**
> run the main.py and double check it

**What changed:** No code changes. Ran pipeline and queried DB to verify: Daniel Rodriguez (`needs_enrichment`, score=0, no email), Sofia Bertini (`needs_enrichment`, score=0, no LinkedIn URL), 5 leads `linkedin_sent`, 3 leads `pending` (score below threshold). All 8 tests on `apollo.py` passed.

**Files affected:** None (verification only)

---

### 6 — Restructure HTML report into four sections

**Prompt:**
> edit the reporter.py i would like that leads with lack of data will be under lack of data section and not failed. also create a new section for leads that are aleardy been touched in the last 180 days.

**What changed:**
- `_build_html()` now splits leads into four groups instead of two:
  - `lacks_data` — `status == "needs_enrichment"`
  - `already_sent` — `not disqualified and message_type == "skipped"`
  - `qualified` — `not disqualified and message_type != "skipped"`
  - `disqualified` — `disqualified and status != "needs_enrichment"`
- Four report sections: ✅ Qualified, ⏭️ Already Touched, 🔍 Lacks Contact Data, ❌ Disqualified
- Stat row updated to six cards: Total, Qualified, QA Passed, Already Touched, Lacks Data, Disqualified

**Files affected:** `agents/reporter.py`

---

### 7 — Update README with new qualifier and reporter logic

**Prompt:**
> update the read me with the new logic

**What changed:**
- Agent 1: noted `apollo_reachable` is now carried from Apollo into the Lead object
- Agent 2: added contact data check section (runs before scoring, sets `needs_enrichment`)
- Agent 5: corrected output path, clarified skipped lead handling
- Agent 6: replaced one-liner with four-section report table
- Data Model: expanded field breakdown, added status values table
- SQLite Schema: added `apollo_reachable` column, updated status comment, added three example queries

**Files affected:** `README.md`

---

### 8 — Document this session's prompts

**Prompt:**
> document the prompts i've used in this conversation

**What changed:** Added this section to `prompts_documentation.md`.

**Files affected:** `prompts_documentation.md`
