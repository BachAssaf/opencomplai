"""EU AI Act machine-readable knowledge pack — bundled into core.

Single source of truth for Annex III high-risk areas, Art. 5 prohibited
practices, Art. 50 limited-risk transparency triggers, and the natural-
person subject-gating cue sets. Bundled here (rather than left in the
optional opencomplai-ai plugin) so the classifier in opencomplai_core.rules
can never run on an empty ruleset — or a different verdict — depending on
whether that optional plugin is installed. opencomplai_ai.knowledge and
opencomplai_ai.models re-export these same symbols for backward
compatibility, so there is exactly one copy of the pack data.
"""

from opencomplai_core.knowledge.annex_iii import ANNEX_III, AnnexIIIEntry
from opencomplai_core.knowledge.limited_risk import LIMITED_RISK, LimitedRiskEntry
from opencomplai_core.knowledge.prohibited import PROHIBITED, ProhibitedEntry
from opencomplai_core.knowledge.subject_cues import (
    NATURAL_PERSON_CUES,
    PRODUCT_OR_ENTITY_CUES,
)

__all__ = [
    "ANNEX_III",
    "LIMITED_RISK",
    "NATURAL_PERSON_CUES",
    "PRODUCT_OR_ENTITY_CUES",
    "PROHIBITED",
    "AnnexIIIEntry",
    "LimitedRiskEntry",
    "ProhibitedEntry",
]
