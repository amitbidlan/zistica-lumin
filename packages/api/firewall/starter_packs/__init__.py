"""Starter packs that ship with Lumin.

Each pack is a YAML file shipped alongside this module. The bootstrap
helper in ``firewall.starter_packs.bootstrap_owasp_pack`` imports the
OWASP LLM Top 10 pack into the DB on first install (when the
``policies`` table is empty and no LUMIN_POLICY_FILE is configured).

All starter rules ship in **mode=shadow** per spec §10.1: operators see
what the rules would have done before they take effect on live traffic.
The dashboard's mode toggle is the promotion path.
"""
