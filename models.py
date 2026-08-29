"""Pydantic v1 models for the lnurlmint extension.

All models use pydantic v1 syntax (BaseModel, validator, root_validator,
class Config) — LNbits pins pydantic 1.10.26.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, root_validator, validator


def _parse_created_at(v):
    """Accept date-only strings (YYYY-MM-DD) and naive datetimes as UTC.

    The DB stores full timestamps (strftime('%s', 'now') on SQLite,
    now() on Postgres), but tests and API callers may pass date-only
    strings — normalize them to timezone-aware datetimes.
    """
    if v is None or isinstance(v, datetime):
        return v
    if isinstance(v, str):
        if len(v) == 10 and v.count("-") == 2:
            return datetime.fromisoformat(v + "T00:00:00+00:00")
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return v


class Mint(BaseModel):
    """DB row model for lnurlmint.mints — all fields match table columns."""

    id: str
    wallet: str
    username: str
    base_url: str = ""
    onion_url: Optional[str] = None
    base_fee_msat: int = 0
    fee_percent_ppm: int = 0
    min_sendable_msat: int = 1000
    max_sendable_msat: int = 1_000_000_000
    min_mint_msat: int = 10_000
    verify_enabled: bool = True
    sunset_mint: bool = False
    mint_privkey: str
    created_at: datetime
    updated_at: datetime


class MintResponse(BaseModel):
    """API response model — mint config without the signing key.

    The `mint_privkey` (secp256k1 private signing key) is never included
    in API responses. It must never leave the server after creation;
    leaking it allows forging mint signatures (C-01).
    """

    id: str
    wallet: str
    username: str
    base_url: str = ""
    onion_url: Optional[str] = None
    base_fee_msat: int = 0
    fee_percent_ppm: int = 0
    min_sendable_msat: int = 1000
    max_sendable_msat: int = 1_000_000_000
    min_mint_msat: int = 10_000
    verify_enabled: bool = True
    sunset_mint: bool = False
    created_at: datetime
    updated_at: datetime


class CreateMint(BaseModel):
    """API request body for creating a mint.

    Server-generated fields (id, wallet, mint_privkey, timestamps) are
    not accepted from the client.
    """

    username: str
    base_fee_msat: int = Field(0, ge=0)
    fee_percent_ppm: int = Field(0, ge=0, le=100_000)
    min_sendable_msat: int = Field(1000, ge=1)
    max_sendable_msat: int = Field(1_000_000_000, ge=1)
    min_mint_msat: int = Field(10_000, ge=0)
    verify_enabled: bool = True
    sunset_mint: bool = False
    base_url: str = ""
    onion_url: Optional[str] = None

    @root_validator
    def _sendable_bounds_ordered(cls, values):
        if values.get("min_sendable_msat", 0) > values.get("max_sendable_msat", 0):
            raise ValueError("min_sendable_msat must be <= max_sendable_msat")
        return values


class UpdateMint(BaseModel):
    """API request body for partially updating a mint config.

    All fields are Optional so partial updates work (only provided,
    non-None fields are applied). Immutable/server-generated fields
    (id, wallet, mint_privkey, created_at, updated_at) are excluded —
    only the 10 configurable parameters from CreateMint are updatable.
    """

    username: Optional[str] = None
    base_fee_msat: Optional[int] = Field(None, ge=0)
    fee_percent_ppm: Optional[int] = Field(None, ge=0, le=100_000)
    min_sendable_msat: Optional[int] = Field(None, ge=1)
    max_sendable_msat: Optional[int] = Field(None, ge=1)
    min_mint_msat: Optional[int] = Field(None, ge=0)
    verify_enabled: Optional[bool] = None
    sunset_mint: Optional[bool] = None
    base_url: Optional[str] = None
    onion_url: Optional[str] = None

    @root_validator
    def _sendable_bounds_ordered(cls, values):
        # Only validate when both bounds are explicitly provided.
        min_s = values.get("min_sendable_msat")
        max_s = values.get("max_sendable_msat")
        if min_s is not None and max_s is not None and min_s > max_s:
            raise ValueError("min_sendable_msat must be <= max_sendable_msat")
        return values


class Note(BaseModel):
    """DB row model for lnurlmint.notes — a bearer note.

    Fields match the m002_notes_records_melts migration columns exactly.
    `id` is sha256(k1) hex (never the spendable credential). `spent`/`pending`
    are the confirm-before-burn state flags. LNbits' dict_to_model
    converts the stored INTEGER 0/1 to bool, matching the Mint model's
    verify_enabled/sunset_mint pattern from Plan 01.
    """

    id: str
    mint_id: str
    amount_msat: int
    spent: bool
    pending: bool
    pending_payment_hash: Optional[str] = None
    comment_hash: Optional[str] = None
    created_at: datetime

    @validator("created_at", pre=True)
    def _parse_created_at(cls, v):
        return _parse_created_at(v)

    @property
    def state(self) -> str:
        """Human-readable note state for the confirm-before-burn machine.

        - 'spent'    — burned for good (positive melt settlement)
        - 'pending'  — a melt is in flight (reserved, not yet confirmed)
        - 'outstanding' — freely spendable
        """
        if self.spent:
            return "spent"
        if self.pending:
            return "pending"
        return "outstanding"


class MintRecord(BaseModel):
    """DB row model for lnurlmint.mints_records — a pending mint.

    A mint invoice awaiting settlement. `minted` is the compare-and-set
    flag (UPDATE ... WHERE minted=0 + rowcount==1) that makes lazy
    settlement materialization race-safe.
    """

    payment_hash: str
    mint_id: str
    pr: str
    amount_msat: int
    minted: bool
    comment_hash: Optional[str] = None
    created_at: datetime

    @validator("created_at", pre=True)
    def _parse_created_at(cls, v):
        return _parse_created_at(v)


class MeltRecord(BaseModel):
    """DB row model for lnurlmint.melts — a pending/settled melt.

    `settled` flags positive settlement (burn confirmed). `note_ids`
    records which notes were burned (comma-separated ids).
    """

    payment_hash: str
    mint_id: str
    note_ids: Optional[str] = None
    amount_msat: int
    pr: str
    settled: bool
    created_at: datetime

    @validator("created_at", pre=True)
    def _parse_created_at(cls, v):
        return _parse_created_at(v)


# ---------------------------------------------------------------------------
# LNURL wire models (Phase 2 — LUD-06 payRequest + LUD-03 withdrawRequest)
#
# These pydantic v1 models serialize the JSON responses returned by the
# LNURL endpoints (Plans 02-03). They use Literal tag fields for protocol
# conformance (LUD-06 "payRequest", LUD-03 "withdrawRequest"). No model
# carries a spendable credential — only hashes and public values.
# ---------------------------------------------------------------------------


class LnurlPayResponse(BaseModel):
    """LUD-06 payRequest response — advertises the mint flow.

    Returned by GET /lnurlmint/lnurlp/{mint_id}. The wallet fetches an
    invoice from the callback URL to mint a bearer note. `withdrawLink`
    points to the informational /w endpoint so the wallet can learn the
    withdraw flow after paying. `commentAllowed` is the max comment
    length (LUD-12), defaulting to 64.
    """

    tag: Literal["payRequest"] = "payRequest"
    callback: str
    minSendable: int
    maxSendable: int
    metadata: str
    withdrawLink: str
    commentAllowed: int = 64


class LnurlPayActionResponse(BaseModel):
    """LUD-06 payRequest callback response — the invoice to pay.

    Returned by GET /lnurlmint/p/cb/{mint_id}. `pr` is the BOLT-11
    invoice. `disposable` is always False (the invoice can be retried).
    `verify` is the verify URL (LUD-21), included only for
    comment-protected mints (Phase 4).
    """

    pr: str
    disposable: Literal[False] = False
    verify: Optional[str] = None


class LnurlPayVerifyResponse(BaseModel):
    """LUD-21 verify response — settlement status for a mint or melt invoice.

    Returned by GET /lnurlmint/verify/{mint_id}/{payment_hash}. `settled`
    reports whether the invoice/payment has settled. `preimage` is the
    hex-encoded preimage, fetched live from the funding source (never
    cached — SEC-02), included only when settled and the preimage is
    available. For a no-comment mint, `preimage` IS the bearer note's
    spend secret — verify refuses to serve it (404) for those mints. For
    a comment-protected mint, the preimage redeems nothing (the
    WALLET-held secret behind `comment` is the note's key), so it's
    served safely. For a melt, the preimage is the outgoing payment's
    proof — harmless (the notes that funded it are already burned). `pr`
    is the BOLT-11 invoice.
    """

    status: Literal["OK"] = "OK"
    settled: bool
    preimage: Optional[str] = None
    pr: str


class LnurlWithdrawResponse(BaseModel):
    """LUD-03 withdrawRequest response — advertises the melt/redeem flow.

    Returned by GET /lnurlmint/w/{mint_id}. Purely informational: tells
    the wallet the callback URL, the k1 (note hash, never the spendable
    credential), and the min/max withdrawable amounts. `mintPubkey` is
    the mint's secp256k1 public key for offline verification (Phase 5).
    """

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: str
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: Optional[str] = None


class WithdrawSuccessResponse(BaseModel):
    """LUD-03 withdraw callback success response.

    Returned by GET /lnurlmint/w/cb/{mint_id} after accepting a melt.
    `status` is always "OK". `sig`/`sig2` are recoverable signatures
    over the new note(s) for offline verification (Phase 5), included
    only on rotate/split/merge — melt returns just `{"status":"OK"}`.
    """

    status: Literal["OK"] = "OK"
    sig: Optional[str] = None
    sig2: Optional[str] = None
