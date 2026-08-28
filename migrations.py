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
