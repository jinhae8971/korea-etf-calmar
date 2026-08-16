#!/usr/bin/env python3
"""워치리스트 산출 — 파이프라인에서 호출되며 단독 실행도 가능."""
from __future__ import annotations
import json, os, statistics, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from core import prices, realization, watchlist as W
from core import milestones as ms


def build(cfg_cycle: dict, series: dict) -> dict:
    with open(ROOT / "config" / "watchlist.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    stocks, etfs = cfg["stocks"], cfg["etfs"]
    pure = cfg_cycle["baskets"]["pure"]["tickers"]
    legacy = cfg_cycle["baskets"]["legacy"]["tickers"]

    # 공통 분기축
    keys = sorted(set.intersection(*[set(W.to_quarterly(series[t]))
                                     for t in series if series.get(t)][:1] or [set()])) \
        if False else sorted({q for t in series for q in W.to_quarterly(series[t])})
    pure_r = W.basket_returns(series, pure, keys)
    legacy_r = W.basket_returns(series, legacy, keys)

    # 순도
    win = cfg["purity_window"]
    dens, pstat = W.purity(stocks, cfg["purity_terms"], win["start"], win["end"],
                           str(ROOT / "data" / "purity.json"))

    # 실현 — 개별 종목 매출 YoY 지속성
    real, rstat = realization.collect(stocks, str(ROOT / "data" / "realization_all.json"))
    persist = {}
    for tk, data in real.items():
        rev = ms.yoy((data or {}).get("revenue") or {})
        if len(rev) >= 4:
            recent = [rev[q] for q in sorted(rev)[-4:]]
            persist[tk] = sum(1 for v in recent if v >= 0.25) / 4

    # 생존력 — 최근 영업이익률
    resil = {}
    for tk, data in real.items():
        rv = (data or {}).get("revenue") or {}
        oi = (data or {}).get("operating_income") or {}
        common = sorted(set(rv) & set(oi))
        if common and rv[common[-1]]:
            resil[tk] = oi[common[-1]] / rv[common[-1]]

    # 독립성·유동성
    indep, liq, exposure = {}, {}, {}
    for tk in list(stocks) + etfs:
        s = series.get(tk)
        if not s:
            continue
        r = W.returns(W.to_quarterly(s), keys)
        c_leg = W.correlation(r, legacy_r)
        c_pure = W.correlation(r, pure_r)
        if c_leg is not None:
            indep[tk] = -c_leg
        if c_leg is not None and c_pure is not None:
            exposure[tk] = c_pure - c_leg
        dv = W.median_dollar_volume(tk)
        if dv:
            liq[tk] = dv

    comp = {
        "purity": W.rank_pct(dens),
        "realization": W.rank_pct(persist),
        "independence": W.rank_pct(indep),
        "resilience": W.rank_pct(resil),
        "liquidity": W.rank_pct(liq),
    }
    stock_scores = W.score(comp, cfg["weights"], universe=list(stocks))
    ranked = sorted(stock_scores.items(), key=lambda kv: -kv[1]["score"])[:cfg["meta"]["top_n"]]

    ecomp = {
        "pure_exposure": W.rank_pct({t: exposure[t] for t in etfs if t in exposure}),
        "independence": W.rank_pct({t: indep[t] for t in etfs if t in indep}),
        "liquidity": W.rank_pct({t: liq[t] for t in etfs if t in liq}),
    }
    etf_scores = W.score(ecomp, cfg["etf_weights"], universe=etfs)
    etf_ranked = sorted(etf_scores.items(), key=lambda kv: -kv[1]["score"])[:5]

    def pack(items, extra=None):
        out = []
        for i, (tk, v) in enumerate(items, 1):
            row = {"rank": i, "ticker": tk, "score": v["score"],
                   "coverage": v["coverage"], "detail": v["detail"]}
            if extra:
                row.update({k: extra.get(k, {}).get(tk) for k in extra})
            out.append(row)
        return out

    return {
        "stocks": pack(ranked, {"density": dens, "persistence": persist,
                                "margin": {k: round(v, 3) for k, v in resil.items()}}),
        "etfs": pack(etf_ranked, {"exposure": {k: round(v, 3) for k, v in exposure.items()}}),
        "weights": cfg["weights"], "etf_weights": cfg["etf_weights"],
        "note": cfg["meta"]["note"],
        "status": {"purity": pstat, "realization": rstat},
    }


if __name__ == "__main__":
    with open(ROOT / "config" / "cycle.yaml", encoding="utf-8") as fh:
        cyc = yaml.safe_load(fh)
    with open(ROOT / "config" / "watchlist.yaml", encoding="utf-8") as fh:
        wl = yaml.safe_load(fh)
    syms = sorted(set(list(wl["stocks"]) + wl["etfs"] + cyc["baskets"]["pure"]["tickers"]
                      + cyc["baskets"]["legacy"]["tickers"] + [cyc["meta"]["benchmark"]]))
    series, _ = prices.collect(syms, str(ROOT / "data" / "prices.json"), 900)
    out = build(cyc, series)
    print(json.dumps(out["stocks"], ensure_ascii=False, indent=1)[:2000])
