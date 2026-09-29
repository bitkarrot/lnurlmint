"""LNURL endpoints for the mint flow — LUD-25 taproot protocol.

Every note is a BIP-341 taproot output key Q stored under hex(Q). A `k1`
is a spend of one: a `ck1` (key path), a `cw1` (script path), or the
64-hex preimage of a bearer note (its short form). Where a `cp1` goes
(mint `comment`, `p1`/`p2`, `?p=`), a 64-hex `h` is the bearer short
form. Decoding/verification lives in spend.py (lnurlcashkernel parity;
arbitrary script-path spends additionally need the optional native
`lnurlcashkernel` package).

Endpoints (mounted under /lnurlmint by __init__.py — an extension cannot
claim the root /.well-known paths, so the LUD-16/NIP-05 equivalents live
here; operators wanting real user@host addresses can rewrite
/.well-known/lnurlp/{u} -> /lnurlmint/lnurlp/{mint_id}/{u} and
/.well-known/nostr.json -> /lnurlmint/nostr.json/{mint_id} in their
reverse proxy):

- GET  /lnurlp/{mint_id}             fixed-identity LUD-06 payRequest
- GET  /lnurlp/{mint_id}/{username}  registered username's payRequest
- GET  /p/cb/{mint_id}               fixed-identity callback (comment required)
- GET  /p/{mint_id}/{username}       registered callback (auto-mint on cx1
                                     branch when no comment; ?nostr= zaps)
- POST   /p/{mint_id}/{username}     claim username for a cx1 branch (?sig=)
- DELETE /p/{mint_id}/{username}     free a username (?sig=)
- GET  /nostr.json/{mint_id}         NIP-05 (?name=)
- GET  /verify/{mint_id}/{payment_hash}  LUD-21 settlement status
- GET  /w/{mint_id}                  informational withdrawRequest (k1 or p)
- GET  /w/cb/{mint_id}               melt/rotate/split/merge

All LNURL errors return {"status": "ERROR", "reason": "..."} as a plain
dict — FastAPI serializes it as JSON with HTTP 200, per LUD-06 error
convention. No logger call includes k1, a spendable credential, or the
full request URL (SEC-05).
"""

import json
import re
from hashlib import sha256
from typing import Optional
from urllib.parse import urlparse

import bolt11
from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse
from loguru import logger

from lnbits.core.services.payments import create_invoice as lnbits_create_invoice

from . import derivation, spend
from . import nostr as nostr_module
from .bech32m import decode_cx1, decode_npub, encode_cx1
from .crud import (
    OutputCollisionError,
    PendingNoteError,
    claim_next_index,
    delete_username,
    find_burn,
    get_mint_by_id,
    mark_pending,
    melt_record_exists,
    mint_record_exists,
    next_index_hint,
    note_pending,
    note_record,
    record_melt,
    record_mint_record,
    swap,
    upsert_username,
    username_branch,
    username_nostr_pubkey,
)
from .models import Mint
from .services import (
    _melt_pay,
    _min_sendable_msat,
    _mint_fee_msat,
    _public_base_url,
    _track_melt_end,
    _track_melt_start,
    _try_settle_mint,
    _verify_melt,
    _verify_mint,
    _zaps_offered,
    spend_domains,
)
from .signing import certificate, mint_pubkey, verify_register_signature

lnurlmint_lnurl_router = APIRouter()

# Maximum number of k1s accepted in a single /w/cb callback request —
# prevents a merge with an unbounded number of inputs from exhausting
# resources (upstream's settings.max_k1s).
_MAX_K1S = 100

# A registrable username: lowercase-only (stored already normalized —
# lookups lowercase their own input), short enough for a Lightning
# Address local-part, and deliberately excluding anything 64-hex/bech32m
# would also match — no registered username can ever be confused for a
# k1/comment/p1 value on another endpoint.
_USERNAME_PATTERN = re.compile(r"^[a-z0-9_.-]{1,32}$")

_INVALID_K1 = "Invalid or already spent k1."


def _err(reason: str) -> dict:
    return {"status": "ERROR", "reason": reason}


def _host_of(url: str) -> str:
    return urlparse(url).hostname or ""


def _known_username(username: str, mint: Mint) -> bool:
    """The mint's own fixed identities: its configured username, or `_` —
    the reserved bare-domain name a WALLET/directory queries instead when
    it wants to display just `{host}` (LUD-16). Case-insensitive."""
    normalized = username.lower()
    return normalized == mint.username.lower() or normalized == "_"


