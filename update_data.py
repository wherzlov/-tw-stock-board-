#!/usr/bin/env python3
"""
每日抓取台股資料並輸出 data.json，供「台股K線看板」自動讀取。
全部使用免費、免註冊的公開來源：

  1. Yahoo 股市 chart API   日 K（開高低收量）與加權指數
     https://query1.finance.yahoo.com/v8/finance/chart/2330.TW
  2. 證交所開放資料 T86     三大法人買賣（算連續買超天數）
     https://www.twse.com.tw/rwd/zh/fund/T86

不需要任何 API key，不需要付費。腳本在 GitHub Actions（或你自己的電腦）執行，
不受瀏覽器跨網域限制。

用法：
  pip install requests
  python update_data.py        # 產生 data.json
"""

import json
import sys
import time
from datetime import date, datetime, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-dashboard/1.0)"}
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
TWSE_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86"
BARS_KEEP = 150          # 輸出保留根數（指標至少需要 71 根）
INST_LOOKBACK = 12       # 三大法人回看交易日數

# 股票池：0 台灣50、1 中型100、2 高股息成分、3 自選股（自行增修）
POOLS = {
    0: [("2330", "台積電", "半導體"), ("2317", "鴻海", "電子組裝"), ("2454", "聯發科", "半導體"),
        ("2382", "廣達", "伺服器"), ("2308", "台達電", "電源"), ("2603", "長榮", "航運"),
        ("2881", "富邦金", "金融"), ("2891", "中信金", "金融"), ("1301", "台塑", "塑膠"),
        ("2412", "中華電", "電信")],
    1: [("3231", "緯創", "伺服器"), ("2379", "瑞昱", "IC設計"), ("3711", "日月光投控", "封測"),
        ("6669", "緯穎", "伺服器"), ("2357", "華碩", "品牌電腦"), ("3034", "聯詠", "IC設計"),
        ("2345", "智邦", "網通"), ("2409", "友達", "面板")],
    2: [("2884", "玉山金", "金融"), ("1216", "統一", "食品"), ("2002", "中鋼", "鋼鐵"),
        ("1303", "南亞", "塑膠")],
    3: [],  # 自選股：填 ("代號", "名稱", "類股")
}
MY_PICKS = ["2330", "2382", "2603", "2345", "3231"]


def get_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as exc:                       # noqa: BLE001
            if attempt == retries - 1:
                print(f"  ! 取得失敗 {url}：{exc}", file=sys.stderr)
                return None
            time.sleep(3 * (attempt + 1))
    return None


DIV1Y = {}          # {代號: 近一年每股配息合計}


def yahoo_bars(symbol, rng="1y"):
    """Yahoo chart API 取日 K 與近一年配息。上市加 .TW，上櫃加 .TWO，指數用 ^TWII。"""
    data = get_json(YAHOO.format(sym=symbol),
                    {"range": rng, "interval": "1d", "events": "div"})
    try:
        result = data["chart"]["result"][0]
        stamps = result["timestamp"]
        q = result["indicators"]["quote"][0]
    except (TypeError, KeyError, IndexError):
        return []
    bars = []
    for i, ts in enumerate(stamps):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        v = q.get("volume", [None] * len(stamps))[i]
        if None in (o, h, l, c) or min(o, h, l, c) <= 0:
            continue
        bars.append({
            "d": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"),
            "o": round(float(o), 2), "h": round(float(h), 2),
            "l": round(float(l), 2), "c": round(float(c), 2),
            "v": round(float(v or 0) / 1000.0, 1),       # 千股
        })
    # 近一年配息（Yahoo events.dividends）
    try:
        divs = result["events"]["dividends"]
        total = sum(float(v.get("amount") or 0) for v in divs.values())
        if total > 0:
            DIV1Y[symbol.split(".")[0]] = round(total, 4)
    except (TypeError, KeyError, AttributeError):
        pass
    bars.sort(key=lambda b: b["d"])
    return bars[-BARS_KEEP:]


def twse_inst(day):
    """證交所 T86：某一交易日的三大法人買賣超（外資＋投信），回傳 {代號: 淨額}。"""
    payload = get_json(TWSE_T86, {"date": day.strftime("%Y%m%d"),
                                  "selectType": "ALLBUT0999", "response": "json"})
    if not payload or payload.get("stat") != "OK":
        return None
    fields = payload.get("fields", [])
    rows = payload.get("data", [])

    def col(*keys):
        for idx, name in enumerate(fields):
            if any(k in name for k in keys):
                return idx
        return None

    i_code = 0
    i_foreign = col("外陸資買賣超股數(不含外資自營商)", "外資買賣超股數")
    i_trust = col("投信買賣超股數")
    out = {}
    for row in rows:
        try:
            code = row[i_code].strip()
            net = 0.0
            for idx in (i_foreign, i_trust):
                if idx is not None:
                    net += float(row[idx].replace(",", "") or 0)
            out[code] = net
        except (ValueError, IndexError, AttributeError):
            continue
    return out


def inst_streaks(codes):
    """回看最近的交易日，算每檔的外資＋投信連續買超天數。"""
    streak = {c: 0 for c in codes}
    done = set()
    day, checked = date.today(), 0
    while checked < INST_LOOKBACK and len(done) < len(codes):
        if day.weekday() < 5:
            table = twse_inst(day)
            time.sleep(1.2)                     # 尊重證交所
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


def main():
    codes = [c for members in POOLS.values() for c, _, _ in members]
    print(f"抓取 {len(codes)} 檔日 K（Yahoo）…")

    stocks, as_of = [], None
    for pool, members in POOLS.items():
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
        print("沒有抓到任何資料，data.json 未更新", file=sys.stderr)
        sys.exit(1)

    print("抓取三大法人買賣超（證交所）…")
    streaks = inst_streaks([s["code"] for s in stocks])
    for s in stocks:
        s["instDays"] = streaks.get(s["code"], 0)
        # 籌碼集中度需要集保股權分散表，公開來源為週資料；未接時看板該項顯示未成立
        s["conc"] = None
        s["concChg"] = None

    market = {}
    tw = yahoo_bars("^TWII")
    if len(tw) >= 2:
        market["index"] = tw[-1]["c"]
        market["indexChg"] = tw[-1]["c"] - tw[-2]["c"]
        market["indexChgPct"] = market["indexChg"] / tw[-2]["c"] * 100
        market["turnover"] = None
    adv = sum(1 for s in stocks if s["bars"][-1]["c"] > s["bars"][-2]["c"])
    market["adv"], market["dec"] = adv, len(stocks) - adv
    market["foreign"] = sum(1 for s in stocks if s["instDays"] > 0)
    market["foreignNote"] = "檔法人連買"

    out = {
        "asOf": (as_of or date.today().isoformat()).replace("-", " / "),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": "Yahoo 股市 + 證交所開放資料（免費、免註冊）",
        "myPicks": MY_PICKS,
        "market": market,
        "stocks": stocks,
    }
    with open("data.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"完成：{len(stocks)} 檔，資料日 {out['asOf']}")


if __name__ == "__main__":
    main()
