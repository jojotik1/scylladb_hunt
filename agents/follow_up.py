"""
agents/follow_up.py
-------------------
FollowUpAgent — Step 2 of the outreach sequence.

Run this after main.py. It queries the DB for leads that:
  1. Have status = TRIGGER_STATUS (default: "linkedin_sent")
  2. Have NOT responded (status != "response_received")
  3. Have had their LinkedIn invite sitting for at least FOLLOW_UP_DELAY_DAYS

For every qualifying lead it dispatches a dry-run follow-up email — prints to
console and appends to data/output/outreach/followup_<timestamp>.json.
Updates the lead's status to "email_sent" in the DB.

Configurable constants (top of file):
  TRIGGER_STATUS       — status that indicates a LinkedIn invite was sent
  FOLLOW_UP_DELAY_DAYS — days to wait before sending the follow-up email
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

# Allow running this file directly: python agents/follow_up.py
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.logging import make_logger

if TYPE_CHECKING:
    from models.lead import Lead


TRIGGER_STATUS       = "linkedin_sent"   # leads in this state are candidates
FOLLOW_UP_DELAY_DAYS = 5                 # days after LinkedIn invite before emailing


class FollowUpAgent:
    """Sends follow-up emails to LinkedIn-invited leads who haven't responded."""

    log = make_logger("FollowUpAgent")

    def __init__(self, db_path: str, output_dir: str = "data") -> None:
        self.db_path    = Path(db_path)
        self.output_dir = Path(output_dir) / "output" / "outreach"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self, leads: "list[Lead] | None" = None) -> None:
        if not self.db_path.exists():
            self.log.error(f"DB not found at {self.db_path}. Run main.py first.")
            return

        candidates = self._fetch_candidates()
        self.log.info(f"▶  Found {len(candidates)} lead(s) with status '{TRIGGER_STATUS}'")

        ready      = []
        too_soon   = []
        no_copy    = []

        for lead in candidates:
            if not lead.get("follow_up_email_body"):
                no_copy.append(lead)
            elif self._days_since(lead["status_updated_at"]) >= FOLLOW_UP_DELAY_DAYS:
                ready.append(lead)
            else:
                too_soon.append(lead)

        for lead in no_copy:
            self.log.warning(
                f"   ⚠️  {lead['name']} ({lead['company_name']}) — "
                f"no follow-up email copy found, skipping"
            )

        for lead in too_soon:
            days = self._days_since(lead["status_updated_at"])
            self.log.info(
                f"   ⏳ {lead['name']} ({lead['company_name']}) — "
                f"only {days:.0f} day(s) since invite, waiting until day {FOLLOW_UP_DELAY_DAYS}"
            )

        if not ready:
            self.log.info("   No leads ready for follow-up yet.")
            return

        self.log.info(f"   {len(ready)} lead(s) ready for follow-up email...")

        records  = []
        ts       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        log_path = self.output_dir / f"followup_{ts}.json"

        # Build a fast lookup map if in-memory leads were passed
        leads_by_id = {l.id: l for l in leads} if leads else {}

        now = datetime.now(timezone.utc).isoformat()
        for lead in ready:
            self._print_dispatch(lead)
            self._update_status(lead["id"], "email_sent")
            records.append(self._build_record(lead))
            # Mirror the status change onto the in-memory Lead object so the
            # reporter can render the follow-up email in the same run's report
            if lead["id"] in leads_by_id:
                mem_lead = leads_by_id[lead["id"]]
                mem_lead.status                  = "email_sent"
                mem_lead.status_updated_at        = now
                mem_lead.follow_up_email_subject  = lead["follow_up_email_subject"] or ""
                mem_lead.follow_up_email_body     = lead["follow_up_email_body"] or ""

        log_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log.info(f"   Follow-up log → {log_path}")

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _fetch_candidates(self) -> list[dict]:
        """Return all leads with TRIGGER_STATUS that have not responded."""
        with sqlite3.connect(self.db_path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute("""
                SELECT id, name, title, email, company_name, linkedin_url,
                       follow_up_email_subject, follow_up_email_body,
                       status, status_updated_at, pain_category
                FROM leads
                WHERE status = ?
            """, (TRIGGER_STATUS,)).fetchall()
        return [dict(r) for r in rows]

    def _update_status(self, lead_id: str, new_status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "UPDATE leads SET status = ?, status_updated_at = ? WHERE id = ?",
                (new_status, now, lead_id),
            )

    # ── Timing logic ───────────────────────────────────────────────────────────

    def _days_since(self, iso_ts: str) -> float:
        sent_at = datetime.fromisoformat(iso_ts)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - sent_at).total_seconds() / 86400

    # ── Dry-run output ─────────────────────────────────────────────────────────

    def _print_dispatch(self, lead: dict) -> None:
        days = self._days_since(lead["status_updated_at"])
        sep  = "─" * 60
        print(f"\n{sep}")
        print(f"  DRY RUN [FOLLOW-UP EMAIL] — {days:.0f} days since LinkedIn invite")
        print(f"  Name    : {lead['name']}")
        print(f"  Title   : {lead['title']}")
        print(f"  Company : {lead['company_name']}")
        print(f"  Email   : {lead['email']}")
        if lead.get("linkedin_url"):
            print(f"  LinkedIn: {lead['linkedin_url']}")
        print()
        print(f"  >> Subject: {lead['follow_up_email_subject']}")
        print("  >> Body:")
        for line in lead["follow_up_email_body"].splitlines():
            print(f"     {line}")
        print(sep)

    def _build_record(self, lead: dict) -> dict:
        return {
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "dry_run":      True,
            "step":         "follow_up_email",
            "days_since_linkedin_invite": round(self._days_since(lead["status_updated_at"])),
            "lead": {
                "id":          lead["id"],
                "name":        lead["name"],
                "title":       lead["title"],
                "company":     lead["company_name"],
                "email":       lead["email"],
                "linkedin_url": lead.get("linkedin_url", ""),
            },
            "email": {
                "subject": lead["follow_up_email_subject"],
                "body":    lead["follow_up_email_body"],
            },
        }


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from utils.logging import configure_logging

    def _parse_args() -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="follow-up",
            description=(
                "GTM Hunter — Follow-up email dispatcher. "
                f"Sends emails to leads with status '{TRIGGER_STATUS}' "
                f"that have not responded after {FOLLOW_UP_DELAY_DAYS} days."
            ),
        )
        parser.add_argument(
            "--output-dir",
            default="data",
            metavar="DIR",
            help="Base output directory (default: data/). Must match the directory used by main.py.",
        )
        return parser.parse_args()

    configure_logging()
    args = _parse_args()

    output_dir = args.output_dir
    db_path    = str(Path(output_dir) / "DB" / "gtm_hunter.db")

    sep = "═" * 60
    print(f"\n{sep}")
    print("  GTM Hunter — Follow-Up Email Dispatcher")
    print(f"  Trigger status : '{TRIGGER_STATUS}'")
    print(f"  Delay threshold: {FOLLOW_UP_DELAY_DAYS} days")
    print(f"  DB             : {db_path}")
    print(f"{sep}\n")

    agent = FollowUpAgent(db_path=db_path, output_dir=output_dir)
    agent.run()

    print(f"\n{sep}\n")
