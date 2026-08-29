# Phase 06: Tor + Frontend - Research

**Researched:** 2026-08-29
**Status:** Complete

## Research Topics

### 1. _public_base_url Tor Substitution

**Source implementation** (`lnurl_mint/config.py` lines 160-166):
```python
def public_base_url(self, request_base_url: str) -> str:
    if self.onion_url:
        request_host = urlparse(request_base_url).hostname or ""
        onion_host = urlparse(self.onion_url).hostname or ""
        if request_host and request_host == onion_host:
            return self.onion_url.rstrip("/")
    return self.base_url.rstrip("/")
```

The source takes `request_base_url` (a string, typically `str(req.base_url)`)
and compares its hostname against the `onion_url`'s hostname. If they match,
the onion URL is used as the base. Otherwise, `base_url` is used.

**Current port** (`lnurlmint/services.py` lines 118-128):
```python
def _public_base_url(request, mint: Mint) -> str:
    if mint.base_url:
        return mint.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")
```

The port currently has no onion detection. It returns `mint.base_url` if set,
otherwise falls back to `request.base_url`.

**Plan for Tor substitution**: Insert the onion check before the `base_url`
fallback. The modified function:
1. If `mint.onion_url` is set, extract the request hostname from
   `request.base_url` (or `request.headers["host"]`) and the onion hostname
   from `urlparse(mint.onion_url).hostname`.
2. If they match, return `mint.onion_url.rstrip("/")`.
3. Otherwise, fall through to the existing `base_url` / `request.base_url`
   logic.

**Spoof-proofness analysis**: The match is against the operator's own
configured `onion_url`. An attacker sending a spoofed Host header that matches
the onion hostname would only cause the server to return the onion URL — which
is the operator's own address. A spoofed Host that doesn't match falls through
to `base_url` (also operator-set). There is no way to make the server return
an attacker-controlled URL. The only way to trigger the onion branch is to
actually connect via the onion service (which means the request genuinely
arrived over Tor).

**X-Forwarded-Host**: `request.base_url` in Starlette is derived from the
Host header. Behind a trusted proxy with `--proxy-headers`, uvicorn populates
Host from X-Forwarded-Host. This is the operator's responsibility to configure
correctly. No custom X-Forwarded-Host parsing is added.

**Call sites**: `_public_base_url` is called in `views_lnurl.py` at:
- Line 86: `get_payrequest` (payRequest advertisement)
- Line 190: `get_pay_callback` (mint callback, for verify URL)
- Line 281: `get_withdraw` (withdrawRequest advertisement)
- Line ~500: `get_withdraw_callback` (not directly, but the callback URL
  in the withdrawRequest response uses it)

All four call sites pass `request` and `mint`, so the signature doesn't
change. The Tor substitution is transparent to all callers.

### 2. Giftcards Pattern (Vue 3 + Quasar SFC Registration)

**How giftcards registers Vue 3 SFCs**:

1. **`__init__.py`** — exports `giftcards_static_files = [{"path":
   "/giftcards/static", "name": "giftcards_static"}]`. LNbits mounts this
   as a static file route.

2. **`static/routes.json`** — a JSON array mapping URL paths to component
   names and file paths:
   ```json
   [
     {"path": "/giftcards/", "name": "PageGiftCards", "template":
      "/giftcards/static/js/index.vue", "component": "/giftcards/static/js/index.js"},
     {"path": "/giftcards/redeem/:raw_token", "name": "PageGiftCardsRedeem",
      "template": "/giftcards/static/js/redeem.vue", "component":
      "/giftcards/static/js/redeem.js"}
   ]
   ```
   LNbits' frontend reads this file to register Vue routes dynamically.

3. **`views.py`** — registers generic routes:
   - `GET /` → `index` (authenticated, `Depends(check_user_exists)`)
   - `GET /redeem/{raw_token}` → `index_public` (no auth)
   - `GET /claim` → `index_public`
   - `GET /claim/{magic_token}` → `index_public`

4. **SFC files** — `.vue` is the template (`<template id="page-name">`),
   `.js` is the component definition (`window.PageName = { template:
   '#page-name', data() {...}, methods: {...} }`).

5. **API calls** — `LNbits.api.request(method, path, key, body)` where
   `key` is `wallet.adminkey` or `wallet.inkey` from
   `this.g.user.wallets[0]`.

6. **Public (no-auth) pages** — the redeem page fetches data via plain
   `fetch()` (not `LNbits.api.request`) since there's no wallet key:
   ```javascript
   const response = await fetch(`/giftcards/api/v1/cards/public/${hash}`)
   ```

**Vendor bundle mechanism**: LNbits' `base.html` template includes the
vendor bundle (Vue 3, Quasar, NostrTools, LNbits runtime). Extension SFCs
are loaded as additional routes via `routes.json`. No build step is needed —
the `.vue` and `.js` files are served as static files and evaluated at
runtime.

