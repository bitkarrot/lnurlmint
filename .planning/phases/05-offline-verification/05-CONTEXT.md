# Phase 05: Offline Verification - Context

**Gathered:** 2026-08-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Each mint signs rotate/split/merge notes with a per-mint secp256k1 keypair,
advertising `mintPubkey` in the withdrawRequest response and returning
`sig`/`sig2` recoverable ECDSA signatures on rotate/split/merge so holders can
verify notes offline without trusting the mint online.

This phase delivers the `signing.py` module (real `sign_note` + test-only
`verify_note`), replaces the `sign_note` stub in `services.py`, advertises
`mintPubkey` in `GET /lnurlmint/w/{mint_id}`, and includes `sig`/`sig2` in the
rotate/split/merge responses from `GET /lnurlmint/w/cb/{mint_id}`. The
`test_offline_verification.py` PoC is ported against LNbits fixtures.

</domain>

<decisions>
## Implementation Decisions

### Option B (per-mint keypair), NOT Option A (node signmessage)
- The source `lnurl-mint/signing.py` uses Option A: `mint_pubkey` fetches the
  funding node's identity pubkey via `fetch_node_info`, and `sign_note` calls
  the node's `signmessage` RPC (lnd/cln). This is rejected for the LNbits port
  because LNbits' `Wallet` abstraction does not expose `signmessage` —
  FakeWallet/VoidWallet have no node identity at all.
- Option B: each mint has its own secp256k1 keypair generated at mint creation
  and stored in `mints.mint_privkey` (already populated since Phase 1 via
  `_generate_mint_privkey` in `crud.py`). `mintPubkey` is the mint's own public
  key (derived from `mint_privkey`), NOT the node's. Tradeoff: holders cannot
  cross-verify `mintPubkey` against the Lightning node pubkey — but the scheme
  is portable across every LNbits backend.

