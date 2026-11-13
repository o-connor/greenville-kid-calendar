# Greenville Kid Calendar

A rolling public iCalendar feed for downtown Greenville activities suitable for active children ages 0–3.

The GitHub Pages site lets visitors browse the full feed directly in a responsive agenda or week view, with week navigation, keyword search, venue filters, and organizer age labels. Calendar downloads are optional. Stale feeds and source refresh failures are clearly labeled.

The page template lives in `templates/index.html`; the daily generator embeds the latest events into `docs/index.html`, so the design survives feed refreshes.

The feed covers the next 45 days and refreshes daily through GitHub Actions. It currently includes:

- Greenville County Library programs at Hughes Main Library, including Story Time and Bilingual Story Time
- The Children's Museum of the Upstate in Greenville
- Toddler and family programs at Upcountry History Museum
- Storytime on the Steps at M. Judson Booksellers
- Toddler-friendly Greenville Zoo special events
- Selected South Carolina Children's Theatre performances for ages 3+
- TD Saturday Market and selected Main Street family events
- Selected toddler and family events from the official City of Greenville calendar
- Family programs at the Greenville County Museum of Art

Kidding Around Greenville and Littles Weekly are used only as supplemental sources. Events must match an approved downtown-area venue and toddler-friendly title or category; distant venues, generic venue-opening listings, and duplicates from official calendars are excluded.

Greenville Symphony events are intentionally excluded from every source.

Events whose official minimum age is above one are labeled `[18m+]`, `[2+]`, or `[3+]`.

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
