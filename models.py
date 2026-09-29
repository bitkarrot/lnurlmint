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
    sunset_date: Optional[str] = None
    registration_enabled: bool = True
    nip05_enabled: bool = True
    zaps_enabled: bool = False
    zap_relays: str = ""
    mint_privkey: str
    created_at: datetime
    updated_at: datetime


class MintResponse(BaseModel):
    """API response model — mint config without the signing key.

    The `mint_privkey` (secp256k1 private signing key, also the zap
    receipt signing key) is never included in API responses. It must
    never leave the server after creation; leaking it allows forging
    mint signatures (C-01).
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
    sunset_date: Optional[str] = None
    registration_enabled: bool = True
    nip05_enabled: bool = True
    zaps_enabled: bool = False
    zap_relays: str = ""
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
    sunset_date: Optional[str] = None
    registration_enabled: bool = True
    nip05_enabled: bool = True
    zaps_enabled: bool = False
    zap_relays: str = ""
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
    only the configurable parameters from CreateMint are updatable.
    """

    username: Optional[str] = None
    base_fee_msat: Optional[int] = Field(None, ge=0)
    fee_percent_ppm: Optional[int] = Field(None, ge=0, le=100_000)
    min_sendable_msat: Optional[int] = Field(None, ge=1)
    max_sendable_msat: Optional[int] = Field(None, ge=1)
    min_mint_msat: Optional[int] = Field(None, ge=0)
    verify_enabled: Optional[bool] = None
    sunset_mint: Optional[bool] = None
    sunset_date: Optional[str] = None
    registration_enabled: Optional[bool] = None
    nip05_enabled: Optional[bool] = None
    zaps_enabled: Optional[bool] = None
    zap_relays: Optional[str] = None
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

    `id` is hex(Q), the note's BIP-341 taproot output key (never a
    spendable credential - a leaked row reveals value but no spend).
    `locked_at` (unix seconds) is when this mint credited the note -
    where a cw1's relative timelock starts counting. `spent`/`pending`
    are the confirm-before-burn state flags.
    """

    id: str
    mint_id: str
    amount_msat: int
    spent: bool
    pending: bool
    pending_payment_hash: Optional[str] = None
    comment_hash: Optional[str] = None  # legacy column, unused since m004
    locked_at: int = 0
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
    settlement materialization race-safe. `note_id` is hex(Q) of the
    note this mint credits once paid (a wallet-supplied cp1/h, or a
    branch-derived key for a registered lightning address).

    `comment_protected` flags whether the note was keyed by a
    wallet-chosen note id (comment/branch) rather than the invoice's own
    payment hash — gates LUD-21 verify, since only in the legacy
    no-comment case does the served preimage remain the bearer secret.

    `zap_request` is the NIP-57 kind 9734 the invoice was bound to,
    stored base64 (LNbits' value sanitizer strips <...> from raw JSON);
    `zap_receipt` holds the published kind 9735's id once relayed.
    """

    payment_hash: str
    mint_id: str
    pr: str
    amount_msat: int
    minted: bool
    note_id: Optional[str] = None
    comment_protected: bool = True
    zap_request: Optional[str] = None
    zap_receipt: Optional[str] = None
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


class UsernameRegistration(BaseModel):
    """DB row model for lnurlmint.usernames — a cx1 lightning-address
    registration (LUD-25 Seed & derivation).

    `cx1` is hex(P || chain_code), the WALLET's watch-only branch export;
    `next_index` is the best-known next-unused index on its
    PURPOSE_LIGHTNING_ADDRESS counter (also the `text/cpub` hint on the
    payRequest). `nostr_pubkey` is the optional NIP-05 npub, hex-decoded.
    """

    mint_id: str
    username: str
    cx1: str
    nostr_pubkey: Optional[str] = None
    next_index: int = 0
    created_at: datetime

    @validator("created_at", pre=True)
    def _parse_created_at(cls, v):
        return _parse_created_at(v)


# ---------------------------------------------------------------------------
# LNURL wire models — LUD-06 payRequest + LUD-03 withdrawRequest (+ LUD-25)
# ---------------------------------------------------------------------------


