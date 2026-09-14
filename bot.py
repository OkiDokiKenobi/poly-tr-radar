"""Coin Radari - Kripto trader'lari icin anlik fiyat, haber ve alarm botu.
Veri: Binance (vadeli) -> OKX yedekli, CoinGecko (piyasa), RSS (haber, TR+EN).
Haberler turkceye cevrilir (MyMemory). Bahis yok, emir yok, bilgi ve alarm.
"""
import html
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FAPI = "https://fapi.binance.com"
OKX = "https://www.okx.com"
COINGECKO = "https://api.coingecko.com/api/v3"
MYMEMORY = "https://api.mymemory.translated.net/get"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("coin-radar")

HEADERS = {"User-Agent": "CoinRadar/1.0 (+telegram bot, info only)"}


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, verify=False,
                             headers=HEADERS, follow_redirects=True)


DISCLAIMER = "\n\nBilgi amacli, yatirim tavsiyesi degil."

# --- Desteklenen coinler ---
DESTEKLENEN = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK",
               "BNB", "TON", "TRX", "DOT", "POL", "ARB", "OP", "UNI",
               "LTC", "NEAR", "APT", "SUI", "SEI", "PEPE", "SHIB"]
MAJOR = {"BTC", "ETH"}

CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
    "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2",
    "LINK": "chainlink", "BNB": "binancecoin", "TON": "the-open-network",
    "TRX": "tron", "DOT": "polkadot", "POL": "matic-network",
    "ARB": "arbitrum", "OP": "optimism", "UNI": "uniswap",
    "LTC": "litecoin", "NEAR": "near", "APT": "aptos",
    "SUI": "sui", "SEI": "sei-network", "PEPE": "pepe", "SHIB": "shiba-inu",
}


def sembol(coin: str) -> str:
    return f"{coin}USDT"


def okx_inst(coin: str) -> str:
    return f"{coin}-USDT-SWAP"


# --- RSS Kaynaklari: (ad, url, dil) dil="tr" ise ceviri yapilmaz ---
RSS_KAYNAKLARI = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "en"),
    ("CoinTelegraph", "https://cointelegraph.com/rss", "en"),
    ("The Block", "https://www.theblock.co/rss.xml", "en"),
    ("KoinMedya", "https://koinmedya.com/feed", "tr"),
    ("KriptoParaHaber", "https://kriptoparahaber.com/feed", "tr"),
    ("CoinTurk", "https://www.coin-turk.com/feed", "tr"),
]

