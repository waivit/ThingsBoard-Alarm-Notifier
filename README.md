# ThingsBoard Alarm Notifier

**pushbridge** — a small Web Push backend that turns
[ThingsBoard](https://thingsboard.io/) alarms into OS-level notifications on a phone. It's
the companion service that lets the [PWA dashboard](../dashboard-pwa) alert its users even
when the app is closed.

```
PWA (/pwa_app) ──subscribe──► pushbridge ──poll──► ThingsBoard
      ▲                            │
      └──────── Web Push ──────────┘  (via the browser's push service)
```

Built solo end-to-end: the push protocol plumbing, the ThingsBoard watcher, the container
image, and the deploy pipeline.

## What it does

- **Hands out the VAPID application server key** the browser needs in order to subscribe.
- **Stores push subscriptions** posted by the PWA, keyed by endpoint (one per browser
  install), persisted to disk so they survive restarts.
- **Polls ThingsBoard active alarms** on a fixed interval with a dedicated read-only user,
  handling JWT login and token refresh transparently.
- **Pushes a notification for every new alarm** — severity translated to Thai, with a deep
  link into the PWA's alarm screen and the alarm id as the notification tag.
- **Prunes dead subscriptions** automatically when a push service reports them expired
  (404/410).

## Why it's worth a look

It's deliberately small, but it handles the parts of Web Push that are easy to get wrong:

- **VAPID lifecycle done properly**: the keypair is generated on first run and persisted
  outside the container image — because regenerating it silently invalidates *every*
  existing subscription. The deploy is built around not losing that file.
- **No duplicate notifications**: a watermark on `createdTime` means an alarm pushes
  exactly once, and the first poll after a restart sets a baseline rather than replaying
  every alarm that already existed.
- **Single-worker by design, on purpose**: the watcher thread and subscription store are
  in-process, so the container pins gunicorn to one worker — a constraint that is
  documented rather than discovered in production.
- **A watcher that cannot die**: poll failures are caught, the token is invalidated, and
  the loop retries — a transient ThingsBoard outage doesn't take push down with it.
- **Same-origin architecture by design**: served under the same domain and gateway as
  ThingsBoard (`/pwa_push/...`), sidestepping CORS entirely instead of papering over it —
  see [DEPLOY.md](DEPLOY.md).
- **CI/CD included**: GitHub Actions lints and builds the image on every push/PR, and
  auto-deploys to the production host over SSH on merge to `main`
  (`.github/workflows/`, `deploy.sh`).

## Tech stack

| Layer | Choice |
|---|---|
| API | Flask 3, gunicorn (1 worker + threads) |
| Push | `pywebpush` + `py-vapid` (VAPID / Web Push protocol) |
| Backend | ThingsBoard REST API (external, not part of this repo) |
| State | JSON file on a bind-mounted host directory |
| Lint | ruff |
| CI/CD | GitHub Actions → SSH deploy to a Docker + Kong host |

## Getting started

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then edit .env (TB_USER/TB_PASS at minimum)
python main.py          # http://127.0.0.1:7082
```

Create a **dedicated ThingsBoard user** for the watcher (read access to alarms) and put its
credentials in `.env` — the app refuses to start without them. All configuration is
environment-based; there is no config file. The VAPID keypair is generated on first run
and saved to `DATA_DIR/vapid_private.pem` — **back it up**.

```bash
ruff check .            # lint
docker build -t pushbridge .
```

## Endpoints

Paths are as this app sees them — Kong strips the `/pwa_push` prefix before forwarding.

| method | path | purpose |
| --- | --- | --- |
| GET | `/vapid-public-key` | app server key the browser needs to subscribe |
| POST | `/register` | body = `PushSubscription` JSON |
| POST | `/unregister` | body = `{ "endpoint": "..." }` |
| GET | `/health` | `{ ok, subscriptions }` |

## Deployment

See [DEPLOY.md](DEPLOY.md) for the full production setup (Docker, Kong routing, state
persistence, HTTPS requirements). In short: the service runs as a container on Kong's
internal network with no host port published, and is routed same-origin with ThingsBoard
so the PWA's calls need no CORS handling. Pushing to `main` triggers the GitHub Actions
workflow in [.github/workflows/deploy.yml](.github/workflows/deploy.yml), which pulls and
rebuilds the container automatically.

## Project structure

```
main.py                       Flask app + VAPID + subscription store + ThingsBoard watcher
requirements.txt              Python dependencies
Dockerfile                    python:3.13-slim + gunicorn (single worker)
docker-compose.pushbridge.yml Container on Kong's network, state bind-mounted
deploy.sh                     Pull + rebuild, run on the host by the deploy workflow
pushbridge.service            systemd unit — alternative for hosts without Docker
.env.example                  All configuration, documented
```
