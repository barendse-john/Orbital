"""Web Push to the phone app (the PWA under /app).

Optional: without pywebpush installed everything else still works and the
app simply offers no notifications. VAPID keys are made on first start and
kept in the data folder, so subscriptions survive restarts - lose the key
and every phone has to re-enable notifications.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CONTACT = "https://github.com/barendse-john/orbital"


class Pusher:
    def __init__(self, data_dir: Path):
        self.enabled = False
        self.public_key = ""
        self.key_path = Path(data_dir) / "vapid_private.pem"
        try:
            from py_vapid import Vapid01
            from cryptography.hazmat.primitives import serialization
            import pywebpush  # noqa: F401
        except ImportError:
            log.warning("pywebpush not installed - app notifications disabled")
            return
        try:
            if not self.key_path.exists():
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                v = Vapid01()
                v.generate_keys()
                v.save_key(str(self.key_path))
                self.key_path.chmod(0o600)
                log.info("Created VAPID key for app notifications")
            v = Vapid01.from_file(str(self.key_path))
            raw = v.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint)
            self.public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
            self.enabled = True
        except Exception as exc:  # noqa: BLE001 - notifications are optional
            log.error("App notifications disabled: %s: %s", type(exc).__name__, exc)

    def _send_one(self, sub: dict, data: str) -> int:
        from pywebpush import WebPushException, webpush
        try:
            webpush(subscription_info=sub, data=data,
                    vapid_private_key=str(self.key_path),
                    vapid_claims={"sub": CONTACT}, ttl=6 * 3600, timeout=15)
            return 201
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", 0) or 0
            log.warning("Push to %s… failed (%s)", sub.get("endpoint", "")[:40], status)
            return status

    async def send(self, subs: list[dict], payload: dict) -> list[str]:
        """Send to every subscription; returns endpoints that are gone for
        good (404/410) so the caller can forget them."""
        if not self.enabled or not subs:
            return []
        data = json.dumps(payload)
        gone = []
        for sub in subs:
            try:
                status = await asyncio.to_thread(self._send_one, sub, data)
            except Exception as exc:  # noqa: BLE001 - one bad phone must not stop the rest
                log.warning("Push failed: %s: %s", type(exc).__name__, exc)
                continue
            if status in (404, 410):
                gone.append(sub["endpoint"])
        return gone
