"""EU AI Act machine-readable knowledge pack.

The pack data is bundled into opencomplai_core.knowledge (D6 — see
packages/core/src/opencomplai_core/knowledge/) so opencomplai_core.rules can
classify without this optional plugin installed. This package re-exports the
same symbols so existing opencomplai-ai importers are unaffected and there is
exactly one copy of the regulation data to keep in sync.
"""

from opencomplai_ai.knowledge.annex_iii import ANNEX_III, AnnexIIIEntry
from opencomplai_ai.knowledge.limited_risk import LIMITED_RISK, LimitedRiskEntry
from opencomplai_ai.knowledge.prohibited import PROHIBITED, ProhibitedEntry

__all__ = [
    "ANNEX_III",
    "LIMITED_RISK",
    "PROHIBITED",
    "AnnexIIIEntry",
    "LimitedRiskEntry",
    "ProhibitedEntry",
]
