from typing import Optional

from lnbits.db import Database

from .models import Mint

db = Database("ext_lnurlmint")


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
