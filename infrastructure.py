"""Guarded DigitalOcean fleet intents for a separate operator worker."""

from __future__ import annotations

from typing import Any

from digitalocean_client import DigitalOceanClient
from infrastructure_inventory import FleetInventoryMixin
from infrastructure_provisioning import FleetProvisioningMixin
from infrastructure_support import InfrastructureError, UTC, _enabled


class FleetController(FleetInventoryMixin, FleetProvisioningMixin):
    """Create and reconcile bounded provider jobs."""

    def __init__(self, database: Any, provider: DigitalOceanClient | None = None):
        self.database = database
        self.provider = provider
