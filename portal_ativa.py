import os

import requests


def bridge(path, payload, timeout=10):
    base = os.environ.get("ATIVA_PORTAL_URL", "").strip().rstrip("/")
    token = os.environ.get("ATIVA_BRIDGE_TOKEN", "").strip()
    if not base or not token:
        return None
    response = requests.post(
        f"{base}/api/ingest/ativa/{path}", json=payload,
        headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
    )
    response.raise_for_status()
    return response.json()
