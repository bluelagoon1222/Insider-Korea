#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Insider-Korea collector  (v1)

DART 'imwon/major shareholder specific securities ownership report' (D002)
  -> parse detail table from the filing document
  -> keep only on-market buys (JANGNAE MAESU)
  -> detect cluster buys (2+ insiders, same stock, 30-day window)
  -> attach post-trade returns vs. market index (Naver Finance)

Env:
  DART_API_KEY     required (GitHub Secret)
  START_DATE       default 20250101
  MAX_DART_CALLS   default 6000   (daily quota is 20000 per key, shared with Buyback-Korea)
  TIME_BUDGET_MIN  default 50
  MAX_PRICE_FETCH  default 700
  FORCE_RESCAN     '1' to rescan the whole range from START_DATE
  SCAN_VERSION     bump to invalidate cached document parses
"""

import io
import json
import os
import re
import sys
import time
import zipfile
import datetime as dt
from html import unescape

import requests

# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------
KEY = os.environ.get("DART_API_KEY", "").strip()
if not KEY:
    print("[FATAL] DART_API_KEY is empty")
    sys.exit(1)

START_DATE      = os.environ.get("START_DATE", "20250101")
MAX_DART_CALLS  = int(os.environ.get("MAX_DART_CALLS", "5000"))
# the DART key is shared with Buyback-Korea (20,000 calls/day):
# 3 weekday runs x 5,000 = 15,000 leaves headroom. Higher values are clamped.
MAX_DART_CALLS  = min(MAX_DART_CALLS, 5000)
TIME_BUDGET     = int(os.environ.get("TIME_BUDGET_MIN", "50")) * 60
MAX_PRICE_FETCH = int(os.environ.get("MAX_PRICE_FETCH", "700"))
FORCE_RESCAN    = os.environ.get("FORCE_RESCAN", "") == "1"
SCAN_VERSION    = os.environ.get("SCAN_VERSION", "1")

DOC_TIME_FRAC = 0.62        # share of the time budget the document stage may use
RESCAN_DAYS   = 10          # always re-check the most recent N days
CLUSTER_DAYS  = 30          # cluster window
CLUSTER_MIN   = 2           # distinct insiders required

DART = "https://opendart.fss.or.kr/api"
NAVER = "https://api.finance.naver.com/siseJson.naver"

DATA   = "data"
PRICED = os.path.join(DATA, "prices")
F_STATE   = os.path.join(DATA, "state.json")
F_FILINGS = os.path.join(DATA, "filings.json")
F_DOCS    = os.path.join(DATA, "docs.json")
F_TRADES  = os.path.join(DATA, "trades.json")
F_SUMMARY = os.path.join(DATA, "summary.json")

T0 = time.time()
CALLS = {"dart": 0, "naver": 0}
DEAD_HOST = set()

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://finance.naver.com/",
})


def elapsed():
    return time.time() - T0


def out_of_time(margin=90):
    return elapsed() > (TIME_BUDGET - margin)


def log(*a):
    print("[%5.0fs]" % elapsed(), *a, flush=True)


def jload(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jdump(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=0)
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# http
# ----------------------------------------------------------------------------
def http_get(url, params=None, host="dart", tries=3, timeout=25, binary=False):
    if host in DEAD_HOST:
        return None
    if host == "dart":
        if CALLS["dart"] >= MAX_DART_CALLS:
            return None
        CALLS["dart"] += 1
    else:
        CALLS["naver"] += 1
    fail = 0
    for i in range(tries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            if r.status_code in (403, 429):
                time.sleep(1.5 * (i + 1))
            fail += 1
        except Exception:
            fail += 1
            time.sleep(0.8 * (i + 1))
    if fail >= tries and host != "dart":
        # fail-fast: a dead non-critical host must not eat the whole run
        DEAD_HOST_COUNT[host] = DEAD_HOST_COUNT.get(host, 0) + 1
        if DEAD_HOST_COUNT[host] >= 12:
            DEAD_HOST.add(host)
            log("!! host disabled for this run:", host)
    return None


DEAD_HOST_COUNT = {}


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace(" ", "").strip()
    s = s.replace("주", "").replace("원", "")
    if s in ("", "-", "--", "N/A", "해당사항없음"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    m = re.match(r"^[-+]?\d+(\.\d+)?$", s)
    if not m:
        return None
    v = float(s)
    if neg:
        v = -v
    return v


def norm_date(s):
    if not s:
        return None
    s = str(s)
    d = re.sub(r"[^0-9]", "", s)
    if len(d) == 8:
        try:
            dt.date(int(d[:4]), int(d[4:6]), int(d[6:8]))
            return d
        except Exception:
            return None
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", s)
    if m:
        y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return "%04d%02d%02d" % (y, mo, da)
        except Exception:
            return None
    return None


def today_str():
    return (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y%m%d")


def month_ranges(start, end):
    """inclusive [start, end] split into <=1 month chunks (DART list limit: 3 months)."""
    s = dt.datetime.strptime(start, "%Y%m%d").date()
    e = dt.datetime.strptime(end, "%Y%m%d").date()
    out = []
    cur = s
    while cur <= e:
        if cur.month == 12:
            nxt = dt.date(cur.year + 1, 1, 1)
        else:
            nxt = dt.date(cur.year, cur.month + 1, 1)
        last = min(nxt - dt.timedelta(days=1), e)
        out.append((cur.strftime("%Y%m%d"), last.strftime("%Y%m%d")))
        cur = last + dt.timedelta(days=1)
    return out


# ----------------------------------------------------------------------------
# step 1. filing list  (D002 = imwon/major shareholder ownership report)
# ----------------------------------------------------------------------------
LIST_MODE = {"detail": True}   # False -> query the whole D group and filter by report name
RPT_KEY = "특정증권등소유상황보고서"


def fetch_list(bgn, end, corp_cls):
    got, page = [], 1
    while True:
        p = {"crtfc_key": KEY, "bgn_de": bgn, "end_de": end,
             "corp_cls": corp_cls, "page_no": page, "page_count": 100}
        if LIST_MODE["detail"]:
            p["pblntf_detail_ty"] = "D002"
        else:
            p["pblntf_ty"] = "D"
        txt = http_get(DART + "/list.json", p)
        if not txt:
            break
        try:
            js = json.loads(txt)
        except Exception:
            break
        st = js.get("status")
        if st == "013":          # no data
            break
        if st != "000":
            log("  list status", st, js.get("message"), bgn, end, corp_cls)
            break
        for it in js.get("list", []):
            if not LIST_MODE["detail"]:
                nm = (it.get("report_nm") or "").replace(" ", "")
                if RPT_KEY not in nm:
                    continue
            got.append({
                "rn": it.get("rcept_no"),
                "rd": it.get("rcept_dt"),
                "cc": it.get("corp_code"),
                "nm": it.get("corp_name"),
                "sc": (it.get("stock_code") or "").strip(),
                "mk": "KOSPI" if it.get("corp_cls") == "Y" else "KOSDAQ",
                "flr": it.get("flr_nm"),
                "rpt": it.get("report_nm"),
            })
        if page >= int(js.get("total_page", 1)):
            break
        page += 1
    return got


def scan_filings(filings, state):
    today = today_str()
    last = state.get("scanned_to")
    if FORCE_RESCAN or not last:
        bgn = START_DATE
    else:
        back = (dt.datetime.strptime(last, "%Y%m%d") - dt.timedelta(days=RESCAN_DAYS)).strftime("%Y%m%d")
        bgn = max(back, START_DATE)
    log("filing scan range", bgn, "~", today)

    reached = bgn
    first = True
    for (b, e) in month_ranges(bgn, today):
        if out_of_time() or CALLS["dart"] >= MAX_DART_CALLS * 0.5:
            log("  stop list scan (budget) at", b)
            break
        n0 = len(filings)
        for cls in ("Y", "K"):
            for it in fetch_list(b, e, cls):
                if it["rn"]:
                    filings[it["rn"]] = it
        if first and len(filings) == n0 and LIST_MODE["detail"]:
            # D002 returned nothing -> fall back to the whole D group + name filter
            log("  !! D002 empty, switching to pblntf_ty=D fallback")
            LIST_MODE["detail"] = False
            for cls in ("Y", "K"):
                for it in fetch_list(b, e, cls):
                    if it["rn"]:
                        filings[it["rn"]] = it
        first = False
        reached = e
        log("  %s~%s  +%d  (total %d)" % (b, e, len(filings) - n0, len(filings)))

    state["scanned_to"] = min(reached, today)
    return filings, state


# ----------------------------------------------------------------------------
# step 2. document parsing
# ----------------------------------------------------------------------------
TAG = re.compile(r"<[^>]+>")

BUY_KEYS  = ("장내매수", "시장내매수")
SELL_KEYS = ("장내매도", "시장내매도")


def clean_cell(s):
    s = re.sub(r"<BR\s*/?>", " ", s, flags=re.I)
    s = TAG.sub("", s)
    s = unescape(s)
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


CELL_RE = re.compile(r"<(T[EDHU])([^>]*)>(.*?)</\1>", re.S | re.I)
SPAN_RE = re.compile(r"colspan\s*=\s*\"?'?(\d+)", re.I)


def parse_tables(xml):
    """each row -> list of (text, colspan)"""
    tables = []
    for t in re.findall(r"<TABLE[^>]*>(.*?)</TABLE>", xml, re.S | re.I):
        rows = []
        for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", t, re.S | re.I):
            cells = []
            for _tag, attrs, body in CELL_RE.findall(tr):
                m = SPAN_RE.search(attrs or "")
                span = int(m.group(1)) if m else 1
                cells.append((clean_cell(body), max(1, min(span, 12))))
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def expand(row):
    """(text, span) -> flat text list + flag list marking slots born from a colspan"""
    texts, spanned = [], []
    for txt, span in row:
        for k in range(span):
            texts.append(txt)
            spanned.append(span > 1)
    return texts, spanned


HEAD_MAP = [
    ("reason", ("보고사유", "취득처분사유", "사유")),
    ("kind",   ("종류",)),
    ("date",   ("변동일", "취득일", "처분일", "거래일")),
    ("method", ("방법",)),
    ("before", ("변동전", "변동 전", "이전")),
    ("delta",  ("증감", "변동수량", "취득처분수량")),
    ("after",  ("변동후", "변동 후", "이후")),
    ("price",  ("단가", "취득처분단가", "거래단가")),
    ("note",   ("비고",)),
]


def map_header(rows):
    """locate the detail table header (1 or 2 physical rows) and map column indices.
    returns (last_header_row_index, {name: col}, n_cols)"""
    for i in range(min(6, len(rows))):
        labels, spanned = expand(rows[i])
        joined = " ".join(labels)
        if "변동" not in joined or not ("증감" in joined or "단가" in joined or "수량" in joined):
            continue
        hi = i
        if i + 1 < len(rows):
            sub, _ = expand(rows[i + 1])
            holes = [k for k, s in enumerate(spanned) if s]
            if holes and len(sub) == len(holes):
                for k, txt in zip(holes, sub):
                    labels[k] = (labels[k] + " " + txt).strip()
                hi = i + 1
            elif len(sub) == len(labels) and ("증감" in " ".join(sub) or "변동" in " ".join(sub)):
                labels = [(a + " " + b).strip() for a, b in zip(labels, sub)]
                hi = i + 1
        idx = {}
        for j, cell in enumerate(labels):
            c = cell.replace(" ", "")
            for name, keys in HEAD_MAP:
                if name in idx:
                    continue
                if any(k.replace(" ", "") in c for k in keys):
                    idx[name] = j
                    break
        if "date" in idx and ("delta" in idx or "price" in idx):
            return hi, idx, len(labels)
    return None, None, 0


def parse_detail(xml):
    """return (rows, meta) ; rows = list of dict"""
    out = []
    meta = {}

    # reporter attributes appear in the cover tables
    m = re.search(r"(등기임원|비등기임원|미등기임원)", xml)
    if m:
        meta["reg"] = m.group(1)
    m = re.search(r"(대표이사|사내이사|사외이사|감사|전무|상무|부사장|사장|회장|부회장|본부장|이사)", xml)
    if m:
        meta["pos"] = m.group(1)

    for rows in parse_tables(xml):
        hi, idx, ncol = map_header(rows)
        if idx is None:
            continue
        for raw in rows[hi + 1:]:
            r, _sp = expand(raw)
            if not r or len(r) < 3:
                continue
            # rowspan on the left-hand columns shortens the row -> right-align it
            if 0 < ncol - len(r) <= 3:
                r = [""] * (ncol - len(r)) + r
            joined = " ".join(r)
            if "합" in r[0] and "계" in r[0]:
                continue
            d = norm_date(r[idx["date"]] if idx.get("date", 99) < len(r) else "")
            if not d:
                continue

            def cell(name, _r=r):
                j = idx.get(name)
                if j is None or j >= len(_r):
                    return ""
                return _r[j]

            method = cell("method") or cell("reason")
            if not any(k in method for k in BUY_KEYS + SELL_KEYS):
                # fall back to the whole row (form layouts differ by year)
                for k in BUY_KEYS + SELL_KEYS:
                    if k in joined:
                        method = k
                        break
                else:
                    method = method or cell("reason") or ""
            side = ""
            if any(k in joined for k in BUY_KEYS):
                side = "B"
            elif any(k in joined for k in SELL_KEYS):
                side = "S"
            elif "시간외" in joined and "매수" in joined:
                side = "B"
                method = method or "시간외매수"
            elif "시간외" in joined and "매도" in joined:
                side = "S"

            out.append({
                "d": d,
                "side": side,
                "mth": (method or "").strip()[:24],
                "kind": cell("kind")[:20],
                "q": num(cell("delta")),
                "u": num(cell("price")),
                "aft": num(cell("after")),
            })
    return out, meta


def fetch_document(rcept_no):
    raw = http_get(DART + "/document.xml",
                   {"crtfc_key": KEY, "rcept_no": rcept_no},
                   binary=True, timeout=40)
    if not raw:
        return None, "nocall"
    if raw[:2] != b"PK":
        try:
            t = raw.decode("utf-8", "ignore")
            st = re.search(r"<status>(\d+)</status>", t)
            return None, "dart" + (st.group(1) if st else "?")
        except Exception:
            return None, "badbody"
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = None
        for n in zf.namelist():
            if n.lower().endswith((".xml", ".html", ".htm")):
                name = n
                break
        if not name:
            return None, "nozip"
        b = zf.read(name)
    except Exception:
        return None, "unzip"
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return b.decode(enc), None
        except Exception:
            continue
    return b.decode("utf-8", "ignore"), None


def collect_docs(filings, docs):
    """fetch + parse documents that are not cached yet (newest first)"""
    todo = [rn for rn in filings
            if docs.get(rn, {}).get("v") != SCAN_VERSION]
    todo.sort(key=lambda rn: filings[rn]["rd"], reverse=True)
    log("documents to fetch:", len(todo))

    done = err = 0
    doc_deadline = TIME_BUDGET * DOC_TIME_FRAC
    for rn in todo:
        if elapsed() > doc_deadline or CALLS["dart"] >= MAX_DART_CALLS:
            log("  stop document fetch (budget). remaining:", len(todo) - done - err)
            break
        xml, e = fetch_document(rn)
        if xml is None:
            docs[rn] = {"v": SCAN_VERSION, "st": e}
            err += 1
            if e == "nocall":
                break
            continue
        try:
            rows, meta = parse_detail(xml)
            keep = [r for r in rows if r["side"] in ("B", "S")]
            docs[rn] = {"v": SCAN_VERSION, "st": "ok", "rows": keep, "meta": meta}
        except Exception as ex:
            docs[rn] = {"v": SCAN_VERSION, "st": "parse:%s" % type(ex).__name__}
            err += 1
        done += 1
        if done % 200 == 0:
            log("  docs %d/%d  (dart calls %d)" % (done, len(todo), CALLS["dart"]))
            jdump(F_DOCS, docs, compact=True)
    log("documents done=%d err=%d" % (done, err))
    return docs


# ----------------------------------------------------------------------------
# step 3. prices (Naver)
# ----------------------------------------------------------------------------
def price_path(code):
    return os.path.join(PRICED, code + ".json")


def fetch_prices(code, start, end):
    txt = http_get(NAVER, {
        "symbol": code, "requestType": 1,
        "startTime": start, "endTime": end, "timeframe": "day",
    }, host="naver", timeout=20)
    if not txt:
        return None
    try:
        arr = json.loads(txt.replace("'", '"'))
    except Exception:
        return None
    ser = {}
    for row in arr[1:]:
        try:
            d = norm_date(row[0])
            c = float(row[4])
            if d and c > 0:
                ser[d] = c
        except Exception:
            continue
    return ser or None


def load_series(code):
    return jload(price_path(code), {})


def ensure_prices(codes, start):
    today = today_str()
    fetched = 0
    for code in codes:
        if fetched >= MAX_PRICE_FETCH or out_of_time(120):
            log("  stop price fetch (budget) after", fetched)
            break
        ser = load_series(code)
        if ser and max(ser.keys()) >= today:
            continue
        need_from = start if not ser else max(ser.keys())
        new = fetch_prices(code, need_from, today)
        fetched += 1
        if new:
            ser.update(new)
            jdump(price_path(code), ser, compact=True)
        time.sleep(0.12)
    log("prices fetched:", fetched, "of", len(codes))


def ensure_index(start):
    """KOSPI / KOSDAQ index, with ETF proxies as fallback"""
    out = {}
    for name, sym, proxy in (("KOSPI", "KOSPI", "069500"), ("KOSDAQ", "KOSDAQ", "229200")):
        p = os.path.join(PRICED, "idx-" + name + ".json")
        ser = jload(p, {})
        if not ser or max(ser.keys()) < today_str():
            new = fetch_prices(sym, start, today_str())
            if not new:
                new = fetch_prices(proxy, start, today_str())
            if new:
                ser.update(new)
                jdump(p, ser, compact=True)
        out[name] = ser
    return out


def sorted_dates(ser):
    return sorted(ser.keys())


def ret_after(ser, dates, d0, offset):
    """close-to-close return from the first trading day >= d0 to +offset trading days"""
    if not dates:
        return None
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] < d0:
            lo = mid + 1
        else:
            hi = mid
    if lo >= len(dates):
        return None
    i = lo
    j = i + offset if offset >= 0 else len(dates) - 1
    if j >= len(dates):
        return None
    p0, p1 = ser[dates[i]], ser[dates[j]]
    if not p0:
        return None
    return round((p1 / p0 - 1) * 100, 2)


# ----------------------------------------------------------------------------
# step 4. build site payload
# ----------------------------------------------------------------------------
OFFSETS = [("d1", 1), ("w1", 5), ("m1", 20), ("nowr", -1)]


def date_window():
    lo = (dt.datetime.strptime(START_DATE, "%Y%m%d") - dt.timedelta(days=200)).strftime("%Y%m%d")
    hi = (dt.datetime.utcnow() + dt.timedelta(hours=9, days=7)).strftime("%Y%m%d")
    return lo, hi


def build(filings, docs):
    trades = []
    d_lo, d_hi = date_window()
    dropped = 0
    for rn, f in filings.items():
        doc = docs.get(rn)
        if not doc or doc.get("st") != "ok":
            continue
        code = f.get("sc") or ""
        if len(code) != 6:
            continue
        meta = doc.get("meta", {})
        for r in doc.get("rows", []):
            if r["side"] not in ("B", "S"):
                continue
            q = r.get("q")
            if not q:
                continue
            q = abs(q)
            if not (d_lo <= r["d"] <= d_hi):
                # a misaligned column picked up some other date (grant date,
                # birth month, ...) - out-of-range rows are discarded
                dropped += 1
                continue
            trades.append({
                "side": r["side"],
                "rn": rn, "rd": f["rd"], "d": r["d"],
                "c": code, "n": f["nm"], "mk": f["mk"],
                "r": (f.get("flr") or "").strip(),
                "reg": meta.get("reg", ""),
                "pos": meta.get("pos", ""),
                "mth": r.get("mth", ""),
                "kind": r.get("kind", ""),
                "q": int(q),
                "u": int(r["u"]) if r.get("u") else None,
                "aft": int(r["aft"]) if r.get("aft") else None,
            })

    log("trade rows:", len(trades), "| dropped out-of-range dates:", dropped)

    codes = sorted({t["c"] for t in trades})
    ensure_prices(codes, START_DATE)
    idx = ensure_index(START_DATE)
    idx_dates = {k: sorted_dates(v) for k, v in idx.items()}

    # returns + amount
    cache = {}
    for t in trades:
        ser = cache.get(t["c"])
        if ser is None:
            ser = load_series(t["c"])
            cache[t["c"]] = ser
        dates = sorted_dates(ser)
        if not t.get("u"):
            # fall back to the close of the first trading day on/after the trade date
            u = None
            for d in dates:
                if d >= t["d"]:
                    u = int(ser[d])
                    break
            t["u"] = u
            t["uest"] = 1 if u else 0
        t["amt"] = int((t["u"] or 0) * t["q"])
        base = idx["KOSPI"] if t["mk"] == "KOSPI" else idx["KOSDAQ"]
        bdates = idx_dates["KOSPI"] if t["mk"] == "KOSPI" else idx_dates["KOSDAQ"]
        for key, off in OFFSETS:
            rs = ret_after(ser, dates, t["d"], off)
            rb = ret_after(base, bdates, t["d"], off)
            t[key] = rs
            t["x" + key] = None if (rs is None or rb is None) else round(rs - rb, 2)
        if dates:
            t["last"] = int(ser[dates[-1]])

    trades.sort(key=lambda t: (t["rd"], t["d"]), reverse=True)

    # ---- clusters (buys only) -------------------------------------------
    buys = [t for t in trades if t["side"] == "B"]
    by_code = {}
    for t in buys:
        by_code.setdefault(t["c"], []).append(t)

    clusters = []
    for code, rows in by_code.items():
        rows = sorted(rows, key=lambda t: t["d"])
        n = len(rows)
        for i in range(n):
            d0 = dt.datetime.strptime(rows[i]["d"], "%Y%m%d").date()
            win = [rows[i]]
            for j in range(i + 1, n):
                dj = dt.datetime.strptime(rows[j]["d"], "%Y%m%d").date()
                if (dj - d0).days <= CLUSTER_DAYS:
                    win.append(rows[j])
                else:
                    break
            people = {w["r"] for w in win if w["r"]}
            if len(people) >= CLUSTER_MIN:
                for w in win:
                    w["cl"] = 1
    for code, rows in by_code.items():
        cl = [t for t in rows if t.get("cl")]
        if not cl:
            continue
        cl = sorted(cl, key=lambda t: t["d"])
        last = cl[-1]
        recent = [t for t in cl
                  if (dt.datetime.strptime(last["d"], "%Y%m%d")
                      - dt.datetime.strptime(t["d"], "%Y%m%d")).days <= CLUSTER_DAYS]
        amt = sum(t["amt"] for t in recent)
        qty = sum(t["q"] for t in recent)
        avg = int(amt / qty) if qty else None
        clusters.append({
            "c": code, "n": last["n"], "mk": last["mk"],
            "from": recent[0]["d"], "to": last["d"],
            "people": sorted({t["r"] for t in recent if t["r"]}),
            "cnt": len(recent), "qty": qty, "amt": amt,
            "avg": avg, "last": last.get("last"),
            "gain": (round((last["last"] / avg - 1) * 100, 2)
                     if avg and last.get("last") else None),
            "xm1": last.get("xm1"),
        })
    clusters.sort(key=lambda c: c["to"], reverse=True)

    # ---- summary --------------------------------------------------------
    today = today_str()
    d30 = (dt.datetime.strptime(today, "%Y%m%d") - dt.timedelta(days=30)).strftime("%Y%m%d")
    recent30 = [t for t in buys if t["d"] >= d30]

    def avg_of(rows, key):
        v = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(v) / len(v), 2) if v else None

    cl_rows = [t for t in buys if t.get("cl")]
    sg_rows = [t for t in buys if not t.get("cl")]

    months = {}
    for t in buys:
        k = t["d"][:6]
        m = months.setdefault(k, {"cnt": 0, "amt": 0})
        m["cnt"] += 1
        m["amt"] += t["amt"]

    summary = {
        "updated": (dt.datetime.utcnow() + dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST"),
        "start": START_DATE,
        "scanned_to": jload(F_STATE, {}).get("scanned_to"),
        "n_filings": len(filings),
        "n_docs_ok": sum(1 for d in docs.values() if d.get("st") == "ok"),
        "n_trades": len(buys),
        "n_sells": len(trades) - len(buys),
        "n_stocks": len(by_code),
        "r30_cnt": len(recent30),
        "r30_amt": sum(t["amt"] for t in recent30),
        "r30_stocks": len({t["c"] for t in recent30}),
        "n_clusters": len(clusters),
        "perf": {
            "cluster": {"n": len(cl_rows),
                        "m1": avg_of(cl_rows, "m1"), "xm1": avg_of(cl_rows, "xm1"),
                        "w1": avg_of(cl_rows, "w1"), "xw1": avg_of(cl_rows, "xw1")},
            "single": {"n": len(sg_rows),
                       "m1": avg_of(sg_rows, "m1"), "xm1": avg_of(sg_rows, "xm1"),
                       "w1": avg_of(sg_rows, "w1"), "xw1": avg_of(sg_rows, "xw1")},
        },
        "months": months,
        "calls": CALLS,
        "runtime_sec": int(elapsed()),
        "dropped_dates": dropped,
        "pending": {
            "docs": sum(1 for rn in filings if docs.get(rn, {}).get("v") != SCAN_VERSION),
            "errors": {},
        },
    }
    ecnt = {}
    for d in docs.values():
        st = d.get("st")
        if st and st != "ok":
            ecnt[st] = ecnt.get(st, 0) + 1
    summary["pending"]["errors"] = ecnt

    return trades, clusters, summary


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    os.makedirs(PRICED, exist_ok=True)
    state   = jload(F_STATE, {})
    filings = jload(F_FILINGS, {})
    docs    = jload(F_DOCS, {})
    log("loaded: filings=%d docs=%d" % (len(filings), len(docs)))

    filings, state = scan_filings(filings, state)
    jdump(F_FILINGS, filings, compact=True)
    jdump(F_STATE, state)

    docs = collect_docs(filings, docs)
    jdump(F_DOCS, docs, compact=True)

    trades, clusters, summary = build(filings, docs)
    jdump(F_TRADES, {"trades": trades, "clusters": clusters}, compact=True)
    jdump(F_SUMMARY, summary)

    log("DONE trades=%d clusters=%d dart=%d naver=%d"
        % (len(trades), len(clusters), CALLS["dart"], CALLS["naver"]))
    log("pending docs:", summary["pending"]["docs"], summary["pending"]["errors"])


if __name__ == "__main__":
    main()
