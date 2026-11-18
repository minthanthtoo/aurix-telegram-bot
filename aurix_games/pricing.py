"""Deterministic supplier-cost to customer-price calculations.

The supplier snapshot keeps wholesale costs private.  This module turns one
approved cost into a customer price using an operator-owned policy.  It is
deliberately pure: no database, network, currency feed, promotion fetch, or
payment side effect is hidden in the calculation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from typing import Any, Mapping


class PricingInputError(ValueError):
    """An operator pricing rule or supplier cost is unsafe."""


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise PricingInputError(f"{label} must be a finite decimal")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise PricingInputError(f"{label} must be a finite decimal") from None
    if not result.is_finite():
        raise PricingInputError(f"{label} must be a finite decimal")
    return result


def _percent(value: Any, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result >= 1:
        raise PricingInputError(f"{label} must be at least 0 and below 1")
    return result


def _positive_int(value: Any, label: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PricingInputError(f"{label} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        raise PricingInputError(f"{label} must be {'non-negative' if allow_zero else 'positive'}")
    return value


def _ceil_minor(value: Decimal, rounding_minor: int) -> int:
    step = Decimal(rounding_minor)
    return int((value / step).to_integral_value(rounding=ROUND_CEILING) * step)


def _money(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


_INTEGER_POLICY_FIELDS = frozenset({
    "target_profit_minor", "operating_fee_minor", "payment_fee_minor",
    "promo_fixed_minor", "rounding_minor", "minimum_price_minor",
    "maximum_price_minor",
})


@dataclass(frozen=True)
class PricingPolicy:
    """Admin-owned pricing policy for one selling currency.

    Percentages are fractions, so ``Decimal("0.15")`` means 15%.  Marketing,
    payback, and payment fees are charged against the final customer price;
    the formula therefore solves for a price that preserves the configured
    target profit after those deductions.
    """

    sell_currency: str = "MMK"
    supplier_currency: str = "USD"
    fx_rate: Decimal = Decimal("4500")
    target_profit_pct: Decimal = Decimal("0.15")
    minimum_profit_pct: Decimal | None = None
    target_profit_minor: int = 0
    operating_fee_minor: int = 0
    payment_fee_pct: Decimal = Decimal("0")
    payment_fee_minor: int = 0
    marketing_pct: Decimal = Decimal("0")
    payback_pct: Decimal = Decimal("0")
    promo_pct: Decimal = Decimal("0")
    promo_fixed_minor: int = 0
    rounding_minor: int = 100
    minimum_price_minor: int = 0
    maximum_price_minor: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.sell_currency, str) or len(self.sell_currency) != 3 or not self.sell_currency.isupper():
            raise PricingInputError("sell_currency must be a three-letter uppercase code")
        if not isinstance(self.supplier_currency, str) or len(self.supplier_currency) != 3 or not self.supplier_currency.isupper():
            raise PricingInputError("supplier_currency must be a three-letter uppercase code")
        if _decimal(self.fx_rate, "fx_rate") <= 0:
            raise PricingInputError("fx_rate must be positive")
        for name in (
            "target_profit_pct",
            "payment_fee_pct",
            "marketing_pct",
            "payback_pct",
            "promo_pct",
        ):
            _percent(getattr(self, name), name)
        if self.minimum_profit_pct is not None:
            _percent(self.minimum_profit_pct, "minimum_profit_pct")
            if self.minimum_profit_pct > self.target_profit_pct:
                raise PricingInputError("minimum_profit_pct cannot exceed target_profit_pct")
        for name in (
            "target_profit_minor",
            "operating_fee_minor",
            "payment_fee_minor",
            "promo_fixed_minor",
            "minimum_price_minor",
        ):
            _positive_int(getattr(self, name), name, allow_zero=True)
        _positive_int(self.rounding_minor, "rounding_minor")
        if self.maximum_price_minor is not None:
            _positive_int(self.maximum_price_minor, "maximum_price_minor")
            if self.maximum_price_minor < self.minimum_price_minor:
                raise PricingInputError("maximum_price_minor cannot be below minimum_price_minor")
        leak = sum(
            (_decimal(getattr(self, name), name) for name in ("payment_fee_pct", "marketing_pct", "payback_pct")),
            Decimal("0"),
        )
        if leak >= 1:
            raise PricingInputError("payment, marketing, and payback percentages must total below 1")

    def as_dict(self) -> dict[str, Any]:
        """Serialize an operator policy without binary floats."""

        result: dict[str, Any] = {
            "sell_currency": self.sell_currency,
            "supplier_currency": self.supplier_currency,
            "fx_rate": format(_decimal(self.fx_rate, "fx_rate"), "f"),
            "target_profit_pct": format(_decimal(self.target_profit_pct, "target_profit_pct"), "f"),
            "minimum_profit_pct": None if self.minimum_profit_pct is None else format(_decimal(self.minimum_profit_pct, "minimum_profit_pct"), "f"),
            "target_profit_minor": self.target_profit_minor,
            "operating_fee_minor": self.operating_fee_minor,
            "payment_fee_pct": format(_decimal(self.payment_fee_pct, "payment_fee_pct"), "f"),
            "payment_fee_minor": self.payment_fee_minor,
            "marketing_pct": format(_decimal(self.marketing_pct, "marketing_pct"), "f"),
            "payback_pct": format(_decimal(self.payback_pct, "payback_pct"), "f"),
            "promo_pct": format(_decimal(self.promo_pct, "promo_pct"), "f"),
            "promo_fixed_minor": self.promo_fixed_minor,
            "rounding_minor": self.rounding_minor,
            "minimum_price_minor": self.minimum_price_minor,
            "maximum_price_minor": self.maximum_price_minor,
        }
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PricingPolicy":
        """Load only the known policy keys from an admin-owned JSON object."""

        if not isinstance(value, Mapping):
            raise PricingInputError("pricing policy must be an object")
        allowed = {
            "sell_currency", "supplier_currency", "fx_rate", "target_profit_pct",
            "minimum_profit_pct", "target_profit_minor", "operating_fee_minor",
            "payment_fee_pct", "payment_fee_minor", "marketing_pct", "payback_pct",
            "promo_pct", "promo_fixed_minor", "rounding_minor", "minimum_price_minor",
            "maximum_price_minor",
        }
        unknown = set(value) - allowed
        if unknown:
            raise PricingInputError("pricing policy contains unsupported fields")
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for name in allowed:
            if name not in value:
                continue
            item = value[name]
            if name.endswith("_pct") or name in {"fx_rate"}:
                if item is not None:
                    item = _decimal(item, name)
            elif name in _INTEGER_POLICY_FIELDS and item is not None:
                if isinstance(item, bool):
                    raise PricingInputError(f"{name} must be an integer")
                if isinstance(item, str):
                    normalized = item.strip()
                    if not normalized.isdecimal():
                        raise PricingInputError(f"{name} must be an integer")
                    item = int(normalized)
            kwargs[name] = item
        # Keep omitted fields on the class defaults while preserving the
        # explicit ``null`` meaning for minimum_profit_pct.
        for name, item in defaults.as_dict().items():
            if name not in kwargs and name not in {"fx_rate", "target_profit_pct", "payment_fee_pct", "marketing_pct", "payback_pct", "promo_pct", "minimum_profit_pct"}:
                kwargs[name] = item
        return cls(**kwargs)

    @classmethod
    def from_complete_mapping(cls, value: Mapping[str, Any]) -> "PricingPolicy":
        """Load a persisted policy only when every commercial field is explicit."""

        if not isinstance(value, Mapping):
            raise PricingInputError("pricing policy must be an object")
        missing = set(cls().as_dict()) - set(value)
        if missing:
            raise PricingInputError("pricing policy is incomplete")
        return cls.from_mapping(value)


@dataclass(frozen=True)
class PriceBreakdown:
    """Private accounting details plus the customer-visible final amount."""

    supplier_cost_minor: int
    base_price_minor: int
    requested_promo_discount_minor: int
    applied_promo_discount_minor: int
    final_price_minor: int
    marketing_minor: int
    payback_minor: int
    payment_fee_minor: int
    operating_fee_minor: int
    profit_minor: int
    profit_pct_on_cost: Decimal
    currency: str

    def as_public_dict(self) -> dict[str, Any]:
        """Return only fields suitable for a customer offer response."""

        return {
            "price_minor": self.final_price_minor,
            "currency": self.currency,
            "promo_discount_minor": self.applied_promo_discount_minor,
        }


def calculate_price(
    supplier_cost: Decimal | str | int | float,
    policy: PricingPolicy,
) -> PriceBreakdown:
    """Calculate a price that preserves target profit after all allocations.

    Promotional discount is capped automatically when applying the requested
    discount would violate the configured target profit.  A maximum price is
    treated as an operator safety limit: if it cannot preserve the target
    profit, the calculation fails instead of silently selling at a loss.
    """

    cost = _decimal(supplier_cost, "supplier_cost")
    if cost < 0:
        raise PricingInputError("supplier_cost cannot be negative")
    fx = _decimal(policy.fx_rate, "fx_rate")
    supplier_minor = _money(cost * fx)
    profit_target = (
        Decimal(supplier_minor) * _decimal(policy.target_profit_pct, "target_profit_pct")
        + Decimal(policy.target_profit_minor)
    )
    fixed = Decimal(policy.operating_fee_minor + policy.payment_fee_minor)
    required_before_variable = Decimal(supplier_minor) + fixed + profit_target
    denominator = Decimal("1") - sum(
        (_decimal(getattr(policy, name), name) for name in ("payment_fee_pct", "marketing_pct", "payback_pct")),
        Decimal("0"),
    )
    raw_base = required_before_variable / denominator
    base_price = max(policy.minimum_price_minor, _ceil_minor(raw_base, policy.rounding_minor))
    requested_discount = _money(
        Decimal(base_price) * _decimal(policy.promo_pct, "promo_pct")
        + Decimal(policy.promo_fixed_minor)
    )
    # Recompute the minimum after rounding.  This protects the profit floor if
    # a policy is changed later or the base price is raised by a minimum.
    minimum_profit_pct = (
        _decimal(policy.minimum_profit_pct, "minimum_profit_pct")
        if policy.minimum_profit_pct is not None
        else _decimal(policy.target_profit_pct, "target_profit_pct")
    )
    raw_minimum = (
        Decimal(supplier_minor) + fixed + Decimal(supplier_minor) * minimum_profit_pct
        + Decimal(policy.target_profit_minor)
    ) / denominator
    minimum_final = max(policy.minimum_price_minor, _ceil_minor(raw_minimum, policy.rounding_minor))
    applied_discount = min(requested_discount, max(0, base_price - minimum_final))
    final_price = max(minimum_final, base_price - applied_discount)
    final_price = _ceil_minor(Decimal(final_price), policy.rounding_minor)
    if policy.maximum_price_minor is not None and final_price > policy.maximum_price_minor:
        raise PricingInputError("maximum_price_minor cannot preserve the target profit")

    marketing = _money(Decimal(final_price) * _decimal(policy.marketing_pct, "marketing_pct"))
    payback = _money(Decimal(final_price) * _decimal(policy.payback_pct, "payback_pct"))
    payment_fee = policy.payment_fee_minor + _money(
        Decimal(final_price) * _decimal(policy.payment_fee_pct, "payment_fee_pct")
    )
    profit = final_price - supplier_minor - policy.operating_fee_minor - payment_fee - marketing - payback
    minimum_profit = (
        Decimal(supplier_minor) * minimum_profit_pct + Decimal(policy.target_profit_minor)
    )
    if Decimal(profit) < minimum_profit:
        raise PricingInputError("promotion or rounding would violate the target profit")
    pct = (Decimal(profit) / Decimal(supplier_minor)) if supplier_minor else Decimal("0")
    return PriceBreakdown(
        supplier_cost_minor=supplier_minor,
        base_price_minor=base_price,
        requested_promo_discount_minor=requested_discount,
        applied_promo_discount_minor=applied_discount,
        final_price_minor=final_price,
        marketing_minor=marketing,
        payback_minor=payback,
        payment_fee_minor=payment_fee,
        operating_fee_minor=policy.operating_fee_minor,
        profit_minor=profit,
        profit_pct_on_cost=pct,
        currency=policy.sell_currency,
    )


__all__ = ["PriceBreakdown", "PricingInputError", "PricingPolicy", "calculate_price"]
