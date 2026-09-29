"""NIP-57 zaps, publish-only (see nostr.py): a registered username's
payRequest says it can be zapped, its callback takes the kind 9734 and
binds the invoice to it by description hash, and once the invoice
settles the mint publishes a kind 9735 receipt signed with its own
Nostr key (mint_privkey).

Ported from upstream's test_zaps.py, adapted to the extension's async
direct-call harness: zap config is per-mint (zaps_enabled / zap_relays
on the mint row via update_mint), relays are mocked at nostr.publish.
"""

import json
import time
from hashlib import sha256
from os import urandom
from typing import Any
from unittest.mock import MagicMock

import pytest
from coincurve import PrivateKey

import lnurlmint.services as services_module
from lnurlmint import bech32m
from lnurlmint import nostr as nostr_module
from lnurlmint.crud import (
    get_mint_by_id,
    update_mint,
)
from lnurlmint.tests.conftest import (
    TEST_MINT_ID,
    TEST_WALLET,
    fresh_branch,
    register_sig,
)
from lnurlmint.views_lnurl import (
    get_pay_callback,
    get_pay_callback_for_username,
    get_payrequest_for_username,
    upsert_registered_username,
)

RELAYS = ["wss://relay.example", "wss://nos.example"]


def _mock_request() -> MagicMock:
    req = MagicMock()
    req.base_url = "http://test/"
    return req


def _zap_request(amount_msat: int | None = 21_000, recipient: str | None = None, **overrides: Any) -> dict[str, Any]:
    """A signed kind-9734 zap request event dict (see also conftest's
    zap_request_for for the json-string form)."""
    sender = PrivateKey()
    tags: list[list[str]] = [
        ["p", recipient or PrivateKey().public_key.format(compressed=True)[1:].hex()],
        ["relays", *RELAYS],
    ]
    if amount_msat is not None:
        tags.append(["amount", str(amount_msat)])
    tags.extend(overrides.pop("extra_tags", []))
    event = nostr_module.sign_event(
        nostr_module.ZAP_REQUEST_KIND, tags, "gm", sender.secret.hex()
    )
    event.update(overrides)
    return event


@pytest.fixture
def captured_publishes(monkeypatch) -> list[tuple[list[str], dict[str, Any]]]:
    """Relay publishes are captured here instead of dialled:
    (relays asked, event) per receipt."""
    sent: list[tuple[list[str], dict[str, Any]]] = []

    async def fake_publish(relays: list[str], event: dict[str, Any]) -> list[str]:
        sent.append((relays, event))
        return relays

    monkeypatch.setattr(nostr_module, "publish", fake_publish)
    return sent


async def _zap_mint():
    """Enable zaps on the test mint with an extra operator relay."""
    await update_mint(
        TEST_MINT_ID, TEST_WALLET,
        zaps_enabled=True,
        zap_relays="wss://mint.example",
        min_mint_msat=0,
    )


async def _register() -> str:
    username = f"zap{urandom(4).hex()}"  # .hex() is always lowercase already
    key, cx1 = fresh_branch()
    sig = register_sig(key, "register", username)
    assert await upsert_registered_username(
        _mock_request(), TEST_MINT_ID, username, cx1=cx1, sig=sig
    ) == {"status": "OK"}
    return username


async def _receipts(node) -> int:
    mint = await get_mint_by_id(TEST_MINT_ID)
    return await services_module.publish_zap_receipts(mint)


def _receipt_for(sent, pr: str):
    for relays, event in sent:
        if ["bolt11", pr] in event["tags"]:
            return relays, event
    return None


async def _receipt_id(payment_hash: str) -> tuple[str | None, bool] | None:
    from lnurlmint.crud import db

    row = await db.fetchone(
        "SELECT zap_receipt, minted FROM lnurlmint.mints_records "
        "WHERE payment_hash = :ph",
        {"ph": payment_hash},
    )
    return (row["zap_receipt"], bool(row["minted"])) if row else None


@pytest.mark.anyio
async def test_a_registered_username_advertises_zaps(node, db_setup, captured_publishes):
    await _zap_mint()
    username = await _register()
    data = await get_payrequest_for_username(TEST_MINT_ID, username, _mock_request())
    assert data["allowsNostr"] is True
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert data["nostrPubkey"] == nostr_module.pubkey_of(mint.mint_privkey)


