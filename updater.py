import requests
import json
import datetime
import time
import os
import pytz


# ---------------- CONFIG ----------------

TIMEOUT = 25
SLEEP = 0.3

IST = pytz.timezone("Asia/Kolkata")


# ---------------- TIME ----------------

def today_ist():
    return datetime.datetime.now(IST).date()


def is_more_than_one_month_old(date_str):

    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    now = datetime.datetime.utcnow()

    return (now - d).days > 31


# ---------------- FETCH ----------------

def fetch_day(date_str):

    date_code = date_str.replace("-", "")
    year = int(date_str[:4])
    md = date_str[5:]

    url = ""
    fallback = ""

    # 2025 & below
    if date_code <= "20251231":

        url = f"https://bfilmyapi2025.pages.dev/daily/data/{year}/{md}_finalsummary.json"
        fallback = url

    # 2026+ archive
    elif year >= 2026 and is_more_than_one_month_old(date_str):

        url = f"https://bfilmyapi{year}.pages.dev/daily/data/{year}/{md}_finalsummary.json"
        fallback = url

    # latest
    else:

        url = f"https://bfilmyapi.pages.dev/daily/data/{date_code}/finalsummary.json"

        fallback = f"https://bfilmyapi{year}.pages.dev/daily/data/{year}/{md}_finalsummary.json"


    params = {"_": int(time.time() * 1000)}

    try:

        r = requests.get(url, params=params, timeout=TIMEOUT)

        if r.status_code != 200 and fallback:

            r = requests.get(fallback, params=params, timeout=TIMEOUT)

        if r.status_code != 200:
            return None

        return r.json()

    except Exception as e:

        print("Fetch error:", date_str, e)
        return None


# ---------------- YEAR LOGIC ----------------

def years_to_update():

    today = today_ist()
    y = today.year

    # Jan 1–2 grace
    if today.month == 1 and today.day <= 2:
        return [y - 1, y]

    return [y]


# ---------------- LOAD / SAVE ----------------

def empty_year(year):

    return {
        "year": year,
        "last_updated": "",
        "movies": {}
    }


def load_year(year):

    fname = f"{year}.json"

    if os.path.exists(fname):

        try:
            with open(fname, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            print("Corrupt file, rebuilding:", fname)
            return empty_year(year)

    return empty_year(year)


def save_year(db, year):

    with open(f"{year}.json", "w", encoding="utf-8") as f:

        json.dump(
            db,
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )


# ---------------- MOVIE INIT / REPAIR ----------------

def ensure_movie(db, name):

    if name not in db["movies"]:

        db["movies"][name] = {
            "daily": {},
            "totals": {},
            "cityMap": {},
            "stateMap": {},
            "chainMap": {}
        }

    else:

        m = db["movies"][name]

        # auto repair
        if "daily" not in m:
            m["daily"] = {}

        if "totals" not in m:
            m["totals"] = {}

        if "cityMap" not in m:
            m["cityMap"] = {}

        if "stateMap" not in m:
            m["stateMap"] = {}

        if "chainMap" not in m:
            m["chainMap"] = {}

    return db["movies"][name]


# ---------------- AGG HELPERS ----------------

def add_stat(map_obj, key, data):

    if key not in map_obj:

        map_obj[key] = {
            "gross": 0,
            "sold": 0,
            "shows": 0,
            "occSum": 0,
            "days": 0
        }

    m = map_obj[key]

    m["gross"] += data.get("gross", 0)
    m["sold"] += data.get("sold", 0)
    m["shows"] += data.get("shows", 0)
    m["occSum"] += data.get("occupancy", 0)
    m["days"] += 1


# ---------------- UPDATE DAY ----------------

def update_day(db, date_str):

    day = fetch_day(date_str)

    if not day or "movies" not in day:
        return False


    if "last_updated" in day:
        db["last_updated"] = day["last_updated"]


    for name, data in day["movies"].items():

        M = ensure_movie(db, name)

        key = date_str.replace("-", "")

        # overwrite daily (hourly refresh)
        M["daily"][key] = [
            data.get("gross", 0),
            data.get("sold", 0),
            data.get("shows", 0),
            data.get("occupancy", 0)
        ]


        # cities / states
        for x in data.get("details", []):

            add_stat(M["cityMap"], x.get("city","NA"), x)
            add_stat(M["stateMap"], x.get("state","NA"), x)


        # chains
        for x in data.get("Chain_details", []):

            add_stat(M["chainMap"], x.get("chain","NA"), x)


    return True


# ---------------- REBUILD TOTALS ----------------

def rebuild_totals(db):

    for m in db["movies"].values():

        gross = sold = shows = occ = days = 0

        for d in m["daily"].values():

            gross += d[0]
            sold += d[1]
            shows += d[2]
            occ += d[3]
            days += 1


        avg = round(occ / days, 2) if days else 0

        m["totals"] = {
            "gross": gross,
            "sold": sold,
            "shows": shows,
            "avgOcc": avg
        }


# ---------------- BUILD TOP ----------------

def build_top(map_obj):

    arr = []

    for k, v in map_obj.items():

        avg = round(v["occSum"] / v["days"], 2) if v["days"] else 0

        arr.append([
            k,
            v["gross"],
            v["sold"],
            v["shows"],
            avg
        ])

    arr.sort(key=lambda x: x[1], reverse=True)

    return arr[:5]


def finalize(db):

    for m in db["movies"].values():

        m["topCities"] = build_top(m["cityMap"])
        m["topStates"] = build_top(m["stateMap"])
        m["topChains"] = build_top(m["chainMap"])

        # cleanup
        del m["cityMap"]
        del m["stateMap"]
        del m["chainMap"]


# ---------------- MAIN ----------------

def main():

    years = years_to_update()

    print("Updating:", years)

    today = today_ist()


    for year in years:

        db = load_year(year)

        start = datetime.date(year, 1, 1)

        if year == today.year:
            end = today
        else:
            end = datetime.date(year, 12, 31)


        d = start

        while d <= end:

            ds = d.strftime("%Y-%m-%d")

            key = ds.replace("-", "")

            need = True


            # skip old existing days
            for m in db["movies"].values():

                if key in m["daily"] and d != today:
                    need = False
                    break


            if need:

                print("Fetch", ds)

                update_day(db, ds)

                time.sleep(SLEEP)


            d += datetime.timedelta(days=1)


        # rebuild & finalize
        rebuild_totals(db)
        finalize(db)

        save_year(db, year)

        print("Saved", year)



if __name__ == "__main__":
    main()
