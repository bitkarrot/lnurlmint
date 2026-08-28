"""LNURL endpoints for the mint flow (Phase 2, Plan 02-02).

LUD-06 payRequest advertisement and the mint callback that creates an
invoice and records a pending mint. The note is NOT materialized at
callback time — it materializes lazily on the first /w or /verify poll
after settlement via _try_settle_mint (services.py).

All LNURL errors return {"status": "ERROR", "reason": "..."} as a plain
dict — FastAPI serializes it as JSON with HTTP 200 (not HTTPException),
per LUD-06 error convention. No logger call includes k1, a spendable
credential, or the full request URL (SEC-05).
"""

import json
import re
from hashlib import sha256
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from loguru import logger

from lnbits.core.services.payments import create_invoice as lnbits_create_invoice

from .crud import get_mint_by_id, get_note, record_mint_record
from .services import _mint_fee_msat, _min_sendable_msat, _public_base_url, _try_settle_mint

lnurlmint_lnurl_router = APIRouter()

# 64-char hex string (sha256 hash or 32-byte preimage hex). Used by the
# /w endpoint (Plan 03) for k1 validation and by the callback (Phase 4)
# for comment-hash validation.
HEX32_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


@lnurlmint_lnurl_router.get("/lnurlp/{mint_id}")
async def get_payrequest(mint_id: str, request: Request) -> dict:
    """LUD-06 payRequest — advertises the mint flow with fee-aware bounds.

    Public endpoint (no auth). Returns the callback URL, fee-aware
    minSendable/maxSendable, metadata (description + text/identifier +
    Mint fees entry), and the withdrawLink pointing to the
    informational /w endpoint. Rejects unknown mints and sunsetting
    mints with LNURL-formatted errors.
    """
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}
    if mint.sunset_mint:
        return {
            "status": "ERROR",
            "reason": "This mint is sunsetting - minting is disabled.",
        }

    base = _public_base_url(request, mint)
    host = urlparse(base).netloc

    metadata_entries = [
        ["text/plain", f"Mint an lnurlcash bearer note on {mint.username}"],
        ["text/identifier", f"{mint.username}@{host}"],
    ]
    if mint.base_fee_msat or mint.fee_percent_ppm:
        metadata_entries.append(
            [
                "text/plain",
                f"Mint fees: {mint.base_fee_msat},{mint.fee_percent_ppm}",
            ]
        )
    metadata = json.dumps(metadata_entries)

    return {
        "tag": "payRequest",
        "callback": f"{base}/lnurlmint/p/cb/{mint_id}",
        "minSendable": _min_sendable_msat(mint),
        "maxSendable": mint.max_sendable_msat,
        "metadata": metadata,
        "withdrawLink": f"{base}/lnurlmint/w/{mint_id}",
        "commentAllowed": 64,
    }


@lnurlmint_lnurl_router.get("/p/cb/{mint_id}")
async def get_pay_callback(mint_id: str, request: Request, amount: int) -> dict:
    """Mint callback — creates an invoice and records a pending mint.

    Public endpoint (no auth). Validates the amount against fee-aware
    bounds, creates an invoice via LNbits on the mint's wallet, records
    the pending mint (net amount after fee) in mints_records, and
    returns {pr, disposable: false}. The note is NOT materialized here
    — lazy settlement materializes it on the first /w poll after the
    invoice settles (MINT-03).
    """
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}
    if mint.sunset_mint:
        return {
            "status": "ERROR",
            "reason": "This mint is sunsetting - minting is disabled.",
        }
    if amount < mint.min_sendable_msat:
        return {"status": "ERROR", "reason": "Amount too low."}
    if amount > mint.max_sendable_msat:
        return {"status": "ERROR", "reason": "Amount too high."}

    net_amount_msat = amount - _mint_fee_msat(amount, mint)
    if net_amount_msat < mint.min_mint_msat:
        return {
            "status": "ERROR",
            "reason": (
                f"Amount too low to mint a note "
                f"(min {mint.min_mint_msat} msat net of fees)."
            ),
        }

    payment = await lnbits_create_invoice(
        wallet_id=mint.wallet,
        amount=amount // 1000,  # msat → sat
        memo=f"lnurlcash mint on {mint.username}",
        extra={"lnurlmint": "mint", "mint_id": mint.id},
    )
    pr = payment.bolt11
    payment_hash = payment.payment_hash

    await record_mint_record(
        payment_hash=payment_hash,
        mint_id=mint.id,
        pr=pr,
        amount_msat=net_amount_msat,
    )

    logger.debug(f"lnurlmint: recorded pending mint for mint_id={mint_id}")
    return {"pr": pr, "disposable": False}


@lnurlmint_lnurl_router.get("/w/{mint_id}")
async def get_withdraw(
    mint_id: str, request: Request, k1: str, amount: Optional[int] = None
) -> dict:
    """LUD-03 withdrawRequest — purely informational note-value advertisement.

    Public endpoint (no auth). Advertises a note's value to any
    spec-compliant LNURL-withdraw wallet WITHOUT burning or altering the
    note. The `k1` is echoed verbatim (the raw bearer secret, never the
    derived note id). `amount` is accepted for LUD-03 compliance but
    ignored — `maxWithdrawable` is authoritative. If the note isn't
    materialized yet, the first poll triggers lazy settlement via
    _try_settle_mint (REDEEM-01). Pending notes are rejected (SEC-04 —
    a pending note is never advertised as withdrawable). No mintPubkey
    in Phase 2 (Phase 5 adds the per-mint keypair).

    No logger call includes k1, request.url, or any query string (SEC-05).
    """
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}

    if not HEX32_PATTERN.match(k1):
        return {"status": "ERROR", "reason": "Unknown note."}

    # Store-hashes-not-secrets: the note id is sha256(k1), never the
    # spendable credential itself (SEC-02).
    note_id = sha256(bytes.fromhex(k1)).hexdigest()

    note = await get_note(note_id, mint_id)
    if note is None:
        # Not materialized yet — try lazy settlement on this poll.
        settled = await _try_settle_mint(note_id, mint)
        if settled:
            note = await get_note(note_id, mint_id)

    if note is None:
        return {"status": "ERROR", "reason": "Unknown note."}
    if note.pending:
        return {"status": "ERROR", "reason": "pending"}
    if note.spent:
        return {"status": "ERROR", "reason": "Note already spent."}

    base = _public_base_url(request, mint)
    return {
        "tag": "withdrawRequest",
        "callback": f"{base}/lnurlmint/w/cb/{mint_id}",
        "k1": k1,
        "minWithdrawable": note.amount_msat,
        "maxWithdrawable": note.amount_msat,
        "defaultDescription": f"lnurlcash bearer note on {mint.username}",
    }
