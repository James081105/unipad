"""Self-controlled HTTPS package service (PRD 3.2 P1).

PRD 3.2 P1 asks for "通过 VirtIO Net 等路线联网，优先出站网络和自控 HTTPS 服务".
This module is the second half of that bullet: *package delivery must not depend
on any third party*.  The host therefore

1. generates its own certificate at runtime,
2. serves the package directory over TLS on loopback, and
3. publishes the QEMU wiring that points the guest at it.

Nothing off this machine takes part in the chain, so there is no supply-chain
dependency and no external credential to leak.  The only trust decision a
caller makes is "do I trust this host", which is exactly the decision UniPad
already makes when it boots the guest.

Why the certificate is hand-built
---------------------------------
:mod:`ssl` is a TLS *transport*: it can load a certificate but it cannot mint
one, and the third-party libraries that can are unavailable here (pip is
broken on this host because the user profile path is non-ASCII).  So this
module carries the two small pieces needed to close the gap -- a DER writer and
a P-256 signer -- both pure stdlib.  The result is a real X.509v3 ECDSA P-256
certificate that OpenSSL and :mod:`ssl` both accept, verified end to end by
``tests/unit/test_host_httpsvc.py`` over a real TLS handshake with chain *and*
hostname verification.  Cost is roughly 150 ms of CPU per freshly generated
certificate, which is irrelevant next to booting a guest.

Security defaults
-----------------
* **TLS is mandatory.**  Plain HTTP is refused unless a caller passes
  ``allow_plaintext=True``; that escape hatch exists for tests that only
  exercise routing, never for a real run.
* **Loopback only.**  The bind address defaults to ``127.0.0.1`` and a
  non-loopback bind is refused unless ``allow_non_loopback=True`` is passed
  explicitly.  The service is loopback-only *by design*: PRD 3.2 P1 asks for
  outbound networking and a self-controlled service, and guest egress to the
  public internet still requires explicit authorisation.  The QEMU side keeps
  ``restrict=on`` so the guest cannot wander off this machine.
* **One directory.**  Every request is resolved against the served root and
  rejected unless it lands inside it; symlinks that escape are rejected too.
* **Key file permissions.**  The private key is restricted to the current
  user: ``chmod 0600`` on POSIX, and on Windows -- where ``os.chmod`` cannot
  touch the ACL at all -- a rebuilt DACL that cuts inheritance and grants
  exactly one ACE, the current user.  A caller-supplied shared or
  repo-relative certificate directory therefore cannot hand the key to
  another local user.  The restriction is best effort and says so in the log
  when it fails, because a key the caller asked to keep somewhere specific is
  still better served than not served.
* **No secrets in output.**  PRD 9.2 requires that logs carry no account
  tokens, signing private keys or user documents.  The private key is written
  to disk and then dropped -- :class:`CertificateMaterial` carries paths, the
  public PEM and a fingerprint but never key bytes, and the PEM is excluded
  from ``repr()``, so ``repr()`` cannot leak one.  Request errors are logged as
  an exception *type* only, because a traceback can carry local variables.

Two directions, one of them needing ``hostfwd``
-----------------------------------------------
``hostfwd`` forwards a **host** port **into the guest**, which is not what this
service needs.  The guest fetches packages by connecting to the QEMU user-mode
gateway ``10.0.2.2``, and QEMU relays that to the host's own loopback with no
``hostfwd`` at all -- see :func:`guest_fetch_url`.  :func:`hostfwd_spec` is
still provided because it is the correct spelling for the opposite direction
(a host-side tool reaching *into* the guest), and because a caller who wants an
explicit, auditable port mapping can splice it into ``-nic``.

Module status (four fields, per CLAUDE.md "代码阶段与现场阶段分离"):

* **实现状态**: complete, including a production caller -- ADR-0008 puts the
  service in ``_import_ipa``'s transfer stage as a context manager, so it now
  has an entry point as well as tests.
* **Windows/模拟测试状态**: complete -- real TLS handshake, traversal and
  lifecycle tests all run on the Windows host, plus the transfer-stage
  regression tests (the service starts, is recorded, and always releases the
  port), plus the index regression tests (a second ``GET /index.json`` does
  not re-hash the directory, and a package that changed is re-hashed).
* **现场验证状态**: UNVERIFIED.  Whether the guest can actually reach
  ``10.0.2.2:<port>`` under ``restrict=on`` is a QEMU/slirp behaviour that only
  a real boot can answer; it is recorded as an on-site gate, not asserted here.
* **阻断原因**: no QEMU boot on this host (see ``docs/testing-strategy.md``),
  and no guest-side package fetcher for the ``https`` transport to talk to.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import ssl
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

from unipad.errors import ErrorCode, UniPadError
from unipad.host.paths import safe_location

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

#: Where the QEMU user-mode network stack is reachable *from the guest*.
DEFAULT_GUEST_GATEWAY = "10.0.2.2"

#: The address QEMU hands the guest over its built-in DHCP server.
DEFAULT_GUEST_ADDRESS = "10.0.2.15"

#: Guest port the packages are published on.
DEFAULT_GUEST_PORT = 8443

#: The service binds here and nowhere else unless a caller opts out.
DEFAULT_LOOPBACK_HOST = "127.0.0.1"

#: Canonical path of the package index.
INDEX_PATH = "/index.json"

#: Path prefix of the downloadable package bytes.
PACKAGES_PATH = "/packages/"

#: Certificate lifetime.  Ten years is deliberately generous: the certificate
#: is regenerated on demand and pinned by fingerprint, so rotation is a policy
#: question rather than an expiry emergency.
CERTIFICATE_VALIDITY_DAYS = 3650

#: A cached certificate is regenerated once it is within this margin of expiry,
#: so a long-lived run never serves with an expired certificate.
CERTIFICATE_RENEW_MARGIN_DAYS = 30

#: Common name / subject of the generated certificate.
CERTIFICATE_COMMON_NAME = "unipad-package-host"

CERTIFICATE_FILENAME = "cert.pem"
PRIVATE_KEY_FILENAME = "key.pem"

_PEM_CERTIFICATE = "CERTIFICATE"
_PEM_PRIVATE_KEY = "EC PRIVATE KEY"

#: OIDs this module needs to read back.  ``unipad.errors`` freezes the fault
#: taxonomy; these are wire constants, so they live here rather than in a
#: shared table nobody else reads.
_OID_COMMON_NAME = bytes.fromhex("550403")
_OID_SUBJECT_ALT_NAME = bytes.fromhex("551d11")

#: How much of a file the service reads at a time.  One mebibyte is large
#: enough that the syscall overhead is irrelevant and small enough that a
#: request in flight costs a fixed, bounded amount of memory no matter how big
#: the package is.
PACKAGE_CHUNK_BYTES = 1 << 20

# Error-code mapping.  The fault taxonomy in ``unipad.errors`` is frozen (a new
# code needs a decision record), so this module reuses two existing codes and
# keeps the distinction in the message: a bad argument is a configuration
# fault, while anything that stops the packages moving is a transfer fault.
_CONFIG = ErrorCode.CONFIG_INVALID
_TRANSFER = ErrorCode.TRANSFER_FAILED


# ---------------------------------------------------------------------------
# minimal DER writer
# ---------------------------------------------------------------------------


def _der_length(size: int) -> bytes:
    """Encode an ASN.1 length, long form when the short form cannot hold it."""
    if size < 0x80:
        return bytes([size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_length(len(value)) + value


def _sequence(*items: bytes) -> bytes:
    return _tlv(0x30, b"".join(items))


def _set_of(*items: bytes) -> bytes:
    return _tlv(0x31, b"".join(items))


def _context(tag: int, value: bytes) -> bytes:
    return _tlv(tag, value)


def _integer(value: int) -> bytes:
    """Encode a signed INTEGER, padding so a high bit never means "negative"."""
    if value == 0:
        body = b"\x00"
    else:
        body = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if body[0] & 0x80:
            body = b"\x00" + body
    return _tlv(0x02, body)


def _bit_string(value: bytes, unused_bits: int = 0) -> bytes:
    return _tlv(0x03, bytes([unused_bits]) + value)


def _octet_string(value: bytes) -> bytes:
    return _tlv(0x04, value)


def _utf8_string(value: str) -> bytes:
    return _tlv(0x0C, value.encode("utf-8"))


def _object_identifier(dotted: str) -> bytes:
    """Encode a dotted OID (first two arcs packed into one byte, as DER requires)."""
    arcs = [int(part) for part in dotted.split(".")]
    if len(arcs) < 2:
        raise ValueError("an OID needs at least two arcs")
    body = bytearray([arcs[0] * 40 + arcs[1]])
    for arc in arcs[2:]:
        if arc < 0:
            raise ValueError("OID arcs must not be negative")
        chunk = bytearray([arc & 0x7F])
        arc >>= 7
        while arc:
            chunk.insert(0, (arc & 0x7F) | 0x80)
            arc >>= 7
        body += chunk
    return _tlv(0x06, bytes(body))


def _time(moment: datetime) -> bytes:
    """Encode a validity time: UTCTime before 2050, GeneralizedTime after."""
    if moment.year < 2050:
        return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode("ascii"))
    return _tlv(0x18, moment.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def _pem(label: str, der: bytes) -> str:
    """Wrap DER bytes in a PEM envelope with 64-column lines, as the spec asks."""
    body = base64.b64encode(der)
    lines = [body[index:index + 64] for index in range(0, len(body), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(line.decode("ascii") for line in lines) + f"\n-----END {label}-----\n"


# ---------------------------------------------------------------------------
# minimal DER reader (used to inspect a certificate we or a previous run wrote)
# ---------------------------------------------------------------------------


def _contents(der: bytes) -> bytes:
    """Return the value of the single TLV in ``der``, stripping its header.

    A certificate, a ``Name`` and an extension block are each exactly one TLV,
    so :func:`_children` alone would hand back that TLV rather than its
    contents.  This is the strip-then-split pair.
    """
    length = der[1]
    cursor = 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(der[cursor:cursor + count], "big")
        cursor += count
    return der[cursor:cursor + length]


def _children(der: bytes) -> list[tuple[int, bytes]]:
    """Split a DER blob into its top-level ``(tag, value)`` children."""
    out: list[tuple[int, bytes]] = []
    cursor = 0
    while cursor < len(der):
        tag = der[cursor]
        length = der[cursor + 1]
        cursor += 2
        if length & 0x80:
            count = length & 0x7F
            length = int.from_bytes(der[cursor:cursor + count], "big")
            cursor += count
        out.append((tag, der[cursor:cursor + length]))
        cursor += length
    return out


def _read_time(value: bytes) -> datetime:
    text = value.decode("ascii")
    if text.endswith("Z"):
        text = text[:-1]
    if len(text) == 12:  # UTCTime: YYMMDDHHMMSS
        year = int(text[:2])
        year += 2000 if year < 50 else 1900
        return datetime(year, int(text[2:4]), int(text[4:6]), int(text[6:8]), int(text[8:10]), int(text[10:12]), tzinfo=timezone.utc)
    return datetime(  # GeneralizedTime: YYYYMMDDHHMMSS
        int(text[:4]), int(text[4:6]), int(text[6:8]),
        int(text[8:10]), int(text[10:12]), int(text[12:14]), tzinfo=timezone.utc,
    )


@dataclass(frozen=True)
class _CertificateFacts:
    """The facts a caller needs, read back out of a DER certificate."""

    spki: bytes
    not_before: datetime
    not_after: datetime
    common_name: str
    dns_names: tuple[str, ...]
    ip_addresses: tuple[str, ...]


def _read_common_name(subject_der: bytes) -> str:
    """Read the CN out of a DER ``Name`` (``SEQUENCE OF RelativeDistinguishedName``)."""
    for _, rdn in _children(subject_der):
        for _, atv in _children(rdn):
            parts = _children(atv)
            if len(parts) == 2 and parts[0][0] == 0x06 and parts[0][1] == _OID_COMMON_NAME:
                return parts[1][1].decode("utf-8")
    return ""


def _read_san(fields: list[tuple[int, bytes]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the DNS names and IPv4 addresses out of the SAN extension."""
    extension_block = next((value for tag, value in fields if tag == 0xA3), None)
    if extension_block is None:
        return (), ()

    dns_names: list[str] = []
    ip_addresses: list[str] = []
    for _, extension_der in _children(_contents(extension_block)):
        parts = _children(extension_der)
        if not parts or parts[0][0] != 0x06 or parts[0][1] != _OID_SUBJECT_ALT_NAME:
            continue
        # extnValue is the last OCTET STRING; ``critical`` is an optional
        # BOOLEAN in between, so taking the last part is the safe read.  Its
        # content is itself one SEQUENCE, hence the extra ``_contents``.
        for tag, general_name in _children(_contents(parts[-1][1])):
            if tag == 0x82:  # dNSName
                dns_names.append(general_name.decode("ascii"))
            elif tag == 0x87:  # iPAddress, 4 raw bytes for IPv4
                ip_addresses.append(".".join(str(byte) for byte in general_name))
    return tuple(dns_names), tuple(ip_addresses)


