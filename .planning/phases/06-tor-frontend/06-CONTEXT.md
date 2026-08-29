# Phase 06: Tor + Frontend - Context

**Gathered:** 2026-08-29
**Status:** Ready for planning

<domain>
## Phase Boundary

Tor visitors get onion-base-URL callback URLs (no clearnet leak), wallet owners
can create and configure their mint via a management SPA, and visitors can view
a public one-pager showing the mint QR, limits, and node info.

This phase delivers three independent but related pieces:

1. **Tor base URL substitution** (Plan 06-01) — modifies the existing
   `_public_base_url` helper in `services.py` to detect when the request Host
   matches the mint's `onion_url` hostname, and returns the onion URL as the
   base for callback/withdrawLink URLs. This is spoof-proof because the match
   is against the operator's own configured `onion_url`, not a raw request
   header.

2. **Management SPA** (Plan 06-02) — replaces the Phase 1 placeholder Vue page
   with a full Vue 3 + Quasar management interface: create mint (all config
   fields), update config, delete mint (with outstanding-notes guard), view
   outstanding notes, see mint activity. Served via the existing `index` generic
   view (authenticated via `check_user_exists`). Requires a new public API
   endpoint for outstanding notes and mint activity.

3. **Public one-pager** (Plan 06-03) — a new Vue 3 + Quasar page served via
   `index_public` (no authentication) at `/lnurlmint/m/{mint_id}`: shows the
   mint QR code (LNURL of the payRequest), mint limits, node info (alias,
   color, capacity, channel/peer counts) with mempool.space/amboss.space
   links, and Tor address if configured. Requires a new public API endpoint
   for mint info + node info.

</domain>

<decisions>
## Implementation Decisions

### Tor substitution: match request Host against onion_url hostname
- The source `config.py` `public_base_url` method (line 160) compares
  `urlparse(request_base_url).hostname` against
  `urlparse(self.onion_url).hostname`. If they match, it returns
  `onion_url.rstrip("/")`; otherwise it returns `base_url.rstrip("/")`.
- The port's `_public_base_url(request, mint)` in `services.py` (line 118)
  currently returns `mint.base_url.rstrip("/")` if set, else falls back to
  `str(request.base_url).rstrip("/")`. Plan 06-01 adds the onion detection:
  if `mint.onion_url` is set AND the request's Host header matches the onion
  hostname, return `onion_url.rstrip("/")`. Otherwise fall through to the
  existing `base_url` / `request.base_url` logic.
- **Spoof-proofness**: an attacker cannot make the server use an arbitrary
  base URL by sending a spoofed Host header. The match is against the
  operator's own pre-configured `onion_url` — a spoofed Host that doesn't
  match the onion hostname falls through to `base_url` (also operator-set).
  The only way to get onion URLs is to actually connect via the onion service.

### X-Forwarded-Host: documented assumption, not implemented
- Behind a reverse proxy (nginx, Caddy, Tor bridge), the Host header may
  arrive as `X-Forwarded-Host`. LNbits' Starlette/uvicorn stack populates
  `request.base_url` from the Host header (or X-Forwarded-Host if the proxy
  is trusted and `--proxy-headers` is passed to uvicorn). This phase does NOT
  add custom X-Forwarded-Host parsing — it relies on `request.base_url` (or
  `request.headers["host"]`) being correct, which is the operator's
  responsibility to configure. This is documented as an assumption in the
  code comments and the plan.

### Management SPA: Vue 3 + Quasar SFCs via LNbits vendor bundle
- The existing Phase 1 placeholder (`static/js/index.vue` + `index.js`) is
  replaced with a full management SPA. The pattern follows `giftcards`:
  `routes.json` maps `/lnurlmint/` to the SFC pair, the `index` generic view
  serves `base.html` (which loads the vendor bundle + routes.json), and the
  SFC uses `LNbits.api.request()` for authenticated API calls.
