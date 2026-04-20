# 🦑 ScyllaDB GTM Hunter

> **Home Assignment — GTM Engineer @ ScyllaDB**  
> Automated competitor displacement pipeline: find DataStax users → qualify leads → personalise outreach → QA → report.

---

## Project Structure

```
scylladb_hunter/
├── main.py                      ← CLI entrypoint + pipeline orchestrator
├── apollo.py                    ← Apollo API connector (mock + live mode, reachability classification)
├── apollo_mock_data.json        ← Offline mock data (10 companies, 10 leads)
│
├── agents/                      ← One file per pipeline stage
│   ├── __init__.py
│   ├── researcher.py            ← Agent 1: fetch leads, detect pain category
│   ├── qualifier.py             ← Agent 2: 20-point scoring rubric
│   ├── copywriter.py            ← Agent 3: Claude API + mock copy templates
│   ├── qa.py                    ← Agent 4: deterministic quality gate
│   ├── sender.py                ← Agent 5: dry-run outreach trigger (console + JSON log)
│   └── reporter.py              ← Agent 6: SQLite persistence + HTML report
│
├── models/
│   ├── __init__.py
│   └── lead.py                  ← Lead dataclass, PAIN_CATEGORIES, PAIN_KEYWORDS
│
├── utils/
│   ├── __init__.py
│   └── logging.py               ← Shared configure_logging() + make_logger()
│
└── data/                        ← Created on first run
    ├── report_<timestamp>.html  ← Self-contained dark-theme HTML report
    ├── outreach_<run_id>.json   ← Dry-run trigger log (one record per dispatched lead)
    └── scylladb_hunter.db       ← SQLite database (append across runs)
```

---

## Setup

```bash
pip install httpx
```

Only external dependency. (`apollo.py` also requires `httpx`.)

---

## Running

### Dry-run demo — no API keys needed
```bash
python main.py
```
Apollo runs in mock mode (offline data, no credits consumed).  
Copywriter uses pre-written pain-angle templates instead of Claude.

### With real Claude-generated copy
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

### With real Apollo leads (live data)
```bash
export APOLLO_API_KEY=your-apollo-key...
python main.py
```
> ⚠️ Apollo's `bulk_match_people` endpoint consumes credits. The pipeline enriches up to 5 leads per run.

### Fully live — real Apollo leads + real Claude copy
```bash
export APOLLO_API_KEY=your-apollo-key...
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

### CLI options
```
python main.py --help

  --output-dir DIR    Output directory for report.html and scylladb_hunter.db (default: data/)
  --api-key KEY       Anthropic API key — overrides ANTHROPIC_API_KEY env var
  --apollo-key KEY    Apollo.io API key — overrides APOLLO_API_KEY env var
