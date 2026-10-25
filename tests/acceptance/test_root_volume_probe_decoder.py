"""The Windows root-volume probe decodes with the repository's own decoder.

Why this file exists
-------------------
``scripts/windows/probe-root-volume.py`` is the thing that turns the pinned
IPSW's OS member into a raw volume Windows QEMU can attach, and for a while it
handed ``UdifImage.write_raw`` a ``LzfseDecoder()`` built with **no argument**.
That constructor's refusal is deliberate -- an image that decoded wrong is
indistinguishable from one that decoded right until the GPT CRC is checked, and
by then 6 GB has been written -- so the probe could never have decoded a real
image at all.  The docstring said so ("No LZFSE decoder is supplied, which is
the honest default on this host"), which was true and also the whole problem:
the honest default was a probe that could not do its job.

``src/unipad/host/lzfse.py`` closed that gap (pure standard library, because
``pip`` is broken on this host), and ``udif.default_lzfse()`` is the wiring.
This file pins the wiring **behaviourally** rather than by reading the source:
a test that asserted the probe's source contains the string ``default_lzfse``
would pass just as well if the call were dead code, and would not notice if the
decoder were swapped back for the refusal.

What is proven here
-------------------
* ``go_decode`` hands ``write_raw`` a decoder that really decodes -- exercised
  against a **real** LZFSE chunk from the pinned image, so the refusal fails
  here rather than an hour into a 6 GB write.
* On a spread of real chunks, the port's output is byte-identical to an
  independently produced decode of the same image
  (``F:/unipad-tmp/os-root.raw``, 6,077,580,800 bytes).  That comparison is the
  strongest one available on this host: it does not ask whether the port agrees
  with itself, it asks whether it agrees with someone else's implementation on
  the real bytes.  It skips when either artifact is absent, because both live
  outside the repository by design (PRD 12).

What is NOT proven here
-----------------------
That the **whole** 6 GB image decodes correctly end to end.  The sample below
covers 64 chunks spread across the image; the other ~3,350 are the same code
paths on different bytes.  A full decode is roughly an hour of pure CPU on this
host and is a measurement to take deliberately, not a test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from unipad.host.udif import CHUNK_LZFSE, UdifImage

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "scripts" / "windows" / "probe-root-volume.py"
REAL_IMAGE = REPO / "local-private/out/components/044-76040-038.dmg"
INDEPENDENT_DECODE = Path("F:/unipad-tmp/os-root.raw")

pytestmark = pytest.mark.acceptance


def _load_probe():
    """Import the real probe script as a module, without running its ``main``."""
    spec = importlib.util.spec_from_file_location("probe_root_volume_under_test", PROBE)
    assert spec is not None and spec.loader is not None, PROBE
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


class _RecordingImage:
    """Stands in for ``UdifImage`` and records the decoder it is handed.

    It then *uses* that decoder on the caller's real chunk, which is what makes
    this more than a source check: the bare refusal raises, so a probe that
    regressed to ``LzfseDecoder()`` fails here rather than at the GPT CRC.
    """

    def __init__(self, compressed: bytes, expected: int, chunk_count: int = 1) -> None:
        self.raw_size = expected
        self._compressed = compressed
        self._chunk_count = chunk_count
        self.decoder = None

    def chunks(self):
        """One chunk per call is enough: ``go_decode`` only counts them."""
        return iter([None] * self._chunk_count)

    def write_raw(self, destination, lzfse, progress=None):  # noqa: ANN001
        self.decoder = lzfse
        produced = lzfse.decode(self._compressed, self.raw_size)
        destination.write_bytes(produced)
        return {"raw_size": len(produced)}


def _first_lzfse_chunk(image: UdifImage) -> tuple[bytes, int]:
    """One real LZFSE chunk from the pinned image, as ``(compressed, expected)``."""
    for chunk in image.chunks():
        if chunk.type == CHUNK_LZFSE:
            # ``_read_compressed`` is private and is used deliberately: there is
            # no public route to "the compressed bytes of this chunk", and the
            # alternative -- going through ``chunk_bytes`` -- would need the very
            # decoder whose wiring is under test.
            return image._read_compressed(chunk), chunk.byte_length
    raise AssertionError("the pinned OS member has no LZFSE chunk")


def test_the_windows_probe_decodes_with_the_repositorys_own_decoder(tmp_path) -> None:
    """``go_decode`` must hand ``write_raw`` a decoder that works.

    Before ``default_lzfse()`` this probe passed a ``LzfseDecoder()`` with no
    argument, whose ``decode`` raises -- so the probe could not decode anything,
    and the only thing standing between that and a silent failure was the
    refusal itself.  Exercised against a real chunk rather than a stub so the
    assertion is about bytes, not about which object was constructed.
    """
    if not REAL_IMAGE.is_file():  # pragma: no cover - local-private/ material
        pytest.skip(f"the pinned OS member is not present at {REAL_IMAGE}")
    compressed, expected = _first_lzfse_chunk(UdifImage(REAL_IMAGE))

    probe = _load_probe()
    stand_in = _RecordingImage(compressed, expected)
    destination = tmp_path / "root.raw"

    original = probe.UdifImage
    probe.UdifImage = lambda source: stand_in
    try:
        produced = probe.go_decode(REAL_IMAGE, destination)
    finally:
        probe.UdifImage = original

    assert stand_in.decoder is not None, "write_raw was handed no decoder at all"
    assert destination.read_bytes() == stand_in.decoder.decode(compressed, expected)
    assert produced["raw_size"] == expected


def _sample_chunks(image: UdifImage, count: int) -> list:
    """``count`` LZFSE chunks spread across the image, not the first ``count``.

    The first chunks of a UDIF image are the ones every quick test decodes; the
    interesting ones are in the middle and at the end, where the recent-offsets
    table and the block boundaries have been exercised.  An even spread covers
    more of the table than a prefix does, and it is deterministic because it is
    derived from the chunk index rather than from anything ambient.
    """
    chunks = [chunk for chunk in image.chunks() if chunk.type == CHUNK_LZFSE]
    if len(chunks) <= count:
        return chunks
    step = len(chunks) / count
    return [chunks[int(index * step)] for index in range(count)]


def test_the_port_agrees_with_an_independent_decode_of_the_real_image() -> None:
    """Byte-for-byte against a decode produced by another implementation.

    ``INDEPENDENT_DECODE`` was produced outside this repository and its GPT CRCs
    matched (see ``tests/unit/test_host_udif.py``).  Comparing the port's output
    for a spread of real chunks against it asks the only question that matters --
    does it produce the right bytes -- rather than whether it agrees with itself.
    """
    if not REAL_IMAGE.is_file() or not INDEPENDENT_DECODE.is_file():
        pytest.skip("the pinned OS member or the independent decode is not on this host")

    image = UdifImage(REAL_IMAGE)
    sample = _sample_chunks(image, 64)
    assert sample, "the pinned image has no LZFSE chunks to compare"

    from unipad.host import lzfse

    with INDEPENDENT_DECODE.open("rb") as reference:
        for chunk in sample:
            produced = lzfse.decode(image._read_compressed(chunk), chunk.byte_length)
            reference.seek(chunk.byte_offset)
            assert produced == reference.read(chunk.byte_length), (
                f"the chunk at sector {chunk.sector} (offset {chunk.byte_offset}, "
                f"{chunk.byte_length} bytes) decoded to different bytes than the "
                "independent decode"
            )
