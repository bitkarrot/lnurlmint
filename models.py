"""Pydantic v1 models for the lnurlmint extension.

All models use pydantic v1 syntax (BaseModel, validator, root_validator,
class Config) — LNbits pins pydantic 1.10.26.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, root_validator


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
