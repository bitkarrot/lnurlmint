"""LUD-25 notes and spends.

Every note is a BIP-341 taproot output key Q, stored under hex(Q). Every
`k1` a redeemer presents is a spend of one: a `ck1` (key path), a `cw1`
(script path), or the 64-hex preimage of a bearer note, the short form of
that note's `cw1`.

Ported from lnurlcash-kernel (MIT): encoding, taproot-output-key recovery,
sighash, leaf policy and time claims are pure Python and live here / in
taproot.py. The one thing that genuinely needs Bitcoin Core's own script
interpreter is a script-path spend of an ARBITRARY leaf - for that this
module defers to `lnurlcashkernel` when the operator has it installed
(linux wheels ship on PyPI). Without it:

  - key-path spends (`ck1`) are still fully verified - a BIP-340 schnorr
    signature by Q over the canonical spend transaction's sighash, via
    coincurve;
  - script-path spends of exactly the bearer-note leaf (NUMS key, one
    `OP_SHA256 <h> OP_EQUAL` leaf, a lone preimage witness) are verified
    directly - the script's whole semantics are `sha256(witness) == h`;
  - any other leaf is rejected, loudly, as unverifiable.

Time is the one thing no kernel decides: `now` and the note's recorded
`locked_at` are passed in here, so a timelock "verified" means this mint
asserted its own clock - a custodial policy, never a consensus proof.
"""

from __future__ import annotations

import hashlib
import struct
import time
from dataclasses import dataclass

from coincurve import PublicKeyXOnly

from .taproot import (
    TAPLEAF_VERSION,
    is_xonly_point,
    output_key,
    preimage_leaf,
    preimage_note,
    tagged_hash,
    tapleaf_hash,
)

try:
    import lnurlcashkernel as _kernel
except ImportError:  # optional: arbitrary-cw1 verification only
    _kernel = None


# ---------------------------------------------------------------------------
# Errors (lnurlcashkernel/errors.py)
# ---------------------------------------------------------------------------


class KernelError(RuntimeError):
    """The verification backend is missing/unusable, or was called
    incorrectly. A bug or a broken install, never a legitimate denial - so
    it is never swallowed into a SpendRejected."""


class SpendRejected(Exception):
    """This spend must be denied. `reason` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedScript(SpendRejected):
    """The leaf uses a tapscript upgrade hook (an unknown leaf version or
    an OP_SUCCESSx opcode) that would otherwise succeed unconditionally -
    or, on a kernel-less install, a leaf this mint cannot execute at all."""


class ScriptInvalid(SpendRejected):
    """Script verification rejected the spend."""


class TimeClaimRejected(SpendRejected):
    """The redeemer's claimed locktime/sequence is not yet satisfied by
    the mint's clock."""


# ---------------------------------------------------------------------------
# Encoding (lnurlcashkernel/encoding.py)
#
# cp1   Q (32)
# ck1   Q (32) || sig (64)
# cw1   u32 locktime || u32 sequence
#       || u16 len(script) || script || u16 len(control_block) || control_block
#       || (u16 len(witness_i) || witness_i)*   bottom of stack first
#
# bech32m, no length cap (see bech32m.py). Short forms: 64 hex chars in a
# `k1` position are a bearer note's preimage (its spend is constructed
# here); 64 hex chars in a `cp1` position are its hash `h`.
# ---------------------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32M_CONST = 0x2BC830A3
_GEN = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


def _polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= _GEN[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert_bits(data: list[int], frm: int, to: int) -> bytes | None:
    """5-bit groups -> bytes, strictly (BIP-173: padding <= 4 bits, all
    zero)."""
    acc = bits = 0
    out = bytearray()
    for v in data:
        acc = (acc << frm) | v
        bits += frm
        while bits >= to:
            bits -= to
            out.append((acc >> bits) & ((1 << to) - 1))
    if bits >= frm or (acc << (to - bits)) & ((1 << to) - 1):
        return None
    return bytes(out)


def _convert_bits_out(data: bytes) -> list[int]:
    """bytes -> 5-bit groups, zero-padded."""
    acc = bits = 0
    out = []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def bech32m_encode(hrp: str, payload: bytes) -> str:
    data = _convert_bits_out(payload)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ _BECH32M_CONST
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[d] for d in data + checksum)


