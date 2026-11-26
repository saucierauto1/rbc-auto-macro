"""Normalised config regions (0..1 of the client area) -> screen pixel rectangles."""

from __future__ import annotations

from ..core.config import Region
from .window import Rect


def to_pixels(region: Region, client: Rect) -> Rect:
    """Map a normalised region onto the client area. Always at least 1x1 and
    clamped inside the client rect, so rounding can't produce an empty grab."""
    left = min(client.left + round(region.x * client.width), client.right - 1)
    top = min(client.top + round(region.y * client.height), client.bottom - 1)
    w = max(1, round(region.w * client.width))
    h = max(1, round(region.h * client.height))
    w = min(w, client.right - left)
    h = min(h, client.bottom - top)
    return Rect(left, top, max(1, w), max(1, h))


def to_local(rect: Rect, client: Rect) -> Rect:
    """Screen rect -> coordinates inside the client area (for overlays)."""
    return Rect(rect.left - client.left, rect.top - client.top, rect.width, rect.height)


def template_scale(client_height: int, reference_height: int) -> float:
    """How much to rescale template images captured at the reference resolution."""
    return client_height / reference_height if reference_height else 1.0
