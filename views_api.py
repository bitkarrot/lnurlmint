from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends

from lnbits.core.models import WalletTypeInfo
from lnbits.decorators import require_admin_key, require_invoice_key

from .crud import create_mint, get_mints_by_wallet, _generate_mint_privkey
from .models import Mint, CreateMint

lnurlmint_api_router = APIRouter(prefix="/api/v1/mints")


@lnurlmint_api_router.post("")
async def api_create_mint(
    data: CreateMint,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict:
    """Create a new per-wallet mint.

    The wallet id is taken from the authenticated admin key (never from
    the request body) so cross-wallet mint creation is impossible.
    """
    mint_id = uuid4().hex
    privkey = _generate_mint_privkey()
    now = datetime.now(timezone.utc)
    mint = Mint(
        id=mint_id,
        wallet=wallet.wallet.id,
        username=data.username,
        base_url=data.base_url,
        onion_url=data.onion_url,
        base_fee_msat=data.base_fee_msat,
        fee_percent_ppm=data.fee_percent_ppm,
        min_sendable_msat=data.min_sendable_msat,
        max_sendable_msat=data.max_sendable_msat,
        min_mint_msat=data.min_mint_msat,
        verify_enabled=data.verify_enabled,
        sunset_mint=data.sunset_mint,
        mint_privkey=privkey,
        created_at=now,
        updated_at=now,
    )
    await create_mint(mint)
    return mint.dict()


@lnurlmint_api_router.get("")
async def api_get_mints(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
) -> list:
    """List all mints owned by the authenticated wallet (wallet-scoped)."""
    mints = await get_mints_by_wallet(wallet.wallet.id)
    return [m.dict() for m in mints]