### 3. Management SPA Scope

**Existing CRUD endpoints** (`views_api.py`):
- `POST /api/v1/mints` — create mint (admin key)
- `GET /api/v1/mints` — list mints (invoice key)
- `GET /api/v1/mints/{mint_id}` — get mint (invoice key)
- `PUT /api/v1/mints/{mint_id}` — update mint (admin key)
- `DELETE /api/v1/mints/{mint_id}` — delete mint (admin key, 409 if
  outstanding notes)

**New endpoints needed**:
- `GET /api/v1/mints/{mint_id}/notes` — outstanding notes for a mint
  (invoice key, wallet-scoped). Returns list of notes with id, amount_msat,
  state, created_at.
- `GET /api/v1/mints/{mint_id}/activity` — recent mint/melt activity
  (invoice key, wallet-scoped). Returns merged list from `mints_records`
  and `melts` tables, sorted by created_at desc, limited to ~20.

**New CRUD functions needed**:
- `get_outstanding_notes(mint_id, wallet_id)` — SELECT from notes JOIN
  mints WHERE mint_id = :mid AND m.wallet = :wallet, ordered by created_at
  desc.
- `get_mint_activity(mint_id, wallet_id, limit=20)` — SELECT from
  mints_records WHERE mint_id = :mid (JOIN mints for wallet scope) UNION
  SELECT from melts WHERE mint_id = :mid (JOIN mints for wallet scope),
  ordered by created_at desc, LIMIT :limit.

**Vue components needed**:
- Mint list (q-table or q-list with username, id, status)
- Create mint dialog (q-dialog with form fields: username, base_fee_msat,
  fee_percent_ppm, min_sendable_msat, max_sendable_msat, min_mint_msat,
  verify_enabled, sunset_mint, base_url, onion_url)
- Edit mint dialog (same fields, pre-filled, PUT request)
- Delete mint button (with 409 error handling)
- Outstanding notes table (per-mint expandable section)
- Activity log table (per-mint expandable section)

**Existing placeholder to replace**: `static/js/index.js` (80 lines) and
`static/js/index.vue` (87 lines) — Phase 1 placeholder with basic
create/list/delete. Plan 06-02 replaces with full SPA including edit,
notes view, and activity log.

### 4. Public One-Pager Scope

**Data shown** (from source `frontend.py`):
- Mint QR code (LNURL of payRequest: `lnurl_encode(f"{base}/lnurlp/{id}")`)
- Lightning address (`username@host`) — **deferred to v2** (LUD-16)
- Mint limits (min_mint_msat, max_mintable_msat fee-aware)
- Node info (alias, color, pubkey, connect string, channels, peers,
  capacity) with mempool.space/amboss.space links
- Tor address if configured (and not already on Tor)
- Sunset notice if sunsetting

**QR generation**: The source uses `qrcode.image.svg.SvgPathImage` for
server-side SVG. The port uses the `lnbits-qrcode` Vue component (same as
giftcards redeem page) for client-side QR rendering. The API endpoint
returns the LNURL string; the SFC passes it to `<lnbits-qrcode :value="lnurl">`.

**Node info source**: LNbits' `get_funding_source()` →
`funding_source.__node_cls__(funding_source)` → `node.get_public_info()`
returns `PublicNodeInfo` (id, alias, color, num_peers, channel_stats).
The API endpoint calls this server-side and includes the result (or null)
in the response.

**mempool.space/amboss.space links**: `https://mempool.space/lightning/node/{pubkey}`
and `https://amboss.space/node/{pubkey}` — same as source's EXPLORERS_ROW.

**Public API endpoint**: `GET /api/v1/public/{mint_id}` (no auth) returns:
```json
{
  "username": "mint",
  "min_mint_msat": 10000,
  "max_mintable_msat": 999999000,
  "sunset_mint": false,
  "onion_url": "http://abc...onion",
  "lnurl": "LNURL1...",
  "mint_pubkey": "02ab...",
  "node_info": {
    "id": "02ab...",
    "alias": "fakenode",
    "color": "#3399ff",
    "num_peers": 5,
    "channel_stats": {"total_capacity": 750000000, "counts": {...}}
  }
}
```

**Route**: `/lnurlmint/m/{mint_id}` → `index_public` (no auth). The Vue
SFC extracts `mint_id` from the URL path and calls the public API endpoint.

### 5. Frontend Test Port

**Source `test_frontend.py`** (234 lines) tests the server-rendered HTML
one-pager:
- Title/description/QR/address present in HTML
- Node info (alias, pubkey, connect string) in HTML
- Explorer links (mempool.space, amboss.space) in HTML
- "No funding source configured" when backend is None
- `base_url` setting overrides request URL
- LNURL encode roundtrip
- Sunset mode: no QR, no "After paying", says "no longer issuing notes"
- Tor section shown/hidden based on config
- Swagger UI / docs page (not applicable to port)

