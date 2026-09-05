"""Compatibility facade for the versioned commerce migration registry."""

from commerce_migrations_capacity import COMMERCE_MIGRATIONS_CAPACITY
from commerce_migrations_endpoints import COMMERCE_MIGRATIONS_ENDPOINTS
from commerce_migrations_identity import COMMERCE_MIGRATIONS_IDENTITY
from commerce_migrations_probes import COMMERCE_MIGRATIONS_PROBES
from commerce_migrations_quota import COMMERCE_MIGRATIONS_QUOTA
from commerce_migrations_receipts import COMMERCE_MIGRATIONS_RECEIPTS
from commerce_migrations_routing import COMMERCE_MIGRATIONS_ROUTING

COMMERCE_MIGRATIONS = (
    COMMERCE_MIGRATIONS_RECEIPTS
    + COMMERCE_MIGRATIONS_CAPACITY
    + COMMERCE_MIGRATIONS_IDENTITY
    + COMMERCE_MIGRATIONS_QUOTA
    + COMMERCE_MIGRATIONS_PROBES
    + COMMERCE_MIGRATIONS_ENDPOINTS
    + COMMERCE_MIGRATIONS_ROUTING
)

__all__ = ["COMMERCE_MIGRATIONS"]
