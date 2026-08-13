"""Art. 50 limited-risk transparency obligations.

Re-exports opencomplai_core.knowledge.limited_risk. The pack data is bundled
into core (D6) alongside the Annex III and Art. 5 packs so there is one
source of truth. Edit the regulation data only in
opencomplai_core.knowledge.limited_risk — this module exists so existing
opencomplai-ai importers keep working unchanged.
"""

from __future__ import annotations

from opencomplai_core.knowledge.limited_risk import (
    LIMITED_RISK,
    LimitedRiskEntry,
    match_limited_risk,
)

__all__ = [
    "LIMITED_RISK",
    "LimitedRiskEntry",
    "match_limited_risk",
]
