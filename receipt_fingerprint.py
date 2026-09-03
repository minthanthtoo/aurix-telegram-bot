"""Cheap, privacy-preserving receipt-image fingerprints.

The fingerprint is a duplicate *signal*, never payment proof.  It is designed
to catch a screenshot that was resized, recompressed, or lightly cropped when
its byte-level SHA-256 and Telegram file identity changed.  Ambiguous matches
remain visible to staff; the fingerprint alone never credits a wallet or
approves an order.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError


FINGERPRINT_VERSION = "ahash16-v1"
FINGERPRINT_SIZE = 16


def receipt_perceptual_hash(image_bytes: bytes) -> str | None:
    """Return a 256-bit average hash, or ``None`` for an undecodable image."""
    if not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("L")
            # A fixed square makes the signal stable across Telegram's
            # recompression and common screenshot resizing.  This intentionally
            # does not crop: crop/overlay changes should be reviewable.
            image = image.resize((FINGERPRINT_SIZE, FINGERPRINT_SIZE), Image.Resampling.LANCZOS)
            # ``tobytes`` avoids Pillow's versioned ``getdata`` deprecation
            # while preserving one byte per grayscale pixel.
            pixels = list(image.tobytes())
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    if len(pixels) != FINGERPRINT_SIZE * FINGERPRINT_SIZE:
        return None
    average = sum(pixels) / len(pixels)
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | int(pixel >= average)
    width = FINGERPRINT_SIZE * FINGERPRINT_SIZE // 4
    return f"{FINGERPRINT_VERSION}:{bits:0{width}x}"


def fingerprint_distance(left: str | None, right: str | None) -> int | None:
    """Return Hamming distance for matching fingerprint versions."""
    if not left or not right:
        return None
    try:
        left_version, left_bits = left.split(":", 1)
        right_version, right_bits = right.split(":", 1)
        if left_version != right_version or len(left_bits) != len(right_bits):
            return None
        return (int(left_bits, 16) ^ int(right_bits, 16)).bit_count()
    except (AttributeError, TypeError, ValueError):
        return None


# A low threshold catches benign re-encoding while reducing collisions between
# different receipts that share the same provider template.  It is only used
# to block a cross-order near-duplicate upload; transaction-reference checks
# remain the authoritative verification control.
NEAR_DUPLICATE_DISTANCE = 6