**Adaptation to LNbits**: The port's one-pager is a Vue SPA, not
server-rendered HTML. The tests are adapted to test the **API endpoint**
(`GET /api/v1/public/{mint_id}`) instead:
- Response contains `username`, `lnurl`, `min_mint_msat`,
  `max_mintable_msat`, `node_info` (or null)
- `base_url` override: LNURL uses `base_url`, not request host
- Sunset: response has `sunset_mint: true` (SPA handles display)
- Tor: response includes `onion_url` when configured
- Node info: present when funding source has Node API, null otherwise

The full `test_frontend.py` port is Phase 7 (TEST-10). Phase 6 includes
basic API endpoint tests.

### 6. Tor Test Port

**Source `test_onion.py`** (66 lines) tests:
1. `public_base_url` prefers onion for matching host
2. `public_base_url` ignores onion for other hosts
3. `public_base_url` ignores request when onion unset
4. `public_base_url` still prefers base_url over request
5. Pay response uses onion URL when reached via onion (HTTP test with
   spoofed Host header)
6. Index shows Tor section when configured
7. Index hides Tor section when not configured
8. Index omits redundant Tor section when already on onion

**Adaptation to LNbits**: Tests 1-4 test the `_public_base_url` function
directly (unit tests). Tests 5-8 test HTTP responses (integration tests
with the LNbits test client). The port:
- Tests 1-4: call `_public_base_url(request, mint)` with mock request
  objects and mints with/without `onion_url`.
- Test 5: HTTP GET to `/lnurlmint/lnurlp/{mint_id}` with Host header set
  to the onion hostname, assert callback URL uses onion base.
- Tests 6-8: test the public API endpoint response (not HTML) — assert
  `onion_url` is present/absent in the JSON response.

The full `test_onion.py` port is Phase 7 (TEST-10). Phase 6 includes
core substitution tests.

### 7. Static File Registration

**Current registration** (`__init__.py` lines 16-21):
```python
lnurlmint_static_files = [
    {"path": "/lnurlmint/static", "name": "lnurlmint_static"}
]
```

This is already registered (EXT-02, Phase 1). LNbits mounts the static
directory at `/lnurlmint/static/`. Vue SFCs (`.vue` and `.js` files) are
served from there and referenced in `routes.json` by their full path
(`/lnurlmint/static/js/index.vue`).

**No changes needed to `__init__.py`** for Phase 6 — the static files
registration is already in place. New SFC files (`public.js`, `public.vue`)
are added to `static/js/` and referenced in `routes.json`.

### 8. LNbits View Patterns

**`index` (authenticated)**:
```python
from lnbits.core.views.generic import index
from lnbits.decorators import check_user_exists

router.add_api_route(
    "/", methods=["GET"], endpoint=index,
    dependencies=[Depends(check_user_exists)]
)
```
Renders `base.html` with `{"user": user.json()}`. The user object is
available to the Vue SPA via `window.g.user`.

**`index_public` (unauthenticated)**:
```python
from lnbits.core.views.generic import index_public

router.add_api_route(
    "/m/{mint_id}", methods=["GET"], endpoint=index_public
)
```
Renders `base.html` with `{"public": True}`. No user object is available.
The Vue SPA must use plain `fetch()` for API calls (no wallet key).

**Key insight**: Both `index` and `index_public` are the SAME function
signature — they're FastAPI endpoints that render `base.html`. The
difference is the `check_user_exists` dependency. The `routes.json` entry
determines which Vue component is loaded for the path.

**Path parameters**: `index_public` accepts path params (e.g.,
`/redeem/{raw_token}`) — FastAPI passes them to the route, and the Vue
router extracts them from `window.location.pathname`. The giftcards
redeem page does this: `const pathParts = window.location.pathname.split('/')`.

## Summary

| Topic | Finding |
|-------|---------|
| Tor substitution | 5-line change to `_public_base_url`: compare request Host to onion hostname, return onion_url if match |
| Giftcards pattern | `routes.json` + `index`/`index_public` + `.vue`/`.js` SFC pairs, no build step |
| Management SPA | Replace placeholder, add notes + activity endpoints, full CRUD UI |
| Public one-pager | New `index_public` route + public API endpoint + Vue SFC with `lnbits-qrcode` |
| Node info | `get_funding_source().__node_cls__().get_public_info()` or null |
| QR generation | `lnbits-qrcode` Vue component (client-side), LNURL computed server-side |
| Static files | Already registered (Phase 1), just add new SFC files |
| View patterns | `index` (auth) + `index_public` (no auth), both render `base.html` |
