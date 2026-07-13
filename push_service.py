"""Web Push helpers for family dashboard push notifications.

This module keeps the push delivery logic separate from the database helpers
and the Flask route layer.

Design rules
------------
* No I/O or side-effects at import time.
* All public helpers are safe to call from any context (Flask request thread,
  background thread, tests).
* Heavy push delivery is offloaded to a daemon thread so it never blocks an
  HTTP response.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

import settings

logger = logging.getLogger(__name__)

VAPID_SUBJECT = "mailto:family-dashboard@example.com"


# ---------------------------------------------------------------------------
# VAPID key helpers
# ---------------------------------------------------------------------------


def _ensure_vapid_keys() -> tuple[str, str]:
    """Return the configured VAPID private/public PEM keys.

    If the config does not already contain keys they are generated, stored,
    and returned.  Returns (private_pem, public_pem) as str.
    """
    cfg = settings.load_config()
    private_key = cfg.get("vapid_private_key", "").strip()
    public_key = cfg.get("vapid_public_key", "").strip()

    if not private_key or not public_key:
        logger.info("No VAPID keys found – generating new pair.")
        vapid = Vapid02()
        vapid.generate_keys()
        # private_pem() returns bytes
        private_key = vapid.private_pem().decode("utf-8")
        public_key = vapid.public_pem().decode("utf-8")
        cfg["vapid_private_key"] = private_key
        cfg["vapid_public_key"] = public_key
        settings.save_config(cfg)
        logger.info("New VAPID keys generated and saved.")

    return private_key, public_key


def get_vapid_public_key_b64() -> str:
    """Return the VAPID public key in URL-safe base64 for the browser PushManager.

    The browser expects the *uncompressed point* (raw) format of the EC public
    key encoded as URL-safe base64 without padding (the "applicationServerKey"
    format).
    """
    _, public_key_pem = _ensure_vapid_keys()
    public_key = load_pem_public_key(public_key_pem.encode("utf-8"))
    key_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(key_bytes).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# Core delivery helper
# ---------------------------------------------------------------------------


def _send_to_subscription(
    subscription: dict[str, Any],
    payload: str,
    vapid: Vapid02,
) -> bool:
    """Attempt to send *payload* to a single push subscription.

    Returns True on success.  Removes the subscription from the database
    if the push service returns 404 or 410 (endpoint gone / expired).
    Returns False on any failure without raising.
    """
    subscription_info: dict[str, Any] = {
        "endpoint": subscription["endpoint"],
        "keys": {
            "p256dh": subscription["p256dh"],
            "auth": subscription["auth"],
        },
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=vapid,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=86400,  # 24 hours – notification survives a device sleep cycle
            content_encoding="aes128gcm",
            timeout=10.0,
        )
        return True
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in {404, 410}:
            logger.warning(
                "Removing expired/invalid push subscription %s (HTTP %s).",
                subscription["id"],
                status_code,
            )
            try:
                settings.delete_push_subscription(subscription["id"])
            except Exception as del_exc:
                logger.error("Could not delete subscription %s: %s", subscription["id"], del_exc)
        else:
            logger.warning(
                "Push to subscription %s failed: %s",
                subscription["id"],
                exc,
            )
        return False
    except Exception as exc:
        logger.warning(
            "Unexpected error sending push to subscription %s: %s",
            subscription["id"],
            exc,
        )
        return False


def _deliver_push(payload: str) -> int:
    """Send *payload* to every stored push subscription.

    Returns the number of successful deliveries.  Meant to be run from a
    background thread so it does not block HTTP responses.
    """
    subscriptions = settings.get_push_subscriptions()
    if not subscriptions:
        logger.debug("No push subscriptions registered – nothing to send.")
        return 0

    private_key_pem, _ = _ensure_vapid_keys()
    # Build the Vapid02 object once; reuse for every subscription.
    vapid = Vapid02.from_pem(private_key_pem.encode("utf-8"))

    sent = 0
    for subscription in subscriptions:
        if _send_to_subscription(subscription, payload, vapid):
            sent += 1

    logger.info("Push delivery complete: %d/%d sent.", sent, len(subscriptions))
    return sent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_announcement_push(announcement_id: int) -> None:
    """Send a push notification for *announcement_id* to all subscribers.

    The actual delivery is offloaded to a daemon background thread so that the
    calling Flask request handler is not blocked.

    Parameters
    ----------
    announcement_id:
        Primary key of the announcement in the database.
    """
    announcement = settings.get_announcement(announcement_id)
    if not announcement:
        logger.warning(
            "Announcement %s not found – push notification skipped.",
            announcement_id,
        )
        return

    payload = json.dumps(
        {
            "id": announcement_id,
            "title": announcement.get("title", "Family Dashboard"),
            "body": announcement.get("body", "New announcement"),
            "url": f"/board#announcement-{announcement_id}",
        },
        ensure_ascii=False,
    )

    thread = threading.Thread(
        target=_deliver_push,
        args=(payload,),
        name=f"PushAnnouncement-{announcement_id}",
        daemon=True,
    )
    thread.start()
    logger.info(
        "Push delivery thread started for announcement %s.", announcement_id
    )


def send_push_to_all(title: str, body: str, url: str = "/board") -> None:
    """Send an arbitrary push notification to all subscribers.

    Runs in a background thread.

    Parameters
    ----------
    title:
        Notification title shown by the OS.
    body:
        Notification body text.
    url:
        URL to open when the user clicks the notification.
    """
    payload = json.dumps(
        {"title": title, "body": body, "url": url},
        ensure_ascii=False,
    )

    thread = threading.Thread(
        target=_deliver_push,
        args=(payload,),
        name="PushToAll",
        daemon=True,
    )
    thread.start()
    logger.info("Push-to-all thread started: %s", title)
