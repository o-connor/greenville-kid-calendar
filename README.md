# Greenville Kid Calendar

A rolling public iCalendar feed for downtown Greenville activities suitable for active children ages 0–3.

The feed covers the next 45 days and refreshes daily through GitHub Actions. It currently includes:

- Greenville County Library programs at Hughes Main Library
- The Children's Museum of the Upstate in Greenville

Events whose official minimum age is above one are labeled `[18m+]` or `[2+]`.

## Apple Calendar

After GitHub Pages is enabled, subscribe to:

`https://o-connor.github.io/greenville-kid-calendar/calendar.ics`

In Calendar on a Mac, choose **File → New Calendar Subscription**, paste the URL, choose iCloud as the location, and select an automatic refresh interval.

## Refresh manually

```sh
python3 scripts/update_calendar.py
```

The generated feed and status page are stored in `docs/`.

## Source reliability

The generator publishes available sources even when one source temporarily fails. It refuses to replace the feed if every source returns zero events. Organizer pages remain authoritative; event details can change.
