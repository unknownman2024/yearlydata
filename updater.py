import asyncio
import aiohttp
import json
import datetime
import os
import pytz
import re

PREFERRED_CHAINS = [
    "PVR",
    "INOX",
    "Cinepolis"
]
OUTPUT_DIR = "moviedata"
os.makedirs(
    OUTPUT_DIR,
    exist_ok=True
)
MIN_MOVIE_DAY_GROSS = 500000

TIMEOUT = 25
CONCURRENCY = 100

IST = pytz.timezone("Asia/Kolkata")

NORTH = {
    "Maharashtra","NCR","Delhi","Gujarat","Uttar Pradesh","West Bengal",
    "Rajasthan","Punjab","Madhya Pradesh","Chhattisgarh","Odisha","Haryana",
    "Bihar","Uttarakhand","Goa","Assam","Jharkhand","Jammu and Kashmir",
    "Andaman And Nicobar Islands","Meghalaya","Himachal Pradesh","Chandigarh",
    "Tripura","Arunachal Pradesh","Sikkim","Manipur","Mizoram","Nagaland"
}

SOUTH = {
    "Tamil Nadu","Karnataka","Kerala",
    "Telangana","Andhra Pradesh","Puducherry"
}

def normalize_movie_name(name):
    return re.sub(r"\s*\[.*?\]\s*$", "", name).strip()


def save_database(years):

    movie_dates = {}

    for year in years:

        fn = os.path.join(
            OUTPUT_DIR,
            f"{year}.json"
        )

        if not os.path.exists(fn):
            continue

        with open(fn, "r", encoding="utf8") as f:
            db = json.load(f)

        for movie_name, movie in db.get("movies", {}).items():

            base_name = normalize_movie_name(movie_name)

            dates = movie_dates.setdefault(base_name, [])

            for d in movie.get("daily", {}):

                dates.append(
                    datetime.datetime.strptime(
                        d,
                        "%Y%m%d"
                    ).date()
                )

    movies = []

    for name, dates in movie_dates.items():

        if not dates:
            continue

        dates = sorted(set(dates))

        runs = []

        start = dates[0]
        prev = dates[0]

        for d in dates[1:]:

            if (d - prev).days > 5:
                runs.append((start, prev))
                start = d

            prev = d

        runs.append((start, prev))

        best_start = None
        best_end = None
        best_length = -1

        for start, end in runs:

            length = (end - start).days + 1

            if length > best_length:
                best_length = length
                best_start = start
                best_end = end


        movies.append([
            name,
            int(best_start.strftime("%Y%m%d")),
            int(best_end.strftime("%Y%m%d"))
        ])

    movies.sort(
        key=lambda x: x[2],
        reverse=True
    )

    data = {
        "u": int(datetime.datetime.now(IST).strftime("%Y%m%d")),
        "m": movies
    }

    with open(
        os.path.join(
            OUTPUT_DIR,
            "database.json.tmp"
        ),
        "w",
        encoding="utf8"
    ) as f:

        json.dump(
            data,
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )

        f.flush()
        os.fsync(f.fileno())

    os.replace(
        os.path.join(
            OUTPUT_DIR,
            "database.json.tmp"
        ),
        os.path.join(
            OUTPUT_DIR,
            "database.json"
        )
    )

    print(
        f"database.json saved ({len(movies)} movies)"
    )

def today_ist():
    return datetime.datetime.now(IST).date()


def safe_num(v):
    return v if isinstance(v, (int, float)) else 0

def is_more_than_one_month_old(date_str):

    d = datetime.datetime.strptime(
        date_str,
        "%Y-%m-%d"
    ).date()

    return (
        datetime.datetime.now(IST).date() - d
    ).days > 31

def get_urls(date_str):
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


async def fetch_json(session, url):
    try:
        async with session.get(url) as r:
            if r.status == 200:
                return await r.json()
    except:
        pass
    return None


async def fetch_day(session, date_str):
    url, fallback = get_urls(date_str)

    data = await fetch_json(session, url)

    if data:
        return date_str, data

    if fallback != url:
        data = await fetch_json(session, fallback)

    return date_str, data


def empty_db(year):
    return {
        "year": year,
        "last_updated": "",
        "_meta": {"lastProcessedDate": None},
        "movieSummary": {},
        "movies": {}
    }


def load_year(year):
    fn = os.path.join(
        OUTPUT_DIR,
        f"{year}.json"
    )

    if not os.path.exists(fn):
        return empty_db(year)

    with open(fn, "r", encoding="utf8") as f:
        db = json.load(f)

    db.setdefault("_meta", {"lastProcessedDate": None})
    db.setdefault("movieSummary", {})

    for m in db.get("movies", {}).values():
        m.setdefault("daily", {})
        m.setdefault("totals", {})
        m.setdefault("cities", {})
        m.setdefault("states", {})
        m.setdefault("chains", {})

    return db


def atomic_save(year, db):
    final_file = os.path.join(
        OUTPUT_DIR,
        f"{year}.json"
    )


    tmp_file = os.path.join(
        OUTPUT_DIR,
        f"{year}.json.tmp"
    )

    with open(tmp_file, "w", encoding="utf8") as f:
        json.dump(
            db,
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )

        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_file, final_file)