- The management API already exists (`views_api.py`): POST/GET/PUT/DELETE on
  `/api/v1/mints`. Plan 06-02 adds two new endpoints:
  - `GET /api/v1/mints/{mint_id}/notes` — outstanding notes for a mint
    (wallet-scoped, `require_invoice_key`)
  - `GET /api/v1/mints/{mint_id}/activity` — recent mint/melt activity log
    (wallet-scoped, `require_invoice_key`)

### Public one-pager: served via index_public, no auth
- A new route `/lnurlmint/m/{mint_id}` is added to `views.py` using
  `index_public` (no `check_user_exists` dependency). The Vue SFC fetches
  public mint data from a new unauthenticated API endpoint.
- New public API endpoint: `GET /lnurlmint/api/v1/public/{mint_id}` — returns
  mint info (username, limits, sunset status, onion_url) and node info (if
  available). This is a new router or added to `views_api.py` without auth
  decorators.

### Node info: LNbits public node API or graceful degradation
- The source `frontend.py` fetches node info via `cached_fetch_node_info`
  (direct lnd/cln REST). The port cannot use that — it uses LNbits' funding
  source abstraction.
- LNbits has a public node API at `/node/public/api/v1/info` (guarded by
  `check_public` which requires `lnbits_node_ui` and `lnbits_public_node_ui`
  settings). It returns `PublicNodeInfo` (id, alias, color, num_peers,
  channel_stats with capacity/counts, addresses).
- The public one-pager's API endpoint will attempt to fetch node info via
  `get_funding_source()` + the node's `get_public_info()` method. If the
  backend doesn't implement the Node API (FakeWallet, VoidWallet) or the
  settings are disabled, the page shows a graceful "Node info unavailable"
  message — matching the source's "No funding source configured" fallback.
- Node info is fetched server-side (not from the browser) to avoid CORS
  issues and to keep the LNbits node URL internal. The API endpoint returns
  the data as JSON; the Vue SFC renders it.

