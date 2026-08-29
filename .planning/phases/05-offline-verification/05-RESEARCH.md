# Phase 05 Research: Offline Verification

**Researched:** 2026-08-29
**Status:** Complete — 2 plans ready

## Summary

Phase 5 ports LUD-25 offline verification from the standalone `lnurl-mint` into
the LNbits extension, using **Option B (per-mint secp256k1 keypair)** instead of
the source's Option A (node `signmessage`). Each mint already has a `mint_privkey`
stored since Phase 1; this phase derives `mintPubkey` from it, advertises it on
`/w`, signs rotate/split/merge notes with a recoverable ECDSA signature, and
ports `test_offline_verification.py` against LNbits fixtures.

The key architectural shift: the source's `signing.py` calls
`node.sign_message` (lnd/cln signmessage RPC, which wraps the message with the
`Lightning Signed Message:` prefix and double-sha256s). The port uses coincurve's
`PrivateKey.sign_recoverable` directly over `sha256(LNURLcash:<amount>:<h>)` —
no Lightning prefix, no node RPC. `verify_note` recovers the pubkey via
`PublicKey.from_signature_and_message` and compares to `mintPubkey`.

---

## Key Finding: coincurve Is Available + API Confirmed

`coincurve` is a transitive dependency already imported by LNbits core:
- `lnbits/wallets/nwc.py` line 11: `from coincurve import PrivateKey, PublicKey`
- `lnbits/utils/nostr.py` uses `coincurve.PublicKey` / `PublicKeyXOnly`

Both required methods are available in the LNbits venv (verified at runtime):

```
$ .venv/bin/python -c "from coincurve import PrivateKey, PublicKey; ..."
sign_recoverable sig: (self, message, hasher=sha256, ...) -> bytes
from_signature_and_message sig: (signature, message, hasher=sha256, ...) -> PublicKey
```

Round-trip verified: `PrivateKey().sign_recoverable(msg)` → 65 bytes →
`PublicKey.from_signature_and_message(sig, msg).format(compressed=True).hex()`
matches `pk.public_key.format(compressed=True).hex()`. **No new dependency.**

---

## Key Finding: mint_privkey Already Populated (No Migration Needed)

The `mint_privkey TEXT NOT NULL` column exists in `m001_initial` (migrations.py
line 30) and `_generate_mint_privkey()` (crud.py line 39) populates it at mint
creation via `coincurve.PrivateKey().secret.hex()`. Every mint row already has
a valid 64-char hex secp256k1 private key. **No Phase 5 migration is required.**

Evidence:
- `migrations.py` line 30: `mint_privkey TEXT NOT NULL`
- `crud.py` lines 39-49: `_generate_mint_privkey()` returns
  `PrivateKey().secret.hex()` (64-char hex)
- `models.py` line 50: `Mint.mint_privkey: str`
- `models.py` line 55-76: `MintResponse` correctly OMITS `mint_privkey` (C-01)

---

## Research Topic 1: signing.py Module (New File)

**Source:** `lnurl-mint/lnurl_mint/signing.py` (93 lines) — uses Option A.

The source's `signing.py`:
- `_message(note_id_hex, amount_msat)` → `f"LNURLcash:{amount_msat}:{note_id_hex}"`
  (line 19-27). **This format is reused verbatim in the port.**
- `mint_pubkey(config)` → fetches node info, returns `info.uri.split("@")[0]`
  (the node identity pubkey). **Replaced** by derivation from `mint.mint_privkey`.
- `sign_note(note_id_hex, amount_msat, config)` → calls
  `sign_message(_message(...), config)` (node signmessage RPC), reorders the
  returned bytes into r ‖ s ‖ recovery-id, hex-encodes. **Replaced** by
  `PrivateKey.sign_recoverable` (already returns r ‖ s ‖ recovery-id).
- `verify_note(pubkey_hex, note_id_hex, amount_msat, signature_hex)` →
  reconstructs the "Lightning Signed Message" digest
  (`sha256(sha256(prefix + message))`), recovers the pubkey via
  `PublicKey.from_signature_and_message(sig, digest, hasher=None)`, compares.
  **Adapted**: no Lightning prefix; use coincurve's default sha256 hasher.

**Port `signing.py` design:**

