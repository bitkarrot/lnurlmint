# Phase 2 Research: Mint + Melt Vertical MVP

**Researched:** 2026-08-28
**Confidence:** HIGH — all source patterns verified against `~/lnurl-mint` and all LNbits APIs verified against `~/lnbits`; exception types, tristate semantics, and task lifecycle confirmed live.

---

## RQ1: Note CRUD State Machine

### Source pattern

The source `NoteStore` (`~/lnurl-mint/lnurl_mint/db.py`) uses a module-level `threading.Lock` + a single `sqlite3.Connection` with `with self._lock, self.conn:` context managers for atomicity. Every state-machine operation runs under the lock in one sqlite transaction (the `with self.conn:` context commits on exit).

**`settle_mint`** (db.py:194-215) — compare-and-set mint materialization:
```python
def settle_mint(self, payment_hash: str) -> int | None:
    with self._lock, self.conn:
        cursor = self.conn.execute(
            "UPDATE mints SET minted = 1 WHERE payment_hash = ? AND minted = 0", (payment_hash,)
        )
        if cursor.rowcount != 1:
            return None  # concurrent request already settled
        row = self.conn.execute(
            "SELECT amount_msat, comment_hash FROM mints WHERE payment_hash = ?", (payment_hash,)
        ).fetchone()
        amount_msat, comment_hash = row
        note_id = comment_hash if comment_hash is not None else payment_hash
        self.conn.execute("INSERT INTO notes (id, amount_msat) VALUES (?, ?)", (note_id, amount_msat))
        return amount_msat
```

**`mark_pending`** (db.py:332-353) — reserve notes for in-flight melt:
```python
def mark_pending(self, note_ids: list[str], payment_hash: str) -> None:
    with self._lock, self.conn:
        for note_id in note_ids:
            row = self.conn.execute("SELECT pending FROM notes WHERE id = ? AND spent = 0", (note_id,)).fetchone()
            if row is None:
                raise ValueError("Invalid or already spent k1.")
            if row[0]:
                raise PendingNoteError("pending")
        for note_id in note_ids:
            self.conn.execute(
                "UPDATE notes SET pending = 1, pending_payment_hash = ? WHERE id = ?", (payment_hash, note_id)
            )
```

**`finalize_melt`** (db.py:355-364) — burn notes for good:
```python
def finalize_melt(self, note_ids: list[str]) -> None:
    with self._lock, self.conn:
        for note_id in note_ids:
            self.conn.execute(
                "UPDATE notes SET spent = 1, pending = 0, pending_payment_hash = NULL WHERE id = ?", (note_id,)
            )
```

**`restore`** (db.py:366-373) — release reservation after confirmed failure:
```python
def restore(self, note_ids: list[str]) -> None:
    with self._lock, self.conn:
        for note_id in note_ids:
            self.conn.execute("UPDATE notes SET pending = 0, pending_payment_hash = NULL WHERE id = ?", (note_id,))
```

**`pending_melts`** (db.py:375-392) — query stranded pending notes:
```python
def pending_melts(self) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for note_id, payment_hash in self.conn.execute(
        "SELECT id, pending_payment_hash FROM notes WHERE pending = 1 AND spent = 0"
    ):
        if payment_hash is not None:
            grouped.setdefault(payment_hash, []).append(note_id)
    return grouped
```

**`record_melt`** (db.py:287-298) — record melt invoice for verify:
```python
def record_melt(self, payment_hash: str, pr: str) -> None:
    with self._lock, self.conn:
        self.conn.execute("INSERT OR IGNORE INTO melts (payment_hash, pr) VALUES (?, ?)", (payment_hash, pr))
```

**`mark_melt_settled`** (db.py:306-323):
```python
def mark_melt_settled(self, payment_hash: str) -> None:
    with self._lock, self.conn:
        self.conn.execute("UPDATE melts SET settled = 1 WHERE payment_hash = ?", (payment_hash,))
```

### LNbits target

LNbits `Database` (`~/lnbits/lnbits/db.py`) provides `async with db.connect() as conn:` for multi-statement atomicity. The `Connection` object's `execute()` commits per call (line 288: `await self.conn.commit()`), but the `async with db.connect()` block holds a single `asyncio.Lock` throughout (line 316: `await self.lock.acquire()`), serializing all access. This replaces the source's `threading.Lock` + `with self.conn:` pattern.

Key `Connection` methods (from Phase 1 research, verified):
- `await conn.execute(query, values)` → returns result with `.rowcount`
- `await conn.fetchone(query, values, model=None)` → returns row or None
- `await conn.fetchall(query, values, model=None)` → returns list

### Port mapping

Each source method maps to an `async` function in `crud.py` using `async with db.connect() as conn:`:

```python
async def settle_mint(payment_hash: str) -> int | None:
    """Compare-and-set: UPDATE mints_records SET minted=1 WHERE minted=0,
    then INSERT note. Returns net_amount_msat or None if already settled."""
    async with db.connect() as conn:
        result = await conn.execute(
            "UPDATE lnurlmint.mints_records SET minted = 1 "
            "WHERE payment_hash = :ph AND minted = 0",
            {"ph": payment_hash},
        )
        if result.rowcount != 1:
            return None
        row = await conn.fetchone(
            "SELECT amount_msat, comment_hash FROM lnurlmint.mints_records "
            "WHERE payment_hash = :ph",
            {"ph": payment_hash},
        )
        if not row:
            return None
        amount_msat = row["amount_msat"]
        comment_hash = row["comment_hash"]
        note_id = comment_hash if comment_hash is not None else payment_hash
        await conn.execute(
            "INSERT INTO lnurlmint.notes (id, mint_id, amount_msat, spent, pending) "
            "VALUES (:id, :mint_id, :amount, 0, 0)",
            {"id": note_id, "mint_id": ..., "amount": amount_msat},
        )
        return amount_msat
```

