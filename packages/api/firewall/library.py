"""Starter pack library — §13 of AGENT_FIREWALL_SPEC.md.

Walks ``packages/api/firewall/starter_packs/`` at module load and
exposes:

  - ``list_packs()`` — metadata for every pack available
  - ``preview_pack(pack_id)`` — load + return the policies in a pack
    without writing to the DB (powers the dashboard's library
    "preview" surface)
  - ``import_pack(db, pack_id)`` — write the pack's policies into the
    DB. Idempotent — a duplicate name from a prior import is skipped,
    not overwritten, so re-importing after operator edits doesn't
    clobber their tuning.

Pack metadata is derived from the filename + a small in-module
catalog (display name + category + description) so the YAML files
themselves stay focused on policies. Adding a new pack to the
catalog requires:

  1. Drop ``foo.yaml`` into ``starter_packs/``.
  2. Add an entry to ``_CATALOG`` below.
  3. Done — ``GET /v1/firewall/library`` surfaces it on the next
     reload, ``POST /v1/firewall/library/foo/import`` works.

Failure modes follow the §13 / Rule 7 contract:

  - Missing or malformed YAML: skipped at list time, surfaced with
    a warning. The list endpoint never raises just because one pack
    is broken.
  - Import-time policy-name conflict: counted as a skip, not an
    error; the rest of the pack still imports.
  - DB unreachable: per-policy try/except so a transient failure
    on one row doesn't abort the import.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from lumin.policy import Policy, PolicyConfigError, load_policy_engine

import policy_store
from db import Database

logger = logging.getLogger("lumin.api.firewall.library")


_PACK_DIR = Path(__file__).parent / "starter_packs"


@dataclass
class PackMeta:
    pack_id: str
    name: str
    category: str
    description: str
    file: Path


# Hard-coded catalog. Source of truth for display metadata; the YAML
# files themselves only declare policies. New packs require a row
# here OR they're listed with a generic auto-derived display name.
_CATALOG: Dict[str, Dict[str, str]] = {
    "owasp_llm_top_10": {
        "name": "OWASP LLM Top 10 (2025)",
        "category": "Threat coverage",
        "description":
            "15 policies mapping to OWASP LLM01–LLM10. Auto-installed "
            "on first boot. The baseline every Lumin deployment ships with.",
    },
    "owasp_agentic_2025": {
        "name": "OWASP Agentic AI Top 10 (2025)",
        "category": "Threat coverage",
        "description":
            "Agent-specific threats: memory poisoning, tool misuse, "
            "privilege compromise, identity spoofing, overreliance.",
    },
    "dev_environment_safety": {
        "name": "Dev environment safety",
        "category": "Use case",
        "description":
            "Conservative defaults for agents with shell + filesystem "
            "access on a developer's machine. rm -rf, dd, credential "
            "reads, force-push, curl|sh.",
    },
    "customer_support_agent": {
        "name": "Customer support agent",
        "category": "Use case",
        "description":
            "PII redaction in replies, refund-amount ceilings, brand "
            "safety, cross-session leakage hints. Tuned for support / "
            "helpdesk bots.",
    },
    "code_assistant": {
        "name": "Code-assistant agent",
        "category": "Use case",
        "description":
            "eval / unsafe deserialization, .git internals writes, "
            "supply-chain registries, hardcoded secrets, TLS disable, "
            "world-writable chmod.",
    },
    "compliance_gdpr": {
        "name": "Compliance — GDPR",
        "category": "Compliance",
        "description":
            "Advisory rules aligned to GDPR Art. 5 + Art. 9. Special "
            "categories, EU PII, profiling lawful-basis flags, "
            "right-to-erasure detection.",
    },
    "compliance_hipaa": {
        "name": "Compliance — HIPAA",
        "category": "Compliance",
        "description":
            "Aligned to HIPAA Privacy Rule § 164.514 Safe Harbor. SSN, "
            "MRN, health-plan IDs, diagnosis-with-name, PHI tool-call "
            "audit.",
    },
    "compliance_pci_dss": {
        "name": "Compliance — PCI DSS v4.0",
        "category": "Compliance",
        "description":
            "Hard-block PAN / CVV / magnetic-stripe / PIN in output "
            "and tool input. Strict-liability posture.",
    },
    "framework_mastra": {
        "name": "Framework — Mastra",
        "category": "Framework",
        "description":
            "Defaults for agents built on Mastra. Workflow-step "
            "approvals, web-fetch SSRF, memory poisoning.",
    },
    "framework_langgraph": {
        "name": "Framework — LangGraph",
        "category": "Framework",
        "description":
            "Defaults for LangGraph multi-node flows. Recursion warns, "
            "checkpointer writes, dynamic subgraph approvals.",
    },
    "cost_guards": {
        "name": "Cost guards",
        "category": "Operational",
        "description":
            "Per-session and per-agent token caps; tool-call burst "
            "rate limits; oversize-input flags. Protects against "
            "runaway loops and cost spikes.",
    },
}


# ---- public API ------------------------------------------------------------


def list_packs(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Return metadata for every pack available. Each entry includes
    pack_id, display name, category, description, and policy count.

    Packs that fail to load (malformed YAML, etc.) are filtered out
    with a warning — the caller never sees them.
    """
    pack_dir = directory or _PACK_DIR
    if not pack_dir.exists():
        return []

    out: List[Dict[str, Any]] = []
    for path in sorted(pack_dir.glob("*.yaml")):
        pack_id = path.stem
        try:
            engine = load_policy_engine(str(path))
        except PolicyConfigError as e:
            logger.warning("library: pack %s rejected: %s", pack_id, e)
            continue
        except Exception:
            logger.exception("library: pack %s failed to load", pack_id)
            continue
        if engine is None:
            continue

        meta = _CATALOG.get(pack_id)
        out.append({
            "pack_id": pack_id,
            "name": (meta or {}).get("name", _humanize(pack_id)),
            "category": (meta or {}).get("category", "Custom"),
            "description": (meta or {}).get("description", ""),
            "policy_count": len(engine.policies),
            "lifecycles": sorted({p.lifecycle for p in engine.policies}),
            "auto_installed": pack_id == "owasp_llm_top_10",
        })
    return out


