"""Rule templates — Slice 2 Tier 1.05.

Each template is a YAML file in this directory. The loader walks the
directory at module-load time, parses templates into a registry, and
the HTTP layer exposes them at:

  GET  /v1/firewall/templates             — list all
  GET  /v1/firewall/templates/{id}        — full template detail
  POST /v1/firewall/templates/{id}/instantiate
       — create a policy from the template + operator's field values

Schema per YAML file:

  id, name, icon, summary, category   — metadata
  fields: [{id, label, hint?, type, default, choices?, ...}]
  defaults: { lifecycle, mode, severity, priority, on_timeout? }
  condition: str.format-style template; ``{field_id}`` interpolates
             field values. Multi-select renders as a Python list
             literal so conditions can use ``in {tools}``.
  description: human description (also str.format-substituted)

Operators never write the condition — the dashboard renders fields
as a form, sends values to ``/instantiate``, server compiles via
``str.format()`` and creates the policy in ``mode=shadow`` (§10.1).
"""
