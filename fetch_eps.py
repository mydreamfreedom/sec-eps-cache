"""
fetch_eps.py
Dijalankan oleh GitHub Actions (bukan Apps Script) - IP GitHub Actions gak
kena block "Undeclared Automated Tool" kayak IP Apps Script.

Alur:
1. Baca tickers.txt (list ticker BB Screener).
2. Ambil CIK tiap ticker dari mirror jadchaar/sec-cik-mapper (sama sumber
   yang dipakai Apps Script buat CIK Map).
3. Buat tiap ticker, fetch EPS 5 tahun + Revenue 5 tahun dari SEC EDGAR
   companyconcept API.
4. Skip ticker yang udah di-cache dan masih fresh (<30 hari) - biar gak
   fetch ulang semua ticker tiap hari, cuma yang baru/basi aja.
5. Simpan semua hasil ke eps_cache.json di root repo.

PENTING: ganti CONTACT_EMAIL di bawah ke email asli kamu sebelum commit.
SEC EDGAR mewajibkan User-Agent berisi identitas asli (kebijakan resmi
mereka, bukan proteksi tambahan dari kita).

============================================================
FIX v2 (lihat CATATAN BUG di bawah) - Revenue CAGR 5Y ngaco
============================================================
BUG LAMA: `years = len(sorted_asc) - 1` menghitung JUMLAH DATA POINT,
bukan rentang tahun kalender sebenarnya. Kalau tag "Revenues" di SEC
punya gap (perusahaan pindah tag pelaporan, contoh umum: dari "Revenues"
ke "RevenueFromContractWithCustomerExcludingAssessedTax" sekitar
2018-2019), hasilnya cuma 2 data point valid padahal jaraknya beneran
5-6 tahun kalender. years dihitung "1" padahal harusnya "5-6" -> growth
5 tahun ke-kompres jadi growth 1 tahun -> CAGR meledak jadi ratusan/
ribuan persen (ini penyebab APP=4908%, LHX=664%, dll di screenshot).

FIX: years dihitung dari SELISIH fy (fiscal year) beneran antara titik
tertua & terbaru, bukan dari jumlah titik data. Ditambah sanity cap:
kalau hasil CAGR masih implausible (>150% atau <-90%), di-set None +
di-flag di reason, daripada lolos ke sheet sebagai angka ngaco.
============================================================

============================================================
FIX v3 - AUTO TICKER SYNC (gak perlu edit tickers.txt manual lagi)
============================================================
Sebelumnya: ticker baru yang lolos gate BB Screener harus ditambahin
MANUAL ke tickers.txt via GitHub web UI tiap kali ada kandidat baru.

SEKARANG: load_tickers() narik daftar Symbol OTOMATIS dari tab
BB_SCREENER_ANALYSIS di Google Sheets (via fitur "Publish to web" jadi
CSV - link publik read-only, gak butuh API key/token). tickers.txt masih
dipertahankan sebagai watchlist TAMBAHAN manual (opsional, misal mau
selalu track ticker tertentu meski belum tentu lolos gate) - hasil akhir
adalah gabungan (union) keduanya, di-dedupe.

SETUP SEKALI (di Google Sheets):
1. File > Share > Publish to web
2. Pilih sheet "BB_SCREENER_ANALYSIS" (bukan "Entire Document")
3. Format: Comma-separated values (.csv)
4. Klik Publish, copy link yang muncul
5. Paste link itu ke SHEET_CSV_URL di bawah, commit ke repo

CATATAN PRIVASI: link publish-to-web itu PUBLIK - siapa aja yang punya
link bisa lihat isi kolom (symbol, skor, dll), meski gak ke-index Google
search. Kalau data ini sensitif, jangan pakai cara ini - tetap manual
edit tickers.txt aja.
============================================================
"""

import csv
import io
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

CONTACT_EMAIL = "effendilay85@gmail.com"  # ganti kalau perlu
USER_AGENT = f"BOS-Indonesia-Research {CONTACT_EMAIL}"

CIK_MAP_URL = "https://raw.githubusercontent.com/jadchaar/sec-cik-mapper/main/mappings/stocks/ticker_to_cik.json"
COMPANYCONCEPT_BASE = "https://data.sec.gov/api/xbrl/companyconcept"
LOOKBACK_YEARS = 5
REFRESH_DAYS = 30
CALL_DELAY_SEC = 0.35
MAX_RETRIES = 3

