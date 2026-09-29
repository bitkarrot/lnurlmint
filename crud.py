import time
from base64 import b64decode, b64encode
from collections.abc import Callable
from typing import Optional

from lnbits.db import Database

from .models import Mint, MintRecord, Note, UsernameRegistration

db = Database("ext_lnurlmint")

# Whitelist of column names that update_mint may set. This guards against
# SQL injection via column-name interpolation (W-01) and prevents accidentally
# updating immutable fields (id, wallet, mint_privkey, created_at, updated_at).
_UPDATABLE_FIELDS = frozenset({
    "username",
    "base_url",
    "onion_url",
    "base_fee_msat",
    "fee_percent_ppm",
    "min_sendable_msat",
    "max_sendable_msat",
    "min_mint_msat",
    "verify_enabled",
    "sunset_mint",
    "sunset_date",
    "registration_enabled",
    "nip05_enabled",
    "zaps_enabled",
    "zap_relays",
})


class PendingNoteError(Exception):
    """Raised by mark_pending when a note is already pending a melt.

    The confirm-before-burn state machine reserves a note (pending=1)
    before sending the melt payment. If a second melt attempt targets a
    note that is already reserved, this error signals the caller to
    reject the duplicate melt (SEC-06 / TEST-01).
    """

    pass


class OutputCollisionError(ValueError):
    """Raised when a replacement note id (a callback's `p1`/`p2`, a mint
    `comment`, or an auto-minted branch index) is already registered.

    Carries its own message ("already in use") so it can be told apart
    from a generic burn-side ValueError when both surface through the
    same handler — the spec's retry-replay path reports an output
    collision differently than an invalid input.
    """

    def __init__(self, note_id: str) -> None:
        super().__init__("already in use")
        self.note_id = note_id


def _encode_zap_request(raw: str) -> str:
    """base64 the raw zap-request JSON before storing.

    LNbits' Connection.rewrite_values strips ``<.*?>`` and ``&...;``
    patterns from every written string — a kind 9734's free-text
    `content` could contain either, which would corrupt the verbatim
    request a kind 9735 receipt MUST embed (clients hash it against the
    invoice's description hash). base64 is sanitizer-proof.
    """
    return b64encode(raw.encode()).decode()


def _decode_zap_request(stored: str) -> str:
    return b64decode(stored.encode()).decode()


def _generate_mint_privkey() -> str:
    """Generate a secp256k1 private key as a 64-char hex string.

    Uses coincurve (a transitive dependency already imported by LNbits
    core's nostr/nwc code). The public key / signing logic is added in
    Phase 5; the column and key generation happen here to avoid a
    later migration.
    """
    from coincurve import PrivateKey

    return PrivateKey().secret.hex()


async def create_mint(mint: Mint) -> Mint:
    """Insert a new mint row and return it."""
    await db.insert("lnurlmint.mints", mint)
    return mint


async def get_mints_by_wallet(wallet_id: str) -> list[Mint]:
    """Return all mints owned by a wallet (wallet-scoped query)."""
    return await db.fetchall(
        "SELECT * FROM lnurlmint.mints WHERE wallet = :wallet",
        {"wallet": wallet_id},
        Mint,
    )


async def get_mint(mint_id: str, wallet_id: str) -> Optional[Mint]:
    """Return a single mint scoped to a wallet, or None if not found.

    The WHERE wallet = :wallet clause enforces cross-wallet isolation —
    wallet A cannot retrieve wallet B's mint.
    """
    return await db.fetchone(
        "SELECT * FROM lnurlmint.mints WHERE id = :id AND wallet = :wallet",
        {"id": mint_id, "wallet": wallet_id},
        Mint,
    )


async def update_mint(mint_id: str, wallet_id: str, **fields) -> Optional[Mint]:
    """Update configurable mint fields (wallet-scoped), return updated mint.

    Builds a dynamic SET clause from the provided field keys (only keys
    that are not None). The WHERE wallet = :wallet clause enforces
    cross-wallet isolation — wallet A cannot update wallet B's mint.
    Returns the updated mint via get_mint, or None if the mint does not
    belong to the wallet.
    """
    if not fields:
        # Nothing to update — just return the current mint (or None).
        return await get_mint(mint_id, wallet_id)

    # Filter against the whitelist so only known-updatable column names
    # reach the SQL string (W-01: guard against column-name injection).
    fields = {k: v for k, v in fields.items() if k in _UPDATABLE_FIELDS}
    if not fields:
        return await get_mint(mint_id, wallet_id)

    set_clauses = ", ".join(f"{k} = :{k}" for k in fields)
    set_clauses += f", updated_at = {db.timestamp_placeholder('now')}"
    values = {"id": mint_id, "wallet": wallet_id, "now": time.time(), **fields}
    await db.execute(
        f"UPDATE lnurlmint.mints SET {set_clauses} "
        "WHERE id = :id AND wallet = :wallet",
        values,
    )
    return await get_mint(mint_id, wallet_id)


