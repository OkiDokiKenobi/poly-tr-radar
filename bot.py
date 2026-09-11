"""Polymarket TR Radar - bilgi amacli Telegram botu (MVP).
Bahis oynatmaz, para toplamaz, yonlendirme linki vermez.
Sadece public veri: gundem + balina + aciklama.
"""
import html
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("poly-tr")

# Not: Bazı ev/ofis ağlarında antivirüs veya proxy self-signed sertifika basar,
# Python doğrulayamaz. Public okuma için doğrulamayı esnetiyoruz.
HEADERS = {"User-Agent": "PolyTR-Radar/1.0 (+telegram bot, info only)"}

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20, verify=False, headers=HEADERS, follow_redirects=True)

DISCLAIMER = (
    "\n\nBilgi amacli, yatirim tavsiyesi degil. "
    "Turkiye'de lisanssiz bahis oynatma/imkan saglama suctur, bu bot bahis oynatmaz."
)


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


def pct(price) -> str:
    try:
        return f"%{float(price)*100:.0f}"
    except (TypeError, ValueError):
        return "-"


def tr_sync(text: str) -> str:
    """EN->TR ceviri, basarisiz olursa orijinali dondur.
    Render IP'si Google'a takildigi icin once MyMemory, sonra Google dene."""
    t = (text or "").strip()
    if not t:
        return t
    t = t[:250]
    try:
        from deep_translator import MyMemoryTranslator
        out = MyMemoryTranslator(source="en", target="tr").translate(t)
        if out and out.strip() and "QUERY LENGTH LIMIT" not in out:
            return out.strip()
    except Exception as e:
        log.warning("mymemory ceviri hata: %s", e)
    try:
        from deep_translator.google import GoogleTranslator
        return GoogleTranslator(source="en", target="tr").translate(t)
    except Exception as e:
        log.warning("google ceviri hata: %s", e)
        return text.strip()


def best_prices(m: dict) -> str:
    """outcomePrices: ["0.62","0.38"] + outcomes: ["Evet","Hayir"] -> 'Evet %62 / Hayir %38'"""
    try:
        outs = m.get("outcomes")
        prcs = m.get("outcomePrices")
        import json
        if isinstance(outs, str):
            outs = json.loads(outs)
        if isinstance(prcs, str):
            prcs = json.loads(prcs)
        return " / ".join(f"{o} {pct(p)}" for o, p in zip(outs, prcs))
    except Exception:
        return "-"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Polymarket TR Radar'a hos geldin.\n\n"
        "/gundem - 24s hacmi en yuksek 10 market\n"
        "/balina - en yuksek hacimli markette 10k$+ islemler\n"
        "/balina <conditionId> - o markette balina islemler\n"
        "/acikla - oran nasil okunur?\n"
        f"{DISCLAIMER}"
    )


