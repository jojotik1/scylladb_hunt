"""
agents/sender.py
----------------
Agent 6 — SenderAgent

Responsibility:
  Simulate the outreach "trigger" — the moment messages would actually be
  dispatched to real people. No real messages are ever sent; this is a
  structured dry-run that:

    1. Prints a clear per-lead dispatch block to the console.
    2. Writes a machine-readable outreach log (JSON) to the output directory.

  This satisfies the assignment requirement:
    "Your code should demonstrate the 'trigger' logic. A dry-run mode that
     logs what would have been sent to a console or a file is perfect."

Output:
  data/output/outreach/outreach_<run_id>.json  — one record per lead that would be contacted
"""

import json
from datetime import datetime, timezone
from pathlib import Path

_STATUS_RANK = {"pending": 0, "linkedin_sent": 1, "email_sent": 2, "response_received": 3}

from models.lead import Lead
from utils.logging import make_logger


class SenderAgent:
    """Dry-run outreach trigger — logs what would have been sent."""

    log = make_logger("SenderAgent")

    def __init__(self, output_dir: str = "output") -> None:
        self.output_dir = Path(output_dir) / "output" / "outreach"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, leads: list[Lead]) -> list[Lead]:
        actionable = [
            l for l in leads
            if not l.disqualified and l.message_type != "skipped" and l.qa_passed
        ]

        self.log.info(f"▶  Dry-run trigger for {len(actionable)} lead(s)...")

        if not actionable:
            self.log.info("   No actionable leads this run — nothing to dispatch.")
            return leads

        run_id = actionable[0].run_id
        log_path = self.output_dir / f"outreach_{run_id}.json"


        records = []
        for lead in actionable:
            self._print_dispatch(lead)
            self._set_status(lead, "linkedin_sent")
            records.append(self._build_record(lead))

        log_path.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
        self.log.info(f"   Outreach log → {log_path}")

        return leads

    # ── Private helpers ───────────────────────────────────────────────────────

    def _set_status(self, lead: Lead, new_status: str) -> None:
        """Advance status only — never downgrade (e.g. response_received → linkedin_sent)."""
        if _STATUS_RANK.get(new_status, 0) > _STATUS_RANK.get(lead.status, 0):
            lead.status = new_status
            lead.status_updated_at = datetime.now(timezone.utc).isoformat()

    def _print_dispatch(self, lead: Lead) -> None:
        tag = "[SECOND TOUCH]" if lead.message_type == "second_touch" else "[COLD OUTREACH]"
        sep = "─" * 60
        print(f"\n{sep}")
        print(f"  DRY RUN {tag} — would send to:")
        print(f"  Name    : {lead.name}")
        print(f"  Title   : {lead.title}")
        print(f"  Company : {lead.company_name}")
        print(f"  Email   : {lead.email}  [{lead.email_status}]")
        if lead.linkedin_url:
            print(f"  LinkedIn: {lead.linkedin_url}")
        print()
        print("  >> LinkedIn Invite:")
        for line in lead.linkedin_invite.splitlines():
            print(f"     {line}")
        print()
        print(f"  >> Follow-up Email Subject: {lead.follow_up_email_subject}")
        print("  >> Follow-up Email Body:")
        for line in lead.follow_up_email_body.splitlines():
            print(f"     {line}")
        print(f"{sep}")

    def _build_record(self, lead: Lead) -> dict:
        return {
            "triggered_at":          datetime.now(timezone.utc).isoformat(),
            "dry_run":               True,
            "message_type":          lead.message_type,
            "lead": {
                "id":                lead.id,
                "name":              lead.name,
                "title":             lead.title,
                "company":           lead.company_name,
                "email":             lead.email,
                "email_status":      lead.email_status,
                "linkedin_url":      lead.linkedin_url,
                "pain_category":     lead.pain_category,
                "qualification_score": lead.qualification_score,
            },
            "outreach": {
                "linkedin_invite":        lead.linkedin_invite,
                "follow_up_email_subject": lead.follow_up_email_subject,
                "follow_up_email_body":   lead.follow_up_email_body,
            },
        }
