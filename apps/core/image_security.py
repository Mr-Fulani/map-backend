"""Shared resource limits for decoding untrusted images."""

from django.conf import settings


DEFAULT_MAX_DECODED_IMAGE_PIXELS = 16_000_000


class ImagePixelLimitExceeded(ValueError):
    """The decoded image would exceed the configured pixel budget."""


def validate_image_pixel_budget(image) -> None:
    """Reject invalid dimensions and decompression bombs before ``image.load``."""
    width, height = image.size
    max_pixels = int(getattr(
        settings,
        'MAX_DECODED_IMAGE_PIXELS',
        DEFAULT_MAX_DECODED_IMAGE_PIXELS,
    ))
    if width <= 0 or height <= 0 or width * height > max_pixels:
        raise ImagePixelLimitExceeded(
            f'Изображение превышает лимит декодирования {max_pixels} пикселей.',
        )
