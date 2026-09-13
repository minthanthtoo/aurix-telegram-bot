"""Safe deterministic UI discovery, capture, and replay primitives."""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .adb import AdbClient, AdbError, ForegroundInterrupted
from .profiles import PayAppProfile
from .store import ObservationStore, hash_reference

try:
    import cv2
    import numpy as np
except ImportError:  # Visual close detection is an optional fast path.
    cv2 = None
    np = None


UTC = timezone.utc
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
AMOUNT_RE = re.compile(r"(?i)(?:MMK\s*)?[+-]?\s*\d[\d,.]*\s*(?:MMK|Ks)?")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?95|0)?9\d{7,10}(?!\d)")
REFERENCE_RE = re.compile(r"(?i)(?=.*\d)[A-Z0-9_-]{8,}")

SAFE_UI_LABELS = {
    "history",
    "transaction history",
    "payment history",
    "incoming",
    "all",
    "cancel",
    "not now",
    "no thanks",
    "login",
    "log in",
    "unlock",
    "i understand",
}
DANGEROUS_LABELS = {
    "send",
    "transfer",
    "pay",
    "confirm",
    "refund",
    "cash out",
    "withdraw",
    "request payment",
    "scan",
}
AUTH_CONTEXT = ("pin", "log in", "login", "unlock", "biometric", "fingerprint")
SAFE_OVERLAYS = ("cancel", "not now", "no thanks", "i understand")


@dataclass(frozen=True)
class UiNode:
    text: str
    resource_id: str
    content_desc: str
    bounds: tuple[int, int, int, int]
    clickable: bool

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


def parse_nodes(xml_text: str) -> list[UiNode]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise AdbError("Android accessibility hierarchy was not valid XML") from exc
    nodes: list[UiNode] = []
    for element in root.iter("node"):
        match = BOUNDS_RE.fullmatch(element.attrib.get("bounds", ""))
        if not match:
            continue
        nodes.append(
            UiNode(
                text=element.attrib.get("text", "").strip(),
                resource_id=element.attrib.get("resource-id", "").strip(),
                content_desc=element.attrib.get("content-desc", "").strip(),
                bounds=tuple(int(value) for value in match.groups()),
                clickable=element.attrib.get("clickable") == "true",
            )
        )
    return nodes