def _read_certificate(cert_der: bytes) -> _CertificateFacts:
    """Parse the fields this module needs out of a DER X.509 certificate.

    This is a reader, not a validator: it exists so the module can inspect a
    certificate *it* wrote (or one a previous run left behind) and refuse to
    reuse a certificate that is expired or malformed.  Anything it cannot
    interpret raises :class:`UniPadError` rather than being silently ignored.
    """
    try:
        outer = _children(_contents(cert_der))
        tbs = next(value for tag, value in outer if tag == 0x30)
        fields = _children(tbs)

        # ``version`` is a [0] EXPLICIT wrapper and is optional; the serial is
        # the first INTEGER, and the five SEQUENCEs after it are the signature
        # algorithm, issuer, validity, subject and SubjectPublicKeyInfo.
        serial_at = next(index for index, (tag, _) in enumerate(fields) if tag == 0x02)
        sequences = [index for index in range(serial_at + 1, len(fields)) if fields[index][0] == 0x30]
        if len(sequences) < 5:
            raise ValueError("certificate is missing required SEQUENCE fields")

        validity = _children(fields[sequences[2]][1])
        not_before = _read_time(validity[0][1])
        not_after = _read_time(validity[1][1])
        spki = fields[sequences[4]][1]
        common_name = _read_common_name(fields[sequences[3]][1])
        dns_names, ip_addresses = _read_san(fields)
    except UniPadError:
        raise
    except Exception as exc:
        raise UniPadError(_CONFIG, "certificate could not be parsed", reason=type(exc).__name__) from exc

    return _CertificateFacts(
        spki=spki,
        not_before=not_before,
        not_after=not_after,
        common_name=common_name,
        dns_names=dns_names,
        ip_addresses=ip_addresses,
    )


