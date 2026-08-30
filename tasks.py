"""Background task wrappers for the lnurlmint extension (Phase 2).

The periodic reconcile task is registered with create_permanent_unique_task
in lnurlmint_start (EXT-03). run_interval(60, reconcile_pending_melts)
calls reconcile every 60 seconds — LNbits' task wrapper crash-restarts
the coroutine if it raises, so reconcile is self-healing.
"""

import asyncio
from collections.abc import Callable, Coroutine

from lnbits.settings import settings

from .services import reconcile_pending_melts


def run_interval(
    seconds: int, func: Callable[[], Coroutine[None, None, None]]
) -> Callable[[], Coroutine[None, None, None]]:
    async def wrapper() -> None:
        while settings.lnbits_running:
            await func()
            await asyncio.sleep(seconds)

    return wrapper


async def wait_for_melt_reconcile() -> None:
    """Periodic reconcile task registered with create_permanent_unique_task.

    Wraps run_interval(60, reconcile_pending_melts) — calls reconcile
    every 60 seconds to resolve stranded pending notes from crashed or
    restarted melts (REC-02). In-flight melts are skipped (SEC-03).
    """
    await run_interval(60, reconcile_pending_melts)()
