#!/usr/bin/env python3
"""
每日抓取台股資料，輸出兩份檔案供「台股K線看板」讀取。全部使用免費、免註冊的公開來源。

兩層設計：

  data.json    關注清單（POOLS + 持股）：完整 150 根日 K、三大法人連買天數、配息。
               供 K 線圖與回測使用。
  market.json  全市場（上市＋上櫃的股票與 ETF）：精簡 90 根日 K，共用日期表、
               純數字陣列，檔案約數 MB。供全市場掃描與三關卡訊號使用。

看板先讀 data.json（快、資料深），再把 market.json 疊上來補齊掃描範圍；
兩邊都有的代號以 data.json 為準。

來源：
  Yahoo 股市 chart API   日 K（開高低收量）、指數、配息
  證交所 / 櫃買 openapi   全市場清單、產業別
  證交所 T86             三大法人買賣超

用法：
  pip install requests
  python update_data.py               # 兩份都產生
  python update_data.py --skip-market # 只更新 data.json（快，約 1 分鐘）
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard/1.0)"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
TWSE_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_INFO = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_ALL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_INFO = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

BARS_KEEP = 150          # data.json 保留根數（指標至少需要 71 根）
BARS_MARKET = 90         # market.json 保留根數（壓檔案大小）
INST_LOOKBACK = 12       # 三大法人回看交易日數
WORKERS = 6              # 全市場抓取並行數（太高會被 Yahoo 限流）

# 關注清單：下方 POOLS 預設 ＋ watchlist.json 裡的代號（合併，不是取代）
# watchlist.json 格式：{"picks": ["2330", "0056", ...]}
# 看板「持股」頁有一鍵複製鍵，把你目前的持股＋存股計畫產成這段 JSON。
# 輸出不標註哪幾檔來自 watchlist.json，所以持股混在預設大型股裡看不出來。
WATCHLIST_FILE = "watchlist.json"

# 以下是 watchlist.json 不存在時的預設：0 台灣50、1 中型100、2 高股息成分、3 自選股
POOLS = {
    0: [("2330", "台積電", "半導體"), ("2317", "鴻海", "電子組裝"), ("2454", "聯發科", "半導體"),
        ("2382", "廣達", "伺服器"), ("2308", "台達電", "電源"), ("2603", "長榮", "航運"),
        ("2881", "富邦金", "金融"), ("2891", "中信金", "金融"), ("1301", "台塑", "塑膠"),
        ("2412", "中華電", "電信")],
    1: [("3231", "緯創", "伺服器"), ("2379", "瑞昱", "IC設計"), ("3711", "日月光投控", "封測"),
        ("6669", "緯穎", "伺服器"), ("2357", "華碩", "品牌電腦"), ("3034", "聯詠", "IC設計"),
        ("2345", "智邦", "網通"), ("2409", "友達", "面板"), ("3008", "大立光", "光學"),
        ("2377", "微星", "主機板"), ("3037", "欣興", "載板"), ("4938", "和碩", "電子組裝"),
        ("1101", "台泥", "水泥"), ("2912", "統一超", "零售"), ("2207", "和泰車", "汽車"),
        ("1476", "儒鴻", "紡織"), ("2618", "長榮航", "航空"), ("1590", "亞德客-KY", "氣動元件"),
        ("3045", "台灣大", "電信"), ("6505", "台塑化", "煉油")],
    2: [("2884", "玉山金", "金融"), ("1216", "統一", "食品"), ("2002", "中鋼", "鋼鐵"),
        ("1303", "南亞", "塑膠")],
    3: [],  # 自選股：填 ("代號", "名稱", "類股")
}
MY_PICKS = ["2330", "2382", "2603", "2345", "3231"]


def get_json(url, params=None, retries=3, quiet=False):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as exc:                       # noqa: BLE001
            if attempt == retries - 1:
                if not quiet:
                    print(f"  ! 取得失敗 {url}：{exc}", file=sys.stderr)
                return None
            time.sleep(2 * (attempt + 1))
    return None


DIV1Y = {}          # {代號: 近一年每股配息合計}


def is_stock(code):
    return len(code) == 4 and code.isdigit() and not code.startswith("0")


def is_etf(code):
    return code.startswith("00") and 5 <= len(code) <= 6


def yahoo_raw(code, market, rng):
    """回傳 (bars, div)。market 1=上市(.TW) 2=上櫃(.TWO)；0 代表兩個都試。"""
    suffixes = [".TW"] if market == 1 else [".TWO"] if market == 2 else [".TW", ".TWO"]
    for suf in suffixes:
        data = get_json(YAHOO.format(sym=code + suf),
                        {"range": rng, "interval": "1d", "events": "div"},
                        retries=2, quiet=True)
        try:
            result = data["chart"]["result"][0]
            stamps = result["timestamp"]
            q = result["indicators"]["quote"][0]
        except (TypeError, KeyError, IndexError):
            continue
        bars = []
        for i, ts in enumerate(stamps):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            v = q.get("volume", [None] * len(stamps))[i]
            if None in (o, h, l, c) or min(o, h, l, c) <= 0:
                continue
            bars.append((datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
                         round(float(o), 2), round(float(h), 2), round(float(l), 2),
                         round(float(c), 2), round(float(v or 0) / 1000.0, 1)))
        div = 0.0
        try:
            div = round(sum(float(x.get("amount") or 0)
                            for x in result["events"]["dividends"].values()), 4)
        except (TypeError, KeyError, AttributeError):
            pass
        bars.sort(key=lambda b: b[0])
        if bars:
            return bars, div
    return [], 0.0


def yahoo_bars(symbol, rng="1y"):
    """關注清單用：回傳 dict 形式的日 K，並記錄配息。"""
    code, _, suf = symbol.partition(".")
    market = 2 if suf == "TWO" else 1
    bars, div = yahoo_raw(code, market, rng)
    if div > 0:
        DIV1Y[code] = div
    return [{"d": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4], "v": b[5]}
            for b in bars][-BARS_KEEP:]


def index_bars():
    data = get_json(YAHOO.format(sym="^TWII"), {"range": "3mo", "interval": "1d"})
    try:
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c]
    except (TypeError, KeyError, IndexError):
        return []
    return closes


# ── 全市場清單 ────────────────────────────────────────────────────────────

def market_listing():
    """回傳 [(code, name, market, industry)]，market 1=上市 2=上櫃。"""
    out, seen = [], set()
    industry = {}

    for url, mk in ((TWSE_INFO, 1), (TPEX_INFO, 2)):
        rows = get_json(url, quiet=True) or []
        for r in rows:
            c = str(r.get("公司代號") or r.get("SecuritiesCompanyCode") or "").strip()
            ind = str(r.get("產業別") or r.get("SecuritiesIndustryCode") or "").strip()
            if c and ind:
                industry[c] = ind

    tw = get_json(TWSE_ALL) or []
    for r in tw:
        c = str(r.get("Code") or "").strip()
        n = str(r.get("Name") or "").strip()
        if c and c not in seen and (is_stock(c) or is_etf(c)):
            seen.add(c)
            out.append((c, n, 1, industry.get(c, "ETF" if is_etf(c) else "上市")))
    print(f"  上市 {len(out)} 檔")

    ot = get_json(TPEX_ALL) or []
    n0 = len(out)
    for r in ot:
        c = str(r.get("SecuritiesCompanyCode") or r.get("Code") or "").strip()
        n = str(r.get("CompanyName") or r.get("Name") or "").strip()
        if c and c not in seen and (is_stock(c) or is_etf(c)):
            seen.add(c)
            out.append((c, n, 2, industry.get(c, "ETF" if is_etf(c) else "上櫃")))
    print(f"  上櫃 {len(out) - n0} 檔")
    return out


def build_market(listing):
    """抓全市場精簡日 K，輸出 market.json 用的 dict。"""
    total = len(listing)
    done = [0]

    def one(item):
        code, name, mk, ind = item
        bars, div = yahoo_raw(code, mk, "6mo")
        done[0] += 1
        if done[0] % 100 == 0:
            print(f"  {done[0]}/{total}…", flush=True)
        if len(bars) < 71:
            return None
        return {"code": code, "name": name, "mk": mk, "ind": ind,
                "bars": bars[-BARS_MARKET:], "div": div}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = [r for r in pool.map(one, listing) if r]

    # 共用日期表：所有出現過的交易日，由舊到新
    dates = sorted({b[0] for r in rows for b in r["bars"]})
    idx = {d: i for i, d in enumerate(dates)}

    stocks = []
    for r in rows:
        start = idx[r["bars"][0][0]]
        seq, cursor = [], start
        for b in r["bars"]:
            gap = idx[b[0]] - cursor
            seq.extend([None] * gap)                  # 停牌日補空
            seq.append([b[1], b[2], b[3], b[4], b[5]])
            cursor = idx[b[0]] + 1
        stocks.append({"c": r["code"], "n": r["name"], "ind": r["ind"],
                       "p": 3 if is_etf(r["code"]) else 0,
                       "i": start, "b": seq, "d": r["div"]})

    return {"asOf": (dates[-1] if dates else date.today().isoformat()),
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "dates": dates, "stocks": stocks}


# ── 三大法人 ──────────────────────────────────────────────────────────────

def twse_inst(day):
    payload = get_json(TWSE_T86, {"date": day.strftime("%Y%m%d"),
                                  "selectType": "ALLBUT0999", "response": "json"},
                       quiet=True)
    if not payload or payload.get("stat") != "OK":
        return None
    fields = payload.get("fields", [])

    def col(*keys):
        for i, name in enumerate(fields):
            if any(k in name for k in keys):
                return i
        return None

    i_foreign = col("外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
    i_trust = col("投信買賣超股數")
    out = {}
    for row in payload.get("data", []):
        try:
            net = 0.0
            for i in (i_foreign, i_trust):
                if i is not None:
                    net += float(row[i].replace(",", "") or 0)
            out[row[0].strip()] = net
        except (ValueError, IndexError, AttributeError):
            continue
    return out


def inst_streaks(codes):
    streak = {c: 0 for c in codes}
    done, day, checked = set(), date.today(), 0
    while checked < INST_LOOKBACK and len(done) < len(codes):
        if day.weekday() < 5:
            table = twse_inst(day)
            time.sleep(1.2)
            if table:
                checked += 1
                for c in codes:
                    if c in done:
                        continue
                    net = table.get(c)
                    if net is None:
                        continue
                    if net > 0:
                        streak[c] += 1
                    else:
                        done.add(c)
        day -= timedelta(days=1)
        if (date.today() - day).days > 40:
            break
    return streak


# ── 主流程 ────────────────────────────────────────────────────────────────

def watchlist_extra():
    """讀 watchlist.json，回傳不在 POOLS 裡的代號 [(code, name, sector, pool)]。
    合併而非取代：你的持股混在預設大型股裡，輸出不標註哪幾檔是你的。"""
    try:
        with open(WATCHLIST_FILE, encoding="utf-8") as fh:
            picks = json.load(fh).get("picks") or []
    except (OSError, ValueError):
        return []
    have = {c for members in POOLS.values() for c, _, _ in members}
    picks = [str(c).strip() for c in picks if str(c).strip()]
    picks = [c for c in dict.fromkeys(picks) if c not in have]
    if not picks:
        return []
    known = {}
    for url in (TWSE_ALL, TPEX_ALL):
        for r in get_json(url, quiet=True) or []:
            c = str(r.get("Code") or r.get("SecuritiesCompanyCode") or "").strip()
            n = str(r.get("Name") or r.get("CompanyName") or "").strip()
            if c and n:
                known.setdefault(c, n)
    print(f"  watchlist.json 額外加入 {len(picks)} 檔")
    return [(c, known.get(c, c), "ETF" if is_etf(c) else "其他",
             3 if is_etf(c) else 0) for c in picks]


def build_watchlist():
    groups = {p: [(c, n, s) for c, n, s in m] for p, m in POOLS.items()}
    for code, name, sector, pool in watchlist_extra():
        groups.setdefault(pool, []).append((code, name, sector))
    stocks, as_of = [], None
    for pool, members in groups.items():
        for code, name, sector in members:
            bars = yahoo_bars(f"{code}.TW") or yahoo_bars(f"{code}.TWO")
            if len(bars) < 71:
                print(f"  ! {code} {name} 資料不足（{len(bars)} 根），略過")
                continue
            stocks.append({"code": code, "name": name, "sector": sector,
                           "pool": pool, "bars": bars, "div1y": DIV1Y.get(code, 0)})
            as_of = max(as_of or "", bars[-1]["d"])
            print(f"  {code} {name} {len(bars)} 根，最新 {bars[-1]['d']} 收 {bars[-1]['c']}")
            time.sleep(0.3)

    if not stocks:
        print("關注清單一檔都沒抓到，data.json 未更新", file=sys.stderr)
        return None

    print("抓取三大法人買賣超（證交所）…")
    streaks = inst_streaks([s["code"] for s in stocks])
    for s in stocks:
        s["instDays"] = streaks.get(s["code"], 0)
        s["conc"] = None          # 籌碼集中度需集保週資料，未接時看板顯示未成立
        s["concChg"] = None

    market = {}
    closes = index_bars()
    if len(closes) >= 2:
        market["index"] = round(closes[-1], 2)
        market["indexChg"] = round(closes[-1] - closes[-2], 2)
        market["indexChgPct"] = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2)
        market["turnover"] = None
    adv = sum(1 for s in stocks if s["bars"][-1]["c"] > s["bars"][-2]["c"])
    market["adv"], market["dec"] = adv, len(stocks) - adv
    market["foreign"] = sum(1 for s in stocks if s["instDays"] > 0)
    market["foreignNote"] = "檔法人連買"

    return {
        "asOf": (as_of or date.today().isoformat()).replace("-", " / "),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "Yahoo 股市 + 證交所開放資料（免費、免註冊）",
        "myPicks": MY_PICKS,
        "market": market,
        "stocks": stocks,
    }


def main():
    skip_market = "--skip-market" in sys.argv

    print("【第一層】關注清單完整日 K")
    watch = build_watchlist()
    if watch:
        with open("data.json", "w", encoding="utf-8") as fh:
            json.dump(watch, fh, ensure_ascii=False, separators=(",", ":"))
        print(f"data.json 完成：{len(watch['stocks'])} 檔，資料日 {watch['asOf']}")
    elif not skip_market:
        print("關注清單失敗，仍繼續產生 market.json", file=sys.stderr)

    if skip_market:
        return

    print("\n【第二層】全市場精簡日 K")
    listing = market_listing()
    if not listing:
        print("取不到全市場清單，market.json 未更新", file=sys.stderr)
        return
    mkt = build_market(listing)
    if not mkt["stocks"]:
        print("全市場一檔都沒抓到，market.json 未更新", file=sys.stderr)
        return
    with open("market.json", "w", encoding="utf-8") as fh:
        json.dump(mkt, fh, ensure_ascii=False, separators=(",", ":"))
    import os
    size = os.path.getsize("market.json") / 1024 / 1024
    print(f"market.json 完成：{len(mkt['stocks'])}/{len(listing)} 檔，"
          f"{len(mkt['dates'])} 個交易日，{size:.1f} MB，資料日 {mkt['asOf']}")


if __name__ == "__main__":
    main()