def bech32m_decode(value: str) -> tuple[str, bytes] | None:
    """Return (hrp, payload bytes), or None if `value` is not valid
    bech32m."""
    if value != value.lower() and value != value.upper():
        return None  # mixed case
    value = value.lower()
    pos = value.rfind("1")
    if pos < 1 or pos + 7 > len(value):
        return None
    hrp, data_part = value[:pos], value[pos + 1 :]
    if any(ord(c) < 33 or ord(c) > 126 for c in hrp):
        return None
    if any(c not in _CHARSET for c in data_part):
        return None
    data = [_CHARSET.index(c) for c in data_part]
    if _polymod(_hrp_expand(hrp) + data) != _BECH32M_CONST:
        return None
    payload = _convert_bits(data[:-6], 5, 8)
    return None if payload is None else (hrp, payload)


CANONICAL_LOCKTIME = 0
CANONICAL_SEQUENCE = 0xFFFFFFFF  # final: no time claim


def decode_cp1(value: str) -> bytes | None:
    """A `cp1` note output key: the 32-byte x-only taproot output key Q.
    None unless Q is the x coordinate of a curve point - no spend could
    open it."""
    decoded = bech32m_decode(value.strip())
    if decoded is None or decoded[0] != "cp" or len(decoded[1]) != 32:
        return None
    if not is_xonly_point(decoded[1]):
        return None
    return decoded[1]


def encode_cp1(q: bytes) -> str:
    if len(q) != 32:
        raise ValueError("Q must be a 32-byte x-only key")
    return bech32m_encode("cp", q)


@dataclass(frozen=True)
class Spend:
    """A spend of a note. Key path (`ck1`): `script`/`control_block` are
    None and `witness` is `(sig,)`. Script path (`cw1`): both are set."""

    locktime: int
    sequence: int
    script: bytes | None
    control_block: bytes | None
    witness: tuple[bytes, ...]
    key: bytes | None = None  # key path only: the Q it signs for

    @property
    def key_path(self) -> bool:
        return self.script is None

    @property
    def stack(self) -> list[bytes]:
        """The full segwit witness stack a verifier checks."""
        if self.key_path:
            return list(self.witness)
        return [*self.witness, self.script, self.control_block]

    @property
    def output_key(self) -> bytes | None:
        """The Q this spend opens - for a script path recomputed from the
        control block, None if that is malformed. What a mint looks up."""
        if self.key_path:
            return self.key
        return output_key(self.script, self.control_block)


def decode_ck1(value: str) -> Spend | None:
    """A `ck1` key-path spend, or None if malformed in any way."""
    decoded = bech32m_decode(value.strip())
    if decoded is None or decoded[0] != "ck" or len(decoded[1]) != 32 + 64:
        return None
    data = decoded[1]
    return Spend(
        CANONICAL_LOCKTIME, CANONICAL_SEQUENCE, None, None, (data[32:],), key=data[:32]
    )


def decode_cw1(value: str) -> Spend | None:
    """A `cw1` script-path spend, or None if malformed in any way."""
    decoded = bech32m_decode(value.strip())
    if decoded is None or decoded[0] != "cw":
        return None
    data = decoded[1]
    if len(data) < 8:
        return None
    locktime = int.from_bytes(data[0:4], "big")
    sequence = int.from_bytes(data[4:8], "big")
    parts: list[bytes] = []
    i = 8
    while i < len(data):
        if i + 2 > len(data):
            return None  # a truncated length prefix
        n = int.from_bytes(data[i : i + 2], "big")
        i += 2
        if i + n > len(data):
            return None  # claims more bytes than remain
        parts.append(data[i : i + n])
        i += n
    if len(parts) < 2:  # script and control block are mandatory
        return None
    return Spend(locktime, sequence, parts[0], parts[1], tuple(parts[2:]))


