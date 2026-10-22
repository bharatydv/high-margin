"""FPFX account endpoints."""

from datetime import datetime

import requests

BASE = "https://fdlapiprod.fpfxtech.io"
OVERVIEW = BASE + "/get/account/overview/{login_id}"
ALL_TRADES = BASE + "/get/account/all-trades/{login_id}"
PAYOUTS = BASE + "/get/payouts/{login_id}"

TIMEOUT = 45


class ApiError(RuntimeError):
    pass


def _get(url, headers=None):
    try:
        response = requests.get(url, timeout=TIMEOUT, headers=headers or {})
    except requests.RequestException as exc:
        raise ApiError(f"Request to {url} failed: {exc}") from exc

    if response.status_code != 200:
        snippet = response.text.strip()[:300].replace("\n", " ")
        raise ApiError(f"{url} returned HTTP {response.status_code}. {snippet}")

    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(f"{url} did not return JSON (content-type "
                       f"{response.headers.get('content-type')!r}).") from exc


def _unwrap_records(payload):
    """Return the list of records from a payload that may be a bare list or wrapped."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "trades", "result", "results", "records", "items",
                    "allTrades", "all_trades", "rows", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = _unwrap_records(value)
                if nested:
                    return nested
        # single record returned bare
        if any(isinstance(v, (str, int, float)) for v in payload.values()):
            return [payload]
    return []


def _unwrap_object(payload):
    """Return the single object from a payload that may be wrapped in data/result."""
    if isinstance(payload, dict):
        for key in ("data", "result", "account", "overview", "response"):
            value = payload.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
        return payload
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    return {}


def fetch_overview(login_id, headers=None):
    """(overview_dict, raw_payload)"""
    raw = _get(OVERVIEW.format(login_id=login_id), headers)
    return _unwrap_object(raw), raw


def fetch_all_trades(login_id, headers=None):
    """(list_of_trade_dicts, raw_payload)"""
    raw = _get(ALL_TRADES.format(login_id=login_id), headers)
    return [r for r in _unwrap_records(raw) if isinstance(r, dict)], raw


def fetch_payouts(login_id, headers=None):
    """(list_of_payout_dicts, raw_payload) for an account login."""
    raw = _get(PAYOUTS.format(login_id=login_id), headers)
    return [r for r in _unwrap_records(raw) if isinstance(r, dict)], raw


def latest_approved_date(payouts):
    """Return the newest approved payout datetime, or None when there is none."""
    latest = None
    for payout in payouts:
        status = str(payout.get("status", "")).strip().casefold()
        value = payout.get("approvedDate", payout.get("approved_date"))
        if status != "approved" or not value:
            continue
        try:
            approved_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if latest is None or approved_at > latest:
            latest = approved_at
    return latest