async def count_outstanding_notes(mint_id: str, wallet_id: str) -> int:
    """Count unspent, non-pending notes for a mint (wallet-scoped via JOIN).

    Joins lnurlmint.notes on lnurlmint.mints so the wallet scoping is
    enforced even though the notes table has no wallet column of its own.
    Used by delete_mint to guard against orphaning outstanding bearer
    notes (a funds-loss scenario).
    """
    result = await db.fetchone(
        "SELECT COUNT(*) as count FROM lnurlmint.notes n "
        "JOIN lnurlmint.mints m ON n.mint_id = m.id "
        "WHERE n.mint_id = :mid AND m.wallet = :wallet AND n.spent = 0",
        {"mid": mint_id, "wallet": wallet_id},
    )
    return int(result["count"]) if result else 0


async def delete_mint(mint_id: str, wallet_id: str) -> bool:
    """Atomically check for outstanding notes and delete a mint.

    Uses a single `async with db.connect() as conn:` block so the
    outstanding-notes check and the delete run in one transaction (the
    LNbits Database abstraction otherwise opens a separate transaction
    per call). Returns True if the mint was deleted, False if it has
    outstanding notes (caller should return 409 Conflict). A mint that
    does not belong to the wallet simply deletes 0 rows and returns True
    (the caller's preceding get_mint/update_mint already enforced the
    404 for cross-wallet access).
    """
    async with db.connect() as conn:
        count_result = await conn.fetchone(
            "SELECT COUNT(*) as count FROM lnurlmint.notes n "
            "JOIN lnurlmint.mints m ON n.mint_id = m.id "
            "WHERE n.mint_id = :mid AND m.wallet = :wallet AND n.spent = 0",
            {"mid": mint_id, "wallet": wallet_id},
        )
        count = int(count_result["count"]) if count_result else 0
        if count > 0:
            return False
        await conn.execute(
            "DELETE FROM lnurlmint.mints WHERE id = :id AND wallet = :wallet",
            {"id": mint_id, "wallet": wallet_id},
        )
    return True


# ---------------------------------------------------------------------------
# Note state-machine CRUD (Phase 2 — confirm-before-burn primitives)
#
# These functions implement the note lifecycle that the LNURL endpoints
# (Plans 02-03) and the confirm-before-burn background task (Plan 04) call.
# Every multi-statement operation (settle_mint, mark_pending) uses a single
# `async with db.connect() as conn:` block for atomicity (REC-03). The
# compare-and-set pattern (UPDATE ... WHERE minted=0 + rowcount==1) protects
# lazy settlement materialization from double-mint races (TEST-02).
# ---------------------------------------------------------------------------


async def get_mint_by_id(mint_id: str) -> Optional[Mint]:
    """Return a mint by id without wallet scoping.

    Used by public LNURL endpoints that have no auth context — the mint
    id in the URL path identifies the mint, not the caller's wallet.
    """
    return await db.fetchone(
        "SELECT * FROM lnurlmint.mints WHERE id = :id",
        {"id": mint_id},
        Mint,
    )


async def get_note(note_id: str, mint_id: str) -> Optional[Note]:
    """Return a single note scoped by mint_id, or None if not found.

    The JOIN on lnurlmint.mints enforces that the mint exists; the
    mint_id scoping prevents cross-mint note access (SEC-07).
    """
    return await db.fetchone(
        "SELECT n.* FROM lnurlmint.notes n "
        "JOIN lnurlmint.mints m ON n.mint_id = m.id "
        "WHERE n.id = :id AND n.mint_id = :mid",
        {"id": note_id, "mid": mint_id},
        Note,
    )


async def get_pending_mint_record(
    note_id: str, mint_id: str
) -> Optional[MintRecord]:
    """Return a pending (unminted) mint record, or None.

    Used by the lazy-settlement poll to check whether a mint invoice
    is still awaiting note materialization. Matches on payment_hash OR
    note_id so note-keyed mints resolve correctly: the /w endpoint is
    passed hex(Q) of the note a mint will credit, which equals the
    record's note_id (a wallet-supplied comment output, a branch-derived
    key, or a migrated legacy payment-hash-derived bearer note).
    """
    return await db.fetchone(
        "SELECT * FROM lnurlmint.mints_records "
        "WHERE (payment_hash = :nid OR note_id = :nid) "
        "AND mint_id = :mid AND minted = 0",
        {"nid": note_id, "mid": mint_id},
        MintRecord,
    )


