"""
HITL reviewer queue — enqueue, route, and list review items (Workstream B).

PERSIST-RISK: review items and their redacted contexts now persist to
evidence-vault (the only service in this stack with a real Postgres+RLS
deployment — TEN-VAULT) instead of process-local dicts, so a restart or a
second replica no longer silently drops in-flight review items. This module
keeps ownership of the business logic (round-robin group assignment,
deriving review_id/context_ref) and calls evidence-vault's /v1/hitl/*
endpoints as its durable backing store, mirroring how main.py's
_record_hitl_event already delegates ledger writes the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request as urlreq
from datetime import UTC, datetime, timedelta

from opencomplai_core.models import (
    RedactedReviewContext,
    ReviewItem,
    ReviewItemState,
    ReviewReason,
)
from opencomplai_core.service_auth import load_shared_secret, mint_service_token

TENANT_ID = os.environ.get("TENANT_ID", "default")
REVIEWER_GROUPS: dict[str, str] = json.loads(
    os.environ.get(
        "REVIEWER_GROUP_MAP",
        '{"default": "compliance-reviewers"}',
    )
)
_GROUP_ASSIGN_INDEX: dict[str, int] = {}

EVIDENCE_VAULT_URL = os.environ.get("EVIDENCE_VAULT_URL", "http://evidence-vault:8002")


def _evidence_vault_headers() -> dict[str, str]:
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
        headers=_evidence_vault_headers(),
        method=method,
    )
    with urlreq.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def derive_review_id(
    tenant_id: str,
    system_id: str,
    commit_ref: str,
    reason: ReviewReason,
    payload_ref: str,
) -> str:
    raw = "|".join([tenant_id, system_id, commit_ref, reason.value, payload_ref])
    return f"rev_sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def build_redacted_context(
    review_id: str,
    reason: ReviewReason,
    detector_ids: list[str] | None = None,
    aggregate_counts: dict[str, int] | None = None,
    evidence_hashes: list[str] | None = None,
) -> RedactedReviewContext:
    return RedactedReviewContext(
        review_id=review_id,
        reason=reason,
        detector_ids=detector_ids or [],
        masked_excerpts=[],
        aggregate_counts=aggregate_counts or {},
        evidence_hashes=evidence_hashes or [],
    )


def context_ref(context: RedactedReviewContext) -> str:
    canonical = context.model_dump_json()
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _item_to_model(item: dict) -> ReviewItem:
    return ReviewItem.model_validate(item)


def enqueue_review(
    *,
    tenant_id: str,
    system_id: str,
    commit_ref: str,
    reason: ReviewReason,
    payload_ref: str,
    context: RedactedReviewContext,
    idempotency_key: str | None = None,
    expires_in_hours: int = 72,
) -> ReviewItem:
    review_id = derive_review_id(tenant_id, system_id, commit_ref, reason, payload_ref)

    existing = get_review_item(tenant_id, review_id)
    if existing is not None:
        return existing

    ctx_ref = context_ref(context)
    _vault_request(
        "POST",
        "/v1/hitl/review-contexts",
        {"context_ref": ctx_ref, "context_json": context.model_dump(mode="json")},
    )

    group = REVIEWER_GROUPS.get(system_id, REVIEWER_GROUPS.get("default", "default"))
    idx = _GROUP_ASSIGN_INDEX.get(group, 0)
    _GROUP_ASSIGN_INDEX[group] = idx + 1
    assigned_to = f"{group}:member-{idx % 3}"

    now = datetime.now(UTC)
    item = ReviewItem(
        review_id=review_id,
        tenant_id=tenant_id,
        system_id=system_id,
        commit_ref=commit_ref,
        reason=reason,
        state=ReviewItemState.ASSIGNED,
        payload_ref=payload_ref,
        context_ref=ctx_ref,
        reviewer_group=group,
        assigned_to=assigned_to,
        idempotency_key=idempotency_key or review_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=expires_in_hours)).isoformat(),
    )
    result = _vault_request(
        "PUT", "/v1/hitl/review-items", item.model_dump(mode="json")
    )
    return _item_to_model(result["item"])


def list_review_items(
    tenant_id: str,
    *,
    state: ReviewItemState | None = None,
    assigned_to: str | None = None,
) -> list[ReviewItem]:
    params = []
    if state is not None:
        params.append(f"state={state.value}")
    if assigned_to is not None:
        params.append(f"assigned_to={assigned_to}")
    query = f"?{'&'.join(params)}" if params else ""
    result = _vault_request("GET", f"/v1/hitl/review-items{query}")
    return [_item_to_model(i) for i in result["items"]]


def get_review_item(tenant_id: str, review_id: str) -> ReviewItem | None:
    req = urlreq.Request(
        f"{EVIDENCE_VAULT_URL}/v1/hitl/review-items/{review_id}",
        headers=_evidence_vault_headers(),
        method="GET",
    )
    try:
        with urlreq.urlopen(req, timeout=5) as resp:
            return _item_to_model(json.loads(resp.read())["item"])
    except urlreq.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def get_review_context(context_ref_value: str) -> RedactedReviewContext | None:
    req = urlreq.Request(
        f"{EVIDENCE_VAULT_URL}/v1/hitl/review-contexts/{context_ref_value}",
        headers=_evidence_vault_headers(),
        method="GET",
    )
    try:
        with urlreq.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            return RedactedReviewContext.model_validate(body["context_json"])
    except urlreq.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def assign_review(tenant_id: str, review_id: str, reviewer_id: str) -> ReviewItem:
    item = get_review_item(tenant_id, review_id)
    if item is None:
        raise KeyError(review_id)
    updated = item.model_copy(
        update={
            "state": ReviewItemState.ASSIGNED,
            "assigned_to": reviewer_id,
        }
    )
    result = _vault_request(
        "PUT", "/v1/hitl/review-items", updated.model_dump(mode="json")
    )
    return _item_to_model(result["item"])


def mark_decided(tenant_id: str, review_id: str, override_id: str) -> ReviewItem:
    item = get_review_item(tenant_id, review_id)
    if item is None:
        raise KeyError(review_id)
    updated = item.model_copy(
        update={
            "state": ReviewItemState.DECIDED,
            "decided_at": datetime.now(UTC).isoformat(),
            "linked_override_id": override_id,
        }
    )
    result = _vault_request(
        "PUT", "/v1/hitl/review-items", updated.model_dump(mode="json")
    )
    return _item_to_model(result["item"])


def enqueue_manifest_discrepancy(
    *,
    tenant_id: str,
    system_id: str,
    commit_ref: str,
    payload_ref: str,
    discrepancies: list[str],
    severity: str,
    locations: list[str],
    detector_ids: list[str] | None = None,
) -> ReviewItem:
    """Enqueue a manifest/code declaration discrepancy for human reconciliation."""
    context = build_redacted_context(
        review_id="pending",
        reason=ReviewReason.MANIFEST_DISCREPANCY,
        detector_ids=detector_ids or [],
        aggregate_counts={
            "discrepancy_count": len(discrepancies),
            "location_count": len(locations),
            "severity_rank": 2 if severity == "major" else 3,
        },
        evidence_hashes=[payload_ref],
    )
    context = context.model_copy(update={"review_id": payload_ref[:32]})
    return enqueue_review(
        tenant_id=tenant_id,
        system_id=system_id,
        commit_ref=commit_ref,
        reason=ReviewReason.MANIFEST_DISCREPANCY,
        payload_ref=payload_ref,
        context=context,
    )
