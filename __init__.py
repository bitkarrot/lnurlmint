import asyncio

from fastapi import APIRouter
from loguru import logger

from .crud import db
from .views import lnurlmint_generic_router
from .views_api import lnurlmint_api_router
from .views_lnurl import lnurlmint_lnurl_router

lnurlmint_ext: APIRouter = APIRouter(prefix="/lnurlmint", tags=["lnurlmint"])
lnurlmint_ext.include_router(lnurlmint_generic_router)
lnurlmint_ext.include_router(lnurlmint_api_router)
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
    """Start background tasks (stub for Phase 1)."""
    # Phase 2 will add: create_permanent_unique_task("ext_lnurlmint", wait_for_reconcile)
    pass


__all__ = ["db", "lnurlmint_ext", "lnurlmint_start", "lnurlmint_static_files", "lnurlmint_stop"]