async def mint_record_exists(payment_hash: str) -> bool:
    """Check whether a mint record exists for a payment hash.

    Used for self-mint rejection: a melt callback must reject a payment
    hash that matches a mint invoice (the mint's own funding invoice).
    """
    row = await db.fetchone(
        "SELECT 1 FROM lnurlmint.mints_records WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )
    return row is not None


async def melt_record_exists(payment_hash: str) -> bool:
    """Check whether a melt record exists for a payment hash.

    Used for duplicate-melt rejection (SEC-06): a melt callback must
    reject a payment hash that has already been processed.
    """
    row = await db.fetchone(
        "SELECT 1 FROM lnurlmint.melts WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )
    return row is not None


async def mint_uses_comment(payment_hash: str, mint_id: str) -> bool:
    """Return True if the mint record used LUD-25 comment protection.

    Used by the /verify endpoint to gate whether the preimage is safe to
    serve: a comment-protected mint keys the note by the WALLET-supplied
    note id (a cp1/h comment, or a registered branch's derived key), not
    the payment preimage, so the preimage is no longer the bearer secret
    and can be revealed. A legacy no-comment mint (migrated with
    comment_protected=0) keys its note under Q(payment_hash) — the
    preimage IS still the bearer secret there and must not be served.
    Scoped by mint_id so a verify call on mint A cannot resolve a
    payment_hash belonging to mint B (W-01).
    """
    row = await db.fetchone(
        "SELECT comment_protected FROM lnurlmint.mints_records "
        "WHERE payment_hash = :ph AND mint_id = :mid",
        {"ph": payment_hash, "mid": mint_id},
    )
    return bool(row and row["comment_protected"])


async def mint_pr(payment_hash: str, mint_id: str) -> Optional[str]:
    """Return the mint invoice string for a payment hash, or None.

    Used by the /verify endpoint (Phase 4) to echo the original payment
    request in the verify response so a wallet can re-display it.
    Scoped by mint_id (W-01).
    """
    row = await db.fetchone(
        "SELECT pr FROM lnurlmint.mints_records "
        "WHERE payment_hash = :ph AND mint_id = :mid",
        {"ph": payment_hash, "mid": mint_id},
    )
    return row["pr"] if row else None


async def melt_pr(payment_hash: str, mint_id: str) -> Optional[str]:
    """Return the melt invoice string for a payment hash, or None.

    Used by the /verify endpoint (Phase 4) to echo the original payment
    request in the verify response. Melt preimages are harmless to
    reveal (the melt direction burns notes; the preimage does not
    unlock any bearer credential). Scoped by mint_id (W-01).
    """
    row = await db.fetchone(
        "SELECT pr FROM lnurlmint.melts "
        "WHERE payment_hash = :ph AND mint_id = :mid",
        {"ph": payment_hash, "mid": mint_id},
    )
    return row["pr"] if row else None


async def mint_settled(payment_hash: str, mint_id: str) -> bool:
    """Whether a mint invoice has ever settled (minted flag is 1).

    Used by LUD-21 verify, which must keep reporting True forever once
    settled, even after the resulting note is later spent. Scoped by
    mint_id (W-01).
    """
    row = await db.fetchone(
        "SELECT minted FROM lnurlmint.mints_records "
        "WHERE payment_hash = :ph AND mint_id = :mid",
        {"ph": payment_hash, "mid": mint_id},
    )
    return bool(row and row["minted"])


async def melt_settled(payment_hash: str, mint_id: str) -> bool:
    """Whether a melt's outgoing payment has been confirmed settled.

    The `settled` flag is set by mark_melt_settled after positive
    settlement confirmation. Used by LUD-21 verify. Scoped by mint_id
    (W-01).
    """
    row = await db.fetchone(
        "SELECT settled FROM lnurlmint.melts "
        "WHERE payment_hash = :ph AND mint_id = :mid",
        {"ph": payment_hash, "mid": mint_id},
    )
    return bool(row and row["settled"])


async def get_mint_id_for_note(note_id: str) -> Optional[str]:
    """Return the mint_id that owns a note, or None.

    Used by reconcile to resolve which wallet a stranded note belongs
    to (the notes table has no wallet column; resolution is via the
    mint_id FK to mints).
    """
    row = await db.fetchone(
        "SELECT mint_id FROM lnurlmint.notes WHERE id = :id",
        {"id": note_id},
    )
    return row["mint_id"] if row else None


async def settle_mint(payment_hash: str) -> Optional[int]:
    """Atomically materialize a note from a settled mint invoice.

    Compare-and-set: UPDATE mints_records SET minted=1 WHERE minted=0
    AND note_id IS NOT NULL, check rowcount==1 (only the winner
    proceeds), then INSERT the note keyed by its recorded note_id
    (hex(Q)) with locked_at=now — where a spend's relative timelock
    (BIP-68, via spend.py) starts counting. All in one
    `async with db.connect() as conn:` block for atomicity (REC-03).
    Returns the note's amount_msat, or None if already settled by a
    concurrent request (TEST-02 double-mint race guard).

    No spendable credential is stored — only the note's public output
    key Q (SEC-02).
    """
    async with db.connect() as conn:
        result = await conn.execute(
            "UPDATE lnurlmint.mints_records SET minted = 1 "
            "WHERE payment_hash = :ph AND minted = 0 AND note_id IS NOT NULL",
            {"ph": payment_hash},
        )
        if result.rowcount != 1:
            # Already settled by a concurrent request — no-op.
            return None
        row = await conn.fetchone(
            "SELECT amount_msat, note_id, mint_id "
            "FROM lnurlmint.mints_records WHERE payment_hash = :ph",
            {"ph": payment_hash},
        )
        if row is None:
            return None
        await conn.execute(
            "INSERT INTO lnurlmint.notes "
            "(id, mint_id, amount_msat, spent, pending, locked_at) "
            "VALUES (:id, :mint_id, :amount, 0, 0, :locked)",
            {
                "id": row["note_id"],
                "mint_id": row["mint_id"],
                "amount": row["amount_msat"],
                "locked": int(time.time()),
            },
        )
        return row["amount_msat"]


async def mark_pending(
    note_ids: list[str], payment_hash: str, mint_id: str
) -> None:
    """Reserve notes for an in-flight melt (all-or-nothing).

    Validates ALL notes are non-pending and non-spent before updating
    any — the validation loop runs first, raising before any UPDATE is
    issued. Then marks each note pending=1 with the melt's payment_hash.
    All in one `async with db.connect() as conn:` block for atomicity
    (REC-03). The mint_id scoping (SEC-07) prevents cross-wallet note
    access.

    Raises:
        PendingNoteError: if any note is already pending a melt.
        ValueError: if any note is invalid or already spent.
    """
    async with db.connect() as conn:
        # Validation loop — complete before any mutation.
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
        # Update loop — only reached if all notes validated.
        for note_id in note_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes "
                "SET pending = 1, pending_payment_hash = :ph "
                "WHERE id = :id AND mint_id = :mid",
                {"ph": payment_hash, "id": note_id, "mid": mint_id},
            )


async def finalize_melt(note_ids: list[str], mint_id: str) -> None:
    """Burn notes for good after a confirmed melt settlement.

    Sets spent=1, pending=0, pending_payment_hash=NULL for each note,
    scoped by mint_id (SEC-07). Called only after positive settlement
    confirmation (paid=True) — never on pending or unconfirmable state.

    All updates run in a single `async with db.connect() as conn:` block
    for atomicity (W-02) — a failure mid-loop cannot leave some notes
    burned and others not, which matters for Phase 3 multi-note melts.
    """
    async with db.connect() as conn:
        for note_id in note_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes "
                "SET spent = 1, pending = 0, pending_payment_hash = NULL "
                "WHERE id = :id AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )


async def restore(note_ids: list[str], mint_id: str) -> None:
    """Release a pending reservation after a confirmed melt failure.

    Sets pending=0, pending_payment_hash=NULL for each note, scoped by
    mint_id (SEC-07). Called only after positive failure confirmation
    (paid=False) — never on pending or unconfirmable state (TEST-03
    tristate: paid=None leaves the note pending).

    All updates run in a single `async with db.connect() as conn:` block
    for atomicity (W-02) — a failure mid-loop cannot leave some notes
    restored and others not, which matters for Phase 3 multi-note melts.
    """
    async with db.connect() as conn:
        for note_id in note_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes "
                "SET pending = 0, pending_payment_hash = NULL "
                "WHERE id = :id AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )


async def pending_melts() -> dict[str, list[str]]:
    """Return all pending notes grouped by melt payment_hash.

    Returns a dict mapping payment_hash → [note_ids] for all pending
    notes across ALL wallets (no wallet scoping — reconcile is a
    system-level operation). The mint_id for each note is resolved
    separately via get_mint_id_for_note when reconcile needs the
    wallet_id for check_transaction_status.
    """
    rows = await db.fetchall(
        "SELECT id, pending_payment_hash, mint_id "
        "FROM lnurlmint.notes "
        "WHERE pending = 1 AND spent = 0 "
        "AND pending_payment_hash IS NOT NULL"
    )
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["pending_payment_hash"], []).append(row["id"])
    return grouped