def ensure_movie(db, name):

    movies = db["movies"]

    if name not in movies:
        movies[name] = {
            "daily": {},
            "totals": {},
            "cities": {},
            "states": {},
            "chains": {},
            "topCities": [],
            "topStates": [],
            "topChains": []
        }

    return movies[name]


def add_stat(container, key, row):
    if not key:
        return

    x = container.setdefault(key, {
        "gross": 0,
        "sold": 0,
        "shows": 0,
        "occSum": 0.0,
        "days": 0
    })

    x["gross"] += int(safe_num(row.get("gross")))
    x["sold"] += int(safe_num(row.get("sold")))
    x["shows"] += int(safe_num(row.get("shows")))

    x["occSum"] = round(
        x["occSum"] + safe_num(row.get("occupancy")),
        2
    )

    x["days"] += 1


def build_top(container, limit=5):

    arr = []

    for k, v in container.items():

        avg = round(
            v["occSum"] / v["days"],
            2
        ) if v["days"] else 0

        arr.append([
            k,
            int(v["gross"]),
            int(v["sold"]),
            int(v["shows"]),
            avg
        ])

    arr.sort(
        key=lambda x: x[1],
        reverse=True
    )

    return arr[:limit]


def build_top_states(state_map):

    ranked = sorted(
        state_map.items(),
        key=lambda x: x[1]["gross"],
        reverse=True
    )

    top5 = ranked[:5]

    rs = {
        "gross": 0,
        "sold": 0,
        "shows": 0,
        "occSum": 0,
        "days": 0
    }

    rn = {
        "gross": 0,
        "sold": 0,
        "shows": 0,
        "occSum": 0,
        "days": 0
    }

    result = []

    for state, stats in top5:

        avg = round(
            stats["occSum"] / stats["days"],
            2
        ) if stats["days"] else 0

        result.append([
            state,
            int(stats["gross"]),
            int(stats["sold"]),
            int(stats["shows"]),
            avg
        ])

    for state, stats in ranked[5:]:

        target = rs if state in SOUTH else rn

        target["gross"] += stats["gross"]
        target["sold"] += stats["sold"]
        target["shows"] += stats["shows"]
        target["occSum"] += stats["occSum"]
        target["days"] += stats["days"]

    if rs["gross"]:

        result.append([
            "RS",
            int(rs["gross"]),
            int(rs["sold"]),
            int(rs["shows"]),
            round(rs["occSum"] / rs["days"], 2)
            if rs["days"] else 0
        ])

    if rn["gross"]:

        result.append([
            "RN",
            int(rn["gross"]),
            int(rn["sold"]),
            int(rn["shows"]),
            round(rn["occSum"] / rn["days"], 2)
            if rn["days"] else 0
        ])

    return result

def rebuild_totals(movie):
    gross = sold = shows = 0
    occ = days = 0

    for d in movie["daily"].values():
        gross += d[0]
        sold += d[1]
        shows += d[2]
        occ += d[3]
        days += 1

    movie["totals"] = {
        "gross": int(gross),
        "sold": int(sold),
        "shows": int(shows),
        "avgOcc": round(occ / days, 2) if days else 0
    }

def build_top_chains(
    chain_map,
    limit=5
):

    result = []

    used = set()

    for chain in PREFERRED_CHAINS:

        if chain not in chain_map:
            continue

        stats = chain_map[chain]

        avg = round(
            stats["occSum"] / stats["days"],
            2
        ) if stats["days"] else 0

        result.append([
            chain,
            int(stats["gross"]),
            int(stats["sold"]),
            int(stats["shows"]),
            avg
        ])

        used.add(chain)

    remaining = []

    for chain, stats in chain_map.items():

        if chain in used:
            continue

        avg = round(
            stats["occSum"] / stats["days"],
            2
        ) if stats["days"] else 0

        remaining.append([
            chain,
            int(stats["gross"]),
            int(stats["sold"]),
            int(stats["shows"]),
            avg
        ])

    remaining.sort(
        key=lambda x: x[1],
        reverse=True
    )

    
    result.extend(
        remaining[
            :max(
                0,
                limit - len(result)
            )
        ]
    )

    return result

def process_day(db, date_str, payload):
    if not payload or "movies" not in payload:
        return

    db["last_updated"] = payload.get("last_updated", "")

    date_key = date_str.replace("-", "")

    base_gross = {}

    for movie_name, data in payload["movies"].items():

        base_name = normalize_movie_name(movie_name)

        base_gross[base_name] = (
            base_gross.get(base_name, 0)
            + safe_num(data.get("gross"))
        )

    for movie_name, data in payload["movies"].items():

        base_name = normalize_movie_name(movie_name)

        if base_gross[base_name] < MIN_MOVIE_DAY_GROSS:
            continue

        movie = ensure_movie(db, movie_name)

        movie["daily"][date_key] = [
            int(safe_num(data.get("gross"))),
            int(safe_num(data.get("sold"))),
            int(safe_num(data.get("shows"))),
            round(safe_num(data.get("occupancy")), 2)
        ]

        for row in (data.get("details") or []):
            city = row.get("city")
            state = row.get("state")

            if city:
                add_stat(movie["cities"], city, row)

            if state:
                add_stat(movie["states"], state, row)

        for row in (data.get("Chain_details") or []):
            chain = row.get("chain")

            if chain:
                add_stat(movie["chains"], chain, row)