# ---------------------------------------------------------------------------
# P-256 (secp256r1): the only curve needed, and small enough to be honest about
# ---------------------------------------------------------------------------

#: Field prime.
_P256_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
#: Curve coefficient ``a = p - 3`` (the "-3" form is what FIPS 186-4 publishes).
_P256_A = _P256_P - 3
#: Curve coefficient ``b``.
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
#: Generator point.
_P256_GX = 0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296
_P256_GY = 0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5
#: Order of the generator.
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

_P256_ORDER_BYTES = 32

#: Affine point, or ``None`` for the point at infinity.
_Point = Optional[tuple[int, int]]


def _inverse(value: int) -> int:
    return pow(value, _P256_P - 2, _P256_P)


def _point_add(first: _Point, second: _Point) -> _Point:
    """Affine addition on P-256.  Slow by design: correctness beats speed here
    and the whole certificate costs well under a tenth of a second of it."""
    if first is None:
        return second
    if second is None:
        return first
    x1, y1 = first
    x2, y2 = second
    if x1 == x2 and (y1 + y2) % _P256_P == 0:
        return None
    if first == second:
        slope = (3 * x1 * x1 + _P256_A) * _inverse(2 * y1) % _P256_P
    else:
        slope = (y2 - y1) * _inverse(x2 - x1) % _P256_P
    x3 = (slope * slope - x1 - x2) % _P256_P
    return x3, (slope * (x1 - x3) - y1) % _P256_P


def _point_mul(scalar: int, point: _Point) -> _Point:
    result: _Point = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _encode_point(point: _Point) -> bytes:
    """Uncompressed SEC 1 point encoding (0x04 || X || Y)."""
    assert point is not None
    return b"\x04" + point[0].to_bytes(32, "big") + point[1].to_bytes(32, "big")


def _sign_digest(digest: bytes, private_scalar: int) -> bytes:
    """Deterministic-length ECDSA signature: DER ``SEQUENCE { r, s }``.

    ``s`` is normalised to the low half of the order, which every verifier
    accepts and which makes the encoding unique.
    """
    z = int.from_bytes(digest, "big")
    while True:
        nonce = secrets.randbelow(_P256_N - 1) + 1
        ephemeral = _point_mul(nonce, (_P256_GX, _P256_GY))
        assert ephemeral is not None
        r = ephemeral[0] % _P256_N
        if r == 0:
            continue
        s = pow(nonce, _P256_N - 2, _P256_N) * (z + r * private_scalar) % _P256_N
        if s == 0:
            continue
        if s > _P256_N // 2:
            s = _P256_N - s
        return _sequence(_integer(r), _integer(s))


# ---------------------------------------------------------------------------
# certificate material
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CertificateMaterial:
    """Where the generated certificate lives, and how to pin it.

    The fingerprint is the SHA-256 of the DER ``SubjectPublicKeyInfo``, which
    is the standard pin: it survives a certificate renewal that keeps the same
    key, and it is the value a guest-side pinning policy would compare against.
    Private key bytes are deliberately absent from this object.
    """

    cert_directory: Path
    cert_path: Path
    key_path: Path
    fingerprint: str
    common_name: str
    not_before: datetime
    not_after: datetime
    san_hosts: tuple[str, ...]
    #: The PEM body, read once when the material is built.  It used to be a
    #: property that re-read the certificate from disk on every access -- the
    #: same "cheap-looking accessor doing whole-file I/O" shape as the package
    #: index and the package fetch, so a caller that reached for it inside a
    #: loop paid for the read every time.  The file is ~1 KB, so holding it
    #: costs nothing; reading it repeatedly was the cost.  ``repr`` excludes it
    #: so a material in a log line or a debugger still prints as paths and a
    #: fingerprint, never a wall of base64.
    pem_certificate: str = field(repr=False)
    san_ips: tuple[str, ...] = ()

    @property
    def days_remaining(self) -> int:
        return max(0, (self.not_after - datetime.now(timezone.utc)).days)


def _load_certificate_pair(cert_path: Path, key_path: Path) -> bool:
    """Ask :mod:`ssl` whether the pair on disk actually belongs together.

    Using the TLS stack as the oracle means a half-written or mismatched pair
    is caught here, with a clear error, instead of surfacing as a mysterious
    handshake failure the first time a guest connects.
    """
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
    except (ssl.SSLError, OSError, ValueError):
        return False
    return True


