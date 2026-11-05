"""Parse the NWS Daily Climate Report (CLI) text product for the day's maximum temperature."""

import re
from dataclasses import dataclass
from datetime import date, datetime

MONTHS = {m: i for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"], start=1)}

SUMMARY = re.compile(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})")
VALID_AS_OF = re.compile(r"VALID\s+(?:TODAY\s+)?AS OF\s+(\d{3,4})\s*(AM|PM)?", re.I)
MAXIMUM = re.compile(r"^\s*MAXIMUM\s+(-?\d+|MM?)R?\b(?:\s+(\d{3,4}\s*[AP]M|MM?))?")
MINIMUM = re.compile(r"^\s*MINIMUM\s+(-?\d+|MM?)R?\b(?:\s+(\d{3,4}\s*[AP]M|MM?))?")
WMO_HEADER = re.compile(r"(?m)^(?=[A-Z]{4}\d{2} [A-Z]{4} \d{6})")


@dataclass(frozen=True)
class CliReport:
    location: str | None  # e.g. "MDW" from the CLIMDW line
    climate_date: date
    max_f: int | None  # None when reported missing
    max_time: str | None  # local standard time, as printed, e.g. "238 PM"
    final: bool  # False for same-day reports ("VALID TODAY AS OF ...")
    text: str
    min_f: int | None = None
    min_time: str | None = None

    def value(self, kind: str) -> int | None:
        return self.max_f if kind == "high" else self.min_f


class CliParseError(ValueError):
    pass


def parse_cli(text: str) -> CliReport:
    upper = text.upper()
    m = SUMMARY.search(upper)
    if not m:
        raise CliParseError("no 'CLIMATE SUMMARY FOR <date>' line")
    month = MONTHS.get(m.group(1))
    if not month:
        raise CliParseError(f"unknown month {m.group(1)!r}")
    day = date(int(m.group(3)), month, int(m.group(2)))

    loc = re.search(r"(?m)^CLI([A-Z0-9]{3})\s*$", upper)
    found: dict[str, tuple[int | None, str | None]] = {}
    in_temperature = False
    for line in upper.splitlines():
        if line.strip().startswith("TEMPERATURE (F)"):
            in_temperature = True
            continue
        if in_temperature:
            for name, pattern in (("max", MAXIMUM), ("min", MINIMUM)):
                mm = pattern.match(line)
                if mm and name not in found:  # the first MAXIMUM/MINIMUM pair is the day's
                    value, t = mm.group(1), mm.group(2)
                    found[name] = (None if value.startswith("M") else int(value),
                                   None if not t or t.startswith("M") else " ".join(t.split()))
            if len(found) == 2 or line.strip().startswith(("PRECIPITATION", "SNOWFALL")):
                break
    if "max" not in found:
        raise CliParseError("no MAXIMUM line in the TEMPERATURE section")
    max_f, max_time = found["max"]
    min_f, min_time = found.get("min", (None, None))
    return CliReport(
        location=loc.group(1) if loc else None,
        climate_date=day,
        max_f=max_f,
        max_time=max_time,
        final=VALID_AS_OF.search(upper) is None,
        text=text,
        min_f=min_f,
        min_time=min_time,
    )


def split_products(text: str) -> list[str]:
    """Split several concatenated products (e.g. an archive download) at each WMO header line."""
    parts = [p.strip("\x01\x03\r\n ") for p in WMO_HEADER.split(text)]
    return [p for p in parts if "CLIMATE SUMMARY FOR" in p.upper()]


def best_report(reports: list[CliReport], day: date, kind: str = "high") -> CliReport | None:
    """The report to settle on: final reports beat same-day ones; later issues (corrections) win."""
    same_day = [r for r in reports if r.climate_date == day and r.value(kind) is not None]
    finals = [r for r in same_day if r.final]
    pool = finals or same_day
    return pool[-1] if pool else None