# --- Takvim: FOMC + onemli tarihler ---
TAKVIM = [
    {"tarih": "2025-07-29", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2025-09-16", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2025-09-17", "olay": "FOMC Karari (faiz)", "onem": "cok_yuksek"},
    {"tarih": "2025-10-28", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2025-10-29", "olay": "FOMC Karari (faiz)", "onem": "cok_yuksek"},
    {"tarih": "2025-12-09", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2025-12-10", "olay": "FOMC Karari (faiz)", "onem": "cok_yuksek"},
    {"tarih": "2026-01-27", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2026-03-17", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2026-03-18", "olay": "FOMC Karari (faiz)", "onem": "cok_yuksek"},
    {"tarih": "2026-04-28", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2026-06-16", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2026-06-17", "olay": "FOMC Karari (faiz)", "onem": "cok_yuksek"},
    {"tarih": "2026-07-28", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2026-09-15", "olay": "FOMC Toplantisi", "onem": "yuksek"},
    {"tarih": "2026-09-16", "olay": "FOMC Karari (faiz)", "onem": "cok_yuksek"},
]

# --- Esikler ---
ESIK_15M_MAJOR = 1.0
ESIK_15M_ALT = 1.5
ESIK_1S = 2.0
ESIK_LIK_15M = 500_000
ESIK_FON = 0.0003
ESIK_OI_1S = 3.0
ESIK_LS_YUKSEK = 2.5
ESIK_LS_DUSUK = 0.4
ESIK_DEFTER = 2.0
ESIK_BLOK = 250_000
COOLDOWN = 45 * 60
GECE_BAS, GECE_BIT = 23, 5

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "state.json")


# =================== YARDIMCI ===================

def state_yukle() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            if isinstance(s, dict):
                s.setdefault("chats", {})
                s.setdefault("snap", {})
                s.setdefault("cd", {})
                s.setdefault("last_check", 0)
                s.setdefault("news_seen", [])
                s.setdefault("tr_cache", {})
                for cid, coins in s["chats"].items():
                    s["chats"][cid] = \
                        [c for c in coins if c in DESTEKLENEN][:10]
                return s
    except (FileNotFoundError, ValueError):
        pass
    except Exception as e:
        log.warning("state okuma hata: %s", e)
    return {"chats": {}, "snap": {}, "cd": {}, "last_check": 0,
            "news_seen": [], "tr_cache": {}}


def state_kaydet(s: dict):
    try:
        # ceviri cache'ini sinirlandir
        son = sure = list(s.get("tr_cache", {}).items())[-300:]
        if son:
            s["tr_cache"] = dict(son)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception as e:
        log.warning("state yazma hata: %s", e)


def fmt_usd(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def fmt_pct(x) -> str:
    if x is None:
        return "-"
    return f"%{x:+.2f}"


def sade_para(x) -> str:
    try:
        if x >= 1000:
            return f"{x:,.0f}"
        if x >= 1:
            return f"{x:,.2f}"
        return f"{x:.4f}"
    except (TypeError, ValueError):
        return "-"


def gece_mi() -> bool:
    h = datetime.now(timezone.utc).hour
    return h >= GECE_BAS or h < GECE_BIT


def cd_ok(s: dict, anahtar: str, simdi: int, sure: int = COOLDOWN) -> bool:
    if simdi - s["cd"].get(anahtar, 0) >= sure:
        s["cd"][anahtar] = simdi
        return True
    return False


# =================== CEVIRI ===================

async def cevir(c: httpx.AsyncClient, metin: str) -> str | None:
    """Ingilizce -> Turkce. Hata/limit olursa None (orijinal kullanilir)."""
    if not metin or not metin.strip():
        return None
    try:
        r = await c.get(MYMEMORY,
                        params={"q": metin[:450], "langpair": "en|tr"},
                        timeout=10)
        r.raise_for_status()
        j = r.json()
        tr = (j.get("responseData") or {}).get("translatedText", "")
        if not tr or "MYMEMORY WARNING" in tr:
            return None
        return html.unescape(tr).strip()
    except Exception as e:
        log.warning("ceviri hata: %s", str(e)[:80])
        return None


def haber_tr_metin(s: dict, haber: dict) -> dict:
    """Türkçe kaynaklari aynen; Ingilizce kaynaklari cache'li cevir."""
    g = dict(haber)
    if haber.get("dil") == "tr":
        g["baslik_tr"] = haber.get("baslik")
        g["ozet_tr"] = haber.get("ozet")
        return g
    anahtar = f"tr:{haber.get('baslik', '')[:80]}"
    ozet_anahtar = f"{anahtar}::ozet"
    if anahtar in s.get("tr_cache", {}):
        g["baslik_tr"] = s["tr_cache"][anahtar]
    else:
        g["baslik_tr"] = None  # alarm turunda asenkron cevrilecek
    g["ozet_tr"] = None
    g["_ceviri_anahtar"] = anahtar
    g["_ceviri_ozet_anahtar"] = ozet_anahtar
    return g


# =================== BINANCE + OKX YEDEKLER ===================

async def bnc(c: httpx.AsyncClient, yol: str, params: dict):
    r = await c.get(f"{FAPI}{yol}", params=params)
    r.raise_for_status()
    return r.json()


async def ox(c: httpx.AsyncClient, yol: str, params: dict):
    r = await c.get(f"{OKX}{yol}", params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    if str(j.get("code", "")) != "0":
        raise RuntimeError(f"OKX {j.get('code')}: {j.get('msg')}")
    return j.get("data", [])


def okx_mumlar(data: list) -> list:
    """OKX candles (yeni->eski) -> Binance formati [ts,o,h,l,c,vol] (eski->yeni)."""
    out = []
    for k in data:
        try:
            out.append([int(k[0]), str(k[1]), str(k[2]),
                        str(k[3]), str(k[4]), str(k[5])])
        except (TypeError, ValueError, IndexError):
            continue
    return list(reversed(out))


async def kline_al(c: httpx.AsyncClient, coin: str, aralik_bn: str,
                   aralik_okx: str, limit: int) -> list:
    sym = sembol(coin)
    try:
        return await bnc(c, "/fapi/v1/klines",
                         {"symbol": sym, "interval": aralik_bn,
                          "limit": limit})
    except Exception:
        pass
    try:
        d = await ox(c, "/api/v5/market/candles",
                     {"instId": okx_inst(coin), "bar": aralik_okx,
                      "limit": str(limit)})
        return okx_mumlar(d)
    except Exception as e:
        log.warning("%s kline(okx) hata: %s", coin, str(e)[:80])
        return []


def mum_degisim(mumlar, adet: int):
    try:
        if len(mumlar) < adet + 1:
            return None
        ilk_ac = float(mumlar[-(adet + 1)][1])
        son_kap = float(mumlar[-1][4])
        return (son_kap - ilk_ac) / ilk_ac * 100
    except (TypeError, ValueError, IndexError):
        return None


def mum_volatilite(mumlar):
    try:
        if not mumlar:
            return None
        vol = [(float(m[2]) - float(m[3])) / float(m[4]) * 100
               for m in mumlar[-6:]]
        return sum(vol) / len(vol)
    except (TypeError, ValueError, IndexError):
        return None


async def sembol_verisi(c: httpx.AsyncClient, coin: str) -> dict:
    v = {"coin": coin, "kaynak": "binance"}
    sym = sembol(coin)

    v["m15"] = await kline_al(c, coin, "15m", "15m", 5)
    v["h1"] = await kline_al(c, coin, "1h", "1H", 14)
    v["d1"] = await kline_al(c, coin, "1d", "1D", 7)

    # fonlama: once Binance, olmazsa OKX
    v["fon"] = v["fon_ts"] = None
    try:
        fr = await bnc(c, "/fapi/v1/fundingRate",
                       {"symbol": sym, "limit": 1})
        if fr:
            v["fon"] = float(fr[0]["fundingRate"])
            v["fon_ts"] = int(fr[0]["fundingTime"])
    except Exception:
        try:
            d = await ox(c, "/api/v5/public/funding-rate",
                         {"instId": okx_inst(coin)})
            if d:
                v["fon"] = float(d[0].get("fundingRate", 0)) or None
                v["kaynak"] = "okx"
        except Exception:
            pass

    # OI
    v["oi"] = None
    try:
        oi = await bnc(c, "/fapi/v1/openInterest", {"symbol": sym})
        v["oi"] = float(oi.get("openInterest", 0)) or None
    except Exception:
        try:
            d = await ox(c, "/api/v5/public/open-interest",
                         {"instId": okx_inst(coin)})
            if d:
                v["oi"] = float(d[0].get("oi", 0)) or None
        except Exception:
            pass

    # L/S orani
    v["ls"] = v["ls_long"] = v["ls_short"] = None
    try:
        ls = await bnc(c, "/futures/data/globalLongShortAccountRatio",
                       {"symbol": sym, "period": "15m", "limit": 2})
        if ls:
            v["ls"] = float(ls[-1]["longShortRatio"])
            v["ls_long"] = float(ls[-1].get("longAccount", 0))
            v["ls_short"] = float(ls[-1].get("shortAccount", 0))
    except Exception:
        try:
            d = await ox(c, "/api/v5/rubik/stat/contracts/"
                            "long-short-account-count",
                         {"instId": okx_inst(coin), "ccy": "USDT"})
            if d:
                v["ls"] = float(d[-1]["longShortRatio"])
        except Exception as e:
            log.warning("%s ls hata: %s", coin, str(e)[:60])

    # emir defteri dengesi
    v["defter"] = None
    try:
        dp = await bnc(c, "/fapi/v1/depth", {"symbol": sym, "limit": 20})
        bid = sum(float(b[1]) * float(b[0]) for b in dp.get("bids", []))
        ask = sum(float(a[1]) * float(a[0]) for a in dp.get("asks", []))
        v["defter"] = (bid / ask) if ask > 0 else None
    except Exception:
        try:
            d = await ox(c, "/api/v5/market/books", {"instId": okx_inst(coin),
                                                     "sz": "20"})
            b0 = d[0] if d else {}
            bid = sum(float(px) * float(sz) for px, sz, *_ in b0.get("bids", []))
            ask = sum(float(px) * float(sz) for px, sz, *_ in b0.get("asks", []))
            v["defter"] = (bid / ask) if ask > 0 else None
        except Exception:
            pass

    # likidasyon: Binance kapatildiysa OKX (state=filled)
    v["lik"] = []
    try:
        v["lik"] = await bnc(c, "/fapi/v1/forceOrders",
                             {"symbol": sym, "limit": 100})
    except Exception:
        pass
    if not v["lik"]:
        try:
            d = await ox(c, "/api/v5/public/liquidation-orders",
                         {"instType": "SWAP", "uly": f"{coin}-USDT",
                          "state": "filled", "limit": "50"})
            for lot in d:
                for det in lot.get("details", []):
                    v["lik"].append({
                        "time": det.get("time") or det.get("ts"),
                        "price": det.get("bkPx"),
                        "origQty": det.get("sz"),
                        "posSide": str(det.get("posSide", "")).lower(),
                        "okx": True,
                    })
        except Exception as e:
            log.warning("%s okx lik hata: %s", coin, str(e)[:60])

    # blok islemler
    v["trd"] = []
    try:
        v["trd"] = await bnc(c, "/fapi/v1/aggTrades",
                             {"symbol": sym, "limit": 100})
    except Exception:
        pass
    if not v["trd"]:
        try:
            v["trd"] = await ox(c, "/api/v5/market/trades",
                                {"instId": okx_inst(coin), "limit": "100"})
        except Exception:
            pass

    # son fiyat
    v["son_fiyat"] = None
    mk = None
    try:
        mk = await bnc(c, "/fapi/v1/premiumIndex", {"symbol": sym})
        v["son_fiyat"] = float(mk.get("markPrice", 0)) or None
        v["mark"] = v["son_fiyat"]
    except Exception:
        try:
            d = await ox(c, "/api/v5/market/ticker", {"instId": okx_inst(coin)})
            if d:
                v["son_fiyat"] = float(d[0].get("last", 0)) or None
                v["mark"] = v["son_fiyat"]
        except Exception:
            pass
    return v


# =================== COINGECKO ===================

async def cg_verisi(c: httpx.AsyncClient, coin: str) -> dict:
    cg_id = CG_IDS.get(coin)
    if not cg_id:
        return {}
    try:
        r = await c.get(f"{COINGECKO}/coins/{cg_id}",
                        params={"localization": "false", "tickers": "false",
                                "community_data": "false",
                                "developer_data": "false",
                                "sparkline": "false"})
        r.raise_for_status()
        d = r.json()
        md = d.get("market_data", {})
        return {
            "mc": md.get("market_cap", {}).get("usd"),
            "vol24": md.get("total_volume", {}).get("usd"),
            "d24": md.get("price_change_percentage_24h"),
            "d7": md.get("price_change_percentage_7d"),
            "d30": md.get("price_change_percentage_30d"),
            "ath": md.get("ath", {}).get("usd"),
            "ath_pct": md.get("ath_change_percentage", {}).get("usd"),
            "supply": md.get("circulating_supply"),
            "supply_max": md.get("max_supply"),
        }
    except Exception as e:
        log.warning("CG %s hata: %s", coin, str(e)[:60])
        return {}


# =================== RSS HABER ===================

def _rss_temizle(html_str: str) -> str:
    if not html_str:
        return ""
    t = re.sub(r"<[^>]+>", "", html_str)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 200:
        return t[:200] + "..."
    return t


async def haberleri_cek(c: httpx.AsyncClient, limit: int = 6) -> list:
    tum = []
    for ad, url, dil in RSS_KAYNAKLARI:
        try:
            r = await c.get(url, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            items = root.findall(".//item") or root.findall(".//entry")
            for item in items[:8]:
                baslik = item.findtext("title") or ""
                link = item.findtext("link") or ""
                ozet = item.findtext("description") or ""
                yayin = (item.findtext("pubDate")
                         or item.findtext("updated")
                         or item.findtext("published") or "")
                if baslik:
                    tum.append({
                        "baslik": baslik.strip(),
                        "link": link.strip(),
                        "ozet": _rss_temizle(ozet),
                        "kaynak": ad,
                        "dil": dil,
                        "yayin": yayin.strip(),
                    })
        except Exception as e:
            log.warning("RSS %s hata: %s", ad, str(e)[:80])
    tum.sort(key=lambda x: x["yayin"], reverse=True)
    seen = set()
    benzersiz = []
    for h in tum:
        key = h["baslik"][:60].lower()
        if key not in seen:
            seen.add(key)
            benzersiz.append(h)
    return benzersiz[:limit]


# =================== TAKVIM ===================

def takvim_gosterim() -> str:
    bugun = datetime.now(timezone.utc).date()
    satirlar = []
    for t in sorted(TAKVIM, key=lambda x: x["tarih"]):
        try:
            dt = datetime.strptime(t["tarih"], "%Y-%m-%d").date()
        except ValueError:
            continue
        kalan = (dt - bugun).days
        if kalan < 0:
            continue
        onem = {"cok_yuksek": "!!", "yuksek": "!", "orta": ""}.get(
            t["onem"], "")
        if kalan == 0:
            etiket = "BUGUN"
        elif kalan == 1:
            etiket = "YARIN"
        else:
            etiket = f"{kalan} gun"
        satirlar.append(
            f"{onem} {t['tarih']} {etiket}: {t['olay']}")
    return "\n".join(satirlar[:12]) or "Yaklasan etkinlik yok."


# =================== ANALIZ ===================

def analiz(coin: str, v: dict, snap: dict, simdi: int,
           ilk_tarama: bool) -> list:
    out = []
    major = coin in MAJOR
    d15 = mum_degisim(v.get("m15", []), 1)
    d60 = mum_degisim(v.get("h1", []), 4)

    oi = v.get("oi")
    oi_gecmis = snap.get("oi_tarihce", [])
    oi_deg = None
    if oi and len(oi_gecmis) >= 4 and oi_gecmis[-4]:
        oi_deg = (oi - oi_gecmis[-4]) / oi_gecmis[-4] * 100
    if oi:
        oi_gecmis = (oi_gecmis + [oi])[-8:]
    snap["oi_tarihce"] = oi_gecmis

    if ilk_tarama:
        return out

    esik15 = ESIK_15M_MAJOR if major else ESIK_15M_ALT
    if d15 is not None and abs(d15) >= esik15:
        out.append(("fiyat15", f"HIZ [{coin}] 15 dkda %{d15:+.1f}"))
    if d60 is not None and abs(d60) >= ESIK_1S:
        out.append(("fiyat60", f"HAREKET [{coin}] 1 saatte %{d60:+.1f}"))

    pencere = simdi * 1000 - 15 * 60 * 1000
    top_lik = long_lik = short_lik = 0.0
    for o in v.get("lik", []) or []:
        try:
            ts = int(o.get("time", 0) or 0)
            if ts < pencere:
                continue
            usd = float(o.get("price", 0)) * float(o.get("origQty", 0))
            top_lik += usd
            ps = str(o.get("posSide", "")).lower()
            if ps == "long":
                long_lik += usd
            elif ps == "short":
                short_lik += usd
            elif v.get("mark"):
                if float(o.get("price", 0)) < v["mark"]:
                    long_lik += usd
                else:
                    short_lik += usd
        except (TypeError, ValueError):
            continue
    if top_lik >= ESIK_LIK_15M:
        out.append(("lik", f"LIKIDASYON [{coin}] 15 dkda {fmt_usd(top_lik)} "
                           f"(long {fmt_usd(long_lik)} / "
                           f"short {fmt_usd(short_lik)})"))

    fon = v.get("fon")
    if fon is not None and abs(fon) >= ESIK_FON:
        yon = "long kalabaligi" if fon > 0 else "short kalabaligi"
        out.append(("fon", f"FONLAMA [{coin}] %{fon*100:.3f} — {yon}"))

    if oi_deg is not None and abs(oi_deg) >= ESIK_OI_1S:
        yatay = d60 is not None and abs(d60) < 1.0
        ek = " + fiyat yatay = patlama riski" if yatay else ""
        out.append(("oi", f"OI [{coin}] 1 saatte %{oi_deg:+.1f}{ek}"))

    ls = v.get("ls")
    if ls is not None and (ls >= ESIK_LS_YUKSEK or ls <= ESIK_LS_DUSUK):
        out.append(("ls", f"L/S [{coin}] oran {ls:.2f} — tek taraf agirlikli"))

    dft = v.get("defter")
    if dft is not None and (dft >= ESIK_DEFTER or dft <= 1 / ESIK_DEFTER):
        taraf = "alis duvari" if dft >= ESIK_DEFTER else "satis duvari"
        out.append(("defter", f"DEFTER [{coin}] bid/ask {dft:.1f}x — {taraf}"))

    try:
        araliklar = []
        for m in v.get("h1", [])[-13:-1]:
            araliklar.append((float(m[2]) - float(m[3])) / float(m[4]) * 100)
        if araliklar:
            son = araliklar[-1]
            if son <= min(araliklar) * 1.05 and son > 0:
                out.append(("squeeze", f"SIKISMA [{coin}] bant daralma — "
                                       f"breakout yakin"))
    except (TypeError, ValueError, IndexError):
        pass

    blok = 0.0
    for t in v.get("trd", []) or []:
        try:
            if t.get("okx"):
                usd = float(t.get("px", 0)) * float(t.get("sz", 0))
            else:
                usd = float(t.get("p", 0)) * float(t.get("q", 0))
            if usd >= ESIK_BLOK and usd > blok:
                blok = usd
        except (TypeError, ValueError):
            continue
    if blok:
        out.append(("blok", f"BLOK [{coin}] tek kalemde {fmt_usd(blok)} emir"))

    return out


# =================== KOMUTLAR ===================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Coin Radari'na hos geldin.\n\n"
        "TAKIP:\n"
        "  /takip BTC ETH SOL — otomatik alarm listene ekle\n"
        "  /listem — takip listeni goster\n"
        "  /birak BTC — takipten cikar\n\n"
        "FIYAT & PIYASA:\n"
        "  /fiyat BTC — fiyat, hacim, market cap, volatilite\n"
        "  /fonlama BTC — funding, OI, long/short orani\n\n"
        "HABERLER & TAKVIM:\n"
        "  /haber — son haberler (Turkce cevirili)\n"
        "  /takvim — yaklasan FOMC, ETF, unlock tarihleri\n\n"
        "GENEL:\n"
        "  /durum — takip listenin anlik ozeti\n"
        "  /yardim — bu mesaj\n\n"
        "Veri: Binance + OKX yedekli, CoinGecko, RSS (TR+EN, aut ceviri)"
        + DISCLAIMER)


async def takip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    red = []
    girilen = []
    for c in context.args:
        c = "".join(ch for ch in c.upper() if ch.isalnum())
        if not c:
            continue
        if c in DESTEKLENEN:
            if c not in girilen:
                girilen.append(c)
        else:
            red.append(c)
    if not girilen:
        await update.message.reply_text(
            "Kullanim: /takip BTC ETH SOL\n\nDesteklenen: "
            + ", ".join(DESTEKLENEN))
        return
    s = state_yukle()
    mevcut = s["chats"].get(cid, [])
    for c in girilen:
        if c not in mevcut:
            mevcut.append(c)
    s["chats"][cid] = [c for c in mevcut if c in DESTEKLENEN][:10]
    state_kaydet(s)
    mesaj = f"Takip basladi: {', '.join(s['chats'][cid])}"
    if red:
        mesaj += f"\n\nDesteklenmiyor: {', '.join(red)}"
    mesaj += ("\n\n15 dk'da bir kontrol, onemliyse yazacagim.\n"
              "Anlik ozet: /durum\nHaberler: /haber")
    await update.message.reply_text(mesaj + DISCLAIMER)


async def birak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    s = state_yukle()
    if not context.args:
        await update.message.reply_text(
            "Kullanim: /birak BTC  veya  /birak hepsi")
        return
    if context.args[0].lower() in ("hepsi", "all", "temizle"):
        s["chats"][cid] = []
    else:
        sil = {"".join(ch for ch in c.upper() if ch.isalnum())
               for c in context.args}
        s["chats"][cid] = [c for c in s["chats"].get(cid, [])
                           if c not in sil]
    state_kaydet(s)
    await update.message.reply_text(
        f"Guncel liste: {', '.join(s['chats'][cid]) or '(bos)'}")


async def listem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    s = state_yukle()
    coins = s["chats"].get(cid, [])
    if not coins:
        await update.message.reply_text("Liste bos. Ornek: /takip BTC ETH")
        return
    await update.message.reply_text(
        "Takip listin:\n" + "\n".join(
            f"  {i+1}. {c}" for i, c in enumerate(coins)
        ) + "\n\nYeni ekle: /takip SOL DOGE")
    return


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Kullanim: /price BTC\nDesteklenen: " + ", ".join(DESTEKLENEN))
        return
    coin = context.args[0].upper()
    coin = "".join(ch for ch in coin if ch.isalnum())
    if coin not in DESTEKLENEN:
        await update.message.reply_text(f"{coin} desteklenmiyor.")
        return
    await update.message.reply_text(f"{coin} verisi cekiliyor...")

    try:
        async with make_client() as c:
            bv = await sembol_verisi(c, coin)
            cv = await cg_verisi(c, coin)

            satir = [f"<b>{coin}/USDT</b>"]
            fiyat = bv.get("son_fiyat")
            if fiyat:
                satir.append(f"Fiyat: ${sade_para(fiyat)}")

            d24 = mum_degisim(bv.get("d1", []), 1)
            if d24 is None:
                d24 = cv.get("d24")
            d7 = cv.get("d7")
            d30 = cv.get("d30")
            deg = []
            if d24 is not None:
                deg.append(f"24s {fmt_pct(d24)}")
            if d7 is not None:
                deg.append(f"7g {fmt_pct(d7)}")
            if d30 is not None:
                deg.append(f"30g {fmt_pct(d30)}")
            if deg:
                satir.append("Degisim: " + " | ".join(deg))

            if cv.get("mc"):
                satir.append(f"Market Cap: {fmt_usd(cv['mc'])}")
            if cv.get("vol24"):
                satir.append(f"24s Hacim: {fmt_usd(cv['vol24'])}")

            vol = mum_volatilite(bv.get("h1", []))
            if vol is not None:
                satir.append(f"Volatilite (1h): %{vol:.2f}")

            if cv.get("ath") and fiyat:
                satir.append(f"ATH: ${sade_para(cv['ath'])} "
                             f"({fmt_pct(cv['ath_pct'])})")

            fon = bv.get("fon")
            if fon is not None:
                satir.append(f"Fonlama: %{fon*100:.4f}")

            if bv.get("oi"):
                satir.append(f"OI: {bv['oi']:,.0f}")

            if bv.get("ls") is not None:
                satir.append(f"L/S: {bv['ls']:.2f}")
            kaynak = bv.get("kaynak", "binance")
            if kaynak == "okx":
                satir.append("(veri kaynagi: OKX)")

            if cv.get("supply") and cv.get("supply_max"):
                pct = cv["supply"] / cv["supply_max"] * 100
                satir.append(f"Dolasim: {cv['supply']:,.0f} / "
                             f"{cv['supply_max']:,.0f} ({pct:.1f}%)")

            await update.message.reply_text(
                "\n".join(satir) + DISCLAIMER, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Veri alinamadi: {e}")


async def funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanim: /funding BTC")
        return
    coin = context.args[0].upper()
    coin = "".join(ch for ch in coin if ch.isalnum())
    if coin not in DESTEKLENEN:
        await update.message.reply_text(f"{coin} desteklenmiyor.")
        return
    await update.message.reply_text(f"{coin} funding verisi cekiliyor...")

    try:
        async with make_client() as c:
            v = await sembol_verisi(c, coin)
            satir = [f"<b>{coin} — Fonlama & Piyasa</b>"]
            fon = v.get("fon")
            if fon is not None:
                yon = "long odiyor" if fon > 0 else "short odiyor"
                gunluk = fon * 3 * 3 * 100
                satir.append(f"Fonlama Orani: %{fon*100:.4f} ({yon})")
                satir.append(f"  Tahmini gunluk: %{gunluk:.4f}")
            else:
                satir.append("Fonlama: alinamadi")
            if v.get("oi"):
                satir.append(f"Acik Pozisyon (OI): {v['oi']:,.0f}")
            if v.get("ls") is not None:
                satir.append(
                    f"Long/Short: {v['ls']:.2f} "
                    f"(L:{v.get('ls_long', 0)*100:.0f}%"
                    f" / S:{v.get('ls_short', 0)*100:.0f}%)")
            dft = v.get("defter")
            if dft is not None:
                durum = "dengeli" if 0.7 < dft < 1.3 else (
                    "alis agir" if dft > 1 else "satis agir")
                satir.append(f"Emir Defteri: {dft:.2f}x ({durum})")
            if v.get("kaynak") == "okx":
                satir.append("(veri kaynagi: OKX)")
            await update.message.reply_text(
                "\n".join(satir) + DISCLAIMER, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Veri alinamadi: {e}")


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Haberler cekiliyor...")
    try:
        async with make_client() as c:
            haberler = await haberleri_cek(c, limit=6)
            s = state_yukle()
            satirlar = []
            for i, h in enumerate(haberler, 1):
                g = haber_tr_metin(s, h)
                baslik = g["baslik_tr"]
                ozet = ""
                if g["dil"] == "tr":
                    ozet = g.get("ozet", "")
                else:
                    ozet = " ".join(g.get("ozet", "").split())[:150]
                bk = g.get("_ceviri_anahtar")
                if bk:
                    tr_b = await cevir(c, g["baslik"][:200])
                    if tr_b:
                        baslik = tr_b
                        s["tr_cache"][bk] = tr_b
                if bk and ozet:
                    tr_o = await cevir(c, ozet[:350])
                    if tr_o:
                        ozet = tr_o
                if dil_kaynak := g.get("dil"):
                    kaynak_etiket = (f"{g['kaynak']} (TR)" if dil_kaynak == "tr"
                                     else f"{g['kaynak']} (cevirildi)")
                else:
                    kaynak_etiket = g.get("kaynak", "")
                parca = [f"<b>{i}. {html.escape(baslik)}</b>",
                         f"  {kaynak_etiket}"]
                if ozet:
                    parca.append(f"  {html.escape(ozet[:180])}")
                parca.append(f"  {g['link']}")
                satirlar.append("\n".join(parca))
            state_kaydet(s)
        await update.message.reply_text(
            "\n\n".join(satirlar) + DISCLAIMER, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"Haber alinamadi: {e}")


async def calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Yaklasan Etkinlikler</b>\n\n" + takvim_gosterim()
        + "\n\n!! = Cok yuksek onem" + DISCLAIMER, parse_mode="HTML")


async def durum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    s = state_yukle()
    coins = s["chats"].get(cid, [])
    if not coins:
        await update.message.reply_text("Liste bos. Ornek: /takip BTC ETH SOL")
        return
    await update.message.reply_text("Anlik ozet cekiliyor...")
    try:
        async with make_client() as c:
            satirlar = []
            for coin in coins[:10]:
                try:
                    v = await sembol_verisi(c, coin)
                except Exception as e:
                    satirlar.append(f"{coin}: hata ({e})")
                    continue
                d15 = mum_degisim(v.get("m15", []), 1)
                d60 = mum_degisim(v.get("h1", []), 4)
                fon = v.get("fon")
                ls = v.get("ls")
                fiyat = v.get("son_fiyat")
                parca = [coin]
                if fiyat:
                    parca.append(f"${sade_para(fiyat)}")
                parca.append(f"15dk {fmt_pct(d15)}" if d15 is not None
                             else "15dk -")
                parca.append(f"1s {fmt_pct(d60)}" if d60 is not None else "1s -")
                parca.append(f"fon %{fon*100:.3f}" if fon is not None
                             else "fon -")
                if ls is not None:
                    parca.append(f"L/S {ls:.2f}")
                satirlar.append(" | ".join(parca))
            await update.message.reply_text(
                "\n".join(satirlar) + DISCLAIMER)
    except Exception as e:
        await update.message.reply_text(f"Ozet alinamadi: {e}")


async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# =================== ALARM SISTEMI ===================

async def alarm_kontrol(context: ContextTypes.DEFAULT_TYPE):
    simdi = int(time.time())
    s = state_yukle()
    aktif = {cid: [c for c in coins if c in DESTEKLENEN]
             for cid, coins in s["chats"].items() if coins}
    if not aktif:
        return
    ilk = not s.get("last_check")

    try:
        async with make_client() as c:
            veriler = {}
            tum_coinler = sorted({c for coins in aktif.values() for c in coins})
            for coin in tum_coinler:
                try:
                    veriler[coin] = await sembol_verisi(c, coin)
                except Exception as e:
                    log.warning("%s veri hata: %s", coin, str(e)[:80])

            for cid, coins in aktif.items():
                uyari = []
                for coin in coins:
                    v = veriler.get(coin)
                    if not v:
                        continue
                    snap = s["snap"].get(coin, {})
                    for tip, mesaj in analiz(coin, v, snap, simdi, ilk):
                        if tip == "squeeze":
                            if cd_ok(s, f"{coin}:{tip}", simdi, 6 * 3600):
                                uyari.append(mesaj)
                        elif cd_ok(s, f"{coin}:{tip}", simdi):
                            uyari.append(mesaj)
                    s["snap"][coin] = snap

                if uyari and not ilk:
                    if gece_mi():
                        uyari = [u for u in uyari
                                 if u.startswith("LIKIDASYON")
                                 or u.startswith("HAREKET")]
                    if uyari:
                        mesaj = "\n\n".join(uyari[:6])
                        if len(uyari) > 6:
                            mesaj += f"\n\n(+{len(uyari)-6} uyari daha)"
                        try:
                            await context.bot.send_message(
                                chat_id=int(cid), text=mesaj + DISCLAIMER)
                        except Exception as e:
                            log.warning("alarm gonderme hata %s: %s",
                                        cid, str(e)[:80])

            # --- Haber alarmi (30 dk'da bir, 6 kaynak) ---
            if not ilk and cd_ok(s, "_news_scan", simdi, 30 * 60):
                try:
                    haberler = await haberleri_cek(c, limit=12)
                    gecerli = [h for h in haberler
                               if h["baslik"][:80]
                               not in s.get("news_seen", [])]
                    if gecerli:
                        # ceviri icin sirayla, mymemory limitini koru
                        cevrilen = []
                        for h in gecerli[:3]:
                            g = haber_tr_metin(s, h)
                            if g["dil"] == "tr":
                                baslik = g["baslik"]
                            else:
                                bk = g.get("_ceviri_anahtar", "")
                                tr_b = s["tr_cache"].get(bk) if bk else None
                                if not tr_b and bk:
                                    tr_b = await cevir(c, g["baslik"][:200])
                                    if tr_b:
                                        s["tr_cache"][bk] = tr_b
                                baslik = tr_b or g["baslik"]
                            cevrilen.append((baslik, g))
                        for cid in aktif:
                            blok = "\n\n".join(
                                f"<b>{html.escape(b)}</b>\n  "
                                f"{g['kaynak']} | {g['link']}"
                                for b, g in cevrilen)
                            try:
                                await context.bot.send_message(
                                    chat_id=int(cid),
                                    text=f"<b>YENI HABER</b>\n\n{blok}"
                                         + DISCLAIMER,
                                    parse_mode="HTML")
                            except Exception as e:
                                log.warning("haber gonder hata: %s",
                                            str(e)[:80])
                        state_kaydet(s)
                    s["news_seen"] = [h["baslik"][:80]
                                      for h in haberler[:40]]
                except Exception as e:
                    log.warning("haber alarm hata: %s", str(e)[:80])

    except Exception as e:
        log.warning("alarm fetch hata: %s", str(e)[:80])
        return

    s["last_check"] = simdi
    state_kaydet(s)


# =================== MAIN ===================

def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN yok .env'de: TELEGRAM_BOT_TOKEN=xxx")
    port = int(os.getenv("PORT", "8080"))

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args):
            pass

    threading.Thread(
        target=HTTPServer(("0.0.0.0", port), Health).serve_forever,
        daemon=True,
    ).start()
    log.info("Health server port %s", port)

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("basla", start))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yardim", help))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("takip", takip))
    app.add_handler(CommandHandler("birak", birak))
    app.add_handler(CommandHandler("listem", listem))
    app.add_handler(CommandHandler("fiyat", price))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("fonlama", funding))
    app.add_handler(CommandHandler("funding", funding))
    app.add_handler(CommandHandler("haber", news))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("takvim", calendar))
    app.add_handler(CommandHandler("calendar", calendar))
    app.add_handler(CommandHandler("calender", calendar))
    app.add_handler(CommandHandler("durum", durum))

    if app.job_queue is None:
        log.warning("job_queue yok - alarmlar calismaz!")
    else:
        app.job_queue.run_repeating(alarm_kontrol, interval=900, first=60)
        log.info("Alarm job kuruldu: 15 dk aralikla")

    log.info("Bot baslatiliyor...")
    app.run_polling()


if __name__ == "__main__":
    main()