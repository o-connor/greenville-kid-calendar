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

UPCOUNTRY_RULES = (
    ("toddler time", "[2+] "),
    ("family fun day", ""),
    ("neighborhood night", ""),
)

M_JUDSON_CATEGORY_URL = (
    "https://kiddingaroundgreenville.com/events/categories/toddler-preschool"
)
M_JUDSON_LOCATION = "M. Judson Booksellers"


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
    location = html.unescape(props.get("LOCATION", ("", ""))[1]).replace("\\,", ",")
    description = html.unescape(props.get("DESCRIPTION", ("", ""))[1]).replace("\\n", " ")
    description = description.replace("\\,", ",").replace("\\;", ";")
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


def m_judson_events(start_day: date, end_day: date) -> tuple[list[Event], list[str]]:
    """Read only M. Judson storytimes from Kidding Around's toddler calendar."""
    events: list[Event] = []
    errors: list[str] = []
    try:
        first_page = fetch_text(M_JUDSON_CATEGORY_URL)
    except Exception as exc:
        return [], [f"M. Judson listing fetch failed: {exc}"]

    page_numbers = [
        int(value) for value in re.findall(r"[?&]pno=(\d+)", first_page)
    ]
    last_page = min(max(page_numbers, default=1), 10)
    pages = [first_page]
    for page_number in range(2, last_page + 1):
        try:
            pages.append(fetch_text(f"{M_JUDSON_CATEGORY_URL}?pno={page_number}"))
        except Exception as exc:
            errors.append(f"M. Judson listing page {page_number} failed: {exc}")

    event_urls = sorted(
        {
            html.unescape(url).rstrip("/")
            for page in pages
            for url in re.findall(
                r'href=["\'](https://kiddingaroundgreenville\.com/events/'
                r'storytime-on-the-steps[^"\']*)["\']',
                page,
                flags=re.IGNORECASE,
            )
        }
    )
    for event_url in event_urls:
        try:
            event = parse_single_vevent(
                fetch_text(f"{event_url}/ical/"),
                event_url,
                "",
                source="M. Judson Booksellers",
            )
        except Exception as exc:
            errors.append(f"M. Judson record fetch failed ({event_url}): {exc}")
            continue
        if not event:
            errors.append(f"M. Judson record parse failed ({event_url})")
            continue
        event_day = event.start.astimezone(TZ).date()
        if start_day <= event_day <= end_day and M_JUDSON_LOCATION in event.location:
            events.append(event)
    return events, errors


