"""
pushbridge — ThingsBoard alarm → Web Push bridge for the Nonthaburi PWA.

Flow:
  1. The PWA (nonthaburi.info/pwa_app) subscribes to Web Push and POSTs its
     subscription here (/register).
  2. A background watcher logs in to ThingsBoard, polls active alarms, and when a
     NEW alarm appears sends a Web Push to every registered subscription.

Served behind Kong under /pwa_push (strip_path=true), so this app sees /register,
/unregister, /vapid-public-key at the root.

Run:  gunicorn --workers 1 --threads 4 main:app    (production, see Dockerfile)
      python main.py                               (local development)
Config via environment / .env — see .env.example.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization
from flask import Flask, jsonify, request
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

# --------------------------------------------------------------------------- config
HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7082"))  # the push backend port (Kong upstream)
TB_HOST = os.environ.get("TB_HOST", "https://nonthaburi.info")
TB_USER = os.environ.get("TB_USER", "")
TB_PASS = os.environ.get("TB_PASS", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@nonthaburi.info")

DATA_DIR = Path(os.environ.get("DATA_DIR", ".")).resolve()
SUBS_FILE = DATA_DIR / "subscriptions.json"
VAPID_KEY_FILE = DATA_DIR / "vapid_private.pem"

SEVERITY_TH = {
    "CRITICAL": "วิกฤต",
    "MAJOR": "สูง",
    "MINOR": "กลาง",
    "WARNING": "เตือน",
    "INDETERMINATE": "ไม่ระบุ",
}

app = Flask(__name__)

# --------------------------------------------------------------------------- VAPID
def load_or_create_vapid() -> tuple[Vapid02, str]:
    """Return (vapid, application_server_key_base64url). Persists the private key."""
    if VAPID_KEY_FILE.exists():
        # from_file is a classmethod returning a NEW instance — calling it on an
        # existing one silently leaves that instance without a key.
        vapid = Vapid02.from_file(str(VAPID_KEY_FILE))
    else:
        vapid = Vapid02()
        vapid.generate_keys()
        vapid.save_key(str(VAPID_KEY_FILE))
        print(f"[vapid] generated new key -> {VAPID_KEY_FILE}")
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    app_server_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    # Log the key on every start: if this value ever changes, the persisted key was
    # lost and every existing browser subscription just became invalid.
    print(f"[vapid] app server key {app_server_key[:12]}…")
    return vapid, app_server_key


VAPID, APP_SERVER_KEY = load_or_create_vapid()

# --------------------------------------------------------------------------- subscriptions
_subs_lock = threading.Lock()


def load_subs() -> dict[str, dict]:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text("utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def save_subs(subs: dict[str, dict]) -> None:
    SUBS_FILE.write_text(json.dumps(subs, ensure_ascii=False), "utf-8")


# key subscriptions by endpoint (unique per browser install)
SUBS: dict[str, dict] = load_subs()

# --------------------------------------------------------------------------- HTTP API
@app.get("/vapid-public-key")
def vapid_public_key():
    return APP_SERVER_KEY, 200, {"Content-Type": "text/plain"}


@app.post("/register")
def register():
    sub = request.get_json(force=True, silent=True) or {}
    endpoint = sub.get("endpoint")
    if not endpoint or "keys" not in sub:
        return jsonify({"ok": False, "error": "bad subscription"}), 400
    with _subs_lock:
        SUBS[endpoint] = sub
        save_subs(SUBS)
    return jsonify({"ok": True})


@app.post("/unregister")
def unregister():
    body = request.get_json(force=True, silent=True) or {}
    endpoint = body.get("endpoint")
    with _subs_lock:
        if endpoint in SUBS:
            del SUBS[endpoint]
            save_subs(SUBS)
    return jsonify({"ok": True})


@app.get("/health")
def health():
    return jsonify({"ok": True, "subscriptions": len(SUBS)})


# --------------------------------------------------------------------------- push send
def send_push(payload: dict) -> None:
    """Send to every subscription; drop the ones the browser has expired (404/410)."""
    with _subs_lock:
        targets = list(SUBS.items())
    dead: list[str] = []
    for endpoint, sub in targets:
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=str(VAPID_KEY_FILE),
                vapid_claims={"sub": VAPID_SUBJECT},
                timeout=10,
            )
        except WebPushException as e:
            code = getattr(e.response, "status_code", None)
            if code in (404, 410):
                dead.append(endpoint)
            else:
                print(f"[push] error {code}: {e}")
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            print(f"[push] send failed: {e}")
    if dead:
        with _subs_lock:
            for endpoint in dead:
                SUBS.pop(endpoint, None)
            save_subs(SUBS)
        print(f"[push] removed {len(dead)} expired subscription(s)")


# --------------------------------------------------------------------------- TB watcher
class TB:
    """Minimal ThingsBoard REST client: login + refresh + active alarms."""

    def __init__(self) -> None:
        self.token = ""
        self.refresh = ""

    def login(self) -> None:
        r = requests.post(
            f"{TB_HOST}/api/auth/login",
            json={"username": TB_USER, "password": TB_PASS},
            timeout=15,
        )
        r.raise_for_status()
        j = r.json()
        self.token, self.refresh = j["token"], j["refreshToken"]

    def _refresh(self) -> bool:
        try:
            r = requests.post(
                f"{TB_HOST}/api/auth/token", json={"refreshToken": self.refresh}, timeout=15
            )
            if r.status_code == 200:
                j = r.json()
                self.token, self.refresh = j["token"], j["refreshToken"]
                return True
        except requests.RequestException:
            pass
        return False

    def active_alarms(self) -> list[dict]:
        url = (
            f"{TB_HOST}/api/v2/alarms?pageSize=100&page=0"
            "&sortProperty=createdTime&sortOrder=DESC&searchStatus=ACTIVE"
        )
        headers = {"X-Authorization": f"Bearer {self.token}"}
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 401:
            if not self._refresh():
                self.login()
            headers = {"X-Authorization": f"Bearer {self.token}"}
            r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json().get("data", [])


def watcher_loop() -> None:
    tb = TB()
    watermark = 0  # createdTime of the newest alarm we've already notified about
    primed = False
    while True:
        try:
            if not tb.token:
                tb.login()
            alarms = tb.active_alarms()
            if alarms:
                newest = max(a["createdTime"] for a in alarms)
                if not primed:
                    # First successful poll — set the baseline, don't notify existing alarms.
                    watermark = newest
                    primed = True
                else:
                    fresh = sorted(
                        (a for a in alarms if a["createdTime"] > watermark),
                        key=lambda a: a["createdTime"],
                    )
                    for a in fresh:
                        sev = SEVERITY_TH.get(a.get("severity", ""), a.get("severity", ""))
                        send_push(
                            {
                                "title": f"⚠ {sev} · {a.get('type', 'alarm')}",
                                "body": a.get("originatorName", ""),
                                "url": "/pwa_app/alarms",
                                "tag": a["id"]["id"],
                            }
                        )
                    if fresh:
                        watermark = newest
            elif not primed:
                primed = True  # no alarms at all — baseline is "nothing"
        except Exception as e:  # noqa: BLE001 - never let the watcher die
            print(f"[watcher] {e}")
            tb.token = ""  # force re-login next round
        time.sleep(POLL_SECONDS)


def start_watcher() -> None:
    """Start the background alarm watcher. Called once per process, at import time."""
    if not TB_USER or not TB_PASS:
        raise SystemExit("TB_USER / TB_PASS must be set (see .env.example)")
    threading.Thread(target=watcher_loop, daemon=True).start()


# Started on import so the watcher also runs under a WSGI server (gunicorn), which
# loads `main:app` directly and never calls main(). Safe only with a single worker —
# see the Dockerfile.
start_watcher()


def main() -> None:
    """Development entry point: the Flask built-in server instead of gunicorn."""
    print(f"[pushbridge] listening on {HOST}:{PORT}  (poll {POLL_SECONDS}s)")
    app.run(host=HOST, port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()