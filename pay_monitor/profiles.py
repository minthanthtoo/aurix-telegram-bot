"""Versioned pay-app selectors and shared receipt locations.

Selectors are semantic hints, not a promise that a future app version keeps the
same UI. The runner records the bounds it actually resolved on the device.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PayAppProfile:
    key: str
    display_name: str
    package: str
    tested_version: str
    history_labels: tuple[str, ...]
    history_resource_suffixes: tuple[str, ...]
    history_row_resource_suffixes: tuple[str, ...]
    detail_resource_suffixes: tuple[str, ...]
    receipt_dirs: tuple[str, ...]
    carousel: bool = False
    carousel_max_pages: int = 1
    opaque_webview: bool = False
    history_wait_seconds: float = 1.5
    history_coordinate: tuple[float, float] | None = None
    launch_wait_seconds: float = 1.5
    post_login_wait_seconds: float = 2.0
    transaction_coordinate: tuple[float, float] | None = None
    tested_window_size: tuple[int, int] | None = None
    login_pin_coordinates: tuple[tuple[str, float, float], ...] = ()
    visual_popup_cross: bool = False


PROFILES = {
    "kpay": PayAppProfile(
        key="kpay",
        display_name="KBZPay",
        package="com.kbzbank.kpaycustomer",
        tested_version="5.8.5 (242)",
        history_labels=("History", "Transaction History", "မှတ်တမ်း", "စာရင်း"),
        history_resource_suffixes=("history", "historyLayout"),
        history_row_resource_suffixes=("tv_trans_type", "tv_trans_time", "tv_money"),
        detail_resource_suffixes=(
            "tv_trans_type",
            "tv_trans_time",
            "tv_money",
            "tv_trans_id",
            "tv_transaction_id",
            "tv_remark",
        ),
        receipt_dirs=("Pictures/KBZ_Customer/",),
        launch_wait_seconds=8.0,
        tested_window_size=(1080, 2340),
        visual_popup_cross=True,
    ),
    "wavepay": PayAppProfile(
        key="wavepay",
        display_name="WavePay",
        package="mm.com.wavemoney.wavepay",
        tested_version="2.6.1 (1470)",
        history_labels=("History", "Transaction History", "မှတ်တမ်း", "စာရင်း"),
        history_resource_suffixes=("history",),
        history_row_resource_suffixes=(),
        detail_resource_suffixes=(),
        receipt_dirs=("Pictures/WavePay/",),
        opaque_webview=True,
        history_wait_seconds=6.0,
        history_coordinate=(0.861111, 0.256410),
        launch_wait_seconds=4.0,
        transaction_coordinate=(0.5, 0.401709),
        tested_window_size=(1080, 2340),
    ),
    "ayapay": PayAppProfile(
        key="ayapay",
        display_name="AYA Pay",
        package="com.ayaplus.subscriber",
        tested_version="3.3.17 (153)",
        history_labels=("History", "Transaction History", "မှတ်တမ်း"),
        history_resource_suffixes=("miniAppCore4",),
        history_row_resource_suffixes=("transaction", "history"),
        detail_resource_suffixes=("transaction", "reference", "amount", "date", "status"),
        receipt_dirs=("DCIM/AYA Pay/",),
    ),
    "uabpay": PayAppProfile(
        key="uabpay",
        display_name="uabpay",
        package="com.uab.uabbankpay",
        tested_version="3.2.5 (145)",
        history_labels=("History", "Payment History", "Transaction History", "မှတ်တမ်း"),
        history_resource_suffixes=("history",),
        history_row_resource_suffixes=("transaction", "history", "amount"),
        detail_resource_suffixes=("transaction", "reference", "amount", "date", "status"),
        receipt_dirs=("Pictures/Screenshots/", "Download/"),
        carousel=False,
        carousel_max_pages=1,
        launch_wait_seconds=7.0,
        post_login_wait_seconds=6.0,
    ),
    "cbpay": PayAppProfile(
        key="cbpay",
        display_name="CB Pay",
        package="com.cbbank.cbmbanking",
        tested_version="1.34.1 (76)",
        history_labels=("History", "Transaction History", "eStatement", "မှတ်တမ်း"),
        history_resource_suffixes=("layout_cb_account_history",),
        history_row_resource_suffixes=("text_view_amount",),
        detail_resource_suffixes=(
            "text_view_transaction_date",
            "text_view_amount",
            "text_view_transaction_id",
            "text_view_transaction_type",
        ),
        receipt_dirs=("Pictures/CBPay Transaction/",),
        carousel=False,
        carousel_max_pages=16,
        launch_wait_seconds=6.0,
        post_login_wait_seconds=6.0,
        tested_window_size=(1080, 2340),
        login_pin_coordinates=(
            ("1", 0.222222, 0.5),
            ("2", 0.5, 0.5),
            ("3", 0.777778, 0.5),
            ("4", 0.222222, 0.593162),
            ("5", 0.5, 0.593162),
            ("6", 0.777778, 0.593162),
            ("7", 0.222222, 0.68547),
            ("8", 0.5, 0.68547),
            ("9", 0.777778, 0.68547),
            ("0", 0.5, 0.77735),
        ),
        visual_popup_cross=True,
    ),
}