def encode_ck1(spend: Spend) -> str:
    if not spend.key_path:
        raise ValueError("a ck1 is a key-path spend")
    if (
        spend.key is None
        or len(spend.key) != 32
        or len(spend.witness) != 1
        or len(spend.witness[0]) != 64
    ):
        raise ValueError("a key-path spend is exactly Q and one 64-byte signature")
    return bech32m_encode("ck", spend.key + spend.witness[0])


def encode_cw1(spend: Spend) -> str:
    if spend.key_path:
        raise ValueError("a cw1 is a script-path spend")
    out = spend.locktime.to_bytes(4, "big") + spend.sequence.to_bytes(4, "big")
    for item in (spend.script, spend.control_block, *spend.witness):
        assert item is not None
        out += len(item).to_bytes(2, "big") + item
    return bech32m_encode("cw", out)


def encode_spend(spend: Spend) -> str:
    """`ck1` for a key-path spend, `cw1` for a script-path one."""
    return encode_ck1(spend) if spend.key_path else encode_cw1(spend)


def _hex32(value: str) -> bytes | None:
    if len(value) != 64 or any(c not in "0123456789abcdefABCDEF" for c in value):
        return None
    return bytes.fromhex(value)


def preimage_spend(preimage: bytes) -> Spend:
    """The script-path spend of the bearer note locked to
    sha256(preimage)."""
    image = hashlib.sha256(preimage).digest()
    _, control = preimage_note(image)
    return Spend(
        CANONICAL_LOCKTIME,
        CANONICAL_SEQUENCE,
        preimage_leaf(image),
        control,
        (preimage,),
    )


def decode_spend(value: str) -> Spend | None:
    """Whatever a redeemer put in `k1`: a `ck1`, a `cw1`, or the 64-hex
    preimage of a bearer note (short form). None if it is none of them."""
    value = value.strip()
    preimage = _hex32(value)
    if preimage is not None:
        return preimage_spend(preimage)
    return decode_ck1(value) or decode_cw1(value)


def decode_note(value: str) -> bytes | None:
    """Whatever was put where a `cp1` goes (a mint comment, p1/p2, ?p=): a
    `cp1`, or the 64-hex `h` of a bearer note (short form). Returns Q."""
    value = value.strip()
    image = _hex32(value)
    if image is not None:
        return preimage_note(image)[0]
    return decode_cp1(value)


# ---------------------------------------------------------------------------
# Leaf policy (lnurlcashkernel/script.py)
# ---------------------------------------------------------------------------

_TAPLEAF_VERSION_MASK = 0xFE
_OP_PUSHDATA1, _OP_PUSHDATA2, _OP_PUSHDATA4 = 0x4C, 0x4D, 0x4E

# BIP-342: 80, 98, 126-129, 131-134, 137-138, 141-142, 149-153, 187-254
OP_SUCCESS = frozenset(
    [
        80,
        98,
        *range(126, 130),
        *range(131, 135),
        137,
        138,
        141,
        142,
        *range(149, 154),
        *range(187, 255),
    ]
)


def _opcodes(script: bytes):
    """The opcodes of `script`, skipping pushed data. Stops at a truncated
    push - consensus fails such a script on its own."""
    i = 0
    while i < len(script):
        op = script[i]
        i += 1
        if 1 <= op <= 75:
            i += op
        elif op == _OP_PUSHDATA1:
            if i + 1 > len(script):
                return
            i += 1 + script[i]
        elif op == _OP_PUSHDATA2:
            if i + 2 > len(script):
                return
            i += 2 + int.from_bytes(script[i : i + 2], "little")
        elif op == _OP_PUSHDATA4:
            if i + 4 > len(script):
                return
            i += 4 + int.from_bytes(script[i : i + 4], "little")
        else:
            yield op