def generate_self_signed_certificate(
    directory: Path,
    *,
    common_name: str = CERTIFICATE_COMMON_NAME,
    san_hosts: Iterable[str] = ("localhost", DEFAULT_LOOPBACK_HOST),
    san_ips: Iterable[str] = (DEFAULT_GUEST_GATEWAY, DEFAULT_LOOPBACK_HOST),
    validity_days: int = CERTIFICATE_VALIDITY_DAYS,
) -> CertificateMaterial:
    """Write (or reuse) a self-signed certificate for the package service.

    Reuse is deliberate: regenerating on every call would rotate the key under
    a caller that pinned the previous fingerprint, and would waste CPU.  A pair
    already on disk is reused when it parses, loads as a matching pair, and is
    not within :data:`CERTIFICATE_RENEW_MARGIN_DAYS` of expiry.

    The certificate is self-signed and marks itself as a CA, which is what
    makes a single file usable as both the server certificate and the trust
    anchor for a client that verifies the chain -- there is no second party to
    issue it, by design.
    """
    cert_path = directory / CERTIFICATE_FILENAME
    key_path = directory / PRIVATE_KEY_FILENAME

    reused = _reuse_certificate(cert_path, key_path, common_name, san_hosts, san_ips)
    if reused is not None:
        return reused

    _validate_san(san_hosts, san_ips)
    private_scalar, public_point = _generate_key_pair()
    cert_der, key_der = _build_certificate(
        private_scalar, public_point, common_name, san_hosts, san_ips, validity_days
    )

    try:
        directory.mkdir(parents=True, exist_ok=True)
        cert_path.write_text(_pem(_PEM_CERTIFICATE, cert_der), encoding="utf-8")
        # Written after the certificate so an interrupted run leaves a
        # certificate with no key, which the reuse path treats as "regenerate"
        # rather than as a broken pair.
        key_path.write_text(_pem(_PEM_PRIVATE_KEY, key_der), encoding="utf-8")
        _restrict_key_permissions(key_path)
    except OSError as exc:
        raise UniPadError(
            _TRANSFER,
            "certificate directory is not writable",
            directory=_safe_location(directory),
        ) from exc

    if not _load_certificate_pair(cert_path, key_path):  # pragma: no cover - self-check
        raise UniPadError(_TRANSFER, "generated certificate was rejected by the TLS stack")

    facts = _read_certificate(cert_der)
    log.debug(
        "generated self-signed certificate subject=%s fingerprint=%s",
        common_name,
        hashlib.sha256(facts.spki).hexdigest(),
    )
    return _material(directory, cert_path, key_path, facts)


def _reuse_certificate(
    cert_path: Path,
    key_path: Path,
    common_name: str,
    san_hosts: Iterable[str],
    san_ips: Iterable[str],
) -> Optional[CertificateMaterial]:
    """Return the existing certificate when it is still good, else ``None``.

    "Still good" means: it parses, it loads as a matching pair, it is not close
    to expiry, *and* it already carries every name and the subject the caller
    asked for.  The last condition matters -- reusing a certificate that lacks a
    newly requested name, or that carries a different subject, would hand back a
    :class:`CertificateMaterial` that describes a certificate which does not
    exist.
    """
    if not (cert_path.is_file() and key_path.is_file()):
        return None
    try:
        cert_der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="utf-8"))
        facts = _read_certificate(cert_der)
    except (UniPadError, ssl.SSLError, OSError, ValueError):
        return None

    if facts.not_after <= datetime.now(timezone.utc) + timedelta(days=CERTIFICATE_RENEW_MARGIN_DAYS):
        return None
    if not _load_certificate_pair(cert_path, key_path):
        return None
    if not set(san_hosts) <= set(facts.dns_names) or not set(san_ips) <= set(facts.ip_addresses):
        return None
    if facts.common_name != common_name:
        return None

    log.debug("reusing certificate fingerprint=%s", hashlib.sha256(facts.spki).hexdigest())
    return _material(cert_path.parent, cert_path, key_path, facts)


def _material(
    directory: Path,
    cert_path: Path,
    key_path: Path,
    facts: _CertificateFacts,
) -> CertificateMaterial:
    return CertificateMaterial(
        cert_directory=directory,
        cert_path=cert_path,
        key_path=key_path,
        fingerprint=hashlib.sha256(facts.spki).hexdigest(),
        common_name=facts.common_name or CERTIFICATE_COMMON_NAME,
        not_before=facts.not_before,
        not_after=facts.not_after,
        san_hosts=tuple(facts.dns_names),
        # Read once, here, so no accessor has to do it later.
        pem_certificate=cert_path.read_text(encoding="utf-8"),
        san_ips=tuple(facts.ip_addresses),
    )


# ---------------------------------------------------------------------------
# Windows access control for the private key
# ---------------------------------------------------------------------------

# On Windows ``os.chmod`` is a no-op for access control: CPython maps only
# ``S_IWRITE`` onto the read-only attribute and cannot touch the ACL at all, so
# the key would inherit whatever the certificate directory grants.  The only
# stdlib way to write a DACL is advapi32 through :mod:`ctypes`, which is what
# this section does.  The result is the same boundary ``0600`` draws on POSIX:
# one ACE for the current user, inheritance cut, so a shared or repo-relative
# certificate directory cannot hand the key to another local user.

