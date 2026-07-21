# alarm-push

ThingsBoard active-alarm → **Web Push** bridge for the Nonthaburi PWA. Polls
`/api/v2/alarms` and pushes a notification to every subscribed browser when a **new**
alarm appears — so the PWA alerts even when it's closed.

```
PWA (/pwa_app) ──subscribe──► alarm-push ──poll──► ThingsBoard
      ▲                            │
      └──────── Web Push ──────────┘  (via the browser's push service)
```

## Setup (Ubuntu, same box as ThingsBoard)

```bash
sudo mkdir -p /opt/alarm-push && sudo chown $USER /opt/alarm-push
cp -r * /opt/alarm-push/ && cd /opt/alarm-push
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then edit .env (TB_USER/TB_PASS at minimum)
```

Create a **dedicated ThingsBoard user** for the watcher (read access to alarms) and put
its credentials in `.env`. The VAPID keypair is generated automatically on first run and
saved to `DATA_DIR/vapid_private.pem` — **back it up**; regenerating it invalidates every
existing subscription.

### Run as a service
```bash
sudo cp alarm-push.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alarm-push
journalctl -u alarm-push -f
```

## Kong route (same-origin as the PWA)

The PWA calls `/pwa_push/...` on `nonthaburi.info`. Route that prefix to this service and
**strip the prefix** so the app sees `/register`, `/vapid-public-key`, `/unregister`:

```bash
# service → this backend
curl -s -X POST http://localhost:8001/services \
  -d name=alarm-push -d url=http://127.0.0.1:7082

# route: nonthaburi.info/pwa_push/* → alarm-push (strip /pwa_push)
curl -s -X POST http://localhost:8001/services/alarm-push/routes \
  -d 'paths[]=/pwa_push' -d strip_path=true -d 'hosts[]=nonthaburi.info'
```

## Endpoints (as seen by this app, after Kong strips the prefix)
| method | path | purpose |
| --- | --- | --- |
| GET | `/vapid-public-key` | app-server key the browser needs to subscribe |
| POST | `/register` | body = PushSubscription JSON |
| POST | `/unregister` | body = `{ "endpoint": "..." }` |
| GET | `/health` | `{ ok, subscriptions }` |

## Notes
- Single instance (subscriptions + watermark are in-process / a JSON file). Fine for one
  small deployment.
- On first successful poll it sets a baseline and does **not** notify pre-existing alarms;
  only alarms created afterward push.
- Expired subscriptions (browser returns 404/410) are pruned automatically.