"""
main.py
-------
CLI entrypoint for the ScyllaDB GTM Hunter.

This file does exactly two things:
  1. Parse CLI arguments.
  2. Call run_pipeline(), which owns the agent orchestration logic.

Nothing domain-specific lives here — import agents from the agents/ package.

Usage:
  python main.py                          # dry-run, output in ./output/
  python main.py --output-dir ./results   # custom output directory
  python main.py --api-key sk-ant-...     # use Claude API for real copy
  ANTHROPIC_API_KEY=sk-ant-... python main.py
"""

import argparse
import os
import sys
from pathlib import Path

# Ensure Unicode output works on Windows terminals with narrow code pages
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure the project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent))

from utils.logging import configure_logging, make_logger
from agents import (
    ResearcherAgent,
    QualifierAgent,
    CopywriterAgent,
    QAAgent,
    SenderAgent,
    ReporterAgent,
    FollowUpAgent,
)

log = make_logger("main")


# ── Orchestrator ──────────────────────────────────────────────────────────────

def run_pipeline(output_dir: str = "data", api_key: str = "", apollo_key: str = "") -> None:
    """
    Execute the GTM Hunter pipeline end-to-end.

    Stages:
      1. ResearcherAgent  — fetch & enrich leads via Apollo (mock mode)
      2. QualifierAgent   — score leads, hard-disqualify ineligible ones
      3. CopywriterAgent  — generate personalised LinkedIn invite + follow-up email
      4. QAAgent          — deterministic quality gate (char limits, phrase checks)
      5. SenderAgent      — dry-run LinkedIn invite dispatch
      6. FollowUpAgent    — dispatch follow-up emails for linkedin_sent leads ≥5 days old
      7. ReporterAgent    — persist to SQLite, render self-contained HTML report
    """
    sep = "═" * 60
    print(f"\n{sep}")
    print("  🦑  ScyllaDB GTM Hunter — 5-Stage Multi-Agent Pipeline")
    print(f"{sep}\n")

    # Apollo client — live if a key is provided, mock otherwise
    from connectors.apollo import create_mock_client, create_live_client
    resolved_apollo_key = apollo_key or os.environ.get("APOLLO_API_KEY", "")
    if resolved_apollo_key:
        apollo_client = create_live_client(api_key=resolved_apollo_key, verbose=False)
    else:
        apollo_client = create_mock_client(verbose=False)

    # Instantiate agents — reporter first so DB tables exist before copywriter queries them
    reporter   = ReporterAgent(output_dir=output_dir)
    researcher = ResearcherAgent(apollo_client)
    qualifier  = QualifierAgent()
    copywriter = CopywriterAgent(api_key=api_key, db_path=str(reporter.db_path))
    qa_agent   = QAAgent()
    sender     = SenderAgent(output_dir=output_dir)
    follow_up  = FollowUpAgent(db_path=str(reporter.db_path), output_dir=output_dir)

    # Run pipeline
    leads = researcher.run()
    leads = qualifier.run(leads)
    leads = copywriter.run(leads)
    leads = qa_agent.run(leads)
    leads = sender.run(leads)
    follow_up.run(leads)   # dispatches follow-up emails, updates Lead objects in memory
    reporter.run(leads)    # sees email_sent status, renders complete report

    # Summary
    already_touched  = [l for l in leads if not l.disqualified and (l.message_type == "skipped" or l.status == "email_sent")]
    lacks_data       = [l for l in leads if l.disqualified and "Missing contact data" in (l.disqualify_reason or "")]
    disqualified     = [l for l in leads if l.disqualified and l not in lacks_data]
    qualified        = [l for l in leads if not l.disqualified and l not in already_touched]
    qa_ok            = [l for l in qualified if l.qa_passed]

    print(f"\n{sep}")
    print("  Pipeline Summary")
    print(f"  Leads loaded:      {len(leads)}")
    print(f"  Qualified:         {len(qualified)}")
    print(f"  QA passed:         {len(qa_ok)}")
    print(f"  Already Touched:   {len(already_touched)}")
    print(f"  Lacks Contact Data:{len(lacks_data)}")
    print(f"  Disqualified:      {len(disqualified)}")
    print(f"  DB:                {reporter.db_path}")
    print(f"  Report:            {reporter.report_path}")
    print(f"{sep}\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scylladb-hunter",
        description="ScyllaDB GTM Hunter — automated DataStax competitor displacement pipeline",
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        metavar="DIR",
        help="Base output directory (default: data/). "
             "DB → <DIR>/DB/scylladb_hunter.db | "
             "Reports → <DIR>/output/reports/ | "
             "Outreach → <DIR>/output/outreach/",
    )
    parser.add_argument(
        "--api-key",
        default="",
        metavar="KEY",
        help="Anthropic API key — overrides the ANTHROPIC_API_KEY env var. "
             "Omit for dry-run demo mode with mock copy templates.",
    )
    parser.add_argument(
        "--apollo-key",
        default="",
        metavar="KEY",
        help="Apollo.io API key — overrides the APOLLO_API_KEY env var. "
             "Omit to run Apollo in mock mode (no credits consumed).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    configure_logging()
    args = _parse_args()
    run_pipeline(output_dir=args.output_dir, api_key=args.api_key, apollo_key=args.apollo_key)