# FIX v3: GANTI link ini ke link "Publish to web" (CSV) dari tab
# BB_SCREENER_ANALYSIS di Sheets kamu. Biarkan string kosong "" kalau
# belum di-setup - fetch_eps.py bakal fallback ke tickers.txt doang.
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQ3sH_eVmw9U4MiSNXitDOJtVZ8CPOdPTtqrlMGRgx3j1BVfTQW7YdYKYK9VY9Ni4IVRHyZcSQLnqzC/pub?gid=208123030&single=true&output=csv"  # contoh: "https://docs.google.com/spreadsheets/d/e/2PACX-.../pub?gid=0&single=true&output=csv"

# FIX v2: sanity cap buat Revenue CAGR - di atas ini dianggap implausible
# buat perusahaan market cap $10B+ (gate BB Screener), kemungkinan besar
# artefak gap data/tag switching, bukan growth beneran.
MAX_PLAUSIBLE_CAGR_PCT = 150
MIN_PLAUSIBLE_CAGR_PCT = -90
MIN_YEAR_SPAN = 2  # minimal rentang 2 tahun kalender biar CAGR ada artinya

OUTPUT_FILE = "eps_cache.json"
TICKERS_FILE = "tickers.txt"


def http_get_json(url, retries=MAX_RETRIES):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8")), 200
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"  HTTP {e.code}, retry in {wait}s...")
                time.sleep(wait)
                continue
            return None, e.code
        except Exception as e:
            print(f"  exception: {e}")
            return None, 0
    return None, 0


def load_tickers_from_txt():
    tickers = []
    try:
        with open(TICKERS_FILE, "r") as f:
            for line in f:
                line = line.strip().upper()
                if line and not line.startswith("#"):
                    tickers.append(line)
    except FileNotFoundError:
        pass
    return tickers


