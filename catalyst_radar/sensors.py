"""센서 5종. 개별 뉴스가 아니라 '상태 전이'를 잡는다.

  제안(PROPOSED) → 검토(REVIEW) → 투표(VOTING) → 가결(PASSED)
                                → 일정확정(SCHEDULED) → 활성화(ACTIVATED)

이번 SOL 건은 제안 6월 / 시그널링 8월 5일 / 투표 8월 22~27일 / 게이트 활성화 9월 3일로
전 단계가 공개돼 있었다. 확률 예측이 아니라 이 캘린더를 먼저 읽는 것이 이 시스템의 전부다.
"""
import datetime as dt
import email.utils
import re
import time
import xml.etree.ElementTree as ET

from net import fetch, get_json, gh_json, post_json, FetchError

# ── 원천 레지스트리 ────────────────────────────────────────────────────
# 심볼 → {proposals: GitHub repo, client: GitHub repo, snapshot: space, cosmos: chain}
SOURCES = {
    "SOL":  {"proposals": "solana-foundation/solana-improvement-documents", "client": "anza-xyz/agave"},
    "ETH":  {"proposals": "ethereum/EIPs", "client": "ethereum/go-ethereum"},
    "BTC":  {"proposals": "bitcoin/bips", "client": "bitcoin/bitcoin"},
    "ADA":  {"proposals": "cardano-foundation/CIPs", "client": "IntersectMBO/cardano-node"},
    "NEAR": {"proposals": "near/NEPs", "client": "near/nearcore"},
    "AVAX": {"proposals": "avalanche-foundation/ACPs", "client": "ava-labs/avalanchego"},
    "SUI":  {"proposals": "sui-foundation/sips", "client": "MystenLabs/sui"},
    "ARB":  {"snapshot": "arbitrumfoundation.eth", "client": "OffchainLabs/nitro"},
    "POL":  {"proposals": "maticnetwork/Polygon-Improvement-Proposals", "client": "0xPolygon/polygon-edge"},
    "ATOM": {"cosmos": "cosmoshub", "client": "cosmos/gaia"},
    "TRX":  {"proposals": "tronprotocol/tips", "client": "tronprotocol/java-tron"},
    "XLM":  {"proposals": "stellar/stellar-protocol", "client": "stellar/stellar-core"},
    "ALGO": {"client": "algorand/go-algorand"},
    "FIL":  {"proposals": "filecoin-project/FIPs", "client": "filecoin-project/lotus"},
    "ZEC":  {"proposals": "zcash/zips", "client": "zcash/zcash"},
    "LTC":  {"client": "litecoin-project/litecoin"},
    "BCH":  {"client": "bitcoin-cash-node/bitcoin-cash-node"},
    "ETC":  {"client": "etclabscore/core-geth"},
    "HBAR": {"proposals": "hashgraph/hedera-improvement-proposal", "client": "hiero-ledger/hiero-consensus-node"},
    "ICP":  {"client": "dfinity/ic"},
    "TAO":  {"client": "opentensor/subtensor"},
    "AAVE": {"snapshot": "aave.eth"},
    "UNI":  {"snapshot": "uniswapgovernance.eth"},
    "MORPHO": {"snapshot": "morpho.eth"},
    "DOT":  {"client": "paritytech/polkadot-sdk"},
    "JUP":  {"snapshot": "jupiterdao.eth"},
    "HYPE": {"client": "hyperliquid-dex/node"},
    "WLD":  {"client": "worldcoin/world-id-contracts"},
    "MNT":  {"snapshot": "bitdao.eth"},
    "VET":  {"client": "vechain/thor"},
    "XRP":  {"proposals": "XRPLF/XRPL-Standards", "client": "XRPLF/rippled"},
    "BNB":  {"proposals": "bnb-chain/BEPs", "client": "bnb-chain/bsc"},
}

# ── 임팩트 분류 ────────────────────────────────────────────────────────
SUPPLY_RE = re.compile(
    r"\b(burn|emission|inflation|issuance|disinflation|tokenomic|supply|unlock|"
    r"vesting|buyback|mint|halving|staking reward|fee (switch|burn))\b", re.I)
INFRA_RE = re.compile(
    r"\b(upgrade|hard ?fork|consensus|finality|throughput|slot|block time|rent|"
    r"gas|scal|client release|mainnet|activation|feature gate|sharding)\b", re.I)