def deduplicate(events: list[Event]) -> list[Event]:
    unique: dict[tuple[str, str, str], Event] = {}
    for event in events:
        key = (
            event.start.astimezone(TZ).isoformat(),
            re.sub(r"^\[(?:18m\+|2\+|3\+)\]\s*", "", event.title).casefold(),
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
    updated = generated_at.astimezone(TZ).strftime("%B %-d, %Y at %-I:%M %p %Z")
    event_count = len(events)
    calendar_markup = render_two_week_calendar(events, generated_at.astimezone(TZ).date())
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
  <meta name="description" content="A rolling two-week calendar of downtown Greenville activities for children ages 0–3.">
  <style>
    :root {{ color-scheme: light; --ink: #253127; --muted: #687269; --paper: #fbfaf5; --card: #fff; --line: #dce2d7; --green: #236b3d; --green-soft: #e8f3e8; --gold: #f0b544; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--paper); color: var(--ink); font: 16px/1.45 ui-rounded, "SF Pro Rounded", system-ui, sans-serif; }}
    main {{ width: min(1180px, calc(100% - 2rem)); margin: 0 auto; padding: 2.5rem 0 4rem; }}
    .top {{ display: flex; align-items: end; justify-content: space-between; gap: 2rem; margin-bottom: 2rem; }}
    .eyebrow {{ margin: 0 0 .4rem; color: var(--green); font-size: .78rem; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }}
    h1 {{ max-width: 720px; margin: 0; font-size: clamp(2rem, 4vw, 3.6rem); line-height: 1.03; letter-spacing: -.04em; }}
    .intro {{ max-width: 650px; margin: .8rem 0 0; color: var(--muted); font-size: 1.05rem; }}
    .actions {{ flex: 0 0 auto; text-align: right; }}
    .button {{ display: inline-block; padding: .8rem 1rem; border-radius: 999px; background: var(--green); color: white; text-decoration: none; font-weight: 750; box-shadow: 0 4px 14px rgb(35 107 61 / 18%); }}
    .button:hover {{ background: #174f2d; }}
    .meta {{ margin: .65rem 0 0; color: var(--muted); font-size: .85rem; }}
    .calendar-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; margin-bottom: .75rem; }}
    h2 {{ margin: 0; font-size: 1.3rem; }}
    .calendar-grid {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: .65rem; align-items: stretch; }}
    .day {{ min-width: 0; min-height: 190px; overflow: hidden; border: 1px solid var(--line); border-radius: .8rem; background: var(--card); }}
    .day > header {{ display: flex; align-items: center; justify-content: space-between; padding: .6rem .7rem; border-bottom: 1px solid var(--line); background: #f6f7f2; color: var(--muted); }}
    .day > header span {{ font-size: .75rem; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
    .day > header strong {{ color: var(--ink); font-size: 1rem; }}
    .day.today {{ border-color: var(--green); box-shadow: 0 0 0 1px var(--green); }}
    .day.today > header {{ background: var(--green-soft); color: var(--green); }}
    .day-events {{ display: grid; gap: .45rem; padding: .5rem; }}
    .event {{ display: grid; gap: .12rem; padding: .55rem; border-left: 3px solid var(--gold); border-radius: .35rem; background: #fff9ea; color: inherit; text-decoration: none; }}
    .event:hover {{ background: #fff2c9; }}
    .event-time {{ color: #765715; font-size: .7rem; font-weight: 800; text-transform: uppercase; }}
    .event-title {{ font-size: .82rem; font-weight: 750; line-height: 1.2; overflow-wrap: anywhere; }}
    .event-source {{ color: var(--muted); font-size: .67rem; line-height: 1.15; }}
    .empty {{ margin: .45rem; color: #9aa19a; font-size: .75rem; font-style: italic; }}
    .details {{ display: grid; grid-template-columns: 1.5fr 1fr; gap: 2rem; margin-top: 2.5rem; padding-top: 1.5rem; border-top: 1px solid var(--line); }}
    .details h2 {{ margin-bottom: .5rem; }}
    .details p, .details li {{ color: var(--muted); }}
    .details a {{ color: var(--green); }}
    .warning {{ padding: .75rem; border-radius: .4rem; background: #fff2cf; }}
    @media (max-width: 900px) {{
      .top {{ align-items: start; flex-direction: column; gap: 1.25rem; }}
      .actions {{ text-align: left; }}
      .calendar-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .day {{ min-height: 0; }}
    }}
    @media (max-width: 560px) {{
      main {{ width: min(100% - 1.25rem, 1180px); padding-top: 1.5rem; }}
      .calendar-grid {{ grid-template-columns: 1fr; }}
      .day > header {{ justify-content: flex-start; gap: .45rem; }}
      .day > header strong {{ order: -1; }}
      .event-title {{ font-size: .9rem; }}
      .details {{ grid-template-columns: 1fr; gap: 1rem; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="top">
      <div>
        <p class="eyebrow">Greenville, South Carolina</p>
        <h1>Little-kid adventures, all in one place.</h1>
        <p class="intro">A rolling calendar for active children ages 0–3, focused on downtown Greenville and Heritage Green.</p>
      </div>
      <div class="actions">
        <a class="button" href="calendar.ics">Subscribe in Apple Calendar</a>
        <p class="meta">{event_count} events in the full 45-day feed<br>Updated {updated}</p>
      </div>
    </section>
    <section aria-labelledby="next-two-weeks">
      <div class="calendar-heading">
        <h2 id="next-two-weeks">The next two weeks</h2>
      </div>
      <div class="calendar-grid">{calendar_markup}</div>
    </section>
    {warning}
    <section class="details">
      <div>
        <h2>What’s included</h2>
        <ul>
          <li>Greenville County Library — Hughes Main Library</li>
          <li>The Children's Museum of the Upstate — Greenville</li>
          <li>Upcountry History Museum — Heritage Green</li>
          <li>M. Judson Booksellers — Storytime on the Steps</li>
        </ul>
        <p>Events marked <strong>[18m+]</strong>, <strong>[2+]</strong>, or <strong>[3+]</strong> are stretch options based on the organizer's stated age range.</p>
      </div>
      <div>
        <h2>Greenville Zoo</h2>
        <p>The zoo does not currently provide a stable feed that can be safely merged automatically. Check its <a href="https://www.greenvillezoo.com/Calendar.aspx">official calendar</a> for zoo updates.</p>
      </div>
    </section>
  </main>
</body>
</html>
"""


def main() -> None:
    generated_at = datetime.now(TZ)
    start_day = generated_at.date()
    end_day = start_day + timedelta(days=WINDOW_DAYS)

    library, library_errors = library_events(start_day, end_day)
    tcmu, tcmu_errors = tcmu_events(start_day, end_day)
    upcountry, upcountry_errors = upcountry_events(start_day, end_day)
    m_judson, m_judson_errors = m_judson_events(start_day, end_day)
    errors = library_errors + tcmu_errors + upcountry_errors + m_judson_errors
    events = deduplicate(library + tcmu + upcountry + m_judson)
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
                    "m_judson": len(m_judson),
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
        f"{len(upcountry)} Upcountry, {len(m_judson)} M. Judson)."
    )
    for warning in errors:
        print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