**Critical schema difference:** The source's `mints` table (pending mint invoices) maps to our `mints_records` table. The source's `notes` table maps to our `notes` table. Our `notes` table has additional columns (`mint_id`, `comment_hash`, `created_at`) that the source's doesn't — these must be populated on INSERT. The `mint_id` must be fetched from `mints_records` in the same transaction (it's already in the row).

**`mark_pending`** — all-or-nothing validation + update in one transaction:
```python
async def mark_pending(note_ids: list[str], payment_hash: str) -> None:
    """Reserve notes for an in-flight melt. Raises PendingNoteError if any
    note is already pending, ValueError if invalid/spent."""
    async with db.connect() as conn:
        for note_id in note_ids:
            row = await conn.fetchone(
                "SELECT pending FROM lnurlmint.notes WHERE id = :id AND spent = 0",
                {"id": note_id},
            )
            if row is None:
                raise ValueError("Invalid or already spent k1.")
            if row["pending"]:
                raise PendingNoteError("pending")
        for note_id in note_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes SET pending = 1, pending_payment_hash = :ph "
                "WHERE id = :id",
                {"ph": payment_hash, "id": note_id},
            )
```

**`finalize_melt`**, **`restore`**, **`pending_melts`**, **`record_melt`**, **`mark_melt_settled`** — direct ports, each using `async with db.connect() as conn:` (or `db.execute` for single-statement ops).

### Gotchas

1. **`mint_id` on notes INSERT** — the source's `notes` table has no `mint_id` column; ours does (FK to `mints`). `settle_mint` must fetch `mint_id` from `mints_records` (already in the SELECT row) and include it in the INSERT. For `swap` (Phase 3), the `mint_id` must be derived from the burned notes.
2. **`conn.execute` commits per call** — unlike the source's `with self.conn:` (one transaction), LNbits' `conn.execute` commits after each call. The `async with db.connect()` lock prevents concurrent access, but there's no rollback if a later statement in the block fails. The source relies on `with self.conn:` rolling back on exception. **For `mark_pending`'s all-or-nothing validation**, if the validation loop raises before any UPDATE, nothing was written — safe. But if an UPDATE fails mid-loop after some have committed, partial state is written. This is mitigated by the `asyncio.Lock` (no concurrency) and by validating all notes before updating any (same as source). A true rollback would require `conn.rollback()` in an except block — but LNbits' Connection doesn't expose that cleanly. The validate-then-update pattern is safe as long as validation is complete before any mutation.
3. **`PendingNoteError`** — must be defined in `crud.py` (or a shared errors module) and imported by the router. The source defines it in `db.py`.
4. **`row["pending"]`** — LNbits returns rows as dict-like objects; `row["pending"]` is an `int` (0/1), not a `bool`. The `if row["pending"]:` check works (0 is falsy, 1 is truthy) but be aware of the type.
5. **`result.rowcount`** — SQLAlchemy's `Result.rowcount` is reliable for UPDATE on both SQLite and Postgres. This is the compare-and-set backstop.

---

## RQ2: LNbits Payment Services API

### Source pattern

The source (`~/lnurl-mint/lnurl_mint/node.py`) uses direct lnd/cln REST calls:
- `create_invoice(amount_msat, config, memo) -> tuple[str, bytes]` — returns `(payment_request, preimage)`
- `pay_invoice(invoice, config, fee_limit_msat) -> PaymentResult` — returns `PaymentResult(preimage, fee_msat)`, raises `PaymentFailed` on clean failure, raises `ValueError`/`httpx.HTTPStatusError` on ambiguous failure
- `is_invoice_settled(payment_hash, config) -> bool`
- `is_payment_complete(payment_hash, config) -> bool` — tristate: returns `True`/`False` for terminal states, **raises** for non-terminal (in-flight/hodl)

The source generates its own preimage (`urandom(32)`) and passes it to lnd/cln, so it always knows the preimage. The payment hash is `sha256(preimage).hexdigest()`.

### LNbits target

LNbits (`~/lnbits/lnbits/core/services/payments.py`) provides:

**`create_invoice`** (line 247-353):
```python
async def create_invoice(
    *,
    wallet_id: str,
    amount: float,           # in SATOSHIS (not msat)
    currency: str | None = "sat",
    memo: str,
    description_hash: bytes | None = None,
    unhashed_description: bytes | None = None,
    expiry: int | None = None,
    extra: dict | None = None,
    webhook: str | None = None,
    internal: bool | None = False,
    payment_hash: str | None = None,   # can supply own hash (hold invoice)
    labels: list[str] | None = None,
    conn: Connection | None = None,
) -> Payment
```
Returns a `Payment` object with:
- `payment.bolt11` — the BOLT11 invoice string
- `payment.payment_hash` — the payment hash
- `payment.checking_id` — the checking ID (= payment_hash for most backends)
- `payment.preimage` — the preimage (if the backend returned one; FakeWallet returns it, real backends may not)

**Key:** `amount` is in **satoshis**, not msat. The source uses msat throughout. Convert: `amount_sat = amount_msat // 1000`.

**`pay_invoice`** (line 58-109):
```python
async def pay_invoice(
    *,
    wallet_id: str,
    payment_request: str,
    max_sat: int | None = None,       # max amount in satoshis
    extra: dict | None = None,
    description: str = "",
    tag: str = "",
    labels: list[str] | None = None,
    conn: Connection | None = None,
) -> Payment
```
Raises `PaymentError` on failure (with `.status` = "failed" or "pending"). Returns `Payment` on success with `payment.status == PaymentState.SUCCESS.value` ("success").

**Key behavior:** `pay_invoice` internally calls `_pay_external_invoice` which spawns a background task (`_fundingsource_pay_invoice`) with a timeout (`settings.lnbits_funding_source_pay_invoice_wait_seconds`). If the timeout fires, the payment is returned as **pending** (not raised) — the actual payment may still complete in the background. If the backend reports failure, `PaymentError` is raised with `status="failed"`.

**`check_payment_status`** (line 628-647):
```python
async def check_payment_status(
    payment: Payment,
    skip_internal_payment_notifications: bool | None = False
) -> PaymentStatus
```
Returns `PaymentStatus` (NamedTuple from `~/lnbits/lnbits/wallets/base.py:71`):
```python
class PaymentStatus(NamedTuple):
    paid: bool | None = None     # True=success, False=failed, None=pending
    fee_msat: int | None = None
    preimage: str | None = None

    @property
    def success(self) -> bool: return self.paid is True
    @property
    def pending(self) -> bool: return self.paid is not True   # NOTE: not True, not "is None"
    @property
    def failed(self) -> bool: return self.paid is False
```

**`check_transaction_status`** (line 613-625) — combines DB lookup + live check:
```python
async def check_transaction_status(
    wallet_id: str, payment_hash: str, conn: Connection | None = None
) -> PaymentStatus
```
Looks up the payment in `apipayments` by `wallet_id` + `payment_hash`. If not found → `PaymentPendingStatus()`. If already success → `PaymentSuccessStatus(fee_msat=payment.fee)`. Otherwise → `check_payment_status(payment)` (live backend check).

**`PaymentState`** (enum, `~/lnbits/lnbits/core/models/payments.py:22`):
```python
class PaymentState(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
```

**`PaymentError`** (`~/lnbits/lnbits/exceptions.py:16`):
```python
class PaymentError(Exception):
    def __init__(self, message: str, status: str = "pending"):
        self.message = message
        self.status = status
```

**`get_standalone_payment`** (`~/lnbits/lnbits/core/crud/payments.py:35`) — look up by checking_id OR payment_hash:
```python
async def get_standalone_payment(
    checking_id_or_hash: str,
    incoming: bool | None = False,
    wallet_id: str | None = None,
    conn: Connection | None = None,
) -> Payment | None
```

### Port mapping

**Mint invoice creation** — source's `create_invoice(amount_msat, config, memo)` → LNbits' `create_invoice(wallet_id=mint.wallet, amount=amount_msat//1000, memo=...)`:
```python
from lnbits.core.services.payments import create_invoice as lnbits_create_invoice

payment = await lnbits_create_invoice(
    wallet_id=mint.wallet,
    amount=amount_msat // 1000,   # msat → sat
    memo=f"lnurlcash mint on {mint.username}",
    extra={"lnurlmint": "mint", "mint_id": mint.id},
)
pr = payment.bolt11
payment_hash = payment.payment_hash
# preimage: LNbits' create_invoice may or may not return one.
# FakeWallet returns it (payment.preimage). Real backends may not.
# For the no-comment case, the note id = payment_hash (= sha256(preimage)),
# so we don't NEED the preimage — we need the payment_hash, which we have.
```

**Critical difference:** The source generates its own preimage and thus knows it. LNbits' `create_invoice` delegates to the funding source's `create_invoice`, which generates its own preimage internally. We get `payment_hash` back but NOT the preimage (unless using FakeWallet or `internal=True`). This is actually fine for the no-comment case: the note's id is `sha256(preimage) = payment_hash`, and we store the `payment_hash` in `mints_records`. The preimage reaches the buyer through the Lightning payment itself. For comment-protected mints (Phase 4), the note id is the comment hash, not the preimage — also fine.

**Melt payment** — source's `pay_invoice(pr, config, fee_limit_msat)` → LNbits' `pay_invoice(wallet_id=mint.wallet, payment_request=pr, max_sat=total_msat//1000)`:
```python
from lnbits.core.services.payments import pay_invoice as lnbits_pay_invoice
from lnbits.exceptions import PaymentError

try:
    payment = await lnbits_pay_invoice(
        wallet_id=wallet_id,
        payment_request=pr,
        max_sat=total_msat // 1000,
        description=f"lnurlcash melt",
        tag="lnurlmint",
    )
    # success — payment.status == "success"
except PaymentError as exc:
    # exc.status is "failed" or "pending"
    # MUST call check_payment_status to determine tristate
    ...
```

**Tristate settlement check** — source's `is_payment_complete(ph, config)` → LNbits' `check_transaction_status(wallet_id, payment_hash)`:
```python
from lnbits.core.services.payments import check_transaction_status

status = await check_transaction_status(wallet_id, payment_hash)
# status.paid: True = settled, False = failed, None = pending
# status.success → True (paid=True)
# status.failed → True (paid=False)
# status.pending → True (paid is not True — includes both None and False!)
```

**IMPORTANT GOTCHA:** `PaymentStatus.pending` returns `self.paid is not True`, which means **`pending` is True for both `paid=None` AND `paid=False`**. Do NOT use `status.pending` to distinguish the tristate. Use `status.success`, `status.failed`, and the `paid is None` check directly:
```python
if status.success:      # paid is True → finalize
    ...
elif status.failed:     # paid is False → restore
    ...
else:                   # paid is None → leave pending
    ...
```

### Gotchas

1. **`amount` is in satoshis, not msat** — `create_invoice` and `pay_invoice`'s `max_sat` take satoshis. The source uses msat throughout. All fee math stays in msat; convert only at the LNbits API boundary.
2. **No `fee_limit_msat` parameter** — LNbits' `pay_invoice` takes `max_sat` (a max amount cap, not a fee limit). The source's `_melt_fee_limit_msat` (routing fee budget) has no direct equivalent. LNbits internally uses `fee_reserve(amount_msat)` for its own fee reserve. The melt fee limit from the source is a protocol contract (ECON-04) but LNbits doesn't expose a per-payment fee limit parameter. **Resolution:** The fee limit is enforced by the mint's own accounting (the mint fee withheld at mint time covers routing). LNbits' `pay_invoice` uses its own `fee_reserve` logic. We lose the ability to set a custom fee limit per payment, but the mint fee ensures the wallet has enough balance. Document this as a known deviation.
3. **`pay_invoice` timeout behavior** — LNbits' `pay_invoice` may return a pending `Payment` (not raise) if the backend doesn't respond within `lnbits_funding_source_pay_invoice_wait_seconds`. This maps to the source's ambiguous failure case. The returned `Payment` has `status == "pending"`. We must check `payment.status` after a successful return too, not just catch exceptions.
4. **`PaymentError.status`** — can be `"failed"` or `"pending"`. A `"pending"` PaymentError means the payment might still complete. A `"failed"` PaymentError means the backend reported failure. Both must be confirmed via `check_payment_status` before restoring.
5. **Preimage availability** — `create_invoice` returns `payment.preimage` which may be `None` for real backends. For the no-comment mint case, we don't need it (note id = payment_hash). For verify (Phase 4), we fetch it live via `check_payment_status` → `status.preimage`. This aligns with the store-hashes-not-secrets policy.
6. **`check_transaction_status` needs `wallet_id`** — the source's `is_payment_complete` takes only `payment_hash` + config. LNbits requires `wallet_id` to look up the payment in `apipayments`. This is fine — we always know the mint's `wallet_id`. But for reconcile, we need to map each pending note's `mint_id` → `mints.wallet` to get the `wallet_id`.

---

## RQ3: LUD-06 payRequest Endpoint

### Source pattern

`get_lnaddress` (`~/lnurl-mint/lnurl_mint/router.py:497-529`) — the `.well-known/lnurlp/{username}` endpoint:
```python
@router.get("/.well-known/lnurlp/{username}", tags=["lnurlcash"])
def get_lnaddress(req: Request, username: str) -> LnurlPayResponse:
    if not _known_username(username):
        raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown user.")
    base, host = settings.public_base_url_and_host(str(req.base_url))
    metadata_entries = [
        ["text/plain", f"Mint an lnurlcash bearer note on {host}"],
        ["text/identifier", f"{username}@{host}"],
    ]
    if settings.base_fee_msat or settings.fee_percent_ppm:
        metadata_entries.append(["text/plain", f"Mint fees: {settings.base_fee_msat},{settings.fee_percent_ppm}"])
    metadata = json.dumps(metadata_entries)
    return LnurlPayResponse(
        callback=f"{base}/p/cb",
        minSendable=_min_sendable_msat(),
        maxSendable=settings.max_sendable_msat,
        metadata=metadata,
        withdrawLink=f"{base}/w",
    )
```

The `LnurlPayResponse` model (`~/lnurl-mint/lnurl_mint/models.py:6-23`):
```python
class LnurlPayResponse(BaseModel):
    tag: Literal["payRequest"] = "payRequest"
    callback: str
    minSendable: int
    maxSendable: int
    metadata: str
    withdrawLink: str
    commentAllowed: int = 64
```

### LNbits target

The port uses per-mint endpoints (not `.well-known`). The payRequest is served at `GET /lnurlmint/lnurlp/{mint_id}`. No auth (public LNURL endpoint). The callback URL is `GET /lnurlmint/p/cb/{mint_id}`.

The `LnurlPayResponse` model from the source is a custom extension (adds `withdrawLink`, `commentAllowed`). The `lnurl` library (used by giftcards) has its own `LnurlPayResponse` but it doesn't include `withdrawLink`. **We must define our own pydantic model** (as the source does) rather than using the `lnurl` library's version.

### Port mapping

```python
@lnurlmint_lnurl_router.get("/lnurlp/{mint_id}")
async def get_payrequest(mint_id: str, request: Request) -> dict:
    mint = await get_mint_by_id(mint_id)  # no wallet scope — public endpoint
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}
    if mint.sunset_mint:
        return {"status": "ERROR", "reason": "This mint is sunsetting - minting is disabled."}
    base = _public_base_url(request, mint)
    metadata_entries = [
        ["text/plain", f"Mint an lnurlcash bearer note on {mint.username}"],
        ["text/identifier", f"{mint.username}@{base}"],  # no LUD-16 address in v1
    ]
    if mint.base_fee_msat or mint.fee_percent_ppm:
        metadata_entries.append(
            ["text/plain", f"Mint fees: {mint.base_fee_msat},{mint.fee_percent_ppm}"]
        )
    metadata = json.dumps(metadata_entries)
    return {
        "tag": "payRequest",
        "callback": f"{base}/lnurlmint/p/cb/{mint_id}",
        "minSendable": _min_sendable_msat(mint),
        "maxSendable": mint.max_sendable_msat,
        "metadata": metadata,
        "withdrawLink": f"{base}/lnurlmint/w/{mint_id}",
        "commentAllowed": 64,
    }
```

**Fee-aware minSendable/maxSendable** — see RQ10 for exact formulas. The source uses module-level functions reading `settings.*`; the port uses per-mint DB columns (`mint.base_fee_msat`, `mint.fee_percent_ppm`, `mint.min_sendable_msat`, `mint.max_sendable_msat`, `mint.min_mint_msat`).

### Gotchas

1. **No `get_mint_by_id` without wallet scope** — Phase 1's `get_mint` takes `wallet_id` for cross-wallet isolation. The LNURL endpoints are public (no auth), so we need a new `get_mint_by_id(mint_id)` (no wallet filter) in `crud.py`. This is safe because the endpoint only reads public config (fees, limits, username) — it never exposes `mint_privkey`.
2. **`base_url` derivation** — the source uses `settings.public_base_url_and_host()` (Tor-aware, Host-header-spoof-proof). The port uses per-mint `base_url` / `onion_url` columns. For Phase 2, use `_public_base_url(request)` (giftcards pattern: X-Forwarded-Host → request.base_url fallback). Tor-aware substitution is Phase 6. For now, `mint.base_url` takes priority if set, else `_public_base_url(request)`.
3. **`text/identifier`** — the source puts `{username}@{host}` (a LUD-16 Lightning Address). In v1 (no Lightning Address), this should be the mint's identifier. Use `{mint.username}@{host}` or just the mint URL. This is informational metadata.
4. **`maxSendable`** — the source advertises `settings.max_sendable_msat` (the raw setting). The actual max note value is `max_mintable_msat()` = `max_sendable_msat - _mint_fee_msat(max_sendable_msat)`. The source advertises the raw `max_sendable_msat` (what the payer pays), not the net note value. The port should do the same: advertise `mint.max_sendable_msat` (gross amount the payer pays).

---

## RQ4: Mint Callback

### Source pattern

`get_pay_callback` (`~/lnurl-mint/lnurl_mint/router.py:582-642`):
```python
@router.get("/p/cb", tags=["lnurlcash"])
async def get_pay_callback(req: Request, amount: int, comment: str | None = None) -> LnurlPayActionResponse:
    if settings.sunset_mint:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "This mint is sunsetting - minting is disabled.")
    if amount < settings.min_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too low.")
    if amount > settings.max_sendable_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Amount too high.")
    net_amount_msat = amount - _mint_fee_msat(amount)
    if net_amount_msat < settings.min_mint_msat:
        raise HTTPException(HTTPStatus.BAD_REQUEST, f"Amount too low to mint a note (min {settings.min_mint_msat} msat net of fees).")
    comment_hash = comment if comment is not None and HEX32_PATTERN.match(comment) else None
    funding_source = _funding_source()
    pr, preimage = await create_invoice(amount, funding_source)
    payment_hash = sha256(preimage).hexdigest()
    notes.create_mint(payment_hash, pr, net_amount_msat, comment_hash)
    base = settings.public_base_url(str(req.base_url))
    verify = f"{base}/verify/{payment_hash}" if settings.verify_enabled and comment_hash is not None else None
    return LnurlPayActionResponse(pr=pr, verify=verify)
```

The `LnurlPayActionResponse` model:
```python
class LnurlPayActionResponse(BaseModel):
    pr: str
    disposable: Literal[False] = False
    verify: str | None = None
```

### LNbits target

The port endpoint is `GET /lnurlmint/p/cb/{mint_id}?amount={msat}`. No auth (public LNURL callback). Uses LNbits' `create_invoice` to create the invoice on the mint's wallet.

### Port mapping

```python
@lnurlmint_lnurl_router.get("/p/cb/{mint_id}")
async def get_pay_callback(mint_id: str, request: Request, amount: int) -> dict:
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}
    if mint.sunset_mint:
        return {"status": "ERROR", "reason": "This mint is sunsetting - minting is disabled."}
    if amount < mint.min_sendable_msat:
        return {"status": "ERROR", "reason": "Amount too low."}
    if amount > mint.max_sendable_msat:
        return {"status": "ERROR", "reason": "Amount too high."}
    net_amount_msat = amount - _mint_fee_msat(amount, mint)
    if net_amount_msat < mint.min_mint_msat:
        return {"status": "ERROR", "reason": f"Amount too low to mint a note (min {mint.min_mint_msat} msat net of fees)."}

    # Create invoice via LNbits
    from lnbits.core.services.payments import create_invoice as lnbits_create_invoice
    payment = await lnbits_create_invoice(
        wallet_id=mint.wallet,
        amount=amount // 1000,  # msat → sat
        memo=f"lnurlcash mint on {mint.username}",
        extra={"lnurlmint": "mint", "mint_id": mint.id},
    )
    pr = payment.bolt11
    payment_hash = payment.payment_hash

    # Record pending mint
    await create_mint_record(
        payment_hash=payment_hash,
        mint_id=mint.id,
        pr=pr,
        amount_msat=net_amount_msat,  # NET amount (after fee)
        comment_hash=None,  # Phase 4 adds comment protection
    )

    return {"pr": pr, "disposable": False}
```

**Lazy settlement** — the note is NOT materialized here. It's materialized lazily on the first `/w` or `/verify` poll after the invoice settles (see RQ1's `settle_mint`). The settlement check uses `check_transaction_status(mint.wallet, payment_hash)`.

