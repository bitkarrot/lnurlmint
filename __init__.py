import asyncio

from fastapi import APIRouter
from loguru import logger

from .crud import db
from .views import lnurlmint_generic_router
from .views_api import lnurlmint_api_router, lnurlmint_public_router
from .views_lnurl import lnurlmint_lnurl_router

lnurlmint_ext: APIRouter = APIRouter(prefix="/lnurlmint", tags=["lnurlmint"])
lnurlmint_ext.include_router(lnurlmint_generic_router)
lnurlmint_ext.include_router(lnurlmint_api_router)
lnurlmint_ext.include_router(lnurlmint_public_router)
lnurlmint_ext.include_router(lnurlmint_lnurl_router)

lnurlmint_static_files = [
    {
        "path": "/lnurlmint/static",
        "name": "lnurlmint_static",
    }
]

scheduled_tasks: list[asyncio.Task] = []


def lnurlmint_stop():
    """Stop background tasks."""
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)


def lnurlmint_start():
    """Start background tasks.

    Schedules a boot-time one-shot reconcile (resolves stranded notes
    from a crashed process immediately on startup, REC-02) and registers
    the periodic reconcile task via create_permanent_unique_task (EXT-03).
    The periodic task calls reconcile_pending_melts every 60 seconds.
    """
    from lnbits.tasks import create_permanent_unique_task

    from .services import boot_reconcile
    from .tasks import wait_for_melt_reconcile

    # Boot-time one-shot reconcile (resolves stranded notes from a
    # crashed process before the periodic task starts).
    asyncio.create_task(boot_reconcile())

    # Periodic reconcile (every 60s).
    task = create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile)
    scheduled_tasks.append(task)


__all__ = ["db", "lnurlmint_ext", "lnurlmint_start", "lnurlmint_static_files", "lnurlmint_stop"]
