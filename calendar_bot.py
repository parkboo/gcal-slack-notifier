#!/usr/bin/env python3
"""Google Calendar -> Slack notifier.

Watches one or more Google Calendars and posts to a Slack channel when events
are created, changed or cancelled. Also posts reminders shortly before an event
starts, a morning notice for all-day events, and a daily digest of recurring
events.

Configuration is read from environment variables. See .env.example.
"""
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import requests
import sqlite3
import argparse
import os
import pytz
import base64
import traceback

script_path = os.path.abspath(__file__)
script_dir = os.path.dirname(script_path)
os.chdir(script_dir)

# Load .env if python-dotenv is installed. Optional: plain env vars work without it.
# Must run after the chdir above so that cron, which starts in /, still finds the file.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(script_dir, '.env'))
except ImportError:
    pass

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']


def env_list(name):
    return [v.strip() for v in os.environ.get(name, '').split(',') if v.strip()]


class Config:
    def __init__(self):
        self.webhook_url = os.environ.get('SLACK_WEBHOOK_URL', '')
        self.calendar_ids = env_list('CALENDAR_IDS')
        self.service_account_file = os.environ.get('GOOGLE_SERVICE_ACCOUNT_FILE',
                                                   'service_account_credentials.json')
        self.db_path = os.environ.get('DB_PATH', 'calendar.sqlite3')
        self.timezone = os.environ.get('TIMEZONE', 'UTC')
        self.language = os.environ.get('LANGUAGE', 'en')
        self._errors = []
        self.reminder_minutes = self._int_env('REMINDER_MINUTES', 15)
        self.allday_notify_hour = self._int_env('ALLDAY_NOTIFY_HOUR', 9)

    # Collect the error instead of raising, so validate() can report everything at once
    def _int_env(self, name, default):
        raw = os.environ.get(name)
        if raw is None or raw == '':
            return default
        try:
            return int(raw)
        except ValueError:
            self._errors.append(f'{name} must be a number, got {raw!r}')
            return default

    # Fail early with a readable message instead of a stack trace deep in the API client
    def validate(self, need_calendars=True):
        errors = list(self._errors)
        if not self.webhook_url:
            errors.append('SLACK_WEBHOOK_URL is not set')
        if need_calendars and not self.calendar_ids:
            errors.append('CALENDAR_IDS is not set (comma separated calendar ids)')
        if not os.path.exists(self.service_account_file):
            errors.append(f'service account file not found: {self.service_account_file}')
        if self.timezone not in pytz.all_timezones_set:
            errors.append(f'unknown TIMEZONE: {self.timezone}')
        if self.language not in MESSAGES:
            errors.append(f'unsupported LANGUAGE: {self.language} (available: {", ".join(MESSAGES)})')
        if errors:
            raise SystemExit('Configuration error:\n' + '\n'.join('  - ' + e for e in errors))

    def tz(self):
        return pytz.timezone(self.timezone)


# User facing strings. Add a language by copying a block and translating it.
MESSAGES = {
    'en': {
        'event_cancelled': 'Event cancelled: {summary}',
        'event_updated': 'Event updated: {summary}',
        'reminder_pretext': 'Starting in {minutes} minutes:',
        'allday_pretext': 'All-day event today:',
        'digest_header': "Today's recurring events",
        'weekdays': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        'datetime_format': '%a, %b %-d, %Y %-I:%M %p',
        'date_format': '%a, %b %-d, %Y',
        'am': 'AM',
        'pm': 'PM',
    },
    'ko': {
        'event_cancelled': '일정 취소: {summary}',
        'event_updated': '일정 변경: {summary}',
        'reminder_pretext': '{minutes}분 후에 일정이 시작됩니다:',
        'allday_pretext': '오늘 하루종일 일정입니다:',
        'digest_header': '오늘의 반복일정',
        'weekdays': ['월', '화', '수', '목', '금', '토', '일'],
        'datetime_format': '%Y년 %-m월 %-d일({weekday}) %p%-I:%M',
        'date_format': '%Y년 %-m월 %-d일({weekday})',
        'am': '오전',
        'pm': '오후',
    },
}


