# Deploy (Docker) — pushbridge

Flask backend served **same-origin** as ThingsBoard at `nonthaburi.info/pwa_push` (its own
Kong service). Same-origin means the PWA's `/pwa_push/...` calls hit this service without
**CORS** and without a second domain. The container is **not published to the host** —
Kong reaches it over the internal Docker network (`kongapi_default`), so the push backend
is never exposed outside Kong.

This is the supported deploy path and the one the CI/CD pipeline drives. For a host
without Docker, see [DEPLOY_SERVICE.md](DEPLOY_SERVICE.md) — that path is manual, with no
auto-deploy.

## 1. Build & serve (Docker)
The host runs everything in Docker — no Python/venv needed on the host at all. The
[Dockerfile](Dockerfile) installs `requirements.txt` into `python:3.13-slim` and runs the
app under **gunicorn with a single worker** (`--workers 1 --threads 4`). One worker is not
a performance choice but a correctness one: the alarm watcher thread and the subscription
store live in process memory, so a second worker would poll ThingsBoard independently and
send a **duplicate push for every alarm**. See
[docker-compose.pushbridge.yml](docker-compose.pushbridge.yml).

```bash
cp .env.example .env    # first time only — then edit .env (TB_USER/TB_PASS at minimum)
docker compose -f docker-compose.pushbridge.yml up -d --build
```

This builds the image fresh each deploy and (re)starts the `pushbridge` container, joined
to Kong's `kongapi_default` network with no host port published — `deploy.sh` does exactly
this after `git pull`.

> **State lives outside the image.** `/var/lib/pushbridge` is **bind-mounted** (not a named
> volume) so `vapid_private.pem` and `subscriptions.json` survive every rebuild. Losing
> the VAPID key invalidates **every existing browser subscription** — back this directory
> up.

## 2. Kong route
```bash
# Kong Admin API — check the actual mapped host port with `docker ps` (this host
# publishes it as 32768, not the default 8001).
ADMIN=http://localhost:32768

# service → the pushbridge container, reached by name over the kongapi_default network
# (port 7082 = gunicorn inside the container; the container publishes no host port)
curl -s -X POST $ADMIN/services \
  -d name=pushbridge -d url=http://pushbridge:7082

# route: nonthaburi.info/pwa_push/* → pushbridge  (STRIP — the app serves at the root)
curl -s -X POST $ADMIN/services/pushbridge/routes \
  -d 'paths[]=/pwa_push' -d strip_path=true -d 'hosts[]=nonthaburi.info'
```

> `strip_path=true` here (unlike the `/pwa_app` route): this app defines its endpoints at
> the root (`/register`, `/vapid-public-key`, `/unregister`, `/health`), so Kong removes
> the `/pwa_push` prefix before forwarding. The PWA only ever calls the prefixed URL, and
> that prefix is fixed — the frontend has it hardcoded, so it stays `/pwa_push` even
> though the service is now named `pushbridge`.

## 3. CI/CD
Pushing to `main` runs two GitHub Actions workflows:

| workflow | trigger | what it does |
| --- | --- | --- |
| [ci.yml](.github/workflows/ci.yml) | push + PR to `main` | `ruff check .`, then `docker build` to prove the image still builds |
| [deploy.yml](.github/workflows/deploy.yml) | push to `main` | SSH to the production host and run `/opt/pushbridge/deploy.sh` |

The deploy job uses the `Production` environment and the `SSH_HOST`, `SSH_USER`,
`SSH_PRIVATE_KEY` secrets. One-time host setup:

```bash
sudo git clone <repo-url> /opt/pushbridge
cd /opt/pushbridge
cp .env.example .env && "$EDITOR" .env    # not in git — create it once, by hand
```

## 4. Verify
```bash
docker compose -f docker-compose.pushbridge.yml logs -f pushbridge
curl -s https://nonthaburi.info/pwa_push/health            # {"ok":true,"subscriptions":N}
curl -s https://nonthaburi.info/pwa_push/vapid-public-key  # base64url app server key
```
- Open the PWA → Settings → toggle **แจ้งเตือน (push)** on → allow → `subscriptions` in
  `/health` goes up by one.
- Trigger a new alarm in ThingsBoard → push arrives within `POLL_SECONDS` even with the
  PWA closed (Android always; iOS 16.4+ only when installed to the home screen).

## Notes
- HTTPS is mandatory for Web Push. `nonthaburi.info` already has TLS (Kong), so serving
  under it satisfies this.
- Single instance by design — subscriptions and the alarm watermark are in-process plus a
  JSON file. Do not scale this service horizontally.
- On first successful poll the watcher sets a baseline and does **not** notify about
  pre-existing alarms; only alarms created afterward push.
- The PWA (`../dashboard-pwa`) must be deployed and routed at `/pwa_app` for the
  notification toggle to have anything to subscribe from.
- [pushbridge.service](pushbridge.service) and [DEPLOY_SERVICE.md](DEPLOY_SERVICE.md) are
  the Docker-free alternative. They are **not** used by this path — `deploy.sh` and the
  deploy workflow only ever drive the container.
