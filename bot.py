"""Coin Radari - takip edilen coinlerde otomatik alarm botu (Faz 1).
Veri: OKX public API (anahtarsiz, global). Bahis yok, emir yok, sadece bilgi.
Alarmlar: fiyat hareketi, hacim patlamasi, likidasyon, fonlama, OI, L/S,
emir defteri dengesizligi, volatilite sikismasi, blok islem.
"""
import html
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OKX = "https://www.okx.com"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("coin-radar")

HEADERS = {"User-Agent": "CoinRadar/1.0 (+telegram bot, info only)"}

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, verify=False, headers=HEADERS, follow_redirects=True)

DISCLAIMER = (
    "\n\nBilgi amacli, yatirim tavsiyesi degil."
)

# --- coin -> OKX USDT perpetual sembolu ---
DESTEKLENEN = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK",
               "BNB", "TON", "TRX", "DOT", "POL", "ARB", "OP", "UNI",
               "LTC", "NEAR", "APT", "SUI", "SEI", "PEPE", "SHIB"]
MAJOR = {"BTC", "ETH"}

def sembol(coin: str) -> str:
    return f"{coin}-USDT-SWAP"

# --- hassas esikler ---
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
COOLDOWN = 45 * 60  # ayni coin+tip icin 45 dk
GECE_BAS, GECE_BIT = 23, 5  # UTC; bu saatlerde sadece cok buyuk olay

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")


def state_yukle() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
            if isinstance(s, dict):
                s.setdefault("chats", {})
                s.setdefault("snap", {})
                s.setdefault("cd", {})
                s.setdefault("last_check", 0)
                # eski listelerdeki desteksiz coinleri temizle
                for cid, coins in s["chats"].items():
                    s["chats"][cid] = [c for c in coins if c in DESTEKLENEN][:10]
                return s
    except (FileNotFoundError, ValueError):
        pass
    except Exception as e:
        log.warning("state okuma hata: %s", e)
    return {"chats": {}, "snap": {}, "cd": {}, "last_check": 0}


