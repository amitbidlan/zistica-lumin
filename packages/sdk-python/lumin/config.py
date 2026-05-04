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
