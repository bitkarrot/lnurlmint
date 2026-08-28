"""Regression tests for the fee/bounds config validation (2026-08-17 review,
F-4 - originally PoC B2).

Pre-fix, config.py put no upper bound on fee_percent_ppm (plain `int = 0`)
and nothing validated the bounds surface at startup: FEE_PERCENT_PPM >=
1_000_000 made _min_sendable_msat's walk non-terminating (fee >= amount at
every step, so the net can never clear min_mint_msat) - a single env var
turned every GET /p and LUD-16 alias into a permanent 100%-CPU hang. Sibling
gaps: min_sendable > max_sendable (every amount rejects), negative base_fee.

The fix is startup validation (pydantic Field bounds + a model validator),
so a poisonous config fails AT BOOT with a clear ValidationError instead of
hanging the first request. These tests pin the bounds, plus the defensive
iteration cap in _min_sendable_msat itself (a settings object mutated
after construction, as tests do, bypasses pydantic - the cap turns the
hang into a loud error even then).

Ported from the source's test_poc_fee_loop.py, adapting to LNbits models:
CreateMint (not Settings) for bounds validation, _min_sendable_msat(mint)
takes a Mint argument, fee settings updated via update_mint (bypasses
pydantic validation for the iteration cap test). The source's
test_zero_health_check_interval is NOT ported (LNbits has no health check
interval setting).
"""

import pytest
from pydantic import ValidationError

from lnurlmint.models import CreateMint
from lnurlmint.services import _min_sendable_msat, _mint_fee_msat
from lnurlmint.crud import get_mint_by_id, update_mint
from lnurlmint.tests.conftest import TEST_MINT_ID, TEST_WALLET


def test_fee_percent_ppm_at_or_above_100_percent_rejected():
    for ppm in (1_000_000, 1_000_001, 2_000_000):
        with pytest.raises(ValidationError):
            CreateMint(fee_percent_ppm=ppm)


def test_fee_percent_ppm_above_the_practical_bound_is_also_rejected():
    # even 999_999 ppm is legal-terminating in theory but costs ~10M loop
    # iterations of CPU per lnaddress request - the bound sits at 100_000
    # (10%), keeping the _min_sendable_msat walk under ~100 steps
    with pytest.raises(ValidationError):
        CreateMint(fee_percent_ppm=100_001)
    assert CreateMint(username="t", fee_percent_ppm=100_000).fee_percent_ppm == 100_000


def test_negative_fee_values_rejected():
    with pytest.raises(ValidationError):
        CreateMint(fee_percent_ppm=-1)
    with pytest.raises(ValidationError):
        CreateMint(base_fee_msat=-1)


def test_inverted_sendable_bounds_rejected():
    # min > max would reject every /p/cb amount as both too low and too
    # high - caught here, not by a wallet's first attempt
    with pytest.raises(ValidationError):
        CreateMint(min_sendable_msat=2_000_000, max_sendable_msat=1_000_000)
    ok = CreateMint(username="t", min_sendable_msat=1_000_000, max_sendable_msat=1_000_000)
    assert ok.min_sendable_msat == ok.max_sendable_msat


@pytest.mark.anyio
async def test_min_sendable_walk_terminates_under_worst_legal_config(db_setup):
    """The most hostile config that still passes validation: ppm at the
    bound (100_000), a large base fee, and a min_mint far above
    min_sendable - the walk climbs far, but must terminate quickly (well
    under the defensive cap)."""
    await update_mint(
        TEST_MINT_ID, TEST_WALLET,
        fee_percent_ppm=100_000,
        base_fee_msat=1_000_000,
        min_mint_msat=1_000_000,
        min_sendable_msat=10_000,
    )
    mint = await get_mint_by_id(TEST_MINT_ID)
    value = _min_sendable_msat(mint)
    # net of fee at the result clears min_mint, by construction
    fee = _mint_fee_msat(value, mint)
    assert value - fee >= mint.min_mint_msat


@pytest.mark.anyio
async def test_iteration_cap_turns_a_pathological_config_into_a_loud_error(db_setup):
    """Defense in depth: a mint row mutated after construction
    bypasses pydantic validation (update_mint filters against
    _UPDATABLE_FIELDS only, not the UpdateMint model). If fee settings
    ever again make the walk non-terminating, the cap must convert the
    silent 100%-CPU hang into a raised error - proven here by the walk
    RAISING (quickly) rather than hanging."""
    # Bypass validation: update_mint does NOT validate via the UpdateMint
    # pydantic model, so fee_percent_ppm=1_000_000 reaches the DB directly.
    await update_mint(
        TEST_MINT_ID, TEST_WALLET,
        fee_percent_ppm=1_000_000,
        base_fee_msat=0,
        min_mint_msat=10_000,
        min_sendable_msat=10_000,
    )
    mint = await get_mint_by_id(TEST_MINT_ID)
    with pytest.raises(RuntimeError, match="did not terminate"):
        _min_sendable_msat(mint)
