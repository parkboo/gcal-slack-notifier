FROM python:3.12-slim

# cron runs the schedule, tzdata makes the container clock match TIMEZONE
RUN apt-get update \
 && apt-get install -y --no-install-recommends cron tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY calendar_bot.py .

# Registered as root's crontab. Deliberately not placed in /etc/cron.d, which
# uses a different format (it needs a user field) and would run the jobs twice.
COPY docker/crontab /app/crontab
RUN crontab /app/crontab

# Config is read from /app/.env (mounted by docker-compose) rather than from the
# environment, because cron does not inherit the container environment.
CMD ["cron", "-f", "-l", "2"]
