#!/usr/bin/env python3
"""Build a rolling downtown Greenville activities calendar for ages 0-3."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUTPUT = DOCS / "calendar.ics"
STATUS = DOCS / "status.json"
TZ = ZoneInfo("America/New_York")
WINDOW_DAYS = 45
USER_AGENT = "GreenvilleKidCalendar/1.0 (+https://github.com/o-connor/greenville-kid-calendar)"

LIBRARY_SERIES = {
    "bouncing-babies": "",
    "baby-crafts": "",
    "stay-and-play": "",
    "toddler-tales": "[18m+] ",
    "musical-jamboree": "[18m+] ",
    "science-station-jr": "[2+] ",
    "preschool-picassos": "[2+] ",
}

TCMU_RULES = (
    ("tcmu tots", "[2+] "),
    ("story time & more", ""),
    ("storytime & more", ""),
    ("music with", ""),
    ("therapy dogs", ""),
    ("kidtoberfest", ""),
    ("animal presentation", ""),
    ("member morning", ""),
    ("on the go", ""),
    ("fall changes", ""),
    ("toddler time", ""),
)


@dataclass(frozen=True)
class Event:
    source: str
    title: str
    start: datetime
    end: datetime
    location: str
    description: str
    url: str


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_ics_datetime(value: str, params: str = "") -> datetime:
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    if "VALUE=DATE" in params or re.fullmatch(r"\d{8}", value):
        return datetime.combine(datetime.strptime(value[:8], "%Y%m%d").date(), time.min, TZ)
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=TZ)


def parse_single_vevent(ics_text: str, source_url: str, label: str) -> Event | None:
    inside = False
    props: dict[str, tuple[str, str]] = {}
    for line in unfold_ics(ics_text):
        if line == "BEGIN:VEVENT":
            inside = True
            continue
        if line == "END:VEVENT":
            break
        if not inside or ":" not in line:
            continue
        left, value = line.split(":", 1)
        name, _, params = left.partition(";")
        props[name.upper()] = (params, value)

    required = {"SUMMARY", "DTSTART", "DTEND"}
    if not required.issubset(props):
        return None

    title = html.unescape(props["SUMMARY"][1]).strip()
    if label and not title.startswith(label.strip()):
        title = f"{label}{title}"
    location = html.unescape(props.get("LOCATION", ("", ""))[1]).replace("\\,", ",")
    description = html.unescape(props.get("DESCRIPTION", ("", ""))[1]).replace("\\n", " ")
    description = description.replace("\\,", ",").replace("\\;", ";")
    return Event(
        source="Greenville County Library",
        title=title,
        start=parse_ics_datetime(props["DTSTART"][1], props["DTSTART"][0]),
        end=parse_ics_datetime(props["DTEND"][1], props["DTEND"][0]),
        location=location.strip(),
        description=re.sub(r"\s+", " ", description).strip(),
        url=source_url,
    )


def library_events(start_day: date, end_day: date) -> tuple[list[Event], list[str]]:
    events: list[Event] = []
    errors: list[str] = []
    pattern = re.compile(
        r'href="data:text/calendar;charset=utf8;base64,([A-Za-z0-9+/=]+)"',
        re.IGNORECASE,
    )
    for slug, label in LIBRARY_SERIES.items():
        url = f"https://www.greenvillelibrary.org/event-series/{slug}"
        try:
            page = fetch_text(url)
            matches = pattern.findall(page)
            if not matches:
                continue
            for encoded in matches:
                try:
                    decoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
                    event = parse_single_vevent(decoded, url, label)
                except Exception as exc:  # Keep other series available if one record is malformed.
                    errors.append(f"Library record parse error ({slug}): {exc}")
                    continue
                if not event:
                    continue
                local_day = event.start.astimezone(TZ).date()
                if (
                    start_day <= local_day <= end_day
                    and "Hughes Main Library" in event.location
                ):
                    events.append(event)
        except Exception as exc:
            errors.append(f"Library fetch failed ({slug}): {exc}")
    return events, errors


def clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def tcmu_label(title: str) -> str | None:
    lowered = title.casefold()
    for phrase, label in TCMU_RULES:
        if phrase in lowered:
            return label
    return None


def tcmu_events(start_day: date, end_day: date) -> tuple[list[Event], list[str]]:
    params = urllib.parse.urlencode(
        {
            "start_date": f"{start_day.isoformat()} 00:00:00",
            "end_date": f"{end_day.isoformat()} 23:59:59",
            "per_page": 100,
        }
    )
    events: list[Event] = []
    errors: list[str] = []
    items: list[dict] = []
    page = 1
    while True:
        url = (
            "https://tcmupstate.org/wp-json/tribe/events/v1/events?"
            f"{params}&page={page}"
        )
        try:
            payload = json.loads(fetch_text(url))
        except Exception as exc:
            if page == 1:
                return [], [f"TCMU fetch failed: {exc}"]
            errors.append(f"TCMU page {page} fetch failed: {exc}")
            break
        items.extend(payload.get("events", []))
        if page >= int(payload.get("total_pages", 1)):
            break
        page += 1

    for item in items:
        venue = (item.get("venue") or {}).get("venue", "")
        if venue != "TCMU Greenville":
            continue
        title = clean_html(item.get("title", ""))
        label = tcmu_label(title)
        if label is None:
            continue
        try:
            start = datetime.fromisoformat(item["start_date"]).replace(tzinfo=TZ)
            end = datetime.fromisoformat(item["end_date"]).replace(tzinfo=TZ)
        except (KeyError, ValueError) as exc:
            errors.append(f"TCMU record parse error ({title}): {exc}")
            continue
        address_bits = [
            (item.get("venue") or {}).get("venue", ""),
            (item.get("venue") or {}).get("address", ""),
            (item.get("venue") or {}).get("city", ""),
            (item.get("venue") or {}).get("state", ""),
            (item.get("venue") or {}).get("zip", ""),
        ]
        location = ", ".join(str(bit).strip() for bit in address_bits if bit)
        events.append(
            Event(
                source="The Children's Museum of the Upstate",
                title=f"{label}{title}",
                start=start,
                end=end,
                location=location,
                description=clean_html(item.get("description", ""))[:800],
                url=item.get("url", "https://tcmupstate.org/greenville/calendar/"),
            )
        )
    return events, errors


def deduplicate(events: list[Event]) -> list[Event]:
    unique: dict[tuple[str, str, str], Event] = {}
    for event in events:
        key = (
            event.start.astimezone(TZ).isoformat(),
            re.sub(r"^\[(?:18m\+|2\+)\]\s*", "", event.title).casefold(),
            event.location.casefold(),
        )
        unique[key] = event
    return sorted(unique.values(), key=lambda event: (event.start, event.title))


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def fold_ics_line(line: str) -> list[str]:
    # RFC 5545 recommends no more than 75 octets per content line.
    chunks: list[str] = []
    current = ""
    limit = 75
    for char in line:
        candidate = current + char
        if len(candidate.encode("utf-8")) > limit:
            chunks.append(current)
            current = " " + char
            limit = 75
        else:
            current = candidate
    chunks.append(current)
    return chunks


def datetime_lines(prefix: str, value: datetime) -> list[str]:
    if value.utcoffset() == timedelta(0):
        return [f"{prefix}:{value.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"]
    return [f"{prefix};TZID=America/New_York:{value.astimezone(TZ).strftime('%Y%m%dT%H%M%S')}"]


def event_uid(event: Event) -> str:
    raw = f"{event.source}|{event.title}|{event.start.isoformat()}|{event.location}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@greenville-kid-calendar"


def render_calendar(events: list[Event], generated_at: datetime) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Greenville Kid Calendar//Downtown Activities//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Downtown Greenville Kid Activities",
        "X-WR-TIMEZONE:America/New_York",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "BEGIN:VTIMEZONE",
        "TZID:America/New_York",
        "X-LIC-LOCATION:America/New_York",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:-0500",
        "TZOFFSETTO:-0400",
        "TZNAME:EDT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0400",
        "TZOFFSETTO:-0500",
        "TZNAME:EST",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    stamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for event in events:
        description = event.description
        source_note = f"Source: {event.source}. Check the linked organizer page before leaving."
        description = f"{description} {source_note}".strip()
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{event_uid(event)}",
                f"DTSTAMP:{stamp}",
                *datetime_lines("DTSTART", event.start),
                *datetime_lines("DTEND", event.end),
                f"SUMMARY:{ics_escape(event.title)}",
                f"LOCATION:{ics_escape(event.location)}",
                f"DESCRIPTION:{ics_escape(description)}",
                f"URL:{event.url}",
                "END:VEVENT",
            ]
        )
    lines.append("END:VCALENDAR")
    folded = [folded for line in lines for folded in fold_ics_line(line)]
    return "\r\n".join(folded) + "\r\n"


def render_index(event_count: int, generated_at: datetime, errors: list[str]) -> str:
    updated = generated_at.astimezone(TZ).strftime("%B %-d, %Y at %-I:%M %p %Z")
    warning = ""
    if errors:
        warning = (
            '<p class="warning">One or more sources had a temporary problem during the latest refresh. '
            "The available sources were still published.</p>"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Downtown Greenville Kid Activities</title>
  <style>
    body {{ max-width: 680px; margin: 4rem auto; padding: 0 1.25rem; font: 17px/1.55 system-ui, sans-serif; color: #233024; }}
    h1 {{ line-height: 1.15; }}
    .button {{ display: inline-block; padding: .8rem 1rem; border-radius: .6rem; background: #236b3d; color: white; text-decoration: none; font-weight: 700; }}
    .meta {{ color: #667066; }}
    .warning {{ padding: .75rem; background: #fff2cf; border-radius: .4rem; }}
  </style>
</head>
<body>
  <h1>Downtown Greenville Kid Activities</h1>
  <p>A rolling 45-day calendar for active children ages 0–3, focused on downtown Greenville, Heritage Green, and nearby family venues.</p>
  <p><a class="button" href="calendar.ics">Subscribe or download calendar</a></p>
  <p class="meta">{event_count} upcoming events · Updated {updated}</p>
  {warning}
  <h2>Included sources</h2>
  <ul>
    <li>Greenville County Library — Hughes Main Library</li>
    <li>The Children's Museum of the Upstate — Greenville</li>
  </ul>
  <p>Events marked <strong>[18m+]</strong> or <strong>[2+]</strong> are stretch options based on the organizer's stated age range.</p>
  <h2>Greenville Zoo</h2>
  <p>The zoo does not currently provide a stable feed that can be safely merged automatically. Use its <a href="https://www.greenvillezoo.com/Calendar.aspx">official calendar</a> for zoo updates.</p>
</body>
</html>
"""


def main() -> None:
    generated_at = datetime.now(TZ)
    start_day = generated_at.date()
    end_day = start_day + timedelta(days=WINDOW_DAYS)

    library, library_errors = library_events(start_day, end_day)
    tcmu, tcmu_errors = tcmu_events(start_day, end_day)
    errors = library_errors + tcmu_errors
    events = deduplicate(library + tcmu)
    if not events:
        raise RuntimeError("No events were found; refusing to replace the published feed.")

    DOCS.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_calendar(events, generated_at), encoding="utf-8")
    (DOCS / "index.html").write_text(
        render_index(len(events), generated_at, errors), encoding="utf-8"
    )
    STATUS.write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "window_start": start_day.isoformat(),
                "window_end": end_day.isoformat(),
                "event_count": len(events),
                "source_counts": {"library": len(library), "tcmu": len(tcmu)},
                "warnings": errors,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Published {len(events)} events ({len(library)} library, {len(tcmu)} TCMU).")
    for warning in errors:
        print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
