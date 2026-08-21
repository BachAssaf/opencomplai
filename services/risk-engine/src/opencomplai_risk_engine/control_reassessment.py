"""
Control freshness reassessment (CTRL-FRESH).

Wraps the pure `opencomplai_core.control_freshness` detectors with the I/O
needed to run them against a system's persisted control registry: fetch the
current controls and stored manifest fingerprint from evidence-vault
(CTRL-STORE), detect TTL-expiry and manifest-change staleness, enqueue
exactly one `ReviewItem` per newly-stale control via
`opencomplai_risk_engine.review_queue.enqueue_review` (idempotent by
`dedup_key`), patch the affected controls to `evidence_stale`, and store the
latest fingerprint. No cron, scheduler, or background worker — this only
runs when `POST /v1/controls/reassess` is called (from the CLI's
`gaps`/`check --with-gaps` flow, today).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request as urlreq

from opencomplai_core.control_catalog import get_catalog
from opencomplai_core.control_freshness import (
    FreshnessConfig,
    StaleControlRow,
    StaleReason,
    detect_manifest_change,
    detect_stale,
    effective_ttl_days,
)
from opencomplai_core.models import ControlInstance, ControlState, ReviewReason
from opencomplai_core.service_auth import load_shared_secret, mint_service_token

from opencomplai_risk_engine.review_queue import build_redacted_context, enqueue_review

TENANT_ID = os.environ.get("TENANT_ID", "default")
EVIDENCE_VAULT_URL = os.environ.get("EVIDENCE_VAULT_URL", "http://evidence-vault:8002")


def _vault_headers() -> dict[str, str]:
    """Signed service-token header for evidence-vault calls, plus X-Tenant-Id.

    Mirrors `main._evidence_vault_headers` / `review_queue._evidence_vault_headers`:
    risk-engine is single-tenant-per-deployment (TENANT_ID env var), not
    per-request tenancy.
    """
    headers = {"Content-Type": "application/json", "X-Tenant-Id": TENANT_ID}
    secret = load_shared_secret()
    if secret is not None:
        token = mint_service_token("risk-engine", secret)
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _vault_request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urlreq.Request(
        f"{EVIDENCE_VAULT_URL}{path}",
        data=data,
        headers=_vault_headers(),
        method=method,
    )
    with urlreq.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _fetch_stored_fingerprint(system_id: str) -> str | None:
    """GET /v1/fingerprints/{system_id}; None on 404 (no fingerprint stored yet).

    Catches HTTPError the way `review_queue.get_review_item` handles 404.
    """
    try:
        result = _vault_request("GET", f"/v1/fingerprints/{system_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    return result["fingerprint"]


def _row_dict(row: StaleControlRow) -> dict:
    return {
        "control_id": row.control_id,
        "system_id": row.system_id,
        "article_ref": row.article_ref,
        "owner": row.owner,
        "state": row.state.value,
        "stale_reason": row.stale_reason.value,
        "expired_at": row.expired_at,
        "dedup_key": row.dedup_key,
        "message": row.message,
    }


def reassess_controls(
    system_id: str,
    commit_ref: str,
    current_fingerprint: str,
    *,
    tenant_id: str = TENANT_ID,
    now: str | None = None,
) -> dict:
    """Detect and act on stale controls for `system_id` (D4/D5/D6).

    1. Load the system's controls and the last stored manifest fingerprint
       from evidence-vault.
    2. Detect TTL-expired controls (`detect_stale`) and, if the fingerprint
       changed, manifest-changed controls (`detect_manifest_change`).
       Deduplicated by `control_id` — the TTL row wins when a control is
       flagged by both detectors.
    3. Enqueue exactly one `ReviewItem` per stale row via `enqueue_review`,
       keyed by the row's deterministic `dedup_key` as both `payload_ref`
       and `idempotency_key` — re-running this against the same evidence
       state returns the existing item rather than creating a duplicate.
    4. Patch newly-stale controls (state != evidence_stale already) to
       `evidence_stale` in the vault.
    5. Always store the latest fingerprint, so the stored value tracks the
       latest run whether or not anything went stale.
    """
    controls = [
        ControlInstance.model_validate(item)
        for item in _vault_request("GET", f"/v1/controls/{system_id}")["items"]
    ]
    stored_fingerprint = _fetch_stored_fingerprint(system_id)
    manifest_changed = (
        stored_fingerprint is not None and stored_fingerprint != current_fingerprint
    )

    catalog = get_catalog()
    config = FreshnessConfig()

    ttl_rows = detect_stale(controls, catalog, now, config=config)
    manifest_rows = detect_manifest_change(
        controls, stored_fingerprint, current_fingerprint, now=now
    )

    # Dedup by control_id — TTL row wins when both detectors flag the same control.
    rows_by_control_id: dict[str, StaleControlRow] = {}
    for row in manifest_rows:
        rows_by_control_id[row.control_id] = row
    for row in ttl_rows:
        rows_by_control_id[row.control_id] = row
    rows = list(rows_by_control_id.values())

    controls_by_id = {c.control_id: c for c in controls}
    review_ids: list[str] = []
    controls_to_patch: list[dict] = []

    for row in rows:
        control = controls_by_id[row.control_id]
        reason = (
            ReviewReason.EVIDENCE_STALE
            if row.stale_reason == StaleReason.TTL_EXPIRED
            else ReviewReason.MANIFEST_CHANGE
        )
        row_ttl_days = effective_ttl_days(control, catalog, config)
        context = build_redacted_context(
            review_id="pending",
            reason=reason,
            detector_ids=[f"control:{row.control_id}"],
            aggregate_counts={"stale_controls": 1},
            evidence_hashes=list(control.evidence_refs),
        )
        item = enqueue_review(
            tenant_id=tenant_id,
            system_id=system_id,
            commit_ref=commit_ref,
            reason=reason,
            payload_ref=row.dedup_key,
            context=context,
            idempotency_key=row.dedup_key,
            expires_in_hours=(row_ttl_days * 24 if row_ttl_days else 72),
        )
        review_ids.append(item.review_id)

        if control.state != ControlState.EVIDENCE_STALE:
            controls_to_patch.append(
                {
                    "control_id": control.control_id,
                    "state": ControlState.EVIDENCE_STALE.value,
                }
            )

    if controls_to_patch:
        _vault_request("PUT", "/v1/controls", {"items": controls_to_patch})

    # Always store the latest fingerprint so the stored value tracks this run.
    _vault_request(
        "PUT", f"/v1/fingerprints/{system_id}", {"fingerprint": current_fingerprint}
    )

    return {
        "system_id": system_id,
        "stored_fingerprint": stored_fingerprint,
        "current_fingerprint": current_fingerprint,
        "manifest_changed": manifest_changed,
        "stale_controls": [_row_dict(row) for row in rows],
        "review_items_enqueued": review_ids,
        "controls_updated": len(controls_to_patch),
    }
