FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Overridden by docker-compose; these are the in-container defaults.
ENV BIND_HOST=0.0.0.0 \
    PORT=7082 \
    DATA_DIR=/var/lib/pushbridge \
    PYTHONUNBUFFERED=1

# Exactly one worker: the alarm watcher thread and the subscription store live in
# process memory, so a second worker would poll and push a duplicate of every alarm.
CMD ["sh", "-c", "exec gunicorn --bind ${BIND_HOST}:${PORT} --workers 1 --threads 4 --access-logfile - main:app"]