async def _registered_username_branch(username: str, mint: Mint) -> Optional[str]:
    """The cx1 hex registered under `username` on this mint, or None —
    unconditionally None while the mint has registration disabled."""
    if not mint.registration_enabled:
        return None
    return await username_branch(mint.id, username.lower())


def _registrable_username(username: str, mint: Mint) -> bool:
    """Syntactically valid AND not one of this mint's own reserved
    identities (its username, the bare-domain `_`)."""
    return bool(_USERNAME_PATTERN.match(username)) and not _known_username(username, mint)


def _owns_branch(
    action: str, domain: str, username: str, branch_hex: str, sig_hex: str
) -> bool:
    """Whether `sig_hex` is a valid ownership-proof Schnorr signature by
    `branch_hex`'s own PURPOSE_WALLET index-0 public key — "the first
    secret" a WALLET derives on a branch — over
    "LNURLcash:<action>:<domain>:<username>". `domain` is this mint's own
    resolved host, so a proof captured by one mint/host can never be
    replayed by it against a different one."""
    branch = bytes.fromhex(branch_hex)
    branch_point, chain_code = branch[:32], branch[32:]
    expected = derivation.derive_pubkey(
        branch_point, chain_code, derivation.PURPOSE_WALLET, 0
    )
    return verify_register_signature(expected, sig_hex, action, domain, username)


# ---------------------------------------------------------------------------
# cx1 registration + NIP-05
# ---------------------------------------------------------------------------


@lnurlmint_lnurl_router.post("/p/{mint_id}/{username}")
async def upsert_registered_username(
    request: Request,
    mint_id: str,
    username: str,
    cx1: str,
    sig: str,
    npub: Optional[str] = None,
) -> dict:
    """LUD-25 cx1 registration: claims `username` for a WALLET's
    watch-only branch export (`cx1<P || chain_code>`), so paying this
    username's lightning address with no comment auto-mints a fresh `cp1`
    note directly on that branch for every payment received.

    `sig` is a required ownership-proof Schnorr signature over
    "LNURLcash:register:<domain>:<username>". A fresh claim proves
    control of the NEW cx1 being submitted; overwriting an
    already-registered name instead proves continued control of the
    branch CURRENTLY on file (a WALLET migrating seeds only needs its
    old one long enough to sign this once).

    `npub`, if given, doubles `username` as a NIP-05 name (see
    get_nip05); rejected outright while the mint has nip05_enabled off.
    Omitted on an overwrite, any previously registered npub is cleared."""
    mint = await get_mint_by_id(mint_id)
    if mint is None or not mint.registration_enabled:
        return _err("Not found")
    username = username.lower()
    if not _registrable_username(username, mint):
        return _err("Invalid or reserved username.")
    branch = decode_cx1(cx1)
    if branch is None:
        return _err("Invalid cx1.")
    nostr_pubkey_hex: Optional[str] = None
    if npub is not None:
        if not mint.nip05_enabled:
            return _err("npub registration (NIP-05) is disabled on this mint.")
        decoded_npub = decode_npub(npub)
        if decoded_npub is None:
            return _err("Invalid npub.")
        nostr_pubkey_hex = decoded_npub.hex()
    existing_branch_hex = await username_branch(mint.id, username)
    # a fresh claim proves control of the NEW branch; an overwrite proves
    # continued control of whichever branch is CURRENTLY on file
    proof_branch_hex = existing_branch_hex if existing_branch_hex is not None else branch.hex()
    domain = _host_of(_public_base_url(request, mint))
    if not _owns_branch("register", domain, username, proof_branch_hex, sig):
        return _err("Invalid ownership signature.")
    await upsert_username(mint.id, username, branch.hex(), nostr_pubkey_hex)
    return {"status": "OK"}


@lnurlmint_lnurl_router.delete("/p/{mint_id}/{username}")
async def delete_registered_username(
    request: Request, mint_id: str, username: str, sig: str
) -> dict:
    """Frees `username` entirely — back to unclaimed,
    first-come-first-served. `sig` proves control of the branch CURRENTLY
    on file, over "LNURLcash:unregister:<domain>:<username>"."""
    mint = await get_mint_by_id(mint_id)
    if mint is None or not mint.registration_enabled:
        return _err("Not found")
    username = username.lower()
    existing_branch_hex = await username_branch(mint.id, username)
    if existing_branch_hex is None:
        return _err("Unknown user.")
    domain = _host_of(_public_base_url(request, mint))
    if not _owns_branch("unregister", domain, username, existing_branch_hex, sig):
        return _err("Invalid ownership signature.")
    await delete_username(mint.id, username)
    return {"status": "OK"}