```python
from hashlib import sha256
from typing import Optional
from coincurve import PrivateKey, PublicKey
from .models import Mint

_DOMAIN_TAG = "LNURLcash"

def _message(note_id_hex: str, amount_msat: int) -> bytes:
    return f"{_DOMAIN_TAG}:{amount_msat}:{note_id_hex}".encode()

def mint_pubkey(mint: Mint) -> Optional[str]:
    """Derive the mint's secp256k1 public key (compressed, hex) from
    mint.mint_privkey. Returns None if privkey is absent/invalid."""
    if not mint.mint_privkey:
        return None
    try:
        return PublicKey.from_secret_key(
            bytes.fromhex(mint.mint_privkey)
        ).format(compressed=True).hex()
    except Exception:
        return None

async def sign_note(h: str, amount_msat: int, mint: Mint) -> Optional[str]:
    """Recoverable ECDSA sig over sha256(LNURLcash:<amount>:<h>) using the
    mint's privkey. 65 bytes (r‖s‖recovery-id), hex. None on failure (never
    raises)."""
    if not mint.mint_privkey:
        return None
    try:
        pk = PrivateKey(bytes.fromhex(mint.mint_privkey))
        return pk.sign_recoverable(_message(h, amount_msat)).hex()
    except Exception as exc:
        logger.warning(f"sign_note: signing failed: {exc}")
        return None

def verify_note(mint_pubkey_hex: str, h: str, amount_msat: int,
                signature_hex: str) -> bool:
    """Test-only: recover pubkey from sig+message, compare to mint_pubkey."""
    try:
        sig = bytes.fromhex(signature_hex)
        recovered = PublicKey.from_signature_and_message(
            sig, _message(h, amount_msat)
        )
        return recovered.format(compressed=True).hex() == mint_pubkey_hex
    except Exception:
        return False
```

**Why no Lightning Signed Message prefix:** Option B signs with the mint's own
keypair via coincurve, not the node's signmessage RPC. The prefix/double-hash
scheme exists because lnd/cln's signmessage applies it internally; a wallet
verifying a signmessage signature reconstructs that exact digest. Since the port
does not use signmessage, the wallet-side verifier (`verify_note`) uses the same
plain `sha256(message)` that coincurve applies by default. The message content
(`LNURLcash:<amount>:<h>`) is identical to the source — only the digest wrapper
differs.

---

## Research Topic 2: Replace sign_note Stub in services.py

**Current state** (services.py lines 509-532): a stub
`async def sign_note(h, amount_msat, mint) -> None: return None` with a
docstring noting "Phase 5 implements real signing."

**Call sites** (views_lnurl.py):
- Line 49-50: `from .services import sign_note`
- Line 501-502 (split): `await sign_note(h, amount, mint)` /
  `await sign_note(h2, change_amount, mint)` — **return value discarded**
- Line 521 (rotate/merge): `await sign_note(h, merged_amount, mint)` —
  **return value discarded**

