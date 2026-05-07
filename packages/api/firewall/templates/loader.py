"""Template loader + compiler — Slice 2 Tier 1.05.

Walks the templates directory at module load and parses every
``*.yaml`` into a registry keyed by template id. The HTTP layer
(routers/firewall.py) reads the registry to render the dashboard's
"+ New rule" gallery; the same module's ``compile_rule`` turns
operator field choices into a real ``Policy`` ready for
``policy_store.create_policy()``.

Why a registry, not on-disk-each-call?

  - Templates are static. There's no reason to re-parse on every
    HTTP request. The registry caches at module load.
  - Tests can call ``reload_templates(directory=...)`` to swap
    the registry for a fixture directory without polluting the
    production module.
  - Operators can drop a custom YAML into the directory and
    restart the API — single source of truth, no DB write needed.

Failure modes:

  - Malformed YAML: log a warning, skip the template, keep the
    rest of the registry usable. Better one bad template than
    zero good ones.
  - Compile-time field-value mismatch (operator picked an option
    not in ``choices``): raise ``ValueError`` with a clean error
    string. The HTTP layer maps this to a 400.
  - Template references a field id that doesn't exist in
    ``fields``: caught at compile time as ``KeyError``, rewrapped
    as ``ValueError`` so callers see a helpful message rather
    than a raw exception.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from lumin.policy import Policy

logger = logging.getLogger("lumin.api.firewall.templates")


_TEMPLATE_DIR = Path(__file__).parent
_REGISTRY: Dict[str, dict] = {}
_LOADED = False


# ---- public API ------------------------------------------------------------


def load_templates(directory: Optional[Path] = None) -> Dict[str, dict]:
    """Return the cached registry, loading it on first call. Pass
    ``directory`` to swap the source — useful for tests."""
    global _LOADED, _REGISTRY
    if directory is not None:
        # Explicit reload against a different directory (test path).
        _REGISTRY = _read_directory(directory)
        _LOADED = True
        return _REGISTRY
    if not _LOADED:
        _REGISTRY = _read_directory(_TEMPLATE_DIR)
        _LOADED = True
    return _REGISTRY


def get_template(template_id: str) -> Optional[dict]:
    return load_templates().get(template_id)


def reload_templates(directory: Optional[Path] = None) -> Dict[str, dict]:
    """Test helper — force a re-read."""
    global _LOADED
    _LOADED = False
    return load_templates(directory)


def list_templates_summary() -> List[dict]:
    """Compact summary for the gallery — drops the heavy ``condition``
    + ``description`` fields so the list endpoint payload stays small."""
    out = []
    for tpl in load_templates().values():
        out.append({
            "id": tpl["id"],
            "name": tpl["name"],
            "icon": tpl.get("icon"),
            "summary": tpl.get("summary"),
            "category": tpl.get("category"),
            "field_count": len(tpl.get("fields", [])),
        })
    return out


def compile_rule(
    template_id: str,
    name: str,
    field_values: Dict[str, Any],
    *,
    mode: Optional[str] = None,
) -> Policy:
    """Render a Policy from a template + operator's field choices.

    Always returns a ``mode=shadow`` policy unless ``mode`` is
    explicitly overridden — matches §10.1 (new policies are safe-by-
    default; operator must promote via ModeToggle).

    Raises ``KeyError`` if the template id is unknown.
    Raises ``ValueError`` for any field-value validation problem.
    """
    tpl = get_template(template_id)
    if tpl is None:
        raise KeyError(f"unknown template: {template_id!r}")

    # Validate + render each field value
    rendered: Dict[str, Any] = {}
    for field in tpl.get("fields", []):
        fid = field["id"]
        ftype = field.get("type", "text")
        if fid in field_values and field_values[fid] is not None:
            value = field_values[fid]
        else:
            value = field.get("default")
        rendered[fid] = _render_field_value(field, ftype, value)

    # Compile condition + description via str.format
    try:
        condition = tpl["condition"].format(**rendered).strip()
    except KeyError as e:
        raise ValueError(
            f"template {template_id!r} condition references unknown field: {e}"
        )
    try:
        description = tpl.get("description", "").format(**rendered).strip()
    except KeyError as e:
        raise ValueError(
            f"template {template_id!r} description references unknown field: {e}"
        )

    # Honor an ``action`` field if the template exposes one (overrides
    # defaults.action). select-typed action field values are rendered
    # as repr() strings — strip the quotes before storing on Policy.
    raw_action = field_values.get("action")
    if raw_action is None:
        raw_action = tpl.get("defaults", {}).get("action", "block")
    action = str(raw_action).strip().strip("'\"")

    defaults = tpl.get("defaults", {}) or {}
    return Policy(
        name=name,
        description=description or tpl.get("summary"),
        # SDK Policy requires trigger ∈ {span_end, trace_end}. Firewall
        # rules route via lifecycle, not trigger; pin to span_end so
        # validation passes.
        trigger="span_end",
        condition=condition,
        action=action,
        severity=defaults.get("severity", "medium"),
        scope_agents=[],
        lifecycle=defaults.get("lifecycle", "post_ingest"),
        mode=mode or "shadow",
        priority=int(defaults.get("priority", 0)),
        on_timeout=defaults.get("on_timeout", "allow"),
        on_internal_error=defaults.get("on_internal_error", "allow"),
    )


# ---- internals -------------------------------------------------------------


def _read_directory(directory: Path) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not directory.exists():
        return out
    for p in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            logger.warning("template %s: invalid YAML — skipping: %s", p.name, e)
            continue
        if not isinstance(data, dict) or "id" not in data:
            logger.warning("template %s: missing id — skipping", p.name)
            continue
        tpl_id = str(data["id"]).strip()
        if not tpl_id:
            continue
        out[tpl_id] = data
    return out


def _render_field_value(field: dict, ftype: str, value: Any) -> str:
    """Convert a field value into the Python literal that the
    condition template will str.format() into. Multi-select renders
    as a list literal so ``tool_name in {tools}`` slots in cleanly.
    select with a custom ``value`` mapping resolves to that mapped
    value; otherwise to the chosen id. text/number render as
    repr-quoted/raw."""
    if ftype == "multi-select":
        if not isinstance(value, list):
            raise ValueError(
                f"field {field['id']!r}: multi-select expects a list, got {type(value).__name__}"
            )
        choices = field.get("choices") or []
        valid_ids = {c["id"] for c in choices}
        if valid_ids:
            for v in value:
                if v not in valid_ids:
                    raise ValueError(
                        f"field {field['id']!r}: choice {v!r} not in "
                        f"{sorted(valid_ids)}"
                    )
        return repr(list(value))

    if ftype == "select":
        choices = field.get("choices") or []
        match = next(
            (c for c in choices if c.get("id") == value),
            None,
        )
        if match is None and choices:
            valid_ids = sorted(c["id"] for c in choices)
            raise ValueError(
                f"field {field['id']!r}: choice {value!r} not in {valid_ids}"
            )
        # If the choice declares a ``value:`` (regex / longer string),
        # use that for substitution. Otherwise the id is the value.
        if match and "value" in match:
            return str(match["value"])
        return str(value)

    if ftype == "number":
        return str(value)

    # text / fallback
    return str(value)
