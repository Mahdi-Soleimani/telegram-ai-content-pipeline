"""
================================================================================
 SESSION STRING GENERATOR -- internal web tool
================================================================================
Small internal web app that wraps the interactive Telethon login flow (the
same thing generate_session.py did on the command line) behind a simple
browser UI: enter phone number -> enter login code -> (enter 2FA password if
the account has one) -> get the SESSION_STRING in a copyable box.

This does the exact same Telegram login Telethon always does; nothing about
the account, the flow, or the resulting string changes -- only the interface
around it. The output is a long secret string that grants full login access
to the Telegram account it was generated for, equivalent to a password. Treat
it, and this tool's access, accordingly:
  - Put this behind a login/IP-restriction if the PaaS platform supports it,
    or at minimum don't share the URL beyond the person who needs it.
  - Never commit a generated SESSION_STRING to source control or chat.
  - This app holds an in-progress Telegram login (an open, authenticated
    connection) in memory for up to SESSION_TTL_MINUTES per browser session;
    it is not meant to be left running/public indefinitely -- only up while
    someone is actively generating a string.

For local testing only, run:  python app.py
On a real server (Render, etc.) always run through main.py / waitress --
see main.py for why (single-thread requirement).

Needs a .env (or platform env vars) with API_ID and API_HASH -- the same
values main.py uses, from https://my.telegram.org.
================================================================================
"""

import asyncio
import logging
import os
import sys
import uuid
import threading
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("session-generator")

load_dotenv()

# Fail loudly and clearly if the required secrets aren't set, instead of a
# bare traceback -- this is the #1 cause of "the site just 502s": the app
# process crashes on import before it ever binds to the port, so the PaaS
# platform's proxy has nothing to talk to and shows a generic 502.
# Check your platform's dashboard -> environment variables if you see this
# in the logs.
try:
    API_ID = int(os.environ["API_ID"])
    API_HASH = os.environ["API_HASH"]
except KeyError as exc:
    log.error(
        "Missing required environment variable: %s. "
        "Set API_ID and API_HASH (from https://my.telegram.org) in your "
        "platform's environment variable settings before the app can start.",
        exc,
    )
    sys.exit(1)
except ValueError:
    log.error("API_ID must be an integer. Check the value you set for it.")
    sys.exit(1)

SESSION_TTL_MINUTES = int(os.getenv("SESSION_TTL_MINUTES", "10"))

# How long Telethon will wait on network calls to Telegram before giving up.
# Without this, a slow/unreachable connection from the server can hang the
# single request-handling thread indefinitely, which -- combined with
# threads=1 in main.py -- freezes the whole app for every visitor until the
# platform's proxy times out and shows a 502/504. Fail fast instead, with a
# clear error message the front-end can show.
TELETHON_TIMEOUT_SECONDS = int(os.getenv("TELETHON_TIMEOUT_SECONDS", "15"))
TELETHON_CONNECTION_RETRIES = int(os.getenv("TELETHON_CONNECTION_RETRIES", "2"))

app = Flask(__name__)

# In-memory store of in-progress logins, keyed by a random id handed to the
# browser. Guarded by a single lock: Telethon's `sync` wrapper binds a client
# to the event loop of the thread that created it, so every operation on a
# given client must run from that same thread. Serializing all requests
# behind one lock keeps that guarantee trivially true without needing a
# thread-affinity scheme -- fine here since this tool has a handful of users
# at most, never real concurrent load.
_lock = threading.Lock()
_sessions: dict = {}


def _cleanup_expired() -> None:
    cutoff = datetime.utcnow() - timedelta(minutes=SESSION_TTL_MINUTES)
    expired = [sid for sid, s in _sessions.items() if s["created"] < cutoff]
    for sid in expired:
        try:
            _sessions[sid]["client"].disconnect()
        except Exception:
            pass
        _sessions.pop(sid, None)


def _ensure_event_loop() -> None:
    # Python only auto-creates an asyncio event loop for the *main* thread.
    # waitress runs every request in a worker thread (e.g. "waitress-0"),
    # which never gets that automatic loop -- so the first time Telethon's
    # sync wrapper asks for one, it hits "There is no current event loop in
    # thread ...". Since main.py pins waitress to a single thread, this only
    # ever needs to run once per process; calling it defensively before every
    # Telethon operation is cheap and keeps this correct even if that ever
    # changes.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _new_client() -> TelegramClient:
    _ensure_event_loop()
    return TelegramClient(
        StringSession(),
        API_ID,
        API_HASH,
        timeout=TELETHON_TIMEOUT_SECONDS,
        connection_retries=TELETHON_CONNECTION_RETRIES,
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    # Cheap liveness probe that never touches Telethon/network -- point your
    # platform's health check here if it supports a custom path, so a slow
    # Telegram connection can never be mistaken for the app being down.
    return jsonify(status="ok")


@app.route("/api/send-code", methods=["POST"])
def send_code():
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    if not phone:
        return jsonify(error="شماره تلفن را وارد کنید."), 400

    with _lock:
        _cleanup_expired()
        client = _new_client()
        try:
            client.connect()
            sent = client.send_code_request(phone)
        except FloodWaitError as exc:
            return jsonify(error=f"تلگرام موقتاً محدود کرده. {exc.seconds} ثانیه دیگر دوباره امتحان کنید."), 429
        except PhoneNumberInvalidError:
            return jsonify(error="شماره تلفن نامعتبر است. با کد کشور کامل وارد کنید، مثل ۹۸۹...+."), 400
        except Exception as exc:
            log.exception("send_code failed")
            return jsonify(error=f"ارسال کد ناموفق بود: {exc}"), 500

        session_id = uuid.uuid4().hex
        _sessions[session_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "created": datetime.utcnow(),
        }

    return jsonify(session_id=session_id)


@app.route("/api/verify", methods=["POST"])
def verify():
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id")
    code = (data.get("code") or "").strip()
    password = data.get("password")

    with _lock:
        state = _sessions.get(session_id)
        if not state:
            return jsonify(error="نشست منقضی شده. از ابتدا شروع کنید."), 400

        _ensure_event_loop()
        client = state["client"]
        try:
            if password:
                client.sign_in(password=password)
            else:
                if not code:
                    return jsonify(error="کد را وارد کنید."), 400
                client.sign_in(
                    phone=state["phone"],
                    code=code,
                    phone_code_hash=state["phone_code_hash"],
                )
        except SessionPasswordNeededError:
            return jsonify(need_password=True)
        except PhoneCodeInvalidError:
            return jsonify(error="کد وارد شده اشتباه است."), 400
        except PhoneCodeExpiredError:
            return jsonify(error="کد منقضی شده. از ابتدا شروع کنید."), 400
        except Exception as exc:
            log.exception("verify failed")
            return jsonify(error=f"تایید ناموفق بود: {exc}"), 500

        session_string = client.session.save()
        try:
            client.disconnect()
        except Exception:
            pass
        _sessions.pop(session_id, None)

    return jsonify(session_string=session_string)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    # threaded=False on purpose: see the _lock comment above -- every
    # in-progress Telethon login must stay on one thread. This is fine for
    # local testing; on a real server always go through main.py + waitress
    # instead (Flask's dev server is not meant for production).
    app.run(host="0.0.0.0", port=port, threaded=False)