def check_leaf(script: bytes, control_block: bytes) -> None:
    """Raise UnsupportedScript if this leaf uses an upgrade hook."""
    if not control_block or control_block[0] & _TAPLEAF_VERSION_MASK != TAPLEAF_VERSION:
        raise UnsupportedScript("unknown tapleaf version")
    if any(op in OP_SUCCESS for op in _opcodes(script)):
        raise UnsupportedScript("leaf uses a reserved OP_SUCCESS opcode")


# ---------------------------------------------------------------------------
# Time claims (lnurlcashkernel/policy.py)
# ---------------------------------------------------------------------------

LOCKTIME_THRESHOLD = 500_000_000  # below this an nLockTime is a block height
CSV_TYPE_FLAG = 1 << 22  # BIP68: relative lock is time-based (512 s units)
CSV_VALUE_MASK = 0xFFFF
CSV_GRANULARITY_SECONDS = 512

SEQUENCE_FINAL = 0xFFFFFFFF
SEQUENCE_DISABLE_FLAG = 1 << 31


def check_time_claim(*, locktime: int, sequence: int, now: int, locked_at: int) -> None:
    """Raise TimeClaimRejected unless the redeemer's claimed
    locktime/sequence is acceptable at `now` (Unix seconds) for a note
    locked at `locked_at`."""
    if locktime:
        if locktime < LOCKTIME_THRESHOLD:
            raise TimeClaimRejected(
                "block-height locktimes have no meaning without a chain"
            )
        if locktime > now:
            raise TimeClaimRejected(
                f"locktime {locktime} is in the future (now {now})"
            )

    if sequence & SEQUENCE_DISABLE_FLAG:
        return  # no relative lock claimed (includes the fully-final 0xffffffff)

    if not sequence & CSV_TYPE_FLAG:
        raise TimeClaimRejected(
            "block-count relative locks have no meaning without a chain"
        )
    elapsed = now - locked_at
    required = (sequence & CSV_VALUE_MASK) * CSV_GRANULARITY_SECONDS
    if elapsed < required:
        raise TimeClaimRejected(
            f"relative lock of {required}s not yet satisfied "
            f"({max(elapsed, 0)}s elapsed)"
        )


# ---------------------------------------------------------------------------
# The canonical spend transaction + sighash (lnurlcashkernel/verify.py +
# sighash.py)
#
# nVersion    2   (OP_CHECKSEQUENCEVERIFY requires >= 2)
# vin[0]      prevout (tagged_hash("LNURLcash/mint", domain), 0),
#             empty scriptSig, nSequence = the redeemer's claimed sequence
# vout[0]     value 0, empty scriptPubKey
# nLockTime   = the redeemer's claimed locktime
# spent       (OP_1 <Q>, 0) - a P2TR output worth 0
# ---------------------------------------------------------------------------

CANONICAL_TX_VERSION = 2
TX_VERSION = 2
SPENT_AMOUNT = 0
_U32_MAX = 0xFFFFFFFF


def p2tr_script(output_key: bytes) -> bytes:
    """scriptPubKey for a taproot output: OP_1 <32-byte key>."""
    return b"\x51\x20" + output_key


def spend_prevout(domain: str) -> bytes:
    """The canonical transaction's prevout txid for the mint at
    `domain`."""
    if not domain:
        raise ValueError("domain is required")
    return tagged_hash("LNURLcash/mint", domain.lower().encode())


def build_spend_tx(
    *, domain: str, stack: list[bytes], locktime: int, sequence: int
) -> bytes:
    """The canonical synthetic spend transaction, with the segwit witness
    `stack` (`[sig]` for a key path, `[*witness, script, control_block]`
    for a script path)."""
    out = struct.pack("<i", TX_VERSION) + b"\x00\x01"  # segwit marker + flag
    out += b"\x01" + spend_prevout(domain) + struct.pack("<I", 0)
    out += b"\x00" + struct.pack("<I", sequence)  # empty scriptSig
    out += b"\x01" + struct.pack("<q", 0) + b"\x00"  # one empty output
    out += _compact_size(len(stack))
    for item in stack:
        out += _compact_size(len(item)) + item
    return out + struct.pack("<I", locktime)


