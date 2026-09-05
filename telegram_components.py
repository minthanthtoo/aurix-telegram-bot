"""Composition helpers for Telegram routing and presentation components."""

from __future__ import annotations

from typing import Any

from telegram_admin_panels import TelegramAdminMixin
from telegram_callbacks import TelegramCallbackMixin
from telegram_commands import TelegramCommandMixin
from telegram_maintenance import TelegramMaintenanceMixin
from telegram_transport_admin import TelegramAdminTransportMixin
from telegram_transport_customer import TelegramCustomerTransportMixin
from telegram_transport_customer_vpn import TelegramCustomerVpnTransportMixin
from telegram_transport_receipts import TelegramReceiptTransportMixin
from telegram_transport_runtime import TelegramRuntimeTransportMixin


class TelegramComponent:
    """Bind one legacy presentation component to the transport host."""

    def __init__(self, host: Any, implementation: type[Any]):
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "implementation", implementation)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"host", "implementation"}:
            object.__setattr__(self, name, value)
            return
        setattr(self.host, name, value)

    def owns(self, name: str) -> bool:
        return any(name in vars(owner) for owner in self.implementation.__mro__)

    def __getattr__(self, name: str) -> Any:
        # Preserve instance and subclass overrides on the transport host.
        missing = object()
        if name in vars(self.host):
            return vars(self.host)[name]
        if getattr(type(self.host), name, missing) is not missing:
            return getattr(self.host, name)
        raw = next(
            (vars(owner)[name] for owner in self.implementation.__mro__ if name in vars(owner)),
            None,
        )
        if isinstance(raw, staticmethod):
            return raw.__func__
        if raw is not None:
            if callable(raw):
                return raw.__get__(self, type(self))
            return raw
        return getattr(self.host, name)


class TelegramComponents:
    """The four bounded presentation collaborators used by ``TelegramBot``."""

    def __init__(self, host: Any):
        self.admin_panels = TelegramComponent(host, TelegramAdminMixin)
        self.callbacks = TelegramComponent(host, TelegramCallbackMixin)
        self.commands = TelegramComponent(host, TelegramCommandMixin)
        self.maintenance = TelegramComponent(host, TelegramMaintenanceMixin)
        self.transport_admin = TelegramComponent(host, TelegramAdminTransportMixin)
        self.transport_customer = TelegramComponent(host, TelegramCustomerTransportMixin)
        self.transport_customer_vpn = TelegramComponent(host, TelegramCustomerVpnTransportMixin)
        self.transport_receipts = TelegramComponent(host, TelegramReceiptTransportMixin)
        self.transport_runtime = TelegramComponent(host, TelegramRuntimeTransportMixin)
        self._all = (
            self.admin_panels,
            self.callbacks,
            self.commands,
            self.maintenance,
            self.transport_admin,
            self.transport_customer,
            self.transport_customer_vpn,
            self.transport_receipts,
            self.transport_runtime,
        )

    def resolve(self, name: str) -> Any:
        for component in self._all:
            if component.owns(name):
                return getattr(component, name)
        raise AttributeError(name)
