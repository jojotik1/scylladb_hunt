"""
agents/reporter.py
------------------
Agent 5 — ReporterAgent

Responsibility:
  - Persist every lead (and run metadata) to SQLite in append mode.
    Each run gets a unique run_id so history is never overwritten.
  - Render a self-contained, dark-theme HTML report with embedded fonts
    (no external network requests needed to view it).

Outputs:
  output/scylladb_hunter.db  — SQLite database (tables: runs, leads)
  output/report.html         — Fully self-contained HTML report
"""

import base64
import html
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from models.lead import Lead
from utils.logging import make_logger


# ── Font embedding helper ─────────────────────────────────────────────────────

def _font_b64(path: str) -> str:
    """Return base64-encoded font data, or empty string if the file is absent."""
    try:
        return base64.b64encode(Path(path).read_bytes()).decode()
    except FileNotFoundError:
        return ""


def _font_face(family: str, weight: int, b64: str) -> str:
    if not b64:
        return ""
    return (
        f"@font-face{{font-family:'{family}';font-weight:{weight};"
        f"font-style:normal;"
        f"src:url('data:font/truetype;base64,{b64}') format('truetype');}}"
    )


def _embedded_fonts() -> str:
    """Build @font-face declarations for DejaVu fonts (Linux and Windows)."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu",          # Debian/Ubuntu
        "/usr/share/fonts/dejavu",                   # Fedora/RHEL
        "C:/Windows/Fonts",                          # Windows (Courier New fallback)
    ]
    font_map = {
        ("ReportMono", 400): ["DejaVuSansMono.ttf",      "courbd.ttf"],
        ("ReportMono", 700): ["DejaVuSansMono-Bold.ttf", "courbd.ttf"],
        ("ReportSans",  400): ["DejaVuSans.ttf",          "arial.ttf"],
        ("ReportSans",  700): ["DejaVuSans-Bold.ttf",     "arialbd.ttf"],
    }
    results = []
    for (family, weight), filenames in font_map.items():
        b64 = ""
        for base in candidates:
            for filename in filenames:
                b64 = _font_b64(f"{base}/{filename}")
                if b64:
                    break
            if b64:
                break
        results.append(_font_face(family, weight, b64))
    return "\n".join(results)


# ── HTML component helpers ────────────────────────────────────────────────────

def _stat_card(value: int, label: str, color: str = "#00ff88") -> str:
    return (
        f'<div class="stat-card">'
        f'<span class="stat-num" style="color:{color}">{value}</span>'
        f'<span class="stat-label">{label}</span>'
        f'</div>'
    )


def _tech_tag(tech: str) -> str:
    color = (
        "#ff4d6d"
        if "datastax" in tech.lower() or "cassandra" in tech.lower()
        else "#1e3a5f"
    )
    return f'<span class="tech-tag" style="background:{color}">{html.escape(tech)}</span>'


def _pain_badge(category: str) -> str:
    colors = {
        "latency":     "#ff6b35",
        "cost":        "#f7c59f",
        "lock_in":     "#e84855",
        "scalability": "#3bceac",
    }
    color = colors.get(category, "#555")
    label = category.replace("_", " ").upper()
    return f'<span class="pain-badge" style="border-color:{color};color:{color}">{label}</span>'


def _score_bar(score: int, max_score: int = 20) -> str:
    pct   = min(100, int(score / max_score * 100))
    color = "#00ff88" if pct >= 70 else "#f7c59f" if pct >= 50 else "#ff4d6d"
    return (
        f'<div class="score-bar-wrap">'
        f'<div class="score-bar" style="width:{pct}%;background:{color}"></div>'
        f'<span class="score-label">{score}/{max_score}</span>'
        f'</div>'
    )


def _message_type_badge(message_type: str) -> str:
    styles = {
        "cold":         ("🧊 COLD OUTREACH",  "#0066ff"),
        "second_touch": ("🔁 SECOND TOUCH",   "#f7c59f"),
        "skipped":      ("⏭️ SKIPPED",         "#4a6080"),
    }
    label, color = styles.get(message_type, ("UNKNOWN", "#555"))
    return (
        f'<span style="font-family:var(--mono);font-size:10px;padding:3px 10px;'
        f'border:1px solid {color};border-radius:4px;color:{color};'
        f'letter-spacing:.1em;white-space:nowrap">{label}</span>'
    )


def _lead_card(lead: Lead, show_copy: bool = True) -> str:
    status_icon = "✅" if lead.qa_passed else ("⚠️" if not lead.disqualified else "❌")

    # QA issues block
    qa_issues_html = ""
    if lead.qa_issues:
        items = "".join(f"<li>{html.escape(i)}</li>" for i in lead.qa_issues)
        qa_issues_html = f'<ul class="qa-issues">{items}</ul>'

    # Copy / disqualification / skipped block
    copy_section = ""
    if lead.message_type == "skipped":
        copy_section = (
            '<div class="disq-reason" style="color:var(--muted)">'
            '⏭️ Skipped — contacted less than 6 months ago. No new message generated.'
            '</div>'
        )
    elif show_copy and lead.linkedin_invite and lead.linkedin_invite != "[GENERATION FAILED]":
        invite_len = len(lead.linkedin_invite)
        len_color  = "#ff4d6d" if invite_len > 300 else "#00ff88"
        escaped_invite  = html.escape(lead.linkedin_invite)
        escaped_subject = html.escape(lead.follow_up_email_subject)
        escaped_body    = html.escape(lead.follow_up_email_body).replace("\n", "<br>")
        copy_section = f"""
        <div class="copy-section">
          <div class="copy-label">
            LinkedIn Invite
            <span style="color:{len_color};font-size:11px">({invite_len}/300 chars)</span>
          </div>
          <div class="copy-box linkedin-box">{escaped_invite}</div>
          <div class="copy-label" style="margin-top:12px">Email Subject</div>
          <div class="copy-box subject-box">{escaped_subject}</div>
          <div class="copy-label" style="margin-top:12px">Follow-up Email</div>
          <div class="copy-box email-box">{escaped_body}</div>
          {qa_issues_html}
        </div>"""
    elif lead.disqualified:
        copy_section = f'<div class="disq-reason">❌ {html.escape(lead.disqualify_reason)}</div>'

    tech_tags   = "".join(_tech_tag(t) for t in lead.company_technologies[:7])
    disq_class  = " disqualified" if lead.disqualified else ""

    return f"""
    <div class="lead-card{disq_class}">
      <div class="lead-header">
        <div class="lead-identity">
          <div class="lead-name">{status_icon} {html.escape(lead.name)}</div>
          <div class="lead-title">{html.escape(lead.title)}</div>
          <div class="lead-company">
            {html.escape(lead.company_name)} · {html.escape(lead.company_industry)} · {lead.company_employees:,} employees
          </div>
        </div>
        <div class="lead-meta">
          {_message_type_badge(lead.message_type)}
          {_pain_badge(lead.pain_category)}
          {_score_bar(lead.qualification_score)}
          <div class="lead-email">
            ✉ {html.escape(lead.email)}
            <span class="email-status {html.escape(lead.email_status)}">{html.escape(lead.email_status)}</span>
          </div>
        </div>
      </div>
      <div class="tech-tags">{tech_tags}</div>
      {copy_section}
    </div>"""


# ── CSS ───────────────────────────────────────────────────────────────────────

_CSS = """\
  :root {
    --bg:      #080c12;
    --surface: #0d1520;
    --surface2:#111c2d;
    --border:  #1a2d45;
    --accent:  #00ff88;
    --accent2: #0066ff;
    --danger:  #ff4d6d;
    --warn:    #f7c59f;
    --text:    #c8d8e8;
    --muted:   #4a6080;
    --mono:    'ReportMono','DejaVu Sans Mono','Courier New',monospace;
    --sans:    'ReportSans','DejaVu Sans',Arial,sans-serif;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:var(--sans); min-height:100vh; }
  body::before {
    content:''; position:fixed; inset:0;
    background-image:
      linear-gradient(rgba(0,102,255,.03) 1px,transparent 1px),
      linear-gradient(90deg,rgba(0,102,255,.03) 1px,transparent 1px);
    background-size:40px 40px; pointer-events:none; z-index:0;
  }
  .container { max-width:1100px; margin:0 auto; padding:0 24px; position:relative; z-index:1; }
  /* Header */
  header { padding:48px 0 32px; border-bottom:1px solid var(--border); margin-bottom:40px; }
  .header-top { display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:16px; }
  .logo-area h1 { font-family:var(--mono); font-size:28px; font-weight:700; color:#fff; letter-spacing:-.5px; }
  .logo-area h1 span { color:var(--accent); }
  .logo-area .subtitle { font-size:13px; color:var(--muted); margin-top:4px; font-family:var(--mono); }
  .run-badge { background:var(--surface2); border:1px solid var(--border); padding:8px 16px;
    border-radius:6px; font-family:var(--mono); font-size:12px; color:var(--muted); }
  .run-badge strong { color:var(--accent); }
  /* Stats */
  .stats-row { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:40px; }
  .stat-card { flex:1; min-width:140px; background:var(--surface); border:1px solid var(--border);
    border-radius:8px; padding:20px; display:flex; flex-direction:column; gap:4px; }
  .stat-num { font-family:var(--mono); font-size:32px; font-weight:700; line-height:1; }
  .stat-label { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.08em; }
  /* Section headers */
  .section-header { font-family:var(--mono); font-size:13px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.12em; margin-bottom:16px; padding-bottom:8px;
    border-bottom:1px solid var(--border); display:flex; align-items:center; gap:8px; }
  .section-header .count { background:var(--accent); color:var(--bg); padding:1px 7px;
    border-radius:10px; font-size:11px; font-weight:700; }
  /* Lead cards */
  .lead-card { background:var(--surface); border:1px solid var(--border); border-radius:10px;
    padding:24px; margin-bottom:20px; transition:border-color .2s; }
  .lead-card:hover { border-color:var(--accent2); }
  .lead-card.disqualified { opacity:.55; border-color:#1a1a2e; }
  .lead-card.disqualified:hover { border-color:var(--danger); opacity:.75; }
  .lead-header { display:flex; justify-content:space-between; align-items:flex-start;
    flex-wrap:wrap; gap:16px; margin-bottom:16px; }
  .lead-identity { min-width:0; flex:1 1 200px; }
  .lead-name { font-family:var(--mono); font-size:16px; font-weight:700; color:#fff; margin-bottom:4px; }
  .lead-title { font-size:14px; color:var(--accent); margin-bottom:4px; font-weight:500; }
  .lead-company { font-size:12px; color:var(--muted); }
  .lead-meta { display:flex; flex-direction:column; gap:8px; align-items:flex-end; flex-shrink:0; }
  .pain-badge { font-family:var(--mono); font-size:10px; padding:3px 10px; border:1px solid;
    border-radius:4px; letter-spacing:.1em; white-space:nowrap; }
  .score-bar-wrap { width:160px; height:6px; background:var(--border); border-radius:3px;
    position:relative; margin-top:18px; }
  .score-bar { height:100%; border-radius:3px; transition:width .4s ease; }
  .score-label { position:absolute; right:0; top:-18px; font-family:var(--mono);
    font-size:11px; color:var(--muted); }
  .lead-email { font-size:12px; color:var(--muted); font-family:var(--mono); margin-top:4px; }
  .email-status { padding:1px 6px; border-radius:3px; font-size:10px; font-weight:700; text-transform:uppercase; }
  .email-status.verified { background:rgba(0,255,136,.15); color:var(--accent); }
  .email-status.guessed  { background:rgba(247,197,159,.15); color:var(--warn); }
  .email-status.unknown  { background:rgba(255,77,109,.1); color:var(--danger); }
  /* Tech tags */
  .tech-tags { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; }
  .tech-tag { font-family:var(--mono); font-size:10px; padding:3px 8px; border-radius:4px;
    color:#fff; opacity:.85; }
  /* Copy section */
  .copy-section { margin-top:8px; }
  .copy-label { font-family:var(--mono); font-size:11px; color:var(--muted);
    text-transform:uppercase; letter-spacing:.1em; margin-bottom:8px; }
  .copy-box { background:var(--surface2); border:1px solid var(--border); border-radius:6px;
    padding:14px 16px; font-size:13px; line-height:1.65; color:var(--text);
    white-space:pre-wrap; word-wrap:break-word; }
  .linkedin-box { border-left:3px solid #0077b5; }
  .subject-box  { border-left:3px solid var(--warn); font-family:var(--mono); font-size:12px; }
  .email-box    { border-left:3px solid var(--accent2); }
  .qa-issues { margin-top:10px; padding:10px 14px; background:rgba(255,77,109,.07);
    border:1px solid rgba(255,77,109,.25); border-radius:6px; list-style:none; }
  .qa-issues li { font-size:12px; color:var(--danger); padding:2px 0; font-family:var(--mono); }
  .qa-issues li::before { content:"⚠ "; }
  .disq-reason { font-family:var(--mono); font-size:12px; color:var(--danger);
    padding:10px; background:rgba(255,77,109,.06); border-radius:6px; }
  /* Footer */
  footer { padding:32px 0 48px; margin-top:48px; border-top:1px solid var(--border);
    font-family:var(--mono); font-size:11px; color:var(--muted); text-align:center; }
  .section { margin-bottom:48px; }"""


# ── Agent ─────────────────────────────────────────────────────────────────────

class ReporterAgent:
    """Persists leads to SQLite and renders a self-contained HTML report."""

    log = make_logger("ReporterAgent")

    def __init__(self, output_dir: str = "output") -> None:
        self.output_dir  = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        db_dir           = self.output_dir / "DB"
        db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path     = db_dir / "scylladb_hunter.db"
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.report_path = self.output_dir / f"report_{ts}.html"
        self._init_db()

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, leads: list[Lead]) -> None:
        self.log.info(f"▶  Persisting {len(leads)} leads to SQLite...")
        self._save_to_db(leads)
        self.log.info(f"   DB → {self.db_path}")

        self.log.info("▶  Generating HTML report...")
        self.report_path.write_text(self._build_html(leads), encoding="utf-8")
        self.log.info(f"   Report → {self.report_path}")

    # ── SQLite ────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id          TEXT PRIMARY KEY,
                    created_at      TEXT,
                    total_leads     INTEGER,
                    qualified_leads INTEGER,
                    qa_passed       INTEGER
                );
            """)
            self._migrate_leads_table(con)

    def _migrate_leads_table(self, con: sqlite3.Connection) -> None:
        """Create or migrate the leads table to the current schema (PK = id only)."""
        # Detect old composite-PK schema by checking if status column exists
        existing = {row[1] for row in con.execute("PRAGMA table_info(leads)").fetchall()}
        if "status" not in existing:
            # Old schema (or no table) — drop and recreate cleanly
            con.execute("DROP TABLE IF EXISTS leads")
            con.execute("""
                CREATE TABLE leads (
                    id                       TEXT PRIMARY KEY,
                    run_id                   TEXT,
                    name                     TEXT,
                    title                    TEXT,
                    email                    TEXT,
                    linkedin_url             TEXT,
                    email_status             TEXT,
                    company_name             TEXT,
                    company_domain           TEXT,
                    company_industry         TEXT,
                    company_employees        INTEGER,
                    company_technologies     TEXT,
                    company_description      TEXT,
                    company_signal_score     INTEGER,
                    qualification_score      INTEGER,
                    qualification_reason     TEXT,
                    pain_category            TEXT,
                    pain_angle               TEXT,
                    disqualified             INTEGER,
                    disqualify_reason        TEXT,
                    linkedin_invite          TEXT,
                    follow_up_email_subject  TEXT,
                    follow_up_email_body     TEXT,
                    qa_passed                INTEGER,
                    qa_issues                TEXT,
                    processed_at             TEXT,
                    message_type             TEXT DEFAULT 'cold',
                    status                   TEXT DEFAULT 'pending',
                    status_updated_at        TEXT,
                    apollo_reachable         INTEGER DEFAULT 1
                )
            """)
        elif "apollo_reachable" not in existing:
            con.execute("ALTER TABLE leads ADD COLUMN apollo_reachable INTEGER DEFAULT 1")

    def _save_to_db(self, leads: list[Lead]) -> None:
        if not leads:
            return
        run_id     = leads[0].run_id
        qualified  = [l for l in leads if not l.disqualified]
        qa_ok      = [l for l in qualified if l.qa_passed]

        # Status rank — used in upsert to prevent downgrading a lead's status
        status_rank = "CASE status " \
            "WHEN 'response_received' THEN 3 " \
            "WHEN 'email_sent' THEN 2 " \
            "WHEN 'linkedin_sent' THEN 1 " \
            "ELSE 0 END"

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                (run_id, datetime.now(timezone.utc).isoformat(),
                 len(leads), len(qualified), len(qa_ok)),
            )
            con.executemany(
                f"""
                INSERT INTO leads VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    run_id                  = excluded.run_id,
                    name                    = excluded.name,
                    title                   = excluded.title,
                    email                   = excluded.email,
                    linkedin_url            = excluded.linkedin_url,
                    email_status            = excluded.email_status,
                    company_name            = excluded.company_name,
                    company_domain          = excluded.company_domain,
                    company_industry        = excluded.company_industry,
                    company_employees       = excluded.company_employees,
                    company_technologies    = excluded.company_technologies,
                    company_description     = excluded.company_description,
                    company_signal_score    = excluded.company_signal_score,
                    qualification_score     = excluded.qualification_score,
                    qualification_reason    = excluded.qualification_reason,
                    pain_category           = excluded.pain_category,
                    pain_angle              = excluded.pain_angle,
                    disqualified            = excluded.disqualified,
                    disqualify_reason       = excluded.disqualify_reason,
                    linkedin_invite         = excluded.linkedin_invite,
                    follow_up_email_subject = excluded.follow_up_email_subject,
                    follow_up_email_body    = excluded.follow_up_email_body,
                    qa_passed               = excluded.qa_passed,
                    qa_issues               = excluded.qa_issues,
                    processed_at            = excluded.processed_at,
                    message_type            = excluded.message_type,
                    -- Only advance status, never downgrade
                    status = CASE
                        WHEN ({status_rank.replace('status', 'leads.status')}) >=
                             ({status_rank.replace('status', 'excluded.status')})
                        THEN leads.status
                        ELSE excluded.status
                    END,
                    status_updated_at = CASE
                        WHEN ({status_rank.replace('status', 'leads.status')}) >=
                             ({status_rank.replace('status', 'excluded.status')})
                        THEN leads.status_updated_at
                        ELSE excluded.status_updated_at
                    END,
                    apollo_reachable        = excluded.apollo_reachable
                """,
                [
                    (
                        l.id, l.run_id, l.name, l.title, l.email,
                        l.linkedin_url, l.email_status,
                        l.company_name, l.company_domain, l.company_industry,
                        l.company_employees,
                        json.dumps(l.company_technologies),
                        l.company_description, l.company_signal_score,
                        l.qualification_score, l.qualification_reason,
                        l.pain_category, l.pain_angle,
                        int(l.disqualified), l.disqualify_reason,
                        l.linkedin_invite, l.follow_up_email_subject,
                        l.follow_up_email_body,
                        int(l.qa_passed), json.dumps(l.qa_issues),
                        l.processed_at, l.message_type,
                        l.status, l.status_updated_at,
                        int(l.apollo_reachable),
                    )
                    for l in leads
                ],
            )

    # ── HTML ──────────────────────────────────────────────────────────────────

    def _build_html(self, leads: list[Lead]) -> str:
        lacks_data   = [l for l in leads if l.status == "needs_enrichment"]
        already_sent = [l for l in leads if not l.disqualified and l.message_type == "skipped"]
        qualified    = [l for l in leads if not l.disqualified and l.message_type != "skipped"]
        disqualified = [l for l in leads if l.disqualified and l.status != "needs_enrichment"]
        qa_passed    = [l for l in qualified if l.qa_passed]
        run_id       = leads[0].run_id if leads else "N/A"
        ts           = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        qualified_cards    = "\n".join(_lead_card(l, show_copy=True)  for l in qualified)
        disqualified_cards = "\n".join(_lead_card(l, show_copy=False) for l in disqualified)
        lacks_data_cards   = "\n".join(_lead_card(l, show_copy=False) for l in lacks_data)
        already_sent_cards = "\n".join(_lead_card(l, show_copy=True)  for l in already_sent)

        no_leads_msg = '<p style="color:var(--muted);font-size:13px">None.</p>'

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ScyllaDB GTM Hunter — Run {run_id}</title>
<style>
  {_embedded_fonts()}
  {_CSS}
</style>
</head>
<body>
<div class="container">

<header>
  <div class="header-top">
    <div class="logo-area">
      <h1>🦑 <span>Scylla</span>DB GTM Hunter</h1>
      <div class="subtitle">// DataStax competitor displacement — automated pipeline</div>
    </div>
    <div class="run-badge">Run <strong>{run_id}</strong><br>{ts}</div>
  </div>
</header>

<div class="stats-row">
  {_stat_card(len(leads),        "Total Leads",    "#c8d8e8")}
  {_stat_card(len(qualified),    "Qualified",      "#00ff88")}
  {_stat_card(len(qa_passed),    "QA Passed",      "#0066ff")}
  {_stat_card(len(already_sent), "Already Touched","#f7c59f")}
  {_stat_card(len(lacks_data),   "Lacks Data",     "#a855f7")}
  {_stat_card(len(disqualified), "Disqualified",   "#ff4d6d")}
</div>

<section class="section">
  <div class="section-header">
    ✅ Qualified Leads <span class="count">{len(qualified)}</span>
  </div>
  {qualified_cards or no_leads_msg}
</section>

<section class="section">
  <div class="section-header">
    ⏭️ Already Touched — last 180 days <span class="count">{len(already_sent)}</span>
  </div>
  {already_sent_cards or no_leads_msg}
</section>

<section class="section">
  <div class="section-header">
    🔍 Lacks Contact Data — needs re-enrichment <span class="count">{len(lacks_data)}</span>
  </div>
  {lacks_data_cards or no_leads_msg}
</section>

<section class="section">
  <div class="section-header">
    ❌ Disqualified Leads <span class="count">{len(disqualified)}</span>
  </div>
  {disqualified_cards or no_leads_msg}
</section>

</div>

<footer>
  <div class="container">
    Generated by ScyllaDB GTM Hunter · {ts} · {len(leads)} leads processed
  </div>
</footer>
</body>
</html>"""
