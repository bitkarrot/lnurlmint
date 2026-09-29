"""BIP-341 output-key arithmetic on PUBLIC data only.

Ported from lnurlcash-kernel's ``taproot.py`` (MIT, lnurlcash/lnurl-mint
ecosystem) so the extension has no native dependency: a mint looks notes up
by their output key Q, but a script-path spend only reveals a leaf and a
control block - Q has to be recomputed from them (fold the merkle path,
tweak the internal key).

Pure Python and deliberately not constant-time: every input here (keys,
leaves, control blocks) is public, nothing secret ever passes through it.
"""

from __future__ import annotations

import hashlib

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

TAPLEAF_VERSION = 0xC0
_TAPLEAF_VERSION_MASK = 0xFE
_MAX_MERKLE_DEPTH = 128

# BIP-341's own nothing-up-my-sleeve point: lift_x(sha256(G uncompressed)).
# Nobody knows its discrete log, so an output key built on it has no key
# path - only its script tree can ever spend it.
NUMS_H = bytes.fromhex(
    "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0"
)

Point = tuple[int, int] | None


def _add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and a[1] != b[1]:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], _P - 2, _P) % _P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], _P - 2, _P) % _P
    x = (lam * lam - a[0] - b[0]) % _P
    return x, (lam * (a[0] - x) - a[1]) % _P


def _mul(pt: Point, k: int) -> Point:
    acc: Point = None
    while k:
        if k & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        k >>= 1
    return acc


def _lift_x(x: bytes) -> Point:
    xi = int.from_bytes(x, "big")
    if xi >= _P:
        return None
    y_sq = (pow(xi, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != y_sq:
        return None
    return xi, y if y % 2 == 0 else _P - y


def is_xonly_point(x: bytes) -> bool:
    """Is `x` a valid BIP-340 x-only public key?"""
    return len(x) == 32 and _lift_x(x) is not None


def tagged_hash(tag: str, msg: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _compact_size(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    return b"\xfe" + n.to_bytes(4, "little")


def tapleaf_hash(script: bytes, version: int = TAPLEAF_VERSION) -> bytes:
    return tagged_hash("TapLeaf", bytes([version]) + _compact_size(len(script)) + script)


def tweak(internal_key: bytes, merkle_root: bytes) -> tuple[bytes, int] | None:
    """(Q x-only, parity of Q's y) for Q = lift_x(internal_key) + t·G, or
    None if internal_key is not a curve point or the tweak overflows."""
    p = _lift_x(internal_key)
    if p is None:
        return None
    t = int.from_bytes(tagged_hash("TapTweak", internal_key + merkle_root), "big")
    if t >= _N:
        return None
    q = _add(p, _mul(_G, t))
    if q is None:
        return None
    return q[0].to_bytes(32, "big"), q[1] & 1


def output_key(script: bytes, control_block: bytes) -> bytes | None:
    """The output key Q that `(script, control_block)` commits to, or None
    if the control block is malformed. Checks the control block's parity
    bit too, so a Q returned here is exactly the one a real verifier will
    accept the spend against."""
    if len(control_block) < 33 or (len(control_block) - 33) % 32:
        return None
    if (len(control_block) - 33) // 32 > _MAX_MERKLE_DEPTH:
        return None
    node = tapleaf_hash(script, control_block[0] & _TAPLEAF_VERSION_MASK)
    for i in range(33, len(control_block), 32):
        sibling = control_block[i : i + 32]
        lo, hi = sorted((node, sibling))
        node = tagged_hash("TapBranch", lo + hi)
    tweaked = tweak(control_block[1:33], node)
    if tweaked is None or tweaked[1] != control_block[0] & 1:
        return None
    return tweaked[0]


def preimage_leaf(image: bytes) -> bytes:
    """`OP_SHA256 <image> OP_EQUAL` - spendable by revealing the preimage
    alone."""
    if len(image) != 32:
        raise ValueError("image must be a 32-byte sha256")
    return b"\xa8\x20" + image + b"\x87"


def preimage_note(image: bytes) -> tuple[bytes, bytes]:
    """(Q, control block) of a bearer note: NUMS internal key, one
    preimage leaf, so revealing the preimage is the only way to spend
    it."""
    leaf = preimage_leaf(image)
    tweaked = tweak(NUMS_H, tapleaf_hash(leaf))
    assert tweaked is not None  # NUMS_H is a valid point, a hash tweak never overflows in practice
    q, parity = tweaked
    return q, bytes([TAPLEAF_VERSION | parity]) + NUMS_H
