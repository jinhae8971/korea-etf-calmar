"""L4-3 자금조달 스트레스.

SA 를 죽인 건 주가가 아니라 마진콜이었다. 가격보다 먼저 움직이는
크레딧 스프레드를 별도 계층으로 둔다.

FRED 는 일부 IP 에서 503 을 반환하므로 3회 연속 실패 시 서킷 브레이커를
열고 캐시로 운영한다. (7일마다 복구 시도)
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timedelta

from .http_client import fetch

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}"
BREAKER_MAX_FAILS = 3
BREAKER_RETRY_DAYS = 7


def _load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _breaker_open(state: dict) -> bool:
    if state.get("fails", 0) < BREAKER_MAX_FAILS:
        return False
    opened = state.get("opened_at")
    if not opened:
        return True
    try:
        when = datetime.fromisoformat(opened)
    except ValueError:
        return True
    return datetime.utcnow() - when < timedelta(days=BREAKER_RETRY_DAYS)


def _fetch_series(series_id: str) -> dict[str, float]:
    raw = fetch(FRED_CSV.format(sid=series_id)).decode("utf-8", "replace")
    reader = csv.reader(io.StringIO(raw))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise ValueError("unexpected FRED csv")
    out: dict[str, float] = {}
    for row in reader:
        if len(row) < 2 or row[1] in (".", ""):
            continue
        try:
            out[row[0]] = float(row[1])
        except ValueError:
            continue
    if not out:
        raise ValueError("empty FRED series")
    return out


def collect(series_map: dict[str, str], cache_path: str, thresholds: dict | None = None) -> dict:
    th = thresholds or {}
    state = _load_state(cache_path)
    cached = state.get("values", {})
    breaker = _breaker_open(state)
    values: dict[str, dict] = {}
    live = False

    for key, series_id in series_map.items():
        if breaker:
            if key in cached:
                values[key] = dict(cached[key], stale=True)
            continue
        try:
            series = _fetch_series(series_id)
            last_date = max(series)
            hist = [series[d] for d in sorted(series)]
            values[key] = {
                "value": series[last_date],
                "date": last_date,
                "change_20d": round(hist[-1] - hist[-21], 3) if len(hist) > 21 else None,
                "stale": False,
            }
            live = True
        except Exception:  # noqa: BLE001
            if key in cached:
                values[key] = dict(cached[key], stale=True)

    if live:
        state["fails"] = 0
        state.pop("opened_at", None)
    elif not breaker:
        state["fails"] = state.get("fails", 0) + 1
        if state["fails"] >= BREAKER_MAX_FAILS:
            state["opened_at"] = datetime.utcnow().isoformat(timespec="seconds")

    state["values"] = {k: {kk: vv for kk, vv in v.items() if kk != "stale"} for k, v in values.items()} or cached
    _save_state(cache_path, state)

    hy = (values.get("hy_oas") or {}).get("value")
    watch, alert = th.get("hy_oas_watch", 4.0), th.get("hy_oas_alert", 5.0)
    if hy is None:
        level = "NO_DATA"
    elif hy >= alert:
        level = "ALERT"
    elif hy >= watch:
        level = "WATCH"
    else:
        level = "NORMAL"

    return {
        "level": level,
        "values": values,
        "mode": "CIRCUIT_OPEN" if breaker else ("OK" if live else "DEGRADED"),
    }
