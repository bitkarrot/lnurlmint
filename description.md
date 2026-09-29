# lnurlmint

Mint and share Lightning-funded bearer notes with LNbits.

lnurlmint implements [LUD-25 lnurlcash](https://github.com/lnurlw/LUDs/blob/luds/25.md), allowing each LNbits wallet to run its own mint. Notes are taproot output keys that circulate offline as `lnurlw://` withdraw links and can be redeemed, rotated, split, merged, or melted back into a Lightning payment.

## Highlights

- Create and configure per-wallet mints.
- Issue taproot-keyed bearer notes backed by Lightning payments (cp1/ck1/cw1, cs1 certificates).
- Rotate, split, merge, and melt notes through compatible LNURL wallets.
- Register lightning usernames, serve NIP-05, and publish NIP-57 zap receipts.
- Verify settlement status and protect against double-spends.
- Use the public mint page to share a mint QR code and LNURL.

Compatible with [lnurl-wallet](https://github.com/dni/lnurl-wallet) and other LUD-03/LUD-06/LUD-25 clients.