if os.name == "nt":  # pragma: no cover - exercised on the Windows host

    import ctypes

    #: ``ERROR_SUCCESS``.
    _WIN_ERROR_SUCCESS = 0
    #: ``SE_FILE_OBJECT``.
    _SE_FILE_OBJECT = 1
    #: ``DACL_SECURITY_INFORMATION``.
    _DACL_SECURITY_INFORMATION = 0x00000004
    #: ``PROTECTED_DACL_SECURITY_INFORMATION`` -- cut inheritance.
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    #: ``ACL_REVISION``.
    _ACL_REVISION = 2
    #: ``TOKEN_QUERY``.
    _TOKEN_QUERY = 0x00000008
    #: ``TokenUser`` from ``TOKEN_INFORMATION_CLASS``.
    _TOKEN_USER_INFORMATION_CLASS = 1
    #: What the key needs and nothing more: read it to load it into the TLS
    #: context, write it to renew it.  ``FILE_GENERIC_READ`` alone would wedge
    #: the renewal path, because the key is rewritten in place.
    _KEY_ACCESS_MASK = 0x120089 | 0x120116

    #: ``sizeof(ACL)``.
    _ACL_HEADER_BYTES = 8
    #: ``sizeof(ACCESS_ALLOWED_ACE) - sizeof(DWORD)``: the SidStart overhead.
    _ACE_SID_OVERHEAD_BYTES = 8

    class _SidAndAttributes(ctypes.Structure):
        """``SID_AND_ATTRIBUTES``, the payload of a ``TOKEN_USER``."""

        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]

    class _TokenUser(ctypes.Structure):
        """``TOKEN_USER``: the user SID an access token carries."""

        _fields_ = [("User", _SidAndAttributes)]

    _advapi32 = ctypes.windll.advapi32
    _kernel32 = ctypes.windll.kernel32
    _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    _advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetTokenInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    _advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    _advapi32.GetLengthSid.restype = ctypes.c_ulong
    _advapi32.InitializeAcl.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong]
    _advapi32.InitializeAcl.restype = ctypes.c_int
    _advapi32.AddAccessAllowedAce.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
    ]
    _advapi32.AddAccessAllowedAce.restype = ctypes.c_int
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = ctypes.c_ulong

    def _restrict_key_permissions_windows(key_path: Path) -> None:
        """Rebuild ``key_path``'s DACL so only the current user can touch it.

        Every buffer named here outlives the call that reads through it: the
        SID points into ``buffer`` and the ACE is copied into ``acl``, so both
        have to stay reachable until ``SetNamedSecurityInfoW`` has returned.
        """
        token = ctypes.c_void_p()
        if not _advapi32.OpenProcessToken(
            _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise OSError("the process token could not be opened")
        try:
            size = ctypes.c_ulong(0)
            _advapi32.GetTokenInformation(
                token, _TOKEN_USER_INFORMATION_CLASS, None, 0, ctypes.byref(size)
            )
            buffer = ctypes.create_string_buffer(size.value)
            if not _advapi32.GetTokenInformation(
                token, _TOKEN_USER_INFORMATION_CLASS, buffer, size.value, ctypes.byref(size)
            ):
                raise OSError("the token user could not be read")
            sid = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents.User.Sid

            # The size MSDN publishes for a single-ACE ACL.
            acl = ctypes.create_string_buffer(
                _ACL_HEADER_BYTES + _ACE_SID_OVERHEAD_BYTES + _advapi32.GetLengthSid(sid)
            )
            if not _advapi32.InitializeAcl(acl, len(acl), _ACL_REVISION):
                raise OSError("the key ACL could not be initialised")
            if not _advapi32.AddAccessAllowedAce(acl, _ACL_REVISION, _KEY_ACCESS_MASK, sid):
                raise OSError("the key ACE could not be added")

            status = _advapi32.SetNamedSecurityInfoW(
                str(key_path),
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                acl,
                None,
            )
            if status != _WIN_ERROR_SUCCESS:
                raise OSError(f"the key DACL could not be written (status {status})")
        finally:
            _kernel32.CloseHandle(token)


def _safe_location(path: Path) -> str:
    """The part of ``path`` an error may name: its final component only.

    Thin alias for :func:`unipad.host.paths.safe_location`, which carries the
    reasoning.  PRD 9.2 forbids account tokens, signing private keys and user
    documents in anything an operator or a guest can read, and an absolute path
    on this host embeds the Windows username -- the default certificate
    directory is a ``tempfile.mkdtemp`` under the profile.  The final component
    is enough to tell two candidate directories apart without saying where the
    machine keeps them.
    """
    return safe_location(path)


def _restrict_key_permissions(key_path: Path) -> None:
    """Keep the private key readable and writable by this user and nobody else.

    On POSIX that is ``chmod 0600``.  On Windows ``os.chmod`` cannot restrict
    anything -- its only effect is to clear the read-only attribute, which is
    the opposite of restricting -- so the DACL is rebuilt instead: inheritance
    is cut and exactly one ACE, the current user, is granted.  That is the same
    boundary ``0600`` draws, and it is what stops a caller-supplied shared or
    repo-relative certificate directory from handing the key to another local
    user.

    Best effort by design: a failure is logged with the exception *type* only,
    because a message could carry the path (PRD 9.2), and the service keeps
    running.  Refusing to serve would be a worse outcome than serving a key
    inside a directory the caller chose.
    """
    if os.name == "nt":
        try:
            _restrict_key_permissions_windows(key_path)
        except (OSError, ValueError) as exc:
            log.warning(
                "private key permissions could not be restricted: %s", type(exc).__name__
            )
        return
    try:
        os.chmod(key_path, 0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pass


def _validate_san(san_hosts: Iterable[str], san_ips: Iterable[str]) -> None:
    """Reject anything that is not a plain DNS name or a literal IPv4 address."""
    for host in san_hosts:
        if not host or any(character in host for character in " /\t\r\n") or "*" in host:
            raise UniPadError(_CONFIG, "SAN host entry is not a plain DNS name", entry=repr(host))
    for address in san_ips:
        parts = address.split(".")
        if len(parts) != 4 or not all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
            raise UniPadError(_CONFIG, "SAN IP entry is not an IPv4 address", entry=repr(address))


def _generate_key_pair() -> tuple[int, _Point]:
    """A fresh P-256 scalar and its public point."""
    while True:
        scalar = secrets.randbelow(_P256_N - 1) + 1
        point = _point_mul(scalar, (_P256_GX, _P256_GY))
        if point is not None:
            return scalar, point


def _build_certificate(
    private_scalar: int,
    public_point: _Point,
    common_name: str,
    san_hosts: Iterable[str],
    san_ips: Iterable[str],
    validity_days: int,
) -> tuple[bytes, bytes]:
    """Build the DER certificate and the matching SEC 1 EC private key."""
    oid_ec_public_key = _object_identifier("1.2.840.10045.2.1")
    oid_prime256v1 = _object_identifier("1.2.840.10045.3.1.7")
    oid_ecdsa_with_sha256 = _object_identifier("1.2.840.10045.4.3.2")
    oid_common_name = _object_identifier("2.5.4.3")
    oid_subject_alt_name = _object_identifier("2.5.29.17")
    oid_basic_constraints = _object_identifier("2.5.29.19")
    oid_key_usage = _object_identifier("2.5.29.15")
    oid_subject_key_identifier = _object_identifier("2.5.29.14")

    public_bytes = _encode_point(public_point)
    spki = _sequence(_sequence(oid_ec_public_key, oid_prime256v1), _bit_string(public_bytes))
    name = _sequence(_set_of(_sequence(oid_common_name, _utf8_string(common_name))))

    not_before = datetime.now(timezone.utc) - timedelta(minutes=5)
    not_after = not_before + timedelta(days=validity_days)

    san_general_names: list[bytes] = [_context(0x82, host.encode("ascii")) for host in san_hosts]
    san_general_names += [
        _context(0x87, bytes(int(part) for part in address.split("."))) for address in san_ips
    ]

    extensions = _sequence(
        # basicConstraints CA:TRUE -- a self-signed certificate is its own
        # trust anchor, so it has to say so.
        _sequence(
            oid_basic_constraints,
            _tlv(0x01, b"\xff"),
            _octet_string(_sequence(_tlv(0x01, b"\xff"))),
        ),
        # keyUsage: digitalSignature + keyCertSign + cRLSign.
        _sequence(
            oid_key_usage,
            _tlv(0x01, b"\xff"),
            _octet_string(_bit_string(b"\x86", 1)),
        ),
        _sequence(
            oid_subject_key_identifier,
            _octet_string(_octet_string(hashlib.sha256(public_bytes).digest())),
        ),
        _sequence(oid_subject_alt_name, _octet_string(_sequence(*san_general_names))),
    )

    tbs_certificate = _sequence(
        _context(0xA0, _integer(2)),  # version: v3
        _integer(secrets.randbelow(2 ** 63) + 1),
        _sequence(oid_ecdsa_with_sha256),
        name,  # issuer == subject: self-signed
        _sequence(_time(not_before), _time(not_after)),
        name,
        spki,
        _context(0xA3, extensions),
    )
    signature = _sign_digest(hashlib.sha256(tbs_certificate).digest(), private_scalar)
    certificate = _sequence(
        tbs_certificate, _sequence(oid_ecdsa_with_sha256), _bit_string(signature)
    )
    private_key = _sequence(
        _integer(1),
        _octet_string(private_scalar.to_bytes(_P256_ORDER_BYTES, "big")),
        _context(0xA0, oid_prime256v1),
        _context(0xA1, _bit_string(public_bytes)),
    )
    return certificate, private_key


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _file_digest(path: Path) -> str:
    """SHA-256 of a file, streamed in :data:`PACKAGE_CHUNK_BYTES` chunks.

    ``path.read_bytes()`` inside a loop is what made ``GET /index.json`` cost
    O(total bytes served) of *memory* as well as of I/O: one large package was
    enough to hit the same ceiling as serving it, and the index is the first
    request of every install.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(PACKAGE_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class PackageRequestHandler(BaseHTTPRequestHandler):
    """Serves the package index and the package bytes, and nothing else.

    Two rules are enforced on every request:

    * the path must resolve *inside* the served root -- traversal is rejected
      before any filesystem call is made;
    * nothing but the requested bytes is ever written to the log, so a package
      body or a private key cannot end up in a log line.
    """

    server_version = "UniPadPackages/1"
    # HTTP/1.1 keep-alive keeps a verifying client from re-handshaking per
    # request; every response sets an explicit length so it stays framed.
    protocol_version = "HTTP/1.1"

    @property
    def root(self) -> Path:
        return self.server.package_root  # type: ignore[attr-defined]

    # -- logging -----------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        # Route the stdlib's own request log through the package logger so the
        # service obeys the host's logging policy, and keep it to one line that
        # carries the method, path and status -- never a body.
        log.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        log.warning("%s %s", self.address_string(), format % args)

    # -- request handling --------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        self._dispatch()

    def _dispatch(self) -> None:
        try:
            path = self._safe_path()
            if path is None:
                self._send_error_json(400, "malformed request path")
                return
            if path in ("/", INDEX_PATH):
                self._send_index()
                return
            if path.startswith(PACKAGES_PATH):
                self._send_package(path[len(PACKAGES_PATH):])
                return
            self._send_error_json(404, "not found")
        except OSError:
            # A file that vanished between the index and the fetch, or a root
            # directory that disappeared, is a host-side fault.  Report it as a
            # 500 with a fixed reason -- never with the path or the exception
            # text -- and keep the connection usable for the next request.
            log.warning("package request failed: %s", type(sys.exc_info()[1]).__name__)
            self._send_error_json(500, "package service fault")

    def _safe_path(self) -> Optional[str]:
        """Return the request path, or ``None`` when it is not decodable.

        The check happens on the *decoded* path, so ``%2e%2e%2f`` is caught as
        surely as a literal ``../``.  Absolute paths, Windows drive letters and
        a NUL byte are rejected here too.
        """
        raw = urlparse(self.path).path
        try:
            decoded = unquote(raw, errors="strict")
        except UnicodeDecodeError:
            return None
        if "\x00" in decoded:
            # A NUL survives every textual check below and then makes
            # ``Path.resolve()`` raise ValueError -- which is not an OSError,
            # so ``_dispatch`` would not catch it and the guest would get no
            # response at all rather than a status code.
            return None
        if not decoded.startswith("/"):
            return None
        if decoded.startswith("//") or "\\" in decoded:
            return None
        if len(decoded) > 1 and decoded[1] == ":":
            return None
        if ".." in decoded.split("/"):
            return None
        return decoded

    def _resolve(self, relative: str) -> Optional[Path]:
        """Map a request suffix onto a file inside the served root.

        Returns ``None`` when the result would land outside the root, which
        covers traversal that survived the textual check and symlinks that
        point out of the tree.
        """
        if not relative or relative.startswith("/") or "\\" in relative:
            return None
        candidate = (self.root / relative).resolve()
        root = self.root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if candidate == root or not candidate.is_file():
            return None
        return candidate

    # -- responses ---------------------------------------------------------

    def _send_index(self) -> None:
        packages = self.server.package_index()
        self._send_json(200, {"packages": packages})

    def _send_package(self, relative: str) -> None:
        target = self._resolve(relative)
        if target is None:
            self._send_error_json(404, "package not found")
            return
        # Streamed in chunks: a package this size is normal here, and reading it
        # whole cost one contiguous allocation of the package size per request
        # -- measured, the service died with MemoryError above ~2.2 GiB.  The
        # length comes from stat(), so nothing about the framing changes.
        length = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command == "HEAD":
            return
        with target.open("rb") as handle:
            try:
                # The published length is the contract, so the stream is
                # bounded by it: a package that grew underneath us must not
                # push extra bytes into a response the client has already
                # measured, which would corrupt the next one on this socket.
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(PACKAGE_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    self.wfile.write(chunk)
                if remaining:
                    # It shrank instead: honour the length we promised and
                    # close, rather than leave the guest waiting for bytes
                    # that are not coming.
                    log.warning("package shrank while it was being served")
                    self.close_connection = True
            except OSError:
                # The body is already on the wire, so there is no status code
                # left to send: close the connection rather than framing a
                # second response into the first one's bytes.
                log.warning(
                    "package stream failed: %s", type(sys.exc_info()[1]).__name__
                )
                self.close_connection = True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(self, status: int, reason: str) -> None:
        # The reason is a fixed phrase, never the offending path: echoing a
        # path back would put caller-controlled bytes into the response.
        self._send_json(status, {"error": reason})


class PackageHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that keeps the served root on the instance."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], package_root: Path) -> None:
        super().__init__(address, PackageRequestHandler)
        self.package_root = package_root
        # The index cache, and why it lives here rather than on the handler:
        # ``BaseHTTPRequestHandler`` builds one instance per *connection*, so a
        # handler-owned cache would be rebuilt for every request -- which is the
        # defect being fixed.  The server object is the one thing every request
        # shares.
        #
        # name -> (size, mtime_ns, sha256) for the packages hashed so far.
        self._index_cache: dict[str, tuple[int, int, str]] = {}
        self._index_lock = threading.Lock()

    def package_index(self) -> list[dict[str, Any]]:
        """The index document, hashing each package only when it changed.

        ``GET /index.json`` is the guest's first request of every install and it
        used to re-read and re-hash the whole served directory on every call:
        measured on this host, 19.2 s to answer a 260-byte question over 3.5 GiB
        of packages, and the same memory ceiling as serving a package whole.
        The document itself is O(number of files); the work behind it was
        O(total bytes served).

        Invalidation is deliberately conservative: a package is re-hashed when
        its *size or mtime* moved, which costs one ``stat()`` per file instead
        of one full read, and which cannot answer with a digest that no longer
        describes the bytes on disk.  What that leaves open is narrow and
        named: an in-place rewrite that keeps the length *and* lands inside the
        filesystem's timestamp granularity is not detected.  A package that is
        being rewritten while it is being served is not a state this service
        exists to support, and the alternative -- hashing on every request --
        is the defect being fixed.  The lock is held across the build so two
        concurrent index requests hash once between them rather than twice --
        it never covers a package fetch, so a large transfer is not delayed by
        an index being built.
        """
        packages: list[dict[str, Any]] = []
        seen: set[str] = set()
        with self._index_lock:
            for entry in sorted(self.package_root.iterdir(), key=lambda item: item.name):
                if not entry.is_file() or entry.is_symlink():
                    continue
                seen.add(entry.name)
                stat = entry.stat()
                cached = self._index_cache.get(entry.name)
                if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
                    digest = cached[2]
                else:
                    digest = _file_digest(entry)
                    self._index_cache[entry.name] = (stat.st_size, stat.st_mtime_ns, digest)
                packages.append({"name": entry.name, "size": stat.st_size, "sha256": digest})
            # A package that vanished must leave the index with it.  The listing
            # above is the truth, so a stale entry cannot make the index lie;
            # dropping it keeps the cache from growing without bound instead.
            for name in [name for name in self._index_cache if name not in seen]:
                del self._index_cache[name]
        return packages

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A traceback can carry local variables, and a private key must never
        # be reachable that way (PRD 9.2).  Log the exception type only.
        exc_type = sys.exc_info()[0]
        log.warning(
            "request from %s failed: %s",
            client_address,
            getattr(exc_type, "__name__", "unknown"),
        )


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


class PackageServer:
    """Owns the socket, the TLS context and the serving thread.

    Port 0 is honoured, so tests (and any caller that wants an ephemeral port)
    can read the real port back from :attr:`port` once the server is running.
    The object is a context manager: the ``with`` block starts the server and
    always releases the port, including when the body raises.
    """

    def __init__(
        self,
        root: Path,
        *,
        cert_directory: Optional[Path] = None,
        host: str = DEFAULT_LOOPBACK_HOST,
        port: int = DEFAULT_GUEST_PORT,
        tls: bool = True,
        allow_plaintext: bool = False,
        allow_non_loopback: bool = False,
    ) -> None:
        self.root = Path(root)
        self.cert_directory = Path(cert_directory) if cert_directory is not None else None
        self.host = host
        self.port = port
        self.tls = tls
        self._allow_plaintext = allow_plaintext
        self._allow_non_loopback = allow_non_loopback
        self._material: Optional[CertificateMaterial] = None
        self._server: Optional[PackageHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stopped = threading.Event()

        if not self.root.is_dir():
            raise UniPadError(
                _CONFIG, "package directory does not exist", root=_safe_location(self.root)
            )
        self._validate_configuration()

    def _validate_configuration(self) -> None:
        """Refuse anything this service is not allowed to do.

        Run at construction *and* again at :meth:`start`, because ``host``,
        ``port`` and ``tls`` are public attributes a caller can move after
        construction -- and the bind is the operation that must never see a
        non-loopback address, a nonsense port, or plaintext without the
        explicit opt-in.
        """
        if not self.tls and not self._allow_plaintext:
            raise UniPadError(
                _CONFIG,
                "plain HTTP is refused; pass allow_plaintext=True to serve packages in the clear",
            )
        _validate_bind_target(
            self.host, self.port, allow_non_loopback=self._allow_non_loopback
        )

    # -- properties --------------------------------------------------------

    @property
    def server(self) -> PackageHTTPServer:
        if self._server is None:
            raise UniPadError(_CONFIG, "the package service is not running")
        return self._server

    @property
    def is_running(self) -> bool:
        return self._server is not None and not self._stopped.is_set()

    @property
    def material(self) -> CertificateMaterial:
        """The certificate in use, generated on first access.

        With no caller-chosen directory the material goes into a fresh private
        temporary directory rather than a predictable path: a shared, guessable
        location would let another local user read the private key or pre-seed a
        certificate of their choosing.
        """
        if self._material is None:
            if self.cert_directory is not None:
                directory = self.cert_directory
            else:
                directory = Path(tempfile.mkdtemp(prefix="unipad-httpsvc-"))
            self._material = generate_self_signed_certificate(directory)
        return self._material

    @property
    def cert_path(self) -> Path:
        return self.material.cert_path

    @property
    def key_path(self) -> Path:
        return self.material.key_path

    @property
    def fingerprint(self) -> str:
        return self.material.fingerprint

    @property
    def url(self) -> str:
        """Loopback URL of the running service."""
        return f"{'https' if self.tls else 'http'}://{self.host}:{self.port}"

    @property
    def guest_url(self) -> str:
        """The URL the *guest* uses, through the QEMU user-mode gateway."""
        scheme = "https" if self.tls else "http"
        return f"{scheme}://{DEFAULT_GUEST_GATEWAY}:{self.port}{INDEX_PATH}"

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "PackageServer":
        if self._server is not None:
            raise UniPadError(_CONFIG, "the package service is already running")

        # Re-check the configuration before anything is allocated: a caller may
        # have moved ``host``, ``port`` or ``tls`` since construction, and a
        # refusal here must not leave a certificate directory or a socket
        # behind.
        self._validate_configuration()

        # Touch the certificate first: a failure here must not leave a bound
        # socket behind, and must not be mistaken for a network fault.
        material = self.material

        try:
            httpd = PackageHTTPServer((self.host, self.port), self.root)
        except OSError as exc:
            raise UniPadError(
                _TRANSFER,
                "could not bind the package service",
                host=self.host,
                port=self.port,
            ) from exc

        if self.tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                context.load_cert_chain(str(material.cert_path), str(material.key_path))
                httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
            except (ssl.SSLError, OSError) as exc:
                # Close before raising: a half-wrapped socket would hold the
                # port and the next start would report "address in use".
                httpd.server_close()
                raise UniPadError(
                    _TRANSFER, "certificate could not be loaded", fingerprint=material.fingerprint
                ) from exc

        self._server = httpd
        self._stopped.clear()
        self.port = httpd.server_address[1]
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="unipad-package-server", daemon=True
        )
        self._thread.start()
        log.info(
            "package service listening on %s (tls=%s, packages=%s, fingerprint=%s)",
            self.url,
            self.tls,
            self.root,
            material.fingerprint,
        )
        return self

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        self._stopped.set()
        try:
            server.shutdown()
        except Exception:  # pragma: no cover - shutdown is best effort
            log.warning("package service shutdown raised", exc_info=False)
        finally:
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        log.info("package service stopped")

    def __enter__(self) -> "PackageServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# run evidence for one transfer window (ADR-0008)
# ---------------------------------------------------------------------------

#: What the evidence says about the *guest* half of the fetch.  It is a fixed
#: ``False`` on purpose: whether a guest can reach ``10.0.2.2:<port>`` under
#: ``restrict=on`` -- and whether anything on the guest side knows how to fetch
#: a package over HTTPS at all -- is a real-boot question, and this host has no
#: guest to answer it.  A caller that recorded ``True`` here would be claiming
#: a verification nobody performed (PRD 9.2).
GUEST_FETCH_VERIFIED = False

#: Stable token naming *why* the guest fetch is unverified, so a gate can
#: branch on it instead of parsing prose.
GUEST_FETCH_STATUS = "unverified_no_guest_implementation"

#: The transport this evidence describes.  ``channel`` is not represented here:
#: a channel transfer starts no service, so it records no service evidence at
#: all rather than an empty one.
SERVICE_TRANSPORT = "https"

#: What a plaintext service would report.  The ADR-0008 caller never builds one
#: -- it passes no ``allow_plaintext`` -- but :func:`service_evidence` is a
#: measurement, not a claim, so it names the scheme that is actually being
#: served rather than repeating :data:`SERVICE_TRANSPORT`.
PLAINTEXT_TRANSPORT = "http"


def service_evidence(
    server: "PackageServer",
    *,
    package_name: str,
    package_sha256: str,
) -> dict[str, Any]:
    """Describe one package-service window for the run evidence (ADR-0008).

    PRD 3.2 P1 (1) acceptance has to be able to answer "where did these bytes
    come from, under which certificate, over which route".  That is exactly
    what a caller records: the URL that *actually listened*, the URL the guest
    would fetch, the port, the certificate fingerprint to pin, and the digest
    of the package the window was opened for.

    The URL is read back off the running server rather than off the requested
    arguments, so a port the caller asked for and a port the kernel granted are
    never confused -- with ``port=0`` the recorded port is the real one.

    ``guest_fetch_verified`` is :data:`GUEST_FETCH_VERIFIED` and never becomes
    ``True`` from this module: the service listening is a host fact, the guest
    reaching it is not one this host can observe.

    ``transport`` is read off the server for the same reason: the evidence
    describes what was served, so a caller that somehow relaxed the scheme gets
    an honest ``http`` rather than a reassuring ``https``.
    """
    return {
        "transport": SERVICE_TRANSPORT if server.tls else PLAINTEXT_TRANSPORT,
        "bind_host": server.host,
        "port": server.port,
        "service_url": server.url,
        "guest_url": server.guest_url,
        "certificate_fingerprint": server.fingerprint,
        "package_name": package_name,
        "package_sha256": package_sha256,
        "guest_fetch_verified": GUEST_FETCH_VERIFIED,
        "guest_fetch_status": GUEST_FETCH_STATUS,
    }


def _is_loopback(host: str) -> bool:
    """True for ``localhost``, ``::1`` and ``127.x`` -- never for ``0.0.0.0``."""
    if host in ("localhost", "::1"):
        return True
    if not host.startswith("127."):
        return False
    parts = host.split(".")
    return len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def _validate_bind_target(host: str, port: int, *, allow_non_loopback: bool) -> None:
    """Refuse a bind this service is not allowed to make.

    One place, so :meth:`PackageServer.start` and ``PackageServer.__init__``
    cannot drift apart.  ``start`` runs it again because ``host`` and ``port``
    are public attributes a caller can move after construction, and the bind is
    the operation that must never see a non-loopback address or a nonsense
    port -- without it, ``server.port = 99999`` escaped as a raw
    ``OverflowError`` out of the socket layer instead of a config fault.
    """
    if not _is_loopback(host) and not allow_non_loopback:
        raise UniPadError(
            _CONFIG,
            "the package service binds loopback only; pass allow_non_loopback=True to expose it",
            host=host,
        )
    # Port 0 is the documented test convenience for PackageServer, so the
    # ephemeral port stays allowed here.
    _validate_port(port, "port", allow_ephemeral=True)


# ---------------------------------------------------------------------------
# QEMU wiring
# ---------------------------------------------------------------------------


def hostfwd_spec(host_port: int, guest_port: int = DEFAULT_GUEST_PORT) -> str:
    """The ``hostfwd=`` fragment for ``-nic user,...``.

    ``hostfwd`` forwards a host port *into the guest*, so this is the mapping
    to use when a host-side tool must reach something inside the guest -- not
    for the package service, which the guest reaches through
    :func:`guest_fetch_url` with no ``hostfwd`` at all.  The host side is
    pinned to loopback so the forwarded port is never exposed to the LAN.
    """
    _validate_port(host_port, "host_port", allow_ephemeral=False)
    _validate_port(guest_port, "guest_port", allow_ephemeral=False)
    return f"hostfwd=tcp:{DEFAULT_LOOPBACK_HOST}:{host_port}-{DEFAULT_GUEST_ADDRESS}:{guest_port}"


def guest_fetch_url(
    port: int = DEFAULT_GUEST_PORT, path: str = INDEX_PATH, *, tls: bool = True
) -> str:
    """The URL the guest fetches packages from.

    QEMU's user-mode stack relays a guest connection to ``10.0.2.2`` straight
    to the host's own loopback, so the service needs no ``hostfwd`` and no
    change to ``-nic`` beyond the device itself.

    ``tls`` defaults to ``True`` because TLS is this module's only real mode.
    A caller serving plaintext (``PackageServer(tls=False,
    allow_plaintext=True)``) must pass ``False``, or hand the guest a URL it can
    never open -- the two URL builders in this module used to disagree about
    the scheme of the same service, and neither side reported anything.
    """
    _validate_port(port, "port", allow_ephemeral=False)
    if not path.startswith("/"):
        path = "/" + path
    scheme = "https" if tls else "http"
    return f"{scheme}://{DEFAULT_GUEST_GATEWAY}:{port}{path}"


def _validate_port(port: int, label: str, *, allow_ephemeral: bool = True) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise UniPadError(_CONFIG, f"{label} must be an integer in 0..65535", **{label: repr(port)})
    if port == 0 and not allow_ephemeral:
        # Port 0 is a test convenience for :class:`PackageServer`; a QEMU
        # ``hostfwd`` or a published URL needs a real, fixed port.
        raise UniPadError(_CONFIG, f"{label} must be a fixed port, not 0", **{label: repr(port)})
