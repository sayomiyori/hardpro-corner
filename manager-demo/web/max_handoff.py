"""Send an explicitly confirmed storefront question to the configured MAX chat."""
from contextlib import closing
import hashlib
from http.client import HTTPSConnection, HTTPException as TransportError
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import time
from urllib.parse import urlencode

MAX_HOST = "platform-api2.max.ru"
REQUEST_LIMIT = 8
REQUEST_WINDOW = 60
RECEIPT_LIFETIME = 86_400


def configured() -> bool:
    chat = os.environ.get("MAX_SUPPORT_CHAT_ID", "")
    config = os.environ.get("MAX_SUPPORT_STATE", "")
    token = os.environ.get("MAX_SUPPORT_TOKEN", "")
    return bool(re.fullmatch(r"-?[1-9][0-9]{0,18}", chat) and token and config
                and Path(config).is_absolute() and Path(config).is_dir())


def send_to_max(message: str) -> None:
    context = ssl.create_default_context()
    if ca := os.environ.get("MAX_SUPPORT_CA_FILE"):
        context.load_verify_locations(cafile=ca)
    conn = HTTPSConnection(MAX_HOST, timeout=7, context=context)
    try:
        path = "/messages?" + urlencode({"chat_id": os.environ["MAX_SUPPORT_CHAT_ID"]})
        conn.request("POST", path, json.dumps({"text": message}, ensure_ascii=False).encode(),
                     {"Authorization": os.environ["MAX_SUPPORT_TOKEN"], "Content-Type": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raise OSError("MAX did not confirm the message")
        data = json.loads(response.read(65_537))
        message_id = data.get("message", {}).get("body", {}).get("mid")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Missing message ID")
    finally:
        conn.close()


def dispatch(request_id: str, details: list[str]) -> tuple[str, str]:
    now = int(time.time())
    fingerprint = hashlib.sha256("\n".join(details).encode()).hexdigest()
    receipt = "HP-" + request_id[:12].upper()
    db_path = Path(os.environ["MAX_SUPPORT_STATE"]) / "receipts.sqlite3"
    # The service keeps delivery receipts, never the question or the MAX token.
    with closing(sqlite3.connect(db_path, timeout=4)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS receipts ("
                   "id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created INTEGER NOT NULL, status TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM receipts WHERE created < ?", (now - RECEIPT_LIFETIME,))
        existing = db.execute("SELECT fingerprint, status FROM receipts WHERE id = ?", (request_id,)).fetchone()
        if existing:
            return (existing[1], receipt) if existing[0] == fingerprint else ("changed", receipt)
        recent = db.execute("SELECT COUNT(*) FROM receipts WHERE created >= ?", (now - REQUEST_WINDOW,)).fetchone()[0]
        if recent >= REQUEST_LIMIT:
            return "busy", receipt
        db.execute("INSERT INTO receipts VALUES (?, ?, ?, 'pending')", (request_id, fingerprint, now))

    status = "delivered"
    try:
        send_to_max("\n".join([f"Вопрос с сайта · {receipt}", *details]))
    except (OSError, TransportError, ValueError, AttributeError, TypeError):
        # An interrupted response may follow a successful send. Never retry an ambiguous delivery.
        status = "unconfirmed"
    with closing(sqlite3.connect(db_path, timeout=4)) as db, db:
        db.execute("UPDATE receipts SET status = ? WHERE id = ?", (status, request_id))
    return status, receipt