def sig_msg(
    *,
    output_key: bytes,
    domain: str,
    locktime: int,
    sequence: int,
    leaf_script: bytes | None,
) -> bytes:
    """BIP-341's SigMsg for input 0 of the canonical spend transaction,
    SIGHASH_DEFAULT, plus BIP-342's extension when `leaf_script` is
    given."""
    spk = p2tr_script(output_key)
    msg = b"\x00"  # hash_type: SIGHASH_DEFAULT
    msg += struct.pack("<i", TX_VERSION) + struct.pack("<I", locktime)
    msg += _sha(spend_prevout(domain) + struct.pack("<I", 0))  # sha_prevouts
    msg += _sha(struct.pack("<q", SPENT_AMOUNT))  # sha_amounts
    msg += _sha(bytes([len(spk)]) + spk)  # sha_scriptpubkeys
    msg += _sha(struct.pack("<I", sequence))  # sha_sequences
    msg += _sha(struct.pack("<q", 0) + b"\x00")  # sha_outputs: one empty output
    msg += bytes([0 if leaf_script is None else 2])  # spend_type, no annex
    msg += struct.pack("<I", 0)  # input_index
    if leaf_script is not None:
        msg += tapleaf_hash(leaf_script) + b"\x00" + b"\xff\xff\xff\xff"
    return msg


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def key_path_sighash(output_key: bytes, domain: str) -> bytes:
    """What a `ck1` signature signs: key path, locktime 0, final
    sequence."""
    msg = sig_msg(
        output_key=output_key,
        domain=domain,
        locktime=0,
        sequence=0xFFFFFFFF,
        leaf_script=None,
    )
    return tagged_hash("TapSighash", b"\x00" + msg)


def script_path_sighash(
    output_key: bytes,
    domain: str,
    leaf_script: bytes,
    *,
    locktime: int = 0,
    sequence: int = 0xFFFFFFFE,
) -> bytes:
    """What a signature inside a `cw1`'s leaf signs, for the claimed
    time."""
    msg = sig_msg(
        output_key=output_key,
        domain=domain,
        locktime=locktime,
        sequence=sequence,
        leaf_script=leaf_script,
    )
    return tagged_hash("TapSighash", b"\x00" + msg)


