"""NIP-57 zaps, publish-only. Ported from upstream lnurl-mint's nostr.py
(MIT).

This mint validates a zap request (kind 9734) on the pay callback, commits
the invoice to it by description hash, and publishes the zap receipt (kind
9735) once the invoice settles. It never subscribes to a relay: every
relay connection here is a write of one event, then a close.

The mint's Nostr signing key is its own `mint_privkey` - the same
per-mint secp256k1 identity that signs `cs1` certificates (upstream uses a
separate NOSTR_KEY env var; a per-mint key is the only identity this
extension has, and reusing it keeps the schema unchanged).
"""

import asyncio
import json
import re
import time
from hashlib import sha256
from os import urandom
from typing import Any, Optional

import websockets
from coincurve import PrivateKey, PublicKeyXOnly
from loguru import logger

ZAP_REQUEST_KIND = 9734
ZAP_RECEIPT_KIND = 9735

_HEX32 = re.compile(r"^[0-9a-f]{64}$")
# The request's relays are the zapper's choice, and this mint dials them:
# TLS only, so a request cannot point it at a plain socket on a host it
# can reach and the zapper cannot, and never more than a handful.
_RELAY_URL = re.compile(r"^wss://[^/\s]+")
_MAX_REQUEST_RELAYS = 8
# one relay's share of a publish: dial, send, hear the OK (or not)
_RELAY_TIMEOUT_SECONDS = 10


def event_id(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> str:
    """NIP-01's event id: sha256 of the canonical serialization."""
    serialized = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    return sha256(serialized.encode()).hexdigest()


def verify_event(event: dict[str, Any]) -> bool:
    """The id recomputes and the BIP-340 signature is the pubkey's."""
    try:
        computed = event_id(event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"])
        if computed != event["id"]:
            return False
        return PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])
        )
    except Exception:
        return False


def pubkey_of(secret_key_hex: str) -> str:
    """The x-only public key (hex) a Nostr secret key signs as."""
    return PrivateKey(bytes.fromhex(secret_key_hex)).public_key.format()[1:].hex()


def sign_event(
    kind: int, tags: list[list[str]], content: str, secret_key_hex: str, created_at: Optional[int] = None
) -> dict[str, Any]:
    key = PrivateKey(bytes.fromhex(secret_key_hex))
    pubkey = key.public_key.format()[1:].hex()
    created_at = int(time.time()) if created_at is None else created_at
    ident = event_id(pubkey, created_at, kind, tags, content)
    sig = key.sign_schnorr(bytes.fromhex(ident), urandom(32)).hex()
    return {
        "id": ident,
        "pubkey": pubkey,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


def _tags(event: dict[str, Any], name: str) -> list[list[str]]:
    return [t for t in event.get("tags", []) if isinstance(t, list) and t and t[0] == name]


def validate_zap_request(raw: str, amount_msat: int) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """NIP-57 Appendix D, the LNURL server's checks on `nostr`: a signed
    kind 9734 with exactly one `p`, at most one `e`, a `relays` tag, and
    an `amount` (if any) equal to the invoice's. Returns (event, None) or
    (None, why). Nothing here says who `p` may be: this mint holds no
    Nostr key for a username, so it attests to what was paid, not to whom
    the payer meant it."""
    try:
        event = json.loads(raw)
    except ValueError:
        return None, "Zap request is not JSON."
    if not isinstance(event, dict) or event.get("kind") != ZAP_REQUEST_KIND:
        return None, "Zap request is not a kind 9734 event."
    if not isinstance(event.get("tags"), list) or not all(
        isinstance(t, list) and all(isinstance(v, str) for v in t) for t in event["tags"]
    ):
        return None, "Zap request tags are malformed."
    if not verify_event(event):
        return None, "Zap request signature is invalid."
    p = _tags(event, "p")
    if len(p) != 1 or len(p[0]) < 2 or not _HEX32.match(p[0][1]):
        return None, "Zap request needs exactly one p tag naming a pubkey."
    e = _tags(event, "e")
    if len(e) > 1 or any(len(t) < 2 or not _HEX32.match(t[1]) for t in e):
        return None, "Zap request e tag is malformed."
    if not _tags(event, "relays"):
        return None, "Zap request has no relays tag."
    amount = _tags(event, "amount")
    if amount and (len(amount[0]) < 2 or amount[0][1] != str(amount_msat)):
        return None, "Zap request amount does not match the invoice."
    return event, None


def relays_of(zap_request: dict[str, Any]) -> list[str]:
    """The relays the zap request asks the receipt to go to. NIP-57 puts
    them all in ONE tag: ["relays", url, url, ...]. wss:// only, the first
    _MAX_REQUEST_RELAYS distinct ones."""
    urls: list[str] = []
    for tag in _tags(zap_request, "relays"):
        for url in tag[1:]:
            url = url.strip()
            if _RELAY_URL.match(url) and url not in urls:
                urls.append(url)
    return urls[:_MAX_REQUEST_RELAYS]


def zap_receipt(
    zap_request: dict[str, Any], raw_request: str, bolt11: str, preimage_hex: Optional[str], secret_key_hex: str
) -> dict[str, Any]:
    """The kind 9735 receipt for a settled zap: the request's e/p/a tags,
    the sender as P, the invoice, the request verbatim as description
    (what a client hashes against the invoice), and the preimage where
    the funding source could still say."""
    tags = [t for t in zap_request["tags"] if t[0] in ("e", "p", "a")]
    tags += [["P", zap_request["pubkey"]], ["bolt11", bolt11], ["description", raw_request]]
    if preimage_hex:
        tags.append(["preimage", preimage_hex])
    return sign_event(ZAP_RECEIPT_KIND, tags, "", secret_key_hex)


async def _publish_one(relay: str, event: dict[str, Any]) -> bool:
    async with websockets.connect(relay, open_timeout=_RELAY_TIMEOUT_SECONDS) as ws:
        await ws.send(json.dumps(["EVENT", event]))
        deadline = time.monotonic() + _RELAY_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
            # anything else the relay says first (NOTICE, AUTH) is not the answer
            if isinstance(message, list) and len(message) >= 3 and message[0] == "OK" and message[1] == event["id"]:
                return bool(message[2])


async def publish(relays: list[str], event: dict[str, Any]) -> list[str]:
    """Send `event` to every relay, each on its own short-lived
    connection, and return the ones that answered OK. A relay that is
    down, slow or refuses costs nothing but a log line."""
    results = await asyncio.gather(*(_publish_one(r, event) for r in relays), return_exceptions=True)
    accepted: list[str] = []
    for relay, result in zip(relays, results):
        if result is True:
            accepted.append(relay)
        else:
            why = result if isinstance(result, BaseException) else "not accepted"
            logger.info(f"zap receipt not taken by {relay}: {why}")
    return accepted
