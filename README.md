# gcal-slack-notifier

English | [한국어](README.ko.md)

A self-hosted replacement for Slack's discontinued **Google Calendar for Team
Events** app. Watches shared Google Calendars and posts new, changed and
cancelled events to a Slack channel, along with reminders before events start.

> [README.ko.md](README.ko.md) is the source of truth. Where the two disagree,
> the Korean version is correct.

## Who this is for

Teams that use Google Calendar to manage their schedule, run their own server,
and are comfortable creating a Google service account. This is a cron script,
not a one-click Slack app — there is no *Add to Slack* button, and setup
involves sharing each calendar with a service account. In exchange there is no
hosted service, no account, and no per-seat pricing.

If you want an installable app instead, this is not it.

## Why this exists

After Google Calendar for Team Events was deprecated, the remaining options were
either personal-status integrations (calendar → your own Slack status) or
freemium services that start charging once a team crosses a usage threshold.
Neither covers the plain case: a shared team calendar posting into a channel.

This has been running from cron for one team since 2024.

### What about the official Google Calendar app for Slack?

Slack points to it as the successor, but it does not notify a channel of your
choice when events on a shared team calendar are added, changed or cancelled.
That gap is the reason this exists.

## What it posts

| Trigger | Message |
|---|---|
| Event created | Title and date, linked to the event (with location if set) |
| Event title or date changed | Before → after |
| Event cancelled | Title and date |
| 15 minutes before an event starts | Reminder with a local-time stamp |
| Each morning | One digest listing today's all-day events and recurring occurrences |

The reminder delay is configurable; the morning hour is whatever you put in cron.

The morning digest covers all-day events and recurring occurrences because those
are the only ones no other path reaches: an all-day event has no start time so it
never matches the reminder, and recurring occurrences are not stored in the
database (only the master is). A timed one-off event already gets its own
reminder, so it is left out. A multi-day event is listed on every day it spans.

An event's location is shown after 📍 when set. If the location holds a meeting
link, the channel message keeps it but the push notification strips the URL. A
location-only change is not announced: people often append the attendee list to
the room name, which would otherwise post on every guest-list edit.

<!--
TODO: add screenshots. Capture two messages from your own Slack channel — one
reminder and one recurring digest — save them as docs/reminder.png and
docs/digest.png, then uncomment the block below. People looking for a
replacement want to see that it looks like what they lost, and awesome-selfhosted
style listings generally expect a screenshot.

| Reminder | Daily digest |
|---|---|
| ![reminder](docs/reminder.png) | ![digest](docs/digest.png) |
-->


## Why the recurring digest is separate

Incremental sync (`syncToken`) returns a recurring series as a **single master
event**, not as individual occurrences. A weekly meeting therefore appears once,
on the date the series started, and never again — so per-occurrence reminders are
impossible from the sync data alone.

The `--daily_digest` run works around this by querying Google directly with
`singleEvents=True`, which expands the series into occurrences, and filtering for
those that carry a `recurringEventId`. It does not use or update sync tokens, so
it cannot disturb the main sync loop.

## Requirements

- Python 3.10+
- A Google Cloud service account with the Calendar API enabled
- A Slack incoming webhook
- Linux, macOS or Windows. On Windows use Task Scheduler instead of cron
  (see below); the Docker setup is Linux-only.

## Setup

### 1. Google service account

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a
   project and enable the **Google Calendar API**.
2. Create a **service account** and download its JSON key as
   `service_account_credentials.json` into the project directory.
3. Open the service account and copy its email address
   (`something@project-id.iam.gserviceaccount.com`).
4. In Google Calendar, for **each calendar you want to watch**: Settings →
   *Share with specific people* → add that email with *See all event details*.

Sharing the calendar with the service account is what grants access. There is no
OAuth consent flow and no user login.

### 2. Slack webhook

Create an incoming webhook for the destination channel:
<https://api.slack.com/messaging/webhooks>. The webhook is bound to one channel,
so the channel is chosen at creation time.

### 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_WEBHOOK_URL` | *(required)* | Incoming webhook URL |
| `CALENDAR_IDS` | *(required)* | Comma-separated calendar ids |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `service_account_credentials.json` | Path to the JSON key |
| `DB_PATH` | `calendar.sqlite3` | Local state file |
| `TIMEZONE` | `UTC` | IANA timezone for formatting and for "today" |
| `LANGUAGE` | `en` | `en` or `ko` |
| `REMINDER_MINUTES` | `15` | Minutes before start to remind |

Calendar ids are under Google Calendar → Settings → *your calendar* →
*Integrate calendar* → **Calendar ID**.

`TIMEZONE` does not have to match the calendars' own timezone. Reminders compare
actual instants, so a calendar in `Asia/Seoul` works fine with `TIMEZONE=UTC`. It
controls how times are printed and which day counts as "today" for the digest.

### 4a. Run with Docker

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Both cron entries run inside the container. To change when the morning digest is
posted, edit the `0 9` in `docker/crontab`.

### 4b. Run without Docker

```bash
pip install -r requirements.txt
python calendar_bot.py --dryrun          # verify config; sends nothing
```

Then add to crontab:

```cron
* * * * * /usr/bin/python3 /path/to/calendar_bot.py >> /var/log/gcal-slack.log 2>&1
0 9 * * * /usr/bin/python3 /path/to/calendar_bot.py --daily_digest >> /var/log/gcal-slack.log 2>&1
```

The per-minute run does change detection and reminders. The morning run posts the
recurring digest and exits without opening the database, so the two can safely
start in the same second.

### 4c. Run on Windows

There is no cron, so register two Task Scheduler tasks. From an elevated prompt,
with `C:\path\to` replaced by wherever you cloned this:

```bat
schtasks /create /tn "gcal-slack" /sc minute /mo 1 ^
  /tr "pythonw C:\path\to\calendar_bot.py"

schtasks /create /tn "gcal-slack-digest" /sc daily /st 09:00 ^
  /tr "pythonw C:\path\to\calendar_bot.py --daily_digest"
```

`pythonw` keeps a console window from flashing every minute. Task Scheduler has no
per-minute option older than Windows 7's `/sc minute`; if yours rejects it, create
a daily task and add a repeat interval of 1 minute in the task's Triggers tab.

## First run

The first run finds every existing event and would treat all of them as new. To
avoid flooding the channel, the script detects an empty database and seeds it
silently — no messages are sent on that run. Notifications begin from the second
run onward.

## Options

```
--verbose                 print every event fetched from the API
--dryrun                  do everything except send Slack messages
--calendar_id ID          process only this calendar
--daily_digest            post today's digest and exit
--backfill_calendar_id    fill calendar_id on rows from an older schema; run once
```

## Notes and limitations

- **Polling, not push.** Changes are picked up on the next cron run, so up to one
  minute of delay. Google also supports push notifications (`events.watch`), which
  would need a publicly reachable HTTPS endpoint. Polling with `syncToken` is
  cheap here because each run only transfers what changed.
- **One channel.** All calendars post to the single channel the webhook is bound
  to. Watching two calendars into two channels means running two copies with
  separate `.env` and `DB_PATH`.
- **Only title and date changes are announced.** Changing a description, location
  or guest list updates the database but sends nothing, to keep the channel quiet.
- **Events more than a week in the past are ignored**, including late-arriving
  cancellations for them.
- **Not affiliated with Google or Slack.**

## Contributing

Issues and pull requests are welcome, but this is maintained on a best-effort
basis — it is a tool the author runs for one team, published in case it is useful
to others. There is no support commitment.

## License

MIT. See [LICENSE](LICENSE).