@lnurlmint_lnurl_router.get("/nostr.json/{mint_id}")
async def get_nip05(mint_id: str, name: Optional[str] = None) -> dict:
    """NIP-05: a registered username that supplied an npub resolves as a
    Nostr identifier. Answers only the one `name` asked about (never the
    whole directory), echoes it verbatim as the map key. An unregistered
    or npub-less name comes back as an empty map — NIP-05's own
    "not found", not an error."""
    mint = await get_mint_by_id(mint_id)
    if mint is None or not mint.nip05_enabled:
        return _err("Not found")
    pubkey_hex = (
        await username_nostr_pubkey(mint.id, name.lower())
        if name is not None and mint.registration_enabled
        else None
    )
    return {"names": {name: pubkey_hex} if pubkey_hex is not None else {}}


# ---------------------------------------------------------------------------
# LUD-06 payRequest + callbacks
# ---------------------------------------------------------------------------


@lnurlmint_lnurl_router.get("/lnurlp/{mint_id}")
async def get_payrequest(mint_id: str, request: Request) -> dict:
    """LUD-06 payRequest for the mint's own fixed identity — advertises
    the mint flow with fee-aware bounds, `withdrawLink` to the
    informational /w endpoint, and `commentAllowed` (mandatory on the
    callback: a `cp1<Q>` or bearer `h` names the note to credit)."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")
    if mint.sunset_mint:
        return _err("This mint is sunsetting - minting is disabled.")

    base = _public_base_url(request, mint)
    host = _host_of(base)

    metadata_entries = [
        ["text/plain", f"Mint an lnurlcash bearer note on {mint.username}"],
        ["text/identifier", f"{mint.username}@{host}"],
    ]
    if mint.base_fee_msat or mint.fee_percent_ppm:
        metadata_entries.append(
            ["text/plain", f"Mint fees: {mint.base_fee_msat},{mint.fee_percent_ppm}"]
        )

    return {
        "tag": "payRequest",
        "callback": f"{base}/lnurlmint/p/cb/{mint_id}",
        "minSendable": _min_sendable_msat(mint),
        "maxSendable": mint.max_sendable_msat,
        "metadata": json.dumps(metadata_entries),
        "withdrawLink": f"{base}/lnurlmint/w/{mint_id}",
        "commentAllowed": 64,
    }


@lnurlmint_lnurl_router.get("/lnurlp/{mint_id}/{username}")
async def get_payrequest_for_username(
    mint_id: str, username: str, request: Request
) -> dict:
    """LUD-06 payRequest for a cx1-registered username — its callback is
    its own path (/p/{mint_id}/{username}), and its metadata additionally
    carries `["text/cpub", "<cx1>:<i>"]` (LUD-25 Internal transfer: this
    mint's best-known next-unused index on the branch — a payer holding a
    note on this mint can derive pk_i itself and name it as p1/p2 on an
    ordinary rotate/split/merge, skipping Lightning entirely)."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")
    if _known_username(username, mint):
        # the mint's own fixed identity (or the bare-domain "_") answers
        # the same way regardless of case
        return await get_payrequest(mint_id, request)
    branch_hex = await _registered_username_branch(username, mint)
    if branch_hex is None:
        return _err("Unknown user.")

    base = _public_base_url(request, mint)
    host = _host_of(base)
    index_hint = await next_index_hint(mint.id, username.lower()) or 0
    metadata_entries = [
        ["text/plain", f"Mint an lnurlcash bearer note on {host}"],
        ["text/identifier", f"{username}@{host}"],
        ["text/cpub", f"{encode_cx1(bytes.fromhex(branch_hex))}:{index_hint}"],
    ]
    if mint.base_fee_msat or mint.fee_percent_ppm:
        metadata_entries.append(
            ["text/plain", f"Mint fees: {mint.base_fee_msat},{mint.fee_percent_ppm}"]
        )
    resp = {
        "tag": "payRequest",
        "callback": f"{base}/lnurlmint/p/{mint_id}/{username}",
        "minSendable": _min_sendable_msat(mint),
        "maxSendable": mint.max_sendable_msat,
        "metadata": json.dumps(metadata_entries),
        "withdrawLink": f"{base}/lnurlmint/w/{mint_id}",
        "commentAllowed": 64,
    }
    # NIP-57: a registered username can be zapped — the note lands on its
    # own branch with no comment needed, and the receipt is what tells
    # the zapper it landed. The fixed identity has no branch to land on.
    if _zaps_offered(mint):
        resp["allowsNostr"] = True
        resp["nostrPubkey"] = nostr_module.pubkey_of(mint.mint_privkey)
    return resp


