# GTM Hunter

**Home Assignment — GTM Engineer @ YotamCo**

Automated competitor displacement pipeline: find Competitor users → qualify leads → generate personalised outreach → QA → trigger → follow up → report.

---

## Quick Start

```bash
pip install httpx
python main.py
```

No API keys needed. Apollo runs on offline mock data; the copywriter uses pre-written pain-angle templates. All output lands in `data/`.

> **Pre-seeded state:** The repo ships with a previous run already in the DB and report folder.
> **Megan Chambers** (VP of Engineering, VaultEdge Payments) was contacted 30 days ago — her LinkedIn invite is in `data/output/reports/report_20260321_120000.html`.
> When you run `python main.py`, Megan will appear in the **Already Touched** section — skipped because she was contacted less than 180 days ago.
> When you run `python agents/follow_up.py`, her follow-up email will be dispatched — 30 days have passed since the LinkedIn invite with no response logged.

---

## Two-Step Outreach Sequence

Outreach is split into two deliberate steps, mirroring how real sales sequences work:

```
Step 1 — python main.py
  Finds & qualifies leads → generates copy → sends LinkedIn invite (dry-run)
  Status set to: linkedin_sent

  (wait for connection acceptance — typically 3–5 days)

Step 2 — python agents/follow_up.py
  Queries DB for linkedin_sent leads with no response after 5+ days
  Dispatches follow-up email (dry-run)
  Status set to: email_sent
```

If a lead responds at any point, their status is updated to `response_received` externally — the follow-up agent will skip them automatically.

---

## Pipeline Overview

```
connectors/apollo.py (mock / live)
      │
      ▼
Agent 1 — ResearcherAgent    Fetch & enrich leads, detect pain category
      │
      ▼
Agent 2 — QualifierAgent     Contact-data check + 20-point scoring rubric
      │
      ▼
Agent 3 — CopywriterAgent    Generates 3 copy variants per lead (Claude API or mock templates)
      │
      ▼
Agent 4 — QAAgent            Scores all variants, selects best, runs quality gate
      │
      ▼
Agent 5 — SenderAgent        Dry-run trigger — dispatches LinkedIn invite, logs to JSON
      │
      ▼
Agent 6 — FollowUpAgent      Queries DB → dispatches follow-up emails to non-responders (5+ days)
      │                       Updates Lead objects in memory so reporter captures the activity
      ▼
Agent 7 — ReporterAgent      SQLite persistence + interactive self-contained HTML report
```

---

## Project Structure

```
yotamco/
├── main.py                      ← Step 1: full pipeline (find → qualify → copy → QA → send → report)
├── requirements.txt
│
├── agents/
│   ├── researcher.py            ← Agent 1: fetch leads, detect pain category
│   ├── qualifier.py             ← Agent 2: 20-point scoring rubric
│   ├── copywriter.py            ← Agent 3: Claude API + mock copy templates
│   ├── qa.py                    ← Agent 4: deterministic quality gate
│   ├── sender.py                ← Agent 5: dry-run LinkedIn invite trigger
│   ├── follow_up.py             ← Agent 6 + Step 2 CLI: follow-up email dispatcher
│   └── reporter.py              ← Agent 7: SQLite + HTML report
│
├── connectors/
│   └── apollo.py                ← Apollo API connector (mock + live mode)
│
├── models/
│   └── lead.py                  ← Lead dataclass shared by all agents
│
├── utils/
│   └── logging.py               ← Shared logger
│
└── data/
    ├── mock_data_source/
    │   └── apollo_mock_data.json        ← Offline mock data (10 companies, 10 leads)
    ├── DB/
    │   ├── gtm_hunter.db           ← SQLite database (pre-seeded with Megan Chambers)
    │   └── inspect_db.py               ← Prints DB schema + all rows for every table
    └── output/
        ├── reports/
        │   ├── report_20260321_120000.html  ← Pre-seeded report (Megan's first-touch run)
        │   └── report_<timestamp>.html      ← Generated on each main.py run
        └── outreach/
            ├── outreach_20260321_120000.json    ← Pre-seeded outreach log (Megan's LinkedIn invite)
            ├── outreach_<run_id>.json           ← LinkedIn invite log (main.py)
            └── followup_<timestamp>.json        ← Follow-up email log (agents/follow_up.py)
```

---

## Running

### Step 1 — Find leads and send LinkedIn invites

```bash
python main.py
```

**With real Claude-generated copy:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

**With real Apollo leads:**
```bash
export APOLLO_API_KEY=your-apollo-key
python main.py
```

