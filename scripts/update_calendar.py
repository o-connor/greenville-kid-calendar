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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

UPCOUNTRY_RULES = (
    ("toddler time", "[2+] "),
    ("family fun day", ""),
    ("neighborhood night", ""),
)

KAG_TODDLER_CATEGORY_URL = (
    "https://kiddingaroundgreenville.com/events/categories/toddler-preschool"
)
KAG_DOWNTOWN_LOCATION_URL = (
    "https://kiddingaroundgreenville.com/locations/downtown-greenville"
)

KAG_HUGHES_RULES = (
    ("lollipops concert", "[3+] "),
    ("musical jamboree", "[18m+] "),
    ("not-so-spooky toddler tales", "[18m+] "),
    ("spooky goose", ""),
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


def parse_single_vevent(
    ics_text: str,
    source_url: str,
    label: str,
    source: str = "Greenville County Library",
) -> Event | None:
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
    location = props.get("LOCATION", ("", ""))[1]
    location = html.unescape(location.replace("\\,", ",").replace("\\;", ";"))
    description = props.get("DESCRIPTION", ("", ""))[1].replace("\\n", " ")
    description = html.unescape(description.replace("\\,", ",").replace("\\;", ";"))
    return Event(
        source=source,
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
        venue_value = item.get("venue")
        if isinstance(venue_value, dict):
            venues = [venue_value]
        elif isinstance(venue_value, list):
            venues = [venue for venue in venue_value if isinstance(venue, dict)]
        else:
            venues = []
        venue = next(
            (
                candidate
                for candidate in venues
                if candidate.get("venue") == "TCMU Greenville"
            ),
            None,
        )
        if venue is None:
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
            venue.get("venue", ""),
            venue.get("address", ""),
            venue.get("city", ""),
            venue.get("state", ""),
            venue.get("zip", ""),
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


def upcountry_label(title: str) -> str | None:
    lowered = title.casefold()
    for phrase, label in UPCOUNTRY_RULES:
        if phrase in lowered:
            return label
    return None


def upcountry_events(start_day: date, end_day: date) -> tuple[list[Event], list[str]]:
    params = urllib.parse.urlencode(
        {
            "start_date": f"{start_day.isoformat()} 00:00:00",
            "end_date": f"{end_day.isoformat()} 23:59:59",
            "per_page": 100,
        }
    )
    url = f"https://upcountryhistory.org/wp-json/tribe/events/v1/events?{params}"
    try:
        items = json.loads(fetch_text(url)).get("events", [])
    except Exception as exc:
        return [], [f"Upcountry History Museum fetch failed: {exc}"]

    events: list[Event] = []
    errors: list[str] = []
    for item in items:
        title = clean_html(item.get("title", ""))
        label = upcountry_label(title)
        if label is None:
            continue
        try:
            start = datetime.fromisoformat(item["start_date"]).replace(tzinfo=TZ)
            end = datetime.fromisoformat(item["end_date"]).replace(tzinfo=TZ)
        except (KeyError, ValueError) as exc:
            errors.append(f"Upcountry record parse error ({title}): {exc}")
            continue
        venue = item.get("venue") if isinstance(item.get("venue"), dict) else {}
        address_bits = [
            venue.get("venue", "Upcountry History Museum"),
            venue.get("address", "540 Buncombe Street"),
            venue.get("city", "Greenville"),
            venue.get("state", "SC"),
            venue.get("zip", "29601"),
        ]
        events.append(
            Event(
                source="Upcountry History Museum",
                title=f"{label}{title}",
                start=start,
                end=end,
                location=", ".join(str(bit).strip() for bit in address_bits if bit),
                description=clean_html(item.get("description", ""))[:800],
                url=item.get("url", "https://upcountryhistory.org/calendar/"),
            )
        )
    return events, errors


def kag_event_links(page: str) -> set[str]:
    return {
        html.unescape(url).rstrip("/")
        for url in re.findall(
            r'href=["\'](https://kiddingaroundgreenville\.com/events/[^"\']+)["\']',
            page,
            flags=re.IGNORECASE,
        )
        if "/categories/" not in url
    }


def kag_selection(title: str, location: str) -> tuple[str, str] | None:
    """Return a display source and age label for approved downtown-area events."""
    lowered_title = title.casefold()
    lowered_location = html.unescape(location).casefold()
    if "m. judson booksellers" in lowered_location and "storytime on the steps" in lowered_title:
        return "M. Judson Booksellers", ""
    if "greenville zoo" in lowered_location:
        return "Greenville Zoo", ""
    if "south carolina children's theatre" in lowered_location:
        return "South Carolina Children's Theatre", "[3+] "
    if "main street" in lowered_location and "trick or treat on main street" in lowered_title:
        return "Downtown Greenville", ""
    if "downtown greenville" in lowered_location and "td saturday market" in lowered_title:
        return "Downtown Greenville", ""
    if "hughes main library" in lowered_location:
        for phrase, label in KAG_HUGHES_RULES:
            if phrase in lowered_title:
                return "Greenville County Library", label
    return None


def kidding_around_events(start_day: date, end_day: date) -> tuple[list[Event], list[str]]:
    """Select toddler-friendly downtown events from Kidding Around Greenville."""
    errors: list[str] = []
    try:
        first_page = fetch_text(KAG_TODDLER_CATEGORY_URL)
    except Exception as exc:
        return [], [f"Kidding Around toddler listing fetch failed: {exc}"]

    page_numbers = [int(value) for value in re.findall(r"[?&]pno=(\d+)", first_page)]
    last_page = min(max(page_numbers, default=1), 10)
    pages = [first_page]
    for page_number in range(2, last_page + 1):
        try:
            pages.append(fetch_text(f"{KAG_TODDLER_CATEGORY_URL}?pno={page_number}"))
        except Exception as exc:
            errors.append(f"Kidding Around toddler page {page_number} failed: {exc}")
    try:
        pages.append(fetch_text(KAG_DOWNTOWN_LOCATION_URL))
    except Exception as exc:
        errors.append(f"Kidding Around downtown listing fetch failed: {exc}")

    event_urls = sorted({url for page in pages for url in kag_event_links(page)})

    def fetch_event(event_url: str) -> tuple[str, Event | None, Exception | None]:
        try:
            event = parse_single_vevent(
                fetch_text(f"{event_url}/ical/"),
                event_url,
                "",
                source="Kidding Around Greenville",
            )
            return event_url, event, None
        except Exception as exc:
            return event_url, None, exc

    fetched: list[tuple[str, Event | None, Exception | None]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_event, event_url) for event_url in event_urls]
        for future in as_completed(futures):
            fetched.append(future.result())

    events: list[Event] = []
    for event_url, event, error in sorted(fetched, key=lambda item: item[0]):
        if error:
            errors.append(f"Kidding Around record fetch failed ({event_url}): {error}")
            continue
        if not event:
            errors.append(f"Kidding Around record parse failed ({event_url})")
            continue
        event_day = event.start.astimezone(TZ).date()
        selection = kag_selection(event.title, event.location)
        if not (start_day <= event_day <= end_day) or selection is None:
            continue
        source, label = selection
        title = event.title
        if label and not title.startswith(label.strip()):
            title = f"{label}{title}"
        events.append(
            Event(
                source=source,
                title=title,
                start=event.start,
                end=event.end,
                location=event.location,
                description=event.description,
                url=event.url,
            )
        )
    return events, errors


def canonical_location(location: str) -> str:
    lowered = html.unescape(location).casefold()
    aliases = (
        ("hughes main library", "hughes main library"),
        ("m. judson booksellers", "m. judson booksellers"),
        ("greenville zoo", "greenville zoo"),
        ("south carolina children's theatre", "south carolina children's theatre"),
        ("tcmu greenville", "tcmu greenville"),
        ("upcountry history museum", "upcountry history museum"),
        ("downtown greenville", "downtown greenville"),
        ("main street", "downtown greenville"),
    )
    for fragment, canonical in aliases:
        if fragment in lowered:
            return canonical
    return re.sub(r"\s+", " ", lowered).strip()


def deduplicate(events: list[Event]) -> list[Event]:
    unique: dict[tuple[str, str, str], Event] = {}
    for event in events:
        key = (
            event.start.astimezone(TZ).isoformat(),
            re.sub(r"^\[(?:18m\+|2\+|3\+)\]\s*", "", event.title).casefold(),
            canonical_location(event.location),
        )
        unique.setdefault(key, event)
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


def display_time(value: datetime) -> str:
    local = value.astimezone(TZ)
    return local.strftime("%-I:%M %p").replace(":00 ", " ")


def render_two_week_calendar(events: list[Event], start_day: date) -> str:
    end_day = start_day + timedelta(days=13)
    grouped: dict[date, list[Event]] = {
        start_day + timedelta(days=offset): [] for offset in range(14)
    }
    for event in events:
        event_day = event.start.astimezone(TZ).date()
        if start_day <= event_day <= end_day:
            grouped[event_day].append(event)

    cards: list[str] = []
    for day, day_events in grouped.items():
        today_class = " today" if day == start_day else ""
        event_markup: list[str] = []
        for event in day_events:
            title = html.escape(event.title)
            url = html.escape(event.url, quote=True)
            source = html.escape(event.source)
            start_label = display_time(event.start)
            end_label = display_time(event.end)
            time_label = start_label if start_label == end_label else f"{start_label}–{end_label}"
            event_markup.append(
                f'<a class="event" href="{url}"><span class="event-time">{time_label}</span>'
                f'<span class="event-title">{title}</span><span class="event-source">{source}</span></a>'
            )
        if not event_markup:
            event_markup.append('<p class="empty">No events listed</p>')
        cards.append(
            f'<section class="day{today_class}" aria-label="{day.strftime("%A, %B %-d")}">'
            f'<header><span>{day.strftime("%a")}</span><strong>{day.strftime("%-d")}</strong></header>'
            f'<div class="day-events">{"".join(event_markup)}</div></section>'
        )
    return "".join(cards)


def render_index(events: list[Event], generated_at: datetime, errors: list[str]) -> str:
    payload = {
        "generated_at": generated_at.isoformat(),
        "window_end": (generated_at.date() + timedelta(days=WINDOW_DAYS)).isoformat(),
        "warnings": errors,
        "events": [
            {"title": e.title, "source": e.source, "start": e.start.isoformat(),
             "end": e.end.isoformat(), "location": e.location,
             "description": e.description, "url": e.url}
            for e in events
        ],
    }
    template = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    return template.replace("__CALENDAR_DATA__", json.dumps(payload).replace("<", "\\u003c"))


def main() -> None:
    generated_at = datetime.now(TZ)
    start_day = generated_at.date()
    end_day = start_day + timedelta(days=WINDOW_DAYS)

    library, library_errors = library_events(start_day, end_day)
    tcmu, tcmu_errors = tcmu_events(start_day, end_day)
    upcountry, upcountry_errors = upcountry_events(start_day, end_day)
    kidding_around, kidding_around_errors = kidding_around_events(start_day, end_day)
    errors = library_errors + tcmu_errors + upcountry_errors + kidding_around_errors
    events = deduplicate(library + tcmu + upcountry + kidding_around)
    if not events:
        raise RuntimeError("No events were found; refusing to replace the published feed.")

    DOCS.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render_calendar(events, generated_at), encoding="utf-8")
    (DOCS / "index.html").write_text(
        render_index(events, generated_at, errors), encoding="utf-8"
    )
    STATUS.write_text(
        json.dumps(
            {
                "generated_at": generated_at.isoformat(),
                "window_start": start_day.isoformat(),
                "window_end": end_day.isoformat(),
                "event_count": len(events),
                "source_counts": {
                    "library": len(library),
                    "tcmu": len(tcmu),
                    "upcountry_history_museum": len(upcountry),
                    "kidding_around_supplemental": len(kidding_around),
                    "m_judson": sum(
                        event.source == "M. Judson Booksellers"
                        for event in kidding_around
                    ),
                    "greenville_zoo": sum(
                        event.source == "Greenville Zoo" for event in kidding_around
                    ),
                    "sc_children_theatre": sum(
                        event.source == "South Carolina Children's Theatre"
                        for event in kidding_around
                    ),
                    "downtown_greenville": sum(
                        event.source == "Downtown Greenville"
                        for event in kidding_around
                    ),
                    "hughes_main_supplemental": sum(
                        event.source == "Greenville County Library"
                        for event in kidding_around
                    ),
                },
                "warnings": errors,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"Published {len(events)} events "
        f"({len(library)} library, {len(tcmu)} TCMU, "
        f"{len(upcountry)} Upcountry, {len(kidding_around)} Kidding Around supplemental)."
    )
    for warning in errors:
        print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
