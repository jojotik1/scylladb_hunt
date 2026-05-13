"""
agents/qualifier.py
-------------------
Agent 2 — QualifierAgent

Responsibility:
  - Hard-disqualify Competitor employees and junior titles immediately.
  - Score every remaining lead on a 20-point rubric.
  - Mark leads below the threshold as disqualified.
  - Return ALL leads (both qualified and disqualified) so the reporter
    can display the full picture.

Scoring rubric (max 20 pts):
  Seniority          0–6 pts
  Company size       0–4 pts
  Competitor signal    0–4 pts
  Email status       0–3 pts
  LinkedIn URL       0–2 pts
  Tech detected      0–1 pt
"""

from datetime import datetime, timezone

from models.lead import Lead
from utils.logging import make_logger


class QualifierAgent:
    """Scores leads and marks disqualified ones."""

    log = make_logger("QualifierAgent")

    PASS_THRESHOLD = 12

    # Highest matching keyword wins (not additive)
    TITLE_SENIORITY_SCORES: dict[str, int] = {
        "cto":       6,
        "vp":        5,
        "head of":   5,
        "director":  4,
        "principal": 3,
        "staff":     3,
        "manager":   3,
        "senior":    1,
        "engineer":  0,
        "analyst":   0,
    }

    DISQUALIFY_TITLES: list[str] = [
        "intern", "junior", "jr.", "associate engineer", "entry",
    ]

    DISQUALIFY_COMPANIES: list[str] = [
        "competitor", "astra db", "competitor project",
    ]

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self, leads: list[Lead]) -> list[Lead]:
        self.log.info(f"▶  Qualifying {len(leads)} leads...")

        for lead in leads:
            self._qualify(lead)

        n_qualified    = sum(1 for l in leads if not l.disqualified)
        n_disqualified = len(leads) - n_qualified
        self.log.info(f"   ✅ Qualified: {n_qualified}  ❌ Disqualified: {n_disqualified}")

        return leads  # reporter needs the full set

    # ── Private helpers ───────────────────────────────────────────────────────

    def _qualify(self, lead: Lead) -> None:
        if self._check_contact_data(lead):
            return
        if self._hard_disqualify(lead):
            return
        self._score(lead)

    def _check_contact_data(self, lead: Lead) -> bool:
        """Flag leads missing email or LinkedIn URL for future re-enrichment."""
        missing = []
        if not lead.email:
            missing.append("no email")
        if not lead.linkedin_url:
            missing.append("no LinkedIn URL")
        if not missing:
            return False

        lead.disqualified = True
        lead.disqualify_reason = "Missing contact data: " + " & ".join(missing)
        lead.status = "needs_enrichment"
        lead.status_updated_at = datetime.now(timezone.utc).isoformat()
        self.log.info(
            f"   ⚠️  {lead.name} ({lead.company_name}) — {lead.disqualify_reason}"
        )
        return True

    def _hard_disqualify(self, lead: Lead) -> bool:
        """Return True (and mutate lead) if the lead is an instant reject."""
        title_lower   = lead.title.lower()
        company_lower = lead.company_name.lower()

        for term in self.DISQUALIFY_TITLES:
            if term in title_lower:
                lead.disqualified     = True
                lead.disqualify_reason = f"Title contains disqualifying term: '{term}'"
                return True

        for name in self.DISQUALIFY_COMPANIES:
            if name in company_lower:
                lead.disqualified     = True
                lead.disqualify_reason = f"Works at competitor/vendor: '{lead.company_name}'"
                return True

        return False

    def _score(self, lead: Lead) -> None:
        score   = 0
        reasons = []

        # 1. Seniority (0–6 pts)
        title_lower   = lead.title.lower()
        seniority_pts = max(
            (pts for kw, pts in self.TITLE_SENIORITY_SCORES.items() if kw in title_lower),
            default=0,
        )
        score += seniority_pts
        reasons.append(f"Seniority: +{seniority_pts}")

        # 2. Company size (0–4 pts)
        emp      = lead.company_employees
        size_pts = 4 if emp >= 1000 else 3 if emp >= 500 else 2 if emp >= 200 else 1 if emp >= 50 else 0
        score   += size_pts
        reasons.append(f"Company size ({emp} emp): +{size_pts}")

        # 3. Competitor signal score (0–4 pts)
        sig     = lead.company_signal_score
        sig_pts = min(4, sig // 25)
        score  += sig_pts
        reasons.append(f"Signal score ({sig}): +{sig_pts}")

        # 4. Email status (0–3 pts)
        email_pts = {"verified": 3, "guessed": 1}.get(lead.email_status, 0)
        score    += email_pts
        reasons.append(f"Email ({lead.email_status}): +{email_pts}")

        # 5. LinkedIn URL present (0–2 pts)
        if lead.linkedin_url:
            score += 2
            reasons.append("LinkedIn URL: +2")

        # 6. Competitor tech detected (0–1 pt)
        has_ds_tech = any(
            "competitor" in t.lower() or "astra" in t.lower()
            for t in lead.company_technologies
        )
        if has_ds_tech:
            score += 1
            reasons.append("Competitor tech detected: +1")

        lead.qualification_score  = score
        lead.qualification_reason = " | ".join(reasons)

        if score < self.PASS_THRESHOLD:
            lead.disqualified     = True
            lead.disqualify_reason = f"Score {score} below threshold {self.PASS_THRESHOLD}"