def redact_text(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        return ""
    folded = normalized.casefold()
    if folded in SAFE_UI_LABELS or folded in DANGEROUS_LABELS:
        return folded
    if PHONE_RE.search(normalized):
        return "<phone>"
    if AMOUNT_RE.fullmatch(normalized):
        return "<amount>"
    if REFERENCE_RE.fullmatch(normalized):
        return "<reference>"
    return "<text>"


def structural_sample(nodes: list[UiNode]) -> list[dict[str, object]]:
    sample = []
    for node in nodes:
        if not (node.text or node.content_desc or node.resource_id):
            continue
        sample.append(
            {
                "text": redact_text(node.text),
                "content_desc": redact_text(node.content_desc),
                "resource_suffix": node.resource_id.rsplit("/", 1)[-1],
                "bounds": list(node.bounds),
                "clickable": node.clickable,
            }
        )
    return sample


def is_dangerous_label(value: str) -> bool:
    folded = " ".join(value.casefold().split())
    return any(
        folded == danger or folded.startswith(f"{danger} ") or folded.endswith(f" {danger}")
        for danger in DANGEROUS_LABELS
    )


class FlowRecorder:
    def __init__(
        self,
        adb: AdbClient,
        profile: PayAppProfile,
        output_dir: Path,
        history_store: ObservationStore | None = None,
        history_since: str | None = None,
        after_reference_hash: str | None = None,
        target_reference_hash: str | None = None,
        max_history_items: int = 20,
    ):
        self.adb = adb
        self.profile = profile
        self.output_dir = output_dir / profile.key
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.output_dir / "screen_flow.jsonl"
        self.history_store = history_store
        self.history_since = history_since
        self.after_reference_hash = after_reference_hash
        self.target_reference_hash = target_reference_hash
        self.max_history_items = max_history_items

    def _assert_foreground(self) -> None:
        current_package = (
            self.adb.current_package() if hasattr(self.adb, "current_package") else None
        )
        allowed_system_surfaces = {
            "android",
            "com.android.systemui",
            "com.google.android.permissioncontroller",
        }
        if current_package and current_package not in {
            self.profile.package,
            *allowed_system_surfaces,
        }:
            raise ForegroundInterrupted(
                f"foreground changed from {self.profile.key} to another app"
            )

    def _tap(self, x: int, y: int) -> None:
        self._assert_foreground()
        self.adb.tap(x, y)

    def _swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._assert_foreground()
        self.adb.swipe(x1, y1, x2, y2)

    def _keyevent(self, key: str) -> None:
        self._assert_foreground()
        self.adb.keyevent(key)

    def history_item_fingerprint(self, nodes: list[UiNode], anchor: UiNode) -> str:
        center_y = anchor.center[1]
        fields = sorted(
            f"{node.resource_id.rsplit('/', 1)[-1]}:{node.text}:{node.content_desc}"
            for node in nodes
            if (node.text or node.content_desc)
            and abs(node.center[1] - center_y) <= 140
        )
        return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()

    def should_open_history_item(
        self,
        nodes: list[UiNode],
        anchor: UiNode,
        *,
        account_hash: str | None = None,
    ) -> tuple[str, bool, str | None, str | None]:
        fingerprint = self.history_item_fingerprint(nodes, anchor)
        if self.history_store is None:
            return fingerprint, True, None, None
        remembered = self.history_store.remember_history_item(
            provider=self.profile.key,
            account_hash=account_hash,
            item_fingerprint=fingerprint,
        )
        return (
            fingerprint,
            remembered.is_new or not remembered.detail_was_checked,
            remembered.provider_reference_hash,
            remembered.transaction_time,
        )

    def mark_history_detail_checked(
        self,
        fingerprint: str,
        nodes: list[UiNode],
        *,
        account_hash: str | None = None,
    ) -> tuple[str | None, str | None]:
        if self.history_store is None:
            return None, None
        reference = next(
            (
                node.text or node.content_desc
                for node in nodes
                if any(marker in node.resource_id.casefold() for marker in ("trans_id", "transaction_id", "reference"))
                and (node.text or node.content_desc)
            ),
            None,
        )
        reference_digest = hash_reference(reference)
        transaction_time = _extract_ui_transaction_time(nodes)
        self.history_store.remember_history_item(
            provider=self.profile.key,
            account_hash=account_hash,
            item_fingerprint=fingerprint,
            provider_reference_hash=reference_digest,
            transaction_time=transaction_time,
            detail_checked=True,
        )
        return reference_digest, transaction_time

    def _record(self, event: str, **fields: object) -> None:
        payload = {
            "at": datetime.now(UTC).isoformat(),
            "provider": self.profile.key,
            "app_version": self.adb.version_name(self.profile.package),
            "event": event,
            **fields,
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def observe(self, sample_name: str | None = None) -> tuple[str, list[UiNode]]:
        self._assert_foreground()
        last_error: AdbError | None = None
        for attempt in range(3):
            xml_text = self.adb.ui_xml()
            try:
                nodes = parse_nodes(xml_text)
                break
            except AdbError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.0)
        else:
            raise last_error or AdbError("Android accessibility hierarchy unavailable")
        digest = hashlib.sha256(xml_text.encode()).hexdigest()
        if sample_name:
            destination = self.output_dir / f"{sample_name}.json"
            destination.write_text(
                json.dumps(
                    {
                        "provider": self.profile.key,
                        "app_version": self.adb.version_name(self.profile.package),
                        "screen_sha256": digest,
                        "nodes": structural_sample(nodes),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        return digest, nodes

    @staticmethod
    def _label(node: UiNode) -> str:
        return (node.text or node.content_desc).strip().casefold()

    def find_label(self, labels: tuple[str, ...]) -> UiNode | None:
        _, nodes = self.observe()
        wanted = {label.casefold() for label in labels}
        return next((node for node in nodes if self._label(node) in wanted), None)

    def find_label_prefix(self, labels: tuple[str, ...]) -> UiNode | None:
        _, nodes = self.observe()
        wanted = tuple(label.casefold() for label in labels)
        candidates = [
            node
            for node in nodes
            if any(
                self._label(node) == label or self._label(node).startswith(f"{label} ")
                for label in wanted
            )
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda node: (
                not node.clickable,
                self._label(node) not in wanted,
                (node.bounds[2] - node.bounds[0]) * (node.bounds[3] - node.bounds[1]),
            ),
        )

    def normalize_uab_launch(self) -> None:
        for _ in range(3):
            registration = self.find_label(("Registration",))
            if registration is None:
                return
            self.back("leave_persisted_registration_route", wait_seconds=1.5)

    def normalize_cb_launch(self) -> None:
        close = self.find_resource_suffix(("btn_close",))
        if close is not None:
            self.tap_node(close, "dismiss_cbpay_anniversary_popup")
            time.sleep(1.2)
        for _ in range(3):
            back = self.find_resource_suffix(("lhs_back_icon",))
            if back is None:
                return
            self.tap_node(back, "leave_cbpay_persisted_detail_route")
            time.sleep(1.2)

    def find_resource_suffix(self, suffixes: tuple[str, ...]) -> UiNode | None:
        _, nodes = self.observe()
        folded = tuple(suffix.casefold() for suffix in suffixes)
        return next(
            (
                node
                for node in nodes
                if any(node.resource_id.casefold().endswith(suffix) for suffix in folded)
            ),
            None,
        )

    def find_history(self, max_scrolls: int = 4) -> UiNode | None:
        wanted = {label.casefold() for label in self.profile.history_labels}
        suffixes = tuple(value.casefold() for value in self.profile.history_resource_suffixes)
        width, height = self.adb.window_size()
        for attempt in range(max_scrolls + 1):
            _, nodes = self.observe()
            found = next((node for node in nodes if self._label(node) in wanted), None)
            if found is None:
                found = next(
                    (
                        node
                        for node in nodes
                        if any(node.resource_id.casefold().endswith(suffix) for suffix in suffixes)
                    ),
                    None,
                )
            if found is not None:
                return found
            if attempt == max_scrolls:
                break
            x = width // 2
            start_y, end_y = int(height * 0.76), int(height * 0.36)
            self._swipe(x, start_y, x, end_y)
            self._record(
                "swipe",
                purpose="find_history",
                attempt=attempt,
                absolute=[x, start_y, x, end_y],
                relative=[0.5, 0.76, 0.5, 0.36],
            )
            time.sleep(0.7)
        return None

    def tap_node(self, node: UiNode, purpose: str) -> None:
        label = self._label(node)
        if is_dangerous_label(label):
            raise AdbError(f"Refusing dangerous control: {label}")
        x, y = node.center
        width, height = self.adb.window_size()
        self._tap(x, y)
        self._record(
            "tap",
            purpose=purpose,
            selector={
                "label": redact_text(node.text or node.content_desc),
                "resource_suffix": node.resource_id.rsplit("/", 1)[-1],
            },
            absolute=[x, y],
            relative=[round(x / width, 6), round(y / height, 6)],
        )
        time.sleep(0.8)

    def back(self, purpose: str, wait_seconds: float = 1.2) -> None:
        self._keyevent("BACK")
        self._record("keyevent", purpose=purpose, key="BACK")
        time.sleep(wait_seconds)

    def dismiss_biometric(self) -> bool:
        _, nodes = self.observe()
        screen = " ".join(f"{node.text} {node.content_desc}" for node in nodes).casefold()
        if not any(word in screen for word in ("biometric", "fingerprint", "face id")):
            return False
        self._keyevent("BACK")
        self._record("keyevent", purpose="dismiss_biometric", key="BACK")
        time.sleep(0.5)
        return True

    def dismiss_safe_overlay(self) -> bool:
        node = self.find_label(SAFE_OVERLAYS)
        if node is None:
            return False
        self.tap_node(node, "dismiss_launch_overlay")
        return True

    def dismiss_anr_wait(self) -> bool:
        wait = self.find_resource_suffix(("aerr_wait",))
        if wait is None:
            return False
        self.tap_node(wait, "wait_for_unresponsive_app")
        time.sleep(5.0)
        return True

    def recover_login_timeout(self, pin: str) -> bool:
        _, nodes = self.observe()
        context = " ".join(f"{node.text} {node.content_desc}" for node in nodes).casefold()
        if "timeout" not in context or not any(
            marker in context for marker in ("login again", "time limit", "expired")
        ):
            return False
        okay = next((node for node in nodes if self._label(node) == "ok"), None)
        if okay is None:
            return False
        self.tap_node(okay, "dismiss_login_timeout")
        self.enter_login_pin(pin, login_flow=True)
        time.sleep(self.profile.post_login_wait_seconds)
        return True

    def dismiss_visual_popup_cross(self) -> bool:
        """Close only a high-confidence white circular X in the top-right popup zone."""
        self._assert_foreground()
        if not self.profile.visual_popup_cross or cv2 is None or np is None:
            return False
        expected_version = self.profile.tested_version.split(" ", 1)[0]
        width, height = self.adb.window_size()
        if self.adb.version_name(self.profile.package) != expected_version:
            return False
        if self.profile.tested_window_size != (width, height):
            return False
        encoded = np.frombuffer(self.adb.screencap_png(), dtype=np.uint8)
        image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
        if image is None or image.shape != (height, width):
            return False
        x0, y0 = int(width * 0.78), int(height * 0.045)
        y1 = int(height * 0.24)
        roi = image[y0:y1, x0:width]
        blurred = cv2.GaussianBlur(roi, (5, 5), 1.2)
        circles = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=int(width * 0.05),
            param1=120,
            param2=24,
            minRadius=int(width * 0.018),
            maxRadius=int(width * 0.065),
        )
        if circles is None:
            return False
        candidates: list[tuple[int, int, int]] = []
        for relative_x, relative_y, radius in circles[0]:
            x, y, radius = int(relative_x) + x0, int(relative_y) + y0, int(radius)
            yy, xx = np.ogrid[-radius:radius + 1, -radius:radius + 1]
            disk = xx * xx + yy * yy <= radius * radius
            patch = image[y - radius:y + radius + 1, x - radius:x + radius + 1]
            if patch.shape != disk.shape:
                continue
            white_fraction = float(np.mean(patch[disk] >= 210))
            center = patch[radius // 2:radius + radius // 2 + 1,
                           radius // 2:radius + radius // 2 + 1]
            dark_center_fraction = float(np.mean(center <= 150)) if center.size else 0.0
            diagonal_radius = max(4, int(radius * 0.5))
            offsets = np.arange(-diagonal_radius, diagonal_radius + 1)
            diagonal_one = image[y + offsets, x + offsets]
            diagonal_two = image[y + offsets, x - offsets]
            dark_diagonal_one = float(np.mean(diagonal_one <= 150))
            dark_diagonal_two = float(np.mean(diagonal_two <= 150))
            if (
                white_fraction >= 0.45
                and dark_center_fraction >= 0.08
                and dark_diagonal_one >= 0.35
                and dark_diagonal_two >= 0.35
            ):
                candidates.append((x, y, radius))
        if not candidates:
            return False
        x, y, _ = max(candidates, key=lambda item: item[0])
        self._tap(x, y)
        self._record(
            "tap",
            purpose="dismiss_visual_top_right_popup_cross",
            selector={"label": "close", "resource_suffix": "visual_white_circle_cross"},
            absolute=[x, y],
            relative=[round(x / width, 6), round(y / height, 6)],
        )
        time.sleep(0.8)
        return True

    def enter_login_pin(self, pin: str, *, login_flow: bool = False) -> bool:
        if not pin.isdigit() or not 4 <= len(pin) <= 8:
            raise AdbError("PIN must contain 4 to 8 digits")
        _, nodes = self.observe()
        context = " ".join(f"{node.text} {node.content_desc}" for node in nodes).casefold()
        if any(
            marker in context
            for marker in ("confirm payment", "transaction pin", "transfer pin", "payment pin")
        ):
            raise AdbError("Refusing PIN entry on a transaction-capable screen")
        keypad = {}
        for node in nodes:
            label = (node.text or node.content_desc).strip()
            if label in "0123456789" and len(label) == 1:
                keypad.setdefault(label, node)
        recognized_context = any(marker in context for marker in AUTH_CONTEXT)
        complete_keypad = set(keypad) == set("0123456789")
        if not recognized_context and not (login_flow and complete_keypad):
            raise AdbError("Refusing PIN entry outside a recognized login/unlock screen")
        if not complete_keypad and self.profile.login_pin_coordinates:
            expected_version = self.profile.tested_version.split(" ", 1)[0]
            actual_version = self.adb.version_name(self.profile.package)
            actual_size = self.adb.window_size()
            if not recognized_context or actual_version != expected_version:
                raise AdbError("Refusing coordinate PIN entry after login-context/version drift")
            if self.profile.tested_window_size != actual_size:
                raise AdbError("Refusing coordinate PIN entry after screen-geometry drift")
            coordinate_keypad = {
                digit: (int(actual_size[0] * x), int(actual_size[1] * y))
                for digit, x, y in self.profile.login_pin_coordinates
            }
            if set(coordinate_keypad) != set("0123456789"):
                raise AdbError("Coordinate PIN profile is incomplete")
            for digit in pin:
                self._tap(*coordinate_keypad[digit])
            self._record(
                "pin_entry",
                purpose="login_only_coordinate_profile",
                digits=len(pin),
                value="<secret>",
            )
            time.sleep(1.2)
            return True
        if not all(digit in keypad for digit in set(pin)):
            raise AdbError("PIN keypad is not semantically exposed; coordinate calibration required")
        for digit in pin:
            x, y = keypad[digit].center
            self._tap(x, y)
        self._record("pin_entry", purpose="login_only", digits=len(pin), value="<secret>")
        time.sleep(1.2)
        return True

    def enumerate_carousel(self) -> list[str]:
        if not self.profile.carousel:
            return []
        width, height = self.adb.window_size()
        y = int(height * 0.28)
        start_x, end_x = int(width * 0.82), int(width * 0.18)
        fingerprints: list[str] = []
        for page in range(self.profile.carousel_max_pages):
            digest, nodes = self.observe(f"wallet_{page:02d}")
            semantic = "|".join(
                sorted(
                    f"{node.resource_id.rsplit('/', 1)[-1]}:{node.text}:{node.content_desc}"
                    for node in nodes
                    if node.text or node.resource_id
                )
            )
            fingerprint = hashlib.sha256(semantic.encode()).hexdigest()
            if fingerprint in fingerprints:
                break
            fingerprints.append(fingerprint)
            self._swipe(start_x, y, end_x, y)
            self._record(
                "swipe",
                purpose="enumerate_wallet_or_card",
                page=page,
                absolute=[start_x, y, end_x, y],
                relative=[0.82, 0.28, 0.18, 0.28],
            )
            time.sleep(0.8)
        return fingerprints

    def capture_uab_accounts(self) -> dict[str, object]:
        def uab_back(purpose: str) -> None:
            width, height = self.adb.window_size()
            x, y = int(width * 0.063), int(height * 0.079)
            self._tap(x, y)
            self._record(
                "tap",
                purpose=purpose,
                selector={"label": "back", "resource_suffix": "flutter_coordinate"},
                absolute=[x, y],
                relative=[0.063, 0.079],
            )
            time.sleep(2.5)

        def return_to_home() -> bool:
            for _ in range(4):
                if self.find_label(("Wallets",)) and self.find_label(("Cards",)):
                    return True
                uab_back("return_to_uabpay_home")
            return False

        def expanded_wallets() -> tuple[list[UiNode], UiNode | None]:
            wallet_tab = self.find_label(("Wallets",))
            if wallet_tab is None:
                return [], None
            for attempt in range(3):
                self.tap_node(wallet_tab, f"select_wallets_{attempt}")
                time.sleep(2.2)
                _, nodes = self.observe("wallets_home")
                wallet_group = next(
                    (
                        node
                        for node in nodes
                        if node.clickable and self._label(node).startswith("wallets,")
                    ),
                    None,
                )
                if wallet_group is None:
                    continue
                self.tap_node(wallet_group, "expand_wallets")
                time.sleep(1.2)
                _, nodes = self.observe("wallets_expanded")
                phone_wallets = [
                    node
                    for node in nodes
                    if node.clickable
                    and PHONE_RE.search(node.text or node.content_desc)
                ]
                fallback_wallets = [
                    node
                    for node in nodes
                    if node.clickable
                    and node.bounds[0] >= 50
                    and node.bounds[2] <= 1030
                    and node.bounds[1] >= 760
                    and node.bounds[2] - node.bounds[0] >= 700
                    and bool((node.text or node.content_desc).strip())
                ]
                return (
                    phone_wallets or fallback_wallets,
                    wallet_group,
                )
            return [], None

        wallet_tab = self.find_label(("Wallets",))
        if wallet_tab is None:
            return {"status": "blocked", "reason": "uabpay Wallets tab not found"}
        wallets, wallet_group = expanded_wallets()
        if not wallets or wallet_group is None:
            return {"status": "blocked", "reason": "uabpay wallet group not found"}
        wallet_count = 0
        transaction_samples = 0
        new_history_items = 0
        expected_wallets = min(len(wallets), 12)
        while wallet_count < expected_wallets:
            if wallet_count:
                if not return_to_home():
                    break
                self.tap_node(wallet_tab, f"select_wallets_for_{wallet_count}")
                time.sleep(2.2)
                self.tap_node(wallet_group, f"expand_wallets_for_{wallet_count}")
                time.sleep(1.2)
            self.tap_node(wallets[wallet_count], f"open_wallet_{wallet_count}")
            self.observe(f"wallet_{wallet_count:02d}_history_list_sample")
            _, detail_nodes = self.observe()
            incoming = next(
                (
                    node
                    for node in detail_nodes
                    if node.clickable
                    and any(
                        marker in self._label(node)
                        for marker in ("received", "cash in", "ငွေလက်ခံ")
                    )
                ),
                None,
            )
            if incoming is not None:
                wallet_digest = hashlib.sha256(
                    (wallets[wallet_count].text or wallets[wallet_count].content_desc).encode(
                        "utf-8"
                    )
                ).hexdigest()
                fingerprint, should_open, _, _ = self.should_open_history_item(
                    detail_nodes, incoming, account_hash=wallet_digest
                )
                if should_open:
                    self.tap_node(incoming, f"open_wallet_{wallet_count}_incoming_transaction")
                    _, transaction_nodes = self.observe(
                        f"wallet_{wallet_count:02d}_transaction_sample"
                    )
                    self.mark_history_detail_checked(
                        fingerprint, transaction_nodes, account_hash=wallet_digest
                    )
                    transaction_samples += 1
                    new_history_items += 1
                    uab_back("leave_uabpay_transaction")
            uab_back("leave_uabpay_wallet")
            wallet_count += 1
        return_to_home()
        card_tab = self.find_label(("Cards",))
        card_captured = False
        if card_tab is not None:
            card = None
            for attempt in range(3):
                self.tap_node(card_tab, f"select_virtual_cards_{attempt}")
                time.sleep(2.2)
                _, nodes = self.observe("virtual_cards_home")
                card = next(
                    (
                        node
                        for node in nodes
                        if node.clickable and self._label(node).startswith("virtual card,")
                    ),
                    None,
                )
                if card is not None:
                    break
            if card is not None:
                self.tap_node(card, "open_virtual_card")
                self.observe("virtual_card_detail")
                card_captured = True
                uab_back("leave_virtual_card")
        return {
            "status": "captured",
            "wallets": wallet_count,
            "wallet_transaction_samples": transaction_samples,
            "new_history_items": new_history_items,
            "virtual_card": card_captured,
        }

    def capture_cb_accounts(
        self,
        *,
        selected_index: int | None = None,
        selected_hash: str | None = None,
    ) -> dict[str, object]:
        width, height = self.adb.window_size()
        pager = None
        dashboard_nodes: list[UiNode] = []
        requested = selected_index is not None or selected_hash is not None
        for _ in range(1 if requested else 8):
            if not requested:
                self.normalize_cb_launch()
            _, dashboard_nodes = self.observe()
            pager = next(
                (
                    node
                    for node in dashboard_nodes
                    if node.resource_id.endswith("pager_dashboard")
                ),
                None,
            )
            if pager is not None:
                break
            time.sleep(2.0)
        if pager is None:
            return {"status": "blocked", "reason": "CB Pay authenticated dashboard not found"}

        index_path = self.output_dir / "account_index.json"

        def account_from(nodes: list[UiNode]) -> UiNode | None:
            return next(
                (
                    node
                    for node in nodes
                    if node.resource_id.endswith(("tv_cb_account_number", "text_view_card_number"))
                    and (node.text or node.content_desc)
                ),
                None,
            )

        def account_hash(node: UiNode) -> str:
            return hashlib.sha256(
                (node.text or node.content_desc).encode("utf-8")
            ).hexdigest()

        def swipe_card(direction: int, purpose: str, page: int) -> None:
            left, right = int(width * 0.18), int(width * 0.82)
            start_x, end_x = (right, left) if direction > 0 else (left, right)
            y = int(height * 0.3)
            self._swipe(start_x, y, end_x, y)
            self._record(
                "swipe",
                purpose=purpose,
                page=page,
                direction="next" if direction > 0 else "previous",
                absolute=[start_x, y, end_x, y],
                relative=[round(start_x / width, 6), 0.3, round(end_x / width, 6), 0.3],
            )
            time.sleep(1.0)

        def load_index() -> list[dict[str, object]]:
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return []
            if payload.get("app_version") != self.adb.version_name(self.profile.package):
                return []
            if payload.get("window_size") != [width, height]:
                return []
            accounts = payload.get("accounts")
            return accounts if isinstance(accounts, list) else []

        def capture_history(
            page: int,
            visible_nodes: list[UiNode] | None = None,
            account_digest: str | None = None,
        ) -> tuple[int, int, int]:
            history_count = 0
            transaction_count = 0
            new_items = 0
            history = next(
                (
                    node
                    for node in (visible_nodes or [])
                    if node.resource_id.endswith("layout_cb_account_history")
                ),
                None,
            )
            if history is None:
                history = self.find_resource_suffix(("layout_cb_account_history",))
            if history is None:
                return history_count, transaction_count, new_items
            self.tap_node(history, f"open_cbpay_account_{page}_history")
            time.sleep(1.5)
            _, history_nodes = self.observe(f"account_{page:02d}_history_list_sample")
            history_count = 1
            incoming_amounts = [
                node
                for node in history_nodes
                if node.resource_id.endswith("text_view_amount")
                and (node.text or node.content_desc).strip()
                and not (node.text or node.content_desc).strip().startswith("-")
            ][: self.max_history_items]
            for incoming_amount in incoming_amounts:
                (
                    fingerprint,
                    should_open,
                    known_reference,
                    known_time,
                ) = self.should_open_history_item(
                    history_nodes,
                    incoming_amount,
                    account_hash=account_digest,
                )
                if known_reference and known_reference in {
                    self.after_reference_hash,
                    self.target_reference_hash,
                }:
                    break
                if self.history_since and known_time and known_time <= self.history_since:
                    break
                if not should_open:
                    continue
                self.tap_node(incoming_amount, f"open_cbpay_account_{page}_positive_transaction")
                time.sleep(1.2)
                _, detail_nodes = self.observe(f"account_{page:02d}_transaction_sample")
                reference_digest, transaction_time = self.mark_history_detail_checked(
                    fingerprint,
                    detail_nodes,
                    account_hash=account_digest,
                )
                transaction_count += 1
                new_items += 1
                self.back("leave_cbpay_transaction", wait_seconds=1.5)
                if reference_digest and reference_digest in {
                    self.after_reference_hash,
                    self.target_reference_hash,
                }:
                    break
                if self.history_since and transaction_time and transaction_time <= self.history_since:
                    break
                break
            self.back("leave_cbpay_history", wait_seconds=1.8)
            return history_count, transaction_count, new_items

        if requested:
            mapping = load_index()
            if not mapping:
                return {"status": "blocked", "reason": "CB Pay account index requires calibration"}
            if selected_index is not None:
                target = next(
                    (entry for entry in mapping if entry.get("index") == selected_index), None
                )
            else:
                prefix = (selected_hash or "").casefold()
                matches = [
                    entry
                    for entry in mapping
                    if str(entry.get("account_hash", "")).casefold().startswith(prefix)
                ]
                target = matches[0] if len(matches) == 1 else None
            if target is None:
                return {"status": "blocked", "reason": "CB Pay selected account is not unique/indexed"}
            current = account_from(dashboard_nodes)
            if current is None:
                return {"status": "blocked", "reason": "CB Pay current account ID not found"}
            current_digest = account_hash(current)
            current_entry = next(
                (entry for entry in mapping if entry.get("account_hash") == current_digest), None
            )
            if current_entry is None:
                return {"status": "blocked", "reason": "CB Pay account mapping drift detected"}
            target_index = int(target["index"])
            delta = target_index - int(current_entry["index"])
            for step in range(abs(delta)):
                swipe_card(1 if delta > 0 else -1, "select_cbpay_account_direct", step)
            if delta:
                _, nodes = self.observe(f"account_{target_index:02d}_home")
            else:
                nodes = dashboard_nodes
            reached = account_from(nodes)
            if reached is None or account_hash(reached) != target.get("account_hash"):
                return {"status": "blocked", "reason": "CB Pay account mapping verification failed"}
            histories, transactions, new_items = capture_history(
                target_index, nodes, str(target["account_hash"])
            )
            return {
                "status": "captured",
                "selected_account_index": target_index,
                "selected_account_hash": str(target["account_hash"])[:12],
                "swipes": abs(delta),
                "history_lists": histories,
                "positive_transaction_samples": transactions,
                "new_history_items": new_items,
            }

        # Rewind to the first card so learned indices remain stable across relaunches.
        for page in range(self.profile.carousel_max_pages):
            _, before_nodes = self.observe()
            before = account_from(before_nodes)
            if before is None:
                break
            swipe_card(-1, "rewind_cbpay_account_cards", page)
            _, after_nodes = self.observe()
            after = account_from(after_nodes)
            if after is None or account_hash(after) == account_hash(before):
                break

        seen_accounts: set[str] = set()
        account_index: list[dict[str, object]] = []
        account_count = 0
        history_count = 0
        transaction_count = 0
        new_item_count = 0
        for page in range(self.profile.carousel_max_pages):
            _, nodes = self.observe(f"account_{page:02d}_home")
            account = account_from(nodes)
            if account is None:
                break
            fingerprint = account_hash(account)
            if fingerprint in seen_accounts:
                break
            seen_accounts.add(fingerprint)
            account_index.append({"index": page, "account_hash": fingerprint})
            account_count += 1
            histories, transactions, new_items = capture_history(
                page, account_digest=fingerprint
            )
            history_count += histories
            transaction_count += transactions
            new_item_count += new_items
            swipe_card(1, "enumerate_cbpay_account_cards", page)
        index_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "provider": "cbpay",
                    "app_version": self.adb.version_name(self.profile.package),
                    "window_size": [width, height],
                    "updated_at": datetime.now(UTC).isoformat(),
                    "accounts": account_index,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "status": "captured",
            "accounts": account_count,
            "history_lists": history_count,
            "positive_transaction_samples": transaction_count,
            "new_history_items": new_item_count,
            "account_index": [
                {"index": entry["index"], "account_hash": str(entry["account_hash"])[:12]}
                for entry in account_index
            ],
        }

    def probe(
        self,
        pin: str | None = None,
        *,
        account_index: int | None = None,
        account_hash: str | None = None,
    ) -> dict[str, object]:
        selective_cb = self.profile.key == "cbpay" and (
            account_index is not None or account_hash is not None
        )
        self.adb.start_package(self.profile.package, force_stop=not selective_cb)
        time.sleep(1.5 if selective_cb else self.profile.launch_wait_seconds)
        self._record("launch", package=self.profile.package)
        self.dismiss_visual_popup_cross()
        if selective_cb:
            fast_result = self.capture_cb_accounts(
                selected_index=account_index,
                selected_hash=account_hash,
            )
            if fast_result.get("status") == "captured":
                return fast_result
        self.dismiss_anr_wait()
        self.dismiss_biometric()
        self.dismiss_safe_overlay()
        if self.profile.key == "uabpay":
            self.normalize_uab_launch()
        elif self.profile.key == "cbpay":
            self.normalize_cb_launch()
        login = self.find_label_prefix(("Login", "Log in", "ဝင်မည်")) if pin else None
        if login is not None:
            self.tap_node(login, "open_login")
            self.dismiss_biometric()
        if pin and login is not None:
            try:
                self.enter_login_pin(pin, login_flow=True)
                time.sleep(self.profile.post_login_wait_seconds)
                if self.profile.key == "cbpay":
                    self.recover_login_timeout(pin)
            except AdbError as exc:
                self._record("blocked", stage="login", reason=str(exc))
                return {"status": "blocked", "reason": str(exc)}
        if self.profile.key == "cbpay":
            self.normalize_cb_launch()
            return self.capture_cb_accounts(
                selected_index=account_index,
                selected_hash=account_hash,
            )
        if self.profile.key == "uabpay":
            return self.capture_uab_accounts()
        wallets = self.enumerate_carousel()
        history = self.find_history(max_scrolls=0 if self.profile.history_coordinate else 4)
        if self.profile.opaque_webview and self.profile.history_coordinate:
            history = None
        if history is None and self.profile.history_coordinate:
            width, height = self.adb.window_size()
            actual_version = self.adb.version_name(self.profile.package)
            expected_version = self.profile.tested_version.split(" ", 1)[0]
            if actual_version != expected_version:
                return {
                    "status": "blocked",
                    "reason": (
                        "coordinate profile version drift: "
                        f"expected {expected_version}, found {actual_version}"
                    ),
                }
            if self.profile.tested_window_size != (width, height):
                return {
                    "status": "blocked",
                    "reason": (
                        "coordinate profile geometry drift: "
                        f"expected {self.profile.tested_window_size}, found {(width, height)}"
                    ),
                }
            x = int(width * self.profile.history_coordinate[0])
            y = int(height * self.profile.history_coordinate[1])
            self._tap(x, y)
            self._record(
                "tap",
                purpose="open_history_coordinate_fallback",
                selector={"label": "စာရင်း", "resource_suffix": "opaque_webview"},
                absolute=[x, y],
                relative=list(self.profile.history_coordinate),
            )
            time.sleep(self.profile.history_wait_seconds)
            self.observe("history_list_sample")
            transaction_captured = False
            list_fingerprint = ""
            should_open = True
            if self.history_store is not None:
                list_fingerprint = _image_region_fingerprint(
                    self.adb.screencap_png(), (0.03, 0.32, 0.97, 0.49)
                )
                remembered = self.history_store.remember_history_item(
                    provider=self.profile.key,
                    account_hash=None,
                    item_fingerprint=list_fingerprint,
                )
                should_open = remembered.is_new or not remembered.detail_was_checked
            if self.profile.transaction_coordinate and should_open:
                tx = self.profile.transaction_coordinate
                tx_x, tx_y = int(width * tx[0]), int(height * tx[1])
                self._tap(tx_x, tx_y)
                self._record(
                    "tap",
                    purpose="open_incoming_transaction_coordinate_fallback",
                    selector={"label": "incoming_row", "resource_suffix": "opaque_webview"},
                    absolute=[tx_x, tx_y],
                    relative=list(tx),
                )
                time.sleep(2.5)
                self.observe("transaction_sample")
                if self.history_store is not None:
                    self.history_store.remember_history_item(
                        provider=self.profile.key,
                        account_hash=None,
                        item_fingerprint=list_fingerprint,
                        detail_checked=True,
                    )
                transaction_captured = True
            return {
                "status": "captured",
                "wallets": len(wallets),
                "history": True,
                "transaction": transaction_captured,
                "new_history_items": int(transaction_captured),
                "opaque_webview": True,
            }
        if history is None and self.dismiss_anr_wait():
            history = self.find_history(max_scrolls=4)
        if history is None:
            self.observe("current_screen")
            return {"status": "blocked", "reason": "history control not found", "wallets": len(wallets)}
        self.tap_node(history, "open_history")
        if self.profile.history_wait_seconds > 0.8:
            time.sleep(self.profile.history_wait_seconds - 0.8)
        self.observe("history_list_sample")
        _, list_nodes = self.observe()
        row = next(
            (
                node
                for node in list_nodes
                if any(
                    node.resource_id.casefold().endswith(suffix.casefold())
                    for suffix in self.profile.history_row_resource_suffixes
                )
            ),
            None,
        )
        should_open = False
        fingerprint = ""
        if row is not None:
            fingerprint, should_open, _, _ = self.should_open_history_item(list_nodes, row)
        if row is not None and should_open:
            self.tap_node(row, "open_first_history_record")
            _, detail_nodes = self.observe("transaction_sample")
            self.mark_history_detail_checked(fingerprint, detail_nodes)
            self._keyevent("BACK")
            self._record("keyevent", purpose="leave_transaction_detail", key="BACK")
        return {
            "status": "captured",
            "wallets": len(wallets),
            "history": True,
            "transaction": row is not None and should_open,
            "new_history_items": int(row is not None and should_open),
            "opaque_webview": self.profile.opaque_webview,
        }


def _extract_ui_transaction_time(nodes: list[UiNode]) -> str | None:
    candidates = [
        node.text or node.content_desc
        for node in nodes
        if any(marker in node.resource_id.casefold() for marker in ("date", "time"))
        and (node.text or node.content_desc)
    ]
    patterns = (
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %I:%M %p",
    )
    for value in candidates:
        normalized = " ".join(value.split())
        for pattern in patterns:
            try:
                parsed = datetime.strptime(normalized, pattern)
            except ValueError:
                continue
            return parsed.replace(tzinfo=ZoneInfo("Asia/Yangon")).astimezone(UTC).isoformat()
    return None


def _image_region_fingerprint(
    png: bytes, region: tuple[float, float, float, float]
) -> str:
    if cv2 is None or np is None:
        return hashlib.sha256(png).hexdigest()
    encoded = np.frombuffer(png, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return hashlib.sha256(png).hexdigest()
    height, width = image.shape
    x0, y0, x1, y1 = region
    crop = image[
        int(height * y0):int(height * y1),
        int(width * x0):int(width * x1),
    ]
    if crop.size == 0:
        return hashlib.sha256(png).hexdigest()
    normalized = cv2.resize(crop, (48, 16), interpolation=cv2.INTER_AREA)
    quantized = (normalized // 16).astype(np.uint8)
    return hashlib.sha256(quantized.tobytes()).hexdigest()


def capture_passive_sources(adb: AdbClient, output_dir: Path, profiles: list[PayAppProfile]) -> None:
    packages = {profile.package: profile.key for profile in profiles}
    notification_lines = []
    current_package = None
    for line in adb.notification_dump().splitlines():
        matched = next((package for package in packages if package in line), None)
        if matched:
            current_package = matched
            notification_lines.append({"provider": packages[matched], "event": "notification_present"})
        elif current_package and any(token in line for token in ("android.title=", "android.text=", "postTime=")):
            key = line.split("=", 1)[0].strip().rsplit(" ", 1)[-1]
            notification_lines.append(
                {"provider": packages[current_package], "field": key, "value": "<redacted>"}
            )
    media = []
    for line in adb.media_rows().splitlines():
        for profile in profiles:
            if any(f"relative_path={path}" in line for path in profile.receipt_dirs):
                media.append({"provider": profile.key, "receipt_dir": next(
                    path for path in profile.receipt_dirs if f"relative_path={path}" in line
                ), "file": "<redacted>", "metadata_present": True})
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "noti.json").write_text(json.dumps(notification_lines, indent=2) + "\n")
    (output_dir / "receipt_dir.json").write_text(json.dumps(media, indent=2) + "\n")
