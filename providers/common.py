"""Shared HTTP for adapters. Always sends a User-Agent (estate lesson: some
providers 403 bare clients silently). Returns (json_or_none, http_status)."""
import httpx

import config

def request_json(method: str, url: str, *, headers=None, json_body=None, params=None):
    h = {"User-Agent": config.USER_AGENT, "Accept": "application/json"}
    if headers:
        h.update(headers)
    with httpx.Client(timeout=config.HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = client.request(method, url, headers=h, json=json_body, params=params)
        status = resp.status_code
        try:
            return resp.json(), status
        except Exception:
            return None, status
