# lnurlmint

  <img src="static/image/lnurlmint.png" alt="lnurlmint icon" align="right" width="160">

An [LNbits](https://github.com/lnbits/lnbits) extension that implements **lnurlcash** ([LUD-25](https://github.com/lnurl/luds/pull/301)) — Lightning bearer assets on top of [LUD-03](https://github.com/lnurl/luds/blob/luds/03.md) `withdrawRequest` and [LUD-06](https://github.com/lnurl/luds/blob/luds/06.md) `payRequest`. A port of the standalone [`lnurl-mint`](https://github.com/lnurlcash/lnurl-mint) FastAPI app into the LNbits extension model.

Each LNbits wallet can run its own mint, issuing bearer notes that circulate offline as `lnurlw://` withdraw links and can be rotated, split, merged, or melted back to a BOLT-11 payment.

## What is a bearer note?

A bearer note is a taproot output key `Q` the mint has credited with value. Whoever can produce a valid spend for `Q` owns the value — the mint enforces single-spend. Notes come in two forms:

- **Bearer notes** — `Q` is a NUMS-taprooted preimage leaf; anyone holding the 32-byte preimage (`k1`, hex) spends it, no signature needed.
- **Key-path notes** — `Q` is a plain x-only public key; spending requires a BIP-340 signature (`ck1`) over the canonical spend transaction's sighash, bound to the mint's domain.
- **Script-path notes** (`cw1`) — `Q` commits to a leaf script (timelocks, multisig, or any tapscript) under a NUMS internal key; spending reveals the leaf, its control block, and a witness.

Notes are identified on the wire by `hex(Q)` or `cp1` encodings; certificates (`cs1`) carry the mint's recoverable ECDSA signature over `(amount, Q)` for offline verification.

1. **Minted** by paying a LUD-06 invoice — the wallet names the note's output (`comment=<h>` or `cp1`), so the invoice preimage is never the secret
2. **Circulated** offline as `lnurlw://<host>/w?k1=<k1>` — no account needed
3. **Redeemed** by anyone with a spec-compliant wallet — rotate, split, merge, or melt back to sats
4. **Recovered** — wallets re-derive keys and find notes via the `?p=` hash lookup

Redeem notes with [lnurl-wallet](https://github.com/dni/lnurl-wallet) (hosted at [wallet.lnurlcash.com](https://wallet.lnurlcash.com)) or any LUD-03/LUD-25 compatible wallet.

## Features

- **Per-wallet mints** — each LNbits wallet can create and configure its own mint with custom fees, limits, and identity
- **Mint** (LUD-06) — pay an invoice; the note lands on the wallet's chosen output (mandatory comment/cp1, or a registered username's derived branch)
- **Melt** (LUD-03) — redeem a note for a BOLT-11 payment, with confirm-before-burn tristate settlement
- **Rotate** — burn a note, mint a fresh one (same value) on a new output
- **Split** — burn one or more notes, mint two (amount + change)
- **Merge** — burn several notes, mint one worth the sum (base-fee refund)
- **Retry-replay** — an exact re-issue of a completed mutation replays its result instead of "already spent"
- **Hash lookup** — `GET /w?p=<hex(Q)|cp1>` finds a note by public id (recovery scans)
- **Username registration** — `POST /p/{u}` with a signed `cx1` registers `<u>@<mint>` as a lightning address; payments mint onto the registration's derivation branch
- **NIP-05** — `/.well-known/nostr.json` serves registered usernames
- **NIP-57 zaps** — zap requests mint with a description-hash-bound invoice; kind-9735 receipts published to the request's relays
- **Internal transfers** — an internal LNbits payment to a minted invoice settles the note without network round-trips
- **Verify** (LUD-21) — settlement status endpoint for mint and melt invoices
- **Offline verification** (LUD-25) — per-mint secp256k1 keypair; `mintPubkey` advertised, `c`/`c2` certificates on rotate/split/merge and informational GETs
- **Sunset mode** — stop issuing new notes (and splits) while allowing existing notes to be redeemed
- **Tor support** — onion URL substitution for callback URLs
- **Mint fees** — configurable base fee + percentage, withheld at mint time
- **Management SPA** — create/configure mints, view outstanding notes and activity
- **Public one-pager** — QR code, LNURL, mint limits, pubkey, and node info

## Installation

### From source (dev)

```sh
cd lnbits/lnbits/extensions
git clone https://github.com/bitkarrot/lnurlmint.git
```

Restart LNbits. The extension appears in the extensions list.

### Requirements

- LNbits >= 1.5.4
- No required Python dependencies beyond what LNbits ships (`bolt11`, `bech32`, `httpx`, `websockets`, `coincurve`, `loguru`).
- **Optional**: `lnurlcashkernel` (Linux wheels; Bitcoin Core's script interpreter). With it installed, arbitrary `cw1` script-path leaves verify with full upstream parity. Without it, key-path (`ck1`) and bearer-preimage-leaf (`cw1`) spends are verified natively and other script leaves are rejected.

## Usage

### 1. Create a mint

In the LNbits UI, open the **lnurlmint** extension and click **Create Mint**. Configure:

| Field | Description | Default |
|-------|-------------|---------|
| Username | Mint identity (shown on the public page) | — |
| Min sendable | Minimum mintable amount (msat) | 10,000 (10 sats) |
| Max sendable | Maximum mintable amount (msat) | 1,000,000,000 (1,000 sats) |
| Min mint | Net-of-fee floor — rejects mints where `amount - fee < min_mint` | 10,000 |
| Base fee | Flat fee per mint (msat) | 0 |
| Fee percent | Percentage fee in ppm (parts per million) | 0 |
| Verify enabled | Toggle LUD-21 verify endpoint | true |
| Sunset mint | Stop issuing new notes | false |
| Sunset date | LUD-25 sunset timestamp (unix seconds) | — |
| Username registration | Allow `POST /p/{u}` registrations | true |
| NIP-05 | Serve `/.well-known/nostr.json` | true |
| Zaps (NIP-57) | Accept zap requests, publish kind-9735 receipts | false |
| Zap relays | Extra wss:// relays for receipts | — |
| Onion URL | Tor hidden service URL for callback substitution | — |

### 2. Share the LNURL

Each mint has a public one-pager at `/lnurlmint/m/{mint_id}` showing a QR code of the mint's LNURL. Share this URL or the LNURL string with anyone who should be able to mint notes.

### 3. Mint a note

A payer scans the QR code (or pastes the LNURL into a wallet); their wallet picks the note's output — a preimage hash `h`, a `cp1` public key, or a `cp1` script-path note — and sends it as `comment`. After paying, the note materializes under that output.

### 4. Redeem a note

The note holder opens the `lnurlw://` link in a compatible wallet and chooses:

- **Melt** — provide a BOLT-11 invoice; the note is reserved, the invoice is paid asynchronously, and the note is burned on settlement
- **Rotate** — get a fresh note (same value) on a new output
- **Split** — break into two notes (specified amount + change)
- **Merge** — combine multiple notes into one

## API Reference

### Management API (admin key)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/lnurlmint/api/v1/mints` | Create a mint |
| `GET` | `/lnurlmint/api/v1/mints` | List mints for wallet |
| `GET` | `/lnurlmint/api/v1/mints/{mint_id}` | Get mint details |
| `PUT` | `/lnurlmint/api/v1/mints/{mint_id}` | Update mint config |
| `DELETE` | `/lnurlmint/api/v1/mints/{mint_id}` | Delete mint (fails if notes outstanding) |
| `GET` | `/lnurlmint/api/v1/mints/{mint_id}/notes` | List outstanding notes |
| `GET` | `/lnurlmint/api/v1/mints/{mint_id}/activity` | Recent mint/melt activity |
| `GET` | `/lnurlmint/api/v1/mints/{mint_id}/usernames` | Registered usernames |

### Public API (no auth)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/lnurlmint/api/v1/public/{mint_id}` | Mint info for the public one-pager |

### LNURL endpoints (no auth)

| Method | Endpoint | LUD | Description |
|--------|----------|-----|-------------|
| `GET` | `/lnurlmint/lnurlp/{mint_id}` | LUD-06 | payRequest with `withdrawLink` advertisement |
| `GET` | `/lnurlmint/lnurlp/{mint_id}/{u}` | LUD-06 | Username payRequest (`allowsNostr` when zaps enabled) |
| `GET` | `/lnurlmint/p/cb/{mint_id}` | LUD-06 | Pay callback — invoice mints onto the `comment` output |
| `GET` | `/lnurlmint/p/{mint_id}/{u}` | LUD-06 | Username callback — mints onto the registered branch |
| `POST/DELETE` | `/lnurlmint/p/{mint_id}/{u}` | LUD-25 | Username register / unregister (signed `cx1`) |
| `GET` | `/lnurlmint/w/{mint_id}` | LUD-03 | withdrawRequest (`k1` or `p` lookup, informational, never burns) |
| `GET` | `/lnurlmint/w/cb/{mint_id}` | LUD-03 | Mutating callback — melt, rotate, split, merge |
| `GET` | `/lnurlmint/verify/{mint_id}/{payment_hash}` | LUD-21 | Settlement status for mint/melt invoices |
| `GET` | `/lnurlmint/lnurlw/{mint_id}[/{u}]` | LUD-25 | Mint-address alias (node info) |
| `GET` | `/lnurlmint/nostr.json/{mint_id}` | NIP-05 | Username → pubkey mapping |

### Withdraw callback semantics (`/w/cb/{mint_id}`)

| `k1` | `pr` | `amount` | `p1`/`p2` | Result |
|------|------|----------|-----------|--------|
| one+ | yes | — | — | **Melt**: note reserved, invoice paid async, burned on settlement |
| one+ | no | no | `p1` | **Rotate/Merge**: burned, new note on `p1` (sum + base-fee refund) |
| one+ | no | yes | `p1`,`p2` | **Split**: all burned, `amount` → `p1`, change → `p2` |

`k1` accepts: a 64-hex preimage (bearer short form), `ck1` (key-path signature), or `cw1` (script-path). `p1`/`p2` accept `cp1` or a 64-hex bearer `h`. Outputs colliding with an existing note or a pending mint are refused with `"already in use"`.

## Security

- **Confirm-before-burn** — melting a note reserves it (pending), pays the invoice asynchronously, and only burns on positive settlement. A failed payment restores the note.
- **No double-spend** — pending notes reject all callbacks with `{"status":"ERROR","reason":"pending"}`; the swap and mint paths atomically reject outputs colliding with any existing or pending note.
- **Comment protection** — every new note is keyed by the wallet's chosen output; the invoice preimage never redeems it. Migrated legacy notes (preimage-keyed, no comment) keep working and are excluded from `/verify` disclosure.
- **No secret leakage** — k1 secrets, preimages, and p1/p2 are never logged; zap requests are stored base64-encoded so LNbits' value rewriting can't corrupt the description-hash commitment.
- **Per-wallet isolation** — every query is scoped by `wallet_id`; no cross-wallet note access is possible.
- **Background reconciliation** — a permanent task checks in-flight melts (settle or restore) and a boot-time sweep recovers notes stranded by a crash.

## Testing

```sh
cd lnbits
.venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/ -v
```

The suite runs ~300 tests ported from `lnurl-mint` plus extension-specific ones — spec test vectors (derivation, ck1, bech32m), offline-verification certificates, script-path spend policy, settlement races, collision griefing, fee conservation, verify disclosure rules, username/NIP-05/NIP-57 flows, and bearer threat scenarios.

## Architecture

```
lnurlmint/
├── __init__.py          # Extension registration, lifecycle, static files
├── bech32m.py           # BIP-350 codec — cp1/cx1/cs1 encodings
├── derivation.py        # LUD-25 branch derivation (registrations, addresses)
├── taproot.py           # Pure-Python taproot math (tapleaf, tweak, NUMS)
├── spend.py             # ck1/cw1 decode+verify, sighash, leaf policy
│                        #   (lnurlcashkernel when installed, else native fallback)
├── signing.py           # Per-mint secp256k1 keypair, cs1 certificates
├── nostr.py             # NIP-57 zap requests/receipts, relay publishing
├── models.py            # Pydantic v1 models (Mint, Note, registrations, wire)
├── migrations.py        # DB migrations (mints, notes, records, melts, usernames)
├── crud.py              # Wallet-scoped DB operations, atomic swap/settle
├── services.py          # Fee math, settlement, melts, reconcile, zap poll
├── tasks.py             # Background reconcile + zap-receipt tasks
├── views.py             # Generic routes (management page)
├── views_api.py         # Management + public REST API
├── views_lnurl.py       # LNURL endpoints (pay, usernames, withdraw, verify, NIP-05)
├── manifest.json        # LNbits extension manifest
├── config.json          # Extension metadata
└── static/              # Management SPA + public one-pager
```

## License

MIT — see [LICENSE](LICENSE).