async def _pay_callback(
    request: Request,
    mint: Mint,
    amount: int,
    comment: Optional[str],
    nostr: Optional[str],
    username: Optional[str],
    branch: Optional[bytes],
) -> dict:
    """Shared LUD-06 callback: returns an invoice whose settlement mints
    a note worth `amount` minus the mint fee under `note_id` — the
    WALLET's `comment` (a `cp1<Q>` or bearer `h`, mandatory for the fixed
    identity) or, for a registered username, the next unused branch key
    this mint derives itself when the comment names no output.

    `nostr` (NIP-57) is a kind-9734 zap request, only taken for a
    registered username on a zap-enabled mint: validated per the NIP's
    Appendix D, the invoice is bound to it by description hash, and the
    request kept for the receipt published once it settles."""
    if mint.sunset_mint:
        return _err("This mint is sunsetting - minting is disabled.")
    if amount < mint.min_sendable_msat:
        return _err("Amount too low.")
    if amount > mint.max_sendable_msat:
        return _err("Amount too high.")
    net_amount_msat = amount - _mint_fee_msat(amount, mint)
    if net_amount_msat < mint.min_mint_msat:
        return _err(
            f"Amount too low to mint a note "
            f"(min {mint.min_mint_msat} msat net of fees)."
        )

    zap_request: Optional[str] = None
    if nostr is not None:
        if not _zaps_offered(mint) or branch is None:
            return _err("Zaps are not offered for this address.")
        _, problem = nostr_module.validate_zap_request(nostr, amount)
        if problem is not None:
            return _err(problem)
        zap_request = nostr

    note_id = spend.decode_note_hex(comment) if comment is not None else None
    if note_id is None and branch is not None:
        # registered username: a comment naming no output (an ordinary
        # human LUD-12 message, or none at all — e.g. a zap) doesn't
        # block minting — auto-mint on this username's own branch.
        if username is None:
            return _err("Unknown user.")
        branch_point, chain_code = branch[:32], branch[32:]
        try:
            note_id, _ = await claim_next_index(
                mint.id,
                username,
                lambda i: derivation.derive_pubkey(
                    branch_point, chain_code, derivation.PURPOSE_LIGHTNING_ADDRESS, i
                ).hex(),
            )
        except ValueError:
            return _err("Unknown user.")
    elif note_id is None:
        return _err(
            "Missing or malformed comment: a cp1<Q>, or a bearer note's "
            "hex-encoded 32-byte hash, is required to mint."
        )

    try:
        if zap_request is None:
            payment = await lnbits_create_invoice(
                wallet_id=mint.wallet,
                amount=amount // 1000,  # msat → sat
                memo=f"lnurlcash mint on {mint.username}",
                extra={"lnurlmint": "mint", "mint_id": mint.id},
            )
        else:
            payment = await lnbits_create_invoice(
                wallet_id=mint.wallet,
                amount=amount // 1000,
                memo=f"lnurlcash zap on {mint.username}",
                description_hash=sha256(zap_request.encode()).digest(),
                extra={"lnurlmint": "mint", "mint_id": mint.id, "zap": True},
            )
    except Exception as exc:
        logger.warning(f"lnurlmint: invoice creation failed: {exc}")
        return _err("Error creating invoice")

    pr = payment.bolt11
    payment_hash = payment.payment_hash
    try:
        await record_mint_record(
            payment_hash=payment_hash,
            mint_id=mint.id,
            pr=pr,
            amount_msat=net_amount_msat,
            note_id=note_id,
            zap_request=zap_request,
        )
    except ValueError as exc:
        return _err(str(exc))

    base = _public_base_url(request, mint)
    resp = {"pr": pr, "disposable": False}
    # every mint is comment-protected now (a wallet-chosen note id or a
    # branch-derived key — never the invoice preimage), so advertising
    # verify is always safe when the mint allows it
    if mint.verify_enabled:
        resp["verify"] = f"{base}/lnurlmint/verify/{mint.id}/{payment_hash}"
    logger.debug(f"lnurlmint: recorded pending mint for mint_id={mint.id}")
    return resp