def finalize(db):

    movie_summary = {}

    for movie_name, movie in db["movies"].items():

        rebuild_totals(movie)

        movie["topCities"] = build_top(
            movie["cities"],
            5
        )

        movie["topStates"] = build_top_states(
            movie["states"]
        )

        movie["topChains"] = build_top_chains(
            movie["chains"]
        )

        base_name = normalize_movie_name(
            movie_name
        )

        summary = movie_summary.setdefault(
            base_name,
            {
                "cities": {},
                "states": {},
                "chains": {}
            }
        )

        for k,v in movie["cities"].items():

            x = summary["cities"].setdefault(
                k,
                {
                    "gross":0,
                    "sold":0,
                    "shows":0,
                    "occSum":0,
                    "days":0
                }
            )

            x["gross"] += v["gross"]
            x["sold"] += v["sold"]
            x["shows"] += v["shows"]
            x["occSum"] += v["occSum"]
            x["days"] += v["days"]

        for k,v in movie["states"].items():

            x = summary["states"].setdefault(
                k,
                {
                    "gross":0,
                    "sold":0,
                    "shows":0,
                    "occSum":0,
                    "days":0
                }
            )

            x["gross"] += v["gross"]
            x["sold"] += v["sold"]
            x["shows"] += v["shows"]
            x["occSum"] += v["occSum"]
            x["days"] += v["days"]

        for k,v in movie["chains"].items():

            x = summary["chains"].setdefault(
                k,
                {
                    "gross":0,
                    "sold":0,
                    "shows":0,
                    "occSum":0,
                    "days":0
                }
            )

            x["gross"] += v["gross"]
            x["sold"] += v["sold"]
            x["shows"] += v["shows"]
            x["occSum"] += v["occSum"]
            x["days"] += v["days"]

        movie.pop("cities", None)
        movie.pop("states", None)
        movie.pop("chains", None)

    db["movieSummary"] = {}

    for movie_name, summary in movie_summary.items():

        db["movieSummary"][movie_name] = {

            "topCities":
                build_top(
                    summary["cities"],
                    10
                ),

            "topStates":
                build_top_states(
                    summary["states"]
                ),

            "topChains":
                build_top_chains(
                    summary["chains"],
                    10
                )

        }

async def update_year(session, year):
    db = load_year(year)

    meta = db["_meta"]
    today = today_ist()

    # Rebuild current year from scratch every run
    if year == today.year:

        db["movies"] = {}
        db["movieSummary"] = {}
        meta["lastProcessedDate"] = None

    # Current year: rebuild from Jan 1 every run
    if year == today.year:

        db["movies"] = {}
        db["movieSummary"] = {}
        meta["lastProcessedDate"] = None

        start = datetime.date(
            year,
            1,
            1
        )

    else:

        if meta["lastProcessedDate"]:
            start = (
                datetime.datetime.strptime(
                    meta["lastProcessedDate"],
                    "%Y-%m-%d"
                ).date()
                + datetime.timedelta(days=1)
            )
        else:
            start = datetime.date(
                year,
                1,
                1
            )

    end = (
        today
        if year == today.year
        else datetime.date(year, 12, 31)
    )

    dates = []

    d = start

    while d <= end:
        dates.append(
            d.strftime("%Y-%m-%d")
        )
        d += datetime.timedelta(days=1)

    if not dates:
        print(f"{year}: already up to date")
        return

    print(
        f"{year}: fetching {len(dates)} days"
    )

    sem = asyncio.Semaphore(
        CONCURRENCY
    )

    async def worker(ds):
        async with sem:
            return await fetch_day(
                session,
                ds
            )

    results = await asyncio.gather(
        *(worker(ds) for ds in dates),
        return_exceptions=True
    )

    for result in results:

        if isinstance(result, Exception):
            continue

        date_str, payload = result

        if payload:
            process_day(
                db,
                date_str,
                payload
            )

    meta["lastProcessedDate"] = (
        end.strftime("%Y-%m-%d")
    )

    finalize(db)

    atomic_save(
        year,
        db
    )

    print(f"{year}: saved")

async def main():

    timeout = aiohttp.ClientTimeout(total=TIMEOUT)

    connector = aiohttp.TCPConnector(
        limit=CONCURRENCY,
        ttl_dns_cache=300,
        enable_cleanup_closed=True
    )

    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector
    ) as session:

        current_year = today_ist().year

        years = list(range(2023, current_year + 1))

        await asyncio.gather(
            *(update_year(session, year) for year in years)
        )
        save_database(years)


if __name__ == "__main__":
    asyncio.run(main())
