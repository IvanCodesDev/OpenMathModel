#!/usr/bin/env python3
"""Reject images that should not be republished, independent of source format.

Both ingesters need this test: figures reach us from PDFs through pdf_layout and
from DOCX archives through ingest_mathmodel_full_problems. Keeping it here means
the DOCX path does not import the PDF stack (pdfplumber, pypdf) to check a PNG.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


# Measured over the published corpus: the one real QR scores 0.27, and the
# highest-scoring non-QR figure -- a dense text screenshot -- scores 0.18.
QR_FINDER_THRESHOLD = 0.22
# Below this run length the 1:1:3:1:1 ratio is resampling noise, not structure.
QR_MIN_MODULE = 1.5
# Side of the square every candidate is resampled onto before scanning, so the
# score means the same thing regardless of the source resolution.
QR_SCAN_SIZE = 160
# A QR symbol is square; the tolerance covers a slightly non-square crop.
QR_ASPECT_RANGE = (0.8, 1.25)
# Modules are pure black or white, so a QR is essentially two-tone. Charts and
# photographs carry mid-greys and fall out here before the expensive scan.
QR_MIN_TWO_TONE = 0.7


def _runs(line: list[int]) -> list[list[int]]:
    """Collapse a scan line into (value, length) pairs."""
    runs: list[list[int]] = []
    for value in line:
        if runs and runs[-1][0] == value:
            runs[-1][1] += 1
        else:
            runs.append([value, 1])
    return runs


def crosses_finder_pattern(line: list[int]) -> bool:
    """Report whether one scan line crosses a QR finder pattern.

    A finder is three concentric squares, so any line through its centre reads
    dark/light/dark/light/dark in 1:1:3:1:1 proportion. Testing the ratio rather
    than matching a fixed template keeps this independent of the symbol's
    version, which matters because the module count grows with the payload.
    """
    sequence = _runs(line)
    for index in range(len(sequence) - 4):
        window = sequence[index:index + 5]
        if [item[0] for item in window] != [1, 0, 1, 0, 1]:
            continue
        lengths = [item[1] for item in window]
        unit = (lengths[0] + lengths[1] + lengths[3] + lengths[4]) / 4
        if unit < QR_MIN_MODULE:
            continue
        if all(abs(lengths[i] - unit) <= unit * 0.5 for i in (0, 1, 3, 4)) \
                and abs(lengths[2] - unit * 3) <= unit * 1.2:
            return True
    return False


def qr_finder_score(path: Path) -> float:
    """Fraction of scan lines that cross a finder pattern, or 0.0 for non-images.

    Rows and columns are both scanned because a finder contributes hits along
    each axis; a chart's grid lines produce runs but essentially never the
    1:1:3:1:1 proportion, which is what separates the two.
    """
    with Image.open(path) as image:
        width, height = image.size
        if not height or not QR_ASPECT_RANGE[0] <= width / height <= QR_ASPECT_RANGE[1]:
            return 0.0
        # A transparent background reads as black once flattened naively, which
        # would invent dark runs, so alpha is composited onto white first.
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            flat = Image.new("RGBA", image.size, (255, 255, 255, 255))
            flat.alpha_composite(image)
            image = flat
        grey = image.convert("L")
        histogram = grey.histogram()
        two_tone = (sum(histogram[:64]) + sum(histogram[192:])) / max(1, width * height)
        if two_tone < QR_MIN_TWO_TONE:
            return 0.0
        size = QR_SCAN_SIZE
        pixels = grey.resize((size, size), Image.BILINEAR).load()
        rows = [[1 if pixels[x, y] < 128 else 0 for x in range(size)] for y in range(size)]
        hits = sum(1 for row in rows if crosses_finder_pattern(row))
        hits += sum(1 for x in range(size)
                    if crosses_finder_pattern([rows[y][x] for y in range(size)]))
    return hits / (2 * size)


def looks_like_qr_code(path: Path) -> bool:
    """Reject QR codes: they point off-site and are not part of the problem.

    Community scans carry the uploader's own contact QR. Republishing it would
    send readers to a third-party account that has nothing to do with the
    problem statement, so the image is dropped rather than mirrored.
    """
    try:
        return qr_finder_score(path) >= QR_FINDER_THRESHOLD
    except Exception:
        # An unreadable or exotic image is not evidence of a QR code; leave it to
        # the caller's own size and format gates.
        return False
