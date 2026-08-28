import time
from typing import Optional

from lnbits.db import Database

from .models import Mint

db = Database("ext_lnurlmint")

# Whitelist of column names that update_mint may set. This guards against
# SQL injection via column-name interpolation (W-01) and prevents accidentally
# updating immutable fields (id, wallet, mint_privkey, created_at, updated_at).
_UPDATABLE_FIELDS = frozenset({
    "username",
    "base_url",
    "onion_url",
    "base_fee_msat",
    "fee_percent_ppm",
    "min_sendable_msat",
    "max_sendable_msat",
    "min_mint_msat",
    "verify_enabled",
    "sunset_mint",
})


def _generate_mint_privkey() -> str:
    """Generate a secp256k1 private key as a 64-char hex string.

    Uses coincurve (a transitive dependency already imported by LNbits
    core's nostr/nwc code). The public key / signing logic is added in
    Phase 5; the column and key generation happen here to avoid a
    later migration.
    """
    from coincurve import PrivateKey

    return PrivateKey().secret.hex()


async def create_mint(mint: Mint) -> Mint:
    """Insert a new mint row and return it."""
    await db.insert("lnurlmint.mints", mint)
    return mint


async def get_mints_by_wallet(wallet_id: str) -> list[Mint]:
    """Return all mints owned by a wallet (wallet-scoped query)."""
    return await db.fetchall(
        "SELECT * FROM lnurlmint.mints WHERE wallet = :wallet",
        {"wallet": wallet_id},
        Mint,
    )


async def get_mint(mint_id: str, wallet_id: str) -> Optional[Mint]:
    """Return a single mint scoped to a wallet, or None if not found.

    The WHERE wallet = :wallet clause enforces cross-wallet isolation —
    wallet A cannot retrieve wallet B's mint.
    """
    return await db.fetchone(
        "SELECT * FROM lnurlmint.mints WHERE id = :id AND wallet = :wallet",
        {"id": mint_id, "wallet": wallet_id},
        Mint,
    )


async def update_mint(mint_id: str, wallet_id: str, **fields) -> Optional[Mint]:
    """Update configurable mint fields (wallet-scoped), return updated mint.

    Builds a dynamic SET clause from the provided field keys (only keys
    that are not None). The WHERE wallet = :wallet clause enforces
    cross-wallet isolation — wallet A cannot update wallet B's mint.
    Returns the updated mint via get_mint, or None if the mint does not
    belong to the wallet.
    """
    if not fields:
        # Nothing to update — just return the current mint (or None).
        return await get_mint(mint_id, wallet_id)

    # Filter against the whitelist so only known-updatable column names
    # reach the SQL string (W-01: guard against column-name injection).
    fields = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not fields:
        return await get_mint(mint_id, wallet_id)

    set_clauses = ", ".join(f"{k} = :{k}" for k in fields)
    set_clauses += f", updated_at = {db.timestamp_placeholder('now')}"
    values = {"id": mint_id, "wallet": wallet_id, "now": time.time(), **fields}
    await db.execute(
        f"UPDATE lnurlmint.mints SET {set_clauses} "
        "WHERE id = :id AND wallet = :wallet",
        values,
    )
    return await get_mint(mint_id, wallet_id)


async def count_outstanding_notes(mint_id: str, wallet_id: str) -> int:
    """Count unspent, non-pending notes for a mint (wallet-scoped via JOIN).

    Joins lnurlmint.notes on lnurlmint.mints so the wallet scoping is
    enforced even though the notes table has no wallet column of its own.
    Used by delete_mint to guard against orphaning outstanding bearer
    notes (a funds-loss scenario).
    """
    result = await db.fetchone(
        "SELECT COUNT(*) as count FROM lnurlmint.notes n "
        "JOIN lnurlmint.mints m ON n.mint_id = m.id "
        "WHERE n.mint_id = :mid AND m.wallet = :wallet AND n.spent = 0",
        {"mid": mint_id, "wallet": wallet_id},
    )
    return int(result["count"]) if result else 0


async def delete_mint(mint_id: str, wallet_id: str) -> bool:
    """Atomically check for outstanding notes and delete a mint.

    Uses a single `async with db.connect() as conn:` block so the
    outstanding-notes check and the delete run in one transaction (the
    LNbits Database abstraction otherwise opens a separate transaction
    per call). Returns True if the mint was deleted, False if it has
    outstanding notes (caller should return 409 Conflict). A mint that
    does not belong to the wallet simply deletes 0 rows and returns True
    (the caller's preceding get_mint/update_mint already enforced the
    404 for cross-wallet access).
    """
    async with db.connect() as conn:
        count_result = await conn.fetchone(
            "SELECT COUNT(*) as count FROM lnurlmint.notes n "
            "JOIN lnurlmint.mints m ON n.mint_id = m.id "
            "WHERE n.mint_id = :mid AND m.wallet = :wallet AND n.spent = 0",
            {"mid": mint_id, "wallet": wallet_id},
        )
        count = int(count_result["count"]) if count_result else 0
        if count > 0:
            return False
        await conn.execute(
            "DELETE FROM lnurlmint.mints WHERE id = :id AND wallet = :wallet",
            {"id": mint_id, "wallet": wallet_id},
        )
    return True
