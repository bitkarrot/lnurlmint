from fastapi import APIRouter, Depends
from lnbits.core.views.generic import index, index_public
from lnbits.decorators import check_user_exists

lnurlmint_generic_router = APIRouter()


lnurlmint_generic_router.add_api_route(
    "/",
    methods=["GET"],
    endpoint=index,
    dependencies=[Depends(check_user_exists)],
)


lnurlmint_generic_router.add_api_route(
    "/m/{mint_id}",
    methods=["GET"],
    endpoint=index_public,
)