class Database:
    '''
    sqlite cli helper:
        $ sqlite3 calendar.sqlite3
        sqlite> .tables
        sqlite> .schema events
        sqlite> .schema sync_tokens
        sqlite> select * from events;
        sqlite> select * from sync_tokens;
    '''

    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.create_table()

    def create_table(self):
        self.conn.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, summary TEXT, start TEXT, end TEXT, canceled DATETIME);')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_events ON events (id);')
        self.conn.execute('CREATE INDEX IF NOT EXISTS idx_start ON events (start);')
        self.conn.execute('CREATE TABLE IF NOT EXISTS sync_tokens (calendar_id TEXT PRIMARY KEY, token TEXT);')

        # Databases created before calendar_id existed need the column added.
        # CREATE TABLE IF NOT EXISTS will not add it, so check and migrate.
        cur = self.conn.cursor()
        columns = [row[1] for row in cur.execute('PRAGMA table_info(events)').fetchall()]
        if 'calendar_id' not in columns:
            self.conn.execute('ALTER TABLE events ADD COLUMN calendar_id TEXT;')
            self.conn.commit()

    def save_sync_token(self, calendar_id, token):
        cur = self.conn.cursor()
        cur.execute('INSERT OR REPLACE INTO sync_tokens VALUES (?, ?)', (calendar_id, token))
        self.conn.commit()

    def load_sync_token(self, calendar_id):
        cur = self.conn.cursor()
        token = cur.execute('SELECT token FROM sync_tokens WHERE calendar_id=?', [calendar_id]).fetchone()
        if token is not None:
            return token[0]
        return None

    def get_event_count(self):
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) FROM events')
        return cur.fetchone()[0]

    def update_event(self, event_id, summary, start, end, calendar_id=None):
        self.conn.execute('UPDATE events SET summary=?, start=?, end=?, calendar_id=? WHERE id=?', (summary, start, end, calendar_id, event_id))

    def insert_event(self, event_id, summary, start, end, calendar_id=None):
        self.conn.execute('INSERT OR IGNORE INTO events (id, summary, start, end, calendar_id) VALUES (?, ?, ?, ?, ?)', (event_id, summary, start, end, calendar_id))

    # Columns are listed explicitly. SELECT * breaks callers whenever a column is added.
    def get_event(self, event_id):
        cur = self.conn.cursor()
        cur.execute('SELECT id, summary, start, end, canceled FROM events where id=?', [event_id])
        return cur.fetchone()

    # Used for all-day events, whose start is a bare date with no offset.
    def get_events_with_start_for_notify(self, start):
        cur = self.conn.cursor()
        cur.execute('SELECT id, summary, start, end, calendar_id FROM events where start=? AND canceled IS NULL', [start])
        return cur.fetchall()

    # Candidates for a timed reminder. Google stores dateTime with the calendar's
    # own UTC offset, which need not match ours, so the caller compares instants
    # rather than strings. Filtering by date prefix keeps the idx_start index usable.
    def get_events_starting_on(self, date_prefixes):
        cur = self.conn.cursor()
        where = ' OR '.join(['start LIKE ?'] * len(date_prefixes))
        cur.execute(f'SELECT id, summary, start, end, calendar_id FROM events '
                    f'WHERE canceled IS NULL AND ({where})',
                    [p + '%' for p in date_prefixes])
        return cur.fetchall()

    # Only fills rows that have no calendar_id yet. Existing values are left alone.
    def fill_calendar_id(self, event_ids, calendar_id):
        updated = 0
        for event_id in event_ids:
            cur = self.conn.execute('UPDATE events SET calendar_id=? WHERE id=? AND calendar_id IS NULL',
                                    (calendar_id, event_id))
            updated += cur.rowcount
        return updated

    def count_events_without_calendar_id(self):
        cur = self.conn.cursor()
        cur.execute('SELECT COUNT(*) FROM events WHERE calendar_id IS NULL')
        return cur.fetchone()[0]

    def mark_event_as_canceled(self, event_id):
        self.conn.execute('UPDATE events SET canceled=CURRENT_TIMESTAMP WHERE id=?', [event_id])
        self.conn.commit()

    def commit(self):
        self.conn.commit()


