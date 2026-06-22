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
DEFAULT_START_YEAR = 2026
DEFAULT_TIMEOUT = 25
DEFAULT_CONCURRENCY = 100
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BACKOFF = 0.75
DEFAULT_MIN_MOVIE_DAY_GROSS = 100000
REBUILD_CURRENT_YEAR_BY_DEFAULT = True

PREFERRED_CHAINS = ["PVR", "INOX", "Cinepolis"]


# ----------------------------
# Time / text helpers
# ----------------------------
def today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def now_ist_str() -> str:
    return dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")


def safe_int(v: Any) -> int:
    return int(v) if isinstance(v, (int, float)) else 0


def safe_float(v: Any) -> float:
    return float(v) if isinstance(v, (int, float)) else 0.0


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_movie_name(name: str) -> str:
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


# ----------------------------
# File helpers
# ----------------------------
def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def clear_year_dir(year_dir: str) -> None:
    if not os.path.isdir(year_dir):
        return
    for fn in os.listdir(year_dir):
        if fn.endswith(".json") or fn.endswith(".json.tmp"):
            try:
                os.remove(os.path.join(year_dir, fn))
            except OSError:
                pass


def clear_summary_file(output_root: str, year: int) -> None:
    path = os.path.join(output_root, "year", f"{year}.json")
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ----------------------------
# URL routing
# ----------------------------
def is_more_than_one_month_old(date_str: str) -> bool:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
    return (today_ist() - d).days > 31


def get_urls(date_str: str):
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


# ----------------------------
# Rollup helpers
# ----------------------------
def empty_rollup() -> Dict[str, Any]:
    return {
        "g": 0,
        "s": 0,
        "sh": 0,
        "ts": 0,
        "ff": 0,
        "hf": 0,
        "_occ_weight": 0.0,
        "_occ_count": 0,
    }