IMPACT = {"SUPPLY": 1.0, "INFRA": 0.7, "FEATURE": 0.45, "OTHER": 0.25}
CERTAINTY = {"PROPOSED": 0.30, "REVIEW": 0.45, "VOTING": 0.65,
             "PASSED": 0.85, "SCHEDULED": 0.90, "ACTIVATED": 1.00}

# 출처 신뢰도 — 1차 원천(레포·투표)과 2차 보도를 같은 무게로 두면 기사가 상위를 덮는다.
# 실측(2026-09-07): 뉴스 기반 오탐이 점수 상위 1~5위를 전부 차지했다.
SOURCE_TRUST = {"proposal": 1.00, "snapshot": 1.00, "cosmos": 1.00,
                "release": 0.95, "unlock": 0.85, "news": 0.35}

# CI가 찍는 태그(build-00388, sdlt-pass-00387 …)는 릴리스가 아니다.
SEMVER_RE = re.compile(r"v?\d+\.\d+(\.\d+)?", re.I)
CI_TAG_RE = re.compile(r"(build|pass|nightly|snapshot|ci|test)[-_]?\d+", re.I)


def is_real_release(tag):
    tag = (tag or "").strip()
    if not tag or CI_TAG_RE.search(tag):
        return False
    return bool(SEMVER_RE.search(tag))
STAGE_ORDER = ["PROPOSED", "REVIEW", "VOTING", "PASSED", "SCHEDULED", "ACTIVATED"]


def classify(title):
    if SUPPLY_RE.search(title or ""):
        return "SUPPLY"
    if INFRA_RE.search(title or ""):
        return "INFRA"
    return "FEATURE"


def _ev(symbol, source, key, stage, title, url, when=None, extra=None,
        classify_text=None):
    e = {"symbol": symbol, "source": source, "key": key, "stage": stage,
         "title": (title or "").strip()[:180], "url": url,
         "impact": classify(classify_text or title), "when": when,
         "trust": SOURCE_TRUST.get(source, 0.5)}
    e["event_date"] = extract_event_date(classify_text or title)
    if extra:
        e.update(extra)
    return e


# ── 센서 1·2: GitHub 개선제안 / 코어 클라이언트 ────────────────────────
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _atom_entries(url):
    """GitHub atom 피드는 API 레이트리밋(미인증 60회/시)과 무관하다.

    러너에 GITHUB_TOKEN 이 없거나 한도를 소진했을 때의 폴백. 정보량은 API보다
    적어(라벨·merged 여부 없음) 단계 판정이 보수적으로 내려간다.
    """
    raw = fetch(url, retries=2)
    root = ET.fromstring(raw)
    out = []
    for e in root.findall(ATOM_NS + "entry"):
        link = e.find(ATOM_NS + "link")
        out.append({
            "title": (e.findtext(ATOM_NS + "title") or "").strip(),
            "url": (link.get("href") if link is not None else "") or "",
            "updated": (e.findtext(ATOM_NS + "updated") or "")[:19],
            "id": (e.findtext(ATOM_NS + "id") or "").strip(),
        })
    return out


def sensor_releases_atom(symbol, repo, lookback_days=45):
    since = time.time() - lookback_days * 86400
    out = []
    for e in _atom_entries("https://github.com/%s/releases.atom" % repo):
        ts = _iso_ts(e["updated"])
        if ts and ts < since:
            continue
        tag = e["id"].rsplit("/", 1)[-1]
        if not is_real_release(tag):
            continue
        pre = bool(re.search(r"(rc|alpha|beta|pre)[.\-0-9]*$", tag, re.I))
        out.append(_ev(symbol, "release", "%s@%s" % (repo, tag),
                       "SCHEDULED" if pre else "ACTIVATED",
                       "%s %s" % (repo.split("/")[-1], e["title"]), e["url"],
                       when=e["updated"][:10],
                       extra={"prerelease": pre, "via": "atom"}))
    return out


