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
"""

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


def load_tickers():
    tickers = []
    with open(TICKERS_FILE, "r") as f:
        for line in f:
            line = line.strip().upper()
            if line and not line.startswith("#"):
                tickers.append(line)
    return tickers


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

    revenue_cagr_5y = None
    rev_points, _ = fetch_concept_history(cik, "Revenues")
    time.sleep(CALL_DELAY_SEC)
    if rev_points and len(rev_points) >= 2:
        sorted_asc = sorted(rev_points, key=lambda p: p["fy"])
        oldest, newest = sorted_asc[0]["val"], sorted_asc[-1]["val"]
        years = len(sorted_asc) - 1
        if oldest > 0 and newest > 0 and years > 0:
            revenue_cagr_5y = ((newest / oldest) ** (1 / years) - 1) * 100

    return {
        "avgEPS5Y": round(avg_eps_5y, 4),
        "revenueCAGR5Y": round(revenue_cagr_5y, 4) if revenue_cagr_5y is not None else None,
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
