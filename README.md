# gcal-slack-notifier

A self-hosted replacement for Slack's discontinued **Google Calendar for Team
Events** app. Watches shared Google Calendars and posts new, changed and
cancelled events to a Slack channel, along with reminders before events start.

## Who this is for

Teams that run a server and are comfortable creating a Google service account.
This is a cron script, not a one-click Slack app — there is no *Add to Slack*
button, and setup involves sharing each calendar with a service account. In
exchange there is no hosted service, no account, and no per-seat pricing.

If you want an installable app instead, this is not it.

## Why this exists

After Google Calendar for Team Events was deprecated, the remaining options were
either personal-status integrations (calendar → your own Slack status) or
freemium services that start charging once a team crosses a usage threshold.
Neither covers the plain case: a shared team calendar posting into a channel.

This has been running from cron for one team since 2024.

### What about the official Google Calendar app for Slack?

Slack points to it as the successor, and for personal use it does its job: it
posts your invitations, reminds you about events you are on, and sets your Slack
status during meetings.

It is built around *your own* calendar, though. For a shared team calendar it can
post a daily or weekly agenda — on paid plans — but it does not announce
individual events as they are added, changed or cancelled. That gap is the reason
this exists.

*Checked July 2026. If the official app covers this now, please open an issue and
this section will be corrected.*

## What it posts

| Trigger | Message |
|---|---|
| Event created | Title and date, linked to the event |
| Event title or date changed | Before → after |
| Event cancelled | Title and date |
| 15 minutes before an event starts | Reminder with a local-time stamp |
| All-day events, each morning | One message per event |
| Recurring events, each morning | A single digest listing today's occurrences |

The reminder delay and the morning hour are configurable.

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
- Linux or macOS. The date formatting uses `%-d`-style strftime directives, which
  are not supported on Windows.

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
| `ALLDAY_NOTIFY_HOUR` | `9` | Hour (0-23) to announce all-day events |

Calendar ids are under Google Calendar → Settings → *your calendar* →
*Integrate calendar* → **Calendar ID**.

`TIMEZONE` does not have to match the calendars' own timezone. Reminders compare
actual instants, so a calendar in `Asia/Seoul` works fine with `TIMEZONE=UTC`. It
controls how times are printed, which day counts as "today" for the digest, and
when `ALLDAY_NOTIFY_HOUR` fires.

### 4a. Run with Docker

```bash
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Both cron entries run inside the container. If you change `ALLDAY_NOTIFY_HOUR`,
change the hour in `docker/crontab` to match.

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
--daily_digest            post today's recurring events and exit
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
