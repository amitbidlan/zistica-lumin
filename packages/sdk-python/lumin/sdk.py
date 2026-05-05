import asyncio
import atexit
import functools
import inspect
import logging
import threading
from typing import Any, Callable, Optional

from .config import Config
from .context import (
    get_current_session,
    get_current_span,
    reset_current_span,
    set_current_span,
)
from .exporter import HTTPExporter
from .policy import PolicyConfigError, load_policy_engine
from .policy_dispatcher import PolicyDispatcher
from .queue import BoundedQueue
from .span import Span, SpanContext

logger = logging.getLogger("lumin.sdk")


class LuminSDK:
    """Coordinates span capture, async queueing, and async export.

    Owns a background thread that runs an asyncio event loop. Sync and async
    user code both submit spans via ``call_soon_threadsafe`` onto that loop's
    queue, so the agent thread is never blocked by I/O.
    """

    def __init__(self, config: Optional[Config] = None, exporter=None):
        self._config = config or Config()
        self._exporter = exporter
        self._queue: Optional[BoundedQueue] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._stop_flushing = False
        self._shutdown_complete = False
        self._ready = threading.Event()
        # Policy Engine (Accountability Layer Part B). Loaded eagerly so
        # any YAML / config errors surface at lumin.configure() time.
        # If loading fails the engine stays disabled — the agent must
        # never fail because Lumin's config is broken (Rule 7).
        self._policy: Optional[PolicyDispatcher] = self._init_policy()
        self._start_background_loop()

    def _init_policy(self) -> Optional[PolicyDispatcher]:
        cfg = self._config
        if not cfg.policy_file:
            return None
        try:
            engine = load_policy_engine(cfg.policy_file)
        except PolicyConfigError as e:
            logger.warning(
                "policy: invalid policy file %s — engine disabled: %s",
                cfg.policy_file, e,
            )
            return None
        except Exception:
            logger.exception(
                "policy: unexpected error loading %s — engine disabled",
                cfg.policy_file,
            )
            return None
        if engine is None:
            return None
        return PolicyDispatcher(
            engine=engine,
            host=cfg.host,
            api_key=cfg.api_key,
            alert_webhook=cfg.alert_webhook,
            timeout=cfg.export_timeout,
        )

    @property
    def config(self) -> Config:
        return self._config

    def _start_background_loop(self) -> None:
        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._queue = BoundedQueue(self._config.max_queue_size)
            if self._exporter is None:
                self._exporter = HTTPExporter(
                    host=self._config.host,
                    api_key=self._config.api_key,
                    timeout=self._config.export_timeout,
                    project=self._config.project,
                )
            self._flush_task = loop.create_task(self._flush_loop())
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._loop_thread = threading.Thread(
            target=run_loop, daemon=True, name="lumin-flusher"
        )
        self._loop_thread.start()
        self._ready.wait()

    async def _flush_loop(self) -> None:
        try:
            while not self._stop_flushing:
                try:
                    await asyncio.sleep(self._config.flush_interval)
                except asyncio.CancelledError:
                    break
                if self._stop_flushing:
                    break
                await self._drain_and_export()
        except Exception:
            pass

    async def _drain_and_export(self) -> None:
        try:
            assert self._queue is not None and self._exporter is not None
            spans = await self._queue.drain(self._config.batch_size)
            if spans:
                await self._exporter.export(spans)
        except Exception:
            pass

    def submit(self, span: Span) -> bool:
        """Schedule a span on the background loop. Non-blocking, thread-safe."""
        if (
            self._loop is None
            or self._queue is None
            or self._shutdown_complete
            or not self._loop.is_running()
        ):
            return False
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, span)
        except RuntimeError:
            return False

        # Policy evaluation (Rule 7: must never fail the agent). The
        # engine is fast (microseconds) so we run it on the agent
        # thread, then ship any violations on the background loop.
        if self._policy is not None:
            try:
                violations = self._policy.on_span_end(span)
            except Exception:
                logger.exception("policy: dispatcher crashed; swallowed")
                violations = []
            if violations:
                try:
                    self._loop.call_soon_threadsafe(
                        lambda v=violations: asyncio.ensure_future(
                            self._policy.ship_async(v)
                        )
                    )
                except RuntimeError:
                    pass

        return True

    def flush(self) -> None:
        """Drain the queue and export synchronously. Blocks until done."""
        if (
            self._loop is None
            or self._shutdown_complete
            or not self._loop.is_running()
        ):
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._drain_and_export(), self._loop
            )
            future.result(timeout=self._config.export_timeout + 1.0)
        except Exception:
            pass

    def shutdown(self) -> None:
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        if self._loop is None or not self._loop.is_running():
            return

        async def _async_shutdown() -> None:
            self._stop_flushing = True
            if self._flush_task is not None and not self._flush_task.done():
                self._flush_task.cancel()
                try:
                    await self._flush_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._drain_and_export()
            if self._exporter is not None:
                await self._exporter.close()
            if self._policy is not None:
                try:
                    await self._policy.close()
                except Exception:
                    pass

        try:
            future = asyncio.run_coroutine_threadsafe(_async_shutdown(), self._loop)
            future.result(timeout=5.0)
        except Exception:
            pass

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

        if self._loop_thread is not None:
            self._loop_thread.join(timeout=2.0)


