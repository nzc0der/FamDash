"""Web Push helpers for iOS-compatible Home Screen PWA notifications.

This module keeps the push delivery logic separate from the database helpers
and the Flask route layer.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

import settings

logger = logging.getLogger(__name__)

VAPID_SUBJECT = "mailto:family-dashboard@example.com"


def _ensure_vapid_keys() -> tuple[str, str]:
    """Return the configured VAPID private/public keys, generating them if needed."""
    cfg = settings.load_config()
    private_key = cfg.get("vapid_private_key")
    public_key = cfg.get("vapid_public_key")

    if not private_key or not public_key:
        vapid = Vapid02()
        vapid.generate_keys()
        private_key = vapid.private_pem().decode("utf-8")
        public_key = vapid.public_pem().decode("utf-8")
        cfg["vapid_private_key"] = private_key
        cfg["vapid_public_key"] = public_key
        settings.save_config(cfg)

    return private_key, public_key


def get_vapid_public_key_b64() -> str:
    """Return the VAPID public key in URL-safe base64 form for browser PushManager."""
    _, public_key_pem = _ensure_vapid_keys()
    public_key = load_pem_public_key(public_key_pem.encode("utf-8"))
    key_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(key_bytes).decode("ascii").rstrip("=")


def send_announcement_push(announcement_id: int) -> int:
    """Send a Web Push notification for one announcement.

    Steps:
    1. Load the announcement from SQLite.
    2. Load all stored push subscriptions.
    3. Send a push payload to each one.
    4. Remove invalid or expired subscriptions.
    """
    announcement = settings.get_announcement(announcement_id)
    if not announcement:
        logger.warning("Announcement %s not found. Skipping push.", announcement_id)
        return 0

    subscriptions = settings.get_push_subscriptions()
    if not subscriptions:
        return 0

    private_key, _ = _ensure_vapid_keys()
    payload = json.dumps(
        {
            "id": announcement_id,
            "title": announcement.get("title", "Family Dashboard"),
            "body": announcement.get("body", "New update"),
            "url": f"/announcements/{announcement_id}",
        },
        ensure_ascii=False,
    )

    sent = 0
    for subscription in subscriptions:
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
                vapid_private_key=Vapid02.from_pem(private_key.encode("utf-8")),
                vapid_claims={"sub": VAPID_SUBJECT},
                ttl=60,
                content_encoding="aes128gcm",
            )
            sent += 1
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code in {404, 410}:
                logger.warning(
                    "Removing invalid push subscription %s (status %s).",
                    subscription["id"],
                    status_code,
                )
                settings.delete_push_subscription(subscription["id"])
            else:
                logger.warning(
                    "Failed to send push to subscription %s: %s",
                    subscription["id"],
                    exc,
                )
        except Exception as exc:
            logger.warning(
                "Unexpected error sending push to subscription %s: %s",
                subscription["id"],
                exc,
            )

    return sent