class LnurlPayResponse(BaseModel):
    """LUD-06 payRequest, extended with lnurlcash's `withdrawLink`.

    `commentAllowed` (LUD-12) advertises room for LUD-25's comment
    protection: a WALLET attaches `comment=` naming the note to credit —
    a `cp1<Q>` or a bearer note's hex `h`. On the fixed identity's
    callback the comment is mandatory; on a registered username's it is
    optional (the mint auto-mints on the registered cx1 branch).

    `allowsNostr`/`nostrPubkey` (NIP-57) appear only on a registered
    username's payRequest when the mint has zaps enabled.
    """

    tag: Literal["payRequest"] = "payRequest"
    callback: str
    minSendable: int
    maxSendable: int
    metadata: str
    withdrawLink: str
    commentAllowed: int = 64
    allowsNostr: Optional[bool] = None
    nostrPubkey: Optional[str] = None


class LnurlPayActionResponse(BaseModel):
    """LUD-06 payRequest callback response — the invoice to pay.

    `disposable` is always False (LUD-11): the address is a permanent,
    repeatable way to mint fresh notes. `verify` is the LUD-21 verify
    URL, advertised when the mint has verify_enabled.
    """

    pr: str
    disposable: Literal[False] = False
    verify: Optional[str] = None
    routes: list = []


class LnurlPayVerifyResponse(BaseModel):
    """LUD-21 verify response — settlement status for a mint or melt
    invoice. `preimage` is fetched live from LNbits on every call (never
    cached); for comment-protected mints and melts it redeems nothing,
    for a legacy no-comment mint it IS the bearer secret and verify
    refuses it (404)."""

    status: Literal["OK"] = "OK"
    settled: bool
    preimage: Optional[str] = None
    pr: str


class LnurlWithdrawResponse(BaseModel):
    """LUD-03 withdrawRequest response for a single bearer note.

    `k1` echoes the literal spend it was queried with; omitted entirely
    when the note was looked up by `p` (LUD-25 "Checking a note without
    exposing it"). `mintPubkey` is the mint's signing key and `c` a
    ready-made `cs1` certificate for the note (LUD-25 Offline
    verification)."""

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: Optional[str] = None
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: Optional[str] = None
    c: Optional[str] = None


class LnurlMintAddressResponse(BaseModel):
    """Informational withdraw-side mirror of the payRequest identity
    (upstream's theoretical /.well-known/lnurlw alias): this mint's own
    node identity plus the amount bounds a note can fall into, and
    `payLink` back to the pay side. Never a functional way to withdraw —
    a mint custodies bearer notes, not accounts."""

    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: Optional[str] = None
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: Optional[str] = None
    payLink: str
    nodeAlias: Optional[str] = None
    nodeUri: Optional[str] = None
    nodeUris: Optional[list] = None
    nodeColor: Optional[str] = None
    nodeCapacity: Optional[int] = None  # msat
    nodeNumChannels: Optional[int] = None
    nodeNumPeers: Optional[int] = None
    sunsetDate: Optional[str] = None
    outstandingNotesMsat: int = 0


class WithdrawSuccessResponse(BaseModel):
    """LUD-03 withdraw callback success response, extended per LUD-25.

    `c`/`c2` are this mint's Offline-verification certificates (`cs1`)
    over a rotate/split/merge's `p1`/`p2`; `pr`/`verify` echo a melt's
    invoice and its LUD-21-style settlement-proof URL when
    verify_enabled. None fields are excluded on the wire by the
    endpoints (they build plain dicts)."""

    status: Literal["OK"] = "OK"
    c: Optional[str] = None
    c2: Optional[str] = None
    pr: Optional[str] = None
    verify: Optional[str] = None


class RegisterUsernameResponse(BaseModel):
    """LUD-25's cx1 registration response (POST/DELETE
    /p/{mint_id}/{username}) - claims or frees `username` for/from a
    WALLET's watch-only branch."""

    status: Literal["OK"] = "OK"


class Nip05Response(BaseModel):
    """NIP-05's nostr.json shape: `names` maps back only the one name
    actually queried, and only if it has an npub on file — never this
    mint's whole directory."""

    names: dict