### Gotchas

1. **`amount` is in msat** — LNURL protocol uses msat for `minSendable`/`maxSendable` and the callback `amount` param. LNbits' `create_invoice` takes satoshis. Convert: `amount // 1000`.
2. **`net_amount_msat` stored in `mints_records`** — the source stores `net_amount_msat` (after fee) in the `mints` table. The note is credited with the net amount. The invoice itself is for the full `amount` (what the payer pays). This is correct: the fee is withheld at mint time.
3. **No preimage from LNbits** — the source generates its own preimage and computes `payment_hash = sha256(preimage).hexdigest()`. LNbits' `create_invoice` generates the preimage internally and returns `payment_hash` directly. We use `payment.payment_hash` — no need to compute it. For the no-comment case, the note id = `payment_hash` (same as source).
4. **Error response format** — LNURL errors should return `{"status": "ERROR", "reason": "..."}` with HTTP 200 (not HTTPException). The source uses `HTTPException` which returns a different JSON shape. The port should return LNURL-formatted errors for protocol compliance. Use `JSONResponse(content={"status": "ERROR", "reason": ...})` or return the dict directly (FastAPI serializes it as JSON with 200).
5. **`verify` URL** — omitted in Phase 2 (verify is Phase 4). The `comment` param is also Phase 4. For Phase 2, the callback is simpler: just `amount` → `pr`.

---

## RQ5: LUD-03 withdrawRequest (Informational /w)

### Source pattern

`get_withdraw` (`~/lnurl-mint/lnurl_mint/router.py:713-760`):
```python
@router.get("/w", tags=["lnurlcash"])
async def get_withdraw(req: Request, k1: str, amount: int | None = None) -> LnurlWithdrawResponse:
    resolved = await _resolve_note(k1)
    if resolved is None:
        if HEX32_PATTERN.match(k1) and notes.note_spent(_note_id(k1)):
            raise HTTPException(HTTPStatus.BAD_REQUEST, "Note already spent.")
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Unknown note.")
    note_id, amount_msat = resolved
    if notes.note_pending(note_id):
        raise HTTPException(HTTPStatus.BAD_REQUEST, "pending")
    base, host = settings.public_base_url_and_host(str(req.base_url))
    return LnurlWithdrawResponse(
        callback=f"{base}/w/cb",
        k1=k1,
        minWithdrawable=amount_msat,
        maxWithdrawable=amount_msat,
        defaultDescription=f"lnurlcash bearer note on {host}",
        mintPubkey=await mint_pubkey(settings.funding_source()),
    )
```

The `LnurlWithdrawResponse` model:
```python
class LnurlWithdrawResponse(BaseModel):
    tag: Literal["withdrawRequest"] = "withdrawRequest"
    callback: str
    k1: str
    minWithdrawable: int
    maxWithdrawable: int
    defaultDescription: str = ""
    mintPubkey: str | None = None
```

**Key behaviors:**
- `k1` is echoed verbatim (the raw secret, never a derived id)
- `amount` query param is accepted but ignored (unauthoritative)
- `minWithdrawable == maxWithdrawable == note value` (authoritative)
- Pending notes are rejected with `"pending"` reason (SEC-04)
- Spent notes are rejected with `"Note already spent."`
- Unknown notes are rejected with `"Unknown note."`
- `mintPubkey` is advertised (Phase 5 for the port — per-mint keypair)

The `_resolve_note` function (router.py:414-420) lazily materializes the note:
```python
async def _resolve_note(k1: str) -> tuple[str, int] | None:
    if not HEX32_PATTERN.match(k1):
        return None
    note_id = _note_id(k1)  # sha256(k1) hex
    amount_msat = await _note_amount_by_id(note_id)
    return (note_id, amount_msat) if amount_msat is not None else None
```

`_note_amount_by_id` (router.py:392-411) checks the DB first, then lazily settles:
```python
async def _note_amount_by_id(note_id: str) -> int | None:
    amount_msat = notes.note_amount(note_id)  # DB lookup: outstanding note
    if amount_msat is not None:
        return amount_msat
    if await _mint_settled(note_id):  # lazy settlement check
        return notes.note_amount(note_id)
    if await _mint_settled_by_comment(note_id):  # comment-protected (Phase 4)
        return notes.note_amount(note_id)
    return None
```

### LNbits target

The port endpoint is `GET /lnurlmint/w/{mint_id}?k1=...`. No auth. The `mint_id` in the path identifies which mint this note belongs to (the source has a single global mint; the port has per-wallet mints). The `k1` is the bearer secret.

### Port mapping

```python
@lnurlmint_lnurl_router.get("/w/{mint_id}")
async def get_withdraw(mint_id: str, request: Request, k1: str, amount: int | None = None) -> dict:
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}
    if not HEX32_PATTERN.match(k1):
        return {"status": "ERROR", "reason": "Unknown note."}
    note_id = sha256(bytes.fromhex(k1)).hexdigest()

    # Try DB first, then lazy-settle
    note = await get_note(note_id, mint_id)
    if note is None:
        # Lazy settlement: check if the mint invoice has settled
        settled = await _try_settle_mint(note_id, mint)
        if settled:
            note = await get_note(note_id, mint_id)
    if note is None:
        # Check if it's a spent note (for distinct error message)
        if await note_is_spent(note_id, mint_id):
            return {"status": "ERROR", "reason": "Note already spent."}
        return {"status": "ERROR", "reason": "Unknown note."}

    if note.pending:
        return {"status": "ERROR", "reason": "pending"}

    base = _public_base_url(request, mint)
    return {
        "tag": "withdrawRequest",
        "callback": f"{base}/lnurlmint/w/cb/{mint_id}",
        "k1": k1,  # echoed verbatim
        "minWithdrawable": note.amount_msat,
        "maxWithdrawable": note.amount_msat,
        "defaultDescription": f"lnurlcash bearer note on {mint.username}",
        # mintPubkey: Phase 5
    }
```

**Lazy settlement** (`_try_settle_mint`):
```python
async def _try_settle_mint(note_id: str, mint: Mint) -> bool:
    """Check if the mint invoice for this note_id (= payment_hash) has settled.
    If so, materialize the note via settle_mint. Returns True if settled."""
    # Check if there's a pending mint record for this payment_hash
    record = await get_pending_mint_record(note_id, mint.id)
    if record is None:
        return False
    # Check settlement via LNbits
    from lnbits.core.services.payments import check_transaction_status
    status = await check_transaction_status(mint.wallet, note_id)
    if not status.success:
        return False
    # Materialize the note (compare-and-set)
    net_amount = await settle_mint(note_id)
    return net_amount is not None
```

