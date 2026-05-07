"""Auto-install the OWASP LLM Top 10 starter pack on first boot.

Triggered from ``main.lifespan`` after the schema migration runs.
Idempotent: skips if any policy row already exists. Operators who want
to opt out can set ``LUMIN_DISABLE_STARTER_PACK=true`` before the
first boot.

The pack ships in mode=shadow (§10.1) — operators promote rules one at
a time after reviewing the dashboard's enforcement timeline.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from lumin.policy import (
    PolicyConfigError,
    load_policy_engine,
)

import policy_store
from db import Database

logger = logging.getLogger("lumin.api.firewall.starter_pack")

_PACK_FILE = Path(__file__).with_name("owasp_llm_top_10.yaml")


def install_owasp_pack_if_fresh(
    db: Database, *, actor: str = "starter_pack_bootstrap"
) -> int:
    """Import the OWASP starter pack into the DB iff:

      - the ``policies`` table has zero rows, AND
      - ``LUMIN_DISABLE_STARTER_PACK`` is not set to a truthy value, AND
      - ``LUMIN_POLICY_FILE`` is unset (operator-supplied YAML wins —
        the user-authored rules are the source of truth).

    Returns the number of rules imported. 0 means we deferred (either
    the table already had rows or the env said skip).
    """
    if _disabled_via_env():
        logger.info("policy: starter pack skipped (LUMIN_DISABLE_STARTER_PACK set)")
        return 0
    if os.environ.get("LUMIN_POLICY_FILE"):
        logger.info(
            "policy: starter pack skipped (LUMIN_POLICY_FILE set — user YAML wins)"
        )
        return 0
    try:
        if policy_store.has_any_policies(db):
            return 0
    except Exception:
        logger.exception("policy: starter pack — has_any_policies check failed")
        return 0

    if not _PACK_FILE.exists():
        logger.warning("policy: starter pack file missing at %s", _PACK_FILE)
        return 0

    try:
        engine = load_policy_engine(str(_PACK_FILE))
    except PolicyConfigError as e:
        logger.warning(
            "policy: starter pack rejected (invalid YAML at %s): %s", _PACK_FILE, e
        )
        return 0
    if engine is None:
        return 0

    imported = 0
    for p in engine.policies:
        try:
            policy_store.create_policy(db, p, actor=actor)
            imported += 1
        except ValueError:
            # Race with concurrent bootstrap or duplicate name —
            # skip and keep importing the rest.
            continue
        except Exception:
            logger.exception(
                "policy: starter pack — failed to import %r", p.name
            )
    if imported:
        logger.info(
            "policy: imported %d OWASP-LLM-Top-10 rules in shadow mode", imported
        )
    return imported


def _disabled_via_env() -> bool:
    raw = os.environ.get("LUMIN_DISABLE_STARTER_PACK", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")
