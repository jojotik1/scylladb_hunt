"""
Apollo API Connector — ScyllaDB GTM Hunter
==========================================
Wraps the four Apollo endpoints needed for the hunter workflow.
Supports LIVE mode (real API key) and MOCK mode (offline, no credits consumed).

Endpoints:
    POST /api/v1/mixed_companies/search       – Find target companies
    POST /api/v1/organizations/bulk_enrich    – Enrich company technographics
    POST /api/v1/mixed_people/api_search      – Find people (names obfuscated)
    POST /api/v1/people/bulk_match            – Full enrich: LinkedIn URL + email
"""

import asyncio
import json
import logging
import random
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import os
import httpx
from pathlib import Path

# ─── Load mock data from JSON file ────────────────────────────────────────────

def _load_mock_data() -> dict:
    """Load mock data from data/mock_data_source/apollo_mock_data.json."""
    project_root = Path(__file__).parent.parent
    candidates = [
        str(project_root / "data" / "mock_data_source" / "apollo_mock_data.json"),
        "data/mock_data_source/apollo_mock_data.json",
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    raise FileNotFoundError(
        "apollo_mock_data.json not found. Expected at data/mock_data_source/apollo_mock_data.json."
    )

_MOCK_DATA = _load_mock_data()
MOCK_COMPANY_SEARCH    = _MOCK_DATA["mock_company_search"]
MOCK_ORG_BULK_ENRICH   = _MOCK_DATA["mock_org_bulk_enrich"]
MOCK_PEOPLE_SEARCH     = _MOCK_DATA["mock_people_search"]
MOCK_PEOPLE_BULK_MATCH = _MOCK_DATA["mock_people_bulk_match"]

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="[%(asctime)s] [ApolloConnector] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    level=logging.DEBUG,
)
logger = logging.getLogger("apollo")

# ─── Errors ───────────────────────────────────────────────────────────────────


class ApolloAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint

    def __str__(self):
        parts = [super().__str__()]
        if self.status_code:
            parts.append(f"status={self.status_code}")
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        return " | ".join(parts)


# ─── Config ───────────────────────────────────────────────────────────────────

APOLLO_BASE_URL = "https://api.apollo.io/api/v1"

DEFAULT_PERSON_TITLES = [
    "VP of Engineering",
    "VP Engineering",
    "Head of Data",
    "Head of Data Platform",
    "Principal Engineer",
    "Principal Data Engineer",
    "Staff Engineer",
    "Staff Software Engineer",
    "Engineering Manager",
    "Director of Engineering",
    "CTO",
]

DEFAULT_INDUSTRIES = [
    "Financial Services",
    "Online Gaming",
    "Internet of Things",
    "Data & Analytics",
    "Payments",
]

DEFAULT_EMPLOYEE_RANGES = ["201,500", "501,1000", "1001,5000"]

DEFAULT_TECHNOLOGIES = ["DataStax", "Apache Cassandra", "Cassandra"]


# ─── Reachability ─────────────────────────────────────────────────────────────

# Ordered list of outreach channels, from highest to lowest preference.
# Each entry describes what Apollo signal combination qualifies a person for it.
REACHABILITY_CHANNELS = [
    {
        "channel": "email",
        "label": "Email (verified)",
        "requires": lambda p: p.get("has_email") and p.get("email_status") == "verified",
    },
    {
        "channel": "email_guessed",
        "label": "Email (guessed — verify before sending)",
        "requires": lambda p: p.get("has_email") and p.get("email_status") == "guessed",
    },
    {
        "channel": "phone",
        "label": "Direct phone",
        "requires": lambda p: p.get("has_direct_phone") == "Yes",
    },
    {
        "channel": "linkedin",
        "label": "LinkedIn (URL known)",
        "requires": lambda p: bool(p.get("linkedin_url")),
    },
    {
        "channel": "linkedin_search",
        "label": "LinkedIn (find manually by name + company)",
        "requires": lambda p: bool(p.get("first_name") and p.get("organization")),
    },
]