async def gundem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Gundemi cekiyorum...")
    try:
        async with make_client() as c:
            r = await c.get(f"{GAMMA}/markets", params={"limit": 50, "closed": "false"})
            r.raise_for_status()
            try:
                markets = r.json()
            except Exception:
                body = (r.text or "")[:200]
                log.error("gamma JSON degil: status=%s ctype=%s body=%r", r.status_code, r.headers.get("content-type"), body)
                await update.message.reply_text(
                    f"API su an duz metin dondu (status {r.status_code}). 1 dk sonra tekrar dene.\nDetay: {body}"
                )
                return
    except Exception as e:
        log.exception("gamma hata")
        await update.message.reply_text(f"Veri alinamadi: {e}")
        return

    # volume24hr'a gore sirala
    def vol(m):
        try:
            return float(m.get("volume24hr") or 0)
        except (TypeError, ValueError):
            return 0

    top = sorted(markets, key=vol, reverse=True)[:10]
    lines = ["<b>24s GUNDEM (hacme gore)</b>"]
    await update.message.reply_text("Basliklar Turkceye cevriliyor...")
    import asyncio
    for i, m in enumerate(top, 1):
        raw_q = str(m.get("question", "-"))[:200]
        tr_q = await asyncio.to_thread(tr_sync, raw_q)
        q = html.escape(tr_q[:150])
        slug = m.get("slug", "")
        link = f"https://polymarket.com/market/{slug}" if slug else "-"
        lines.append(
            f"\n{i}. {q}\n"
            f"   {html.escape(best_prices(m))} | 24s: {fmt_usd(m.get('volume24hr'))} | lik: {fmt_usd(m.get('liquidityNum'))}\n"
            f"   {link}"
        )
    lines.append(DISCLAIMER)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def balina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cond = context.args[0].strip() if context.args else None
    if not cond:
        # conditionId verilmemisse: en yuksek hacimli marketi bul
        await update.message.reply_text("En yuksek hacimli market bulunuyor...")
        try:
            async with make_client() as c:
                r = await c.get(f"{GAMMA}/markets", params={"limit": 20, "closed": "false"})
                r.raise_for_status()
                markets = r.json()
            def vol(m):
                try:
                    return float(m.get("volume24hr") or 0)
                except (TypeError, ValueError):
                    return 0
            top = sorted(markets, key=vol, reverse=True)[0]
            cond = top.get("conditionId")
            q = top.get("question", "-")
        except Exception as e:
            await update.message.reply_text(f"Market bulunamadi: {e}")
            return
    else:
        q = cond

    await update.message.reply_text(f"Balina islemler taraniyor...\n{q}")
    try:
        async with make_client() as c:
            r = await c.get(f"{DATA_API}/trades", params={"market": cond, "limit": 100})
            r.raise_for_status()
            trades = r.json()
    except Exception as e:
        await update.message.reply_text(f"Islem verisi alinamadi: {e}")
        return

    big = []
    for t in trades:
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
            usd = size * price
        except (TypeError, ValueError):
            continue
        if usd >= 10_000:
            big.append((usd, t))
    big.sort(reverse=True, key=lambda x: x[0])
    big = big[:10]

    if not big:
        await update.message.reply_text(
            "Son 100 islemde 10k$+ balina islem yok. Daha sakin market." + DISCLAIMER
        )
        return

    lines = [f"<b>BALINA ({html.escape(str(q)[:100])})</b>"]
    for usd, t in big:
        side = html.escape(str(t.get("side", "-")))
        out = html.escape(str(t.get("outcome", "-")))
        ts = t.get("timestamp")
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%m-%d %H:%M UTC")
        except (TypeError, ValueError):
            dt = "-"
        lines.append(f"\n{fmt_usd(usd)} {side} {out} @ {t.get('price')} | {dt}")
    lines.append(DISCLAIMER)
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def acikla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Oran nasil okunur?\n\n"
        "Evet %62 = piyasa o sonucun %62 ihtimal verdigini dusunuyor demek. "
        "Fiyat 0.62$ ise dogru bilirsen 1$ alirsin.\n\n"
        "3 kural:\n"
        "1. Yuksek oran = yuksek beklenti, dusuk kazanc.\n"
        "2. Hacmi dusuk markette fiyat kolay oynar, aldanma.\n"
        "3. Kaybedecegin parayla oynama, bu kumar sayilabilir.\n"
        f"{DISCLAIMER}"
    )


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN yok. .env dosyasina yaz: TELEGRAM_BOT_TOKEN=xxx")
    # Koyeb/Render free web service saglik kontrolu icin mini HTTP server
    # Platform PORT verir, yoksa 8080. / ve /health -> 200 OK doner.
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
    app.add_handler(CommandHandler("gundem", gundem))
    app.add_handler(CommandHandler("balina", balina))
    app.add_handler(CommandHandler("acikla", acikla))
    log.info("Bot baslatiliyor...")
    app.run_polling()


if __name__ == "__main__":
    main()
