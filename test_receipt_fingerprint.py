import io
import unittest

from PIL import Image, ImageDraw

from receipt_fingerprint import fingerprint_distance, receipt_perceptual_hash


class ReceiptFingerprintTest(unittest.TestCase):
    @staticmethod
    def _image_bytes(*, invert: bool = False, quality: int = 95) -> bytes:
        image = Image.new("RGB", (320, 180), "white" if not invert else "black")
        draw = ImageDraw.Draw(image)
        fill = "black" if not invert else "white"
        draw.rectangle((20, 20, 210, 65), fill=fill)
        draw.rectangle((80, 100, 300, 145), outline=fill, width=8)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality)
        return output.getvalue()

    def test_reencoded_receipt_has_a_close_fingerprint(self):
        original = receipt_perceptual_hash(self._image_bytes(quality=95))
        recompressed = receipt_perceptual_hash(self._image_bytes(quality=45))

        self.assertIsNotNone(original)
        self.assertIsNotNone(recompressed)
        self.assertLessEqual(fingerprint_distance(original, recompressed), 6)

    def test_different_receipt_is_not_treated_as_near_duplicate(self):
        first = receipt_perceptual_hash(self._image_bytes())
        other = receipt_perceptual_hash(self._image_bytes(invert=True))

        self.assertIsNotNone(first)
        self.assertIsNotNone(other)
        self.assertGreater(fingerprint_distance(first, other), 6)

    def test_invalid_image_has_no_fingerprint(self):
        self.assertIsNone(receipt_perceptual_hash(b"not an image"))
        self.assertIsNone(fingerprint_distance(None, "ahash16-v1:00"))


if __name__ == "__main__":
    unittest.main()

