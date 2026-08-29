import time
from typing import Optional

from lnbits.db import Database

from .models import Mint, MintRecord, Note

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
})


class PendingNoteError(Exception):
    """Raised by mark_pending when a note is already pending a melt.

    The confirm-before-burn state machine reserves a note (pending=1)
    before sending the melt payment. If a second melt attempt targets a
    note that is already reserved, this error signals the caller to
    reject the duplicate melt (SEC-06 / TEST-01).
    """

    pass


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
    comment_hash so comment-protected mints (Phase 4) resolve correctly:
    the /w endpoint passes sha256(k1) as note_id, which equals the
    comment_hash for comment-protected mints (not the payment_hash).
    """
    return await db.fetchone(
        "SELECT * FROM lnurlmint.mints_records "
        "WHERE (payment_hash = :nid OR comment_hash = :nid) "
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

    Used by the /verify endpoint (Phase 4) to gate whether the preimage
    is safe to serve: a comment-protected mint keys the note by the
    WALLET-supplied comment hash (not the payment preimage), so the
    preimage is no longer the bearer secret and can be revealed. A
    no-comment mint keys the note by the payment_hash (which is sha256
    of the preimage) — the preimage IS the bearer secret and must not
    be served. Scoped by mint_id so a verify call on mint A cannot
    resolve a payment_hash belonging to mint B (W-01).
    """
    row = await db.fetchone(
        "SELECT comment_hash FROM lnurlmint.mints_records "
        "WHERE payment_hash = :ph AND mint_id = :mid",
        {"ph": payment_hash, "mid": mint_id},
    )
    return bool(row and row["comment_hash"] is not None)


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

    Compare-and-set: UPDATE mints_records SET minted=1 WHERE minted=0,
    check rowcount==1 (only the winner proceeds), then INSERT the note.
    All in one `async with db.connect() as conn:` block for atomicity
    (REC-03). Returns the note's amount_msat, or None if already
    settled by a concurrent request (TEST-02 double-mint race guard).

    The note id is the comment_hash if present (comment-protected mint,
    Phase 4), otherwise the payment_hash (plain hash-keyed mint).
    No spendable credential is stored — only its hash (SEC-02).
    """
    async with db.connect() as conn:
        result = await conn.execute(
            "UPDATE lnurlmint.mints_records SET minted = 1 "
            "WHERE payment_hash = :ph AND minted = 0",
            {"ph": payment_hash},
        )
        if result.rowcount != 1:
            # Already settled by a concurrent request — no-op.
            return None
        row = await conn.fetchone(
            "SELECT amount_msat, comment_hash, mint_id "
            "FROM lnurlmint.mints_records WHERE payment_hash = :ph",
            {"ph": payment_hash},
        )
        if row is None:
            return None
        note_id = (
            row["comment_hash"] if row["comment_hash"] is not None
            else payment_hash
        )
        await conn.execute(
            "INSERT INTO lnurlmint.notes "
            "(id, mint_id, amount_msat, spent, pending) "
            "VALUES (:id, :mint_id, :amount, 0, 0)",
            {
                "id": note_id,
                "mint_id": row["mint_id"],
                "amount": row["amount_msat"],
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
    comment_hash: Optional[str] = None,
) -> None:
    """Record a pending mint invoice awaiting settlement.

    Stores the NET amount (after fee) — the note is credited with
    net_amount_msat when it materializes via settle_mint. The minted
    flag starts at 0 (pending) and is flipped to 1 by settle_mint's
    compare-and-set on first settlement poll. No spendable credential
    is stored — only the payment hash and invoice string (SEC-02).

    INSERT OR IGNORE handles the edge case where the same payment_hash
    is submitted twice (the PRIMARY KEY constraint prevents duplicates).

    When comment_hash is not None (LUD-25 comment protection, Phase 4),
    the collision check runs first inside a single db.connect() block:
    a comment_hash that already exists as a note id, another mint
    record's comment_hash, OR a pending mint's payment_hash is rejected
    with ValueError("comment already in use"). The payment_hash check
    prevents a comment_hash from colliding with a pending no-comment
    mint's payment_hash — which would let the comment-protected mint
    settle first and brick the no-comment mint's settle_mint INSERT
    forever (PK collision on notes.id). A UNIQUE index on comment_hash
    (m003) is the last line of defense against a TOCTOU race between
    the SELECT and INSERT under db.connect() (which is a process-level
    lock, not a DB transaction — LNbits' Connection.execute auto-commits
    per statement).
    """
    if comment_hash is None:
        # No comment protection — payment_hash is the PRIMARY KEY, so
        # INSERT OR IGNORE handles duplicates. Single-statement path.
        await db.execute(
            "INSERT OR IGNORE INTO lnurlmint.mints_records "
            "(payment_hash, mint_id, pr, amount_msat, minted, comment_hash) "
            "VALUES (:ph, :mid, :pr, :amount, 0, :ch)",
            {
                "ph": payment_hash,
                "mid": mint_id,
                "pr": pr,
                "amount": amount_msat,
                "ch": comment_hash,
            },
        )
        return

    # Comment-protected mint — collision check + INSERT under db.connect()
    # (process-level lock). The UNIQUE index on comment_hash (m003) is the
    # last line of defense against a TOCTOU race between the SELECT and
    # INSERT. Use a plain INSERT (not INSERT OR IGNORE) so a unique
    # violation from a concurrent insert surfaces as an error rather than
    # silently dropping the row.
    async with db.connect() as conn:
        collision = await conn.fetchone(
            "SELECT 1 FROM lnurlmint.notes WHERE id = :ch "
            "UNION SELECT 1 FROM lnurlmint.mints_records "
            "WHERE comment_hash = :ch OR payment_hash = :ch",
            {"ch": comment_hash},
        )
        if collision is not None:
            raise ValueError("comment already in use")
        try:
            await conn.execute(
                "INSERT INTO lnurlmint.mints_records "
                "(payment_hash, mint_id, pr, amount_msat, minted, comment_hash) "
                "VALUES (:ph, :mid, :pr, :amount, 0, :ch)",
                {
                    "ph": payment_hash,
                    "mid": mint_id,
                    "pr": pr,
                    "amount": amount_msat,
                    "ch": comment_hash,
                },
            )
        except Exception:
            # UNIQUE violation on comment_hash from a concurrent insert
            # — the collision check passed but another request inserted
            # the same comment_hash in the gap. Treat as a collision.
            raise ValueError("comment already in use")


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
    duplicate burn id or collision on either table; PendingNoteError on
    pending burn id. The mint_id scoping (SEC-07) prevents cross-wallet
    note access — all queries are scoped by mint_id.

    The collision check on mints_records prevents the A1 pending-mint
    squat attack (TEST-08): a squatter note planted under a victim's
    future note id would shadow that mint and brick settle_mint's
    INSERT forever. The generic error message reveals no information
    about which table collided (no info leak).
    """
    async with db.connect() as conn:
        # 1. Dedup check — duplicate burn ids or mint note ids are
        # rejected before any validation. The source relies on the burn
        # loop finding the note already spent on the second pass; our
        # validate-then-burn structure doesn't burn during validation,
        # so we check duplicates explicitly (RQ1 gotcha #5).
        if len(set(burn_ids)) != len(burn_ids):
            raise ValueError("Invalid or already spent k1.")
        if len(set(mint_note_ids)) != len(mint_note_ids):
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
        for note_id in mint_note_ids:
            # Collision check: mints_records (pending/settled mint
            # invoices) — prevents the A1 pending-mint squat attack.
            collision = await conn.fetchone(
                "SELECT 1 FROM lnurlmint.mints_records "
                "WHERE payment_hash = :id",
                {"id": note_id},
            )
            if collision is not None:
                raise ValueError("Invalid or already spent k1.")
            # Collision check: notes (existing outstanding/spent/pending
            # notes) — prevents overwriting an existing note id.
            collision = await conn.fetchone(
                "SELECT 1 FROM lnurlmint.notes WHERE id = :id",
                {"id": note_id},
            )
            if collision is not None:
                raise ValueError("Invalid or already spent k1.")

        # 3. Burn phase — all validated, no failure expected.
        for note_id in burn_ids:
            await conn.execute(
                "UPDATE lnurlmint.notes SET spent = 1 "
                "WHERE id = :id AND mint_id = :mid",
                {"id": note_id, "mid": mint_id},
            )

        # 4. Mint phase — all collision-checked, no failure expected.
        for note_id, amount_msat in zip(mint_note_ids, mint_amounts):
            await conn.execute(
                "INSERT INTO lnurlmint.notes "
                "(id, mint_id, amount_msat, spent, pending) "
                "VALUES (:id, :mint_id, :amount, 0, 0)",
                {"id": note_id, "mint_id": mint_id, "amount": amount_msat},
            )


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