def preview_pack(pack_id: str, directory: Optional[Path] = None) -> Dict[str, Any]:
    """Load a pack and return its full policy list (un-imported).

    Returned shape mirrors what /v1/policies emits per row, so the
    dashboard preview surface and the live policies surface can use
    one renderer. ``Policy`` objects are dumped to dicts with the
    same keys policy_store.create_policy expects.
    """
    pack_path = _resolve_pack_path(pack_id, directory)
    engine = load_policy_engine(str(pack_path))
    policies = engine.policies if engine else []

    meta = _CATALOG.get(pack_id, {})
    return {
        "pack_id": pack_id,
        "name": meta.get("name", _humanize(pack_id)),
        "category": meta.get("category", "Custom"),
        "description": meta.get("description", ""),
        "policy_count": len(policies),
        "policies": [_policy_to_preview_row(p) for p in policies],
    }


@dataclass
class ImportResult:
    pack_id: str
    imported: int
    skipped_duplicates: int
    failed: int
    skipped_names: List[str]


def import_pack(
    db: Database,
    pack_id: str,
    *,
    directory: Optional[Path] = None,
    actor: str = "library_import",
) -> ImportResult:
    """Import every policy in ``pack_id`` into the DB. Idempotent —
    duplicate names are SKIPPED, never overwritten.

    Per §10.1 every imported policy lands in mode=shadow. The pack
    YAMLs already declare ``mode: shadow`` so this is enforced in
    two places: pack source + DB-insert default. The double-check
    catches a community-contributed pack that forgot the field.
    """
    pack_path = _resolve_pack_path(pack_id, directory)
    engine = load_policy_engine(str(pack_path))
    policies = engine.policies if engine else []

    imported = 0
    skipped: List[str] = []
    failed = 0

    for policy in policies:
        # Defensive shadow override — every starter-pack import lands
        # in shadow regardless of what the YAML declared (Rule §10.1).
        if getattr(policy, "mode", None) != "shadow":
            try:
                policy.mode = "shadow"  # type: ignore[attr-defined]
            except Exception:
                pass
        try:
            policy_store.create_policy(db, policy, actor=actor)
            imported += 1
        except ValueError:
            # Duplicate name — operator already has this rule, leave
            # their version alone.
            skipped.append(policy.name)
        except Exception:
            logger.exception(
                "library: failed to import policy %r from pack %s",
                policy.name,
                pack_id,
            )
            failed += 1

    if imported or skipped:
        logger.info(
            "library: pack %s — imported=%d, skipped_duplicates=%d, failed=%d",
            pack_id,
            imported,
            len(skipped),
            failed,
        )
    return ImportResult(
        pack_id=pack_id,
        imported=imported,
        skipped_duplicates=len(skipped),
        failed=failed,
        skipped_names=skipped,
    )


# ---- internals -------------------------------------------------------------


def _resolve_pack_path(pack_id: str, directory: Optional[Path]) -> Path:
    pack_dir = directory or _PACK_DIR
    candidate = pack_dir / f"{pack_id}.yaml"
    if not candidate.exists():
        raise FileNotFoundError(f"pack not found: {pack_id}")
    # Defensive: forbid path traversal in pack_id
    if ".." in pack_id or "/" in pack_id or "\\" in pack_id:
        raise ValueError(f"invalid pack_id: {pack_id}")
    return candidate


def _humanize(pack_id: str) -> str:
    return pack_id.replace("_", " ").title()


def _policy_to_preview_row(policy: Policy) -> Dict[str, Any]:
    """Dict shape for the dashboard's pack preview. Matches the
    fields /v1/policies returns so the existing PolicyRow renderer
    works for previews unchanged."""
    return {
        "name": policy.name,
        "description": getattr(policy, "description", ""),
        "lifecycle": policy.lifecycle,
        "mode": getattr(policy, "mode", "shadow"),
        "priority": getattr(policy, "priority", 50),
        "trigger": getattr(policy, "trigger", "span_end"),
        "condition": getattr(policy, "condition", ""),
        "action": getattr(policy, "action", "flag"),
        "severity": getattr(policy, "severity", "medium"),
    }
