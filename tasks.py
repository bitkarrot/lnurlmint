"""Background task wrappers for the lnurlmint extension.

The periodic tasks are registered with create_permanent_unique_task in
lnurlmint_start (EXT-03). run_interval wraps a coroutine into a loop
guarded on settings.lnbits_running — LNbits' task wrapper crash-restarts
the coroutine if it raises, so the tasks are self-healing.

- wait_for_melt_reconcile: every 60s, resolves stranded pending notes
  from crashed or restarted melts (REC-02). In-flight melts are skipped
  (SEC-03).
- wait_for_zap_receipts: every 5s, settles recently issued zap invoices
  and publishes their kind 9735 receipts (upstream's zap poll). A cheap
  no-op while no mint has zaps enabled.
"""

import asyncio
from collections.abc import Callable, Coroutine

from lnbits.settings import settings

from .services import publish_all_zap_receipts, reconcile_pending_melts


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


async def wait_for_zap_receipts() -> None:
    """Periodic zap-receipt task (NIP-57) — settle recently issued zap
    invoices and publish their kind 9735 receipts every 5 seconds
    (upstream's poll cadence)."""
    await run_interval(5, publish_all_zap_receipts)()
