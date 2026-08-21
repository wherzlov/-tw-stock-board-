// 瀏覽器端抓取台股資料（免註冊）。外部站台都不給 CORS，統一走 r.jina.ai 讀取代理，
// 回傳的是包了 markdown 外殼的文字，所以自己把裡面的 JSON 切出來。
const TIMEOUT = 20000;

function extractJSON(txt) {
  const starts = [txt.indexOf("["), txt.indexOf("{")].filter(x => x >= 0);
  if (!starts.length) throw new Error("no json");
  const i = Math.min(...starts);
  const j = Math.max(txt.lastIndexOf("]"), txt.lastIndexOf("}"));
  return JSON.parse(txt.slice(i, j + 1));
}

async function once(url) {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), TIMEOUT);
  try {
    const res = await fetch("https://r.jina.ai/" + url, { signal: ac.signal, cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return extractJSON(await res.text());
  } finally { clearTimeout(t); }
}

// r.jina.ai 會限流（HTTP 403/429）。遇到限流就退避重試，並全域降速，
// 避免整批請求連環失敗導致「一直是示意資料」。
let gate = Promise.resolve(), gap = 120;
function slot() {
  const p = gate.then(() => new Promise(r => setTimeout(r, gap)));
  gate = p;
  return p;
}

async function getJSON(url, tries = 4) {
  let last;
  for (let k = 0; k < tries; k++) {
    await slot();
    try {
      const v = await once(url);
      gap = Math.max(120, gap - 40);
      return v;
    } catch (e) {
      last = e;
      const limited = /HTTP (403|429)/.test(String(e && e.message));
      if (limited) gap = Math.min(2500, gap * 2 + 200);
      if (k < tries - 1) await new Promise(r => setTimeout(r, limited ? 2000 + k * 2500 : 900 + k * 1200));
    }
  }
  throw last;
}

const NAMES = {}, DIVS = {};
export function div1y(code) {
  if (DIVS[code] != null) return DIVS[code];
  try { const v = parseFloat(localStorage.getItem("twk:div:" + code)); return isFinite(v) ? v : 0; } catch (e) { return 0; }
}
const num = v => +String(v == null ? 0 : v).replace(/,/g, "") || 0;
const isStock = c => /^[1-9]\d{3}$/.test(c);
const isETF = c => /^00\d{2,3}[A-Z]?$/.test(c);
const okCode = c => isStock(c) || isETF(c);

// 全市場清單：上市（證交所 openapi）＋上櫃（櫃買 openapi，取不到時只用上市）
export async function fetchAllCodes() {
  const out = [];
  const notes = [];
  let sumValue = 0, adv = 0, dec = 0, flat = 0;
  try {
    const tw = await getJSON("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL");
    tw.forEach(r => {
      const c = (r.Code || "").trim();
      if (!okCode(c)) return;
      const chg = num(r.Change);
      sumValue += num(r.TradeValue);
      if (chg > 0) adv++; else if (chg < 0) dec++; else flat++;
      out.push({ code: c, name: (r.Name || "").trim(), market: isETF(c) ? 3 : 1, turnover: num(r.TradeValue) });
    });
  } catch (e) { notes.push("上市清單取得失敗"); }
  try {
    const ot = await getJSON("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", 1);
    ot.forEach(r => {
      const c = (r.SecuritiesCompanyCode || r.Code || "").trim();
      if (okCode(c)) out.push({ code: c, name: (r.CompanyName || r.Name || "").trim(), market: isETF(c) ? 3 : 2, turnover: num(r.TradeValue || r.Amount) });
    });
  } catch (e) { notes.push("上櫃清單暫時取不到，本次只掃上市"); }
  if (!out.length) throw new Error(notes.join("；") || "清單取得失敗");
  out.sort((a, b) => b.turnover - a.turnover);
  out.notes = notes;
  out.summary = { turnoverYi: sumValue / 1e8, adv, dec, flat };
  return out;
}

// 加權指數：Yahoo 的 ^TWII，同一條管線
export async function fetchIndex() {
  const d = await getJSON("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?range=3mo&interval=1d", 1);
  const r = d && d.chart && d.chart.result && d.chart.result[0];
  if (!r || !r.timestamp) throw new Error("index unavailable");
  const c = r.indicators.quote[0].close.filter(x => x != null);
  const n = c.length - 1, close = c[n], prev = c[n - 1];
  const ma20 = c.slice(-20).reduce((s, x) => s + x, 0) / Math.min(20, c.length);
  return { index: close, indexChg: close - prev, indexChgPct: (close / prev - 1) * 100, above20: close > ma20 };
}

// 中文名對照表：上市＋上櫃清單（含 ETF），存 24 小時。與日 K 快取無關，
// 所以即使日 K 是從快取拿的，名稱仍然查得到。
export async function fetchNameMap() {
  try {
    const raw = localStorage.getItem("twk:namemap");
    if (raw) {
      const o = JSON.parse(raw);
      if (Date.now() - o.t < 24 * 3600 * 1000 && o.m && Object.keys(o.m).length) {
        Object.keys(o.m).forEach(c => { NAMES[c] = o.m[c]; });
        return o.m;
      }
    }
  } catch (e) {}
  const list = await fetchAllCodes();
  const m = {};
  list.forEach(s => { if (s.name) { m[s.code] = s.name; NAMES[s.code] = s.name; } });
  try { localStorage.setItem("twk:namemap", JSON.stringify({ t: Date.now(), m })); } catch (e) {}
  return m;
}

// 加權指數日 K（一年）：用來和自己的交易同期比較
export async function fetchIndexBars() {
  const d = await getJSON("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?range=1y&interval=1d", 1);
  const r = d && d.chart && d.chart.result && d.chart.result[0];
  if (!r || !r.timestamp) throw new Error("index unavailable");
  const q = r.indicators.quote[0], out = [];
  for (let i = 0; i < r.timestamp.length; i++) {
    if (q.close[i] == null) continue;
    out.push({ d: new Date(r.timestamp[i] * 1000).toISOString().slice(0, 10), c: q.close[i] });
  }
  return out;
}

// 日 K 快取：同一盤中重抓不必再打一次網路，但換盤（新交易日／收盤後）一定重抓
const CACHE_MS = 20 * 60 * 1000;
// 目前所屬的盤別戳記：台北時間 09:00 前算前一日的盤，之後算當日的盤
function sessionStamp() {
  const tpe = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
  if (tpe.getHours() < 9) tpe.setDate(tpe.getDate() - 1);
  return tpe.toISOString().slice(0, 10);
}
function cacheGet(code) {
  try {
    const raw = localStorage.getItem("twk3:" + code);
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (o.s !== sessionStamp()) return null;
    if (Date.now() - o.t >= CACHE_MS) return null;
    if (o.d > 0 && DIVS[code] == null) DIVS[code] = o.d;
    return o.b;
  } catch (e) { return null; }
}
function cacheSet(code, bars) {
  try { localStorage.setItem("twk3:" + code, JSON.stringify({ t: Date.now(), s: sessionStamp(), b: bars.slice(-140), d: DIVS[code] || 0 })); } catch (e) { /* 配額滿就算了 */ }
}

export function metaName(code) {
  if (NAMES[code]) return NAMES[code];
  try { return localStorage.getItem("twk:name:" + code) || ""; } catch (e) { return ""; }
}
function setName(code, nm) {
  NAMES[code] = nm;
  try { localStorage.setItem("twk:name:" + code, nm); } catch (e) {}
}

async function fetchBars(code) {
  const hit = cacheGet(code);
  if (hit) return hit;
  for (const sfx of [".TW", ".TWO"]) {
    try {
      const d = await getJSON("https://query1.finance.yahoo.com/v8/finance/chart/" + code + sfx +
        "?range=1y&interval=1d&events=div", 1);
      const r = d && d.chart && d.chart.result && d.chart.result[0];
      if (!r || !r.timestamp) continue;
      const mn = r.meta && (r.meta.longName || r.meta.shortName);
      if (mn) setName(code, String(mn).trim());
      try {
        const dv = r.events && r.events.dividends;
        if (dv) {
          const tot = Object.keys(dv).reduce((a, k) => a + (+dv[k].amount || 0), 0);
          if (tot > 0) { DIVS[code] = Math.round(tot * 10000) / 10000; try { localStorage.setItem("twk:div:" + code, String(DIVS[code])); } catch (e) {} }
        }
      } catch (e) {}
      const q = r.indicators.quote[0], out = [];
      for (let i = 0; i < r.timestamp.length; i++) {
        if (q.close[i] == null || q.open[i] == null) continue;
        out.push({
          d: new Date(r.timestamp[i] * 1000).toISOString().slice(0, 10),
          o: q.open[i], h: q.high[i], l: q.low[i], c: q.close[i], v: q.volume[i] || 0
        });
      }
      if (out.length > 70) { cacheSet(code, out); return out; }
    } catch (e) { /* 換下一個交易所代號 */ }
  }
  return null;
}

// 外資買賣超：證交所 T86（每日全市場三大法人），一天一次請求，整日結果存快取共用。
// dates 為 YYYY-MM-DD 陣列（新到舊），onEach(date, netLots) 每抓到一天就回報。
async function t86Day(ymd) {
  const key = "twk:t86:" + ymd;
  try {
    const raw = localStorage.getItem(key);
    if (raw) return JSON.parse(raw);
  } catch (e) {}
  const d = await getJSON("https://www.twse.com.tw/rwd/zh/fund/T86?date=" + ymd + "&selectType=ALL&response=json", 1);
  if (!d || d.stat !== "OK" || !d.data) throw new Error("no data");
  const head = d.fields || [];
  const iCode = 0;
  const iNet = head.findIndex(h => /外陸資買賣超股數\(不含外資自營商\)|外資買賣超股數/.test(h));
  const map = {};
  d.data.forEach(r => {
    const c = String(r[iCode]).trim();
    if (!okCode(c)) return;
    map[c] = Math.round(num(r[iNet >= 0 ? iNet : 4]) / 1000);
  });
  try { localStorage.setItem(key, JSON.stringify(map)); } catch (e) {}
  return map;
}

// 上櫃三大法人（櫃買中心），日期用民國年
async function tpexDay(ymd) {
  const key = "twk:otc3i:" + ymd;
  try { const raw = localStorage.getItem(key); if (raw) return JSON.parse(raw); } catch (e) {}
  const y = +ymd.slice(0, 4) - 1911, roc = y + "/" + ymd.slice(4, 6) + "/" + ymd.slice(6, 8);
  const urls = [
    "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d=" + roc + "&o=json",
    "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date=" + ymd + "&response=json"
  ];
  for (const u of urls) {
    try {
      const d = await getJSON(u, 1);
      const rows = d.aaData || d.tables && d.tables[0] && d.tables[0].data || d.data;
      if (!rows || !rows.length) continue;
      const map = {};
      rows.forEach(r => {
        const c = String(r[0]).trim();
        if (!okCode(c)) return;
        // 外資買賣超股數通常在第 10–12 欄之間，取欄位數最接近的那個
        const cand = [10, 11, 12, 9].map(i => num(r[i])).find(v => v !== 0);
        map[c] = Math.round((cand || 0) / 1000);
      });
      if (Object.keys(map).length) {
        try { localStorage.setItem(key, JSON.stringify(map)); } catch (e) {}
        return map;
      }
    } catch (e) {}
  }
  throw new Error("otc no data");
}

export async function fetchInstFlow(code, dates, onEach) {
  const out = {};
  const CH = 3;
  for (let i = 0; i < dates.length; i += CH) {
    const part = dates.slice(i, i + CH);
    await Promise.all(part.map(async dt => {
      const ymd = dt.replace(/-/g, "");
      let v = null;
      try { const map = await t86Day(ymd); v = map[code]; } catch (e) {}
      if (v == null) { try { const m2 = await tpexDay(ymd); v = m2[code]; } catch (e) {} }
      if (v != null) { out[dt] = v; if (onEach) onEach(dt, v); }
    }));
  }
  return out;
}

// meta: [[code, name, sector, market], ...]；onProgress(done, total, ok)
export async function fetchUniverse(meta, onProgress) {
  const stocks = [];
  let done = 0;
  const CHUNK = 4;
  for (let i = 0; i < meta.length; i += CHUNK) {
    const part = meta.slice(i, i + CHUNK);
    const res = await Promise.all(part.map(async m => {
      const bars = await fetchBars(m[0]);
      const nm = (m[1] && m[1] !== m[0]) ? m[1] : (metaName(m[0]) || m[1]);
      return bars ? { code: m[0], name: nm, sector: m[2], pool: m[3], bars } : null;
    }));
    res.forEach(s => { if (s) stocks.push(s); });
    done += part.length;
    if (onProgress) onProgress(Math.min(done, meta.length), meta.length, stocks.length);
  }
  if (!stocks.length) throw new Error("每一檔的日 K 都抓不到（資料源可能限流，稍後再按一次）");
  // 取全體最新的一根 K 當資料日：單一檔若停牌／資料落後不會拖累整體
  let newest = "";
  stocks.forEach(s => { const d = s.bars[s.bars.length - 1].d; if (d > newest) newest = d; });
  const last = newest.split("-");
  return { asOf: last[0] + " / " + last[1] + " / " + last[2], stocks };
}