class CalendarBot():
    def __init__(self, config, verbose=False, dryrun=False):
        self.config = config
        self.verbose = verbose
        self.dryrun = dryrun
        self.msg = MESSAGES[config.language]

    def get_event_url(self, event_id, calendar_id):
        # https://stackoverflow.com/questions/53928044/how-do-i-construct-a-link-to-a-google-calendar-event
        # base64 encode f"{event_id} {calendar_id}" and strip trailing '='
        # https://www.google.com/calendar/event?eid={base64}
        eid = base64.b64encode(f"{event_id} {calendar_id}".encode('utf-8')).decode('utf-8').replace("=", "")
        event_url = f"https://www.google.com/calendar/event?eid={eid}"
        return event_url

    def send_message_to_slack(self, msg, date=None, url=None):
        if url is not None:
            # A rich_text link element serialises to "URL (text)" in push notification
            # fallbacks, exposing the raw URL. mrkdwn `<URL|text>` shows only the text.
            payload = {
                    "text": f"{msg} {date}",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*<{url}|{msg}>* {date}"
                            }
                        }
                    ]
            }
            print(payload)
            requests.post(self.config.webhook_url, json=payload)
        else:
            payload = {"text": msg}
            requests.post(self.config.webhook_url, json=payload)

    def send_upcoming_events_to_slack(self, title, id, calendar_id, ts=None, text=None,
                                      pretext=None):
        if pretext is None:
            pretext = self.msg['reminder_pretext'].format(minutes=self.config.reminder_minutes)

        # Events stored before the calendar_id column existed have no calendar_id.
        # Send without a link rather than building a wrong one.
        event_url = self.get_event_url(id, calendar_id) if calendar_id else None

        if text is None:
            # All-day events have no time, so callers pass text instead of ts
            text = f"<!date^{ts}" + "^{date_num} {time_secs}| >" if ts is not None else ""

        attachment = {
            "pretext": pretext,
            "color": "#2eb886",
            "title": title,
            "text": text
        }
        if event_url:
            attachment["title_link"] = event_url
        payload = {"attachments": [attachment]}
        print(payload)

        requests.post(self.config.webhook_url, json=payload)

        if self.verbose:
            print(f"{pretext} {title} {event_url}")

    def send_daily_digest_to_slack(self, header, lines, fallback):
        # Post several events as a single message
        payload = {
            "text": f"{header}\n" + "\n".join(fallback),  # plain text so push notifications do not expose URLs
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*{header}*\n" + "\n".join(lines)
                    }
                }
            ]
        }
        print(payload)
        requests.post(self.config.webhook_url, json=payload)

    def toLocalDate(self, d):
        datetime_obj = datetime.fromisoformat(d)
        weekday = self.msg['weekdays'][datetime_obj.weekday()]

        # Weekday names come from MESSAGES rather than the C locale, so no
        # locale needs to be generated on the host.
        if datetime_obj.time() != datetime.min.time():
            fmt = self.msg['datetime_format'].replace('{weekday}', weekday)
            # %p is locale dependent; normalise the English output ourselves
            return datetime_obj.strftime(fmt).replace("AM", self.msg.get('am', 'AM')).replace("PM", self.msg.get('pm', 'PM'))
        else:
            fmt = self.msg['date_format'].replace('{weekday}', weekday)
            return datetime_obj.strftime(fmt)

    # Fetch events from Google Calendar using the stored sync token
    def fetch_remote_events(self, service, calendar_id, sync_token, verbose):
        events = []
        next_sync_token = sync_token
        page_token = None
        time_min = None
        if sync_token is None:
            current_date = datetime.now()
            one_week_earlier = current_date - timedelta(weeks=1)
            time_min = one_week_earlier.strftime("%Y-%m-%dT%H:%M:%SZ")

        print("use sync token:" + str(sync_token))

        while True:
            events_result = service.events().list(calendarId=calendar_id, timeMin=time_min, syncToken=sync_token, pageToken=page_token).execute()
            events.extend(events_result.get('items', []))
            next_sync_token = events_result.get('nextSyncToken')
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break

        if verbose:
            for event in events:
                print('------------------------------')
                print(event)
        return (events, next_sync_token)

    # Fetch a time range directly. Does not use a sync token, so incremental sync is unaffected.
    def fetch_events_in_range(self, service, calendar_id, time_min, time_max=None, single_events=True):
        events = []
        page_token = None
        while True:
            params = {
                'calendarId': calendar_id,
                'timeMin': time_min,
                'singleEvents': single_events,  # True expands recurring events into instances
                'pageToken': page_token,
            }
            if time_max is not None:
                params['timeMax'] = time_max
            if single_events:
                params['orderBy'] = 'startTime'  # only valid together with singleEvents=True
            events_result = service.events().list(**params).execute()
            events.extend(events_result.get('items', []))
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break

        if self.verbose:
            for event in events:
                print('------------------------------')
                print(event)
        return events

    # Fetch today's events with recurring events expanded into instances.
    # Sync tokens only return the master event of a recurring series, so this
    # separate query is what makes per-occurrence notification possible.
    def fetch_today_events(self, service, calendar_id):
        tz = self.config.tz()
        day_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        return self.fetch_events_in_range(service, calendar_id, day_start.isoformat(), day_end.isoformat())

    # Fill calendar_id for events stored before that column existed.
    # Sends nothing and does not touch sync tokens.
    def backfill_calendar_id(self, db, service, calendar_ids):
        tz = self.config.tz()
        # Only events that may still trigger a notification matter, so look back a month
        time_min = (datetime.now(tz) - timedelta(days=30)).isoformat()

        total = 0
        for calendar_id in calendar_ids:
            # singleEvents=False: recurring events are stored under the master id,
            # so query in the same shape the database uses
            events = self.fetch_events_in_range(service, calendar_id, time_min, single_events=False)
            updated = db.fill_calendar_id([e['id'] for e in events], calendar_id)
            print(f"{calendar_id}: filled {updated}")
            total += updated
        db.commit()
        print(f"filled {total} total, {db.count_events_without_calendar_id()} events still without calendar_id")

    # Post today's recurring events as one digest (run from a morning cron)
    def notify_todays_recurring_events(self, service, calendar_ids):
        rows = []
        for calendar_id in calendar_ids:
            print(f'calendar_id={calendar_id}')
            for event in self.fetch_today_events(service, calendar_id):
                if event.get('status') == 'cancelled':
                    continue
                # Only instances of a recurring series carry recurringEventId.
                # Individually modified instances carry it too, so they are included.
                if 'recurringEventId' not in event:
                    continue
                try:
                    start = event['start'].get('dateTime', event['start'].get('date'))
                    end = event['end'].get('dateTime', event['end'].get('date'))
                except KeyError:
                    print("event without start/end")
                    continue
                rows.append((
                    start,
                    event.get('summary', 'No Title').strip(),
                    self.get_event_period(start, end),
                    # Use the htmlLink the API returns; it points at the specific instance
                    event.get('htmlLink') or self.get_event_url(event['id'], calendar_id),
                ))

        if not rows:
            print("no recurring events today")
            return

        rows.sort(key=lambda r: r[0])
        lines = [f"• *<{url}|{summary}>* {period}" for _, summary, period, url in rows]
        fallback = [f"• {summary} {period}" for _, summary, period, _ in rows]
        for line in fallback:
            print(line)

        if not self.dryrun:
            self.send_daily_digest_to_slack(self.msg['digest_header'], lines, fallback)

    # Format an event period. Shows only the start when start and end are the same day.
    def get_event_period(self, start, end):
        # All-day events end on the following day, so show the day before
        if end is not None and len(end) <= 10:
            date_end = datetime.fromisoformat(end)
            date_before = date_end - timedelta(days=1)
            localStart = self.toLocalDate(start)
            localEnd = self.toLocalDate(date_before.strftime('%Y-%m-%d'))

            if localStart == localEnd:
                return localStart
            else:
                return f"{localStart}~{localEnd}"
        else:
            # Spans multiple days
            if start[:10] != end[:10]:
                return f"{self.toLocalDate(start)}~{self.toLocalDate(end)}"
            else:
                # Same day, so the end time adds nothing
                return self.toLocalDate(start)

    def handle_cancelled_event(self, db, remote_event, calendar_id=None):
        # Guard against a cancellation arriving for an event that is not in the database
        try:
            row = db.get_event(remote_event['id'])
            id, summary, start, end, canceled = row
            msg = self.msg['event_cancelled'].format(summary=summary)
            date = self.get_event_period(start, end)
            print(msg)

            tz = self.config.tz()
            date_start = datetime.fromisoformat(start).astimezone(tz)

            # Cancellations sometimes arrive for long past events; ignore those
            if datetime.now(tz) - date_start > timedelta(weeks=1):
                print(f"ignoring cancellation of past event: {summary} {start}")
                return

            url = None
            if calendar_id:
                url = self.get_event_url(id, calendar_id)
                print(url)
            if not self.dryrun:
                self.send_message_to_slack(msg, date, url)
            db.mark_event_as_canceled(id)
        except Exception:
            print("event not found in database")
            traceback.print_exc()

    def handle_updated_event(self, db, remote_event, calendar_id=None):
        # Some events have no start/end
        try:
            remote_start = remote_event['start'].get('dateTime', remote_event['start'].get('date'))
            remote_end = remote_event['end'].get('dateTime', remote_event['end'].get('date'))
        except Exception:
            print("event without start/end")
            return

        try:
            row = db.get_event(remote_event['id'])
            id, summary, start, end, canceled = row
            remote_summary = remote_event.get('summary', 'No Title').strip()
            msg = self.msg['event_updated'].format(summary=summary)
            date = f"{self.get_event_period(start, end)} → {remote_summary} {self.get_event_period(remote_start, remote_end)}"
            db.update_event(id, remote_summary, remote_start, remote_end, calendar_id)

            # Ignore changes to anything other than the title or the dates
            if summary == remote_summary and start == remote_start and end == remote_end:
                return

            print(msg)
            url = None
            if calendar_id:
                url = self.get_event_url(id, calendar_id)
                print(url)
            if not self.dryrun:
                self.send_message_to_slack(msg, date, url)
        except Exception:
            pass

    def handle_new_event(self, db, remote_event, calendar_id=None):
        id = remote_event['id']
        remote_summary = remote_event.get('summary', 'No Title').strip()
        remote_start = remote_event['start'].get('dateTime', remote_event['start'].get('date'))
        remote_end = remote_event['end'].get('dateTime', remote_event['end'].get('date'))
        db.insert_event(id, remote_summary, remote_start, remote_end, calendar_id)

        msg = f"{remote_summary}"
        date = f"{self.get_event_period(remote_start, remote_end)}"
        print(msg)
        url = None
        if calendar_id:
            url = self.get_event_url(id, calendar_id)
            print(url)
        if not self.dryrun:
            self.send_message_to_slack(msg, date, url)

    # NOTE: this queries the whole database, not one calendar. Calling it inside
    # the calendar loop sends one duplicate notification per configured calendar.
    def search_upcoming_events(self, db):
        # Find events starting REMINDER_MINUTES from now, and at ALLDAY_NOTIFY_HOUR
        # sharp also look for all-day events starting today.
        tz = self.config.tz()
        current_date = datetime.now(tz)
        target = (current_date + timedelta(minutes=self.config.reminder_minutes)).replace(second=0, microsecond=0)
        print("reminder target: " + target.isoformat())

        # An event stored with a different UTC offset can fall on the neighbouring
        # calendar date, so look at three days and compare the actual instants.
        prefixes = [(target + timedelta(days=d)).strftime('%Y-%m-%d') for d in (-1, 0, 1)]
        target_ts = int(target.timestamp())
        results = []
        for row in db.get_events_starting_on(prefixes):
            start = row[2]
            if len(start) <= 10:
                continue  # all-day events are handled separately below
            try:
                start_dt = datetime.fromisoformat(start)
            except ValueError:
                continue
            if start_dt.tzinfo is None:
                start_dt = tz.localize(start_dt)
            if int(start_dt.timestamp()) == target_ts:
                results.append(row)

        if current_date.hour == self.config.allday_notify_hour and current_date.minute == 0:
            results_allday = db.get_events_with_start_for_notify(current_date.strftime("%Y-%m-%d"))
            for row in results_allday:
                id, summary, start, end, calendar_id = row
                print(f"all-day event: {summary} {self.get_event_period(start, end)}")
                if not self.dryrun:
                    # All-day events have no time, so pass the period text instead of ts
                    self.send_upcoming_events_to_slack(summary, id, calendar_id,
                                                       text=self.get_event_period(start, end),
                                                       pretext=self.msg['allday_pretext'])

        for row in results:
            id, summary, start, end, calendar_id = row
            print(f"upcoming event: {summary} {self.get_event_period(start, end)}")
            if not self.dryrun:
                ts = int(datetime.fromisoformat(start).timestamp())
                self.send_upcoming_events_to_slack(summary, id, calendar_id, ts)