def state_kaydet(s: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception as e:
        log.warning("state yazma hata: %s", e)


def fmt_usd(x) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    if v >= 1_000_000:
        return f"${v/1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def gece_mi() -> bool:
    h = datetime.now(timezone.utc).hour
    return h >= GECE_BAS or h < GECE_BIT


def cd_ok(s: dict, anahtar: str, simdi: int, sure: int = COOLDOWN) -> bool:
    if simdi - s["cd"].get(anahtar, 0) >= sure:
        s["cd"][anahtar] = simdi
        return True
    return False


# --- OKX okuma ---
async def ox(c: httpx.AsyncClient, yol: str, params: dict):
    r = await c.get(f"{OKX}{yol}", params=params)
    r.raise_for_status()
    j = r.json()
    if str(j.get("code", "")) != "0":
        raise RuntimeError(f"OKX hata {j.get('code')}: {j.get('msg')}")
    return j.get("data", [])


def okx_mumlar(data: list) -> list:
    """OKX candles (yeniden eskiye) -> [ts,o,h,l,c,vol] eskiden yeniye."""
    out = []
    for k in data:
        try:
            out.append([int(k[0]), str(k[1]), str(k[2]), str(k[3]), str(k[4]), str(k[5])])
        except (TypeError, ValueError, IndexError):
            continue
    return list(reversed(out))


async def sembol_verisi(c: httpx.AsyncClient, inst: str) -> dict:
    v = {"inst": inst}
    try:
        v["m15"] = okx_mumlar(await ox(c, "/api/v5/market/candles",
                                       {"instId": inst, "bar": "15m", "limit": "5"}))
    except Exception as e:
        log.warning("%s m15 hata: %s", inst, e)
        v["m15"] = []
    try:
        v["h1"] = okx_mumlar(await ox(c, "/api/v5/market/candles",
                                      {"instId": inst, "bar": "1H", "limit": "14"}))
    except Exception as e:
        log.warning("%s h1 hata: %s", inst, e)
        v["h1"] = []
    try:
        fr = await ox(c, "/api/v5/public/funding-rate", {"instId": inst})
        v["fon"] = float(fr[0]["fundingRate"]) if fr else None
    except Exception:
        v["fon"] = None
    try:
        oi = await ox(c, "/api/v5/public/open-interest", {"instId": inst})
        v["oi"] = float(oi[0]["oi"]) if oi else None
    except Exception:
        v["oi"] = None
    try:
        ls = await ox(c, "/api/v5/rubik/stat/contracts/long-short-account-count",
                      {"instId": inst, "ccy": "USDT"})
        v["ls"] = float(ls[-1]["longShortRatio"]) if ls else None
    except Exception as e:
        log.warning("%s ls hata: %s", inst, e)
        v["ls"] = None
    try:
        dp = await ox(c, "/api/v5/market/books", {"instId": inst, "sz": "20"})
        b0 = dp[0] if dp else {}
        bid = sum(float(px) * float(sz) for px, sz, *_ in b0.get("bids", []))
        ask = sum(float(px) * float(sz) for px, sz, *_ in b0.get("asks", []))
        v["defter"] = (bid / ask) if ask > 0 else None
    except Exception:
        v["defter"] = None
    try:
        lik = await ox(c, "/api/v5/public/liquidation-orders",
                       {"instId": inst, "mgnMode": "cross", "limit": "100"})
        v["lik"] = lik[0].get("details", []) if lik else []
    except Exception as e:
        log.warning("%s lik hata: %s", inst, e)
        v["lik"] = []
    try:
        v["trd"] = await ox(c, "/api/v5/market/trades", {"instId": inst, "limit": "100"})
    except Exception:
        v["trd"] = []
    return v


def mum_degisim(mumlar, adet: int):
    """Son kapanmis `adet` mumun yuzde degisimi."""
    try:
        if len(mumlar) < adet + 1:
            return None
        ilk_ac = float(mumlar[-(adet + 1)][1])
        son_kap = float(mumlar[-1][4])
        return (son_kap - ilk_ac) / ilk_ac * 100
    except (TypeError, ValueError, IndexError):
        return None


def analiz(coin: str, v: dict, snap: dict, simdi: int, ilk_tarama: bool) -> list:
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
            ts = int(o.get("ts", 0))
            if ts < pencere:
                continue
            usd = float(o.get("px", 0)) * float(o.get("sz", 0))
            top_lik += usd
            ps = str(o.get("posSide", "")).lower()
            if ps == "long":
                long_lik += usd
            elif ps == "short":
                short_lik += usd
            elif str(o.get("side", "")).lower() == "sell":
                long_lik += usd
            else:
                short_lik += usd
        except (TypeError, ValueError):
            continue
    if top_lik >= ESIK_LIK_15M:
        out.append(("lik", f"LIKIDASYON [{coin}] 15 dkda {fmt_usd(top_lik)} "
                           f"(long {fmt_usd(long_lik)} / short {fmt_usd(short_lik)})"))

    fon = v.get("fon")
    if fon is not None and abs(fon) >= ESIK_FON:
        yon = "long kalabaligi" if fon > 0 else "short kalabaligi"
        out.append(("fon", f"FONLAMA [{coin}] %{fon*100:.3f} — {yon}, ters hareket riski"))

    if oi_deg is not None and abs(oi_deg) >= ESIK_OI_1S:
        yatay = d60 is not None and abs(d60) < 1.0
        ek = " + fiyat yatay = pozisyon birikiyor, patlama yakin" if yatay else ""
        out.append(("oi", f"OI [{coin}] 1 saatte %{oi_deg:+.1f}{ek}"))

    ls = v.get("ls")
    if ls is not None and (ls >= ESIK_LS_YUKSEK or ls <= ESIK_LS_DUSUK):
        out.append(("ls", f"L/S [{coin}] oran {ls:.2f} — kalabalik tek tarafta, sert ters mum riski"))

    dft = v.get("defter")
    if dft is not None and (dft >= ESIK_DEFTER or dft <= 1 / ESIK_DEFTER):
        taraf = "altta alis duvari" if dft >= ESIK_DEFTER else "ustte satis duvari"
        out.append(("defter", f"DEFTER [{coin}] bid/ask {dft:.1f}x — {taraf} (spoof olabilir)"))

    try:
        araliklar = []
        for m in v.get("h1", [])[-13:-1]:
            araliklar.append((float(m[2]) - float(m[3])) / float(m[4]) * 100)
        if araliklar:
            son = araliklar[-1]
            if son <= min(araliklar) * 1.05 and son > 0:
                out.append(("squeeze", f"SIKISMA [{coin}] 1s bant son 12 saatin en dari (%{son:.2f}) — breakout yakin"))
    except (TypeError, ValueError, IndexError):
        pass

    blok = 0.0
    for t in v.get("trd", []) or []:
        try:
            usd = float(t.get("px", 0)) * float(t.get("sz", 0))
            if usd >= ESIK_BLOK and usd > blok:
                blok = usd
        except (TypeError, ValueError):
            continue
    if blok:
        out.append(("blok", f"BLOK [{coin}] tek kalemde {fmt_usd(blok)} emir"))

    return out


# --- komutlar ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Coin Radari'na hos geldin.\n\n"
        "/takip BTC ETH SOL - sectiklerine otomatik alarm (15 dk)\n"
        "/durum - takip ettiklerinin anlik ozeti\n"
        "/listem - takip listen\n"
        "/birak BTC - takipten cikar\n\n"
        "Alarmlar: hizli fiyat, likidasyon, fonlama, OI, L/S, defter, sikisma, blok islem."
        + DISCLAIMER)


