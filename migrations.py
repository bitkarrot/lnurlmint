"""Database migrations for the lnurlmint extension.

Migrations are discovered by LNbits' migration runner via the regex
``^m(\\d\\d\\d)_`` on module attributes and run in definition order.
"""


async def m001_initial(db):
    """Create the lnurlmint.mints table.

    All 15 columns from DATA-01. Booleans are stored as INTEGER (0/1)
    per LNbits convention. Timestamps use db.timestamp_now for defaults
    (cross-DB: strftime on SQLite, now() on Postgres).
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS lnurlmint.mints (
            id               TEXT PRIMARY KEY,
            wallet           TEXT NOT NULL,
            username         TEXT NOT NULL,
            base_url         TEXT NOT NULL DEFAULT '',
            onion_url        TEXT,
            base_fee_msat    INTEGER NOT NULL DEFAULT 0,
            fee_percent_ppm  INTEGER NOT NULL DEFAULT 0,
            min_sendable_msat INTEGER NOT NULL DEFAULT 1000,
            max_sendable_msat INTEGER NOT NULL DEFAULT 1000000000,
            min_mint_msat    INTEGER NOT NULL DEFAULT 10000,
            verify_enabled   INTEGER NOT NULL DEFAULT 1,
            sunset_mint      INTEGER NOT NULL DEFAULT 0,
            mint_privkey     TEXT NOT NULL,
            created_at       TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """,
            updated_at       TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """
        );
        """
    )
    # SQLite cannot create indexes with a schema prefix on an attached
    # database, so use the db-specific table reference.
    table = f"{db.references_schema}mints"
    await db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lnurlmint_mints_wallet ON {table}(wallet);
        """
    )


async def m002_notes_records_melts(db):
    """Create the lnurlmint.notes, mints_records, and melts tables.

    Completes the data model (DATA-02, DATA-03) so Phase 2 can implement
    note CRUD, the confirm-before-burn state machine, and background
    reconciliation without further schema changes.

    Store-hashes invariant (SEC-02): no table holds the raw bearer
    credential. notes.id is sha256(k1) hex, never the spendable value
    itself. The funding invoice proof is fetched live from the funding
    source on verify, never persisted here.

    - notes: outstanding bearer notes. id = sha256(k1). `spent`/`pending`
      are the confirm-before-burn state flags (mutually exclusive in
      steady state; pending=1 means a melt is in flight). `pending_payment_hash`
      lets reconcile identify which melt invoice to confirm for a stranded
      note. `comment_hash` keys a comment-protected note (LUD-25) instead
      of the payment hash.
    - mints_records: pending mints awaiting settlement. `minted` is the
      compare-and-set flag (UPDATE ... WHERE minted=0 + rowcount==1) that
      makes lazy settlement materialization race-safe.
    - melts: pending/settled melts. `settled` flags positive settlement
      (burn confirmed). `note_ids` records which notes were burned.
    """
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS lnurlmint.notes (
            id                   TEXT PRIMARY KEY,
            mint_id              TEXT NOT NULL,
            amount_msat          INTEGER NOT NULL,
            spent                INTEGER NOT NULL DEFAULT 0,
            pending              INTEGER NOT NULL DEFAULT 0,
            pending_payment_hash TEXT,
            comment_hash         TEXT,
            created_at           TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """
        );
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS lnurlmint.mints_records (
            payment_hash TEXT PRIMARY KEY,
            mint_id      TEXT NOT NULL,
            pr           TEXT NOT NULL,
            amount_msat  INTEGER NOT NULL,
            minted       INTEGER NOT NULL DEFAULT 0,
            comment_hash TEXT,
            created_at   TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """
        );
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS lnurlmint.melts (
            payment_hash TEXT PRIMARY KEY,
            mint_id      TEXT NOT NULL,
            note_ids     TEXT,
            amount_msat  INTEGER NOT NULL,
            pr           TEXT NOT NULL,
            settled      INTEGER NOT NULL DEFAULT 0,
            created_at   TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """
        );
        """
    )
    # Indexes — SQLite cannot create indexes with a schema prefix on an
    # attached database, so use the db-specific table reference.
    notes = f"{db.references_schema}notes"
    await db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lnurlmint_notes_mint_id ON {notes}(mint_id);
        """
    )
    await db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lnurlmint_notes_pending ON {notes}(pending);
        """
    )
    mints_records = f"{db.references_schema}mints_records"
    await db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lnurlmint_mints_records_mint_id ON {mints_records}(mint_id);
        """
    )
    melts = f"{db.references_schema}melts"
    await db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lnurlmint_melts_mint_id ON {melts}(mint_id);
        """
    )


async def m003_comment_hash_unique(db):
    """Add a UNIQUE index on mints_records.comment_hash (Phase 4).

    A UNIQUE constraint on comment_hash prevents two concurrent
    record_mint_record calls from both passing the collision SELECT and
    inserting the same comment_hash — which would brick one of the mints
    (settle_mint's INSERT into notes would PK-collide with the other's
    note). SQLite and Postgres both allow multiple NULLs in a UNIQUE
    index, so no-comment mints (comment_hash=NULL) are unaffected.

    The collision check in record_mint_record also checks
    mints_records.payment_hash (the PK) and notes.id, but the UNIQUE
    index is the last line of defense against a TOCTOU race between the
    SELECT and INSERT under db.connect() (which is a process-level lock,
    not a DB transaction — LNbits' Connection.execute auto-commits per
    statement).
    """
    mints_records = f"{db.references_schema}mints_records"
    await db.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lnurlmint_mints_records_comment_hash
        ON {mints_records}(comment_hash);
        """
    )


