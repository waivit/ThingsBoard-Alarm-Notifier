# Deploy (systemd) — pushbridge

The Docker-free alternative: the app runs directly on the host in a virtualenv, supervised
by systemd. Use this only when the host has no Docker — [DEPLOY.md](DEPLOY.md) is the
supported path.

> **No pipeline here.** CI/CD only drives the container: [deploy.sh](deploy.sh) runs
> `docker compose`, and the deploy workflow just calls that script. On this path every
> update is manual (see [4. Update](#4-update)). Do not point the deploy workflow at a
> systemd host.

Compared to the container, this path has one real drawback: the app must listen on an
address Kong can reach from **inside its own container**, so port 7082 ends up bound on
the host rather than being invisible on a private Docker network. Firewall it.

## 1. Install
```bash
sudo git clone <repo-url> /opt/pushbridge
sudo chown -R "$USER" /opt/pushbridge
cd /opt/pushbridge

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env && "$EDITOR" .env    # not in git — create it once, by hand
```

Create a **dedicated ThingsBoard user** for the watcher (read access to alarms) and put
its credentials in `.env` — the app refuses to start without `TB_USER` / `TB_PASS`.

Two values in `.env` matter more here than under Docker, because nothing overrides them:

| variable | value | why |
| --- | --- | --- |
| `BIND_HOST` | `0.0.0.0` | Kong runs in a container; `127.0.0.1` would only be reachable from the host itself, not from inside Kong. See §2. |
| `DATA_DIR` | `/var/lib/pushbridge` | `vapid_private.pem` + `subscriptions.json` live here. The unit's `StateDirectory=pushbridge` creates it with the right ownership. |

> **Back up `DATA_DIR`.** Losing `vapid_private.pem` invalidates **every existing browser
> subscription** — every user has to toggle notifications on again, with no error shown
> anywhere.

## 2. Run as a service
[pushbridge.service](pushbridge.service) runs `main.py` under the venv, restarts on
failure, and creates `/var/lib/pushbridge` via `StateDirectory`.

```bash
sudo cp pushbridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pushbridge
journalctl -u pushbridge -f
```

`main.py` serves through Flask's built-in server on this path. That is fine for the load
this service sees (a handful of subscribe/unsubscribe calls); to use gunicorn instead —
matching what the container does — change `ExecStart` to:

```ini
ExecStart=/opt/pushbridge/.venv/bin/gunicorn --bind 0.0.0.0:7082 --workers 1 --threads 4 main:app
```

**Keep `--workers 1`.** The alarm watcher thread and the subscription store live in
process memory, so a second worker polls ThingsBoard independently and sends a duplicate
push for every alarm.

## 3. Kong route
```bash
# Kong Admin API — check the actual mapped host port with `docker ps` (this host
# publishes it as 32768, not the default 8001).
ADMIN=http://localhost:32768
```

The upstream is the part that differs from the Docker path. Kong runs inside a container,
so `127.0.0.1` there means *Kong itself*, not the host — the upstream must be the host's
address on the Docker bridge:

```bash
ip -4 addr show docker0        # typically 172.17.0.1
HOST_IP=172.17.0.1

curl -s -X POST $ADMIN/services \
  -d name=pushbridge -d "url=http://$HOST_IP:7082"

# route: nonthaburi.info/pwa_push/* → pushbridge  (STRIP — the app serves at the root)
curl -s -X POST $ADMIN/services/pushbridge/routes \
  -d 'paths[]=/pwa_push' -d strip_path=true -d 'hosts[]=nonthaburi.info'
```

Verify the address before relying on it — a host that runs Kong with `network_mode: host`,
or on a custom bridge, needs a different one. From inside the Kong container:

```bash
docker exec <kong-container> curl -s http://172.17.0.1:7082/health
```

> `strip_path=true` (unlike the `/pwa_app` route): this app defines its endpoints at the
> root (`/register`, `/vapid-public-key`, `/unregister`, `/health`), so Kong removes the
> `/pwa_push` prefix before forwarding. The PWA only ever calls the prefixed URL, and that
> prefix is hardcoded in the frontend — it stays `/pwa_push` regardless of this path.

Because 7082 is now open on the host, restrict it to the Docker bridge:

```bash
sudo ufw allow in on docker0 to any port 7082
sudo ufw deny 7082
```

## 4. Update
There is no auto-deploy on this path. To pick up a new commit:

```bash
cd /opt/pushbridge
git pull origin main
.venv/bin/pip install -r requirements.txt    # only if requirements.txt changed
sudo systemctl restart pushbridge
```

## 5. Verify
```bash
systemctl status pushbridge
journalctl -u pushbridge -f
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
  JSON file. Do not run a second copy of this service; both would poll and both would push.
- On first successful poll the watcher sets a baseline and does **not** notify about
  pre-existing alarms; only alarms created afterward push.
- The PWA (`../dashboard-pwa`) must be deployed and routed at `/pwa_app` for the
  notification toggle to have anything to subscribe from.
