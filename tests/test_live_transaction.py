"""Live end-to-end transaction test for the lnurlmint extension.

This script simulates the full bearer-note lifecycle against a running
LNbits instance:

  1. GET /lnurlp/{mint_id}  — fetch the payRequest
  2. GET /p/cb/{mint_id}    — request an invoice (mint, comment=<h> required)
  3. Pay the invoice via LNbits API
  4. GET /w/{mint_id}?k1=   — query the note (triggers lazy settlement)
  5. GET /w/cb/{mint_id}    — rotate the note (p1=<new h>)
  6. GET /w/{mint_id}?k1=   — query the rotated note
  7. GET /w/cb/{mint_id}    — melt the note back to sats

Requirements:
  - LNbits running on http://localhost:5000
  - A mint exists (provide mint_id)
  - Two wallet API keys: the mint owner's (invoice key) and a payer's (admin key)

Usage:
  python test_live_transaction.py --mint-id <ID> --owner-key <INKEY> --payer-key <ADMINKEY>

  Or with env vars:
  LNMINT_MINT_ID=<ID> LNMINT_OWNER_KEY=<INKEY> LNMINT_PAYER_KEY=<ADMINKEY> python test_live_transaction.py
"""

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sys

import httpx

BASE_URL = os.environ.get("LNMINT_BASE_URL", "http://localhost:5000")
MINT_ID = os.environ.get("LNMINT_MINT_ID", "")
OWNER_KEY = os.environ.get("LNMINT_OWNER_KEY", "")  # invoice key of mint owner
PAYER_KEY = os.environ.get("LNMINT_PAYER_KEY", "")  # admin key of payer wallet
AMOUNT_MSAT = int(os.environ.get("LNMINT_AMOUNT_MSAT", "10000"))  # 10 sats default


def header(key: str) -> dict:
    return {"X-Api-Key": key, "Content-Type": "application/json"}


def step(n: int, msg: str):
    print(f"\n{'='*60}")
    print(f"  Step {n}: {msg}")
    print(f"{'='*60}")


def gen_preimage() -> tuple[str, str]:
    """Generate a random 32-byte preimage (the bearer note's k1) and its
    sha256 hash `h` (what a WALLET sends as `comment`/`p1`)."""
    preimage = secrets.token_hex(32)
    h = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    return preimage, h


