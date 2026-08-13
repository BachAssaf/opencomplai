"""
Egress policy for the AI plugin: offline mode and data-egress consent
(AI-EGRESS, findings 78 and 79).

Two independent gates sit in front of anything that leaves the machine:

1. **Offline mode** (``OPENCOMPLAI_OFFLINE``) — a hard, fail-closed switch for
   regulated deployments. When set, no snippet is sent and no model is
   downloaded, regardless of configuration or consent. It cannot be overridden
   by config, only by unsetting the variable.

2. **Consent** — the ``saas`` backend sends source snippets to a third party.
   That is a materially different act from running a model locally, so it
   requires a recorded, explicit, one-time opt-in rather than being implied by
   selecting a model from a list.

Consent is recorded in ``~/.opencomplai/ai-config.yaml`` alongside the model
choice, with a timestamp and a policy version. Bumping
``EGRESS_CONSENT_VERSION`` invalidates prior consent and forces a re-prompt —
what the user agreed to is only meaningful for the terms they were shown.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import yaml

from opencomplai_ai.config import _AI_CONFIG_FILE, _CONFIG_DIR

#: Bump when what is sent, or where it is sent, materially changes.
EGRESS_CONSENT_VERSION = 1

_OFFLINE_ENV_VAR = "OPENCOMPLAI_OFFLINE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class OfflineModeError(RuntimeError):
    """Raised when an operation requiring network access runs under offline mode."""


def is_offline(env: dict[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get(_OFFLINE_ENV_VAR, "").strip().lower() in _TRUTHY


def require_online(operation: str, env: dict[str, str] | None = None) -> None:
    """
    Fail closed when offline mode is active.

    Raises rather than degrading silently: an operator who set
    ``OPENCOMPLAI_OFFLINE=1`` needs to know an operation was refused, not to
    discover later that it quietly produced a lower-quality result.
    """
    if is_offline(env):
        raise OfflineModeError(
            f"{operation} requires network access, but {_OFFLINE_ENV_VAR} is set. "
            f"Unset it to allow this, or choose a local model with "
            f"'opencomplai ai configure'."
        )


@dataclass(frozen=True)
class ConsentRecord:
    granted_at: str
    version: int

    @property
    def current(self) -> bool:
        return self.version == EGRESS_CONSENT_VERSION


def _read_config() -> dict:
    if not _AI_CONFIG_FILE.exists():
        return {}
    try:
        return yaml.safe_load(_AI_CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        # A malformed config must not be read as "consent granted".
        return {}


def get_consent() -> ConsentRecord | None:
    raw = _read_config().get("saas_egress_consent")
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    granted_at = raw.get("granted_at")
    if not isinstance(version, int) or not isinstance(granted_at, str):
        return None
    return ConsentRecord(granted_at=granted_at, version=version)


def has_consent() -> bool:
    record = get_consent()
    return record is not None and record.current


def record_consent() -> ConsentRecord:
    """Persist a consent grant at the current policy version."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = _read_config()
    record = ConsentRecord(
        granted_at=datetime.now(UTC).isoformat(), version=EGRESS_CONSENT_VERSION
    )
    existing["saas_egress_consent"] = {
        "granted_at": record.granted_at,
        "version": record.version,
    }
    _AI_CONFIG_FILE.write_text(yaml.safe_dump(existing), encoding="utf-8")
    return record


def revoke_consent() -> None:
    existing = _read_config()
    if "saas_egress_consent" not in existing:
        return
    del existing["saas_egress_consent"]
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _AI_CONFIG_FILE.write_text(yaml.safe_dump(existing), encoding="utf-8")


CONSENT_NOTICE = """\
The 'saas' backend sends source code from this repository to
https://api.opencomplai.com for classification.

  What is sent:  code snippets around detected AI usage, plus the declared
                 purpose and file location.
  Scrubbed first: secret- and PII-shaped content is redacted before sending.
                 Pattern-based redaction is a mitigation, not a guarantee —
                 it cannot catch a credential that looks like ordinary text.
  What is not:   your repository as a whole is never uploaded.

Every other model in the catalog runs entirely on this machine and sends
nothing. Set OPENCOMPLAI_OFFLINE=1 to block all network access outright.\
"""


def stdin_is_interactive() -> bool:
    """
    Whether a prompt can actually be answered.

    Guards every ``input()`` in the plugin. Without this a CI run either
    crashes on ``EOFError`` or — worse, and the behaviour actually observed in
    this repo — blocks forever on a non-interactive stdin (finding 78).
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


__all__ = [
    "CONSENT_NOTICE",
    "EGRESS_CONSENT_VERSION",
    "ConsentRecord",
    "OfflineModeError",
    "get_consent",
    "has_consent",
    "is_offline",
    "record_consent",
    "require_online",
    "revoke_consent",
    "stdin_is_interactive",
]