**Fully live:**
```bash
export APOLLO_API_KEY=your-apollo-key
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

**CLI flags** (take priority over environment variables):
```
python main.py --api-key sk-ant-...
python main.py --apollo-key your-key
python main.py --output-dir ./results
```

> Apollo's `bulk_match_people` endpoint enriches up to 5 leads per run (credit-consuming step).

---

### Step 2 — Send follow-up emails to non-responders

```bash
python agents/follow_up.py
```

Checks the DB for any lead with status `linkedin_sent` where:
- No response has been logged (`status != response_received`)
- At least **5 days** have passed since the LinkedIn invite (`status_updated_at`)

For each qualifying lead it dispatches a dry-run follow-up email and updates their status to `email_sent`.

**Configurable constants in `agents/follow_up.py`:**

| Constant | Default | Meaning |
|---|---|---|
| `TRIGGER_STATUS` | `linkedin_sent` | Status that indicates a LinkedIn invite was sent |
| `FOLLOW_UP_DELAY_DAYS` | `5` | Days to wait before sending the follow-up email |

```
python agents/follow_up.py --output-dir ./results
```

---

## Output

| File | Produced by | Contents |
|---|---|---|
| `data/DB/gtm_hunter.db` | Both steps | SQLite — all leads across all runs, status never downgraded |
| `data/output/reports/report_<ts>.html` | `main.py` | Self-contained dark-theme HTML report |
| `data/output/outreach/outreach_<run_id>.json` | `main.py` | LinkedIn invite dry-run log |
| `data/output/outreach/followup_<ts>.json` | `agents/follow_up.py` | Follow-up email dry-run log |

---

## Agent Details

### Agent 1 — ResearcherAgent
Calls Apollo's `run_full_pipeline()`, which classifies every person by reachability channel before any credits are consumed. Leads without Apollo contact data are kept and flagged for LinkedIn outreach rather than silently dropped. The researcher then detects a dominant **pain category** per lead via keyword scoring across company description, tech stack, and industry.

| Category | Keywords |
|---|---|
| `latency` | real-time, millisecond, latency, fast, instant, leaderboard, live, streaming |
| `cost` | cost, overhead, budget, spend, pricing, license, enterprise fee |
| `lock_in` | competitor, proprietary, vendor, migration, open-source, competitor |
| `scalability` | scale, billion, sensor, iot, massive, throughput, volume, high-velocity |

### Agent 2 — QualifierAgent

**Step 1 — Contact data check:** leads missing an email or LinkedIn URL are immediately flagged `needs_enrichment` and excluded from scoring. They appear in the report's "Lacks Contact Data" section and can be retried with a different enrichment source.

**Step 2 — 20-point scoring rubric:**

| Signal | Points |
|---|---|
| Seniority (CTO=6, VP=5, Head/Director=4–5, Principal/Staff/Manager=3, Senior=1) | 0–6 |
| Company size (50→1, 200→2, 500→3, 1000+→4) | 0–4 |
| Competitor signal score (÷25, max 4) | 0–4 |
| Email verified (+3) / guessed (+1) | 0–3 |
| LinkedIn URL present | 0–2 |
| Competitor tech detected | 0–1 |

**Hard disqualifiers:** titles containing `intern`, `junior`, `jr.`, `associate engineer`, `entry`; companies matching `competitor`, `astra db`, `competitor project`.

**Pass threshold:** ≥ 12 points.

### Agent 3 — CopywriterAgent

Generates **3 copy variants** per qualified lead — each with a different hook angle (performance, technical detail, proof-point story). The pain category detected by Agent 1 drives the angle set. Agent 4 then scores all variants and selects the best one.

- **Live mode** — calls `claude-sonnet-4-20250514` via the Anthropic API (3 calls per lead, each prompted with a different angle hint)
- **Mock mode** — 3 pre-written pain-angle templates per category (no API key needed)

Also checks the DB before writing copy:
- Contacted **< 6 months ago** → skip (`message_type = skipped`)
- Contacted **≥ 6 months ago** → second-touch message (`message_type = second_touch`)
- **Never contacted** → cold outreach (`message_type = cold`)

### Agent 4 — QAAgent

Pure Python — no LLM. Scores all 3 variants from Agent 3 and selects the best one, then runs the final quality gate on the winner.

**Variant selection:** each variant is scored on a deterministic rubric — invite content density (up to 4 pts), company name present (+2), YotamCo mentioned (+2), subject and body length (+1 each), minus 2 pts per blocked phrase hit. The variant with the fewest issues wins; score breaks ties. The selected variant number is shown in the HTML report (`QA: variant N/3`).

**Quality gate** — the selected variant passes only if all checks pass:

| Check | Rule |
|---|---|
| LinkedIn invite length | ≤ 300 characters |
| Generic phrases | Blocked in both invite and email body |
| Company name | Must appear in the LinkedIn invite |
| YotamCo mention | Invite must contain "yotamco" |
| Email subject | ≥ 5 characters |
| Email body | ≥ 100 characters |

Blocked phrases: `touch base`, `leverage`, `synergy`, `game-changer`, `revolutionize`, `quick call`, `pick your brain`, `circle back`, `i hope this finds you well`, and more.

### Agent 5 — SenderAgent

Dry-run LinkedIn invite trigger. For every QA-passed, non-skipped lead:

1. Prints a structured dispatch block to the console
2. Sets `lead.status = linkedin_sent` with the current timestamp
3. Appends the record to `data/output/outreach/outreach_<run_id>.json`

### Agent 6 — FollowUpAgent (runs inside `main.py`, before the reporter)

Runs automatically as part of the `main.py` pipeline — **before** the ReporterAgent — so that follow-up activity is captured in the same run's report rather than requiring a separate script execution.

Queries the DB for leads with `status = linkedin_sent` whose invite has gone unanswered for `FOLLOW_UP_DELAY_DAYS` (default: 5). For each qualifying lead:

1. Prints a dry-run email dispatch block to the console (days since invite, subject, body)
2. Sets `status = email_sent` in the DB **and** on the in-memory Lead object
3. Appends the record to `data/output/outreach/followup_<timestamp>.json`

Because the Lead object is updated in memory before the reporter runs, the "Already Touched" section of that run's report shows the follow-up email content and a `📧 FOLLOW-UP SENT` timestamp — all in a single report.

Leads already at `response_received` or `email_sent` are never re-triggered.

> **Standalone mode** — `python agents/follow_up.py` also works independently for cases where you want to trigger follow-ups without running the full pipeline.

### Agent 7 — ReporterAgent

Persists all leads to SQLite and renders a self-contained, interactive HTML report. All sections start collapsed — click a section header to expand it.

| Section | Who |
|---|---|
| ✅ Qualified Leads | Passed scoring, QA-checked, LinkedIn invite dispatched |
| 📨 Already Touched | Contacted < 180 days ago — shows LinkedIn sent date; or `📧 FOLLOW-UP SENT` with timestamp if follow-up was dispatched this run |
| 🔍 Lacks Contact Data | Missing email or LinkedIn URL — queued for re-enrichment |
| ❌ Disqualified | Failed scoring or hard-disqualified by title/company |

- SQLite rows are updated in place across runs — status never downgraded
- HTML report is fully self-contained (fonts embedded as base64), no internet needed to view
- Each qualified lead card shows which variant QA selected (`QA: variant N/3`)

---

## Lead Status Lifecycle

```
pending
  │
  ├─→ needs_enrichment   (QualifierAgent — missing email or LinkedIn URL)
  │
  └─→ linkedin_sent      (SenderAgent — LinkedIn invite dispatched)
        │
        ├─→ response_received   (set externally — lead replied, do not re-contact)
        │
        └─→ email_sent          (FollowUpAgent — follow-up email dispatched after 5 days)