### Gotchas

1. **`mint_id` in path** — the source has a single mint, so `/w` doesn't need a mint identifier. The port needs `mint_id` in the path to scope the note lookup. This means the LNURL withdraw URL encodes both the mint_id and the k1: `/lnurlmint/w/{mint_id}?k1=...`. This is a deviation from the source's bare `/w?k1=...` but necessary for multi-tenancy.
2. **`k1` is the raw secret** — it's echoed verbatim in the response. It's never stored (only `sha256(k1)` = `note_id` is stored). This is the store-hashes-not-secrets policy.
3. **Lazy settlement on `/w`** — the first `/w` (or `/verify`) poll after settlement materializes the note. This is the source's pattern. The port must do the same: check `mints_records` for a pending mint with `payment_hash = note_id`, check settlement, call `settle_mint`.
4. **`note_id = payment_hash`** — for the no-comment case, the note's id IS the payment hash of the funding invoice. This is how `_resolve_note` finds the pending mint record: `note_id` = `sha256(k1)` = `sha256(preimage)` = `payment_hash`.
5. **Pending rejection** — the source raises `HTTPException(400, "pending")`. The port should return `{"status": "ERROR", "reason": "pending"}` for LNURL compliance. The source's `LnurlErrorResponseHandler` (router.py:39) converts HTTPException to LNURL error format. The port should do this directly.
6. **No `mintPubkey` in Phase 2** — the source advertises `mintPubkey` (the node's identity key). The port uses per-mint keypair (Phase 5). In Phase 2, omit `mintPubkey` (set to None / exclude from response).

---

## RQ6: Melt Callback

### Source pattern

`get_withdraw_callback` (`~/lnurl-mint/lnurl_mint/router.py:763-911`) — the melt branch (pr is not None):
```python
@router.get("/w/cb", tags=["lnurlcash"])
async def get_withdraw_callback(
    req: Request,
    background_tasks: BackgroundTasks,
    k1: list[str] = Query(...),
    pr: str | None = None,
    amount: int | None = None,
    h: str | None = None,
    h2: str | None = None,
) -> WithdrawSuccessResponse:
    # ... validation ...
    if pr is not None and (len(k1) > 1 or amount is not None):
        raise HTTPException(400, "pr cannot be combined with multiple k1s or amount - merge or split first.")

    # ... resolve notes, validate amounts ...
    if pr is not None:
        decoded = bolt11.decode(pr)
        if decoded.amount_msat != total_msat:
            raise HTTPException(400, f"Invoice must be for exactly {total_msat} msat.")
        # Reject self-mint invoice
        if decoded.has_payment_hash and notes.mint_pr(decoded.payment_hash) is not None:
            raise HTTPException(400, "Cannot melt into an invoice this mint issued itself.")
        # Reject duplicate melt payment hash
        if decoded.has_payment_hash and notes.melt_pr(decoded.payment_hash) is not None:
            raise HTTPException(400, "Invoice already used by an earlier melt - use a fresh one.")

        funding_source = _funding_source()
        try:
            notes.mark_pending(note_ids, decoded.payment_hash)
        except PendingNoteError:
            raise HTTPException(400, "pending")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        _track_melt_start(decoded.payment_hash)  # register in-flight BEFORE response
        try:
            if decoded.has_payment_hash:
                notes.record_melt(decoded.payment_hash, pr)
            background_tasks.add_task(_melt_pay, note_ids, pr, decoded, funding_source)
        except Exception:
            _track_melt_end(decoded.payment_hash)  # never scheduled — drop registration
            raise
        return WithdrawSuccessResponse()

    # ... rotate/split/merge branches (Phase 3) ...
```

### LNbits target

The port endpoint is `GET /lnurlmint/w/cb/{mint_id}?k1=...&pr=...`. No auth. Uses FastAPI's `BackgroundTasks` for the async melt payment (same as source). The `k1` parameter can be repeated (`k1=a&k1=b` for merge — Phase 3; single k1 for melt — Phase 2).

### Port mapping

```python
@lnurlmint_lnurl_router.get("/w/cb/{mint_id}")
async def get_withdraw_callback(
    mint_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    k1: list[str] = Query(...),
    pr: str | None = None,
    amount: int | None = None,
    h: str | None = None,
    h2: str | None = None,
) -> dict:
    mint = await get_mint_by_id(mint_id)
    if mint is None:
        return {"status": "ERROR", "reason": "Unknown mint."}

    # Validation: pr MUST NOT combine with multiple k1s or amount
    if pr is not None and (len(k1) > 1 or amount is not None):
        return {"status": "ERROR", "reason": "pr cannot be combined with multiple k1s or amount - merge or split first."}

    # h required when pr is absent (Phase 3 validates this for rotate/split/merge)
    if pr is None:
        if h is None or not HEX32_PATTERN.match(h):
            return {"status": "ERROR", "reason": "missing h"}
        # Phase 3: rotate/split/merge
        ...

    # Melt branch (pr is not None, single k1)
    # Resolve the note
    note_k1 = k1[0]
    if not HEX32_PATTERN.match(note_k1):
        return {"status": "ERROR", "reason": "Invalid or already spent k1."}
    note_id = sha256(bytes.fromhex(note_k1)).hexdigest()
    note = await _resolve_and_materialize_note(note_id, mint)
    if note is None:
        return {"status": "ERROR", "reason": "Invalid or already spent k1."}
    total_msat = note.amount_msat

    # Validate invoice
    try:
        decoded = bolt11.decode(pr)
    except Exception as exc:
        return {"status": "ERROR", "reason": f"Invalid invoice: {exc!s}"}
    if decoded.amount_msat != total_msat:
        return {"status": "ERROR", "reason": f"Invoice must be for exactly {total_msat} msat."}

    # Reject self-mint invoice (payment hash in mints_records)
    if decoded.has_payment_hash and await mint_record_exists(decoded.payment_hash):
        return {"status": "ERROR", "reason": "Cannot melt into an invoice this mint issued itself."}

    # Reject duplicate melt payment hash (SEC-06)
    if decoded.has_payment_hash and await melt_record_exists(decoded.payment_hash):
        return {"status": "ERROR", "reason": "Invoice already used by an earlier melt - use a fresh one."}

    # Atomically mark pending
    try:
        await mark_pending([note_id], decoded.payment_hash)
    except PendingNoteError:
        return {"status": "ERROR", "reason": "pending"}
    except ValueError as exc:
        return {"status": "ERROR", "reason": str(exc)}

    # Register in-flight BEFORE scheduling background task
    _track_melt_start(decoded.payment_hash)
    try:
        if decoded.has_payment_hash:
            await record_melt(decoded.payment_hash, pr, mint.id)
        background_tasks.add_task(_melt_pay, [note_id], pr, decoded, mint)
    except Exception:
        _track_melt_end(decoded.payment_hash)
        raise

    return {"status": "OK"}
```

### Gotchas

1. **`k1: list[str] = Query(...)`** — FastAPI collects repeated query params into a list. `?k1=a&k1=b` → `["a", "b"]`. Single `?k1=a` → `["a"]`. This matches the source's pattern.
2. **Self-mint rejection** — the source checks `notes.mint_pr(decoded.payment_hash)` (the `mints` table). The port checks `mints_records` for the payment hash. This prevents paying an invoice this mint itself issued (a cycle).
3. **Duplicate melt rejection (SEC-06)** — the source checks `notes.melt_pr(decoded.payment_hash)` (the `melts` table). The port checks `melts` for the payment hash. `record_melt` is unconditional (INSERT OR IGNORE), so even a failed melt keeps its row.
4. **`_track_melt_start` before `background_tasks.add_task`** — the in-flight registration MUST happen before the response goes out and the background task starts. This prevents a reconcile race (TEST-04). The `try/except` around `background_tasks.add_task` drops the registration if scheduling fails.
5. **`background_tasks.add_task`** — FastAPI runs background tasks after the response is sent. This is the same mechanism the source uses. The task function `_melt_pay` must never raise (it handles all exceptions internally).
6. **`bolt11.decode`** — available in LNbits (it imports `bolt11` in `payments.py`). The decoded invoice has `.amount_msat`, `.payment_hash`, `.has_payment_hash`.

---

## RQ7: Tristate Settlement in _melt_pay

### Source pattern

`_melt_pay` (`~/lnurl-mint/lnurl_mint/router.py:126-201`):
```python
async def _melt_pay(note_ids, pr, decoded, funding_source) -> None:
    amount_msat = decoded.amount_msat
    try:
        try:
            result = await pay_invoice(pr, funding_source, _melt_fee_limit_msat(amount_msat))
        except Exception as exc:
            if not decoded.has_payment_hash:
                log_internal_error(f"melt {note_ids}: error paying invoice, nothing to confirm against - left pending", exc)
                return
            completed = await _confirm_payment(decoded.payment_hash, funding_source)
            if completed is None:
                log_internal_error(f"melt {note_ids}: could not confirm payment status after retries - left pending", exc)
                return
            if not completed:
                logging.info("melt %s: confirmed not paid (%s) - restoring", note_ids, exc)
                notes.restore(note_ids)
                return
            notes.finalize_melt(note_ids)
            notes.mark_melt_settled(decoded.payment_hash)
            log_melt(note_ids, amount_msat, None)
            return

        # pay_invoice succeeded
        notes.finalize_melt(note_ids)
        if decoded.has_payment_hash:
            notes.mark_melt_settled(decoded.payment_hash)
        log_melt(note_ids, amount_msat, result.fee_msat)
    finally:
        _track_melt_end(decoded.payment_hash)
```

`_confirm_payment` (router.py:98-123):
```python
async def _confirm_payment(payment_hash, funding_source, delays=None) -> bool | None:
    if delays is None:
        delays = _CONFIRMATION_RETRY_DELAYS_SECONDS  # (1, 2, 4, 8, 16)
    for delay in (0, *delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await is_payment_complete(payment_hash, funding_source)
        except Exception as exc:
            logging.warning("confirm payment %s: attempt failed, retrying: %s", payment_hash, exc)
    return None
```

The tristate contract:
- `pay_invoice` succeeds → **finalize** (burn notes)
- `pay_invoice` raises → call `_confirm_payment`:
  - `completed is True` → **finalize** (payment went through despite the raise)
  - `completed is False` → **restore** (confirmed not paid)
  - `completed is None` → **leave pending** (unconfirmable — operator must resolve)

### LNbits target

LNbits' `pay_invoice` raises `PaymentError` on failure. `PaymentError.status` can be `"failed"` or `"pending"`. A `"pending"` status means the payment might still complete (timeout). A `"failed"` status means the backend reported failure.

BUT: `pay_invoice` can also **return** a pending `Payment` (not raise) if the backend doesn't respond within the timeout. In that case `payment.status == "pending"` (or `payment.pending == True`).

`check_transaction_status(wallet_id, payment_hash)` returns `PaymentStatus` with `.paid` being `True`/`False`/`None` — the tristate.

### Port mapping

```python
async def _melt_pay(note_ids: list[str], pr: str, decoded, mint: Mint) -> None:
    """Background melt payment task. Never raises.
    Tristate: paid=True → finalize, paid=False → restore, paid=None → leave pending."""
    amount_msat = decoded.amount_msat
    payment_hash = decoded.payment_hash
    wallet_id = mint.wallet
    try:
        try:
            payment = await lnbits_pay_invoice(
                wallet_id=wallet_id,
                payment_request=pr,
                max_sat=amount_msat // 1000,
                description=f"lnurlcash melt",
                tag="lnurlmint",
            )
            # pay_invoice returned (not raised) — check status
            if payment.status == PaymentState.SUCCESS.value:
                # Success — finalize
                await finalize_melt(note_ids)
                if decoded.has_payment_hash:
                    await mark_melt_settled(payment_hash)
                return
            # payment is pending (timeout) — fall through to confirmation
            raise PaymentError("Payment timed out", status="pending")
        except PaymentError as exc:
            if not decoded.has_payment_hash:
                logger.error(f"melt {note_ids}: error paying invoice, nothing to confirm against - left pending: {exc}")
                return
            completed = await _confirm_payment(payment_hash, wallet_id)
            if completed is None:
                logger.error(f"melt {note_ids}: could not confirm payment status after retries - left pending")
                return
            if not completed:
                logger.info(f"melt {note_ids}: confirmed not paid - restoring")
                await restore(note_ids)
                return
            # confirmed paid despite the raise
            await finalize_melt(note_ids)
            if decoded.has_payment_hash:
                await mark_melt_settled(payment_hash)
            return
        except Exception as exc:
            # Unexpected exception (not PaymentError) — still confirm
            if not decoded.has_payment_hash:
                logger.error(f"melt {note_ids}: unexpected error - left pending: {exc}")
                return
            completed = await _confirm_payment(payment_hash, wallet_id)
            if completed is None:
                logger.error(f"melt {note_ids}: could not confirm after unexpected error - left pending")
                return
            if not completed:
                await restore(note_ids)
                return
            await finalize_melt(note_ids)
            if decoded.has_payment_hash:
                await mark_melt_settled(payment_hash)
            return
    finally:
        _track_melt_end(payment_hash)


async def _confirm_payment(payment_hash: str, wallet_id: str, delays: tuple[int, ...] | None = None) -> bool | None:
    """Retry check_transaction_status with backoff. Returns True/False/None."""
    if delays is None:
        delays = _CONFIRMATION_RETRY_DELAYS_SECONDS  # (1, 2, 4, 8, 16)
    for delay in (0, *delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            status = await check_transaction_status(wallet_id, payment_hash)
            if status.success:
                return True
            if status.failed:
                return False
            # status.paid is None → still pending, retry
            # BUT: PaymentStatus.pending is True for both None and False.
            # Use status.paid directly:
            if status.paid is None:
                continue  # retry
            # paid is False but status.failed didn't catch it? shouldn't happen
            return False
        except Exception as exc:
            logger.warning(f"confirm payment {payment_hash}: attempt failed, retrying: {exc}")
    return None
```

### Gotchas

1. **`PaymentStatus.pending` is unreliable for tristate** — `pending` returns `self.paid is not True`, which is True for both `paid=None` AND `paid=False`. Use `status.success` (paid is True), `status.failed` (paid is False), and `status.paid is None` (truly pending) directly. This is the single most critical gotcha in the port.
2. **`pay_invoice` can return pending without raising** — LNbits' `pay_invoice` may return a `Payment` with `status="pending"` if the backend times out. The source's `pay_invoice` always raises on failure. The port must check `payment.status` even on a successful return.
3. **`_confirm_payment` maps `is_payment_complete` → `check_transaction_status`** — the source's `is_payment_complete` raises for non-terminal states (hodl). LNbits' `check_transaction_status` returns `PaymentStatus(paid=None)` for pending. The port's `_confirm_payment` treats `paid=None` as "retry" (continue loop), which is equivalent to the source's "raise → retry". If all retries return `None`, the port returns `None` (leave pending) — same as source.
4. **`PaymentError` vs generic `Exception`** — the source catches `Exception` broadly (any raise from `pay_invoice`). The port should catch `PaymentError` (LNbits' payment failure) and also `Exception` (unexpected errors). Both trigger the confirmation path. The `PaymentError.status` field is informational — we always confirm via `check_transaction_status` regardless.
5. **`finally: _track_melt_end`** — the in-flight registration MUST be cleared in `finally`, regardless of outcome. This is the same as the source. If the process crashes, the registration is lost (in-process dict), and reconcile picks up the pending note on restart.
6. **`delays=()` for reconcile** — `_confirm_payment` with `delays=()` does a single attempt (no retries). This is used by `reconcile_pending_melts` to avoid blocking boot for minutes. The default `(1, 2, 4, 8, 16)` (~31s total) is used by `_melt_pay` for live melt attempts. Tests use `delays=()` for speed (monkeypatch `_CONFIRMATION_RETRY_DELAYS_SECONDS`).

---

## RQ8: In-Flight Melt Tracking

### Source pattern

(`~/lnurl-mint/lnurl_mint/router.py:75-95`):
```python
_in_flight_melts: dict[str, int] = {}
_in_flight_melts_lock = threading.Lock()

def _track_melt_start(payment_hash: str) -> None:
    with _in_flight_melts_lock:
        _in_flight_melts[payment_hash] = _in_flight_melts.get(payment_hash, 0) + 1

def _track_melt_end(payment_hash: str) -> None:
    with _in_flight_melts_lock:
        remaining = _in_flight_melts.get(payment_hash, 0) - 1
        if remaining > 0:
            _in_flight_melts[payment_hash] = remaining
        else:
            _in_flight_melts.pop(payment_hash, None)

def _melt_in_flight(payment_hash: str) -> bool:
    with _in_flight_melts_lock:
        return payment_hash in _in_flight_melts
```

The source uses `threading.Lock` because tests drive requests from threads (see `melt_in_background` in conftest.py). The comment (router.py:73-74) says: "All normal access is on the event loop, but the lock keeps the refcount correct when tests drive requests from threads."

### LNbits target

LNbits runs on asyncio (single event loop). The source's `threading.Lock` works but is unnecessary in production (all access is on the event loop). However, tests may use threads or `asyncio.gather` for concurrency.

### Port mapping

**Decision: use `asyncio.Lock`** — LNbits is async-native. The source uses `threading.Lock` only because its tests use threads. The port's tests will use `asyncio.gather` (async-native), so `asyncio.Lock` is the correct choice. However, `asyncio.Lock` requires `async/await` for every access, which is fine since all callers are async.

```python
import asyncio

_in_flight_melts: dict[str, int] = {}
_in_flight_melts_lock = asyncio.Lock()

async def _track_melt_start(payment_hash: str) -> None:
    async with _in_flight_melts_lock:
        _in_flight_melts[payment_hash] = _in_flight_melts.get(payment_hash, 0) + 1

async def _track_melt_end(payment_hash: str) -> None:
    async with _in_flight_melts_lock:
        remaining = _in_flight_melts.get(payment_hash, 0) - 1
        if remaining > 0:
            _in_flight_melts[payment_hash] = remaining
        else:
            _in_flight_melts.pop(payment_hash, None)

async def _melt_in_flight(payment_hash: str) -> bool:
    async with _in_flight_melts_lock:
        return payment_hash in _in_flight_melts
```

**IMPORTANT:** This changes the call sites from sync to async. The melt callback (`get_withdraw_callback`) must `await _track_melt_start(...)` instead of calling it synchronously. The `_melt_pay` `finally` block must `await _track_melt_end(...)`. The `reconcile_pending_melts` must `await _melt_in_flight(...)`.

### Gotchas

1. **`asyncio.Lock` vs `threading.Lock`** — the source uses `threading.Lock` because its tests use `threading.Thread` for the `melt_in_background` fixture. The port's tests will use `asyncio.gather` (async-native), so `asyncio.Lock` is correct. If any test needs to check `_melt_in_flight` from a non-async context, it won't work with `asyncio.Lock`. But all test code should be async.
2. **Registration timing** — `_track_melt_start` MUST be called after `mark_pending` succeeds and BEFORE `background_tasks.add_task`. This prevents the reconcile race (TEST-04). The source does this synchronously; the port must `await` it.
3. **`finally` block** — `_track_melt_end` in the `finally` of `_melt_pay` must be `await`ed. If `_melt_pay` crashes, the `finally` still runs (Python guarantee). If the event loop is shutting down, the `await` might be cancelled — but that's fine, the registration is in-process and lost on restart anyway.
4. **Refcount for shared payment hashes** — two melts of different notes into the same invoice share one payment hash. The refcount ensures the hash stays in-flight until BOTH attempts finish. The source supports this; the port preserves it. (Phase 2 doesn't exercise this — the duplicate-melt guard rejects it — but the refcount pattern is preserved for safety.)

---

## RQ9: Reconcile

### Source pattern

`reconcile_pending_melts` (`~/lnurl-mint/lnurl_mint/router.py:204-254`):
```python
async def reconcile_pending_melts(funding_source: LightningBackendConfig) -> None:
    for payment_hash, note_ids in notes.pending_melts().items():
        if _melt_in_flight(payment_hash):
            continue  # skip live attempts
        completed = await _confirm_payment(payment_hash, funding_source, delays=())
        if completed is None:
            log_internal_error(f"reconcile: melt {note_ids} still unconfirmed at boot - left pending", ...)
            continue
        if completed:
            amount_msat = sum(notes.note_amount(note_id) or 0 for note_id in note_ids)
            notes.finalize_melt(note_ids)
            notes.mark_melt_settled(payment_hash)
            logging.info(f"reconcile: melt {note_ids} confirmed paid at boot - finalized")
            log_melt(note_ids, amount_msat, None)
        else:
            notes.restore(note_ids)
            logging.info(f"reconcile: melt {note_ids} confirmed not paid at boot - restored")
```

Called from `server.py`:
- **Boot-time** (lifespan, line 130): `await _reconcile_pending_melts_safely(funding_source)` — one-shot after confirming funding source is reachable
- **Periodic** (`_monitor_funding_source`, line 70): `await _reconcile_pending_melts_safely(funding_source)` — on every healthy tick

### LNbits target

The port uses `create_permanent_unique_task` + `run_interval` (giftcards pattern) instead of the source's custom monitor loop. Boot-time reconcile runs in `lnurlmint_start()` before the periodic task is registered.

### Port mapping

**`tasks.py`:**
```python
from lnbits.tasks import run_interval

async def wait_for_melt_reconcile() -> None:
    """Periodic reconcile task registered with create_permanent_unique_task."""
    await run_interval(60, reconcile_pending_melts)()
```

**`__init__.py`:**
```python
def lnurlmint_start():
    """Start background tasks."""
    from lnbits.tasks import create_permanent_unique_task
    from .tasks import wait_for_melt_reconcile
    from .services import boot_reconcile

    # Boot-time one-shot reconcile (resolves stranded notes from crashed process)
    # create_permanent_unique_task wraps in catch_everything_and_restart,
    # but we want a one-shot, not a loop. Use asyncio.create_task for the
    # boot reconcile, and create_permanent_unique_task for the periodic loop.
    import asyncio
    asyncio.create_task(boot_reconcile())

    # Periodic reconcile
    task = create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile)
    scheduled_tasks.append(task)
```

**`services.py`:**
```python
async def boot_reconcile() -> None:
    """One-shot reconcile at boot. Guarded against exceptions."""
    try:
        await reconcile_pending_melts()
    except Exception as exc:
        logger.error(f"boot reconcile failed: {exc}")

async def reconcile_pending_melts() -> None:
    """Resolve every note left pending by a crashed/restarted melt."""
    pending = await pending_melts()  # dict[str, list[str]] from crud
    for payment_hash, note_ids in pending.items():
        if await _melt_in_flight(payment_hash):
            continue  # skip live attempts
        # Need wallet_id for check_transaction_status — look up via note's mint
        mint_id = await get_mint_id_for_note(note_ids[0])
        mint = await get_mint_by_id(mint_id)
        if mint is None:
            logger.error(f"reconcile: could not find mint for note {note_ids[0]}")
            continue
        completed = await _confirm_payment(payment_hash, mint.wallet, delays=())
        if completed is None:
            logger.error(f"reconcile: melt {note_ids} still unconfirmed - left pending")
            continue
        if completed:
            await finalize_melt(note_ids)
            await mark_melt_settled(payment_hash)
            logger.info(f"reconcile: melt {note_ids} confirmed paid - finalized")
        else:
            await restore(note_ids)
            logger.info(f"reconcile: melt {note_ids} confirmed not paid - restored")
```

### Gotchas

1. **`pending_melts` needs wallet scoping** — the source's `pending_melts()` returns ALL pending notes (single mint). The port must return pending notes across ALL wallets (reconcile is a system-level task, not wallet-scoped). The `pending_melts` query in `crud.py` should NOT filter by wallet — it scans all pending notes. But each note's `mint_id` → `mints.wallet` must be resolved to get the `wallet_id` for `check_transaction_status`.
2. **`_confirm_payment` needs `wallet_id`** — the source's `_confirm_payment` takes `funding_source` (a single global config). The port takes `wallet_id` (per-mint). Reconcile must resolve each pending note's wallet via `mint_id` → `mints.wallet`.
3. **`delays=()` for reconcile** — single attempt per pending note. The source uses this to avoid blocking boot. The port does the same.
4. **`boot_reconcile` as `asyncio.create_task`** — not `create_permanent_unique_task` (which wraps in a restart loop). Boot reconcile is a one-shot. If it fails, the periodic task will pick up the same notes on the next tick. Using `asyncio.create_task` directly means it runs once and completes. The `catch_everything_and_restart` wrapper would turn it into an infinite loop.
5. **`run_interval` runs `func` then sleeps** — `run_interval(60, reconcile_pending_melts)()` calls `reconcile_pending_melts()` immediately, then sleeps 60s, then repeats. The first tick happens immediately (which is fine — boot_reconcile may have already handled it). The `while settings.lnbits_running` loop in `run_interval` handles shutdown.
6. **Skip health-check integration** — the source's `_monitor_funding_source` probes the funding source and only reconciles on healthy ticks. The port skips this (per CONTEXT.md decision): `create_permanent_unique_task` handles crash-restart, and funding source health is LNbits core's responsibility. If the funding source is down, `check_transaction_status` will return `PaymentPendingStatus()` (paid=None), and reconcile leaves the note pending — same as the source's behavior when the funding source is unreachable.

---

## RQ10: Fee Math

### Source pattern

All fee functions are in `~/lnurl-mint/lnurl_mint/router.py`, reading from `settings.*` (module-level config):

**`_mint_fee_msat`** (router.py:423-432):
```python
def _mint_fee_msat(amount_msat: int) -> int:
    fee_msat = settings.base_fee_msat + (amount_msat * settings.fee_percent_ppm) // 1_000_000
    return -(-fee_msat // 1000) * 1000  # round UP to nearest whole sat
```

**`_min_sendable_msat`** (router.py:435-453):
```python
def _min_sendable_msat() -> int:
    amount_msat = max(settings.min_sendable_msat, settings.min_mint_msat)
    for _ in range(100_000):
        if amount_msat - _mint_fee_msat(amount_msat) >= settings.min_mint_msat:
            return amount_msat
        amount_msat += 1000
    raise RuntimeError("minSendable walk did not terminate - check the fee settings (fee_percent_ppm too high?)")
```

**`max_mintable_msat`** (router.py:456-468):
```python
def max_mintable_msat() -> int:
    return settings.max_sendable_msat - _mint_fee_msat(settings.max_sendable_msat)
```

**`_melt_fee_limit_msat`** (router.py:471-483):
```python
def _melt_fee_limit_msat(amount_msat: int) -> int:
    return max(round(amount_msat * 0.005), 5000, _mint_fee_msat(amount_msat))
```

### LNbits target

The port uses per-mint DB columns instead of `settings.*`. The fee functions take a `Mint` parameter.

### Port mapping

```python
def _mint_fee_msat(amount_msat: int, mint: Mint) -> int:
    """Mint fee: base_fee + ppm, rounded UP to nearest whole sat."""
    fee_msat = mint.base_fee_msat + (amount_msat * mint.fee_percent_ppm) // 1_000_000
    return -(-fee_msat // 1000) * 1000

def _min_sendable_msat(mint: Mint) -> int:
    """Fee-aware minSendable: walk up until net >= min_mint_msat."""
    amount_msat = max(mint.min_sendable_msat, mint.min_mint_msat)
    for _ in range(100_000):
        if amount_msat - _mint_fee_msat(amount_msat, mint) >= mint.min_mint_msat:
            return amount_msat
        amount_msat += 1000
    raise RuntimeError("minSendable walk did not terminate - check fee settings")

def max_mintable_msat(mint: Mint) -> int:
    """Max note value = max_sendable - mint_fee."""
    return mint.max_sendable_msat - _mint_fee_msat(mint.max_sendable_msat, mint)

def _melt_fee_limit_msat(amount_msat: int, mint: Mint) -> int:
    """Melt fee budget: max(0.5%, 5000msat, mint_fee)."""
    return max(round(amount_msat * 0.005), 5000, _mint_fee_msat(amount_msat, mint))
```

### Gotchas

1. **`-(-x // 1000) * 1000` is ceil-rounding to sat** — this is the "round UP" idiom for integers. `-(-x // 1000)` = `ceil(x / 1000)`. Then `* 1000` brings it back to msat. This ensures the mint is never short a sat. Must be preserved exactly.
2. **`_melt_fee_limit_msat` is not directly usable in LNbits** — LNbits' `pay_invoice` doesn't accept a fee limit parameter (it uses its own `fee_reserve`). The fee limit is a protocol contract (ECON-04) but can't be enforced per-payment via LNbits' API. The mint fee withheld at mint time ensures the wallet has enough balance. Document this as a known deviation — the fee limit formula is preserved for accounting/logging but not enforced at the LNbits payment layer.
3. **`_min_sendable_msat` walk termination** — the 100,000 iteration cap is a safety valve. With `fee_percent_ppm` validated to ≤ 100,000 (10%), the walk always terminates quickly. The port preserves this cap.
4. **Per-mint fees** — the source reads `settings.base_fee_msat` etc. (global). The port reads `mint.base_fee_msat` etc. (per-mint DB columns). The fee functions must take `mint` as a parameter. This is a straightforward change but affects every call site.

---

## RQ11: Background Task Lifecycle

### Source pattern

`server.py` lifespan (lines 73-141):
- Boot-time: probe funding source, run `reconcile_pending_melts` if healthy
- Periodic: `asyncio.create_task(_monitor_funding_source(...))` — runs forever, reconciles on every healthy tick
- Shutdown: `monitor_task.cancel()` + `contextlib.suppress(asyncio.CancelledError)`

### LNbits target

**Giftcards pattern** (`~/giftcards/__init__.py:36-42`):
```python
def giftcards_start():
    """Start background tasks."""
    from lnbits.tasks import create_permanent_unique_task
    task = create_permanent_unique_task("ext_giftcards", wait_for_expiry)
    scheduled_tasks.append(task)

def giftcards_stop():
    """Stop background tasks."""
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)
```

**`create_permanent_unique_task`** (`~/lnbits/lnbits/tasks.py:39-42`):
```python
def create_permanent_unique_task(name: str, coro: Callable[[], Coroutine]) -> asyncio.Task:
    return create_unique_task(name, catch_everything_and_restart(coro, name))
```

**`catch_everything_and_restart`** (tasks.py:58-71):
```python
async def catch_everything_and_restart(func, name="unnamed"):
    try:
        return await func()
    except asyncio.CancelledError:
        raise  # must propagate
    except Exception as exc:
        logger.error(f"exception in background task `{name}`:", exc)
        logger.error(traceback.format_exc())
        logger.error("will restart the task in 5 seconds.")
        await asyncio.sleep(5)
        return await catch_everything_and_restart(func, name)
```

**`run_interval`** (tasks.py:152-167):
```python
def run_interval(interval_seconds: int, func: Callable[[], Coroutine]) -> Callable[[], Coroutine]:
    async def wrapper() -> None:
        while settings.lnbits_running:
            try:
                await func()
            except Exception as e:
                logger.error(f"Error occurred in interval task: {e}")
                logger.warning(traceback.format_exc())
            await asyncio.sleep(interval_seconds)
    return wrapper
```

### Port mapping

**`__init__.py`** (updated from Phase 1 stub):
```python
def lnurlmint_start():
    """Start background tasks."""
    from lnbits.tasks import create_permanent_unique_task
    from .tasks import wait_for_melt_reconcile
    from .services import boot_reconcile
    import asyncio

    # Boot-time one-shot reconcile
    asyncio.create_task(boot_reconcile())

    # Periodic reconcile (every 60s)
    task = create_permanent_unique_task("ext_lnurlmint", wait_for_melt_reconcile)
    scheduled_tasks.append(task)

def lnurlmint_stop():
    """Stop background tasks."""
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)
```

**`tasks.py`** (new file):
```python
from lnbits.tasks import run_interval

async def wait_for_melt_reconcile() -> None:
    """Periodic reconcile registered with create_permanent_unique_task."""
    await run_interval(60, reconcile_pending_melts)()
```

### Gotchas

1. **`lnurlmint_start` is sync, not async** — LNbits' loader calls `ext_start()` synchronously. `asyncio.create_task(boot_reconcile())` schedules the coroutine on the running event loop without blocking. This works because `lnurlmint_start` is called after the event loop is running (during app startup).
2. **`create_permanent_unique_task` wraps in `catch_everything_and_restart`** — if `wait_for_melt_reconcile` raises, it restarts after 5s. But `run_interval` already catches exceptions internally (try/except around `await func()`). So `catch_everything_and_restart` is a second safety net — if `run_interval` itself raises (shouldn't happen), it restarts.
3. **`scheduled_tasks` list** — `lnurlmint_stop` cancels all tasks in this list. The boot_reconcile task is NOT in this list (it's a one-shot that completes quickly). If it's still running at shutdown, it'll be cancelled when the event loop closes. This is fine — boot_reconcile is guarded against exceptions.
4. **`run_interval` runs immediately on first tick** — `await func()` is called before the first `asyncio.sleep`. So the periodic reconcile runs immediately at start, then every 60s. This means boot_reconcile and the first periodic tick may overlap. This is safe because `pending_melts` is idempotent (a note already finalized/restored won't be pending anymore). The in-flight check also prevents double-processing.
5. **No `_monitor_funding_source`** — the source's monitor probes the funding source and only reconciles on healthy ticks. The port skips this (per CONTEXT.md). `run_interval` runs regardless of funding source health. If the funding source is down, `check_transaction_status` returns `paid=None`, and reconcile leaves the note pending. This is the correct behavior.

---

## RQ12: PoC Test Fixtures

### Source pattern

**`FakeNode`** (`~/lnurl-mint/tests/conftest.py:81-208`) — monkeypatches `router_module.create_invoice`, `pay_invoice`, `is_payment_complete`, etc. Key controllable behaviors:
- `fail_payments = False` → `pay_invoice` raises `ValueError`
- `fail_reason = None` → `pay_invoice` raises `PaymentFailed(fail_reason)`
- `payment_actually_completed = False` → `is_payment_complete` returns `False` (or raises if `is_payment_complete_raises`)
- `is_payment_complete_raises = False` → `is_payment_complete` raises `ConnectionError`
- `pay_delay = 0.0` → `pay_invoice` sleeps (simulates in-flight payment)

**`HodlNode`** (`~/lnurl-mint/tests/test_melt_restore_double_payout_poc.py:59-123`) — models a hodl invoice:
- `pay_mode = "ambiguous"` → `pay_invoice` raises `ValueError` (stream ended without terminal status), HTLC stays live
- `pay_mode = "failed"` → `pay_invoice` raises `PaymentFailed` (terminal FAILED), HTLC stays live
- `pay_mode = "benign_failed"` → `pay_invoice` raises `PaymentFailed` (no route, no HTLC)
- `is_payment_complete` raises while `pending_hodl` is non-empty (can't confirm either way)
- `settle_hodl_payments()` → reality catches up, `paid_out` updated

**`InFlightNode`** (`~/lnurl-mint/tests/test_poc_reconcile_inflight_race.py:52-93`) — models the pre-registration window:
- `pay_invoice` sets `pay_started` event, waits for `pay_release` event
- `is_payment_complete` returns `False` (what lnd 404 / cln empty listpays report for unregistered payment)

**Test setup** (`conftest.py:184-213`):
- `monkeypatch.setattr(router_module, "create_invoice", fake.create_invoice)` etc.
- `monkeypatch.setattr(router_module, "_CONFIRMATION_RETRY_DELAYS_SECONDS", ())` — no backoff in tests
- `TestClient(app)` — FastAPI test client
- `mint_note` fixture — mints a settled note and returns its k1 (preimage hex)

### LNbits target

The port can't directly use `FakeNode` because it monkeypatches the source's `router_module` functions. The port uses LNbits' `create_invoice` / `pay_invoice` / `check_transaction_status` from `lnbits.core.services.payments`. These internally call `get_funding_source()` which returns the configured wallet (FakeWallet, VoidWallet, etc.).

**Approach (per CONTEXT.md):** Monkeypatch `lnbits.core.services.payments.create_invoice` / `pay_invoice` / `check_transaction_status` (or the funding source itself) with controllable tristate behavior. Do NOT use LNbits' `FakeWallet` directly (can't easily model `paid=None`).

### Port mapping

**FakeNode for LNbits** — a test fixture that monkeypatches the LNbits payment service functions:

```python
class FakeNode:
    """Test fixture that monkeypatches LNbits payment services with
    controllable tristate behavior. Models the same scenarios as the
    source's FakeNode/HodlNode/InFlightNode."""
    def __init__(self):
        self.settled: set[str] = set()
        self.last_payment_hash: str = ""
        self.last_preimage: str = ""
        self.preimages: dict[str, str] = {}  # payment_hash → preimage hex
        self.paid: list[str] = []
        self.fail_payments = False
        self.fail_reason: str | None = None
        self.payment_actually_completed = False
        self.is_payment_complete_raises = False
        self.is_payment_complete_called = False
        self.is_payment_complete_calls = 0
        self.pay_delay = 0.0
        self.pay_started = asyncio.Event()
        self.pay_release = asyncio.Event()

    async def create_invoice(self, *, wallet_id, amount, memo="", **kwargs):
        """Replacement for lnbits.core.services.payments.create_invoice."""
        preimage = urandom(32)
        self.last_preimage = preimage.hex()
        payment_hash = sha256(preimage).hexdigest()
        self.last_payment_hash = payment_hash
        self.preimages[payment_hash] = preimage.hex()
        pr = fake_invoice(amount * 1000, payment_hash)  # amount is in sat
        # Return a Payment-like object
        return Payment(
            checking_id=payment_hash,
            payment_hash=payment_hash,
            wallet_id=wallet_id,
            amount=amount * 1000,
            fee=0,
            bolt11=pr,
            status=PaymentState.PENDING,
            preimage=preimage.hex(),
        )

    async def pay_invoice(self, *, wallet_id, payment_request, **kwargs):
        """Replacement for lnbits.core.services.payments.pay_invoice."""
        if self.pay_delay:
            await asyncio.sleep(self.pay_delay)
        if self.fail_reason is not None:
            raise PaymentError(self.fail_reason, status="failed")
        if self.fail_payments:
            raise PaymentError("Payment failed: no route.", status="failed")
        decoded = bolt11.decode(payment_request)
        self.paid.append(payment_request)
        return Payment(
            checking_id=decoded.payment_hash,
            payment_hash=decoded.payment_hash,
            wallet_id=wallet_id,
            amount=-(decoded.amount_msat or 0),
            fee=0,
            bolt11=payment_request,
            status=PaymentState.SUCCESS,
        )

    async def check_transaction_status(self, wallet_id, payment_hash):
        """Replacement for lnbits.core.services.payments.check_transaction_status."""
        self.is_payment_complete_called = True
        self.is_payment_complete_calls += 1
        if self.is_payment_complete_raises:
            raise ConnectionError("funding source unreachable")
        if self.payment_actually_completed:
            return PaymentSuccessStatus()
        return PaymentPendingStatus()  # paid=None
```

**Fixture:**
```python
@pytest.fixture
def node(monkeypatch):
    fake = FakeNode()
    import lnurlmint.services as services_module
    monkeypatch.setattr(services_module, "lnbits_create_invoice", fake.create_invoice)
    monkeypatch.setattr(services_module, "lnbits_pay_invoice", fake.pay_invoice)
    monkeypatch.setattr(services_module, "check_transaction_status", fake.check_transaction_status)
    monkeypatch.setattr(services_module, "_CONFIRMATION_RETRY_DELAYS_SECONDS", ())
    return fake
```

**Key:** The port's `services.py` imports the LNbits payment functions at module level:
```python
from lnbits.core.services.payments import create_invoice as lnbits_create_invoice
from lnbits.core.services.payments import pay_invoice as lnbits_pay_invoice
from lnbits.core.services.payments import check_transaction_status
```
Tests monkeypatch these module-level names (same pattern as the source's `monkeypatch.setattr(router_module, "create_invoice", ...)`).

**HodlNode for LNbits** — models the tristate `paid=None`:
```python
class HodlNode(FakeNode):
    def __init__(self):
        super().__init__()
        self.pay_mode = "ok"
        self.pending_hodl: list[str] = []

    async def pay_invoice(self, *, wallet_id, payment_request, **kwargs):
        if self.pay_mode == "ambiguous":
            self.pending_hodl.append(payment_request)
            raise PaymentError("lnd did not report a terminal payment status.", status="pending")
        if self.pay_mode == "failed":
            self.pending_hodl.append(payment_request)
            raise PaymentError("Timed out trying to find a route.", status="failed")
        if self.pay_mode == "benign_failed":
            raise PaymentError("Could not find a route.", status="failed")
        self.paid.append(payment_request)
        decoded = bolt11.decode(payment_request)
        return Payment(..., status=PaymentState.SUCCESS)

    async def check_transaction_status(self, wallet_id, payment_hash):
        self.is_payment_complete_calls += 1
        if self.pending_hodl:
            # Can't confirm either way — return paid=None
            return PaymentPendingStatus()  # paid=None
        if payment_hash in [sha256(bytes.fromhex(p)).hexdigest() for p in self.paid]:
            return PaymentSuccessStatus()
        return PaymentFailedStatus()  # paid=False

    def settle_hodl_payments(self):
        self.paid.extend(self.pending_hodl)
        self.pending_hodl.clear()
```

**IMPORTANT:** The source's `HodlNode.is_payment_complete` **raises** for pending hodl. The port's `check_transaction_status` returns `PaymentPendingStatus()` (paid=None) instead of raising. This is because LNbits' `PaymentStatus` models the tristate via `paid=None` rather than raising. The `_confirm_payment` function handles both: `paid=None` → retry/leave pending, exception → retry/leave pending. The behavior is equivalent.

### Gotchas

1. **Monkeypatching module-level imports** — the port's `services.py` must import LNbits functions at module level (`from lnbits.core.services.payments import create_invoice as lnbits_create_invoice`). Tests monkeypatch `services_module.lnbits_create_invoice` etc. This is the same pattern as the source.
2. **`Payment` model construction in tests** — the fake `create_invoice` must return a `Payment` object with the right fields. `Payment` is a pydantic model from `lnbits.core.models.payments`. It requires `checking_id`, `payment_hash`, `wallet_id`, `amount`, `fee`, `bolt11`, `status`. The `amount` is in msat (positive for incoming, negative for outgoing).
3. **`PaymentState.SUCCESS.value`** — the `status` field is a string (`"success"`, `"pending"`, `"failed"`), not the enum. Use `PaymentState.SUCCESS.value` or the string directly.
4. **`paid=None` modeling** — the source's `is_payment_complete` raises for pending hodl. The port's `check_transaction_status` returns `PaymentPendingStatus()` (paid=None). This is the key adaptation: raising → returning `paid=None`. The `_confirm_payment` function treats both the same (retry/leave pending).
5. **`fake_invoice` helper** — the source's `fake_invoice` creates a syntactically-valid BOLT11 using `bolt11.encode`. The port can reuse this (bolt11 is available in LNbits).
6. **In-memory SQLite** — the port's tests need an in-memory SQLite database for the extension's tables. LNbits' `Database` can be configured for testing. The test setup must run the migrations on the in-memory DB. This requires a test fixture that initializes `db = Database("ext_lnurlmint")` with an in-memory path and runs `m001_initial` + `m002_notes_records_melts`.
7. **`mint_note` fixture** — the port's `mint_note` fixture mints a note by calling the payRequest callback, then simulates settlement by adding the payment_hash to `fake.settled` and polling `/w` to trigger lazy materialization. This is more complex than the source (which directly manipulates `notes.settled`), because the port goes through LNbits' payment service layer.

---

## RQ13: No-Secret-Logging

### Source pattern

`server.py` lifespan (lines 76-91):
```python
# LUD-25: a bearer note's k1 lives in the query string of /w and
# /w/cb for as long as the note is held - unlike an ephemeral
# LUD-03 k1, that can be a long time, turning access logs into a
# durable theft vector (see the spec's "Secrets in GET query strings").
# uvicorn's default access log records the full request line, query
# string included, for every route - disabled here rather than scoped
# to just those two, since nothing below the ASGI app can tell
# uvicorn's access logger apart per route.
logging.getLogger("uvicorn.access").disabled = True
```

The source disables uvicorn's access log entirely at startup. This is a blunt instrument but effective: no request line (including query strings with k1) is ever logged.

Additionally, the source's `LnurlErrorResponseHandler` (router.py:39) and error handling (`log_internal_error`) never include request query params in error messages. The `log_internal_error` function (`~/lnurl-mint/lnurl_mint/errors.py`) generates a reference ID and logs the exception text, never the request URL.

### LNbits target

LNbits uses `loguru` for logging (not Python's `logging` module directly). The `uvicorn.access` logger may or may not be active depending on the LNbits deployment configuration. LNbits core doesn't disable access logging globally.

The port CANNOT disable `uvicorn.access` globally (it's an extension, not the whole app). Disabling it in `lnurlmint_start` would affect all routes, not just lnurlmint's.

### Port mapping

1. **Never log query params** — ensure no `logger.info()`, `logger.debug()`, `logger.error()` call in the lnurlmint codebase includes `k1`, `pr` (which contains a payment hash but not a secret), `h`, `h2`, or any request URL with query strings. Use `mint_id` and `payment_hash` (which are not secrets) in logs instead.

2. **Error responses never include secrets** — LNURL error responses (`{"status": "ERROR", "reason": "..."}`) use generic reasons ("Unknown note.", "pending", "Invalid or already spent k1.") that don't echo the k1 or any secret.

3. **Document the access log risk** — in the extension docs, note that operators should configure their reverse proxy to not log query strings on `/lnurlmint/w/` and `/lnurlmint/w/cb/` routes, or disable access logging for these paths. This is the same recommendation the source makes (server.py:83-84: "An operator wanting access logs for the rest should add them at a reverse proxy in front").

4. **No `log_internal_error` with request data** — the source's `log_internal_error` never receives request data. The port uses `logger.error()` with `payment_hash` and `note_ids` (not secrets) for internal errors.

### Gotchas

1. **Can't disable uvicorn.access from an extension** — the source disables it globally because it's the whole app. An extension can't do this without affecting other extensions. The best an extension can do is ensure its own code never logs secrets and document the reverse proxy recommendation.
2. **`loguru` vs `logging`** — LNbits uses `loguru` (`from loguru import logger`). The source uses Python's `logging` module. The port uses `loguru` (LNbits convention). The `uvicorn.access` logger is a standard `logging` logger, not loguru — but uvicorn may be configured to use loguru as a sink. Check LNbits' logging config.
3. **`request.url` in debug logs** — be careful with FastAPI's `Request` object. `request.url` includes query params. Never log `str(request.url)` or `request.url_for(...)` in the lnurlmint codebase. Use `request.url.path` (no query string) if needed.
4. **`pr` in logs** — the BOLT11 invoice `pr` contains the payment hash but not the preimage. It's safe to log `payment_hash` (it's a hash, not a secret). But logging the full `pr` is unnecessary and may expose metadata. Prefer `payment_hash` in logs.

---

## RQ14: Wallet Scoping for Notes

### Source pattern

The source has a single global mint. The `notes` table has no wallet/mint column — all notes belong to the one mint. Every query is unscoped.

### LNbits target

The port has per-wallet mints. The `notes` table has a `mint_id` column (FK to `mints`), and `mints` has a `wallet` column. All note queries must be scoped through this join: `notes.mint_id → mints.id → mints.wallet`.

Phase 1 already established this pattern: `count_outstanding_notes` (crud.py:98-112) joins `notes` on `mints` to enforce wallet scoping:
```python
result = await db.fetchone(
    "SELECT COUNT(*) as count FROM lnurlmint.notes n "
    "JOIN lnurlmint.mints m ON n.mint_id = m.id "
    "WHERE n.mint_id = :mid AND m.wallet = :wallet AND n.spent = 0",
    {"mid": mint_id, "wallet": wallet_id},
)
```

### Port mapping

Each state-machine operation must be wallet-scoped. The key question is: which operations need wallet scoping, and which don't?

**Operations that need wallet scoping (via JOIN on mints.wallet):**
- `get_note(note_id, mint_id)` — lookup a note scoped to a mint (the mint_id already scopes to a wallet, since each mint belongs to one wallet)
- `note_amount(note_id)` — must be scoped to a mint (the source's `note_amount` is unscoped; the port needs `mint_id`)
- `note_spent(note_id)` — same
- `note_pending(note_id)` — same

**Operations that DON'T need wallet scoping (but need mint_id):**
- `settle_mint(payment_hash)` — operates on `mints_records` (which has `mint_id`). The `payment_hash` is globally unique (PRIMARY KEY), so no wallet scoping needed. The `mint_id` is fetched from the record for the note INSERT.
- `mark_pending(note_ids, payment_hash)` — operates on `notes` by `id` (PRIMARY KEY). The note_ids are globally unique. But for SEC-07, we should verify the notes belong to the expected mint. Add `AND mint_id = :mint_id` to the WHERE clause.
- `finalize_melt(note_ids)` — same as mark_pending. Add `AND mint_id = :mint_id` for safety.
- `restore(note_ids)` — same.
- `pending_melts()` — returns ALL pending notes across ALL wallets (reconcile is system-level). No wallet scoping. But each note's `mint_id` must be resolved to get `mints.wallet` for `check_transaction_status`.

**Operations that need mint_id scoping:**
- `record_melt(payment_hash, pr, mint_id)` — INSERT into `melts` with `mint_id`
- `mark_melt_settled(payment_hash)` — UPDATE `melts` by `payment_hash` (PRIMARY KEY)

### Port mapping for state-machine queries

**`get_note` (new, replaces source's `note_amount`/`note_spent`/`note_pending`):**
```python
async def get_note(note_id: str, mint_id: str) -> Optional[Note]:
    """Get a note by id, scoped to a mint. Returns None if not found."""
    return await db.fetchone(
        "SELECT n.* FROM lnurlmint.notes n "
        "JOIN lnurlmint.mints m ON n.mint_id = m.id "
        "WHERE n.id = :id AND n.mint_id = :mid",
        {"id": note_id, "mid": mint_id},
        Note,
    )
```

**`mark_pending` (with mint_id scoping):**
```python
async def mark_pending(note_ids: list[str], payment_hash: str, mint_id: str) -> None:
    async with db.connect() as conn:
        for note_id in note_ids:
            row = await conn.fetchone(
                "SELECT pending FROM lnurlmint.notes "
                "WHERE id = :id AND spent = 0 AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )
            if row is None:
                raise ValueError("Invalid or already spent k1.")
            if row["pending"]:
                raise PendingNoteError("pending")
        for note_id in note_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes SET pending = 1, pending_payment_hash = :ph "
                "WHERE id = :id AND mint_id = :mid",
                {"ph": payment_hash, "id": note_id, "mid": mint_id},
            )
```

**`pending_melts` (no wallet scoping — system-level):**
```python
async def pending_melts() -> dict[str, list[str]]:
    """All pending notes grouped by payment_hash. No wallet scoping —
    reconcile is system-level. Each note's mint_id is used to resolve
    the wallet_id for check_transaction_status."""
    rows = await db.fetchall(
        "SELECT id, pending_payment_hash, mint_id FROM lnurlmint.notes "
        "WHERE pending = 1 AND spent = 0 AND pending_payment_hash IS NOT NULL"
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["pending_payment_hash"], []).append(row["id"])
    return grouped
```

### Gotchas

1. **`mint_id` as the scoping key** — the `notes` table has `mint_id` (FK to `mints`), not `wallet`. Wallet scoping is achieved through `mint_id → mints.wallet`. For most operations, scoping by `mint_id` is sufficient (each mint belongs to one wallet). Direct wallet scoping (JOIN on mints.wallet) is only needed for management queries (list all notes for a wallet).
2. **LNURL endpoints are public (no auth)** — the `/w/{mint_id}` and `/w/cb/{mint_id}` endpoints don't have `wallet_id` from auth. They have `mint_id` from the path. The note lookup is scoped by `mint_id` (from the path), not `wallet_id`. This is correct: the `mint_id` in the URL identifies the mint, and notes are scoped to that mint.
3. **`pending_melts` is system-level** — reconcile scans ALL pending notes across ALL wallets. No wallet scoping. But `check_transaction_status` needs `wallet_id`, which is resolved via `note.mint_id → mints.wallet`. A helper function `get_mint_id_for_note(note_id)` or including `mint_id` in the `pending_melts` result is needed.
4. **`mints_records` has `mint_id`** — the `mints_records` table (pending mints) has a `mint_id` column. `settle_mint` fetches `mint_id` from the record for the note INSERT. This is already in the schema (Phase 1).
5. **Cross-wallet note access is impossible** — a note's `id` (sha256(k1)) is globally unique, but queries always include `mint_id` scoping. A wallet A holder can't look up wallet B's note because the `mint_id` in the URL path is wallet A's mint. Even if they guess wallet B's `mint_id`, the note lookup is scoped to that mint — they'd need wallet B's `mint_id` AND the note's `k1`, which is the bearer secret. This is the same security model as the source (single mint, k1 is the secret).

---

## Summary of Key Port Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `asyncio.Lock` for in-flight registry (not `threading.Lock`) | LNbits is async-native; tests use `asyncio.gather` not threads |
| 2 | Monkeypatch module-level imports in `services.py` for tests | Same pattern as source's `monkeypatch.setattr(router_module, ...)` |
| 3 | `paid=None` via `PaymentPendingStatus()` (not raising) | LNbits' `PaymentStatus` models tristate via `paid=None`, not exceptions |
| 4 | Use `status.success`/`status.failed`/`status.paid is None` (NOT `status.pending`) | `PaymentStatus.pending` is `paid is not True` — True for both None and False |
| 5 | `pay_invoice` return value checked for pending status (not just catch exceptions) | LNbits may return pending Payment without raising on timeout |
| 6 | `amount` in satoshis at LNbits API boundary (msat everywhere else) | LNbits' `create_invoice`/`pay_invoice` take sat, not msat |
| 7 | No per-payment fee limit via LNbits API | LNbits' `pay_invoice` doesn't expose `fee_limit_msat`; fee limit is accounting-only |
| 8 | `boot_reconcile` via `asyncio.create_task` (not `create_permanent_unique_task`) | One-shot, not a restart loop; periodic task handles retries |
| 9 | `pending_melts` is system-level (no wallet scoping) | Reconcile scans all wallets; `wallet_id` resolved per-note via `mint_id` |
| 10 | LNURL errors return `{"status": "ERROR", "reason": "..."}` with HTTP 200 | LNURL protocol compliance (not FastAPI HTTPException) |
| 11 | Can't disable `uvicorn.access` from extension | Document reverse proxy recommendation; never log secrets in extension code |
| 12 | `mint_id` in URL path scopes note lookups | Public endpoints have no auth; `mint_id` from path replaces `wallet_id` from auth |
