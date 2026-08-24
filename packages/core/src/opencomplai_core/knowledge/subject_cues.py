"""Natural-person subject-gating cue sets — machine-readable knowledge pack.

Several Annex III sub-points (credit scoring, insurance pricing, benefit
eligibility, employment, education, recidivism, migration risk, ...) only
regulate systems that score/profile a *natural person*. The regulation
text is explicit about this scope (e.g. Annex III 5(b): "creditworthiness
of natural persons"). A pure keyword/code-signal match on "credit_score"
or "risk_score" cannot tell a consumer-lending decision apart from a bond
desk pricing counterparty risk, a fraud model scoring a transaction, or a
vendor-risk dashboard scoring a supplier — both use identical vocabulary.

These two cue sets let matchers distinguish the two without needing the
LLM backend: presence of a person cue confirms the natural-person
reading; presence of a product/entity cue with no person cue in the same
text is evidence the subject is not a natural person. Ambiguous text
(neither list matches) stays high-risk — a missed compliance flag is
worse than a reviewable one, so gating only downgrades on positive
evidence of a non-person subject, never on absence of evidence.

Bundled into core (not the optional opencomplai-ai plugin) so
opencomplai_core.rules always has non-empty cue sets to gate against,
regardless of whether opencomplai-ai is installed — installing or
uninstalling that optional plugin must never silently change a
pass/fail verdict. opencomplai_ai.models re-exports these same symbols;
edit only here.
"""

from __future__ import annotations

NATURAL_PERSON_CUES: frozenset[str] = frozenset(
    {
        "applicant",
        "borrower",
        "consumer",
        "customer",
        "citizen",
        "individual",
        "person",
        "people",
        "user",
        "employee",
        "candidate",
        "worker",
        "student",
        "patient",
        "resident",
        "claimant",
        "policyholder",
        "tenant",
        "beneficiary",
        "household",
        "voter",
        "defendant",
        "offender",
        "suspect",
        "migrant",
        "asylum_seeker",
        "traveler",
        "victim",
    }
)

PRODUCT_OR_ENTITY_CUES: frozenset[str] = frozenset(
    {
        "portfolio",
        "counterparty",
        "vendor",
        "supplier",
        "merchant",
        "bond",
        "security",
        "securities",
        "instrument",
        "commercial",
        "corporate",
        "b2b",
        "sku",
        "product",
        "inventory",
        "shipment",
        "transaction",
        "invoice",
        "asset",
        "fund",
        "issuer",
        "entity",
        "company",
        "business",
        "wholesale",
        "fleet",
        "device",
        "sensor",
        "machine",
        "equipment",
    }
)
