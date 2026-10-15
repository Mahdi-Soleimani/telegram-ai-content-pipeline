"""
Production entrypoint for the PaaS deployment.

Use this instead of `python app.py` on the server: it runs the same Flask
app behind waitress (a proper WSGI server) with exactly ONE worker thread.
That single-thread constraint is required -- see the `_lock` comment in
app.py: an in-progress Telethon login is bound to the thread that started
it, so this app must never run with more than one thread/worker at a time.

Run with:  python main.py
"""

import os

from waitress import serve

from app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    serve(app, host="0.0.0.0", port=port, threads=1)