@pytest.mark.anyio
async def test_the_fixed_identity_does_not(node, db_setup, captured_publishes):
    await _zap_mint()
    data = await get_payrequest_for_username(TEST_MINT_ID, "testuser", _mock_request())
    assert "allowsNostr" not in data and "nostrPubkey" not in data


@pytest.mark.anyio
async def test_without_zaps_enabled_nothing_is_advertised_and_a_zap_is_refused(
    node, db_setup
):
    username = await _register()
    data = await get_payrequest_for_username(TEST_MINT_ID, username, _mock_request())
    assert "allowsNostr" not in data
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(),
        amount=21_000, nostr=json.dumps(_zap_request()),
    )
    assert resp == {"status": "ERROR", "reason": "Zaps are not offered for this address."}


@pytest.mark.anyio
async def test_a_zap_binds_the_invoice_to_the_request_and_publishes_a_receipt(
    node, db_setup, captured_publishes
):
    await _zap_mint()
    username = await _register()
    request = _zap_request()
    raw = json.dumps(request)
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(), amount=21_000, nostr=raw
    )
    assert "pr" in resp, resp
    import bolt11

    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    # the invoice's `h` tag is sha256(zap request) — what clients check
    # the receipt against
    assert node.description_hashes[payment_hash] == sha256(raw.encode()).digest()
    # unpaid: no receipt
    await _receipts(node)
    assert _receipt_for(captured_publishes, resp["pr"]) is None

    node.settled.add(payment_hash)
    await _receipts(node)
    relays, receipt = _receipt_for(captured_publishes, resp["pr"])
    assert set(relays) == {*RELAYS, "wss://mint.example"}
    assert receipt["kind"] == 9735
    mint = await get_mint_by_id(TEST_MINT_ID)
    assert receipt["pubkey"] == nostr_module.pubkey_of(mint.mint_privkey)
    assert nostr_module.verify_event(receipt)
    tags = {t[0]: t[1:] for t in receipt["tags"]}
    assert tags["p"] == [request["tags"][0][1]]
    assert tags["P"] == [request["pubkey"]]
    assert tags["bolt11"] == [resp["pr"]]
    assert tags["description"] == [raw]
    assert tags["preimage"] == [node.preimages[payment_hash]]
    # published once, never again
    assert await _receipt_id(payment_hash) == (receipt["id"], True)
    await _receipts(node)
    assert sum(1 for _, e in captured_publishes if e["id"] == receipt["id"]) == 1


@pytest.mark.anyio
async def test_a_receipt_no_relay_takes_is_retried(
    node, db_setup, captured_publishes, monkeypatch
):
    await _zap_mint()
    username = await _register()
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(),
        amount=21_000, nostr=json.dumps(_zap_request()),
    )
    import bolt11

    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(payment_hash)

    async def nobody(relays: list[str], event: dict[str, Any]) -> list[str]:
        return []

    monkeypatch.setattr(nostr_module, "publish", nobody)
    await _receipts(node)
    assert (await _receipt_id(payment_hash))[0] is None

    async def everybody(relays: list[str], event: dict[str, Any]) -> list[str]:
        captured_publishes.append((relays, event))
        return relays

    monkeypatch.setattr(nostr_module, "publish", everybody)
    await _receipts(node)
    assert _receipt_for(captured_publishes, resp["pr"]) is not None
    assert (await _receipt_id(payment_hash))[0] is not None