def classify_reachability(person: dict) -> dict:
    """
    Annotate a person dict with reachability metadata without removing anyone.

    Adds three keys:
        _apollo_reachable (bool):    True if Apollo has email or direct phone.
        _outreach_channels (list):   Ordered list of viable channel names,
                                     e.g. ["email", "linkedin"].
        _outreach_note (str):        Human-readable summary for the report/DB.

    People with no Apollo contact data are flagged for LinkedIn manual outreach
    rather than dropped — they still have name + company + title signal.
    """
    channels = [
        ch["channel"]
        for ch in REACHABILITY_CHANNELS
        if ch["requires"](person)
    ]

    apollo_reachable = any(
        ch in channels for ch in ("email", "email_guessed", "phone")
    )

    if not channels:
        note = "No contact data in Apollo — search LinkedIn by name and company."
    else:
        labels = [ch["label"] for ch in REACHABILITY_CHANNELS if ch["channel"] in channels]
        note = "Preferred outreach: " + " → ".join(labels)

    return {
        **person,
        "_apollo_reachable": apollo_reachable,
        "_outreach_channels": channels,
        "_outreach_note": note,
    }


# ─── Client ───────────────────────────────────────────────────────────────────


@dataclass
class ApolloClient:
    """
    Apollo API client with mock/live mode support.

    Args:
        api_key:    Apollo master API key (required in live mode).
        mock_mode:  Use offline mock data (default: True).
        timeout:    HTTP timeout in seconds (default: 15).
        verbose:    Enable detailed request/response logging (default: True).
    """

    api_key: str | None = None
    mock_mode: bool = True
    timeout: float = 15.0
    verbose: bool = True

    def __post_init__(self):
        if not self.mock_mode and not self.api_key:
            raise ValueError("api_key is required when mock_mode=False")

        mode_label = "🟡 MOCK" if self.mock_mode else "🟢 LIVE"
        logger.info(f"Initialized in {mode_label} mode")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "Accept": "application/json",
            "x-api-key": self.api_key or "",
        }

    def _add_meta(self, data: dict, endpoint: str, request_params: dict = {}) -> dict:
        result = deepcopy(data)
        result["_meta"] = {
            "mode": "mock",
            "endpoint": f"{APOLLO_BASE_URL}/{endpoint}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_params": request_params,
            "note": "Offline mock data — API credits are not consumed in mock mode. Set mock_mode=False to use live Apollo API.",
        }
        return result

    def _simulate_latency(self, base_ms: int = 300) -> None:
        """Simulate realistic API latency in mock mode."""
        delay = (base_ms + random.randint(0, 300)) / 1000
        time.sleep(delay)

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{APOLLO_BASE_URL}{endpoint}"
        if self.verbose:
            logger.debug(f"POST {url}\n{json.dumps(payload, indent=2)}")

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException:
            raise ApolloAPIError(f"Request timed out after {self.timeout}s", endpoint=endpoint)

        if not response.is_success:
            raise ApolloAPIError(
                f"HTTP {response.status_code}: {response.text}",
                status_code=response.status_code,
                endpoint=endpoint,
            )

        return response.json()

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Company Search
    #    POST /api/v1/mixed_companies/search
    #
    #    Strategy: search for companies in industries known for DataStax/Cassandra
    #    workloads. Filter by size and optionally by detected technology stack.
    # ──────────────────────────────────────────────────────────────────────────

    def search_companies(
        self,
        industries: list[str] = DEFAULT_INDUSTRIES,
        employee_ranges: list[str] = DEFAULT_EMPLOYEE_RANGES,
        technologies: list[str] = DEFAULT_TECHNOLOGIES,
        keywords: list[str] | None = None,
        locations: list[str] | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        """
        Search for companies likely using DataStax.

        Args:
            industries:      Industry verticals to target.
            employee_ranges: Apollo employee range strings, e.g. ["201,500"].
            technologies:    Technology names to filter by (technographic signal).
            keywords:        Free-text keywords (e.g. ["real-time", "cassandra"]).
            locations:       Company HQ locations, e.g. ["California, US"].
            page:            Page number (default 1).
            per_page:        Results per page, max 100 (default 10).

        Returns:
            Apollo mixed_companies/search response dict.
        """
        if self.mock_mode:
            logger.info("🟡 [MOCK] search_companies — returning offline mock data")
            self._simulate_latency()
            return self._add_meta(
                MOCK_COMPANY_SEARCH,
                "mixed_companies/search",
                {"industries": industries, "employee_ranges": employee_ranges},
            )

        payload: dict[str, Any] = {
            "organization_industry_tag_ids": industries,
            "organization_num_employees_ranges": employee_ranges,
            "currently_using_any_of_technology_uids": technologies,
            "page": page,
            "per_page": per_page,
        }
        if keywords:
            payload["q_keywords"] = " ".join(keywords)
        if locations:
            payload["organization_locations"] = locations

        logger.info("🟢 [LIVE] search_companies — calling Apollo API")
        return self._post("/mixed_companies/search", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Organization Bulk Enrich
    #    POST /api/v1/organizations/bulk_enrich
    #
    #    Strategy: confirm DataStax usage + get full technographic profiles,
    #    funding info, and departmental headcount.
    # ──────────────────────────────────────────────────────────────────────────

    def bulk_enrich_organizations(self, organizations: list[dict]) -> dict:
        """
        Bulk enrich organizations by domain or Apollo ID.

        Args:
            organizations: List of dicts with 'id' and/or 'domain' keys.
                           Example: [{"id": "abc123", "domain": "example.com"}]
                           Apollo allows max 10 per call.

        Returns:
            Enriched organization data including tech stack and headcount.
        """
        if self.mock_mode:
            logger.info("🟡 [MOCK] bulk_enrich_organizations — returning offline mock data")
            self._simulate_latency()
            return self._add_meta(
                MOCK_ORG_BULK_ENRICH,
                "organizations/bulk_enrich",
                {"organization_count": len(organizations)},
            )

        if len(organizations) > 10:
            raise ApolloAPIError(
                "Apollo allows max 10 orgs per bulk_enrich call. Split into batches.",
                endpoint="/organizations/bulk_enrich",
            )

        payload = {
            "organizations": [
                {k: v for k, v in {"id": o.get("id"), "domain": o.get("domain")}.items() if v}
                for o in organizations
            ]
        }

        logger.info(f"🟢 [LIVE] bulk_enrich_organizations — enriching {len(organizations)} orgs")
        return self._post("/organizations/bulk_enrich", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # 3. People Search
    #    POST /api/v1/mixed_people/api_search
    #
    #    Strategy: within confirmed companies, find engineering/data decision-makers.
    #    Names come back obfuscated.
    # ──────────────────────────────────────────────────────────────────────────

    def search_people(
        self,
        titles: list[str] = DEFAULT_PERSON_TITLES,
        domains: list[str] | None = None,
        org_ids: list[str] | None = None,
        seniorities: list[str] | None = None,
        page: int = 1,
        per_page: int = 10,
    ) -> dict:
        """
        Search for relevant people within target companies (free, no credits).

        Names are obfuscated in the response. Use bulk_match_people() to unlock
        full profiles for selected leads.

        Args:
            titles:      Target job titles.
            domains:     Company domains to scope the search.
            org_ids:     Apollo organization IDs (alternative to domains).
            seniorities: e.g. ["vp", "director", "manager", "individual_contributor"].
            page:        Page number.
            per_page:    Results per page, max 100.

        Returns:
            People search response with obfuscated names.
        """
        if self.mock_mode:
            logger.info("🟡 [MOCK] search_people — returning offline mock data")
            self._simulate_latency()
            return self._add_meta(
                MOCK_PEOPLE_SEARCH,
                "mixed_people/api_search",
                {"person_titles": titles, "domains": domains},
            )

        payload: dict[str, Any] = {
            "person_titles": titles,
            "page": page,
            "per_page": per_page,
        }
        if seniorities:
            payload["person_seniorities"] = seniorities
        if domains:
            payload["q_organization_domains"] = domains
        if org_ids:
            payload["organization_ids"] = org_ids

        logger.info(f"🟢 [LIVE] search_people — targeting {len(domains or org_ids or [])} companies")
        return self._post("/mixed_people/api_search", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # 4. People Bulk Match (Full Enrich)
    #    POST /api/v1/people/bulk_match
    #
    #    Strategy: enrich only SELECTED leads (those passing scoring filters)
    #    to get: full name, LinkedIn URL, verified email, employment history.
    # ──────────────────────────────────────────────────────────────────────────

    def bulk_match_people(self, people: list[dict]) -> dict:
        """
        Fully enrich people by Apollo ID.

        Unlocks: full name, LinkedIn URL, verified email, employment history.
        Only call this for leads that have passed your quality filters.

        Args:
            people: List of dicts with at least an 'id' key from search_people().
                    Apollo allows max 10 per call.

        Returns:
            Fully enriched person profiles with LinkedIn URLs and emails.
        """
        if self.mock_mode:
            logger.info("🟡 [MOCK] bulk_match_people — returning offline mock data")
            logger.info(f"   Would have enriched {len(people)} people (mock mode — no real API calls made)")
            self._simulate_latency(base_ms=600)
            return self._add_meta(
                MOCK_PEOPLE_BULK_MATCH,
                "people/bulk_match",
                {
                    "requested_ids": [p.get("id") for p in people],
                    "credits_consumed": 0,
                    "note": "In LIVE mode this endpoint makes real Apollo API calls and consumes credits.",
                },
            )

        if len(people) > 10:
            logger.warning(f"bulk_match_people: Apollo limit is 10/call. Truncating from {len(people)} to 10.")
            people = people[:10]

        payload = {
            "details": [
                {
                    "id": p["id"],
                    "reveal_personal_emails": False,
                    "reveal_phone_number": False,
                }
                for p in people
            ]
        }

        logger.info(f"🟢 [LIVE] bulk_match_people — enriching {len(people)} people (CREDIT-CONSUMING)")
        return self._post("/people/bulk_match", payload)

    # ──────────────────────────────────────────────────────────────────────────
    # Full Pipeline
    # ──────────────────────────────────────────────────────────────────────────

    def run_full_pipeline(
        self,
        min_signal_score: int = 80,
        max_leads_to_enrich: int = 5,
    ) -> dict:
        """
        Run the full GTM hunter pipeline end-to-end:

        1. Find companies likely using DataStax (free)
        2. Enrich companies to confirm tech stack (free)
        3. Search for decision-makers within qualified companies (free)
        4. Enrich selected leads to get LinkedIn URLs + emails (credit-consuming)

        Args:
            min_signal_score:    Minimum datastax_signal_score to qualify a company (0–100).
            max_leads_to_enrich: Max people to enrich with credits.

        Returns:
            Dict with keys: companies, enriched_companies, qualified_companies,
                            raw_people, enriched_people.
        """
        sep = "═" * 50
        logger.info(sep)
        logger.info(" ScyllaDB GTM Hunter — Full Pipeline Starting")
        logger.info(sep)

        # Step 1: Company Discovery
        logger.info("STEP 1/4 — Searching for DataStax-adjacent companies...")
        company_results = self.search_companies()
        companies = company_results.get("organizations", [])
        logger.info(f"  Found {len(companies)} candidate companies")

        # Step 2: Company Enrichment + Filtering
        logger.info("STEP 2/4 — Enriching company technographics...")
        enrich_input = [{"id": c["id"], "domain": c["primary_domain"]} for c in companies]
        enriched_result = self.bulk_enrich_organizations(enrich_input)
        enriched_companies = enriched_result.get("organizations", [])

        qualified = [c for c in companies if c.get("datastax_signal_score", 0) >= min_signal_score]
        logger.info(
            f"  {len(qualified)}/{len(companies)} companies passed "
            f"signal score threshold (≥{min_signal_score})"
        )

        # Step 3: People Search
        logger.info("STEP 3/4 — Searching for target personas in qualified companies...")
        domains = [c["primary_domain"] for c in qualified if c.get("primary_domain")]
        people_results = self.search_people(domains=domains)
        raw_people = people_results.get("people", [])
        logger.info(f"  Found {len(raw_people)} matching people (names still obfuscated)")

        # Step 3b: Classify reachability for ALL people — no one is dropped.
        # People without Apollo contact data are flagged for LinkedIn outreach instead.
        raw_people = [classify_reachability(p) for p in raw_people]

        apollo_reachable  = [p for p in raw_people if p["_apollo_reachable"]]
        linkedin_only     = [p for p in raw_people if not p["_apollo_reachable"]]
        logger.info(
            f"  Reachability: {len(apollo_reachable)} via Apollo (email/phone), "
            f"{len(linkedin_only)} LinkedIn-only"
        )
        if linkedin_only:
            logger.info(
                f"  LinkedIn-only leads will NOT consume credits but are kept for "
                f"manual/LinkedIn outreach: "
                + ", ".join(p["first_name"] for p in linkedin_only)
            )

        # Step 4: People Enrichment (credit-consuming).
        # Only enrich leads Apollo can actually reach; LinkedIn-only leads are
        # preserved in the output with _apollo_reachable=False so downstream
        # systems (CRM, sequencer) can route them to the right channel.
        to_enrich = apollo_reachable[:max_leads_to_enrich]
        logger.info(f"STEP 4/4 — Enriching {len(to_enrich)} Apollo-reachable leads (LinkedIn URLs, emails)...")
        enriched_people_result = self.bulk_match_people(to_enrich)
        enriched_people = enriched_people_result.get("matches", [])

        # Carry reachability flags forward onto enriched records too.
        flags_by_id = {p["id"]: p for p in raw_people}
        for ep in enriched_people:
            source_flags = flags_by_id.get(ep["id"], {})
            ep["_apollo_reachable"]  = source_flags.get("_apollo_reachable", True)
            ep["_outreach_channels"] = source_flags.get("_outreach_channels", [])
            ep["_outreach_note"]     = source_flags.get("_outreach_note", "")

        logger.info(f"  Enrichment complete. {len(enriched_people)} leads ready with full profiles.")

        logger.info(sep)
        logger.info(" Pipeline complete!")
        logger.info(f"  Companies found:           {len(companies)}")
        logger.info(f"  Companies qualified:        {len(qualified)}")
        logger.info(f"  People found:               {len(raw_people)}")
        logger.info(f"    ↳ Apollo-reachable:       {len(apollo_reachable)}")
        logger.info(f"    ↳ LinkedIn-only:          {len(linkedin_only)}")
        logger.info(f"  Leads fully enriched:       {len(enriched_people)}")
        logger.info(sep)

        return {
            "companies": companies,
            "enriched_companies": enriched_companies,
            "qualified_companies": qualified,
            "raw_people": raw_people,           # ALL people, reachability-flagged
            "apollo_reachable_people": apollo_reachable,
            "linkedin_only_people": linkedin_only,
            "enriched_people": enriched_people,
        }


# ─── Convenience factories ────────────────────────────────────────────────────


def create_mock_client(**kwargs) -> ApolloClient:
    """Create an Apollo client in MOCK mode (no API key required)."""
    return ApolloClient(mock_mode=True, **kwargs)


def create_live_client(api_key: str, **kwargs) -> ApolloClient:
    """Create an Apollo client in LIVE mode (requires a valid Apollo master API key)."""
    return ApolloClient(api_key=api_key, mock_mode=False, **kwargs)