```

Status only ever advances — no step will downgrade a lead already marked `response_received`.

---

## SQLite Schema

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT,
    total_leads INTEGER,
    qualified_leads INTEGER,
    qa_passed INTEGER
);

CREATE TABLE leads (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    name TEXT, title TEXT, email TEXT,
    linkedin_url TEXT, email_status TEXT,
    company_name TEXT, company_domain TEXT, company_industry TEXT,
    company_employees INTEGER, company_technologies TEXT,
    company_description TEXT, company_signal_score INTEGER,
    qualification_score INTEGER, qualification_reason TEXT,
    pain_category TEXT, pain_angle TEXT,
    disqualified INTEGER, disqualify_reason TEXT,
    linkedin_invite TEXT, follow_up_email_subject TEXT, follow_up_email_body TEXT,
    qa_passed INTEGER, qa_issues TEXT,
    processed_at TEXT,
    message_type TEXT DEFAULT 'cold',
    status TEXT DEFAULT 'pending',
    status_updated_at TEXT,
    apollo_reachable INTEGER DEFAULT 1
);
```

**Inspect the DB:**

```bash
python data/DB/inspect_db.py
```

Prints the schema (column names, types, defaults, primary keys) and all row data for every table. Long text fields are truncated at 120 characters for readability.

**Useful queries:**

```sql
-- Leads ready for follow-up email (also surfaced by agents/follow_up.py automatically)
SELECT name, email, status_updated_at FROM leads WHERE status = 'linkedin_sent';

-- Leads to retry with a different enrichment source
SELECT name, company_name, disqualify_reason FROM leads WHERE status = 'needs_enrichment';

-- Full pipeline history across all runs
SELECT r.run_id, r.total_leads, r.qualified_leads, r.qa_passed,
       COUNT(l.id) AS enrichment_needed
FROM runs r
LEFT JOIN leads l ON l.run_id = r.run_id AND l.status = 'needs_enrichment'
GROUP BY r.run_id;
```