def add_rollup(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
    g = safe_int(row.get("g"))
    s = safe_int(row.get("s"))
    sh = safe_int(row.get("sh"))
    ts = safe_int(row.get("ts"))
    ff = safe_int(row.get("ff"))
    hf = safe_int(row.get("hf"))
    o = safe_float(row.get("o"))

    bucket["g"] += g
    bucket["s"] += s
    bucket["sh"] += sh
    bucket["ts"] += ts
    bucket["ff"] += ff
    bucket["hf"] += hf

    if ts > 0:
        bucket["_occ_weight"] += o * ts
    else:
        bucket["_occ_weight"] += o
    bucket["_occ_count"] += 1


def finalize_rollup(bucket: Dict[str, Any]) -> Dict[str, Any]:
    ts = int(bucket["ts"])
    if ts > 0:
        o = round(bucket["_occ_weight"] / ts, 2)
    elif bucket["_occ_count"] > 0:
        o = round(bucket["_occ_weight"] / bucket["_occ_count"], 2)
    else:
        o = 0.0

    return {
        "g": int(bucket["g"]),
        "s": int(bucket["s"]),
        "sh": int(bucket["sh"]),
        "ts": int(bucket["ts"]),
        "ff": int(bucket["ff"]),
        "hf": int(bucket["hf"]),
        "o": o,
    }


def normalize_day_entry(day: Any) -> Dict[str, Any]:
    if isinstance(day, list):
        g = safe_int(day[0]) if len(day) > 0 else 0
        s = safe_int(day[1]) if len(day) > 1 else 0
        sh = safe_int(day[2]) if len(day) > 2 else 0
        o = safe_float(day[3]) if len(day) > 3 else 0.0
        return {
            "g": g,
            "s": s,
            "sh": sh,
            "ts": 0,
            "ff": 0,
            "hf": 0,
            "_occ_weight": o,
            "_occ_count": 1 if o else 0,
        }

    if not isinstance(day, dict):
        return empty_rollup()

    g = safe_int(day.get("g", day.get("gross")))
    s = safe_int(day.get("s", day.get("sold")))
    sh = safe_int(day.get("sh", day.get("shows")))
    ts = safe_int(day.get("ts", day.get("totalSeats")))
    ff = safe_int(day.get("ff", day.get("fastfilling")))
    hf = safe_int(day.get("hf", day.get("housefull")))
    o = safe_float(day.get("o", day.get("occupancy")))

    return {
        "g": g,
        "s": s,
        "sh": sh,
        "ts": ts,
        "ff": ff,
        "hf": hf,
        "_occ_weight": (o * ts) if ts else o,
        "_occ_count": 1 if o else 0,
    }


def normalize_totals_entry(src: Any) -> Dict[str, Any]:
    if not isinstance(src, dict):
        return empty_rollup()

    g = safe_int(src.get("g", src.get("gross")))
    s = safe_int(src.get("s", src.get("sold")))
    sh = safe_int(src.get("sh", src.get("shows")))
    ts = safe_int(src.get("ts", src.get("totalSeats")))
    ff = safe_int(src.get("ff", src.get("fastfilling")))
    hf = safe_int(src.get("hf", src.get("housefull")))
    o = safe_float(src.get("o", src.get("occupancy")))

    return {
        "g": g,
        "s": s,
        "sh": sh,
        "ts": ts,
        "ff": ff,
        "hf": hf,
        "_occ_weight": (o * ts) if ts else o,
        "_occ_count": 1 if o else 0,
    }


# ----------------------------
# State DB structure
# ----------------------------
def empty_movie_bucket() -> Dict[str, Any]:
    return {
        "d": {},
        "_t": empty_rollup(),
    }


def normalize_movie_entry(movie: Any) -> Dict[str, Any]:
    if not isinstance(movie, dict):
        return empty_movie_bucket()

    if "d" in movie and "_t" in movie:
        movie["d"] = {
            dk: normalize_day_entry(dv)
            for dk, dv in (movie.get("d") or {}).items()
        }
        movie["_t"] = normalize_totals_entry(movie.get("_t"))
        return movie

    dsrc = movie.get("daily") or movie.get("d") or {}
    daily: Dict[str, Any] = {}
    if isinstance(dsrc, dict):
        for dk, dv in dsrc.items():
            daily[dk] = normalize_day_entry(dv)

    totals_src = movie.get("totals") or movie.get("_t") or {}
    totals = normalize_totals_entry(totals_src)

    if daily:
        totals = empty_rollup()
        for dv in daily.values():
            add_rollup(totals, dv)

    return {
        "d": daily,
        "_t": totals,
    }


def empty_state_db(year: int, state_name: str, state_key: str) -> Dict[str, Any]:
    return {
        "y": year,
        "s": state_name,
        "k": state_key,
        "u": "",
        "_m": {
            "lpd": None,
        },
        "movies": {},
    }


def load_existing_state_dbs(output_root: str, year: int) -> Dict[str, Dict[str, Any]]:
    year_dir = os.path.join(output_root, str(year))
    if not os.path.isdir(year_dir):
        return {}

    out: Dict[str, Dict[str, Any]] = {}

    for fn in os.listdir(year_dir):
        if not fn.endswith(".json"):
            continue

        path = os.path.join(year_dir, fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                db = json.load(f)

            db.setdefault("_m", {"lpd": None})
            db.setdefault("u", "")
            db.setdefault("s", db.get("s", ""))
            db.setdefault("k", db.get("k", slugify_filename(db.get("s", fn[:-5]))))
            db.setdefault("movies", {})

            normalized_movies = {}
            for movie_name, movie in db["movies"].items():
                normalized_movies[movie_name] = normalize_movie_entry(movie)

            db["movies"] = normalized_movies
            out[db["k"]] = db

        except Exception:
            continue

    return out


def ensure_state_movie(state_db: Dict[str, Any], movie_name: str) -> Dict[str, Any]:
    movies = state_db["movies"]
    if movie_name not in movies:
        movies[movie_name] = empty_movie_bucket()
    else:
        movies[movie_name] = normalize_movie_entry(movies[movie_name])
    return movies[movie_name]


# ----------------------------
# Year summary DB structure
# ----------------------------
def empty_year_movie_bucket() -> Dict[str, Any]:
    return {
        "t": empty_rollup(),
        "_states": {},
    }


def normalize_year_movie_entry(movie: Any) -> Dict[str, Any]:
    if not isinstance(movie, dict):
        return empty_year_movie_bucket()

    if "t" in movie and "_states" in movie:
        movie["t"] = normalize_totals_entry(movie.get("t"))
        movie["_states"] = {
            state_name: normalize_totals_entry(stats)
            for state_name, stats in (movie.get("_states") or {}).items()
        }
        return movie

    # Final file reload support
    totals = normalize_totals_entry(movie.get("t") or movie.get("totals") or movie.get("_t"))
    states_src = movie.get("states") or {}
    states: Dict[str, Any] = {}
    if isinstance(states_src, dict):
        for state_name, stats in states_src.items():
            states[state_name] = normalize_totals_entry(stats)

    return {
        "t": totals,
        "_states": states,
    }


def empty_year_db(year: int) -> Dict[str, Any]:
    return {
        "y": year,
        "u": "",
        "_m": {
            "lpd": None,
        },
        "movies": {},
    }


def load_existing_year_db(output_root: str, year: int) -> Dict[str, Any]:
    path = os.path.join(output_root, "year", f"{year}.json")
    if not os.path.exists(path):
        return empty_year_db(year)

    try:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        return empty_year_db(year)

    db.setdefault("_m", {"lpd": None})
    db.setdefault("u", "")
    db.setdefault("movies", {})

    normalized_movies = {}
    for movie_name, movie in db["movies"].items():
        normalized_movies[movie_name] = normalize_year_movie_entry(movie)
    db["movies"] = normalized_movies

    return db


def ensure_year_movie(year_db: Dict[str, Any], movie_name: str) -> Dict[str, Any]:
    movies = year_db["movies"]
    if movie_name not in movies:
        movies[movie_name] = empty_year_movie_bucket()
    else:
        movies[movie_name] = normalize_year_movie_entry(movies[movie_name])
    return movies[movie_name]


# ----------------------------
# HTTP
# ----------------------------
def fetch_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json,text/plain,*/*",
        "User-Agent": "Mozilla/5.0",
    }


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    retries: int = DEFAULT_RETRIES,
) -> Optional[Dict[str, Any]]:
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


# ----------------------------
# Core processing
# ----------------------------
def build_base_gross_map(payload: Dict[str, Any]) -> Dict[str, int]:
    base_gross = defaultdict(int)
    for movie_name, data in (payload.get("movies") or {}).items():
        base_name = normalize_movie_name(movie_name)
        base_gross[base_name] += safe_int(data.get("gross"))
    return base_gross


def add_state_day(
    state_db: Dict[str, Any],
    movie_name: str,
    date_key: str,
    rows: List[Dict[str, Any]],
    payload_last_updated: str,
) -> None:
    movie = ensure_state_movie(state_db, movie_name)
    day = movie["d"].setdefault(date_key, empty_rollup())

    if payload_last_updated:
        state_db["u"] = payload_last_updated

    for row in rows:
        add_rollup(day, row)
        add_rollup(movie["_t"], row)


def add_year_state_day(
    year_db: Dict[str, Any],
    movie_name: str,
    rows_by_state: Dict[str, List[Dict[str, Any]]],
    payload_last_updated: str,
) -> None:
    movie = ensure_year_movie(year_db, movie_name)

    if payload_last_updated:
        year_db["u"] = payload_last_updated

    for state_name, rows in rows_by_state.items():
        state_bucket = movie["_states"].setdefault(state_name, empty_rollup())
        for row in rows:
            add_rollup(state_bucket, row)
            add_rollup(movie["t"], row)


def process_day_into_states_and_year(
    year: int,
    date_str: str,
    payload: Dict[str, Any],
    state_dbs: Dict[str, Dict[str, Any]],
    year_db: Dict[str, Any],
    min_movie_day_gross: int,
) -> None:
    if not payload or "movies" not in payload:
        return

    date_key = date_str.replace("-", "")
    payload_last_updated = payload.get("last_updated", "")

    base_gross = build_base_gross_map(payload)

    for movie_title, data in payload["movies"].items():
        base_name = normalize_movie_name(movie_title)

        if base_gross[base_name] < min_movie_day_gross:
            continue

        state_rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for row in (data.get("details") or []):
            state_name = normalize_state_name(row.get("state") or "")
            if not state_name:
                continue
            state_rows[state_name].append(row)

        if not state_rows:
            continue

        # State-wise files: daywise only, no city breakdown stored.
        for state_name, rows in state_rows.items():
            state_key = slugify_filename(state_name)
            if state_key not in state_dbs:
                state_dbs[state_key] = empty_state_db(year, state_name, state_key)

            add_state_day(
                state_db=state_dbs[state_key],
                movie_name=base_name,
                date_key=date_key,
                rows=rows,
                payload_last_updated=payload_last_updated,
            )

        # Year summary file: movie-wise state totals only, no daywise breakdown.
        add_year_state_day(
            year_db=year_db,
            movie_name=base_name,
            rows_by_state=state_rows,
            payload_last_updated=payload_last_updated,
        )


def finalize_state_db(state_db: Dict[str, Any]) -> None:
    final_movies: Dict[str, Any] = {}

    for movie_name, movie in state_db["movies"].items():
        movie = normalize_movie_entry(movie)

        daily = movie.get("d") or {}
        finalized_daily: Dict[str, Any] = {}
        total = normalize_totals_entry(movie.get("_t"))

        for date_key in sorted(daily.keys()):
            day = normalize_day_entry(daily[date_key])

            ts = int(day["ts"])
            if ts > 0:
                o = round(day["_occ_weight"] / ts, 2)
            elif day["_occ_count"] > 0:
                o = round(day["_occ_weight"] / day["_occ_count"], 2)
            else:
                o = 0.0

            finalized_daily[date_key] = {
                "g": int(day["g"]),
                "s": int(day["s"]),
                "sh": int(day["sh"]),
                "ts": int(day["ts"]),
                "ff": int(day["ff"]),
                "hf": int(day["hf"]),
                "o": o,
            }

        t_ts = int(total["ts"])
        if t_ts > 0:
            t_o = round(total["_occ_weight"] / t_ts, 2)
        elif total["_occ_count"] > 0:
            t_o = round(total["_occ_weight"] / total["_occ_count"], 2)
        else:
            t_o = 0.0

        final_movies[movie_name] = {
            "d": finalized_daily,
            "t": {
                "g": int(total["g"]),
                "s": int(total["s"]),
                "sh": int(total["sh"]),
                "ts": int(total["ts"]),
                "ff": int(total["ff"]),
                "hf": int(total["hf"]),
                "o": t_o,
            },
        }

    state_db["movies"] = dict(
        sorted(
            final_movies.items(),
            key=lambda kv: kv[1]["t"]["g"],
            reverse=True,
        )
    )

    if not state_db.get("u"):
        state_db["u"] = now_ist_str()


def finalize_year_db(year_db: Dict[str, Any]) -> None:
    final_movies: Dict[str, Any] = {}

    for movie_name, movie in year_db["movies"].items():
        movie = normalize_year_movie_entry(movie)

        total = normalize_totals_entry(movie.get("t"))
        states = movie.get("_states") or {}

        t_ts = int(total["ts"])
        if t_ts > 0:
            t_o = round(total["_occ_weight"] / t_ts, 2)
        elif total["_occ_count"] > 0:
            t_o = round(total["_occ_weight"] / total["_occ_count"], 2)
        else:
            t_o = 0.0

        final_states: Dict[str, Any] = {}
        for state_name, stats in states.items():
            s = normalize_totals_entry(stats)
            s_ts = int(s["ts"])
            if s_ts > 0:
                s_o = round(s["_occ_weight"] / s_ts, 2)
            elif s["_occ_count"] > 0:
                s_o = round(s["_occ_weight"] / s["_occ_count"], 2)
            else:
                s_o = 0.0

            final_states[state_name] = {
                "g": int(s["g"]),
                "s": int(s["s"]),
                "sh": int(s["sh"]),
                "ts": int(s["ts"]),
                "ff": int(s["ff"]),
                "hf": int(s["hf"]),
                "o": s_o,
            }

        final_movies[movie_name] = {
            "t": {
                "g": int(total["g"]),
                "s": int(total["s"]),
                "sh": int(total["sh"]),
                "ts": int(total["ts"]),
                "ff": int(total["ff"]),
                "hf": int(total["hf"]),
                "o": t_o,
            },
            "states": dict(
                sorted(
                    final_states.items(),
                    key=lambda kv: kv[1]["g"],
                    reverse=True,
                )
            ),
        }

    year_db["movies"] = dict(
        sorted(
            final_movies.items(),
            key=lambda kv: kv[1]["t"]["g"],
            reverse=True,
        )
    )

    if not year_db.get("u"):
        year_db["u"] = now_ist_str()


def save_state_db(output_root: str, year: int, state_db: Dict[str, Any]) -> str:
    year_dir = os.path.join(output_root, str(year))
    os.makedirs(year_dir, exist_ok=True)

    path = os.path.join(year_dir, f"{state_db['k']}.json")
    atomic_write_json(path, state_db)
    return path


def save_year_db(output_root: str, year: int, year_db: Dict[str, Any]) -> str:
    year_dir = os.path.join(output_root, "year")
    os.makedirs(year_dir, exist_ok=True)

    path = os.path.join(year_dir, f"{year}.json")
    atomic_write_json(path, year_db)
    return path


async def update_year(
    session: aiohttp.ClientSession,
    year: int,
    output_root: str,
    min_movie_day_gross: int,
    concurrency: int,
    rebuild_current_year: bool,
) -> None:
    state_year_dir = os.path.join(output_root, str(year))

    if year == today_ist().year and rebuild_current_year:
        clear_year_dir(state_year_dir)
        clear_summary_file(output_root, year)
        state_dbs: Dict[str, Dict[str, Any]] = {}
        year_db = empty_year_db(year)
        start = dt.date(year, 1, 1)
    else:
        state_dbs = load_existing_state_dbs(output_root, year)
        year_db = load_existing_year_db(output_root, year)
        start = get_year_start_for_update(year, state_dbs, rebuild_current_year=False)

    end = get_year_end_for_update(year)

    if start > end:
        print(f"{year}: already up to date")
        return

    dates: List[str] = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += dt.timedelta(days=1)

    print(f"{year}: fetching {len(dates)} days")

    sem = asyncio.Semaphore(concurrency)

    async def worker(ds: str):
        async with sem:
            return await fetch_day(session, ds)

    results = await asyncio.gather(
        *(worker(ds) for ds in dates),
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, Exception):
            continue

        date_str, payload = result
        if payload:
            process_day_into_states_and_year(
                year=year,
                date_str=date_str,
                payload=payload,
                state_dbs=state_dbs,
                year_db=year_db,
                min_movie_day_gross=min_movie_day_gross,
            )

    if dates:
        last_date = dates[-1]
        for db in state_dbs.values():
            db.setdefault("_m", {})
            db["_m"]["lpd"] = last_date
        year_db.setdefault("_m", {})
        year_db["_m"]["lpd"] = last_date

    saved = 0
    for _, state_db in state_dbs.items():
        if not state_db.get("movies"):
            continue
        finalize_state_db(state_db)
        save_state_db(output_root, year, state_db)
        saved += 1

    finalize_year_db(year_db)
    save_year_db(output_root, year, year_db)

    print(f"{year}: saved {saved} state files + 1 yearly summary")


# ----------------------------
# Main
# ----------------------------
def get_year_start_for_update(
    year: int,
    state_dbs: Dict[str, Dict[str, Any]],
    rebuild_current_year: bool,
) -> dt.date:
    if year == today_ist().year and rebuild_current_year:
        return dt.date(year, 1, 1)

    last_dates = []
    for db in state_dbs.values():
        lpd = db.get("_m", {}).get("lpd")
        if lpd:
            try:
                last_dates.append(dt.datetime.strptime(lpd, "%Y-%m-%d").date())
            except Exception:
                pass

    if last_dates:
        return max(last_dates) + dt.timedelta(days=1)

    return dt.date(year, 1, 1)


def get_year_end_for_update(year: int) -> dt.date:
    return today_ist() if year == today_ist().year else dt.date(year, 12, 31)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Build state-wise and yearly movie JSON files from daily summaries.")
    parser.add_argument("--start-year", type=int, default=DEFAULT_START_YEAR)
    parser.add_argument("--end-year", type=int, default=today_ist().year)
    parser.add_argument("--output-dir", type=str, default="statedata")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--min-movie-day-gross", type=int, default=DEFAULT_MIN_MOVIE_DAY_GROSS)
    parser.add_argument(
        "--rebuild-current-year",
        action="store_true",
        default=REBUILD_CURRENT_YEAR_BY_DEFAULT,
        help="Rebuild the current year from Jan 1 every run.",
    )
    parser.add_argument(
        "--no-rebuild-current-year",
        action="store_false",
        dest="rebuild_current_year",
    )

    args = parser.parse_args()

    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(
        limit=args.concurrency,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
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
                concurrency=args.concurrency,
                rebuild_current_year=args.rebuild_current_year,
            )

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