```

API keys can be passed as environment variables or CLI flags — CLI flags take priority.

---

## Apollo Connector — Reachability Classification

`apollo.py` now classifies every person returned by the people search into one of five outreach channels before any credits are consumed:

| Channel | Condition |
|---|---|
| `email` | Apollo has a **verified** email |
| `email_guessed` | Apollo has an email marked **guessed** |
| `phone` | Apollo has a direct phone number |
| `linkedin` | Apollo has a LinkedIn URL |
| `linkedin_search` | Name + company known — find manually on LinkedIn |

**Key behaviour change:** previously, leads without Apollo contact data were silently dropped before enrichment. Now they are **kept** and flagged with `_apollo_reachable=False` so downstream systems (CRM, sequencer) can route them to the right outreach channel. Only Apollo-reachable leads (email / phone) consume enrichment credits.

Each person dict is annotated with three extra fields:

```python
_apollo_reachable   # bool — True if email or phone is available via Apollo
_outreach_channels  # list[str] — ordered channel names, e.g. ["email", "linkedin"]
_outreach_note      # str — human-readable summary for the report / DB
```

`run_full_pipeline()` returns two additional keys alongside the original five:

```python
"apollo_reachable_people"  # leads with email or phone — enriched with credits
"linkedin_only_people"     # leads without Apollo contact data — kept, not enriched
```

---

## Agent Details

### Agent 1 — `agents/researcher.py`
Calls `apollo.py`'s `run_full_pipeline()`, which classifies every person by **reachability channel** before enrichment. The researcher builds a `Lead` object for every enriched person and copies the `_apollo_reachable` flag from Apollo into `lead.apollo_reachable`, making it available to all downstream agents. It also detects the dominant **pain category** per lead via keyword scoring across company description, tech stack, and industry.

| Category | Keywords |
|---|---|
| `latency` | real-time, millisecond, latency, fast, instant, leaderboard, live, streaming |
| `cost` | cost, overhead, budget, spend, pricing, license, enterprise fee |
| `lock_in` | datastax, proprietary, vendor, migration, open-source, cassandra |
| `scalability` | scale, billion, sensor, iot, massive, throughput, volume, high-velocity |

### Agent 2 — `agents/qualifier.py`

**Contact data check (runs first — before scoring):**  
If a lead has no email or no LinkedIn URL after enrichment, it is immediately flagged:
- `disqualified = True`
- `disqualify_reason = "Missing contact data: no email"` (or `no LinkedIn URL`)
- `status = "needs_enrichment"` — queryable in the DB for future re-enrichment attempts

These leads are never scored and never reach the copywriter or sender.

**20-point scoring rubric** (applied only to leads that pass the contact data check):

| Signal | Points |
|---|---|
| Seniority (CTO=6, VP=5, Head/Director=4–5, Principal/Staff/Manager=3, Senior=1) | 0–6 |
| Company size (50→1, 200→2, 500→3, 1000+→4) | 0–4 |
| DataStax signal score (÷25, max 4) | 0–4 |
| Email verified (+3) / guessed (+1) | 0–3 |
| LinkedIn URL present | 0–2 |
| DataStax/Cassandra tech detected | 0–1 |

**Hard disqualifiers:** titles containing `intern`, `junior`, `jr.`, `associate engineer`, `entry`; companies matching `datastax`, `astra db`, `apache cassandra project`.  
**Pass threshold:** ≥ 12 points.

### Agent 3 — `agents/copywriter.py`
- **LIVE** (with API key): calls `claude-sonnet-4-20250514`
- **MOCK** (no key): pre-written pain-angle templates
- Generates: LinkedIn invite (≤300 chars) + follow-up email subject + body

### Agent 4 — `agents/qa.py`
Pure Python — no LLM. Blocks: invites over 300 chars, generic phrases ("touch base", "leverage", "synergy", etc.), missing company/ScyllaDB mention, absent/short email subject or body.

### Agent 5 — `agents/sender.py` ← Trigger logic
Dry-run outreach dispatcher — the explicit "trigger" step. For every QA-passed lead:
- Prints a structured dispatch block to the console showing exactly what would be sent and to whom.
- Writes `data/outreach_<run_id>.json` — a machine-readable log of all triggered outreach.

No real messages are sent. Leads contacted < 6 months ago (`message_type=skipped`) are excluded from the trigger.

### Agent 6 — `agents/reporter.py`
Persists all leads to SQLite and renders a self-contained HTML report with four sections:

| Section | Who |
|---|---|
| ✅ Qualified Leads | Passed scoring, QA-checked, copy written |
| ⏭️ Already Touched — last 180 days | Qualified but skipped by copywriter (contacted < 180 days ago) |
| 🔍 Lacks Contact Data | `status = needs_enrichment` — missing email or LinkedIn URL |
| ❌ Disqualified | Failed scoring, hard-disqualified by title/company |

- SQLite: appends each run with a unique `run_id` timestamp; status is never downgraded
- HTML: fully self-contained (fonts embedded as base64), works offline

---

## Data Model

`models/lead.py` defines the single `Lead` dataclass shared by all agents. Fields are populated stage by stage:

```
Lead
 ├── Identity           → ResearcherAgent  (name, email, linkedin_url, email_status)
 ├── apollo_reachable   → ResearcherAgent  (bool — carried from apollo._apollo_reachable)
 ├── Company            → ResearcherAgent  (name, domain, industry, employees, tech stack)
 ├── Qualification      → QualifierAgent   (score, reason, disqualified, status)
 ├── Copy               → CopywriterAgent  (linkedin_invite, email subject + body, message_type)
 ├── QA                 → QAAgent          (qa_passed, qa_issues)
 ├── Outreach status    → SenderAgent      (status, status_updated_at)
 └── Meta               → ResearcherAgent  (run_id, processed_at)
```

**Lead status values:**

| Status | Set by | Meaning |
|---|---|---|
| `pending` | default | Not yet actioned |
| `needs_enrichment` | QualifierAgent | Missing email or LinkedIn URL — retry with another enrichment source |
| `linkedin_sent` | SenderAgent | Outreach triggered this run |
| `email_sent` | external | Follow-up email dispatched |
| `response_received` | external | Lead replied — do not re-contact |

Status only ever advances — re-running the pipeline will not downgrade a lead already marked `response_received`.

---

## SQLite Schema

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, created_at TEXT,
    total_leads INTEGER, qualified_leads INTEGER, qa_passed INTEGER
);
CREATE TABLE leads (
    id TEXT PRIMARY KEY,      -- one row per lead, updated in place across runs
    run_id TEXT,              -- last run that touched this lead
    name TEXT, title TEXT, email TEXT,
    linkedin_url TEXT, email_status TEXT, company_name TEXT,
    company_domain TEXT, company_industry TEXT, company_employees INTEGER,
    company_technologies TEXT,      -- JSON array
    company_description TEXT, company_signal_score INTEGER,
    qualification_score INTEGER, qualification_reason TEXT,
    pain_category TEXT, pain_angle TEXT,
    disqualified INTEGER, disqualify_reason TEXT,
    linkedin_invite TEXT, follow_up_email_subject TEXT, follow_up_email_body TEXT,
    qa_passed INTEGER, qa_issues TEXT,  -- JSON array
    processed_at TEXT,
    message_type TEXT DEFAULT 'cold',   -- "cold" | "second_touch" | "skipped"
    status TEXT DEFAULT 'pending',      -- "pending" | "needs_enrichment" | "linkedin_sent" | "email_sent" | "response_received"
    status_updated_at TEXT,
    apollo_reachable INTEGER DEFAULT 1  -- 0 = Apollo had no email/phone at search time
);
```

**Useful queries:**

```sql
-- Leads to retry with a different enrichment source
SELECT name, company_name, disqualify_reason FROM leads WHERE status = 'needs_enrichment';

-- Leads ready for a follow-up email
SELECT name, email FROM leads WHERE status = 'linkedin_sent';

-- Full pipeline history across all runs
SELECT r.run_id, r.total_leads, r.qualified_leads, r.qa_passed, COUNT(l.id) as enrichment_needed
FROM runs r LEFT JOIN leads l ON l.run_id = r.run_id AND l.status = 'needs_enrichment'
GROUP BY r.run_id;
```

Status only ever advances — re-running the pipeline will not downgrade a lead already marked `response_received`.
