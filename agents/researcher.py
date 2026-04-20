"""
agents/researcher.py
--------------------
Agent 1 — ResearcherAgent

Responsibility:
  - Calls the Apollo pipeline (mock or live) to fetch enriched lead data.
  - Merges company technographic data with person records.
  - Detects the dominant pain category for each lead via keyword scoring.

Outputs:
  - List[Lead] with id, identity, company, pain_category, pain_angle, run_id all populated.
    Qualification fields are left at their defaults for QualifierAgent to fill.
"""

from datetime import datetime, timezone

from models.lead import Lead, PAIN_CATEGORIES, PAIN_KEYWORDS
from utils.logging import make_logger


class ResearcherAgent:
    """Loads enriched leads from Apollo and detects pain categories."""

    log = make_logger("ResearcherAgent")

    def __init__(self, apollo_client) -> None:
        self.apollo = apollo_client

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> list[Lead]:
        self.log.info("▶  Running full Apollo pipeline...")
        results = self.apollo.run_full_pipeline()

        enriched_people  = results["enriched_people"]
        companies_by_id  = {c["id"]: c for c in results["companies"]}

        run_id       = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        processed_at = datetime.now(timezone.utc).isoformat()

        leads = [
            self._build_lead(person, companies_by_id, run_id, processed_at)
            for person in enriched_people
        ]

        self.log.info(f"   Loaded {len(leads)} leads")
        return leads

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_lead(
        self,
        person: dict,
        companies_by_id: dict,
        run_id: str,
        processed_at: str,
    ) -> Lead:
        org          = person.get("organization", {})
        company_base = companies_by_id.get(org.get("id", ""), {})

        # Merge tech stacks from both sources, deduplicate
        tech_names = list(set(
            org.get("technology_names", []) +
            company_base.get("technology_names", [])
        ))

        description  = company_base.get("short_description", "")
        industry     = org.get("industry") or company_base.get("industry", "")
        signal_score = company_base.get("datastax_signal_score", 0)
        pain_cat     = self._detect_pain(description, tech_names, industry)

        return Lead(
            id=person["id"],
            first_name=person["first_name"],
            last_name=person["last_name"],
            name=person["name"],
            title=person["title"],
            email=person.get("email", ""),
            linkedin_url=person.get("linkedin_url", ""),
            email_status=person.get("email_status", "unknown"),
            apollo_reachable=person.get("_apollo_reachable", True),
            company_name=org.get("name", ""),
            company_domain=org.get("primary_domain", ""),
            company_industry=industry,
            company_employees=org.get("estimated_num_employees", 0),
            company_technologies=tech_names,
            company_description=description,
            company_signal_score=signal_score,
            pain_category=pain_cat,
            pain_angle=PAIN_CATEGORIES.get(pain_cat, ""),
            run_id=run_id,
            processed_at=processed_at,
        )

    def _detect_pain(
        self,
        description: str,
        techs: list[str],
        industry: str,
    ) -> str:
        """Score each pain category by keyword hits; return the top match."""
        combined = (description + " " + " ".join(techs) + " " + industry).lower()
        scores = {
            cat: sum(1 for kw in keywords if kw in combined)
            for cat, keywords in PAIN_KEYWORDS.items()
        }
        return max(scores, key=scores.get)