### Signature scheme — coincurve recoverable ECDSA over sha256(message)
- The source wraps the message with the `Lightning Signed Message:` prefix and
  double-sha256s it (because lnd/cln's `signmessage` does that internally).
  Option B does NOT use `signmessage`, so the prefix/double-hash scheme is NOT
  replicated. Instead, `sign_note` uses coincurve's `PrivateKey.sign_recoverable`
  with the default sha256 hasher over `LNURLcash:<amount_msat>:<note_id_hex>`.
- `verify_note` uses coincurve's `PublicKey.from_signature_and_message` with the
  default sha256 hasher to recover the pubkey from the 65-byte (r ‖ s ‖
  recovery-id) signature and compares it to the advertised `mintPubkey`.
- coincurve's `sign_recoverable` returns 65 bytes in r ‖ s ‖ recovery-id order
  (matching raw BOLT-11 signature layout) — no reordering needed (the source
  reordered because lnd's signmessage returns recovery-id-leading bytes).

### mintPubkey derivation — computed each call (cheap, no cache)
- `PublicKey.from_secret_key(bytes.fromhex(mint.mint_privkey)).format(compressed=True).hex()`.
  Computed on each `/w` call. Derivation is a single secp256k1 scalar-mult —
  negligible vs. a DB round-trip. No caching layer needed (avoids staleness if a
  mint's privkey were ever rotated, which v1 does not support anyway).

### sign_note location — new `signing.py` module, services.py re-exports
- A new `lnurlmint/signing.py` module owns `sign_note`, `verify_note`, and a
  `mint_pubkey` derivation helper. `services.py` replaces its stub `sign_note`
  with a thin re-export (`from .signing import sign_note`) so the existing
  import in `views_lnurl.py` (`from .services import sign_note`) keeps working
  with no views change for the import itself.
- `sign_note` is `async` (matches the existing stub signature
  `async def sign_note(h, amount_msat, mint) -> Optional[str]`) even though the
  coincurve call is synchronous — keeping the signature stable avoids touching
  the `await sign_note(...)` call sites in `views_lnurl.py`.

### Signing failures swallowed (return None, never raise)
- `sign_note` wraps the coincurve call in `try/except Exception`, logs a
  warning (so a persistently broken key never looks identical to "offline
  verification just turned off"), and returns `None`. A rotate/split/merge must
  never be blocked by a signing error — the note operation already succeeded
  (swap committed); the signature is a bonus, not a gate.

### sig/sig2 in /w/cb response — return WithdrawSuccessResponse, not plain dict
- The rotate/merge branch currently returns `{"status": "OK"}` (a plain dict).
  It will return `WithdrawSuccessResponse(status="OK", sig=...)` (or a dict with
  `sig`/`sig2` keys) so the signature is included. Melt keeps returning the
  plain `{"status": "OK"}` (no signature on melt — the burned note has no new
  holder to verify).
- Split: `sig = sign_note(h, amount, mint)`, `sig2 = sign_note(h2, change_amount, mint)`.
- Rotate/merge: `sig = sign_note(h, merged_amount, mint)`, no `sig2`.

### Claude's Discretion
- Whether `mint_pubkey` derivation lives in `signing.py` or `crud.py` —
  `signing.py` is the natural home (it's the signing module).
- Whether to return a `WithdrawSuccessResponse` model instance or a plain dict
  with `status`/`sig`/`sig2` keys — either serializes identically; the model is
  preferred for type safety.
- Exact test fixture helper name for deriving the expected mintPubkey in tests.

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`crud._generate_mint_privkey`** (crud.py line 39) — already generates a
  coincurve `PrivateKey().secret.hex()` at mint creation. The `mint_privkey`
  column is `TEXT NOT NULL` in `m001_initial` (migrations.py line 30), so every
  mint row already has a valid 64-char hex privkey.
- **`Mint.mint_privkey`** (models.py line 50) — the DB row model already
  exposes the privkey. `MintResponse` (line 55) correctly OMITS it from API
  responses (C-01: signing key never leaves the server).
- **`LnurlWithdrawResponse.mintPubkey`** (models.py line 293) — already
  `Optional[str] = None`. Plan 05-01 just sets it.
- **`WithdrawSuccessResponse.sig`/`sig2`** (models.py lines 306-307) — already
  `Optional[str] = None`. Plan 05-02 just sets them.
- **`services.sign_note` stub** (services.py line 522) — `async def
  sign_note(h, amount_msat, mint) -> None: return None`. The /w/cb endpoint
  already `await`s it for rotate/split/merge (views_lnurl.py lines 501-502,
  521) but discards the result. Plan 05-02 captures the return value and
  includes it in the response.
- **`views_lnurl.py` /w endpoint** (line 236) — returns a plain dict; needs
  `mintPubkey` added to the response dict.
- **`tests/conftest.py`** — `fresh_secret()` (line 86), `mint_note()` (line
  365, returns `(k1, note_id, mint)`), `FakeNode` (line 94). The ported
  `test_offline_verification.py` reuses these. `mint.mint_privkey` is available
  from the `mint_note` return, so tests derive the expected mintPubkey from it.

### Established Patterns
- **No new dependencies** — `coincurve` is already a transitive LNbits dep
  (used by `lnbits/wallets/nwc.py` line 11, `lnbits/utils/nostr.py`). Both
  `PrivateKey.sign_recoverable` and `PublicKey.from_signature_and_message` are
  available in the LNbits venv (verified).
- **LNURL error format** — `{"status":"ERROR","reason":"..."}` with HTTP 200.
- **No-secret-logging** — `mint_privkey` is a signing key; never logged. The
  warning log on signing failure includes the exception message but NOT the
  privkey or the note hash `h` (h is a WALLET-supplied hash, not a secret, but
  keeping logs clean matches SEC-05 discipline).
- **pydantic v1** — `WithdrawSuccessResponse` already uses `Literal["OK"]`.

### Integration Points
- **`signing.py`** — New file. `sign_note(h, amount_msat, mint) ->
  Optional[str]`, `verify_note(mint_pubkey, h, amount_msat, sig) -> bool`,
  `mint_pubkey(mint) -> Optional[str]`.
- **`services.py`** — Replace the stub `sign_note` with `from .signing import
  sign_note` (re-export). Remove the stub function body.
- **`views_lnurl.py` /w** — Add `mintPubkey` to the response dict (call
  `mint_pubkey(mint)` from `signing.py`).
- **`views_lnurl.py` /w/cb** — Capture `sign_note` return values and include
  `sig`/`sig2` in the rotate/split/merge response (return
  `WithdrawSuccessResponse` or a dict with those keys).
- **`tests/test_offline_verification.py`** — New test file ported from source.

</code_context>

<specifics>
## Specific Ideas

- The message format is exactly `LNURLcash:<amount_msat>:<note_id_hex>` —
  `amount_msat` is the integer msat value of the NEW note (after swap), and
  `note_id_hex` is the `h`/`h2` the WALLET supplied (a sha256 hex, never the raw
  secret). The mint signs exactly what it was given, never a secret it derived.
- `mintPubkey` is advertised on `/w` (the withdrawRequest side), NOT on the
  payRequest side — a wallet paying the mint invoice can already recover the
  mint's node id from the invoice's own signature, so a freshly minted note
  needs no separate field. Only notes obtained via rotate/split/merge (which
  have no invoice) need `mintPubkey` + `sig` for offline verification.
- Melt carries NO signature — the burned note has no new holder to verify
  offline. The melt response stays `{"status": "OK"}` (no `sig`/`sig2`).
- `verify_note` is test-only — the mint never calls it in production. It exists
  so the test suite can confirm `sign_note` produces what the spec's algorithm
  expects (recover the pubkey, compare to `mintPubkey`).
- The source test `test_signing_failure_is_swallowed_not_raised` monkeypatches
  `lnurl_mint.signing.sign_message` to raise. The port monkeypatches
  `lnurlmint.signing.sign_note` (or the coincurve call inside it) to raise —
  the rotate must still return `{"status": "OK"}` with no `sig`.

</specifics>

<deferred>
## Deferred Ideas

- **Cross-verification against the Lightning node pubkey** — Option A
  (signmessage) would let holders verify `mintPubkey` matches the node they
  paid. Rejected for v1 (LNbits Wallet abstraction lacks signmessage); a future
  extension could add an optional node-signmessage path for backends that
  expose it.
- **mint_privkey rotation** — v1 generates the keypair once at mint creation
  and never rotates it. Rotating would invalidate all outstanding signatures.
  Not in scope.
- **TEST-09 / TEST-10** — the full `test_offline_verification.py` port is
  Phase 5 scope (TEST-10 lists it among the remaining tests); the bearer threat
  suite (TEST-09) is Phase 7.

</deferred>
