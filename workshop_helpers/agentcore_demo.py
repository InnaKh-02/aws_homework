from __future__ import annotations

from .demo_core import WorkshopE2EDemoBase
from .demo_gateway import WorkshopE2EGatewayMixin
from .demo_runtime import WorkshopE2ERuntimeMixin


class WorkshopE2EDemo(
    WorkshopE2ERuntimeMixin,
    WorkshopE2EGatewayMixin,
    WorkshopE2EDemoBase,
):
    """Public facade for the workshop demo helper, split by responsibility."""


__all__ = ["WorkshopE2EDemo"]
