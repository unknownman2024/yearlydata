import argparse
import asyncio
import datetime as dt
import json
import os
import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ----------------------------
# Tunables
# ----------------------------
DEFAULT_START_YEAR = 2023
DEFAULT_TIMEOUT = 25
DEFAULT_CONCURRENCY = 100
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.75
DEFAULT_MIN_MOVIE_DAY_GROSS = 100000

PREFERRED_CHAINS = ["PVR", "INOX", "Cinepolis"]  # kept for future extensions

# ----------------------------
# Helpers
# ----------------------------
def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def safe_int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def safe_float(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_movie_name(name: str) -> str:
    # Strip only a trailing bracketed suffix, e.g. "Zootopia 2 [3D | Hindi]" -> "Zootopia 2"
    return normalize_spaces(re.sub(r"\s*\[[^\]]*\]\s*$", "", name or ""))


def normalize_state_name(name: str) -> str:
    return normalize_spaces(name or "")


def slugify_filename(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "unknown"


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def build_date_list(year: int) -> List[str]:
    start = dt.date(year, 1, 1)
    end = today_ist() if year == today_ist().year else dt.date(year, 12, 31)
    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += dt.timedelta(days=1)
    return dates


def get_urls(date_str):
    # Preserved from your current logic.
    date_code = date_str.replace("-", "")
    year = int(date_str[:4])
    md = date_str[5:]

    if date_code <= "20251231":
        url = f"https://bfilmyapi2025.pages.dev/daily/data/{year}/{md}_finalsummary.json"
        fallback = url

    elif year >= 2026 and is_more_than_one_month_old(date_str):
        url = f"https://bfilmyapi{year}.pages.dev/daily/data/{year}/{md}_finalsummary.json"
        fallback = url

    else:
        url = f"https://bfilmyapi.pages.dev/daily/data/{date_code}/finalsummary.json"
        fallback = f"https://bfilmyapi{year}.pages.dev/daily/data/{year}/{md}_finalsummary.json"

    return url, fallback


def is_more_than_one_month_old(date_str: str) -> bool:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    return (today_ist() - d).days > 31


def empty_state_db(year: int, state_name: str, state_key: str) -> Dict[str, Any]:
    return {
        "year": year,
        "state": state_name,
        "state_key": state_key,
        "last_updated": "",
        "_meta": {
            "lastProcessedDate": None
        },
        "movies": {}
    }


def ensure_movie(state_db: Dict[str, Any], movie_name: str) -> Dict[str, Any]:
    movies = state_db["movies"]
    if movie_name not in movies:
        movies[movie_name] = {
            "variants": set(),
            "daily": {},
            "_cityAgg": {},
            "totals": {},
            "topCities": []
        }
    return movies[movie_name]


def init_rollup() -> Dict[str, Any]:
    return {
        "gross": 0,
        "sold": 0,
        "shows": 0,
        "totalSeats": 0,
        "fastfilling": 0,
        "housefull": 0,
        "occWeight": 0.0,
        "occSum": 0.0,
        "rows": 0
    }


def add_rollup(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
    gross = safe_int(row.get("gross"))
    sold = safe_int(row.get("sold"))
    shows = safe_int(row.get("shows"))
    total_seats = safe_int(row.get("totalSeats"))
    fastfilling = safe_int(row.get("fastfilling"))
    housefull = safe_int(row.get("housefull"))
    occ = safe_float(row.get("occupancy"))

    bucket["gross"] += gross
    bucket["sold"] += sold
    bucket["shows"] += shows
    bucket["totalSeats"] += total_seats
    bucket["fastfilling"] += fastfilling
    bucket["housefull"] += housefull
    bucket["occSum"] += occ
    bucket["occWeight"] += occ * total_seats if total_seats else occ
    bucket["rows"] += 1


def finalize_rollup(bucket: Dict[str, Any], state_name: Optional[str] = None, city: Optional[str] = None) -> Dict[str, Any]:
    total_seats = int(bucket["totalSeats"])
    if total_seats:
        occupancy = round(bucket["occWeight"] / total_seats, 2)
    elif bucket["rows"]:
        occupancy = round(bucket["occSum"] / bucket["rows"], 2)
    else:
        occupancy = 0.0

    result = {
        "gross": int(bucket["gross"]),
        "sold": int(bucket["sold"]),
        "shows": int(bucket["shows"]),
        "totalSeats": int(bucket["totalSeats"]),
        "fastfilling": int(bucket["fastfilling"]),
        "housefull": int(bucket["housefull"]),
        "occupancy": occupancy
    }
    if city is not None:
        result["city"] = city
    if state_name is not None:
        result["state"] = state_name
    return result


def sort_details(detail_map: Dict[str, Dict[str, Any]], state_name: str) -> List[Dict[str, Any]]:
    rows = []
    for city, bucket in detail_map.items():
        row = finalize_rollup(bucket, state_name=state_name, city=city)
        rows.append(row)
    rows.sort(key=lambda x: (x["gross"], x["sold"], x["shows"]), reverse=True)
    return rows


def compute_totals_from_daily(movie: Dict[str, Any]) -> None:
    gross = sold = shows = total_seats = fastfilling = housefull = 0
    occ_weight = 0.0

    for day in movie["daily"].values():
        gross += safe_int(day.get("gross"))
        sold += safe_int(day.get("sold"))
        shows += safe_int(day.get("shows"))
        total_seats += safe_int(day.get("totalSeats"))
        fastfilling += safe_int(day.get("fastfilling"))
        housefull += safe_int(day.get("housefull"))

        day_occ = safe_float(day.get("occupancy"))
        day_seats = safe_int(day.get("totalSeats"))
        occ_weight += day_occ * day_seats if day_seats else day_occ

    avg_occupancy = round(occ_weight / total_seats, 2) if total_seats else 0.0

    movie["totals"] = {
        "gross": int(gross),
        "sold": int(sold),
        "shows": int(shows),
        "totalSeats": int(total_seats),
        "fastfilling": int(fastfilling),
        "housefull": int(housefull),
        "occupancy": avg_occupancy
    }


def fetch_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0"
    }


async def fetch_json(session: aiohttp.ClientSession, url: str, retries: int = DEFAULT_RETRIES) -> Optional[Dict[str, Any]]:
    for attempt in range(retries):
        try:
            async with session.get(url, headers=fetch_headers()) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                text = await resp.text()
                return json.loads(text)
        except Exception:
            if attempt + 1 >= retries:
                return None
            await asyncio.sleep(DEFAULT_RETRY_BACKOFF * (2 ** attempt))
    return None


async def fetch_day(session: aiohttp.ClientSession, date_str: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    url, fallback = get_urls(date_str)

    payload = await fetch_json(session, url)
    if payload:
        return date_str, payload

    if fallback != url:
        payload = await fetch_json(session, fallback)

    return date_str, payload


def add_day_to_state(
    state_db: Dict[str, Any],
    date_key: str,
    source_title: str,
    base_name: str,
    state_name: str,
    rows: List[Dict[str, Any]],
    source_last_updated: str
) -> None:
    movie = ensure_movie(state_db, base_name)
    movie["variants"].add(source_title)

    daily = movie["daily"].setdefault(
        date_key,
        {
            "gross": 0,
            "sold": 0,
            "shows": 0,
            "totalSeats": 0,
            "fastfilling": 0,
            "housefull": 0,
            "occupancy": 0.0,
            "details": {}
        }
    )

    if source_last_updated:
        state_db["last_updated"] = source_last_updated

    for row in rows:
        gross = safe_int(row.get("gross"))
        sold = safe_int(row.get("sold"))
        shows = safe_int(row.get("shows"))
        total_seats = safe_int(row.get("totalSeats"))
        fastfilling = safe_int(row.get("fastfilling"))
        housefull = safe_int(row.get("housefull"))
        city = normalize_spaces(row.get("city") or "")

        daily["gross"] += gross
        daily["sold"] += sold
        daily["shows"] += shows
        daily["totalSeats"] += total_seats
        daily["fastfilling"] += fastfilling
        daily["housefull"] += housefull

        if city:
            city_bucket = daily["details"].setdefault(city, init_rollup())
            add_rollup(city_bucket, row)

            movie_city_bucket = movie["_cityAgg"].setdefault(city, init_rollup())
            add_rollup(movie_city_bucket, row)

    # Reliable state-day occupancy.
    if daily["totalSeats"]:
        daily["occupancy"] = round((daily["sold"] / daily["totalSeats"]) * 100, 2)
    elif rows:
        daily["occupancy"] = round(
            sum(safe_float(r.get("occupancy")) for r in rows) / len(rows),
            2
        )
    else:
        daily["occupancy"] = 0.0


def finalize_state_db(state_db: Dict[str, Any]) -> None:
    final_movies: Dict[str, Any] = {}

    for movie_name, movie in state_db["movies"].items():
        compute_totals_from_daily(movie)

        variants = sorted(movie["variants"])
        movie["variants"] = variants

        # Finalize daily city breakdowns
        daily_final = {}
        for date_key in sorted(movie["daily"].keys()):
            day = movie["daily"][date_key]
            details = sort_details(day.pop("details", {}), state_db["state"])
            day["details"] = details
            daily_final[date_key] = day
        movie["daily"] = daily_final

        # Movie-level top cities inside this state
        top_cities = []
        for city, bucket in movie["_cityAgg"].items():
            row = finalize_rollup(bucket, state_name=state_db["state"], city=city)
            top_cities.append([city, row["gross"], row["sold"], row["shows"], row["occupancy"]])
        top_cities.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
        movie["topCities"] = top_cities[:10]

        movie.pop("_cityAgg", None)
        final_movies[movie_name] = movie

    state_db["movies"] = dict(
        sorted(
            final_movies.items(),
            key=lambda kv: kv[1]["totals"].get("gross", 0),
            reverse=True
        )
    )


def save_state_db(output_root: str, year: int, state_db: Dict[str, Any]) -> str:
    year_dir = os.path.join(output_root, str(year))
    os.makedirs(year_dir, exist_ok=True)

    path = os.path.join(year_dir, f"{state_db['state_key']}.json")
    atomic_write_json(path, state_db)
    return path


def build_base_gross_map(payload: Dict[str, Any]) -> Dict[str, int]:
    base_gross = defaultdict(int)
    for movie_name, data in (payload.get("movies") or {}).items():
        base_name = normalize_movie_name(movie_name)
        base_gross[base_name] += safe_int(data.get("gross"))
    return base_gross


def process_day_into_states(
    year: int,
    date_str: str,
    payload: Dict[str, Any],
    state_dbs: Dict[str, Dict[str, Any]],
    min_movie_day_gross: int
) -> None:
    if not payload or "movies" not in payload:
        return

    date_key = date_str.replace("-", "")
    source_last_updated = payload.get("last_updated", "")

    base_gross = build_base_gross_map(payload)

    for movie_name, data in payload["movies"].items():
        base_name = normalize_movie_name(movie_name)

        # Keep your existing noise filter: only process meaningful movie-days.
        if base_gross[base_name] < min_movie_day_gross:
            continue

        by_state: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for row in (data.get("details") or []):
            state_name = normalize_state_name(row.get("state") or "")
            if not state_name:
                continue
            by_state[state_name].append(row)

        if not by_state:
            continue

        for state_name, rows in by_state.items():
            state_key = slugify_filename(state_name)
            if state_key not in state_dbs:
                state_dbs[state_key] = empty_state_db(year, state_name, state_key)

            add_day_to_state(
                state_db=state_dbs[state_key],
                date_key=date_key,
                source_title=movie_name,
                base_name=base_name,
                state_name=state_name,
                rows=rows,
                source_last_updated=source_last_updated
            )


async def update_year(
    session: aiohttp.ClientSession,
    year: int,
    output_root: str,
    min_movie_day_gross: int,
    concurrency: int
) -> None:
    dates = build_date_list(year)
    if not dates:
        return

    print(f"{year}: fetching {len(dates)} days")

    sem = asyncio.Semaphore(concurrency)
    state_dbs: Dict[str, Dict[str, Any]] = {}

    async def worker(ds: str):
        async with sem:
            return await fetch_day(session, ds)

    results = await asyncio.gather(
        *(worker(ds) for ds in dates),
        return_exceptions=True
    )

    for result in results:
        if isinstance(result, Exception):
            continue
        date_str, payload = result
        if payload:
            process_day_into_states(
                year=year,
                date_str=date_str,
                payload=payload,
                state_dbs=state_dbs,
                min_movie_day_gross=min_movie_day_gross
            )

    # Finalize + save all state files for this year
    saved = 0
    for state_key, state_db in state_dbs.items():
        finalize_state_db(state_db)
        save_state_db(output_root, year, state_db)
        saved += 1

    print(f"{year}: saved {saved} state files")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Build state-wise movie JSON files from daily summaries.")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=today_ist().year)
    parser.add_argument("--output-dir", type=str, default="statedata")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--min-movie-day-gross", type=int, default=DEFAULT_MIN_MOVIE_DAY_GROSS)
    args = parser.parse_args()

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(
        limit=args.concurrency,
        ttl_dns_cache=300,
        enable_cleanup_closed=True
    )

    years = list(range(args.start_year, args.end_year + 1))
    if not years:
        print("No years to process.")
        return

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        for year in years:
            await update_year(
                session=session,
                year=year,
                output_root=args.output_dir,
                min_movie_day_gross=args.min_movie_day_gross,
                concurrency=args.concurrency
            )

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
