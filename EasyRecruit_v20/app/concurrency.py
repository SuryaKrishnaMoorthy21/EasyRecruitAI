"""
Bounded execution of CPU-heavy work (Task 5).

Parsing a document and running spaCy / the sentence-transformer are CPU- and memory-hungry. Before
this module nothing limited how many ran at once: a 15-file batch started 15 simultaneous analyses,
a few simultaneous batches filled FastAPI's shared 40-thread pool (starving every other endpoint), and
twelve simultaneous *first* requests each built their own copy of the NLP models.

AnalysisGate fixes the first two, `singleton_lock` the third.
"""
import asyncio
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

# One lock serialises the construction of every lazily-created NLP singleton in the process. Loading
# spaCy / a transformer is slow and memory-hungry, so two of them must never run at the same time anyway.
# Re-entrant, because one factory may legitimately trigger another.
singleton_lock = threading.RLock()


class AnalysisBusy(HTTPException):
    """No analysis slot became free in time (HTTP 503 + Retry-After)."""

    def __init__(self, retry_after: int = 15):
        super().__init__(
            status_code=503,
            detail="The server is busy analysing other resumes. Please try again in a moment.",
            headers={"Retry-After": str(retry_after)},
        )


class AnalysisTimeout(HTTPException):
    """A processing step exceeded its time budget (HTTP 504)."""

    def __init__(self):
        super().__init__(
            status_code=504,
            detail="Analysis took too long and was stopped. Please try again or upload a shorter document.",
        )


class AnalysisGate:
    """
    Limits how many CPU-heavy jobs EXECUTE at any instant in this process.

      * Jobs run on a dedicated thread pool of `max_concurrent` workers, so analysis can never starve
        the framework's shared pool (which also runs every other sync endpoint and dependency).
      * Admission is an asyncio semaphore: work that is waiting is a suspended coroutine, not a
        blocked thread.
      * Python threads cannot be killed, so a job that outlives its timeout keeps running. Its slot is
        therefore released only when its thread REALLY finishes. A timeout frees the caller, never
        the CPU, and the concurrency bound stays true even while jobs hang.
    """

    def __init__(self, max_concurrent: int):
        self.max_concurrent = max_concurrent
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="analysis")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self.running = 0          # jobs currently holding a slot (diagnostics / tests)
        self.waiting = 0          # callers currently waiting for a slot

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    def _semaphore_for_running_loop(self) -> asyncio.Semaphore:
        # Production has exactly one event loop. Tests create one per TestClient, and an asyncio
        # semaphore must not be shared between loops, so rebuild it if the loop changed.
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._loop, self._semaphore = loop, asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

    @staticmethod
    def _call_not_started(callback: Optional[Callable[[], None]]) -> None:
        if callback is not None:
            try:
                callback()
            except Exception:                                   # cleanup must never mask the real outcome
                logger.debug("on_not_started callback failed", exc_info=True)

    def _finished(self, semaphore: asyncio.Semaphore, future: "asyncio.Future") -> None:
        # Runs on the event loop when the worker thread has really finished (success, error or after a timeout).
        self.running -= 1
        semaphore.release()
        if not future.cancelled():
            future.exception()                                  # mark retrieved: nobody may be awaiting it any more

    async def run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        timeout: float,
        queue_timeout: Optional[float] = None,
        on_not_started: Optional[Callable[[], None]] = None,
    ) -> Any:
        """
        Run fn(*args) on a worker thread once a slot is free and return its result.

        timeout        -- seconds the job may EXECUTE (queue time not included); AnalysisTimeout afterwards.
        queue_timeout  -- seconds to wait for a free slot (None = wait indefinitely, e.g. batch items,
                          which are bounded by the batch deadline instead); AnalysisBusy afterwards.
        on_not_started -- called if the job is never submitted (busy / cancelled while queued) so the
                          caller can release what the job would otherwise have owned. Once a job has been
                          submitted the job itself owns that cleanup, because it outlives a timeout.
        """
        semaphore = self._semaphore_for_running_loop()
        self.waiting += 1
        try:
            if queue_timeout is None:
                await semaphore.acquire()
            else:
                acquire = asyncio.ensure_future(semaphore.acquire())
                try:
                    done, _ = await asyncio.wait({acquire}, timeout=queue_timeout)
                except asyncio.CancelledError:
                    acquire.cancel()
                    if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                        semaphore.release()                     # the acquire raced to completion: hand the slot back
                    raise
                if not done:
                    acquire.cancel()
                    await asyncio.sleep(0)
                    if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                        semaphore.release()
                    raise AnalysisBusy()
        except BaseException:
            self._call_not_started(on_not_started)
            raise
        finally:
            self.waiting -= 1

        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, functools.partial(fn, *args))
        except BaseException:
            semaphore.release()
            self._call_not_started(on_not_started)
            raise
        self.running += 1
        future.add_done_callback(functools.partial(self._finished, semaphore))

        # asyncio.wait neither cancels `future` on timeout nor when this task is cancelled, which is
        # exactly the accounting we want: the job keeps its slot until its thread is done.
        done, _ = await asyncio.wait({future}, timeout=timeout)
        if not done:
            logger.warning(
                "Analysis step exceeded %ss; the caller was released but the worker keeps its slot until it finishes",
                timeout,
            )
            raise AnalysisTimeout()
        return future.result()


_gate: Optional[AnalysisGate] = None
_gate_lock = threading.Lock()


def get_gate() -> AnalysisGate:
    """The process-wide gate; rebuilt only if ANALYSIS_CONCURRENCY changed (tests, hot reconfiguration)."""
    global _gate
    wanted = int(settings.ANALYSIS_CONCURRENCY)
    gate = _gate
    if gate is None or gate.max_concurrent != wanted:
        with _gate_lock:
            if _gate is None or _gate.max_concurrent != wanted:
                previous, _gate = _gate, AnalysisGate(wanted)
                if previous is not None:
                    previous.shutdown()                         # running jobs finish; idle workers exit
            gate = _gate
    return gate
