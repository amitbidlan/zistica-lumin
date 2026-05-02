import asyncio
from typing import List, Optional

from .span import Span


class BoundedQueue:
    """Asyncio-backed bounded FIFO queue. Drops new items on overflow."""

    def __init__(self, max_size: int = 10_000):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._dropped = 0

    def put_nowait(self, span: Span) -> bool:
        try:
            self._queue.put_nowait(span)
            return True
        except asyncio.QueueFull:
            self._dropped += 1
            return False

    async def drain(self, max_items: Optional[int] = None) -> List[Span]:
        items: List[Span] = []
        limit = max_items if max_items is not None else self._queue.qsize()
        for _ in range(limit):
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    def __len__(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped
