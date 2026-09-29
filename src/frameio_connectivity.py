from __future__ import annotations

import os
import sys
from typing import Any

import requests

IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_BASE_URL = "https://api.frame.io/v4"
FRAMEIO_SCOPE = "openid AdobeID frame.s2s.all"


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_access_token(client_id: str, client_secret: str) -> str:
    response = requests.post(
        IMS_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
            "scope": FRAMEIO_SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("Adobe IMS did not return an access_token.")
    return token


def frameio_get(token: str, path: str) -> Any:
    response = requests.get(
        f"{FRAMEIO_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=45,
    )
    response.raise_for_status()
    return response.json()


def count_records(payload: Any) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("data", "accounts", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        return 1 if payload else 0
    return 0


def main() -> int:
    client_id = require_env("FRAMEIO_CLIENT_ID")
    client_secret = require_env("FRAMEIO_CLIENT_SECRET")

    token = get_access_token(client_id, client_secret)
    me = frameio_get(token, "/me")
    accounts = frameio_get(token, "/accounts")

    me_ok = bool(me)
    account_count = count_records(accounts)

    print("=" * 72)
    print("VIDEO QC AUTOMATION - FRAME.IO S2S CONNECTIVITY")
    print("=" * 72)
    print("Authentication            : SUCCESS")
    print(f"/v4/me response           : {'SUCCESS' if me_ok else 'EMPTY'}")
    print(f"Accessible account records: {account_count}")
    print("Sensitive values printed  : NONE")
    print("Frame.io writes made      : NONE")
    print("=" * 72)

    if not me_ok:
        raise RuntimeError("/v4/me returned an empty response.")
    if account_count < 1:
        raise RuntimeError("No accessible Frame.io accounts were returned.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        body = ""
        if exc.response is not None:
            try:
                payload = exc.response.json()
                if isinstance(payload, dict):
                    body = str(payload.get("error") or payload.get("message") or "")
            except Exception:
                body = ""
        suffix = f" | {body[:250]}" if body else ""
        print(f"ERROR: HTTP {status}{suffix}", file=sys.stderr)
        raise
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
