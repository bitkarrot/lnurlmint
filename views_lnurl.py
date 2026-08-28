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

import bolt11
from fastapi import APIRouter, BackgroundTasks, Query, Request
from loguru import logger

from lnbits.core.services.payments import create_invoice as lnbits_create_invoice

from .crud import (
    PendingNoteError,
    get_mint_by_id,
    get_note,
    mark_pending,
    melt_record_exists,
    mint_record_exists,
    record_melt,
    record_mint_record,
    swap,
)
from .services import (
    _melt_pay,
    _mint_fee_msat,
    _min_sendable_msat,
    _public_base_url,
    _track_melt_end,
    _track_melt_start,
    _try_settle_mint,
    sign_note,
)

lnurlmint_lnurl_router = APIRouter()

# 64-char hex string (sha256 hash or 32-byte preimage hex). Used by the
# /w endpoint (Plan 03) for k1 validation and by the callback (Phase 4)
# for comment-hash validation.
HEX32_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

# Maximum number of k1s accepted in a single /w/cb callback request.
# Prevents a merge with an unbounded number of inputs from exhausting
# resources. The source uses settings.max_k1s = 100; the port uses a
# module-level constant (no per-mint setting for this).
_MAX_K1S = 100


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


@lnurlmint_lnurl_router.get("/w/cb/{mint_id}")
async def get_withdraw_callback(
    mint_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    k1: list[str] = Query(...),
    pr: Optional[str] = None,
    amount: Optional[int] = None,
    h: Optional[str] = None,
    h2: Optional[str] = None,
) -> dict:
    """LUD-03 withdraw callback — melt, rotate, or merge a bearer note.

    Public endpoint (no auth). Three branches based on query params:

    - **Melt** (pr present, single k1): validates the invoice, rejects
      duplicate/self-mint payment hashes (SEC-06), atomically reserves
      the note via mark_pending, registers the melt as in-flight
      (_track_melt_start, SEC-03), records the melt invoice, replies
      {\"status\":\"OK\"} immediately, and schedules the background
      _melt_pay task (tristate settlement).
    - **Rotate/merge** (pr absent, h present, amount absent): resolves
      all k1 → note_ids + values (with lazy settlement), burns all and
      mints one note keyed by h worth sum + (n-1)*base_fee refund via
      swap. Rotate is merge with n=1 (refund=0, value-neutral).
    - **Split** (pr absent, amount present): Plan 02 adds this branch
      with h2 validation and two-note mint arithmetic.

    pr MUST NOT combine with multiple k1s or amount (REDEEM-06); h is
    required when pr is absent; h2 additionally required when amount is
    present (Plan 02). Requests with more than _MAX_K1S k1s are rejected.

    No logger call includes k1, pr, h, h2, request.url, or any query
    string (SEC-05). Use mint_id, note_id, and payment_hash (all
    hashes, not secrets) if logging is needed.
    """
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}

    # REDEEM-06: pr MUST NOT combine with multiple k1s or amount.
    if pr is not None and (len(k1) > 1 or amount is not None):
        return {
            "status": "ERROR",
            "reason": "pr cannot be combined with multiple k1s or amount - merge or split first.",
        }

    # Reject requests with too many k1s (prevents unbounded merge
    # inputs from exhausting resources).
    if len(k1) > _MAX_K1S:
        return {
            "status": "ERROR",
            "reason": f"Too many k1s (max {_MAX_K1S}).",
        }

    # h required when pr is absent (REDEEM-06). h2 validation for split
    # is added in Plan 02.
    if pr is None:
        if h is None or not HEX32_PATTERN.match(h):
            return {"status": "ERROR", "reason": "missing h"}

    # --- Melt branch (pr is not None, single k1) ---
    if pr is not None:
        note_k1 = k1[0]
        if not HEX32_PATTERN.match(note_k1):
            return {"status": "ERROR", "reason": "Invalid or already spent k1."}

        note_id = sha256(bytes.fromhex(note_k1)).hexdigest()

        note = await get_note(note_id, mint_id)
        if note is None:
            settled = await _try_settle_mint(note_id, mint)
            if settled:
                note = await get_note(note_id, mint_id)
        if note is None:
            return {"status": "ERROR", "reason": "Invalid or already spent k1."}
        if note.pending:
            return {"status": "ERROR", "reason": "pending"}

        total_msat = note.amount_msat

        try:
            decoded = bolt11.decode(pr)
        except Exception as exc:
            return {"status": "ERROR", "reason": f"Invalid invoice: {exc!s}"}

        if decoded.amount_msat != total_msat:
            return {
                "status": "ERROR",
                "reason": f"Invoice must be for exactly {total_msat} msat.",
            }

        # Self-mint rejection (SEC-06): reject if the pr's payment_hash
        # exists in mints_records — the mint must not melt into an
        # invoice it issued itself.
        if decoded.has_payment_hash and await mint_record_exists(
            decoded.payment_hash
        ):
            return {
                "status": "ERROR",
                "reason": "Cannot melt into an invoice this mint issued itself.",
            }

        # Duplicate-melt rejection (SEC-06): reject if the pr's
        # payment_hash was already used by an earlier melt.
        if decoded.has_payment_hash and await melt_record_exists(
            decoded.payment_hash
        ):
            return {
                "status": "ERROR",
                "reason": "Invoice already used by an earlier melt - use a fresh one.",
            }

        # Atomically reserve the note (all-or-nothing, mint_id-scoped).
        try:
            await mark_pending([note_id], decoded.payment_hash, mint_id)
        except PendingNoteError:
            return {"status": "ERROR", "reason": "pending"}
        except ValueError as exc:
            return {"status": "ERROR", "reason": str(exc)}

        # Register in-flight AFTER mark_pending succeeds, BEFORE the
        # background task (SEC-03 — prevents the reconcile race).
        if decoded.has_payment_hash:
            await _track_melt_start(decoded.payment_hash)

        # Record the melt invoice and schedule the background tristate
        # settlement. If either raises (DB error during INSERT, or an
        # unexpected exception from FastAPI's task scheduling), release
        # the in-flight registration — _melt_pay is never scheduled, so
        # its finally block (which calls _track_melt_end) never runs.
        # Without this guard the note is stranded until process restart
        # (W-01).
        try:
            if decoded.has_payment_hash:
                await record_melt(
                    decoded.payment_hash, pr, mint.id, note_id, total_msat
                )
            background_tasks.add_task(_melt_pay, [note_id], pr, decoded, mint)
        except Exception:
            if decoded.has_payment_hash:
                await _track_melt_end(decoded.payment_hash)
            raise

        logger.debug(f"lnurlmint: scheduled melt for mint_id={mint_id}")
        return {"status": "OK"}

    # --- Split branch (pr is None, amount is not None) ---
    # Plan 02 adds the full split branch with h2 validation and
    # two-note mint arithmetic. For now, reject split requests.
    if amount is not None:
        return {"status": "ERROR", "reason": "Split not available."}

    # --- Rotate/merge branch (pr is None, h is present, amount is None) ---
    # Resolve all k1 → note_ids + values (with lazy settlement via
    # _try_settle_mint). Rotate is merge with n=1 (refund=0, value-
    # neutral). Merge refunds (n-1)*base_fee_msat — every base fee
    # collected beyond the single one this now-one note should have
    # cost. Both return {"status":"OK"} (sig deferred to Phase 5).
    note_ids: list[str] = []
    values: list[int] = []
    for note_k1 in k1:
        if not HEX32_PATTERN.match(note_k1):
            return {"status": "ERROR", "reason": "Invalid or already spent k1."}
        note_id = sha256(bytes.fromhex(note_k1)).hexdigest()
        note = await get_note(note_id, mint_id)
        if note is None:
            settled = await _try_settle_mint(note_id, mint)
            if settled:
                note = await get_note(note_id, mint_id)
        if note is None:
            return {"status": "ERROR", "reason": "Invalid or already spent k1."}
        if note.pending:
            return {"status": "ERROR", "reason": "pending"}
        if note.spent:
            return {"status": "ERROR", "reason": "Invalid or already spent k1."}
        note_ids.append(note_id)
        values.append(note.amount_msat)

    total_msat = sum(values)
    refund = (len(note_ids) - 1) * mint.base_fee_msat
    merged_amount = total_msat + refund

    try:
        await swap(note_ids, [h], [merged_amount], mint_id)
    except PendingNoteError:
        return {"status": "ERROR", "reason": "pending"}
    except ValueError as exc:
        return {"status": "ERROR", "reason": str(exc)}

    await sign_note(h, merged_amount, mint)
    logger.debug(f"lnurlmint: rotate/merge for mint_id={mint_id}")
    return {"status": "OK"}