def load_tickers_from_sheet():
    """FIX v3: narik daftar Symbol dari link Publish-to-web (CSV) tab
    BB_SCREENER_ANALYSIS. Return [] kalau SHEET_CSV_URL kosong atau gagal
    fetch - gak bikin seluruh workflow gagal, cuma fallback ke tickers.txt."""
    if not SHEET_CSV_URL:
        return []
    req = urllib.request.Request(SHEET_CSV_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8")
    except Exception as e:
        print(f"  GAGAL fetch ticker dari Google Sheet: {e}")
        return []

    reader = csv.DictReader(io.StringIO(text))
    tickers = []
    for row in reader:
        sym = (row.get("Symbol") or "").strip().upper()
        if sym:
            tickers.append(sym)
    return tickers


def load_tickers():
    manual = load_tickers_from_txt()
    from_sheet = load_tickers_from_sheet()
    print(f"Ticker dari tickers.txt (manual watchlist tambahan): {len(manual)}")
    print(f"Ticker dari Google Sheet (auto, BB_SCREENER_ANALYSIS)  : {len(from_sheet)}")
    combined = sorted(set(manual) | set(from_sheet))
    return combined


def load_cik_map():
    print("Fetching CIK map from GitHub mirror...")
    data, code = http_get_json(CIK_MAP_URL)
    if data is None:
        print(f"  GAGAL fetch CIK map (HTTP {code})")
        return {}
    print(f"  OK, {len(data)} ticker termuat di CIK map")
    return {k.upper(): str(v) for k, v in data.items()}


def load_existing_cache():
    try:
        with open(OUTPUT_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def is_fresh(entry):
    if not entry or "lastFetched" not in entry:
        return False
    try:
        last = datetime.fromisoformat(entry["lastFetched"].replace("Z", "+00:00"))
    except ValueError:
        return False
    age_days = (datetime.now(timezone.utc) - last).days
    return age_days < REFRESH_DAYS


def fetch_concept_history(cik, tag):
    cik_padded = str(cik).zfill(10)
    url = f"{COMPANYCONCEPT_BASE}/CIK{cik_padded}/us-gaap/{tag}.json"
    data, code = http_get_json(url)
    if data is None:
        return None, f"http_{code}"

    units_obj = data.get("units", {})
    unit_key = next(iter(units_obj), None)
    if not unit_key:
        return None, "no_units"
    points = units_obj[unit_key]

    annual = [p for p in points if p.get("fp") == "FY" and p.get("form") in ("10-K", "10-K/A")]
    if not annual:
        return None, "no_annual_data"

    by_fy = {}
    for p in annual:
        fy = p["fy"]
        if fy not in by_fy or p["filed"] > by_fy[fy]["filed"]:
            by_fy[fy] = p

    sorted_points = sorted(by_fy.values(), key=lambda p: p["fy"], reverse=True)[:LOOKBACK_YEARS]
    return [{"fy": p["fy"], "val": p["val"]} for p in sorted_points], None


def calc_revenue_cagr_5y(rev_points):
    """
    FIX v2: years dihitung dari SELISIH fy (fiscal year) beneran antara
    titik tertua & terbaru - bukan dari jumlah titik data (len-1). Ini
    yang bikin CAGR immune terhadap gap data akibat tag switching SEC.
    Return (cagr_or_None, reason_if_none).
    """
    if not rev_points or len(rev_points) < 2:
        return None, "insufficient_revenue_points"

    sorted_asc = sorted(rev_points, key=lambda p: p["fy"])
    oldest, newest = sorted_asc[0], sorted_asc[-1]
    years = newest["fy"] - oldest["fy"]  # <- FIX: fy gap, bukan len-1

    if years < MIN_YEAR_SPAN:
        return None, f"year_span_too_small({years})"
    if oldest["val"] <= 0 or newest["val"] <= 0:
        return None, "non_positive_base_or_end"

    cagr = ((newest["val"] / oldest["val"]) ** (1 / years) - 1) * 100

    if cagr > MAX_PLAUSIBLE_CAGR_PCT or cagr < MIN_PLAUSIBLE_CAGR_PCT:
        return None, f"implausible_cagr({round(cagr, 1)}%,years={years})"

    return round(cagr, 4), None


def fetch_ticker_financials(symbol, cik):
    eps_points, reason = fetch_concept_history(cik, "EarningsPerShareDiluted")
    time.sleep(CALL_DELAY_SEC)
    if eps_points is None:
        eps_points, reason = fetch_concept_history(cik, "EarningsPerShareBasic")
        time.sleep(CALL_DELAY_SEC)
    if eps_points is None or len(eps_points) < 2:
        return None, reason or "insufficient_eps_data"

    eps_vals = [p["val"] for p in eps_points]
    avg_eps_5y = sum(eps_vals) / len(eps_vals)

    rev_points, _ = fetch_concept_history(cik, "Revenues")
    time.sleep(CALL_DELAY_SEC)
    revenue_cagr_5y, cagr_reason = calc_revenue_cagr_5y(rev_points)
    if revenue_cagr_5y is None and cagr_reason:
        print(f"  (info) revenueCAGR5Y skipped: {cagr_reason}")

    return {
        "avgEPS5Y": round(avg_eps_5y, 4),
        "revenueCAGR5Y": revenue_cagr_5y,
        "lastFetched": datetime.now(timezone.utc).isoformat(),
    }, None


def main():
    tickers = load_tickers()
    print(f"Total ticker di watchlist: {len(tickers)}")

    cik_map = load_cik_map()
    cache = load_existing_cache()

    fetched, skipped_fresh, failed = 0, 0, 0
    fail_reasons = []

    for symbol in tickers:
        existing = cache.get(symbol)
        if is_fresh(existing):
            skipped_fresh += 1
            continue

        cik = cik_map.get(symbol)
        if not cik:
            failed += 1
            fail_reasons.append(f"{symbol}:cik_not_found")
            print(f"{symbol}: CIK gak ketemu, skip")
            continue

        print(f"{symbol}: fetching...")
        result, reason = fetch_ticker_financials(symbol, cik)
        if result is None:
            failed += 1
            fail_reasons.append(f"{symbol}:{reason}")
            print(f"  GAGAL: {reason}")
            continue

        cache[symbol] = result
        fetched += 1
        print(f"  OK avgEPS5Y={result['avgEPS5Y']} revCAGR5Y={result['revenueCAGR5Y']}")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)

    print("\n=== SUMMARY ===")
    print(f"Fetched baru : {fetched}")
    print(f"Skip (fresh) : {skipped_fresh}")
    print(f"Gagal        : {failed}")
    if fail_reasons:
        print("Sample gagal:", ", ".join(fail_reasons[:10]))
    print(f"Total ticker di cache: {len(cache)}")


if __name__ == "__main__":
    main()
