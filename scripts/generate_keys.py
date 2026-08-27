from __future__ import annotations

import base64
import secrets

for name in ("APP_ENCRYPTION_KEY", "LOOKUP_HMAC_KEY", "BACKUP_ENCRYPTION_KEY"):
    print(f"{name}={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}")
