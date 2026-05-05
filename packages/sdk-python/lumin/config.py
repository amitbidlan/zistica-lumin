import os
from dataclasses import dataclass, field
from typing import Optional


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value else default


@dataclass
class Config:
    host: str = field(default_factory=lambda: _env_str("LUMIN_HOST", "http://localhost:8000"))
    api_key: Optional[str] = field(default_factory=lambda: _env_str("LUMIN_API_KEY"))
    project: str = field(default_factory=lambda: _env_str("LUMIN_PROJECT", "default"))
    capture_inputs: bool = True
    capture_outputs: bool = True
    max_payload_size: int = 10_240
    batch_size: int = 100
    flush_interval: float = 2.0
    max_queue_size: int = 10_000
    export_timeout: float = 5.0
    # Policy Engine — Accountability Layer Part B. When set, the SDK
    # loads policies from this YAML file and evaluates them after every
    # span/trace. Violations get POSTed to the API at /v1/violations.
    # When unset, the engine is disabled — zero overhead.
    policy_file: Optional[str] = field(default_factory=lambda: _env_str("LUMIN_POLICY_FILE"))
    # Optional global webhook URL — fires for every "alert"-action
    # violation that doesn't have its own webhook_url. Per Rule 7,
    # webhook failures never reach agent code.
    alert_webhook: Optional[str] = field(default_factory=lambda: _env_str("LUMIN_ALERT_WEBHOOK"))
