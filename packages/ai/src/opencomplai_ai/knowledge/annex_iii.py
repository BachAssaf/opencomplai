"""Annex III high-risk AI use case areas.

Re-exports opencomplai_core.knowledge.annex_iii. The pack data is bundled
into core (D6) so opencomplai_core.rules never runs on an empty ruleset when
this optional plugin is absent. Edit the regulation data only in
opencomplai_core.knowledge.annex_iii — this module exists so existing
opencomplai-ai importers keep working unchanged.
"""

from __future__ import annotations

from opencomplai_core.knowledge.annex_iii import (
    ANNEX_III,
    AnnexIIIEntry,
    all_code_signals,
    all_keywords,
    lookup_by_area,
    lookup_by_code_signal,
    lookup_by_keyword,
)

__all__ = [
    "ANNEX_III",
    "AnnexIIIEntry",
    "all_code_signals",
    "all_keywords",
    "lookup_by_area",
    "lookup_by_code_signal",
    "lookup_by_keyword",
]