def sensor_github_proposals(symbol, repo, lookback_days=45):
    out, since = [], time.time() - lookback_days * 86400
    try:
        data = gh_json("/repos/%s/pulls?state=all&per_page=30&sort=updated&direction=desc" % repo)
    except FetchError:
        # 폴백: 커밋 atom. 제안 단계 판정은 불가하므로 PROPOSED 로만 올린다.
        out = []
        for br in ("main", "master"):
            try:
                for e in _atom_entries("https://github.com/%s/commits/%s.atom" % (repo, br)):
                    ts = _iso_ts(e["updated"])
                    if ts and ts < since:
                        continue
                    if not (SUPPLY_RE.search(e["title"]) or INFRA_RE.search(e["title"])):
                        continue
                    if not STATUS_CHANGE_RE.search(e["title"]):
                        continue          # 'Update EIP-xxxx: 오타 수정' 류 편집 커밋 배제
                    out.append(_ev(symbol, "proposal", "%s~%s" % (repo, e["id"][-12:]),
                                   "PROPOSED", e["title"], e["url"],
                                   when=e["updated"][:10], extra={"via": "atom"}))
                return out
            except FetchError:
                continue
        raise
    for pr in data or []:
        upd = _iso_ts(pr.get("updated_at"))
        if upd and upd < since:
            break
        title_txt = pr.get("title") or ""
        if not (SUPPLY_RE.search(title_txt) or INFRA_RE.search(title_txt)):
            continue
        if not STATUS_CHANGE_RE.search(title_txt):
            continue                      # 'Update EIP-8037: 문구 정정' 류 배제
        labels = " ".join(l.get("name", "").lower() for l in pr.get("labels") or [])
        if pr.get("merged_at"):
            stage = "PASSED"
        elif "final" in labels or "accepted" in labels or "last call" in labels:
            stage = "PASSED"
        elif "review" in labels or "peer" in labels:
            stage = "REVIEW"
        else:
            stage = "PROPOSED"
        out.append(_ev(symbol, "proposal", "%s#%s" % (repo, pr["number"]), stage,
                       pr.get("title"), pr.get("html_url"),
                       extra={"updated_at": pr.get("updated_at")}))
    return out


def sensor_client_releases(symbol, repo, lookback_days=45):
    out, since = [], time.time() - lookback_days * 86400
    try:
        data = gh_json("/repos/%s/releases?per_page=10" % repo)
    except FetchError:
        return sensor_releases_atom(symbol, repo, lookback_days)
    for rel in data or []:
        pub = _iso_ts(rel.get("published_at") or rel.get("created_at"))
        if pub and pub < since:
            continue
        if not is_real_release(rel.get("tag_name")):
            continue
        stage = "SCHEDULED" if rel.get("prerelease") or rel.get("draft") else "ACTIVATED"
        title = "%s %s" % (repo.split("/")[-1], rel.get("tag_name") or "")
        body_head = (rel.get("body") or "")[:1200]
        out.append(_ev(symbol, "release", "%s@%s" % (repo, rel.get("tag_name")), stage,
                       title + " — " + _first_line(body_head), rel.get("html_url"),
                       classify_text=title + " " + body_head,
                       when=(rel.get("published_at") or "")[:10],
                       extra={"prerelease": bool(rel.get("prerelease"))}))
    return out


# ── 센서 3: Snapshot 오프체인 투표 ─────────────────────────────────────
SNAPSHOT_Q = """query($s:String!){proposals(first:12,
  where:{space:$s}, orderBy:"created", orderDirection:desc)
  {id title state start end}}"""


def sensor_snapshot(symbol, space):
    d = post_json("https://hub.snapshot.org/graphql",
                  {"query": SNAPSHOT_Q, "variables": {"s": space}})
    out = []
    stale_before = time.time() - 14 * 86400
    for p in ((d or {}).get("data") or {}).get("proposals") or []:
        st = p.get("state")
        if st == "closed" and (p.get("end") or 0) < stale_before:
            continue                      # 2주 넘게 지난 종료 투표는 카탈리스트가 아니다
        stage = {"pending": "SCHEDULED", "active": "VOTING", "closed": "PASSED"}.get(st, "PROPOSED")
        out.append(_ev(symbol, "snapshot", "snapshot:%s" % p["id"], stage, p.get("title"),
                       "https://snapshot.org/#/%s/proposal/%s" % (space, p["id"]),
                       when=_ts_date(p.get("end")),
                       extra={"event_date": _ts_date(p.get("end")),
                              "vote_state": st}))
    return out