@lnurlmint_lnurl_router.get("/p/cb/{mint_id}")
async def get_pay_callback(
    mint_id: str,
    request: Request,
    amount: int,
    comment: Optional[str] = None,
    nostr: Optional[str] = None,
) -> dict:
    """LUD-06 callback for the mint's own fixed identity. `comment` is
    REQUIRED and must name an output (a `cp1<Q>` or bearer `h`) — the
    payment preimage is never the note's key. Zaps are refused here (no
    branch for the note to land on)."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")
    return await _pay_callback(
        request, mint, amount, comment, nostr, username=None, branch=None
    )


@lnurlmint_lnurl_router.get("/p/{mint_id}/{username}")
async def get_pay_callback_for_username(
    mint_id: str,
    username: str,
    request: Request,
    amount: int,
    comment: Optional[str] = None,
    nostr: Optional[str] = None,
) -> dict:
    """LUD-06 callback for a cx1-registered lightning address — declared
    after /p/cb/{mint_id} so the literal wins route resolution (they are
    different arities, but keep the ordering upstream relies on anyway)."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")
    username = username.lower()
    if _known_username(username, mint):
        # the mint's own fixed identity pays through the fixed callback —
        # comment stays mandatory there
        return await _pay_callback(request, mint, amount, comment, nostr)
    branch_hex = await _registered_username_branch(username, mint)
    if branch_hex is None:
        return _err("Unknown user.")
    return await _pay_callback(
        request, mint, amount, comment, nostr, username=username, branch=bytes.fromhex(branch_hex)
    )


# ---------------------------------------------------------------------------
# LUD-21 verify
# ---------------------------------------------------------------------------


@lnurlmint_lnurl_router.get("/verify/{mint_id}/{payment_hash}")
async def verify_invoice(mint_id: str, payment_hash: str) -> JSONResponse:
    """LUD-21 verify — settlement status for a mint or melt invoice.

    Served only while the mint has verify_enabled on: false disables the
    endpoint entirely (404), not just its advertisement. For a legacy
    no-comment mint (comment_protected=0), the served preimage IS the
    bearer note's spend secret, so verify refuses it (404). For every
    new-protocol mint and all melts, the preimage is harmless and served
    normally, fetched live from LNbits (never cached — SEC-02).

    No logger call includes payment_hash or preimage (SEC-05).
    """
    mint = await get_mint_by_id(mint_id)
    if mint is None or not mint.verify_enabled:
        return JSONResponse(
            status_code=404,
            content={"status": "ERROR", "reason": "Not found"},
        )
    result = await _verify_mint(payment_hash, mint)
    if result is None:
        result = await _verify_melt(payment_hash, mint)
    if result is None:
        return JSONResponse(
            status_code=404,
            content={"status": "ERROR", "reason": "Not found"},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "OK", **result},
    )


# ---------------------------------------------------------------------------
# Note spend resolution (shared by /w and /w/cb)
# ---------------------------------------------------------------------------


class _Rejected(Exception):
    """A k1 that names no note, or doesn't open the one it names.
    `reason` is safe to hand back (see spend.verify)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def _verified_note(
    k1: str, mint: Mint, request: Request
) -> tuple[str, int, bool, bool]:
    """(note id, value, spent, pending) of the note `k1` spends, once the
    spend is verified to open it (spend.verify) — spent notes included,
    so a retried burn can still be recognised. Raises _Rejected
    otherwise: the generic invalid-k1 reason when `k1` is no spend or
    names no note on file, or a script path's own specific reason."""
    parsed = spend.parse(k1)
    if parsed is None:
        raise _Rejected(_INVALID_K1)
    # lazy settlement: the note may exist only as a settled-but-never-
    # polled mint invoice (its record's note_id is the same hex(Q))
    record = await note_record(parsed.note_id, mint.id)
    if record is None:
        await _try_settle_mint(parsed.note_id, mint)
        record = await note_record(parsed.note_id, mint.id)
    if record is None:
        raise _Rejected(_INVALID_K1)
    amount_msat, locked_at, spent_flag, pending_flag = record
    reason = spend.verify(parsed, locked_at, spend_domains(request, mint))
    if reason is not None:
        raise _Rejected(reason)
    return parsed.note_id, amount_msat, spent_flag, pending_flag


async def _note_by_ref(value: str, mint: Mint) -> Optional[str]:
    """The note id `value` names where a `cp1` goes (?p=, and every other
    cp1-shaped input): a `cp1`, or a bearer note's hex `h` (its short
    form). None if `value` is neither."""
    note_id = spend.decode_note_hex(value)
    if note_id is None:
        return None
    if await note_record(note_id, mint.id) is None:
        await _try_settle_mint(note_id, mint)
    return note_id


# ---------------------------------------------------------------------------
# LUD-03 withdrawRequest (informational) + the mutating callback
# ---------------------------------------------------------------------------


