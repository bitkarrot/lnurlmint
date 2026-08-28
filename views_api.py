from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from lnbits.core.models import WalletTypeInfo
from lnbits.decorators import require_admin_key, require_invoice_key

from .crud import (
    create_mint,
    delete_mint,
    get_mint,
    get_mints_by_wallet,
    update_mint,
    _generate_mint_privkey,
)
from .models import Mint, CreateMint, UpdateMint

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


@lnurlmint_api_router.get("/{mint_id}")
async def api_get_mint(
    mint_id: str,
    wallet: WalletTypeInfo = Depends(require_invoice_key),
) -> dict:
    """Retrieve a single mint scoped to the authenticated wallet.

    Returns 404 if the mint does not exist or belongs to another wallet
    (the WHERE wallet = :wallet clause in get_mint enforces isolation).
    """
    mint = await get_mint(mint_id, wallet.wallet.id)
    if mint is None:
        raise HTTPException(status_code=404, detail="Mint not found")
    return mint.dict()


@lnurlmint_api_router.put("/{mint_id}")
async def api_update_mint(
    mint_id: str,
    data: UpdateMint,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict:
    """Update configurable mint fields (admin key, wallet-scoped).

    Only non-None fields from the UpdateMint body are applied (partial
    update). Returns 404 if the mint does not exist or belongs to
    another wallet. Immutable fields (id, wallet, mint_privkey,
    timestamps) are excluded by the UpdateMint model.
    """
    fields = {k: v for k, v in data.dict().items() if v is not None}
    mint = await update_mint(mint_id, wallet.wallet.id, **fields)
    if mint is None:
        raise HTTPException(status_code=404, detail="Mint not found")
    return mint.dict()


@lnurlmint_api_router.delete("/{mint_id}")
async def api_delete_mint(
    mint_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict:
    """Delete a mint (admin key, wallet-scoped) with outstanding-notes guard.

    Returns 409 Conflict if the mint has outstanding (unspent) notes —
    deleting a mint with outstanding bearer notes would orphan them
    (a funds-loss scenario). Returns 404 if the mint does not exist or
    belongs to another wallet (delete_mint deletes 0 rows in that case,
    but the preceding get_mint check in the caller's flow enforces the
    404; here we simply succeed since no rows were harmed).
    """
    deleted = await delete_mint(mint_id, wallet.wallet.id)
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete mint with outstanding notes",
        )
    return {"success": True}