# ── 센서 4: Cosmos 온체인 거버넌스 ─────────────────────────────────────
def sensor_cosmos(symbol, chain):
    d = get_json("https://rest.cosmos.directory/%s/cosmos/gov/v1/proposals"
                 "?pagination.limit=8&pagination.reverse=true" % chain)
    out = []
    for p in (d or {}).get("proposals") or []:
        status = (p.get("status") or "").replace("PROPOSAL_STATUS_", "")
        stage = {"DEPOSIT_PERIOD": "PROPOSED", "VOTING_PERIOD": "VOTING",
                 "PASSED": "PASSED", "REJECTED": "REJECTED", "FAILED": "REJECTED"}.get(status, "PROPOSED")
        if stage == "REJECTED":
            continue
        title = p.get("title") or (p.get("messages") or [{}])[0].get("@type", "")
        out.append(_ev(symbol, "cosmos", "%s:gov:%s" % (chain, p.get("id")), stage, title,
                       "https://www.mintscan.io/%s/proposals/%s" % (chain, p.get("id")),
                       when=(p.get("voting_end_time") or "")[:10],
                       extra={"event_date": (p.get("voting_end_time") or "")[:10]}))
    return out


# ── 센서 5: 일정 공지(보조) ────────────────────────────────────────────
DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b|"
    r"\bQ[1-4]\s*20\d\d\b|\b20\d\d-\d\d-\d\d\b", re.I)


NEWS_FRESH_DAYS = 10


def sensor_news(symbol, name):
    q = ('"%s" (crypto OR blockchain OR token) '
         '(upgrade OR proposal OR mainnet OR governance OR burn OR unlock '
         'OR activation OR vote)' % name)
    url = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en"
           % urlquote(q))
    raw = fetch(url, retries=2, timeout=25)
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    fresh_after = time.time() - NEWS_FRESH_DAYS * 86400
    for item in root.iter("item"):
        title = (item.findtext("title") or "")
        m = DATE_RE.search(title)
        if not m:
            continue                      # 날짜가 박힌 공지만 — 시황 기사 배제
        if not (SUPPLY_RE.search(title) or INFRA_RE.search(title)):
            continue
        if not _crypto_context(title, symbol, name):
            continue                      # 동명이의 기사 배제(FLR→에너지 기업 등)
        pub = item.findtext("pubDate") or ""
        pub_ts = _rfc822_ts(pub)
        if pub_ts and pub_ts < fresh_after:
            continue                      # 오래된 기사 — 재유입 차단
        if _is_past_month(m.group(0)):
            continue                      # 이미 지난 일자를 가리키는 제목 배제
        out.append(_ev(symbol, "news", "news:%s" % _slug(title), "SCHEDULED", title,
                       item.findtext("link"), when=pub[:16]))
    return out[:3]


def _rfc822_ts(s):
    if not s:
        return None
    try:
        return email.utils.mktime_tz(email.utils.parsedate_tz(s))
    except (TypeError, ValueError):
        return None


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]


def _is_past_month(frag, today=None):
    """'July 12' 같은 월·일 표기가 이미 지난 달을 가리키면 True.

    PUMP 의 7월 언락 기사가 9월 브리프에 재등장한 실측 오탐에 대한 대응.
    연도가 명시된 표기(2026-10-05)는 여기서 다루지 않고 그대로 통과시킨다.
    """
    today = today or dt.date.today()
    m = re.match(r"([A-Za-z]{3})", frag.strip())
    if not m:
        return False
    try:
        mon = MONTHS.index(m.group(1).lower()) + 1
    except ValueError:
        return False
    # 12개월 원형 거리: 뒤로 1개월 이상 벌어졌으면 과거로 본다.
    delta = (mon - today.month) % 12
    return delta > 6


# ── 유틸 ───────────────────────────────────────────────────────────────
def urlquote(s):
    import urllib.parse
    return urllib.parse.quote(s)


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower())[:60]


def _first_line(s):
    for ln in (s or "").splitlines():
        ln = ln.strip("#* -")
        if len(ln) > 12:
            return ln[:110]
    return ""


def _iso_ts(s):
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _ts_date(ts):
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except (TypeError, ValueError):
        return None