async def m004_taproot_protocol(db):
    """Upgrade to the LUD-25 taproot protocol (upstream v0.12.x parity).

    - notes.id becomes hex(Q), the note's taproot output key, instead of
      sha256(k1)/comment hash. Every existing note id IS a preimage hash
      `h` (whether from a no-comment mint - sha256 of the invoice
      preimage - or a comment-protected one - the wallet's comment hash),
      so each row is rewritten to hex(Q) of the bearer note that same
      `h` defines (NUMS key + `OP_SHA256 <h> OP_EQUAL` leaf, see
      taproot.preimage_note). The old preimage stays the spend, so
      outstanding notes keep working - a holder's k1 does not change.
    - notes gains `locked_at` (unix seconds) - where a cw1's relative
      timelock (BIP-68) starts counting. Existing rows get 0 = "credited
      long ago", the right answer for any relative lock on a note that
      old.
    - mints_records.comment_hash is renamed note_id and likewise
      rewritten to hex(Q). A legacy NO-comment pending mint gets
      note_id = hex(Q(payment_hash)) - the wallet's preimage k1 still
      redeems it - and comment_protected = 0 so LUD-21 verify still
      refuses to leak that preimage (it IS the bearer secret there).
    - mints gains the new per-mint toggles: registration_enabled (cx1
      lightning-address registration), nip05_enabled, zaps_enabled,
      zap_relays, sunset_date.
    - burns (LUD-25 "Retrying a mutation" replay table) and usernames
      (cx1 branch registrations, per-mint) are created.
    """
    from .taproot import preimage_note

    async def _columns(table: str) -> set:
        """Column names of `table` (cross-DB: SQLite pragma / Postgres
        information_schema, the latter scoped to the extension schema)."""
        if db.type == "SQLITE":
            rows = await db.fetchall(
                f"SELECT name FROM pragma_table_info('{table}')"
            )
        else:
            rows = await db.fetchall(
                "SELECT column_name AS name FROM information_schema.columns "
                "WHERE table_name = :t AND table_schema = :s",
                {"t": table, "s": db.schema},
            )
        return {r["name"] for r in rows}

    # --- notes: locked_at + id rewrite to hex(Q) ------------------------
    if "locked_at" not in await _columns("notes"):
        await db.execute(
            "ALTER TABLE lnurlmint.notes ADD COLUMN locked_at INTEGER NOT NULL DEFAULT 0"
        )

    rows = await db.fetchall("SELECT id, mint_id FROM lnurlmint.notes")
    for row in rows:
        old_id = row["id"]
        try:
            new_id = preimage_note(bytes.fromhex(old_id))[0].hex()
        except Exception:
            continue  # not a 64-hex id - leave untouched
        if new_id != old_id:
            await db.execute(
                "UPDATE lnurlmint.notes SET id = :new_id "
                "WHERE id = :old_id AND mint_id = :mid",
                {"new_id": new_id, "old_id": old_id, "mid": row["mint_id"]},
            )

    # --- mints_records: comment_hash -> note_id + zap columns -----------
    colnames = await _columns("mints_records")
    if "comment_hash" in colnames and "note_id" not in colnames:
        await db.execute(
            "ALTER TABLE lnurlmint.mints_records RENAME COLUMN comment_hash TO note_id"
        )
    if "comment_protected" not in colnames:
        await db.execute(
            "ALTER TABLE lnurlmint.mints_records "
            "ADD COLUMN comment_protected INTEGER NOT NULL DEFAULT 1"
        )
    if "zap_request" not in colnames:
        await db.execute(
            "ALTER TABLE lnurlmint.mints_records ADD COLUMN zap_request TEXT"
        )
    if "zap_receipt" not in colnames:
        await db.execute(
            "ALTER TABLE lnurlmint.mints_records ADD COLUMN zap_receipt TEXT"
        )

    recs = await db.fetchall(
        "SELECT payment_hash, mint_id, note_id FROM lnurlmint.mints_records"
    )
    for rec in recs:
        ph, note_id = rec["payment_hash"], rec["note_id"]
        if note_id is not None:
            # comment-protected mint: note_id held the wallet's comment
            # hash `h` - rewrite to hex(Q(h)), keep comment_protected = 1
            try:
                new_id = preimage_note(bytes.fromhex(note_id))[0].hex()
            except Exception:
                continue
            await db.execute(
                "UPDATE lnurlmint.mints_records SET note_id = :nid "
                "WHERE payment_hash = :ph",
                {"nid": new_id, "ph": ph},
            )
        else:
            # legacy no-comment mint: the wallet redeems with the invoice
            # preimage, i.e. h = sha256(preimage) = payment_hash. Key the
            # note under hex(Q(payment_hash)) so the same k1 still works,
            # and mark comment_protected = 0 - the preimage remains the
            # bearer secret, so LUD-21 verify must keep refusing it.
            try:
                new_id = preimage_note(bytes.fromhex(ph))[0].hex()
            except Exception:
                continue
            await db.execute(
                "UPDATE lnurlmint.mints_records "
                "SET note_id = :nid, comment_protected = 0 "
                "WHERE payment_hash = :ph",
                {"nid": new_id, "ph": ph},
            )

    # --- mints: new per-mint feature toggles -----------------------------
    mcolnames = await _columns("mints")
    for col, ddl in (
        ("registration_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("nip05_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("zaps_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("zap_relays", "TEXT NOT NULL DEFAULT ''"),
        ("sunset_date", "TEXT"),
    ):
        if col not in mcolnames:
            await db.execute(
                f"ALTER TABLE lnurlmint.mints ADD COLUMN {col} {ddl}"
            )

    # --- burns: LUD-25 mutation-replay table ------------------------------
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS lnurlmint.burns (
            burn_key     TEXT NOT NULL,
            mint_id      TEXT NOT NULL,
            id           TEXT NOT NULL,
            id2          TEXT,
            amount1_msat INTEGER NOT NULL,
            amount2_msat INTEGER,
            created_at   TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """,
            PRIMARY KEY (mint_id, burn_key)
        );
        """
    )

    # --- usernames: per-mint cx1 branch registrations ---------------------
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS lnurlmint.usernames (
            mint_id      TEXT NOT NULL,
            username     TEXT NOT NULL,
            cx1          TEXT NOT NULL,
            nostr_pubkey TEXT,
            next_index   INTEGER NOT NULL DEFAULT 0,
            created_at   TIMESTAMP NOT NULL DEFAULT """
        + db.timestamp_now
        + """,
            PRIMARY KEY (mint_id, username)
        );
        """
    )

    mints_records = f"{db.references_schema}mints_records"
    await db.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_lnurlmint_mints_records_note_id
        ON {mints_records}(mint_id, note_id);
        """
    )