**Plan:** Replace the stub body with a re-export from `signing.py`:
```python
from .signing import sign_note  # re-export (Phase 5)
```
The `async` signature is preserved (signing.py's `sign_note` is `async`), so the
`await sign_note(...)` call sites need no change for the import. Plan 05-02
captures the return value and includes it in the response.

**Why re-export rather than move the call sites to import from signing.py
directly:** Minimizes the diff — `views_lnurl.py` already imports `sign_note`
from `.services`. Keeping that import stable means Plan 05-01 (signing.py +
mintPubkey) and Plan 05-02 (sig/sig2 in response) are independently testable.

---

## Research Topic 3: mintPubkey Derivation

`PublicKey.from_secret_key(bytes.fromhex(mint.mint_privkey)).format(compressed=True).hex()`

- Compressed format (33 bytes → 66 hex chars) matches how the source's
  `node.pubkey` is derived (`identity_key.public_key.format(compressed=True).hex()`
  — source conftest.py line 122).
- Computed each call — a single secp256k1 scalar-mult, negligible vs. the DB
  fetch already done in `/w`. No cache (avoids staleness; v1 never rotates the
  keypair).
- Returns `None` if `mint_privkey` is empty/invalid (defensive — the column is
  `NOT NULL`, but a corrupt row should not 500 the `/w` endpoint).

---

## Research Topic 4: mintPubkey in /w Response

**Current** (views_lnurl.py lines 280-287): `/w` returns a plain dict without
`mintPubkey`:
```python
return {
    "tag": "withdrawRequest",
    "callback": f"{base}/lnurlmint/w/cb/{mint_id}",
    "k1": k1,
    "minWithdrawable": note.amount_msat,
    "maxWithdrawable": note.amount_msat,
    "defaultDescription": f"lnurlcash bearer note on {mint.username}",
}
```

**Plan:** Add `"mintPubkey": mint_pubkey(mint)` to the dict. `mint_pubkey`
returns `Optional[str]`, so the field is `None` when no keypair (which never
happens in practice since `mint_privkey` is `NOT NULL`, but keeps the
`Optional[str]` contract of `LnurlWithdrawResponse.mintPubkey`).

The source advertises `mintPubkey` on `/w` (router.py line 759:
`mintPubkey=await mint_pubkey(...)`) — same placement. The source's docstring
(router.py lines 728-733) explains why `/w` (not `/lnurlp`): a wallet paying the
mint invoice already recovers the node id from the invoice signature, so a
freshly minted note needs no separate field; only rotate/split/merge notes
(obtained via `/w/cb`, no invoice) need `mintPubkey` + `sig`.

---

## Research Topic 5: sig/sig2 in /w/cb Response

**Current** (views_lnurl.py):
- Split (lines 495-504): calls `swap`, then `await sign_note(h, amount, mint)`
  and `await sign_note(h2, change_amount, mint)` (discarded), returns
  `{"status": "OK"}`.
- Rotate/merge (lines 514-523): calls `swap`, then
  `await sign_note(h, merged_amount, mint)` (discarded), returns
  `{"status": "OK"}`.
- Melt (line 448): returns `{"status": "OK"}` (no signing — unchanged).

**Source** (router.py lines 936-952):
- Split: `WithdrawSuccessResponse(sig=await sign_note(h, amount, ...),
  sig2=await sign_note(h2, change_amount, ...))`
- Rotate/merge: `WithdrawSuccessResponse(sig=await sign_note(h, merged_amount, ...))`

**Plan:** Capture the `sign_note` return values and return a
`WithdrawSuccessResponse` (or dict with `status`/`sig`/`sig2`):
- Split: `sig = await sign_note(h, amount, mint)`,
  `sig2 = await sign_note(h2, change_amount, mint)` →
  `WithdrawSuccessResponse(status="OK", sig=sig, sig2=sig2)`
- Rotate/merge: `sig = await sign_note(h, merged_amount, mint)` →
  `WithdrawSuccessResponse(status="OK", sig=sig)`
- Melt: unchanged (`{"status": "OK"}` — no sig).

`WithdrawSuccessResponse` (models.py lines 296-307) already has
`sig: Optional[str] = None` and `sig2: Optional[str] = None`, so a `None` sig
(when signing fails) serializes to `"sig": null` — but the source tests assert
`"sig" not in data` when signing is unavailable. **Decision:** when `sig` is
`None`, omit the key from the response dict (build a dict and conditionally add
`sig`/`sig2`), OR return the model and accept `"sig": null`. The source tests
use `assert "sig" not in data` — so the port must OMIT the key when `None` to
match. **Plan 05-02 builds the response dict conditionally** (only add `sig` if
not None), matching the source's `WithdrawSuccessResponse` serialization
behavior (pydantic v1 with `exclude_none`-style or explicit dict construction).

Actually — pydantic v1 `WithdrawSuccessResponse(status="OK", sig=None)` serializes
to `{"status": "OK", "sig": null, "sig2": null}` by default (includes None
fields). To match the source's `"sig" not in data` assertions, the response must
be built as a plain dict with only the non-None keys:
```python
resp = {"status": "OK"}
if sig is not None:
    resp["sig"] = sig
if sig2 is not None:
    resp["sig2"] = sig2
return resp
```
This matches both the source test assertions and the melt case (`{"status": "OK"}`).

---

## Research Topic 6: Signature Message Format

`LNURLcash:<amount_msat>:<note_id_hex>` — exact format from source
(`signing.py` line 27: `f"{_DOMAIN_TAG}:{amount_msat}:{note_id_hex}"`).

- `amount_msat` — integer msat, the NEW note's value (after swap).
- `note_id_hex` — the `h`/`h2` the WALLET supplied (a 64-char sha256 hex).
  Never the raw secret `k1` — the mint signs the hash, not the credential.

Examples:
- Rotate 5000 msat note to new `h`: `LNURLcash:5000:<h>`
- Split 5000 into 2000 (`h`) + 3000 (`h2`):
  `sig` over `LNURLcash:2000:<h>`, `sig2` over `LNURLcash:3000:<h2>`
- Merge 2000 + 3000 into 5000 (`h`): `sig` over `LNURLcash:5000:<h>`

---

## Research Topic 7: test_offline_verification.py Port

**Source:** `lnurl-mint/tests/test_offline_verification.py` (135 lines, 12 tests).

**Source test → port adaptation map:**

| Source test | Adaptation |
|-------------|------------|
| `test_mint_pubkey_absent_without_a_funding_source` | N/A — Option B always has a keypair. Replace with `test_mint_pubkey_always_present` (mintPrivkey is NOT NULL). |
| `test_signature_absent_without_a_funding_source` | N/A — replace with `test_signature_present_after_rotate` (signing always works with a stored privkey). |
| `test_mint_pubkey_is_the_funding_source_nodes_own_identity` | Adapt → `test_mint_pubkey_matches_derived_pubkey` (mintPubkey == PublicKey.from_secret_key(mint.mint_privkey)). |
| `test_rotate_returns_a_valid_signature` | Port directly — `verify_note(mint_pubkey, h, 5000, data["sig"])`. |
| `test_split_returns_valid_signatures_for_both_notes` | Port directly — verify `sig` over (h, 2000) and `sig2` over (h2, 3000). |
| `test_merge_returns_a_valid_signature` | Port directly — verify `sig` over (h, 5000). |
| `test_melt_carries_no_signature` | Port directly — `data == {"status": "OK"}`. |
| `test_signature_does_not_verify_against_wrong_amount` | Port directly. |
| `test_signature_does_not_verify_against_wrong_k1` | Port directly. |
| `test_signature_does_not_verify_against_wrong_pubkey` | Port directly. |
| `test_signing_failure_is_swallowed_not_raised` | Adapt — monkeypatch `lnurlmint.signing.sign_note` (or the coincurve call) to raise; rotate still returns `{"status": "OK"}` with no `sig`. |
| `test_signing_failure_is_still_logged` | Adapt — assert a warning log with "sign_note". |
| `test_mint_pubkey_failure_is_logged` | Adapt — monkeypatch `mint_pubkey` to raise; `/w` still returns 200 (mintPubkey omitted/None). |

**Test fixture helper:** derive expected mintPubkey in tests via
`PublicKey.from_secret_key(bytes.fromhex(mint.mint_privkey)).format(compressed=True).hex()`.
The `mint_note` fixture returns `(k1, note_id, mint)`, so `mint.mint_privkey` is
available. A `mint_pubkey` helper in conftest (or imported from `signing.py`)
provides the expected value.

**Note on the two "absent" source tests:** Option A (source) makes
`mintPubkey`/`sig` absent when no funding source is configured. Option B (port)
always has a keypair, so those tests are replaced with presence/derivation
tests. The "signing failure swallowed" and "logged" tests ARE ported (they test
the failure path, which is still relevant — a corrupt privkey or coincurve
error).

---

## Research Topic 8: coincurve Availability (Confirmed)

- `coincurve` is imported by `lnbits/wallets/nwc.py` (line 11) and
  `lnbits/utils/nostr.py` — transitive dep, already in LNbits' environment.
- `crud._generate_mint_privkey` (crud.py line 47) already imports
  `from coincurve import PrivateKey` — so coincurve is already used in the
  extension codebase.
- `PublicKey.from_signature_and_message` is available (verified at runtime in
  the LNbits venv) for recovery.
- `PrivateKey.sign_recoverable` returns 65 bytes (r ‖ s ‖ recovery-id) —
  matches the source's post-reorder layout, so no reordering needed.
- **No new dependency added** (EXT-04 preserved).

---

## Dependency / Ordering Notes

- **Plan 05-01** (signing.py + mintPubkey on /w) has no dependency on Plan
  05-02 — `mint_pubkey` and `sign_note` can be added to `signing.py` together,
  but the `/w` advertisement and the `/w/cb` sig inclusion are independent
  edits. Plan 05-01 ships `signing.py` (with `sign_note` + `verify_note` +
  `mint_pubkey`), replaces the `services.sign_note` stub, and wires `mintPubkey`
  into `/w`. Plan 05-02 wires `sig`/`sig2` into `/w/cb` and ports the tests.
- Both plans depend on Phase 3 (rotate/split/merge must exist to sign) —
  already complete.
- No migration needed (mint_privkey column + population since Phase 1).
