"""Read the Central Bank of Myanmar's published USD/MMK market trading rate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from urllib.request import Request, urlopen


SOURCE_ID = "cbm-market-trading-usd-mmk"
SOURCE_LABEL = "Central Bank of Myanmar market trading rate"
SOURCE_URL = "https://forex.cbm.gov.mm/index.php/fxrate"
MMT = timezone(timedelta(hours=6, minutes=30))
MAX_PAGE_BYTES = 1_000_000
TIMEOUT_SECONDS = 10


class FXRateUnavailable(ValueError):
    """The official market rate could not be read or validated."""


@dataclass(frozen=True)
class CBMMarketRate:
    rate: Decimal
    market_date: date
    observed_at: datetime
    fetched_at: datetime
    source_id: str = SOURCE_ID
    source_url: str = SOURCE_URL


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.page_text: list[str] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data: str) -> None:
        self.page_text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def _published_date(text: str) -> date:
    match = re.search(
        r"Foreign\s+Exchange\s+Rates\s+as\s+of\s+(\d{1,2})\s*"
        r"(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise FXRateUnavailable("CBM market-rate publication date is missing")
    try:
        return date.fromisoformat(
            f"{int(match.group(3)):04d}-{datetime.strptime(match.group(2), '%B').month:02d}-{int(match.group(1)):02d}"
        )
    except ValueError as exc:
        raise FXRateUnavailable("CBM market-rate publication date is invalid") from exc


def parse_market_rate(page: str | bytes, *, fetched_at: datetime | None = None) -> CBMMarketRate:
    """Read the USD row's Market Trading Rate, never the separate reference rate."""

    if isinstance(page, bytes):
        try:
            page = page.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FXRateUnavailable("CBM market-rate page is not UTF-8") from exc
    if not isinstance(page, str) or not page or len(page.encode("utf-8")) > MAX_PAGE_BYTES:
        raise FXRateUnavailable("CBM market-rate page is empty or too large")
    parser = _PageParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception as exc:
        raise FXRateUnavailable("CBM market-rate page could not be parsed") from exc

    market_column = None
    usd_text = None
    for index, row in enumerate(parser.rows):
        normalized = [" ".join(cell.lower().split()) for cell in row]
        if "market trading rate" in normalized:
            market_column = normalized.index("market trading rate")
            for data_row in parser.rows[index + 1:]:
                cells = [" ".join(cell.lower().split()) for cell in data_row]
                if "usd" in cells or any("united state dollar" in cell for cell in cells):
                    # CBM's USD data row has an unlabeled unit cell absent from
                    # its header, shifting both rate columns by one.
                    column_offset = len(data_row) - len(normalized)
                    rate_column = market_column + column_offset
                    if column_offset not in {0, 1} or rate_column >= len(data_row):
                        break
                    usd_text = data_row[rate_column]
                    break
            break
    if market_column is None or usd_text is None:
        raise FXRateUnavailable("CBM USD market trading rate is missing")
    try:
        rate = Decimal(usd_text.replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise FXRateUnavailable("CBM USD market trading rate is invalid") from exc
    if not rate.is_finite() or rate < Decimal("100") or rate > Decimal("100000"):
        raise FXRateUnavailable("CBM USD market trading rate is outside the supported range")

    published = _published_date(" ".join(parser.page_text))
    now = fetched_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise FXRateUnavailable("FX fetch time must include a timezone")
    now = now.astimezone(timezone.utc)
    observed_at = datetime.combine(published, time.min, tzinfo=MMT).astimezone(timezone.utc)
    if observed_at > now:
        raise FXRateUnavailable("CBM market-rate publication date is in the future")
    return CBMMarketRate(rate, published, observed_at, now)


def fetch_market_rate(*, now: datetime | None = None) -> CBMMarketRate:
    """Fetch the fixed official CBM rate page with bounded HTTPS-only access."""

    request = Request(
        SOURCE_URL,
        headers={"Accept": "text/html", "User-Agent": "AuriX-Game-Store/1.0"},
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            if response.geturl() != SOURCE_URL:
                raise FXRateUnavailable("CBM market-rate redirect is not allowed")
            content_type = response.headers.get_content_type().lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise FXRateUnavailable("CBM market-rate response is not HTML")
            body = response.read(MAX_PAGE_BYTES + 1)
    except FXRateUnavailable:
        raise
    except Exception as exc:
        raise FXRateUnavailable("CBM market-rate source is unavailable") from exc
    if len(body) > MAX_PAGE_BYTES:
        raise FXRateUnavailable("CBM market-rate response is too large")
    return parse_market_rate(body, fetched_at=now)


__all__ = [
    "CBMMarketRate", "FXRateUnavailable", "SOURCE_ID", "SOURCE_LABEL",
    "SOURCE_URL", "fetch_market_rate", "parse_market_rate",
]
