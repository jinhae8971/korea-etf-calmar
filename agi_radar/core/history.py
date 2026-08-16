"""지표 시계열 누적.

대시보드 추이 차트와 주간 리뷰는 둘 다 '과거 판정 이력'을 필요로 한다.
latest.json 은 스냅샷이라 이력이 없으므로, 매 실행마다 압축 레코드를
docs/data/history.json 에 누적한다. (같은 종가일은 덮어쓴다 — 멱등)
"""

from __future__ import annotations

import json
import os

MAX_RECORDS = 400


def _load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def compact(report: dict) -> dict:
    hedge = report.get("hedge") or {}
    crowd = report.get("crowding") or {}
    fund = report.get("funding") or {}
    longs = [n for n in (report.get("nodes") or []) if n.get("role") == "long"]
    hy = (fund.get("values") or {}).get("hy_oas") or {}
    return {
        "d": report.get("as_of_close"),
        "state": (report.get("verdict") or {}).get("final_state"),
        "score": (report.get("rule_verdict") or {}).get("score"),
        "hedge": hedge.get("status"),
        "corr": hedge.get("corr20"),
        "ratio": hedge.get("spread_vol_ratio"),
        "both_lose": hedge.get("both_legs_lose"),
        "spread": hedge.get("spread_return"),
        "crowd": crowd.get("score"),
        "crowd_lv": crowd.get("level"),
        "crowd_watch": (crowd.get("cutoffs") or {}).get("watch"),
        "crowd_alert": (crowd.get("cutoffs") or {}).get("alert"),
        "fund_lv": fund.get("level"),
        "hy": hy.get("value"),
        "breadth": (report.get("breadth") or {}).get("ratio"),
        "top": longs[0]["label"] if longs else None,
        "alerts": [a["code"] for a in (report.get("rule_verdict") or {}).get("alerts", [])],
    }


def append(path: str, report: dict) -> list[dict]:
    """같은 종가일 레코드는 교체한다 — 재실행해도 중복이 쌓이지 않는다."""
    record = compact(report)
    if not record["d"]:
        return _load(path)
    records = [r for r in _load(path) if r.get("d") != record["d"]]
    records.append(record)
    records.sort(key=lambda r: r.get("d") or "")
    records = records[-MAX_RECORDS:]
    _save(path, records)
    return records


def load(path: str) -> list[dict]:
    return _load(path)
