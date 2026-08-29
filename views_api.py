from datetime import datetime, timezone
from uuid import uuid4

from bech32 import bech32_encode, convertbits
from fastapi import APIRouter, Depends, HTTPException, Request

from lnbits.core.models import WalletTypeInfo
from lnbits.decorators import require_admin_key, require_invoice_key
from lnbits.wallets import get_funding_source
from lnbits.wallets.base import Feature

from .crud import (
    create_mint,
    delete_mint,
    get_mint,
    get_mint_by_id,
    get_mints_by_wallet,
    get_outstanding_notes,
    get_mint_activity,
    update_mint,
    _generate_mint_privkey,
)
from .models import Mint, CreateMint, UpdateMint, MintResponse
from .services import _public_base_url, max_mintable_msat
from .signing import mint_pubkey

lnurlmint_api_router = APIRouter(prefix="/api/v1/mints")
lnurlmint_public_router = APIRouter(prefix="/api/v1/public")


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
    return MintResponse(**mint.dict(exclude={"mint_privkey"})).dict()


@lnurlmint_api_router.get("")
async def api_get_mints(
    wallet: WalletTypeInfo = Depends(require_invoice_key),
) -> list:
    """List all mints owned by the authenticated wallet (wallet-scoped)."""
    mints = await get_mints_by_wallet(wallet.wallet.id)
    return [MintResponse(**m.dict(exclude={"mint_privkey"})).dict() for m in mints]


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
    return MintResponse(**mint.dict(exclude={"mint_privkey"})).dict()


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
    return MintResponse(**mint.dict(exclude={"mint_privkey"})).dict()


@lnurlmint_api_router.delete("/{mint_id}")
async def api_delete_mint(
    mint_id: str,
    wallet: WalletTypeInfo = Depends(require_admin_key),
) -> dict:
    """Delete a mint (admin key, wallet-scoped) with outstanding-notes guard.

    Returns 409 Conflict if the mint has outstanding (unspent) notes —
    deleting a mint with outstanding bearer notes would orphan them
    (a funds-loss scenario). Returns 404 if the mint does not exist or
    belongs to another wallet (checked via get_mint before deleting).
    """
    # Verify the mint exists and belongs to this wallet before deleting.
    # Without this, a cross-wallet delete returns 200 (delete_mint finds
    # 0 outstanding notes via the wallet-scoped JOIN and deletes 0 rows,
    # returning True) — the caller would see success for a mint it
    # doesn't own. The get_mint check enforces the 404.
    mint = await get_mint(mint_id, wallet.wallet.id)
    if mint is None:
        raise HTTPException(status_code=404, detail="Mint not found")
    deleted = await delete_mint(mint_id, wallet.wallet.id)
    if not deleted:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete mint with outstanding notes",
        )
    return {"success": True}


@lnurlmint_api_router.get("/{mint_id}/notes")
async def api_get_mint_notes(
    mint_id: str,
    wallet: WalletTypeInfo = Depends(require_invoice_key),
) -> list:
    """List outstanding notes for a mint (invoice key, wallet-scoped).

    Returns 404 if the mint does not exist or belongs to another
    wallet. Notes include id, amount_msat, spent, pending, and
    created_at.
    """
    mint = await get_mint(mint_id, wallet.wallet.id)
    if mint is None:
        raise HTTPException(status_code=404, detail="Mint not found")
    notes = await get_outstanding_notes(mint_id, wallet.wallet.id)
    return [
        {
            "id": n.id,
            "amount_msat": n.amount_msat,
            "spent": n.spent,
            "pending": n.pending,
            "created_at": n.created_at.isoformat() if n.created_at else None,
        }
        for n in notes
    ]


@lnurlmint_api_router.get("/{mint_id}/activity")
async def api_get_mint_activity(
    mint_id: str,
    wallet: WalletTypeInfo = Depends(require_invoice_key),
) -> list:
    """Recent mint/melt activity for a mint (invoice key, wallet-scoped).

    Returns 404 if the mint does not exist or belongs to another
    wallet. Activity records include type, amount_msat, payment_hash,
    pr, settled/minted, and created_at.
    """
    mint = await get_mint(mint_id, wallet.wallet.id)
    if mint is None:
        raise HTTPException(status_code=404, detail="Mint not found")
    activity = await get_mint_activity(mint_id, wallet.wallet.id)
    return [
        {
            "type": r["type"],
            "amount_msat": r["amount_msat"],
            "payment_hash": r["payment_hash"],
            "pr": r["pr"],
            "settled": bool(r.get("settled") or r.get("minted")),
            "created_at": str(r["created_at"]),
        }
        for r in activity
    ]


# ---------------------------------------------------------------------------
# Public mint info (Phase 6 — unauthenticated one-pager data endpoint)
# ---------------------------------------------------------------------------


@lnurlmint_public_router.get("/{mint_id}")
async def api_get_public_mint_info(mint_id: str, request: Request) -> dict:
    """Public mint info for the one-pager (no authentication).

    Returns mint metadata (username, limits, sunset, onion_url),
    the LNURL of the payRequest (bech32-encoded, Tor-aware), the
    mint's public signing key, and node info (if the funding source
    implements the Node API). Returns 404 for unknown mint_id.

    No sensitive data is exposed: no wallet_id, no mint_privkey,
    no note secrets. The mint_pubkey is the mint's public signing
    key (not a secret). Node info is already public on the LN graph.
    """
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        raise HTTPException(status_code=404, detail="Mint not found")

    base = _public_base_url(request, mint)
    lnurl_url = f"{base}/lnurlmint/lnurlp/{mint_id}"
    lnurl_data = convertbits(lnurl_url.encode(), 8, 5, True)
    lnurl = bech32_encode("lnurl", lnurl_data).upper()

    # Fetch node info if the funding source implements the Node API.
    node_info = None
    try:
        funding_source = get_funding_source()
        if (
            funding_source.features
            and Feature.nodemanager in funding_source.features
            and funding_source.__node_cls__
        ):
            node = funding_source.__node_cls__(funding_source)
            public_info = await node.get_public_info()
            node_info = {
                "id": public_info.id,
                "alias": public_info.alias,
                "color": public_info.color,
                "num_peers": public_info.num_peers,
                "capacity_msat": public_info.channel_stats.total_capacity,
                "num_channels": sum(
                    public_info.channel_stats.counts.values()
                ),
                "addresses": public_info.addresses,
            }
    except Exception:
        pass  # Graceful degradation — node_info stays null.

    return {
        "username": mint.username,
        "lnurl": lnurl,
        "min_mint_msat": mint.min_mint_msat,
        "max_mintable_msat": max_mintable_msat(mint),
        "sunset_mint": mint.sunset_mint,
        "onion_url": mint.onion_url,
        "mint_pubkey": mint_pubkey(mint),
        "node_info": node_info,
    }