async def record_melt(
    payment_hash: str,
    pr: str,
    mint_id: str,
    note_ids: str,
    amount_msat: int,
) -> None:
    """Record a melt invoice for verify and duplicate-melt detection.

    INSERT OR IGNORE so a re-record (e.g. reconcile retry) does not
    fail. The settled flag starts at 0 and is set to 1 by
    mark_melt_settled after positive settlement.
    """
    await db.execute(
        "INSERT OR IGNORE INTO lnurlmint.melts "
        "(payment_hash, mint_id, pr, note_ids, amount_msat, settled) "
        "VALUES (:ph, :mid, :pr, :nids, :amount, 0)",
        {
            "ph": payment_hash,
            "mid": mint_id,
            "pr": pr,
            "nids": note_ids,
            "amount": amount_msat,
        },
    )


async def mark_melt_settled(payment_hash: str) -> None:
    """Mark a melt record as settled (burn confirmed).

    Called after finalize_melt completes — the melt's payment_hash is
    now positively settled, so verify can report it as such.
    """
    await db.execute(
        "UPDATE lnurlmint.melts SET settled = 1 WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )


async def record_mint_record(
    payment_hash: str,
    mint_id: str,
    pr: str,
    amount_msat: int,
    note_id: str,
    zap_request: Optional[str] = None,
) -> None:
    """Record a pending mint invoice awaiting settlement.

    Stores the NET amount (after fee) — the note is credited with
    net_amount_msat when it materializes via settle_mint, keyed by
    `note_id` = hex(Q): the WALLET-supplied output (a `cp1` or a bearer
    note's hex `h` short form), a registered username's branch-derived
    key, or — for a migrated pre-m004 record — Q(payment_hash). No
    spendable credential is stored — only the payment hash, the invoice
    string (for LUD-21 verify), and the note's public output key
    (SEC-02).

    `note_id` is always set (comment protection or a branch is mandatory
    in the taproot protocol), so the collision check always runs inside
    a single db.connect() block: a note_id that already exists as a note,
    another mint record's note_id, OR a pending mint's payment_hash is
    rejected with ValueError("already in use"). The checks are global
    (NOT mint-scoped) because notes.id is a global PRIMARY KEY — a
    planted duplicate under any mint would brick settle_mint's INSERT
    forever.
    """
    async with db.connect() as conn:
        collision = await conn.fetchone(
            "SELECT 1 FROM lnurlmint.notes WHERE id = :nid "
            "UNION SELECT 1 FROM lnurlmint.mints_records "
            "WHERE note_id = :nid OR payment_hash = :nid",
            {"nid": note_id},
        )
        if collision is not None:
            raise ValueError("already in use")
        try:
            await conn.execute(
                "INSERT INTO lnurlmint.mints_records "
                "(payment_hash, mint_id, pr, amount_msat, minted, note_id, "
                "comment_protected, zap_request) "
                "VALUES (:ph, :mid, :pr, :amount, 0, :nid, 1, :zap)",
                {
                    "ph": payment_hash,
                    "mid": mint_id,
                    "pr": pr,
                    "amount": amount_msat,
                    "nid": note_id,
                    "zap": _encode_zap_request(zap_request) if zap_request else None,
                },
            )
        except Exception as exc:
            # PK/unique violation from a concurrent insert — the
            # collision check passed but another request inserted in the
            # gap. Treat as a collision.
            raise ValueError("already in use") from exc


# ---------------------------------------------------------------------------
# swap primitive (Phase 3 — rotate/split/merge core)
#
# swap atomically burns N notes and mints M notes in one
# `async with db.connect() as conn:` block. It is the core primitive for
# rotate (burn 1, mint 1), split (burn N, mint 2), and merge (burn N,
# mint 1). The validate-then-burn-then-mint structure ensures no partial
# state: ALL validation (burn ids + collision checks) completes before
# ANY mutation (REC-03, SEC-06, TEST-08).
#
# LNbits' conn.execute commits per call — there is no automatic rollback
# if a later statement fails. Separating validation from mutation
# guarantees that if any check fails, nothing has been burned or minted
# yet. The collision check on both mints_records (pending/settled mint
# invoices) AND notes (existing notes) prevents the A1 pending-mint
# squat attack (TEST-08): an attacker planting a note under a victim's
# future note id would shadow that mint and brick settle_mint's INSERT
# forever. The generic "Invalid or already spent k1." error message
# reveals no information about which table collided (no info leak).
# ---------------------------------------------------------------------------


async def swap(
    burn_ids: list[str],
    mint_note_ids: list[str],
    mint_amounts: list[int],
    mint_id: str,
) -> None:
    """Atomically burn N notes and mint M notes in one db.connect() block.

    Validate-then-burn-then-mint: all burn validations (not spent, not
    pending) and all mint collision checks (notes + mints_records)
    complete before any mutation. Raises ValueError on invalid/spent/
    duplicate burn id; OutputCollisionError on a mint-side collision;
    PendingNoteError on a pending burn id. The mint_id scoping (SEC-07)
    prevents cross-wallet note burns — but the mint-side collision
    checks are deliberately global, because notes.id is a global
    PRIMARY KEY (a squatter note planted under another mint's pending
    mint's note_id would brick that mint's settle INSERT forever -
    the A1 pending-mint squat attack, TEST-08).

    Per LUD-25, `mint_note_ids` are output keys (hex(Q)) the WALLET
    disclosed as `p1`/`p2` — this side never generates, sees, or
    persists a spend.

    Also records this burn keyed by the exact set of `burn_ids` in the
    same connect block — LUD-25's "Retrying a mutation" needs find_burn
    to answer a retried rotate/split/merge with the original result
    rather than "already spent".
    """
    async with db.connect() as conn:
        # 1. Dedup check — duplicate burn ids or mint note ids are
        # rejected before any validation.
        if len(set(burn_ids)) != len(burn_ids):
            raise ValueError("Invalid or already spent k1.")

        # 2. Validation phase — complete before any mutation.
        for note_id in burn_ids:
            row = await conn.fetchone(
                "SELECT pending FROM lnurlmint.notes "
                "WHERE id = :id AND spent = 0 AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )
            if row is None:
                raise ValueError("Invalid or already spent k1.")
            if row["pending"]:
                raise PendingNoteError("pending")
        seen_mint_ids: set = set()
        for note_id in mint_note_ids:
            # Collision check: mints_records.note_id (the note some mint
            # invoice will credit once paid) — as taken as one already on
            # file, per LUD-25. Global, not mint-scoped: see docstring.
            collision = await conn.fetchone(
                "SELECT 1 FROM lnurlmint.mints_records "
                "WHERE note_id = :id",
                {"id": note_id},
            )
            if collision is not None:
                raise OutputCollisionError(note_id)
            if (
                note_id in seen_mint_ids
                or (
                    await conn.fetchone(
                        "SELECT 1 FROM lnurlmint.notes WHERE id = :id",
                        {"id": note_id},
                    )
                )
                is not None
            ):
                raise OutputCollisionError(note_id)
            seen_mint_ids.add(note_id)

        # 3. Burn phase — all validated, no failure expected.
        for note_id in burn_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes SET spent = 1 "
                "WHERE id = :id AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )

        # 4. Mint phase — all collision-checked, no failure expected.
        locked_at = int(time.time())
        for note_id, amount_msat in zip(mint_note_ids, mint_amounts, strict=True):
            await conn.execute(
                "INSERT INTO lnurlmint.notes "
                "(id, mint_id, amount_msat, spent, pending, locked_at) "
                "VALUES (:id, :mint_id, :amount, 0, 0, :locked)",
                {
                    "id": note_id,
                    "mint_id": mint_id,
                    "amount": amount_msat,
                    "locked": locked_at,
                },
            )

        # 5. Record the burn for LUD-25 mutation-replay (find_burn).
        await conn.execute(
            "INSERT INTO lnurlmint.burns "
            "(burn_key, mint_id, id, id2, amount1_msat, amount2_msat) "
            "VALUES (:bk, :mid, :id, :id2, :a1, :a2)",
            {
                "bk": _burn_key(burn_ids),
                "mid": mint_id,
                "id": mint_note_ids[0],
                "id2": mint_note_ids[1] if len(mint_note_ids) > 1 else None,
                "a1": mint_amounts[0],
                "a2": mint_amounts[1] if len(mint_amounts) > 1 else None,
            },
        )


def _burn_key(note_ids: list[str]) -> str:
    """Canonical identity of a burn: the note ids it spent, order
    independent (a merge's k1s can arrive in any order) but otherwise
    exact - the same set burned by two different requests is the same
    burn."""
    return "|".join(sorted(note_ids))


async def find_burn(
    note_ids: list[str], mint_id: str
) -> Optional[tuple[str, Optional[str], int, Optional[int]]]:
    """If `note_ids`, as a set, were burned together by one earlier
    rotate/split/merge on this mint (see swap), returns the (id, id2,
    amount1_msat, amount2_msat) that burn produced — everything the
    callback's LUD-25 retry handling needs to answer a retried request
    with the original result instead of "already spent". None if this
    exact set was never burned together here — including a partial
    overlap, which is a genuine conflict, not a replay."""
    row = await db.fetchone(
        "SELECT id, id2, amount1_msat, amount2_msat FROM lnurlmint.burns "
        "WHERE mint_id = :mid AND burn_key = :bk",
        {"mid": mint_id, "bk": _burn_key(note_ids)},
    )
    return (row["id"], row["id2"], row["amount1_msat"], row["amount2_msat"]) if row else None


# ---------------------------------------------------------------------------
# Note lookups for the taproot protocol (spend verification needs
# locked_at; informational endpoints need spent/pending for known notes)
# ---------------------------------------------------------------------------


async def note_record(
    note_id: str, mint_id: str
) -> Optional[tuple[int, int, bool, bool]]:
    """(amount_msat, locked_at, spent, pending) of the note `note_id`
    (hex(Q)) on this mint, spent or not — what verifying a spend of it
    needs (locked_at starts a relative timelock), even for a burn being
    retried. None if this mint never issued it."""
    row = await db.fetchone(
        "SELECT amount_msat, locked_at, spent, pending "
        "FROM lnurlmint.notes WHERE id = :id AND mint_id = :mid",
        {"id": note_id, "mid": mint_id},
    )
    return (
        (row["amount_msat"], row["locked_at"], bool(row["spent"]), bool(row["pending"]))
        if row
        else None
    )


async def note_amount(note_id: str, mint_id: str) -> Optional[int]:
    """Value of the outstanding (unspent) note `note_id` on this mint."""
    row = await db.fetchone(
        "SELECT amount_msat FROM lnurlmint.notes "
        "WHERE id = :id AND spent = 0 AND mint_id = :mid",
        {"id": note_id, "mid": mint_id},
    )
    return row["amount_msat"] if row else None


async def note_pending(note_id: str, mint_id: str) -> bool:
    """Whether `note_id` names an outstanding note on this mint currently
    reserved by an in-flight melt (see mark_pending). The informational
    withdraw endpoint must say so instead of advertising it as
    withdrawable — exactly the lie a sell-during-melt scam needs."""
    row = await db.fetchone(
        "SELECT pending FROM lnurlmint.notes "
        "WHERE id = :id AND spent = 0 AND mint_id = :mid",
        {"id": note_id, "mid": mint_id},
    )
    return bool(row and row["pending"])


async def note_spent(note_id: str, mint_id: str) -> bool:
    """Whether `note_id` names a note this mint actually issued and has
    since burned — as opposed to one that never existed at all. Burned
    rows are kept, so this distinguishes "already spent" from "unknown"
    for callers (the hash-lookup endpoint) that want to report which."""
    row = await db.fetchone(
        "SELECT spent FROM lnurlmint.notes WHERE id = :id AND mint_id = :mid",
        {"id": note_id, "mid": mint_id},
    )
    return bool(row and row["spent"])


async def outstanding_msat(mint_id: str) -> int:
    """Total value (msat) of every currently outstanding bearer note on
    this mint — its total liability, reported on the mint-address
    discovery response and the public one-pager. Includes notes reserved
    by an in-flight melt (pending = 1): a reservation is not a burn —
    the note is still outstanding, per LUD-25, until its melt settles.

    A lower bound: a settled-but-never-looked-up mint still only exists
    as a row in mints_records (lazy materialization, see settle_mint)."""
    row = await db.fetchone(
        "SELECT COALESCE(SUM(amount_msat), 0) AS total "
        "FROM lnurlmint.notes WHERE spent = 0 AND mint_id = :mid",
        {"mid": mint_id},
    )
    return int(row["total"]) if row else 0


async def id_in_use(note_id: str) -> bool:
    """Whether `note_id` (hex(Q)) already names a note — spent or not,
    on ANY mint — or the note some mint invoice will credit once paid.
    Global on purpose: notes.id is a global PRIMARY KEY, so a planted
    duplicate under a different mint would still brick settle_mint's
    INSERT (the A1 squat attack)."""
    row = await db.fetchone(
        "SELECT 1 FROM lnurlmint.notes WHERE id = :nid "
        "UNION SELECT 1 FROM lnurlmint.mints_records WHERE note_id = :nid",
        {"nid": note_id},
    )
    return row is not None


# ---------------------------------------------------------------------------
# Username registrations (LUD-25 cx1 lightning-address auto-mint)
#
# Per-mint: every mint has its own username namespace (the PRIMARY KEY is
# (mint_id, username)). A registration is a public-key binding, never a
# balance or an account.
# ---------------------------------------------------------------------------


async def upsert_username(
    mint_id: str, username: str, cx1_hex: str, nostr_pubkey_hex: Optional[str] = None
) -> None:
    """Claim `username` for the watch-only branch `cx1_hex` on this mint,
    or wholesale replace an existing claim's branch/npub — the endpoint
    gates every call behind its own ownership-proof signature before ever
    calling this (a fresh claim proves control of THIS cx1, an overwrite
    proves control of the branch currently on file). `next_index` always
    resets to 0 — an overwrite means a different branch, whose own index
    0 was never tried yet.

    `nostr_pubkey_hex`, if given, doubles `username` as a NIP-05 name.
    Omitting it on an overwrite clears any previously registered one —
    this call replaces the registration wholesale, it does not merge.
    """
    await db.execute(
        "INSERT INTO lnurlmint.usernames "
        "(mint_id, username, cx1, nostr_pubkey, next_index) "
        "VALUES (:mid, :u, :cx1, :npub, 0) "
        + (
            "ON CONFLICT(mint_id, username) DO UPDATE SET "
            "cx1 = excluded.cx1, nostr_pubkey = excluded.nostr_pubkey, "
            "next_index = 0"
            if db.type == "SQLITE"
            else "ON CONFLICT (mint_id, username) DO UPDATE SET "
            "cx1 = EXCLUDED.cx1, nostr_pubkey = EXCLUDED.nostr_pubkey, "
            "next_index = 0"
        ),
        {"mid": mint_id, "u": username, "cx1": cx1_hex, "npub": nostr_pubkey_hex},
    )


async def delete_username(mint_id: str, username: str) -> None:
    """Free `username` on this mint entirely — back to unclaimed,
    first-come-first-served. A no-op if it was never claimed."""
    await db.execute(
        "DELETE FROM lnurlmint.usernames WHERE mint_id = :mid AND username = :u",
        {"mid": mint_id, "u": username},
    )


async def username_branch(mint_id: str, username: str) -> Optional[str]:
    """The cx1 hex (P || chain_code) registered under `username` on this
    mint, or None if it was never claimed."""
    row = await db.fetchone(
        "SELECT cx1 FROM lnurlmint.usernames WHERE mint_id = :mid AND username = :u",
        {"mid": mint_id, "u": username},
    )
    return row["cx1"] if row else None


async def username_nostr_pubkey(mint_id: str, username: str) -> Optional[str]:
    """The hex Nostr pubkey `username` registered on this mint, or None —
    "not a NIP-05 name" to the nostr.json endpoint."""
    row = await db.fetchone(
        "SELECT nostr_pubkey FROM lnurlmint.usernames "
        "WHERE mint_id = :mid AND username = :u",
        {"mid": mint_id, "u": username},
    )
    return row["nostr_pubkey"] if row else None


async def next_index_hint(mint_id: str, username: str) -> Optional[int]:
    """The persisted best-known next-unused index on `username`'s
    registered branch (the `text/cpub` metadata hint, LUD-25 Internal
    transfer) — purely advisory: a WALLET starts guessing from it, but a
    stale or already-taken index is still rejected exactly like any other
    p1/p2 collision. None if `username` was never claimed."""
    row = await db.fetchone(
        "SELECT next_index FROM lnurlmint.usernames "
        "WHERE mint_id = :mid AND username = :u",
        {"mid": mint_id, "u": username},
    )
    return row["next_index"] if row else None


async def list_usernames(mint_id: str) -> list[UsernameRegistration]:
    """All registered usernames for a mint (management API)."""
    return await db.fetchall(
        "SELECT * FROM lnurlmint.usernames WHERE mint_id = :mid "
        "ORDER BY username",
        {"mid": mint_id},
        UsernameRegistration,
    )


async def claim_next_index(
    mint_id: str, username: str, derive: Callable[[int], str]
) -> tuple[str, int]:
    """Pick and reserve the next usable note index on `username`'s
    registered branch — LUD-25's own race-avoidance under Lightning
    Address auto-mint: `derive(i)` is tried starting at the persisted
    next_index, skipping any index whose resulting pubkey already names
    an outstanding or previously-minted note (the same collision
    record_mint_record itself would reject) rather than crediting into an
    index a pending rotate/split/merge might also be about to install.
    Persists next_index past the winner and returns (pk_hex, index).
    Raises ValueError if `username` was never registered on this mint."""
    async with db.connect() as conn:
        row = await conn.fetchone(
            "SELECT next_index FROM lnurlmint.usernames "
            "WHERE mint_id = :mid AND username = :u",
            {"mid": mint_id, "u": username},
        )
        if row is None:
            raise ValueError("Unknown username.")
        index = row["next_index"]
        while True:
            pk_hex = derive(index)
            collision = await conn.fetchone(
                "SELECT 1 FROM lnurlmint.notes WHERE id = :nid "
                "UNION SELECT 1 FROM lnurlmint.mints_records WHERE note_id = :nid",
                {"nid": pk_hex},
            )
            if collision is None:
                break
            index += 1
        await conn.execute(
            "UPDATE lnurlmint.usernames SET next_index = :i "
            "WHERE mint_id = :mid AND username = :u",
            {"i": index + 1, "mid": mint_id, "u": username},
        )
        return pk_hex, index


# ---------------------------------------------------------------------------
# NIP-57 zap bookkeeping (see nostr.py / views_lnurl.py)
# ---------------------------------------------------------------------------


async def pending_zap_mints(mint_id: str, created_since: int, limit: int) -> list[str]:
    """Payment hashes of the `limit` newest unpaid zap invoices on this
    mint created at or after `created_since` (unix seconds) — what the
    settlement poll checks. Older ones are left alone."""
    rows = await db.fetchall(
        "SELECT payment_hash FROM lnurlmint.mints_records "
        "WHERE mint_id = :mid AND minted = 0 AND zap_request IS NOT NULL "
        "AND created_at >= :since ORDER BY created_at DESC LIMIT :limit",
        {"mid": mint_id, "since": created_since, "limit": limit},
    )
    return [r["payment_hash"] for r in rows]


async def unpublished_zaps(mint_id: str) -> list[tuple[str, str, str]]:
    """(payment_hash, pr, zap_request) of every settled zap invoice on
    this mint whose kind 9735 receipt has not reached a relay yet. The
    stored zap_request is base64 (see _encode_zap_request) — decoded here
    back to the verbatim request."""
    rows = await db.fetchall(
        "SELECT payment_hash, pr, zap_request FROM lnurlmint.mints_records "
        "WHERE mint_id = :mid AND minted = 1 AND zap_request IS NOT NULL "
        "AND zap_receipt IS NULL",
        {"mid": mint_id},
    )
    return [(r["payment_hash"], r["pr"], _decode_zap_request(r["zap_request"])) for r in rows]


async def mark_zap_published(payment_hash: str, receipt_id: str) -> None:
    await db.execute(
        "UPDATE lnurlmint.mints_records SET zap_receipt = :rid "
        "WHERE payment_hash = :ph",
        {"rid": receipt_id, "ph": payment_hash},
    )


async def zaps_enabled_mints() -> list[str]:
    """Mint ids with NIP-57 zaps turned on — the zap-poll task's
    iteration set (empty for most installs: a single cheap SELECT)."""
    rows = await db.fetchall(
        "SELECT id FROM lnurlmint.mints WHERE zaps_enabled = 1"
    )
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# Management SPA queries (Phase 6 — outstanding notes + activity log)
# ---------------------------------------------------------------------------


async def get_outstanding_notes(mint_id: str, wallet_id: str) -> list[Note]:
    """Return all notes for a mint, wallet-scoped via JOIN (SEC-07).

    Ordered by created_at descending. Used by the management SPA's
    outstanding notes view.
    """
    return await db.fetchall(
        "SELECT n.* FROM lnurlmint.notes n "
        "JOIN lnurlmint.mints m ON n.mint_id = m.id "
        "WHERE n.mint_id = :mid AND m.wallet = :wallet "
        "ORDER BY n.created_at DESC",
        {"mid": mint_id, "wallet": wallet_id},
        Note,
    )


async def get_mint_activity(
    mint_id: str, wallet_id: str, limit: int = 20
) -> list[dict]:
    """Return recent mint and melt records, wallet-scoped (SEC-07).

    Merges mints_records and melts tables via UNION ALL, sorted by
    created_at desc with a single LIMIT — so the most-recent records
    across both tables are returned (not capped per-table).
    Each record is a dict with: type ('mint' or 'melt'), amount_msat,
    payment_hash, pr, settled (bool), created_at.
    """
    return await db.fetchall(
        "SELECT * FROM ("
        "  SELECT r.payment_hash, r.amount_msat, r.pr, r.minted AS settled, "
        "  r.created_at, 'mint' AS type "
        "  FROM lnurlmint.mints_records r "
        "  JOIN lnurlmint.mints m ON r.mint_id = m.id "
        "  WHERE r.mint_id = :mid AND m.wallet = :wallet "
        "  UNION ALL "
        "  SELECT r.payment_hash, r.amount_msat, r.pr, r.settled, "
        "  r.created_at, 'melt' AS type "
        "  FROM lnurlmint.melts r "
        "  JOIN lnurlmint.mints m ON r.mint_id = m.id "
        "  WHERE r.mint_id = :mid AND m.wallet = :wallet "
        ") ORDER BY created_at DESC LIMIT :limit",
        {"mid": mint_id, "wallet": wallet_id, "limit": limit},
    )
