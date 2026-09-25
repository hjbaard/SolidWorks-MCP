"""Single-threaded COM execution for SolidWorks.

COM is single-threaded-apartment (STA) and thread-affine: every call must run on
the same thread that CoInitialized the apartment and connected to SolidWorks.
The MCP server runs on an asyncio event loop, and tool handlers may be scheduled
in ways that make "just call it inline" unsafe over time. So we pin ALL COM work
to one dedicated thread and have handlers submit callables to it.

This is how the kickoff principle "one COM session, single-threaded" is actively
enforced inside an async server -- it does not happen by itself.

The worker blocks on a queue (event-driven, no polling) and runs one job at a
time, which also serialises calls exactly as the stateful SolidWorks API wants.

Design constraints to be aware of:
- Non-pumping STA: the loop only blocks on the queue; it never calls
  PumpWaitingMessages. That is fine for a purely outbound, out-of-process client
  that registers NO COM event sinks (the current usage). Do not register event
  sinks on this thread without adding a message pump.
- No in-flight cancellation: a COM call cannot be force-aborted once started, so
  `call` enforces a timeout on the *await* (surfacing a readable error) but the
  worker thread itself stays on the blocked call until SolidWorks returns.
"""

import asyncio
import concurrent.futures
import queue
import threading

import pythoncom

from .errors import SolidWorksError

# Most SolidWorks API calls finish in well under a second; a generous ceiling
# turns an unattended hang (e.g. an unsuppressed modal dialog) into a readable
# error instead of an infinite wait that wedges the whole server.
DEFAULT_CALL_TIMEOUT_S = 120.0


class ComWorker:
    """Runs submitted callables on one dedicated STA thread, one at a time."""

    _STOP = object()

    def __init__(self, call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S) -> None:
        self._call_timeout_s = call_timeout_s
        self._queue: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._shutting_down = False
        self._thread = threading.Thread(target=self._run, name="solidworks-com", daemon=True)
        self._thread.start()
        self._ready.wait()  # block until the apartment is initialised

    def _run(self) -> None:
        pythoncom.CoInitialize()  # STA for this thread
        self._ready.set()
        try:
            while True:
                job = self._queue.get()
                if job is self._STOP:
                    break
                fn, fut = job
                if not fut.set_running_or_notify_cancel():
                    continue  # awaiter timed out / gave up before we started this job
                try:
                    fut.set_result(fn())
                except BaseException as exc:  # noqa: BLE001 - relay to the caller
                    fut.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()

    def submit(self, fn) -> "concurrent.futures.Future":
        if self._shutting_down:
            raise SolidWorksError("The COM worker is shut down; no new operations are possible.")
        fut: "concurrent.futures.Future" = concurrent.futures.Future()
        self._queue.put((fn, fut))
        return fut

    async def call(self, fn):
        """Submit `fn` to the COM thread and await its result, with a timeout.

        On timeout the COM call is almost always blocked on a modal dialog. We
        cannot force-abort it, so we raise a clear, actionable error rather than
        hang forever. The job, if not yet started, is skipped when dequeued.
        """
        fut = self.submit(fn)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), self._call_timeout_s)
        except asyncio.TimeoutError:
            raise SolidWorksError(
                f"SolidWorks did not respond within {self._call_timeout_s:.0f}s. "
                "A modal dialog is probably open in SolidWorks. "
                "Close any dialogs; restart the server if it keeps hanging."
            )

    def shutdown(self) -> None:
        self._shutting_down = True
        self._queue.put(self._STOP)
        self._thread.join(timeout=5)
