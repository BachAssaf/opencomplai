"""EU AI Act machine-readable knowledge pack — bundled into core.

Single source of truth for Annex III high-risk areas, Art. 5 prohibited
practices, and Art. 50 limited-risk transparency triggers. Bundled here
(rather than left in the optional opencomplai-ai plugin) so the classifier
in opencomplai_core.rules can never run on an empty ruleset on a standard
install. opencomplai_ai.knowledge re-exports these same symbols for
backward compatibility, so there is exactly one copy of the pack data.
"""

from opencomplai_core.knowledge.annex_iii import ANNEX_III, AnnexIIIEntry
from opencomplai_core.knowledge.limited_risk import LIMITED_RISK, LimitedRiskEntry
from opencomplai_core.knowledge.prohibited import PROHIBITED, ProhibitedEntry

__all__ = [
    "ANNEX_III",
    "LIMITED_RISK",
    "PROHIBITED",
    "AnnexIIIEntry",
    "LimitedRiskEntry",
    "ProhibitedEntry",
]