def _compact_size(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    return b"\xfe" + struct.pack("<I", n)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def kernel_available() -> bool:
    """Whether the optional `lnurlcashkernel` native package is installed
    (arbitrary-tapscript verification)."""
    return _kernel is not None


def _verify_preimage_leaf(spend: Spend) -> bool:
    """The one script whose semantics this mint can check without Bitcoin
    Core's interpreter: the bearer-note leaf `OP_SHA256 <h> OP_EQUAL`
    under the NUMS internal key, spent by revealing the preimage alone.

    Equivalent to the script execution it stands in for: the witness must
    be exactly one item (the script pushes nothing else), and
    sha256(witness) must equal the leaf's embedded image. The control
    block has already committed the leaf to Q (see Spend.output_key and
    verify_spend's Q equality check), and check_leaf/check_time_claim run
    before this in verify_spend exactly as upstream's."""
    if spend.key_path:
        return False
    assert spend.script is not None
    if len(spend.script) != 35 or spend.script[:2] != b"\xa8\x20" or spend.script[-1:] != b"\x87":
        return False
    image = spend.script[2:34]
    if len(spend.witness) != 1:
        return False
    return hashlib.sha256(spend.witness[0]).digest() == image


def verify_witness(
    *,
    output_key: bytes,
    domain: str,
    stack: list[bytes],
    locktime: int,
    sequence: int,
    spend: Spend | None = None,
) -> bool:
    """Does this witness stack spend `output_key`?

    With `lnurlcashkernel` installed this is Bitcoin Core's own script
    interpreter, unmodified (upstream parity). Without it: a key-path
    spend ([sig]) is verified as a BIP-340 schnorr signature over the
    canonical sighash, and a script-path spend only when it is exactly
    the bearer preimage leaf (`_verify_preimage_leaf`); anything else is
    False."""
    if len(output_key) != 32:
        raise ValueError("output_key must be a 32-byte x-only key")
    if not (0 <= locktime <= _U32_MAX and 0 <= sequence <= _U32_MAX):
        return False
    if not stack:
        return False

    if _kernel is not None:
        spk = p2tr_script(output_key)
        tx = build_spend_tx(domain=domain, stack=stack, locktime=locktime, sequence=sequence)
        return _kernel._native.verify_input(
            script_pubkey=spk,
            amount=SPENT_AMOUNT,
            tx=tx,
            spent_outputs=[(spk, SPENT_AMOUNT)],
            input_index=0,
        )

    if spend is None or spend.key_path:
        # key path: [schnorr sig] over the canonical sighash
        if len(stack) != 1 or len(stack[0]) != 64:
            return False
        try:
            return PublicKeyXOnly(output_key).verify(
                stack[0], key_path_sighash(output_key, domain)
            )
        except Exception:
            return False
    return _verify_preimage_leaf(spend)


def verify_spend(
    *,
    output_key: bytes,
    domain: str,
    spend: Spend,
    now: int,
    locked_at: int,
) -> None:
    """Mint-facing: accept `spend` of the note `output_key`, or raise
    `SpendRejected`. `now`/`locked_at` are the mint's own clock, in Unix
    seconds."""
    if spend.output_key != output_key:
        raise ScriptInvalid("spend does not open this note")

    if spend.key_path:
        if (spend.locktime, spend.sequence) != (CANONICAL_LOCKTIME, CANONICAL_SEQUENCE):
            raise ScriptInvalid("a key-path spend carries no time claim")
    else:
        check_leaf(spend.script, spend.control_block)  # type: ignore[arg-type]
        check_time_claim(
            locktime=spend.locktime, sequence=spend.sequence, now=now, locked_at=locked_at
        )

    if _kernel is None and not spend.key_path and not _verify_preimage_leaf(spend):
        raise UnsupportedScript(
            "script-path spend needs lnurlcashkernel to verify "
            "(only the bearer preimage leaf is checked without it)"
        )

    if not verify_witness(
        output_key=output_key,
        domain=domain,
        stack=spend.stack,
        locktime=spend.locktime,
        sequence=spend.sequence,
        spend=spend,
    ):
        raise ScriptInvalid("spend verification failed")


@dataclass(frozen=True)
class ParsedK1:
    """A decoded `k1`: the note it names, and how to check it opens it."""

    note_id: str  # hex(Q)
    spend: Spend


def decode_note_hex(value: str) -> str | None:
    """hex(Q) of whatever was put where a `cp1` goes (a mint comment,
    p1/p2, ?p=): a `cp1`, or a bearer note's 64-hex `h` (short form). None
    if it is neither."""
    q = decode_note(value)
    return q.hex() if q is not None else None


def parse(k1: str) -> ParsedK1 | None:
    """The note `k1` claims to spend, or None if `k1` is no spend at all.
    Doesn't verify anything that needs the note's record; see verify."""
    spend = decode_spend(k1)
    q = spend.output_key if spend is not None else None
    if spend is None or q is None:
        return None
    return ParsedK1(q.hex(), spend)


def verify(parsed: ParsedK1, locked_at: int, domains: list[str]) -> str | None:
    """None if `parsed` opens its note, else why not. `domains` are every
    host this mint answers on: a signature bound to any of them is this
    mint's. `locked_at` is when this mint credited the note (the start of
    a relative timelock).

    The reason is safe to hand back for a script path - a `cw1` discloses
    its whole secret already, so explaining its failure can't help anyone
    guess another - but a key-path failure only ever says "invalid", same
    as an unknown note."""
    spend = parsed.spend
    q = bytes.fromhex(parsed.note_id)
    now = int(time.time())
    reason = None
    for domain in domains:
        try:
            verify_spend(output_key=q, domain=domain, spend=spend, now=now, locked_at=locked_at)
            return None
        except SpendRejected as exc:
            reason = exc.reason
    if spend.key_path:
        return "Invalid or already spent k1."
    return reason
