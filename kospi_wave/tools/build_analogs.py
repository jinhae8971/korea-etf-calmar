"""과거 급등→조정 사례 라이브러리 생성 (1회성, 결과는 kospi_wave/analogs.json 으로 커밋).

세계 주요 지수 주봉 전체 이력에서 '저점→고점 2.2배 이상, 25~150주' 상승 에피소드를 모두 뽑아
상승 모양(로그 정규화 40점)과 고점 이후 260주 경로(고점 대비 %)를 저장한다.
과거 데이터라 바뀌지 않으므로 매일 다시 받지 않는다. 재생성: python kospi_wave/tools/build_analogs.py
"""
import json, os
import numpy as np, pandas as pd, yfinance as yf

TICK = {"코스피": "^KS11", "코스닥": "^KQ11", "닛케이": "^N225", "대만": "^TWII", "상해": "000001.SS",
        "선전": "399001.SZ", "항셍": "^HSI", "나스닥": "^IXIC", "SOX": "^SOX", "S&P500": "^GSPC",
        "다우": "^DJI", "센섹스": "^BSESN", "보베스파": "^BVSP", "인니": "^JKSE", "아르헨": "^MERV", "튀르키예": "XU100.IS"}
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "analogs.json")


def shape(seg, n=40):
    x = np.log(np.asarray(seg, float)); x = (x - x[0]) / (x[-1] - x[0])
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)


eps = []
for name, t in TICK.items():
    d = yf.download(t, period="max", interval="1wk", progress=False, auto_adjust=True, threads=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    c = d["Close"].dropna(); c = c[c > 0]; v = c.values
    last_pk = -999
    for i in range(60, len(c) - 20):
        if v[i] != v[max(0, i - 60):i + 61].max() or i - last_pk < 52:
            continue
        if name == "코스피" and c.index[i].year >= 2026:
            continue
        j0 = max(0, i - 150); lo = j0 + int(np.argmin(v[j0:i])); r = v[i] / v[lo]; dur = i - lo
        if r < 2.2 or dur < 25:
            continue
        last_pk = i
        eps.append({"name": name, "low": str(c.index[lo].date()), "peak": str(c.index[i].date()),
                    "x": round(float(r), 3), "rise_w": int(dur),
                    "shape": [round(float(s), 4) for s in shape(v[lo:i + 1])],
                    "post": [round(float(p / v[i] * 100), 2) for p in v[i:i + 261]]})
    print(name, len(c), "episodes", sum(e["name"] == name for e in eps))
json.dump({"built": str(pd.Timestamp.now().date()), "episodes": eps}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
print("total", len(eps), "→", OUT)
