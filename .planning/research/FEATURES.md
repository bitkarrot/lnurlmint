# Feature Research

**Domain:** LUD-25 lnurlcash mint — Lightning bearer assets as an LNbits extension (`lnurlmint`)
**Researched:** 2026-08-28
**Confidence:** HIGH (source `lnurl-mint` README + `router.py` read in full; reference `giftcards` extension README + `config.json` + `views_api.py`/`services.py`/`tasks.py` heads read)

## Feature Landscape

### Table Stakes (Users Expect These)

Features required for LUD-25 spec conformance or for the extension to be a usable LNbits extension at all. Missing any of these = the mint is not a LUD-25 mint, or the extension is not an LNbits extension.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **Mint (LUD-06 payRequest + `/p/cb` callback)** | Core of LUD-25: a bearer note is minted by paying an invoice whose preimage becomes the note. Without this there is no mint. | MEDIUM | Port from `lnurl-mint`. `/p/cb` generates invoice via LNbits `create_invoice`, stores `payment_hash` + `pr` + `net_amount_msat` via `NoteStore.create_mint`, discards preimage. `disposable: false` (LUD-11). Depends on: per-wallet mint model, LNbits funding abstraction. |
| **Melt (LUD-03 `/w/cb` callback with `pr`)** | Core of LUD-25: redeem a note back to a BOLT-11 payment. Async (respond OK on reserve, pay in background, burn on settle). | HIGH | Port. The confirm-before-burn state machine (`mark_pending` → `_melt_pay` → `finalize_melt`/`restore`/leave-pending) is the highest-risk logic in the whole system — a regression is a funds-loss bug. Depends on: note store, background melt reconciliation, LNbits `pay_invoice`. |
| **Rotate (`/w/cb`, one `k1`, no `pr`, `h`)** | Core LUD-25 transform: burn a note, mint a new one keyed by WALLET-supplied `h`. The minimal "change my secret" operation. | MEDIUM | Port. Requires WALLET to supply `h` (hash of new secret); mint never generates secrets. Carries `sig` if offline verification configured. |
| **Split (`/w/cb`, one+ `k1`, `amount`, `h`+`h2`)** | Core LUD-25 transform: burn note(s), mint two notes (`amount` keyed by `h`, remainder keyed by `h2`). | MEDIUM | Port. `h2` required when `amount` present. Rejected in sunset mode (grows outstanding notes). |
| **Merge (`/w/cb`, many `k1`, `h`)** | Core LUD-25 transform: burn many notes, mint one worth the sum keyed by `h`. | MEDIUM | Port. Allowed in sunset (doesn't grow liability). |
| **Informational `/w` (LUD-03 withdrawRequest, never burns)** | Spec requires the mutating callback to live on a distinct URL from the informational one. `/w` echoes literal `k1`, ignores `amount`, reports `min=max=note value`. | LOW | Port. Also advertises `mintPubkey`. Rejects pending notes with reason `"pending"`. |
| **`h`/`h2` WALLET-supplied secret hashes** | LUD-25: the mint MUST NOT generate replacement-note secrets. `h` required when `pr` absent; `h2` required when `amount` present. Missing → `{"status":"ERROR","reason":"missing h"}`. | LOW | Port. Pure validation; enforces the no-secret-generation invariant. |
| **Store-hashes-not-secrets policy** | LUD-25 security invariant: notes stored keyed by `sha256(k1)`; preimage discarded at invoice-creation; for rotate/split/merge the mint never even sees the secret. | MEDIUM | Port. Maps to LNbits `Database` rows. The DB schema must never persist a spendable secret. |
| **Confirm-before-burn melt discipline** | LUD-25: a melted `k1` MUST NOT be burned until the outgoing payment actually settles. Pending state blocks all other callbacks on that `k1` with reason `"pending"`. | HIGH | Port. The single most security-critical behavior. Must survive the DB-transaction port of the module-level lock. |
| **Async melt (respond OK before paying)** | LUD-03 step 6 / LUD-25: `/w/cb` replies `{"status":"OK"}` on reserve, pays `pr` as a background task. Melt failure is never reported back through the callback. | MEDIUM | Port. Becomes a FastAPI `BackgroundTask` or LNbits `create_permanent_unique_task`-managed coroutine. |
| **Background melt reconciliation** | LUD-25: resolve notes left pending by a crash/restart/unconfirmable outcome. Confirm-before-acting: leave pending if outcome can't be established. | HIGH | Port. Replaces `server.py` lifespan + monitor with `lnurlmint_start`/`lnurlmint_stop` + `create_permanent_unique_task`. In-flight melt tracking (`_in_flight_melts`) must be preserved to avoid the reconcile-inflight double-spend (PoC `test_poc_reconcile_inflight_race.py`). |
| **Per-wallet mint model (multi-tenancy)** | LNbits extension convention: each wallet owns its mint, fees, limits, notes. A single global mint is explicitly rejected (see Anti-Features). | HIGH | New-for-LNbits. `giftcards` demonstrates the pattern: `Database("ext_lnurlmint")`, `wallet_id`-scoped CRUD, `require_admin_key`/`require_invoice_key` decorators. Each mint row = one LNbits wallet's mint instance. |
| **LNbits extension scaffold** | Non-negotiable: `__init__.py` with `lnurlmint_ext` router + `lnurlmint_start`/`lnurlmint_stop` lifecycle, `manifest.json`, `config.json`, static files registration. | LOW | New-for-LNbits. Mirror `giftcards/__init__.py`. `config.json` `min_lnbits_version` should match or exceed giftcards' `1.5.4`. |
| **LNbits `Database` note store (SQLite + Postgres)** | LNbits extensions must use the `Database` abstraction, not standalone sqlite. Replaces `lnurl-mint`'s sqlite + module-level lock. | HIGH | New-for-LNbits. The confirm-before-burn / pending / restore state machine becomes DB-transaction-based. Postgres compatibility is a new constraint `lnurl-mint` never had. |
| **LNbits wallet as funding source (`create_invoice`/`pay_invoice`)** | Replaces `node.py`'s direct lnd/cln REST client. Idiomatic LNbits; per-wallet Lightning. | MEDIUM | New-for-LNbits. `giftcards/services.py` shows `pay_invoice(wallet_id=..., payment_request=...)` and `update_wallet_balance`. The signing primitive (`signmessage`) needs a separate investigation — LNbits' funding node may not expose it the same way (see Offline Verification below). |
| **Mint fees (`BASE_FEE_MSAT` / `FEE_PERCENT_PPM`)** | LUD-25 optional but documented as core: fee withheld at mint time, credited to `k1=P`'s note net of fee. Advertised in LUD-16 metadata as `Mint fees: <base>,<ppm>`. | MEDIUM | Port. Per-mint DB rows replace `config.py` settings. `MIN_MINT_MSAT` net-of-fee floor, fee-aware `minSendable` walk (`_min_sendable_msat`), `max_mintable_msat` ceiling, fee rounded up to whole sat. |
| **Public one-pager frontend (`GET /`)** | The note-holder's face of the mint: mint QR (LNURL of the LUD-16 address), lightning address, mint limits, node info incl. capacity + mempool.space/amboss.space links. | MEDIUM | Port from `frontend.py`. Adapt to LNbits `index_public` generic view + static assets. QR/address generation reuses `giftcards`' `_lnurl_bech32`/`_public_base_url` helpers. |
| **No-secret-logging (disable access logs on withdraw routes)** | LUD-25: a bearer note's `k1` can sit in a withdraw URL far longer than an ephemeral LUD-03 `k1`. `lnurl-mint` disables uvicorn's access log entirely. | LOW | Port. In LNbits context, ensure the extension's routes don't end up in per-request logs that persist query strings; verify LNbits' own logging config doesn't re-enable it. |
| **Full test suite (security PoCs)** | The PoCs encode real funds-loss guarantees (double-spend, race, preimage leak, fee conservation, reconcile-inflight). Non-negotiable per PROJECT.md. | HIGH | Port. ~25 tests adapted to LNbits test fixtures + wallet mocks. This is the acceptance gate for "behavioral parity." |

### Differentiators (Competitive Advantage)

Features that set `lnurlmint` apart from a bare `lnurl-mint` port or from other LNbits bearer-instrument extensions. Not strictly required for LUD-25 conformance, but valuable.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **LUD-21 verify (`/verify/{payment_hash}`) with `VERIFY_ENABLED` off-switch + comment-protection gating** | Lets a nodeless wallet poll settlement status and (for comment-protected mints) receive the preimage. The `VERIFY_ENABLED=false` → 404 (not just unadvertised) is a deliberate security stance rare in the ecosystem. | MEDIUM | Port. **Security tradeoff (observer race):** the payment hash travels inside the invoice, so anyone who sees an unpaid mint invoice can poll `/verify` and, on settle, grab the preimage and rotate — first rotater wins. This is why verify is gated on comment protection for the mint side: with a WALLET-supplied `comment` hash, the preimage redeems nothing. For melts, preimage is never a bearer secret (notes already burned), so verify is always safe there. Table-stakes-adjacent: spec calls it optional, but the gating logic is non-trivial and is a real differentiator in how seriously it takes the preimage-as-secret reality. |
| **LUD-25 comment protection (note keyed by WALLET-supplied hash, not preimage)** | Closes the routing-node preimage race: a WALLET sends a 32-byte hex hash as `comment` (LUD-12), and the resulting note is keyed by that hash instead of the payment preimage. The preimage then redeems nothing. | MEDIUM | Port. `comment` is never rejected for wrong shape (a non-LNURLcash wallet may send an ordinary LUD-12 comment); only a bare 64-hex-char value triggers the protected path. Spec-required for verify-on-mint to be safe. Arguably table-stakes for any mint that enables verify, but optional in the spec overall. |
| **Offline verification (`mintPubkey` + recoverable `sig`/`sig2`)** | A holder verifies a note's issuer and amount without contacting the mint. `mintPubkey` advertised on `/w`; `sig`/`sig2` on rotate/split/merge responses over `h`/`h2`. | HIGH | Port. **Key port risk:** `lnurl-mint` signs via the funding source's `signmessage` RPC (lnd `/v1/signmessage`, cln `signmessage`) — the standard "Lightning Signed Message:" prefix + double-sha256. LNbits' funding node abstraction may not expose `signmessage` the same way; needs research to find an equivalent signing primitive or fall back gracefully (signing failures are swallowed, never block a rotate/split/merge). `mint_pubkey` derivation from node URI must be preserved. |
| **Sunset mode (`SUNSET_MINT`)** | Wind-down: `/p/cb` and split reject (grow outstanding notes); rotate/merge/melt still allowed (don't grow liability, holders can consolidate/redeem). | LOW | Port. Per-mint DB flag instead of env var. Clean operator story for ending a mint without stranding holders. |
| **Tor / `ONION_URL` base substitution** | If a wallet connects via the onion address, callback/QR URLs use `ONION_URL` as base instead of clearnet `BASE_URL` — otherwise a fixed clearnet URL leaks into a Tor visitor's QR and breaks payment over Tor. | MEDIUM | Port. `config.py`'s `public_base_url` Tor-awareness. In LNbits, must integrate with LNbits' own request/base_url handling rather than a standalone `public_base_url`. `giftcards`' `_public_base_url` (X-Forwarded-Host/Proto) is the analogous pattern to study. |
| **Lightning Address (LUD-16) via LNbits `lnurlp` extension** | Mint advertises its payRequest through LNbits' existing address system — no `.well-known/lnurlp/{user}` route conflict with LNbits' built-in lnurlp. | MEDIUM | New-for-LNbits. `lnurl-mint` owns its own `.well-known` routes; the port delegates to `lnurlp`. The mint's payRequest (with `withdrawLink`) must be reachable through LNbits' address resolution. Bare-domain `_@host` convention should be preserved if LNbits' lnurlp supports it. |
| **Management SPA (create/configure mint, view notes)** | Per-wallet mints require a UI: wallet owner creates their mint, sets fees/limits/sunset/verify toggle, views outstanding notes, sees mint activity. | HIGH | New-for-LNbits. `lnurl-mint` has no management UI (single global mint, env-configured). `giftcards` provides the template: Vue 3 + Quasar, `index`/`index_public` generic views, `WalletTypeInfo` + `require_admin_key` decorators, dashboard with filter/search/bulk actions. This is the largest new-for-LNbits surface. |
| **Mint-address endpoint (`/.well-known/lnurlw/{username}`)** | Theoretical/withdraw-side mirror of the LUD-16 address: returns node identity, capacity, amount bounds, `payLink` back to payRequest. Informational only (no `k1`, no balance to draw). | LOW | Port. No LUD number; exists so a wallet resolving the address on its withdraw side learns something useful instead of a 404. May be lower priority given LNbits lnurlp delegation complicates the routing. |
| **Node info caching (`cached_fetch_node_info`, 1h TTL)** | Avoids a fresh getinfo + capacity RPC per page view / mint-address lookup. Failed fetch never cached. | LOW | Port. LNbits may already have node-info caching primitives to reuse; investigate during research. |
| **Capacity from public graph only** | `NodeInfo.capacity` = total publicly-announced channel capacity, never a private/authenticated view. Deliberately leaks no more than the node's public presence already does. | LOW | Port. Requires the funding node to expose a public-graph lookup (lnd `GetNodeInfo`, cln `listchannels` filtered to `source=<own id>`). Verify LNbits' funding abstraction surfaces this; may need a new path. |
| **Fee-aware melt fee limit (`_melt_fee_limit_msat`)** | The routing-fee budget for a melt scales with the mint fee that was charged (meant to cover eventual melt routing cost), floored at 0.5%/5000msat. | LOW | Port. Subtle but important: a high-fee mint gets a correspondingly higher route-finding tolerance. |
| **Bare-domain `_@host` LUD-16 convention** | Reserved `_` username answers alongside the configured username, so a wallet resolving just the bare domain gets the same identity. `text/identifier` echoes whichever name was queried. | LOW | Port. Depends on how LNbits lnurlp handles reserved usernames. |

### Anti-Features (Commonly Requested, Often Problematic)

Things to deliberately NOT build, documented to prevent scope creep.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| **Bundling `lnurl-wallet` (SolidJS holder SPA)** | "One extension does both mint and wallet." | Conflates issuer and holder trust domains; doubles frontend scope (SolidJS vs LNbits' Vue/Quasar); `lnurl-wallet` is mint-agnostic and a separate project. A bundled wallet would also imply the mint extension vouches for a specific holder, undermining the bearer-asset trust model. | Keep `lnurl-wallet` separate. A later phase may *serve* it as an extension sub-route (static hosting only), but v1 is mint-only. Note-holders use any spec-compliant wallet. |
| **Direct lnd/cln REST funding (`node.py`'s own backend client)** | "I want to run the mint against my own node directly, not through LNbits' wallet." | LNbits already abstracts Lightning per-wallet; a second funding path doubles the security-critical surface, diverges from idiomatic extension model, and re-introduces macaroon/rune scoping complexity. Also blocks per-wallet multi-tenancy. | Use LNbits `create_invoice`/`pay_invoice` exclusively. The signing-primitive gap (signmessage) is the one place this bites — solve it inside the LNbits abstraction, not by re-importing `node.py`. |
| **Single global mint (admin-only)** | "Just one mint for the whole node, like `lnurl-mint`." | Breaks LNbits' multi-tenant model; one operator's mint fees/limits/sunset leak into every user; no per-wallet note isolation. | Per-wallet mints. Each LNbits wallet owns its mint row + notes. |
| **Custodial user accounts / per-user balances** | "Users should have an account and balance at the mint." | LUD-25 is bearer notes, not accounts. The mint only ever custodies bearer notes keyed by hashes, never per-user balances. Accounts reintroduce KYC/chargeback/custody concerns and break the offline-circulation property. | Bearer notes only. The mint-address endpoint deliberately carries no `k1` and no balance for exactly this reason. |
| **Serving `.well-known/lnurlp/{user}` / `.well-known/lnurlw/{user}` from the extension** | "The mint should own its address routes like `lnurl-mint` does." | Conflicts with LNbits' built-in `lnurlp` extension which already owns `.well-known/lnurlp/{user}`. Two extensions on the same route = undefined behavior. | Delegate Lightning Address resolution to LNbits `lnurlp`; advertise the mint's payRequest through that system. |
| **Multi-process / multi-worker deployment** | "Scale the mint with `--workers > 1`." | `lnurl-mint` explicitly forbids this: note reservation, burning, and melt reconciliation are coordinated in a single process (module-level lock + in-process background tasks). A second process silently voids those guarantees — double spends. | Single-process per mint. In LNbits this is already the model (one LNbits server process). The DB-transaction port must preserve single-writer semantics for the pending→finalize/restore transition. |
| **Caching preimages locally** | "Speed up `/verify` by caching the preimage." | The preimage IS the bearer note's spend secret (mint side, no-comment). Caching it persists a secret, violating store-hashes-not-secrets and creating a funds-loss vector if the cache leaks. | Fetch preimage live from the funding source on every `/verify` call, never persist. |
| **New Python dependencies** | "Just add `coincurve`/`bolt11`/`bech32` to the extension." | LNbits extension docs (`docs/devs/extensions.md`) forbid adding deps not already in LNbits' `pyproject.toml`. | Verify each `lnurl-mint` dep is already in LNbits before assuming; implement signing with whatever LNbits already ships. |

## Feature Dependencies

```
[Per-wallet mint model]
    └──requires──> [LNbits extension scaffold]
    └──requires──> [LNbits Database note store]

[Mint (LUD-06 payRequest + /p/cb)]
    └──requires──> [Per-wallet mint model]
    └──requires──> [LNbits wallet funding (create_invoice)]
    └──requires──> [Store-hashes-not-secrets policy]
    └──requires──> [Mint fees] (optional but coupled)

[Melt (/w/cb with pr)]
    └──requires──> [Mint] (notes must exist to melt)
    └──requires──> [Confirm-before-burn discipline]
    └──requires──> [Async melt + background reconciliation]
    └──requires──> [LNbits wallet funding (pay_invoice)]

[Rotate / Split / Merge (/w/cb without pr)]
    └──requires──> [Mint]
    └──requires──> [h/h2 WALLET-supplied secret hashes]

[Informational /w]
    └──requires──> [Mint]
    └──enhances──> [Offline verification (advertises mintPubkey)]

[LUD-21 verify (/verify/{payment_hash})]
    └──requires──> [Mint] and/or [Melt]
    └──requires──> [LUD-25 comment protection] (for mint-side verify to be safe)
    └──conflicts──> [No-comment mint] (MUST NOT offer verify for a no-comment mint's payment_hash)

[LUD-25 comment protection]
    └──enhances──> [Mint] (closes preimage race)
    └──enables──> [LUD-21 verify on mint side]

[Offline verification (mintPubkey + sig/sig2)]
    └──requires──> [Rotate/Split/Merge] (sig is on their responses)
    └──requires──> [Signing primitive from funding node] (research item)
    └──enhances──> [Informational /w] (advertises mintPubkey)

[Sunset mode]
    └──requires──> [Per-wallet mint model] (per-mint flag)
    └──affects──> [Mint] and [Split] (rejects both)

[Tor / ONION_URL base substitution]
    └──requires──> [LNbits request/base_url handling]
    └──enhances──> [Public one-pager] and [all callback URL generation]

[Lightning Address via LNbits lnurlp]
    └──requires──> [LNbits lnurlp extension]
    └──requires──> [Mint (payRequest)]
    └──conflicts──> [Serving own .well-known routes] (anti-feature)

[Public one-pager frontend]
    └──requires──> [Mint] (needs limits/address to display)
    └──requires──> [Lightning Address] (QR encodes the address's LNURL)
    └──enhances──> [Tor] (advertises onion alternative)

[Management SPA]
    └──requires──> [Per-wallet mint model] (per-wallet mints need a UI to create/configure)
    └──requires──> [Mint fees] / [Sunset] / [Verify toggle] (config surfaces)

[Full test suite (PoCs)]
    └──requires──> [every above feature] (PoCs assert behavior parity)
```

### Dependency Notes

- **Melt requires the full confirm-before-burn + reconciliation stack:** this is the single most coupled feature. It cannot be shipped incrementally without the background reconciliation task — a pending note left by a crashed melt must be resolvable or it's a permanent funds freeze.
- **LUD-21 verify on the mint side requires comment protection:** the spec (and `router.py` `verify_invoice`) explicitly refuse verify for a no-comment mint's payment_hash, because there the preimage IS the bearer secret. Verify and comment protection are not independent.
- **Offline verification has an open research dependency:** the signing primitive (`signmessage`) that `lnurl-mint` uses via direct lnd/cln REST may not be exposed by LNbits' funding abstraction. If no equivalent exists, offline verification degrades gracefully (signing failures swallowed, `sig` omitted) — but the `mintPubkey` advertisement and the recoverable-sig guarantee are a real differentiator worth solving.
- **Lightning Address delegation conflicts with serving own `.well-known` routes:** these are mutually exclusive. The port must choose delegation (the PROJECT.md decision).
- **Per-wallet mint model is the root new-for-LNbits dependency:** Management SPA, per-mint fees/sunset/verify, and per-wallet note isolation all branch from it. It must land before any of those.

## MVP Definition

### Launch With (v1)

Minimum viable product — full LUD-25 behavioral parity with `lnurl-mint`, in the LNbits extension model.

- [ ] **LNbits extension scaffold** — without this, nothing runs.
- [ ] **Per-wallet mint model + LNbits Database note store** — the foundation every other feature sits on.
- [ ] **LNbits wallet funding (create_invoice/pay_invoice)** — replaces `node.py`.
- [ ] **Mint (LUD-06 payRequest + /p/cb)** — core issuance.
- [ ] **Informational /w** — spec-required distinct-from-callback endpoint.
- [ ] **Melt (/w/cb with pr) + confirm-before-burn + async + background reconciliation** — core redemption; highest risk.
- [ ] **Rotate / Split / Merge (/w/cb without pr) + h/h2** — core transforms.
- [ ] **Store-hashes-not-secrets + no-secret-logging** — security invariants.
- [ ] **Mint fees (BASE_FEE_MSAT/FEE_PERCENT_PPM, MIN_MINT_MSAT, fee-aware bounds)** — revenue model + spec.
- [ ] **LUD-25 comment protection** — required for safe verify; closes preimage race.
- [ ] **LUD-21 verify (VERIFY_ENABLED off-switch + comment gating)** — spec optional but PROJECT.md requires full parity.
- [ ] **Offline verification (mintPubkey + sig/sig2)** — PROJECT.md requires; contingent on signing-primitive research.
- [ ] **Sunset mode** — low-complexity, full-parity.
- [ ] **Tor / ONION_URL base substitution** — full-parity.
- [ ] **Lightning Address via LNbits lnurlp** — the address entry point.
- [ ] **Public one-pager frontend** — the note-holder's face of the mint.
- [ ] **Management SPA** — per-wallet mints require it.
- [ ] **Full test suite (security PoCs)** — the acceptance gate.

### Add After Validation (v1.x)

- [ ] **Mint-address endpoint (`/.well-known/lnurlw/{user}`)** — theoretical, no LUD number; lower value, depends on lnurlp delegation routing.
- [ ] **Node info caching (1h TTL)** — optimization; ship live fetch first, add caching under load.
- [ ] **Serving `lnurl-wallet` as an extension sub-route (static only)** — only after v1 mint is validated; explicitly a later phase.

### Future Consideration (v2+)

- [ ] **Capacity-from-public-graph on LNbits funding abstraction** — if LNbits doesn't expose a public-graph lookup, this degrades to `0` gracefully; revisit if a path emerges.
- [ ] **Bare-domain `_@host` convention** — depends on LNbits lnurlp support for reserved usernames; revisit if lnurlp gains it.

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| LNbits extension scaffold | HIGH | LOW | P1 |
| Per-wallet mint model + DB note store | HIGH | HIGH | P1 |
| LNbits wallet funding | HIGH | MEDIUM | P1 |
| Mint (LUD-06 /p/cb) | HIGH | MEDIUM | P1 |
| Informational /w | HIGH | LOW | P1 |
| Melt + confirm-before-burn + reconcile | HIGH | HIGH | P1 |
| Rotate/Split/Merge + h/h2 | HIGH | MEDIUM | P1 |
| Store-hashes-not-secrets + no-secret-logging | HIGH | MEDIUM | P1 |
| Mint fees | MEDIUM | MEDIUM | P1 |
| LUD-25 comment protection | HIGH | MEDIUM | P1 |
| LUD-21 verify (gated) | MEDIUM | MEDIUM | P1 |
| Offline verification (mintPubkey/sig) | MEDIUM | HIGH | P1 |
| Sunset mode | MEDIUM | LOW | P1 |
| Tor / ONION_URL | MEDIUM | MEDIUM | P1 |
| Lightning Address via lnurlp | HIGH | MEDIUM | P1 |
| Public one-pager frontend | HIGH | MEDIUM | P1 |
| Management SPA | HIGH | HIGH | P1 |
| Full test suite (PoCs) | HIGH | HIGH | P1 |
| Mint-address endpoint | LOW | LOW | P2 |
| Node info caching | LOW | LOW | P2 |
| Capacity from public graph | LOW | LOW | P3 |
| Bare-domain `_@host` | LOW | LOW | P3 |

**Priority key:**
- P1: Must have for launch (full LUD-25 parity per PROJECT.md)
- P2: Should have, add when possible
- P3: Nice to have, future consideration

## Competitor Feature Analysis

| Feature | `lnurl-mint` (source) | `giftcards` (reference LNbits ext) | `lnurlmint` (our approach) |
|---------|------------------------|-----------------------------------|-----------------------------|
| **Bearer instrument creation** | Mint via LUD-06 payRequest; preimage becomes note | Gift card creation: deduct sats from issuer wallet, lock into card with unguessable token | Port `lnurl-mint`'s payRequest mint; fund via LNbits `create_invoice` per-wallet |
| **Redemption / withdraw** | LUD-03 `/w` + `/w/cb` callback (melt/rotate/split/merge) | LNURL-withdraw endpoint (`giftcards_lnurl_router`); recipient scans QR, claims into wallet | Port `lnurl-mint`'s full `/w`+`/w/cb` callback with all four transforms; giftcards' LNURL-withdraw is the single-shot claim analogue |
| **Expiry / sweep** | None — bearer notes don't expire | Expiry sweep task (`wait_for_expiry` via `create_permanent_unique_task`); auto-refund unclaimed sats to issuer | No expiry for bearer notes (anti-feature: notes are perpetual). Reuse the `create_permanent_unique_task` *pattern* for melt reconciliation instead |
| **Email delivery** | None | `deliver` endpoint sends branded card image via email | Out of scope (bearer notes are transferred out-of-band by the holder) |
| **Image rendering** | One-pager QR only | Pillow-based card image rendering (templates, drag-and-drop QR/text, 3x PNG) | One-pager QR only (port `frontend.py`); no gift-card-style image rendering |
| **Bulk creation** | None | CSV upload, same-amount bulk | None (each mint is a single payRequest; bulk minting is a wallet-side concern) |
| **Management UI** | None (env-configured single mint) | Issuer dashboard: filter/search/bulk actions, create dialog, design editor | New Management SPA: create/configure mint (fees/limits/sunset/verify), view outstanding notes — `giftcards` dashboard is the structural template |
| **Funding** | Direct lnd/cln REST (`node.py`) | LNbits `pay_invoice`/`update_wallet_balance` per-wallet | LNbits wallet funding (drop `node.py`); signing primitive is the open research item |
| **Multi-tenancy** | Single global mint | Per-wallet cards (`wallet_id`-scoped CRUD) | Per-wallet mints (each wallet owns a mint row + notes) |
| **Lightning Address** | Owns `.well-known/lnurlp/{user}` | N/A (gift cards use shareable links, not LUD-16) | Delegate to LNbits `lnurlp` extension (avoid route conflict) |
| **Offline verification** | `mintPubkey` + recoverable `sig`/`sig2` via `signmessage` | None | Port; contingent on LNbits funding node exposing a signing primitive |
| **Security model** | Confirm-before-burn, store-hashes-not-secrets, no-secret-logging, observer-race-documented verify | Token-hash redemption, admin/invoice key auth | Port `lnurl-mint`'s full security model; add LNbits `require_admin_key`/`require_invoice_key` for management endpoints |
| **Background tasks** | `server.py` lifespan + monitor for reconcile | `create_permanent_unique_task` for expiry sweep | `create_permanent_unique_task` for melt reconciliation (giftcards pattern, `lnurl-mint` semantics) |
| **DB** | Standalone sqlite + module-level lock | LNbits `Database("ext_giftcards")` | LNbits `Database("ext_lnurlmint")`; confirm-before-burn becomes DB-transaction-based |

### Mapping Summary (giftcards → lnurlmint)

**Analogous (reuse the pattern):**
- Per-wallet CRUD with `wallet_id` scoping → per-wallet mints + notes
- `require_admin_key`/`require_invoice_key` decorators → management API auth
- `create_permanent_unique_task` + `run_interval` → melt reconciliation task (different job, same lifecycle)
- `index`/`index_public` generic views + Vue/Quasar static assets → public one-pager + management SPA
- `_public_base_url` (X-Forwarded-Host/Proto) → Tor-aware base URL handling
- `_lnurl_bech32`/`_lnurl_qr_data` → mint QR encoding on the one-pager
- `config.json` / `manifest.json` / `__init__.py` scaffold → extension scaffold

**Giftcards-specific (do NOT port):**
- Gift card design editor (drag-and-drop QR/text, templates, Pillow rendering) — lnurlmint has no card image
- Email delivery + magic links — bearer notes transfer out-of-band
- Expiry sweep / auto-refund — bearer notes are perpetual; reconciliation is about melt settlement, not expiry
- Bulk CSV creation — minting is per-payRequest
- Printable PNG card images — one-pager QR only

## Sources

- `/home/exedev/lnurl-mint/README.md` — full feature/endpoint documentation (read in full, 417 lines)
- `/home/exedev/lnurl-mint/lnurl_mint/router.py` — endpoint behavior, fee math, in-flight melt tracking, reconcile, verify gating, comment protection (read lines 1–782)
- `/home/exedev/lnurlmint/.planning/PROJECT.md` — project context, validated requirements, out-of-scope, key decisions
- `/home/exedev/giftcards/README.md` — reference extension features (read in full, 157 lines)
- `/home/exedev/giftcards/config.json` — `min_lnbits_version: 1.5.4`, extension metadata shape
- `/home/exedev/giftcards/views_api.py` — API router structure, LNURL encoding helpers, `_public_base_url`, auth decorators (read lines 1–199)
- `/home/exedev/giftcards/services.py` — funding primitives, expiry reclaim, image rendering (grep scan)
- `/home/exedev/giftcards/tasks.py` — `create_permanent_unique_task` + `run_interval` expiry sweep pattern (read in full)
- LUD-25 draft (`github.com/lnurl/luds/blob/lnurlcash/25.md`) — referenced via PROJECT.md Context

---
*Feature research for: LUD-25 lnurlcash mint as an LNbits extension*
*Researched: 2026-08-28*