### QR code generation: pyqrcode (server-side, already a dep)
- The source uses `qrcode` (Python qrcode library) for SVG QR codes. The
  port uses `pyqrcode` (already in LNbits' `pyproject.toml`, per EXT-04).
- The public API endpoint returns the LNURL string; the Vue SFC renders the
  QR code client-side using the LNbits `lnbits-qrcode` Vue component (same
  component used by giftcards' redeem page). This avoids server-side SVG
  generation and leverages the existing LNbits UI infrastructure.
- The LNURL is bech32-encoded: `lnurl_encode(f"{base}/lnurlmint/lnurlp/{mint_id}")`
  using `bech32_encode("lnurl", convertbits(url.encode(), 8, 5, True)).upper()`.

### mint activity log: from mints_records + melts tables
- The "mint activity" view shows recent mint and melt records from the
  existing `mints_records` and `melts` tables. A new CRUD function
  `get_mint_activity(mint_id, wallet_id, limit=20)` queries both tables,
  merges and sorts by `created_at` descending. No new table is needed.

### Claude's Discretion
- Whether the public mint info endpoint lives in `views_api.py` (without auth
  decorators) or a new `views_public.py` — either works; `views_api.py` is
  preferred to avoid a new file.
- Whether node info is fetched via `get_funding_source().__node_cls__` or
  via an internal HTTP call to `/node/public/api/v1/info` — direct
  `get_funding_source()` is preferred (no HTTP self-call).
- Exact Quasar components used in the SPA (q-table vs q-list, q-dialog vs
  q-page) — follow giftcards patterns where applicable.
- Whether the public one-pager uses a separate Vue SFC pair or shares the
  management SPA's index — separate is correct (different auth, different
  data, different routes.json entry).

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`services._public_base_url`** (services.py line 118) — the Tor
  substitution target. Currently returns `mint.base_url.rstrip("/")` if set,
  else `str(request.base_url).rstrip("/")`. Plan 06-01 adds onion detection
  before the base_url fallback.
- **`Mint.onion_url`** (models.py line 42) — `Optional[str] = None`. Already
  stored in the DB, already settable via `CreateMint`/`UpdateMint`. No
  migration needed.
- **`Mint.base_url`** (models.py line 41) — `str = ""`. Already the
  spoof-proof override mechanism.
- **`views.py`** (13 lines) — currently has only the `index` route at `/`.
  Plan 06-03 adds an `index_public` route at `/m/{mint_id}`.
- **`views_api.py`** (127 lines) — full CRUD API already exists:
  POST/GET/GET/{id}/PUT/{id}/DELETE/{id}. Plans 06-02 and 06-03 add new
  endpoints.
- **`static/routes.json`** — currently has one route (`/lnurlmint/` →
  `PageLnurlmint`). Plan 06-03 adds `/lnurlmint/m/:mint_id` →
  `PageLnurlmintPublic`.
- **`static/js/index.js` + `index.vue`** — Phase 1 placeholder (80 + 87
  lines). Plan 06-02 replaces these with the full management SPA.
- **`crud.py`** — has `get_mint_by_id` (public, no wallet scope), all note
  CRUD, `mints_records` and `melts` access. Plan 06-02 adds
  `get_outstanding_notes` and `get_mint_activity`.
- **`services.max_mintable_msat`** (services.py line 96) — fee-aware max
  note value. Used by the public one-pager to show the effective max.
- **`services._min_sendable_msat`** (services.py line 75) — fee-aware min.
  Used by the payRequest; the one-pager shows `min_mint_msat` as the floor.
- **`signing.mint_pubkey`** (signing.py) — the mint's public key. Could be
  shown on the one-pager for offline verification transparency.
- **`giftcards/views.py`** — reference for `index` + `index_public` pattern
  (lines 8-35). The `index_public` route at `/redeem/{raw_token}` is the
  exact pattern for the one-pager's `/m/{mint_id}`.
- **`giftcards/static/js/redeem.js` + `redeem.vue`** — reference for a
  public (no-auth) Vue SFC that fetches data and renders a QR code. Uses
  `lnbits-qrcode` component, `NostrTools.nip19.encodeBytes` for bech32.
- **`giftcards/static/routes.json`** — reference for multi-route routes.json
  with path params (`/giftcards/redeem/:raw_token`).
- **LNbits `PublicNodeInfo`** (nodes/base.py line 103) — `id`, `alias`,
  `color`, `num_peers`, `channel_stats` (with `total_capacity`,
  `counts`), `addresses`. This is the data structure for the one-pager's
  node info section.
- **LNbits `get_funding_source()`** (lnbits.wallets) — returns the active
  wallet backend. `funding_source.__node_cls__` is the Node class (if the
  backend implements the Node API). `funding_source.features` lists
  supported features (check for `Feature.nodemanager`).

### Established Patterns
- **LNbits generic views** — `index` (authenticated, `check_user_exists`)
  renders `base.html` with `{"user": user.json()}`. `index_public`
  (unauthenticated) renders `base.html` with `{"public": True}`. Both are
  imported from `lnbits.core.views.generic`.
- **Vue SFC pattern** — `routes.json` maps URL paths to component names +
  template/component file paths. The SFC `.vue` file is the template
  (`<template id="...">`), the `.js` file defines `window.PageName = {
  template: '#...', data() {...}, methods: {...} }`.
- **API calls from Vue** — `LNbits.api.request(method, path, key, body)`
  uses the wallet's admin/invoice key from `this.g.user.wallets[0]`.
- **LNbits node API** — `get_funding_source()` → check `features` for
  `Feature.nodemanager` → `funding_source.__node_cls__(funding_source)` →
  `node.get_public_info()`. Cached via `cache.save_result`.
- **No new dependencies** — `pyqrcode` is already in LNbits. `bech32` is
  already used by the extension. Vue/Quasar components are in the LNbits
  vendor bundle.

### Integration Points
- **`services.py`** — `_public_base_url` modified for Tor substitution.
- **`views.py`** — add `index_public` route at `/m/{mint_id}`.
- **`views_api.py`** — add `GET /api/v1/mints/{mint_id}/notes`,
  `GET /api/v1/mints/{mint_id}/activity`, `GET /api/v1/public/{mint_id}`.
- **`crud.py`** — add `get_outstanding_notes(mint_id, wallet_id)`,
  `get_mint_activity(mint_id, wallet_id, limit)`.
- **`static/routes.json`** — add public one-pager route.
- **`static/js/index.js` + `index.vue`** — replace with full management SPA.
- **`static/js/public.js` + `public.vue`** — new public one-pager SFC pair.
- **`tests/test_onion.py`** — new test file for Tor substitution.
- **`tests/test_frontend.py`** — new test file for frontend API endpoints.

</code_context>

<specifics>
## Specific Ideas

- The Tor substitution is a 5-line change to `_public_base_url`: extract
  the request hostname, extract the onion hostname, compare, return
  onion_url if match. The existing `base_url` fallback stays as-is. The
  `request.base_url` fallback (when `base_url` is empty) also gets the
  onion check first.
- The public one-pager's API endpoint (`GET /api/v1/public/{mint_id}`) is
  the single endpoint the Vue SFC calls. It returns a JSON object with:
  `username`, `min_mint_msat`, `max_mintable_msat` (fee-aware),
  `sunset_mint`, `onion_url`, `lnurl` (pre-encoded), `node_info` (or null),
  `mint_pubkey` (for offline verification transparency). This keeps the
  SFC simple — one fetch, render the response.
- The management SPA's "outstanding notes" view is a q-table showing note
  id (truncated), amount (in sats), state (outstanding/pending/spent), and
  created_at. The "activity" view is a q-table showing type (mint/melt),
  amount, payment_hash (truncated), and timestamp.
- The public one-pager shows the mint QR as an `lnbits-qrcode` component
  (same as giftcards redeem page). The LNURL value is computed server-side
  in the API endpoint (using `_public_base_url` + bech32 encode) so the
  SFC doesn't need to know the base URL logic.
- Node info links: `https://mempool.space/lightning/node/{pubkey}` and
  `https://amboss.space/node/{pubkey}` — same as the source's
  `EXPLORERS_ROW` template.
- The public one-pager route `/m/{mint_id}` uses a short path to keep QR
  codes small (the LNURL encodes `/lnurlmint/lnurlp/{mint_id}`, not the
  one-pager URL — but the one-pager URL itself should be shareable).

</specifics>

<deferred>
## Deferred Ideas

- **Lightning Address (LUD-16)** — the source's one-pager shows
  `username@host` as a lightning address. The port defers this to v2
  (requires lnurlp extension PR). The one-pager shows the raw LNURL/QR
  instead.
- **Node info caching (OPT-01)** — the source caches node info for 1h.
  The port fetches it live on each page view (or uses LNbits' built-in
  `cache.save_result` with a default TTL). Explicit caching is deferred.
- **Sunset-mode one-pager variants** — the source's frontend has
  SUNSET_SECTION that replaces the pay/QR section when sunsetting. The
  port's one-pager can show a "mint is sunsetting" banner but the full
  sunset UX variant is deferred to keep the SPA simple.
- **Swagger UI / docs page** — the source serves a self-hosted Swagger UI.
  LNbits has its own API docs infrastructure; this is not replicated.
- **test_frontend.py full port** — the source's test_frontend.py tests
  the server-rendered HTML one-pager. The port's one-pager is a Vue SPA,
  so those tests don't directly apply. The API endpoint tests replace
  them. Full frontend test port is Phase 7 (TEST-10).
- **test_onion.py full port** — the source's test_onion.py tests the
  `settings.public_base_url` method and the server-rendered HTML. The
  port tests the `_public_base_url` function and the LNURL endpoint
  responses. Full test port is Phase 7 (TEST-10).

</deferred>
