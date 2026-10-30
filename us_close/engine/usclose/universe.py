# -*- coding: utf-8 -*-
"""Universe definitions for the US close briefing."""

# (label, yahoo symbol, kind)  kind: idx = % change, yld = bp change, lvl = vol index (pt change)
CROSS = [
    ("S&P 500", "^GSPC", "idx"),
    ("나스닥100", "^NDX", "idx"),
    ("다우", "^DJI", "idx"),
    ("러셀2000", "^RUT", "idx"),
    ("필라델피아 반도체", "^SOX", "idx"),
    ("VIX", "^VIX", "lvl"),
    ("미 5년물", "^FVX", "yld"),
    ("미 10년물", "^TNX", "yld"),
    ("미 30년물", "^TYX", "yld"),
    ("달러인덱스", "DX-Y.NYB", "idx"),
    ("원/달러", "USDKRW=X", "idx"),
    ("WTI", "CL=F", "idx"),
    ("금", "GC=F", "idx"),
    ("비트코인", "BTC-USD", "idx"),
]

SECTORS = [
    ("XLK", "정보기술"), ("XLC", "커뮤니케이션"), ("XLY", "경기소비재"), ("XLP", "필수소비재"),
    ("XLE", "에너지"), ("XLF", "금융"), ("XLV", "헬스케어"), ("XLI", "산업재"),
    ("XLB", "소재"), ("XLRE", "부동산"), ("XLU", "유틸리티"),
]

THEMES = [
    ("SMH", "반도체"), ("IGV", "소프트웨어"), ("KRE", "지역은행"), ("XBI", "바이오"),
    ("ITB", "주택건설"), ("XOP", "석유 E&P"), ("GDX", "금광"), ("ARKK", "혁신성장"),
    ("IBIT", "비트코인 ETF"),
]

CORE = [("SPY", "S&P 500"), ("QQQ", "나스닥100"), ("DIA", "다우"), ("IWM", "러셀2000"), ("RSP", "S&P 동일가중")]

# factor spreads: (label, long, short)
FACTORS = [
    ("동일가중 vs 시총가중", "RSP", "SPY"),
    ("소형주 vs 대형주", "IWM", "SPY"),
    ("성장 vs 가치", "IWF", "IWD"),
    ("모멘텀 vs 저변동", "MTUM", "USMV"),
]

# large caps for treemap / breadth / movers: symbol -> sector
LARGE = {
    # 정보기술
    "AAPL": "정보기술", "MSFT": "정보기술", "NVDA": "정보기술", "AVGO": "정보기술", "ORCL": "정보기술",
    "CRM": "정보기술", "AMD": "정보기술", "ADBE": "정보기술", "CSCO": "정보기술", "ACN": "정보기술",
    "IBM": "정보기술", "QCOM": "정보기술", "TXN": "정보기술", "INTU": "정보기술", "NOW": "정보기술",
    "AMAT": "정보기술", "MU": "정보기술", "LRCX": "정보기술", "KLAC": "정보기술", "ADI": "정보기술",
    "PANW": "정보기술", "INTC": "정보기술", "PLTR": "정보기술", "ANET": "정보기술", "SNPS": "정보기술",
    # 커뮤니케이션
    "GOOGL": "커뮤니케이션", "META": "커뮤니케이션", "NFLX": "커뮤니케이션", "TMUS": "커뮤니케이션",
    "DIS": "커뮤니케이션", "VZ": "커뮤니케이션", "T": "커뮤니케이션", "CMCSA": "커뮤니케이션",
    # 경기소비재
    "AMZN": "경기소비재", "TSLA": "경기소비재", "HD": "경기소비재", "MCD": "경기소비재", "LOW": "경기소비재",
    "BKNG": "경기소비재", "TJX": "경기소비재", "NKE": "경기소비재", "SBUX": "경기소비재",
    # 필수소비재
    "WMT": "필수소비재", "COST": "필수소비재", "PG": "필수소비재", "KO": "필수소비재", "PEP": "필수소비재",
    "PM": "필수소비재", "MDLZ": "필수소비재",
    # 헬스케어
    "LLY": "헬스케어", "UNH": "헬스케어", "JNJ": "헬스케어", "ABBV": "헬스케어", "MRK": "헬스케어",
    "TMO": "헬스케어", "ABT": "헬스케어", "ISRG": "헬스케어", "AMGN": "헬스케어", "DHR": "헬스케어",
    "PFE": "헬스케어", "GILD": "헬스케어", "VRTX": "헬스케어",
    # 금융
    "BRK-B": "금융", "JPM": "금융", "V": "금융", "MA": "금융", "BAC": "금융", "WFC": "금융", "GS": "금융",
    "MS": "금융", "AXP": "금융", "BLK": "금융", "SCHW": "금융", "C": "금융", "SPGI": "금융", "HOOD": "금융",
    # 산업재
    "GE": "산업재", "CAT": "산업재", "RTX": "산업재", "HON": "산업재", "UNP": "산업재", "BA": "산업재",
    "DE": "산업재", "LMT": "산업재", "GEV": "산업재", "UBER": "산업재", "SPCX": "산업재",
    # 에너지·유틸·소재·부동산
    "XOM": "에너지", "CVX": "에너지", "COP": "에너지",
    "NEE": "유틸리티", "SO": "유틸리티", "CEG": "유틸리티", "VST": "유틸리티",
    "LIN": "소재", "PLD": "부동산", "AMT": "부동산",
}

TECH_FOCUS = ["QQQ", "SPY", "SOXX", "DRAM"]  # calibrated chart analysis with probabilities (2026-10-31: AAPL -> SOXX, DRAM)
