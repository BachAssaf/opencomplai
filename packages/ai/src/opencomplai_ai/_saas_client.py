"""Cloud API backend — routes to https://api.opencomplai.com/v1/intent."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from opencomplai_ai.egress import has_consent, is_offline
from opencomplai_ai.models import (
    IntentAnnotation,
    derive_eu_obligations,
    derive_risk_tier,
)
from opencomplai_ai.redaction import redact

_API_URL = "https://api.opencomplai.com/v1/intent"


def _unavailable(reason: str, legacy: bool) -> IntentAnnotation | None:
    """Uniform 'this backend produced nothing' result, with the reason kept."""
    if legacy:
        return IntentAnnotation(
            model_id="saas", risk_tier="minimal", explanation=reason
        )
    return None


class SaaSIntentClient:
    def __init__(self) -> None:
        self._api_key = os.environ.get("OPENCOMPLAI_API_KEY", "")

    def classify(
        self,
        snippet: str,
        declared_purpose: str = "",
        location: str = "",
        *,
        token: str = "",
        ai_usage_type: str | None = None,
        legacy: bool = False,
    ) -> IntentAnnotation | None:
        # Checked before the API key: offline mode is a hard operator policy
        # and must not depend on whether credentials happen to be configured.
        if is_offline():
            return _unavailable(
                "OPENCOMPLAI_OFFLINE is set — no code was sent. "
                "Choose a local model with 'opencomplai ai configure'.",
                legacy,
            )

        # Sending source code to a third party requires a recorded opt-in
        # (AI-EGRESS). Selecting the model from a list is not consent.
        if not has_consent():
            return _unavailable(
                "Data egress to the cloud intent API has not been consented to — "
                "no code was sent. Run 'opencomplai ai configure --model saas' to "
                "review what is sent and opt in.",
                legacy,
            )

        if not self._api_key:
            return _unavailable(
                "OPENCOMPLAI_API_KEY not set — set it to use cloud intent analysis.",
                legacy,
            )
        try:
            # Scrub before the payload is built, so there is no path where an
            # unredacted snippet reaches the request body.
            scrubbed = redact(snippet)
            payload = json.dumps(
                {
                    "snippet": scrubbed.text,
                    "declared_purpose": redact(declared_purpose).text,
                    "location": location,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                _API_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())

            area = data.get("annex_iii_area")
            if isinstance(area, float):
                area = int(area) if area == int(area) else None
            if not isinstance(area, int) or area not in range(1, 9):
                area = None

            autonomy = data.get("decision_autonomy", "unknown")
            subject = data.get("subject_type", "unknown")
            consequential = data.get("consequential", "unknown")

            # Backstop: don't trust the cloud API's area/tier blindly when it
            # reports a subject_type that contradicts a subject-gated area
            # (same rationale as the local GGUF backend in explainer.py).
            if area is not None and subject in ("legal_entity", "system"):
                from opencomplai_ai.knowledge.annex_iii import lookup_by_area

                entries = lookup_by_area(area)
                if entries and entries[0].subject_gated:
                    area = None

            obligations = derive_eu_obligations(
                autonomy,  # type: ignore[arg-type]
                subject,  # type: ignore[arg-type]
                consequential,  # type: ignore[arg-type]
                annex_iii_area=area,
            )
            art5 = bool(data.get("art5_prohibited", False))
            art6_3 = bool(data.get("art6_3_profiling", False)) and area is not None
            tier = data.get("risk_tier")
            if area is None and tier == "high_risk":
                tier = None
            if tier not in ("prohibited", "high_risk", "limited_risk", "minimal"):
                tier = derive_risk_tier(
                    art5_prohibited=art5,
                    annex_iii_area=area,
                )
            ann = IntentAnnotation(
                annex_iii_area=area,
                art5_prohibited=art5,
                art6_3_profiling=art6_3,
                risk_tier=tier,  # type: ignore[arg-type]
                ai_usage_type=ai_usage_type,
                decision_autonomy=autonomy,  # type: ignore[arg-type]
                subject_type=subject,  # type: ignore[arg-type]
                consequential=consequential,  # type: ignore[arg-type]
                eu_obligation=obligations,
                explanation=data.get("explanation"),
                model_id="saas",
                confidence=data.get("confidence", 0.9),
            )
            if ann.risk_tier == "minimal" and not legacy:
                return None
            return ann
        except Exception:
            return _unavailable(
                "Cloud intent API unavailable — check OPENCOMPLAI_API_KEY and network.",
                legacy,
            )