@pytest.mark.anyio
async def test_an_ordinary_address_payment_publishes_nothing(
    node, db_setup, captured_publishes
):
    await _zap_mint()
    await update_mint(TEST_MINT_ID, TEST_WALLET, verify_enabled=True)
    username = await _register()
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(), amount=21_000
    )
    assert resp.get("pr"), resp
    import bolt11

    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    node.settled.add(payment_hash)
    # not a zap, so the poll leaves it to settle lazily as ever
    await _receipts(node)
    assert (await _receipt_id(payment_hash))[0] is None
    from lnurlmint.views_lnurl import verify_invoice

    r = await verify_invoice(TEST_MINT_ID, payment_hash)
    assert json.loads(r.body)["settled"] is True
    await _receipts(node)
    assert _receipt_for(captured_publishes, resp["pr"]) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "request_json, reason",
    [
        ("not json", "Zap request is not JSON."),
        (json.dumps({**_zap_request(), "kind": 1}), "Zap request is not a kind 9734 event."),
        (json.dumps({**_zap_request(), "sig": "00" * 64}), "Zap request signature is invalid."),
        (json.dumps({**_zap_request(), "content": "tampered"}), "Zap request signature is invalid."),
        (json.dumps(_zap_request(amount_msat=20_000)), "Zap request amount does not match the invoice."),
        (
            json.dumps(_zap_request(extra_tags=[["p", "ab" * 32]])),
            "Zap request needs exactly one p tag naming a pubkey.",
        ),
        (json.dumps(_zap_request(extra_tags=[["e", "nothex"]])), "Zap request e tag is malformed."),
    ],
)
async def test_a_bad_zap_request_is_refused_before_any_invoice(
    node, db_setup, request_json: str, reason: str
):
    await _zap_mint()
    username = await _register()
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(), amount=21_000, nostr=request_json
    )
    assert resp == {"status": "ERROR", "reason": reason}


@pytest.mark.anyio
async def test_a_zap_request_without_relays_is_refused(node, db_setup):
    await _zap_mint()
    username = await _register()
    request = _zap_request()
    request["tags"] = [t for t in request["tags"] if t[0] != "relays"]
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(),
        amount=21_000, nostr=json.dumps(request),
    )
    # re-signed by nobody: the id no longer matches, the first thing checked
    assert resp["status"] == "ERROR"


@pytest.mark.anyio
async def test_the_fixed_identity_refuses_a_zap(node, db_setup):
    await _zap_mint()
    resp = await get_pay_callback(
        TEST_MINT_ID, _mock_request(), 21_000,
        nostr=json.dumps(_zap_request()),
        comment=bech32m.encode_cp1(PrivateKey().public_key.format(compressed=True)[1:]),
    )
    assert resp == {"status": "ERROR", "reason": "Zaps are not offered for this address."}


def test_the_request_cannot_point_the_mint_at_arbitrary_sockets():
    request = _zap_request()
    request["tags"] = [
        ["p", "ab" * 32],
        ["relays", "ws://127.0.0.1:9735", "http://relay.example", "wss://one.example", " wss://one.example"]
        + [f"wss://r{i}.example" for i in range(20)],
    ]
    relays = nostr_module.relays_of(request)
    assert relays[:1] == ["wss://one.example"]
    assert len(relays) == 8
    assert all(r.startswith("wss://") for r in relays)


@pytest.mark.anyio
async def test_the_settlement_poll_is_bounded(node, db_setup):
    await _zap_mint()
    username = await _register()
    for _ in range(3):
        await get_pay_callback_for_username(
            TEST_MINT_ID, username, _mock_request(),
            amount=21_000, nostr=json.dumps(_zap_request()),
        )
    from lnurlmint.crud import pending_zap_mints

    mint = await get_mint_by_id(TEST_MINT_ID)
    assert len(await pending_zap_mints(mint.id, 0, 2)) == 2
    # an invoice older than the window is not polled, however new the rest are
    assert await pending_zap_mints(mint.id, int(time.time()) + 10**9, 100) == []


@pytest.mark.anyio
async def test_a_zap_with_an_empty_lud12_comment_still_gets_a_receipt(
    node, db_setup, captured_publishes
):
    """A zapping client may also send the empty LUD-12 comment advertised
    by the address. It must not block the zap or its receipt."""
    await _zap_mint()
    username = await _register()
    raw = json.dumps(_zap_request())
    resp = await get_pay_callback_for_username(
        TEST_MINT_ID, username, _mock_request(),
        amount=21_000, nostr=raw, comment="",
    )
    assert "pr" in resp, resp
    import bolt11

    payment_hash = bolt11.decode(resp["pr"]).payment_hash
    assert node.description_hashes[payment_hash] == sha256(raw.encode()).digest()

    node.settled.add(payment_hash)
    await _receipts(node)
    assert _receipt_for(captured_publishes, resp["pr"]) is not None