# ── 오케스트레이션 ─────────────────────────────────────────────────────
def collect(universe, name_by_symbol=None, use_news=True):
    """유니버스 전체 센서 수집. (events, coverage) 반환."""
    events, cov = [], {"attempted": 0, "ok": 0, "failed": []}
    name_by_symbol = name_by_symbol or {}
    for row in universe:
        sym = row["symbol"]
        src = SOURCES.get(sym, {})
        jobs = []
        if src.get("proposals"):
            jobs.append(("proposal", sensor_github_proposals, src["proposals"]))
        if src.get("client"):
            jobs.append(("release", sensor_client_releases, src["client"]))
        if src.get("snapshot"):
            jobs.append(("snapshot", sensor_snapshot, src["snapshot"]))
        if src.get("cosmos"):
            jobs.append(("cosmos", sensor_cosmos, src["cosmos"]))
        for label, fn, arg in jobs:
            cov["attempted"] += 1
            try:
                events.extend(fn(sym, arg))
                cov["ok"] += 1
            except (FetchError, Exception) as e:      # 한 원천 실패가 전체를 죽이지 않음
                cov["failed"].append("%s/%s: %s" % (sym, label, type(e).__name__))
        # 뉴스는 레지스트리 유무와 무관하게 전 종목에 돌린다.
        # 초기 구현은 '원천 없는 종목만' 으로 두었는데, 그 결과 이번 사태의 당사자인
        # SOL(레지스트리 보유)의 9/9 Transaction V1 일정이 통째로 누락됐다.
        if use_news:
            cov["attempted"] += 1
            try:
                events.extend(sensor_news(sym, name_by_symbol.get(sym, row.get("name") or sym)))
                cov["ok"] += 1
            except Exception as e:
                cov["failed"].append("%s/news: %s" % (sym, type(e).__name__))
    cov["rate"] = round(cov["ok"] / cov["attempted"], 3) if cov["attempted"] else 0.0
    return events, cov


CRYPTO_CTX_RE = re.compile(
    r"\b(blockchain|crypto|token|onchain|on-chain|mainnet|testnet|validator|"
    r"staking|protocol|network upgrade|hard ?fork|governance|defi|layer ?[12]|"
    r"L[12]\b|node|wallet|airdrop|tokenomics)\b", re.I)


def _crypto_context(title, symbol, name):
    """제목에 자산 식별자와 크립토 문맥이 함께 있어야 통과.

    실측 오탐: FLR 검색이 'The Sun Also Rises In Washington County: MarkWest…'
    (에너지 기업 기사)를 물어왔다. 이름만으로 검색하면 동명이의가 섞인다.
    """
    t = title or ""
    ident = re.search(r"\b(%s|%s)\b" % (re.escape(symbol), re.escape(name or symbol)), t, re.I)
    return bool(ident)


# ── 이벤트 예정일 추출 ─────────────────────────────────────────────────
# 관측일(when)과 이벤트 발생 예정일(event_date)은 다른 값이다. D-day 관리는
# 후자로만 성립한다 — 첫 구현에서 이 둘을 뭉뚱그려 캘린더가 비었다.
_ISO_D = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
_MDY = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:\s*,?\s*(20\d\d))?\b", re.I)


def extract_event_date(text, today=None, horizon_days=180):
    """본문에서 '앞으로 일어날' 날짜를 뽑는다. 없으면 None.

    연도가 없으면 오늘 이후로 가장 가까운 해를 채운다(12월→1월 경계 처리).
    과거이거나 지평(기본 180일)을 넘으면 버린다.
    """
    today = today or dt.date.today()
    cands = []
    for y, m, d in _ISO_D.findall(text or ""):
        try:
            cands.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    for mon, day, yr in _MDY.findall(text or ""):
        mi = MONTHS.index(mon[:3].lower()) + 1
        for year in ([int(yr)] if yr else [today.year, today.year + 1]):
            try:
                cand = dt.date(year, mi, int(day))
            except ValueError:
                continue
            if cand >= today or yr:
                cands.append(cand)
                break
    future = [c for c in cands
              if today <= c <= today + dt.timedelta(days=horizon_days)]
    return min(future).isoformat() if future else None


# atom 폴백에서 편집성 커밋과 상태 변경을 가르는 패턴.
# 실측: ETH 커밋 피드가 'Update EIP-8037: correct the ... timing' 류로 브리프를 덮었다.
STATUS_CHANGE_RE = re.compile(
    r"\b(add|new|create|move to|status|last ?call|final|review|accepted|"
    r"withdraw|activate|schedule|deploy|fork|mainnet|testnet)\b", re.I)


def dedupe_releases(events, per_repo=2):
    """레포당 최근 릴리스 N건만 남긴다.

    실측: hiero-consensus-node 가 rc/alpha 를 대량 배포해 브리프를 점유했다.
    """
    seen, out = {}, []
    for e in events:
        if e.get("source") != "release":
            out.append(e)
            continue
        repo = str(e.get("key", "")).split("@")[0]
        seen[repo] = seen.get(repo, 0) + 1
        if seen[repo] <= per_repo:
            out.append(e)
    return out
