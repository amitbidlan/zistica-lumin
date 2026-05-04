from typing import List

import pytest

import lumin.sdk as sdk_module
from lumin.config import Config
from lumin.sdk import LuminSDK
from lumin.span import Span


class CapturingExporter:
    """Async test exporter that records spans in memory instead of sending HTTP."""

    def __init__(self):
        self.spans: List[Span] = []

    async def export(self, spans):
        self.spans.extend(spans)

    async def close(self):
        pass


@pytest.fixture
def sdk():
    """Provide an SDK with a capturing exporter and no background flushing.

    Background flusher is effectively disabled by setting flush_interval very
    high — tests call ``sdk.flush()`` explicitly to drain the queue.
    """
    exporter = CapturingExporter()
    config = Config(host="http://test", flush_interval=3600.0, export_timeout=0.5)
    s = LuminSDK(config=config, exporter=exporter)

    previous = sdk_module._global_sdk
    sdk_module._global_sdk = s
    try:
        yield s
    finally:
        sdk_module._global_sdk = previous
        s.shutdown()


@pytest.fixture
def captured(sdk) -> List[Span]:
    return sdk._exporter.spans
