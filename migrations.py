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