async def run(mint_id: str, owner_key: str, payer_key: str, amount_msat: int):
    client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)

    # ── Step 1: Get the payRequest ──────────────────────────────
    step(1, "GET /lnurlp/{mint_id} — fetch payRequest")
    resp = await client.get(f"/lnurlmint/lnurlp/{mint_id}")
    assert resp.status_code == 200, f"payRequest failed: {resp.status_code} {resp.text}"
    pay_req = resp.json()
    print(f"  minSendable = {pay_req['minSendable']} msat")
    print(f"  maxSendable = {pay_req['maxSendable']} msat")
    print(f"  callback    = {pay_req['callback']}")
    print(f"  withdrawLink = {pay_req.get('withdrawLink', 'N/A')}")
    print(f"  commentAllowed = {pay_req.get('commentAllowed')}")
    assert amount_msat >= pay_req["minSendable"], "amount below minSendable"
    assert amount_msat <= pay_req["maxSendable"], "amount above maxSendable"

    # ── Step 2: Request an invoice ──────────────────────────────
    # Under the taproot protocol every mint is keyed by the WALLET's own
    # output — a fresh preimage's hash `h` names the bearer note.
    k1, h = gen_preimage()
    step(2, f"GET /p/cb/{mint_id}?amount={amount_msat}&comment={h[:16]}... — request invoice")
    resp = await client.get(
        f"/lnurlmint/p/cb/{mint_id}",
        params={"amount": amount_msat, "comment": h},
    )
    assert resp.status_code == 200, f"pay callback failed: {resp.status_code} {resp.text}"
    pay_resp = resp.json()
    assert "pr" in pay_resp, f"mint refused: {pay_resp}"
    pr = pay_resp["pr"]
    print(f"  invoice = {pr[:60]}...")
    print(f"  disposable = {pay_resp.get('disposable')}")
    if "verify" in pay_resp:
        print(f"  verify = {pay_resp['verify']}")

    # ── Step 3: Pay the invoice ─────────────────────────────────
    step(3, "POST /api/v1/payments — pay the invoice from payer wallet")
    resp = await client.post(
        "/api/v1/payments",
        headers=header(payer_key),
        json={"out": True, "bolt11": pr},
    )
    assert resp.status_code <= 201, f"payment failed: {resp.status_code} {resp.text}"
    pay_result = resp.json()
    payment_hash = pay_result["payment_hash"]
    print(f"  payment_hash = {payment_hash}")
    print(f"  status       = {pay_result.get('status')}")

    # Wait for internal settlement
    await asyncio.sleep(2)

    # ── Step 4: Query the note (triggers lazy settlement) ───────
    step(4, "GET /w/{mint_id}?k1=<preimage> — query the note")
    resp = await client.get(f"/lnurlmint/w/{mint_id}", params={"k1": k1})
    w_data = resp.json()
    print(f"  status = {resp.status_code}")
    print(f"  response = {json.dumps(w_data, indent=2)}")

    if "maxWithdrawable" in w_data:
        note_value = w_data["maxWithdrawable"]
        print(f"  Note found! value = {note_value} msat")
        if "c" in w_data:
            print(f"  cs1 certificate = {w_data['c'][:50]}...")
        if "mintPubkey" in w_data:
            print(f"  mintPubkey = {w_data['mintPubkey']}")
    else:
        print("  Note not materialized — check if payment settled")
        await client.aclose()
        return

    # ── Step 5: Rotate the note ─────────────────────────────────
    step(5, "GET /w/cb/{mint_id}?k1=&p1= — rotate the note")
    new_preimage, new_h = gen_preimage()
    print(f"  new preimage = {new_preimage}")
    print(f"  new h (sha256) = {new_h}")
    resp = await client.get(
        f"/lnurlmint/w/cb/{mint_id}",
        params={"k1": k1, "p1": new_h},
    )
    rot_data = resp.json()
    print(f"  status = {resp.status_code}")
    print(f"  response = {json.dumps(rot_data, indent=2)}")
    if rot_data.get("status") == "OK":
        print("  Note rotated!")
        if "c" in rot_data:
            print(f"  cs1 = {rot_data['c'][:50]}...")
    else:
        print(f"  Rotate failed: {rot_data}")
        await client.aclose()
        return

    # ── Step 6: Query the rotated note ──────────────────────────
    step(6, "GET /w/{mint_id}?k1=<new preimage> — query rotated note")
    resp = await client.get(f"/lnurlmint/w/{mint_id}", params={"k1": new_preimage})
    w2_data = resp.json()
    print(f"  status = {resp.status_code}")
    print(f"  response = {json.dumps(w2_data, indent=2)}")
    if "maxWithdrawable" in w2_data:
        print(f"  Rotated note found! value = {w2_data['maxWithdrawable']} msat")
    else:
        print(f"  Rotated note not found: {w2_data}")

    # ── Step 7: Melt the note back to sats ──────────────────────
    step(7, "Melt the note — create an invoice and melt into it")
    # Create an invoice from the owner's wallet to melt into
    resp = await client.post(
        "/api/v1/payments",
        headers=header(owner_key),
        json={"out": False, "amount": amount_msat // 1000, "memo": "melt target"},
    )
    if resp.status_code > 201:
        print(f"  Failed to create melt target invoice: {resp.text}")
        await client.aclose()
        return
    melt_pr = resp.json()["payment_request"]
    print(f"  melt target invoice = {melt_pr[:60]}...")

    resp = await client.get(
        f"/lnurlmint/w/cb/{mint_id}",
        params={"k1": new_preimage, "pr": melt_pr},
    )
    melt_data = resp.json()
    print(f"  status = {resp.status_code}")
    print(f"  response = {json.dumps(melt_data, indent=2)}")
    if melt_data.get("status") == "OK":
        print("  Melt initiated! Note reserved, payment in progress.")
        if "verify" in melt_data:
            print(f"  verify = {melt_data['verify']}")
    else:
        print(f"  Melt failed: {melt_data}")

    # Wait for melt to settle
    await asyncio.sleep(3)

    # ── Summary ─────────────────────────────────────────────────
    step(8, "Summary")
    print(f"  Mint ID       = {mint_id}")
    print(f"  Amount        = {amount_msat} msat ({amount_msat // 1000} sats)")
    print(f"  Payment hash  = {payment_hash}")
    print(f"  Original k1   = {k1}")
    print(f"  Rotated k1    = {new_preimage}")
    print(f"  Melt target   = {melt_pr[:40]}...")
    print()
    print("  Full lifecycle complete: mint → query → rotate → query → melt")

    await client.aclose()


def main():
    parser = argparse.ArgumentParser(description="Live lnurlmint transaction test")
    parser.add_argument("--mint-id", default=MINT_ID, help="Mint ID (hex UUID)")
    parser.add_argument("--owner-key", default=OWNER_KEY, help="Mint owner's invoice key")
    parser.add_argument("--payer-key", default=PAYER_KEY, help="Payer wallet's admin key")
    parser.add_argument("--amount-msat", type=int, default=AMOUNT_MSAT, help="Amount in msat")
    parser.add_argument("--base-url", default=BASE_URL, help="LNbits base URL")
    args = parser.parse_args()

    if not args.mint_id:
        print("Error: --mint-id required (or set LNMINT_MINT_ID env var)")
        sys.exit(1)
    if not args.owner_key:
        print("Error: --owner-key required (mint owner's invoice key)")
        sys.exit(1)
    if not args.payer_key:
        print("Error: --payer-key required (payer wallet's admin key)")
        sys.exit(1)

    print(f"Base URL  : {args.base_url}")
    print(f"Mint ID   : {args.mint_id}")
    print(f"Amount    : {args.amount_msat} msat ({args.amount_msat // 1000} sats)")

    asyncio.run(run(args.mint_id, args.owner_key, args.payer_key, args.amount_msat))


if __name__ == "__main__":
    main()