async def takip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    girilen = []
    for c in context.args:
        c = "".join(ch for ch in c.upper() if ch.isalnum())
        if c in DESTEKLENEN and c not in girilen:
            girilen.append(c)
    if not girilen:
        await update.message.reply_text(
            "Kullanim: /takip BTC ETH SOL\nDesteklenen: " + ", ".join(DESTEKLENEN))
        return
    s = state_yukle()
    mevcut = s["chats"].get(cid, [])
    for c in girilen:
        if c not in mevcut:
            mevcut.append(c)
    s["chats"][cid] = [c for c in mevcut if c in DESTEKLENEN][:10]
    state_kaydet(s)
    await update.message.reply_text(
        f"Takip basladi: {', '.join(s['chats'][cid])}\n"
        "15 dakikada bir kontrol edip onemliyse yazacagim.\n"
        "Anlik ozet: /durum" + DISCLAIMER)


async def birak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    s = state_yukle()
    if not context.args:
        await update.message.reply_text("Kullanim: /birak BTC  veya  /birak hepsi")
        return
    if context.args[0].lower() in ("hepsi", "all", "temizle"):
        s["chats"][cid] = []
    else:
        sil = {"".join(ch for ch in c.upper() if ch.isalnum()) for c in context.args}
        s["chats"][cid] = [c for c in s["chats"].get(cid, []) if c not in sil]
    state_kaydet(s)
    await update.message.reply_text(f"Guncel liste: {', '.join(s['chats'][cid]) or '(bos)'}")


async def listem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = str(update.effective_chat.id)
    s = state_yukle()
    await update.message.reply_text(
        f"Takip ettiklerin: {', '.join(s['chats'].get(cid, [])) or '(bos)'}\n"
        "Ekle: /takip BTC ETH")


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
                    v = await sembol_verisi(c, sembol(coin))
                except Exception as e:
                    satirlar.append(f"{coin}: alinamadi ({e})")
                    continue
                d15 = mum_degisim(v.get("m15", []), 1)
                d60 = mum_degisim(v.get("h1", []), 4)
                fon = v.get("fon")
                parca = [coin]
                parca.append(f"15dk %{d15:+.1f}" if d15 is not None else "15dk -")
                parca.append(f"1s %{d60:+.1f}" if d60 is not None else "1s -")
                parca.append(f"fon %{fon*100:.3f}" if fon is not None else "fon -")
                if v.get("ls") is not None:
                    parca.append(f"L/S {v['ls']:.2f}")
                satirlar.append(" | ".join(parca))
            await update.message.reply_text("\n".join(satirlar) + DISCLAIMER)
    except Exception as e:
        await update.message.reply_text(f"Ozet alinamadi: {e}")


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
            for coin in sorted({c for coins in aktif.values() for c in coins}):
                try:
                    veriler[coin] = await sembol_verisi(c, sembol(coin))
                except Exception as e:
                    log.warning("%s veri hata: %s", coin, e)
    except Exception as e:
        log.warning("alarm fetch hata: %s", e)
        return

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
                         if u.startswith("LIKIDASYON") or u.startswith("HAREKET")]
            if uyari:
                mesaj = "\n\n".join(uyari[:6])
                if len(uyari) > 6:
                    mesaj += f"\n\n(+{len(uyari)-6} uyari daha)"
                try:
                    await context.bot.send_message(chat_id=int(cid), text=mesaj + DISCLAIMER)
                except Exception as e:
                    log.warning("alarm gonderme hata %s: %s", cid, e)
    s["last_check"] = simdi
    state_kaydet(s)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN yok. .env dosyasina yaz: TELEGRAM_BOT_TOKEN=xxx")
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("takip", takip))
    app.add_handler(CommandHandler("birak", birak))
    app.add_handler(CommandHandler("listem", listem))
    app.add_handler(CommandHandler("durum", durum))
    if app.job_queue is None:
        log.warning("job_queue yok (apscheduler kurulmamis olabilir) - alarmlar calismaz!")
    else:
        app.job_queue.run_repeating(alarm_kontrol, interval=900, first=60)
        log.info("Alarm job kuruldu: 15 dakikada bir")
    log.info("Bot baslatiliyor...")
    app.run_polling()


if __name__ == "__main__":
    main()
