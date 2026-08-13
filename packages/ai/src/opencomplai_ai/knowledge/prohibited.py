"""Art. 5 prohibited AI practices.

Re-exports opencomplai_core.knowledge.prohibited. The pack data is bundled
into core (D6) so opencomplai_core.rules never runs on an empty ruleset when
this optional plugin is absent. Edit the regulation data only in
opencomplai_core.knowledge.prohibited — this module exists so existing
opencomplai-ai importers keep working unchanged.
"""

from __future__ import annotations

from opencomplai_core.knowledge.prohibited import (
    PROHIBITED,
    ProhibitedEntry,
    match_prohibited,
)

__all__ = [
    "PROHIBITED",
    "ProhibitedEntry",
    "match_prohibited",
]
