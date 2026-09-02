# lnurlmint

  <img src="static/image/lnurlmint.png" alt="lnurlmint icon" align="right" width="160">

An [LNbits](https://github.com/lnbits/lnbits) extension that implements **lnurlcash** ([LUD-25](https://github.com/lnurl/luds/pull/301)) — Lightning bearer assets on top of [LUD-03](https://github.com/lnurl/luds/blob/luds/03.md) `withdrawRequest` and [LUD-06](https://github.com/lnurl/luds/blob/luds/06.md) `payRequest`. A port of the standalone [`lnurl-mint`](https://github.com/dni/lnurl-mint) FastAPI app into the LNbits extension model.

Each LNbits wallet can run its own mint, issuing bearer notes that circulate offline as `lnurlw://` withdraw links and can be rotated, split, merged, or melted back to a BOLT-11 payment.

## What is a bearer note?

A bearer note is a `k1` (a secret preimage) that the mint has credited with value. It is:

1. **Minted** by paying a LUD-06 invoice — the payment preimage *is* the note
2. **Circulated** offline as `lnurlw://<host>/w?k1=<k1>` — no account needed
3. **Redeemed** by anyone with a spec-compliant wallet — rotate, split, merge, or melt back to sats

Redeem notes with [lnurl-wallet](https://github.com/dni/lnurl-wallet) (hosted at [wallet.lnurlcash.com](https://wallet.lnurlcash.com)) or any LUD-03/LUD-25 compatible wallet.

## Features

- **Per-wallet mints** — each LNbits wallet can create and configure its own mint with custom fees, limits, and identity
- **Mint** (LUD-06) — pay an invoice, receive a bearer note keyed by the payment preimage
- **Melt** (LUD-03) — redeem a note for a BOLT-11 payment, with confirm-before-burn tristate settlement
- **Rotate** — burn a note, mint a fresh one keyed by a wallet-supplied hash (same value)
- **Split** — burn one or more notes, mint two (amount + change)
- **Merge** — burn several notes, mint one worth the sum
- **Verify** (LUD-21) — settlement status endpoint for mint and melt invoices
- **Offline verification** (LUD-25) — per-mint secp256k1 keypair; `mintPubkey` advertised, `sig`/`sig2` on rotate/split/merge
- **Comment protection** — notes can be keyed by a wallet-supplied comment hash instead of the payment preimage
- **Sunset mode** — stop issuing new notes while allowing existing notes to be redeemed
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
- No additional Python dependencies — uses only libraries already in LNbits (`bolt11`, `bech32`, `httpx`, `pyqrcode`, `loguru`)

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
| Onion URL | Tor hidden service URL for callback substitution | — |

### 2. Share the LNURL

Each mint has a public one-pager at `/lnurlmint/m/{mint_id}` showing a QR code of the mint's LNURL. Share this URL or the LNURL string with anyone who should be able to mint notes.

### 3. Mint a note

A payer scans the QR code (or pastes the LNURL into a wallet), pays the invoice, and receives a bearer note. The payment preimage becomes the note's secret (`k1`).

### 4. Redeem a note

The note holder opens the `lnurlw://` link in a compatible wallet and chooses:

- **Melt** — provide a BOLT-11 invoice; the note is reserved, the invoice is paid asynchronously, and the note is burned on settlement
- **Rotate** — get a fresh note (same value) keyed by a new hash
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

### Public API (no auth)

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/lnurlmint/api/v1/public/{mint_id}` | Mint info for the public one-pager |

### LNURL endpoints (no auth)

| Method | Endpoint | LUD | Description |
|--------|----------|-----|-------------|
| `GET` | `/lnurlmint/lnurlp/{mint_id}` | LUD-06 | payRequest with `withdrawLink` advertisement |
| `GET` | `/lnurlmint/p/cb/{mint_id}` | LUD-06 | Pay callback — returns invoice, preimage becomes note |
| `GET` | `/lnurlmint/w/{mint_id}` | LUD-03 | withdrawRequest (informational, never burns) |
| `GET` | `/lnurlmint/w/cb/{mint_id}` | LUD-03 | Mutating callback — melt, rotate, split, merge |
| `GET` | `/lnurlmint/verify/{mint_id}/{payment_hash}` | LUD-21 | Settlement status for mint/melt invoices |

### Withdraw callback semantics (`/w/cb/{mint_id}`)

| `k1` | `pr` | `amount` | Result |
|------|------|----------|--------|
| one | yes | — | **Melt**: note reserved, invoice paid async, burned on settlement |
| one | no | no | **Rotate**: burned, new note keyed by `h` (same value) |
| one+ | no | yes | **Split**: all burned, two notes minted (`amount` → `h`, remainder → `h2`) |
| many | no | — | **Merge**: all burned, one note worth the sum keyed by `h` |

`h` (sha256 of the new note's preimage) is required whenever `pr` is absent. `h2` is additionally required for splits. The wallet — never the mint — generates the replacement note's secret.

## Security

- **Confirm-before-burn** — melting a note reserves it (pending), pays the invoice asynchronously, and only burns on positive settlement. A failed payment restores the note.
- **No double-spend** — pending notes reject all callbacks with `{"status":"ERROR","reason":"pending"}`
- **No secret leakage** — the mint stores `sha256(k1)` (the hash), never the preimage itself. For rotate/split/merge, the mint never sees the new note's secret — only the wallet-supplied hash.
- **Per-wallet isolation** — every query is scoped by `wallet_id`; no cross-wallet note access is possible
- **Background reconciliation** — a permanent task checks in-flight melts and resolves them (settle or restore) even if payment hooks are missed

## Testing

```sh
cd lnbits
.venv/bin/python -m pytest lnbits/extensions/lnurlmint/tests/ -v
```

The test suite includes 167 test functions across 21 files, ported from the original `lnurl-mint` project — covering double-spend, race conditions, preimage leakage, fee conservation, reconcile-inflight, bearer threat scenarios, and all LNURL endpoint behavior.

## Architecture

```
lnurlmint/
├── __init__.py          # Extension registration, lifecycle, static files
├── config.py            # Extension-level settings (env vars)
├── models.py            # Pydantic v1 models (Mint, Note, wire models)
├── migrations.py        # DB migrations (mints, notes, mints_records, melts)
├── crud.py              # Wallet-scoped DB operations
├── services.py          # Business logic (mint, melt, rotate, split, merge, reconcile)
├── signing.py           # Per-mint secp256k1 keypair, sign_note
├── tasks.py             # Background reconcile task
├── views.py             # Generic routes (management page)
├── views_api.py         # Management + public REST API
├── views_lnurl.py       # LNURL endpoints (payRequest, withdraw, verify)
├── manifest.json        # LNbits extension manifest
├── config.json          # LNbits extension config
├── static/
│   ├── js/
│   │   ├── index.js     # Management SPA (Vue 3, inline template)
│   │   ├── index.vue    # Dummy template (required by LNbits loader)
│   │   ├── public.js    # Public one-pager (Vue 3, inline template)
│   │   └── public.vue   # Dummy template
│   └── routes.json      # SPA route definitions
└── tests/               # 167 test functions, 21 files
```

## Differences from standalone `lnurl-mint`

| Aspect | `lnurl-mint` (standalone) | `lnurlmint` (LNbits extension) |
|--------|---------------------------|-------------------------------|
| Funding | Direct lnd/cln REST client | LNbits wallet abstraction (`create_invoice`/`pay_invoice`) |
| Multi-tenancy | Single global mint | Per-wallet mints |
| Database | Standalone sqlite + module-level lock | LNbits `Database` (SQLite + Postgres) |
| Lightning Address | `.well-known/lnurlp/{username}` (LUD-16) | Raw LNURL/QR (LUD-16 deferred to v2) |
| Frontend | Server-rendered HTML page | Vue 3 SPA (management + public) |
| Background tasks | FastAPI lifespan + monitor | LNbits `create_permanent_unique_task` |
| Offline verification | Node-direct `signmessage` (Option A) | Per-mint keypair (Option B, portable) |

## Credits

- **dni** — original [`lnurl-mint`](https://github.com/dni/lnurl-mint) implementation
- **bitkarrot** — LNbits extension port

## License

MIT
