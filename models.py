"""Pydantic v1 models for the lnurlmint extension.

All models use pydantic v1 syntax (BaseModel, validator, root_validator,
class Config) — LNbits pins pydantic 1.10.26.
"""

from datetime import datetime, timezone
from typing import Optional

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