_global_sdk: Optional[LuminSDK] = None
_global_lock = threading.Lock()


def _get_sdk() -> LuminSDK:
    global _global_sdk
    with _global_lock:
        if _global_sdk is None or _global_sdk._shutdown_complete:
            _global_sdk = LuminSDK()
        return _global_sdk


def _set_sdk(sdk: Optional[LuminSDK]) -> None:
    global _global_sdk
    with _global_lock:
        old = _global_sdk
        _global_sdk = sdk
    if old is not None and old is not sdk:
        old.shutdown()


def configure(**kwargs: Any) -> None:
    """Configure the global SDK. Replaces any previous configuration."""
    filtered = {k: v for k, v in kwargs.items() if v is not None}
    config = Config(**filtered)
    _set_sdk(LuminSDK(config))


def span(name: str, type: str = "custom") -> SpanContext:
    """Open a span as a context manager."""
    return SpanContext(_get_sdk(), name, type)


def _resolve_session_id(
    explicit: Optional[str], parent: Optional[Span]
) -> Optional[str]:
    """Pick the session_id to attach to a new span.

    Priority: explicit @trace(session_id=...) > active lumin.session()
    context > parent span's session_id (so children inherit). None means
    "no session" and the field is omitted from storage.
    """
    if explicit is not None:
        return explicit
    current = get_current_session()
    if current is not None:
        return current.id
    if parent is not None and parent.session_id:
        return parent.session_id
    return None


def trace(
    _func: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    type: str = "custom",
    session_id: Optional[str] = None,
):
    """Decorator that records a span around a function call.

    Works on both sync and async functions (auto-detected).
    Usage:
        @trace
        def f(...): ...

        @trace(name="my_step", type="llm")
        async def g(...): ...

        @trace(session_id="user-123-conv-456")
        def h(...): ...   # always tagged with this session
    """

    def decorator(fn: Callable) -> Callable:
        span_name = name or fn.__name__

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                sdk = _get_sdk()
                cfg = sdk.config
                parent = get_current_span()
                s = Span.create(span_name, type, parent=parent)
                s.session_id = _resolve_session_id(session_id, parent)
                if cfg.capture_inputs:
                    s.set_input(
                        {"args": list(args), "kwargs": kwargs}, cfg.max_payload_size
                    )
                token = set_current_span(s)
                try:
                    result = await fn(*args, **kwargs)
                    if cfg.capture_outputs:
                        s.set_output(result, cfg.max_payload_size)
                    return result
                except Exception as e:
                    s.set_error(e)
                    raise
                finally:
                    s.end()
                    sdk.submit(s)
                    reset_current_span(token)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            sdk = _get_sdk()
            cfg = sdk.config
            parent = get_current_span()
            s = Span.create(span_name, type, parent=parent)
            s.session_id = _resolve_session_id(session_id, parent)
            if cfg.capture_inputs:
                s.set_input(
                    {"args": list(args), "kwargs": kwargs}, cfg.max_payload_size
                )
            token = set_current_span(s)
            try:
                result = fn(*args, **kwargs)
                if cfg.capture_outputs:
                    s.set_output(result, cfg.max_payload_size)
                return result
            except Exception as e:
                s.set_error(e)
                raise
            finally:
                s.end()
                sdk.submit(s)
                reset_current_span(token)

        return sync_wrapper

    if _func is None:
        return decorator
    return decorator(_func)


def _shutdown_at_exit() -> None:
    sdk = _global_sdk
    if sdk is not None:
        try:
            sdk.shutdown()
        except Exception:
            pass


atexit.register(_shutdown_at_exit)