@lnurlmint_lnurl_router.get("/w/{mint_id}")
async def get_withdraw(
    mint_id: str,
    request: Request,
    k1: Optional[str] = None,
    p: Optional[str] = None,
    amount: Optional[int] = None,
) -> dict:
    """LUD-03 withdrawRequest for a bearer note — purely informational,
    never burns or alters the note.

    Exactly one of `k1`/`p` must be given. With `k1` — a `ck1`, a `cw1`,
    or a bearer note's hex preimage — the spend is verified in full
    against its note first, so a WALLET checking a received note learns
    whether its spend actually opens it; a script path's own failure
    reason is passed on. `p` (LUD-25 "Checking a note without exposing
    it") is the note's `cp1` or hex `h`, accepted only here, never at
    /w/cb; the response then omits `k1`.

    `amount` is ignored (a note's URL may encode a wallet-declared value
    for offline display; maxWithdrawable is authoritative).

    `mintPubkey` is the mint's signing key and `c` a ready-made `cs1`
    certificate for the note (LUD-25 Offline verification) — a holder
    need not force a rotate just to obtain one, and a certificate isn't
    a spend authorization, so handing one out for `p` never requires
    proof the caller holds the spend."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")

    if (k1 is None) == (p is None):
        return _err("Specify exactly one of k1 or p.")

    if k1 is not None:
        try:
            note_id, amount_msat, spent_flag, _ = await _verified_note(k1, mint, request)
        except _Rejected as exc:
            reason = exc.reason if exc.reason != _INVALID_K1 else "Unknown note."
            return _err(reason)
    else:
        if p is None:
            return _err("Specify exactly one of k1 or p.")
        ref = await _note_by_ref(p, mint)
        record = await note_record(ref, mint.id) if ref is not None else None
        if ref is None or record is None:
            return _err("Unknown note.")
        note_id = ref
        amount_msat, _, spent_flag, _ = record

    # a note names its own spent state: disclosed, while keeping the
    # spend itself off the wire
    if spent_flag:
        return _err("Note already spent.")
    if await note_pending(note_id, mint.id):
        return _err("pending")

    base = _public_base_url(request, mint)
    host = _host_of(base)
    resp = {
        "tag": "withdrawRequest",
        "callback": f"{base}/lnurlmint/w/cb/{mint_id}",
        "minWithdrawable": amount_msat,
        "maxWithdrawable": amount_msat,
        "defaultDescription": f"lnurlcash bearer note on {host}",
        "mintPubkey": mint_pubkey(mint),
        "c": await certificate(note_id, amount_msat, mint),
    }
    if k1 is not None:
        resp["k1"] = k1
    return resp


@lnurlmint_lnurl_router.get("/w/cb/{mint_id}")
async def get_withdraw_callback(
    mint_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    k1: list[str] = Query(...),
    pr: Optional[str] = None,
    amount: Optional[int] = None,
    p1: Optional[str] = None,
    p2: Optional[str] = None,
) -> dict:
    """The lnurlcash redeem callback — melt, rotate, split, merge.

    Every `k1` is a spend (ck1/cw1/hex-preimage) verified in full against
    its note before anything burns. `pr` MUST NOT combine with multiple
    k1s or with `amount`. `p1`/`p2` are the replacement notes' output
    keys (`cp1<Q>` or bearer `h` — this mint never generates one on
    WALLET's behalf): p1 required whenever `pr` is absent, p2 whenever
    `amount` is too.

    - Melt (pr, single k1): reserves the note via mark_pending, records
      the melt invoice, replies {"status":"OK"} immediately, and
      schedules the background tristate settlement (_melt_pay). A `pr`
      naming an invoice this same mint issued is rejected (self-melt),
      as is a payment hash an earlier melt already used.
    - Split (amount, p1+p2): burns all inputs, mints `amount` under p1
      and `change = total - amount - base_fee` under p2.
    - Rotate/merge (no amount): burns all inputs, mints one note worth
      sum + (n-1)*base_fee refund under p1.

    LUD-25 "Retrying a mutation": a rotate/split/merge whose burned notes
    and p1/p2/amount exactly match an earlier completed one gets that
    same result replayed (c/c2 recomputed deterministically) instead of
    "already spent" (see find_burn).

    No logger call includes k1, pr, p1, p2, request.url, or any query
    string (SEC-05)."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")

    if len(k1) > _MAX_K1S:
        return _err(f"Too many k1s (max {_MAX_K1S}).")

    if pr is not None and (len(k1) > 1 or amount is not None):
        return _err(
            "pr cannot be combined with multiple k1s or amount - merge or split first."
        )

    # split grows the number of outstanding notes just like a fresh mint
    # — rejected while sunsetting. rotate/merge/melt are unaffected.
    if mint.sunset_mint and amount is not None:
        return _err("This mint is sunsetting - splitting is disabled.")

    # checked before any note is resolved, so an invalid/missing output
    # never burns anything. p1/p2 decode to hex(Q) the same way a mint
    # comment does (cp1, or a bearer note's hex h).
    p1_id: Optional[str] = None
    p2_id: Optional[str] = None
    if pr is None:
        p1_id = spend.decode_note_hex(p1) if p1 is not None else None
        if p1_id is None:
            return _err("missing p1")
        if amount is not None:
            p2_id = spend.decode_note_hex(p2) if p2 is not None else None
            if p2_id is None:
                return _err("missing p2")

    # every k1 is verified against its note once, spent or not — notes of
    # any kind mix freely in one request, per spec
    verified: list[tuple[str, int, bool, bool]] = []
    for note_k1 in k1:
        try:
            verified.append(await _verified_note(note_k1, mint, request))
        except _Rejected as exc:
            return _err(exc.reason)
    note_ids = [note_id for note_id, _, _, _ in verified]

    if pr is None:
        # LUD-25 "Retrying a mutation": an exact replay (same burned set,
        # same p1/p2, same amount) gets the original result again, not
        # "already spent" — matched on note ids (Q), never raw k1s (one
        # note may be opened by more than one valid spend).
        burn = await find_burn(note_ids, mint.id)
        if burn is not None:
            recorded_p1, recorded_p2, amount1_msat, amount2_msat = burn
            recorded_amount = amount1_msat if recorded_p2 is not None else None
            if (
                recorded_p1 == p1_id
                and recorded_p2 == p2_id
                and recorded_amount == amount
            ):
                resp = {"status": "OK"}
                sig = await certificate(recorded_p1, amount1_msat, mint)
                if sig is not None:
                    resp["c"] = sig
                if recorded_p2 is not None and amount2_msat is not None:
                    sig2 = await certificate(recorded_p2, amount2_msat, mint)
                    if sig2 is not None:
                        resp["c2"] = sig2
                return resp

    if any(spent_flag for _, _, spent_flag, _ in verified):
        return _err(_INVALID_K1)
    values = [amount_msat for _, amount_msat, _, _ in verified]
    total_msat = sum(values)

    # --- Melt branch (pr is not None, single k1) ---
    if pr is not None:
        try:
            decoded = bolt11.decode(pr)
        except Exception as exc:
            return _err(f"Invalid invoice: {exc!s}")
        if decoded.amount_msat != total_msat:
            return _err(f"Invoice must be for exactly {total_msat} msat.")

        # self-mint rejection (SEC-06): the mint must not melt into an
        # invoice it issued itself.
        if decoded.has_payment_hash and await mint_record_exists(decoded.payment_hash):
            return _err("Cannot melt into an invoice this mint issued itself.")

        # duplicate-melt rejection (SEC-06): a payment hash an earlier
        # melt already used is never paid into again — the funding source
        # dedupes by payment hash, so the second melt would be confirmed
        # against the FIRST payment and burn its note without funds
        # moving.
        if decoded.has_payment_hash and await melt_record_exists(decoded.payment_hash):
            return _err("Invoice already used by an earlier melt - use a fresh one.")

        # atomically reserve the note (all-or-nothing, mint_id-scoped)
        try:
            await mark_pending(note_ids, decoded.payment_hash, mint.id)
        except PendingNoteError:
            return _err("pending")
        except ValueError as exc:
            return _err(str(exc))

        # register in-flight AFTER mark_pending succeeds, BEFORE the
        # background task (SEC-03 — prevents the reconcile race)
        if decoded.has_payment_hash:
            await _track_melt_start(decoded.payment_hash)
        try:
            if decoded.has_payment_hash:
                await record_melt(
                    decoded.payment_hash, pr, mint.id, ",".join(note_ids), total_msat
                )
            background_tasks.add_task(_melt_pay, note_ids, pr, decoded, mint)
        except Exception:
            if decoded.has_payment_hash:
                await _track_melt_end(decoded.payment_hash)
            raise

        resp = {"status": "OK"}
        if mint.verify_enabled and decoded.has_payment_hash:
            base = _public_base_url(request, mint)
            resp["pr"] = pr
            resp["verify"] = f"{base}/lnurlmint/verify/{mint_id}/{decoded.payment_hash}"
        logger.debug(f"lnurlmint: scheduled melt for mint_id={mint_id}")
        return resp

    # --- Split branch (pr is None, amount is not None) ---
    # base_fee_msat (never fee_percent_ppm — already withheld once, at
    # mint time) comes out of change, not the requested amount, so a
    # holder can't dodge it by splitting into dust.
    try:
        if amount is not None:
            if not 0 < amount < total_msat:
                return _err(f"amount must be between 0 and {total_msat} msat.")
            change_before_fee = total_msat - amount
            if change_before_fee < mint.base_fee_msat:
                return _err("insufficient value")
            change_amount = change_before_fee - mint.base_fee_msat
            if change_amount < 1:
                return _err("insufficient value")
            if p1_id is None or p2_id is None:
                return _err("missing p1/p2")
            await swap(note_ids, [p1_id, p2_id], [amount, change_amount], mint.id)
            resp = {"status": "OK"}
            sig = await certificate(p1_id, amount, mint)
            if sig is not None:
                resp["c"] = sig
            sig2 = await certificate(p2_id, change_amount, mint)
            if sig2 is not None:
                resp["c2"] = sig2
            logger.debug(f"lnurlmint: split for mint_id={mint_id}")
            return resp

        # --- Rotate/merge branch (pr is None, amount is None) ---
        # rotate is a merge of one (refund 0); a merge refunds
        # (n-1)*base_fee — every base fee collected beyond the single one
        # this now-one note should have cost, per LUD-25.
        if p1_id is None:
            return _err("missing p1")
        refund = (len(note_ids) - 1) * mint.base_fee_msat
        merged_amount = total_msat + refund
        await swap(note_ids, [p1_id], [merged_amount], mint.id)
        resp = {"status": "OK"}
        sig = await certificate(p1_id, merged_amount, mint)
        if sig is not None:
            resp["c"] = sig
        logger.debug(f"lnurlmint: rotate/merge for mint_id={mint_id}")
        return resp
    except OutputCollisionError as exc:
        return _err(str(exc))
    except PendingNoteError:
        return _err("pending")
    except ValueError as exc:
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Mint-address discovery (theoretical withdraw-side mirror of the
# payRequest — upstream's /.well-known/lnurlw/{username} alias)
# ---------------------------------------------------------------------------


@lnurlmint_lnurl_router.get("/lnurlw/{mint_id}")
@lnurlmint_lnurl_router.get("/lnurlw/{mint_id}/{username}")
async def get_mint_address(
    mint_id: str, request: Request, username: Optional[str] = None
) -> dict:
    """Informational mint-address alias: node identity/capacity, mint
    bounds, outstanding liability, sunset date, and `payLink` back to the
    payRequest. Answers for the fixed identity (no username, `mint.
    username`, or the reserved `_`) and for registered usernames."""
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return _err("Unknown mint.")
    if username is not None:
        if not _known_username(username, mint) and (
            await _registered_username_branch(username, mint) is None
        ):
            return _err("Unknown user.")
        pay_username = (
            username
            if not _known_username(username, mint)
            else mint.username
        )
    else:
        pay_username = mint.username

    base = _public_base_url(request, mint)
    host = _host_of(base)
    from .crud import outstanding_msat
    from .services import max_mintable_msat
    from .views_api import _public_node_info

    node_info = await _public_node_info()
    resp = {
        "tag": "withdrawRequest",
        "callback": f"{base}/lnurlmint/w/{mint_id}",
        "minWithdrawable": mint.min_mint_msat,
        "maxWithdrawable": max_mintable_msat(mint),
        "defaultDescription": f"lnurlcash bearer note on {host}",
        "mintPubkey": mint_pubkey(mint),
        "payLink": f"{base}/lnurlmint/lnurlp/{mint_id}"
        if _known_username(pay_username, mint)
        else f"{base}/lnurlmint/lnurlp/{mint_id}/{pay_username}",
        "sunsetDate": mint.sunset_date,
        "outstandingNotesMsat": await outstanding_msat(mint.id),
    }
    if node_info:
        resp.update(
            {
                "nodeAlias": node_info.get("alias"),
                "nodeUri": node_info.get("uris", [None])[0]
                if node_info.get("uris")
                else node_info.get("id"),
                "nodeUris": node_info.get("uris"),
                "nodeColor": node_info.get("color"),
                "nodeCapacity": node_info.get("capacity_msat"),
                "nodeNumChannels": node_info.get("num_channels"),
                "nodeNumPeers": node_info.get("num_peers"),
            }
        )
    return resp