def main():
    parser = argparse.ArgumentParser(description="Google Calendar -> Slack notifier")
    parser.add_argument("--verbose", help="increase output verbosity", action="store_true")
    parser.add_argument("--dryrun", help="for test (not sending slack messages)", action="store_true")
    parser.add_argument("--calendar_id", help="process only this calendar id", type=str, default="", required=False)
    parser.add_argument("--daily_digest", help="notify today's recurring events and exit (for the morning cron)", action="store_true")
    parser.add_argument("--backfill_calendar_id", help="fill calendar_id of existing events and exit (run once, sends nothing)", action="store_true")
    args = parser.parse_args()

    config = Config()
    config.validate(need_calendars=not args.calendar_id)

    bot = CalendarBot(config, verbose=args.verbose, dryrun=args.dryrun)

    print(datetime.now(config.tz()).strftime('%Y-%m-%d %H:%M:%S'))

    credentials = service_account.Credentials.from_service_account_file(
            config.service_account_file, scopes=SCOPES)
    service = build('calendar', 'v3', credentials=credentials)

    calendar_ids = [args.calendar_id] if args.calendar_id else config.calendar_ids

    # Morning cron: post today's recurring events and exit without touching sync tokens.
    # This reads Google directly and needs no database, so it does not contend with the
    # per-minute cron for the SQLite lock when both start in the same second.
    if args.daily_digest:
        bot.notify_todays_recurring_events(service, calendar_ids)
        return

    db = Database(config.db_path)

    # One-off migration helper for databases created before the calendar_id column
    if args.backfill_calendar_id:
        bot.backfill_calendar_id(db, service, calendar_ids)
        return

    rows = db.get_event_count()
    if rows == 0:
        # On the very first run the database is empty and every event would look new,
        # so seed it silently instead of flooding the channel
        bot.dryrun = True

    # Queries the whole database, so call it once outside the calendar loop
    bot.search_upcoming_events(db)

    for calendar_id in calendar_ids:
        print(f'calendar_id={calendar_id}')

        sync_token = db.load_sync_token(calendar_id)
        (remote_events, next_sync_token) = bot.fetch_remote_events(service, calendar_id, sync_token, args.verbose)
        if len(remote_events) == 0:
            print("No upcoming events found.")
            continue

        for remote_event in remote_events:
            # A deleted event arrives as
            # {'kind': 'calendar#event', 'etag': '...', 'id': '...', 'status': 'cancelled'}
            if remote_event['status'] == 'cancelled':
                bot.handle_cancelled_event(db, remote_event, calendar_id)
                continue

            remote_id = remote_event['id']
            # Skip events that are already well in the past
            try:
                tz = config.tz()
                remote_start = remote_event['start'].get('dateTime', remote_event['start'].get('date'))
                # All-day events have no time, so treat them as starting at midnight local time
                if len(remote_start) <= 10:
                    naive_midnight = datetime.fromisoformat(remote_start)
                    start_dt = tz.localize(naive_midnight)
                else:
                    start_dt = datetime.fromisoformat(remote_start)
                if datetime.now(tz) - start_dt > timedelta(weeks=1):
                    print(f"ignoring past event: {remote_event.get('summary')} {remote_start}")
                    continue
            except Exception as e:
                print(e)

            row = db.get_event(remote_id)
            if row is not None:
                bot.handle_updated_event(db, remote_event, calendar_id)
            else:
                bot.handle_new_event(db, remote_event, calendar_id)

        db.commit()
        db.save_sync_token(calendar_id, next_sync_token)


if __name__ == "__main__":
    main()
