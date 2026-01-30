import requests
import json
import datetime
import time
import os
import pytz


# ------------ CONFIG ------------

TIMEOUT = 25
SLEEP = 0.25

IST = pytz.timezone("Asia/Kolkata")


# ------------ TIME ------------

def today_ist():
    return datetime.datetime.now(IST).date()


def is_more_than_one_month_old(date_str):

    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    now = datetime.datetime.utcnow()

    return (now - d).days > 31


# ------------ FETCH ------------

def fetch_day(date_str):

    date_code = date_str.replace("-", "")
    year = int(date_str[:4])
    md = date_str[5:]

    url = ""
    fallback = ""

    if date_code <= "20251231":

        url = f"https://bfilmyapi2025.pages.dev/daily/data/{year}/{md}_finalsummary.json"
        fallback = url

    elif year >= 2026 and is_more_than_one_month_old(date_str):

        url = f"https://bfilmyapi{year}.pages.dev/daily/data/{year}/{md}_finalsummary.json"
        fallback = url

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

    except:
        return None


# ------------ YEAR SELECTION ------------

def years_to_update():

    today = today_ist()

    y = today.year

    # Jan 1–2 → prev + current
    if today.month == 1 and today.day <= 2:
        return [y-1, y]

    return [y]


# ------------ LOAD / SAVE ------------

def load_year(year):

    fname = f"{year}.json"

    if os.path.exists(fname):

        with open(fname, "r", encoding="utf-8") as f:
            return json.load(f)

    return {
        "year": year,
        "last_updated": "",
        "movies": {}
    }


def save_year(db, year):

    with open(f"{year}.json", "w", encoding="utf-8") as f:

        json.dump(
            db,
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )


# ------------ CORE ------------

def ensure_movie(db, name):

    if name not in db["movies"]:

        db["movies"][name] = {
            "daily": {},
            "totals": {},
            "cityMap": {},
            "stateMap": {},
            "chainMap": {}
        }

    return db["movies"][name]


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

    m["gross"] += data["gross"]
    m["sold"] += data["sold"]
    m["shows"] += data["shows"]
    m["occSum"] += data["occupancy"]
    m["days"] += 1


# ------------ UPDATE DAY ------------

def update_day(db, date_str):

    day = fetch_day(date_str)

    if not day:
        return False


    if "last_updated" in day:
        db["last_updated"] = day["last_updated"]


    for name, data in day["movies"].items():

        M = ensure_movie(db, name)

        key = date_str.replace("-", "")

        # overwrite daily
        M["daily"][key] = [
            data["gross"],
            data["sold"],
            data["shows"],
            data["occupancy"]
        ]


        # city/state/chain
        for x in data["details"]:
            add_stat(M["cityMap"], x["city"], x)
            add_stat(M["stateMap"], x["state"], x)

        for x in data["Chain_details"]:
            add_stat(M["chainMap"], x["chain"], x)


    return True


# ------------ REBUILD TOTALS ------------

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


# ------------ BUILD TOP ------------

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

        del m["cityMap"]
        del m["stateMap"]
        del m["chainMap"]


# ------------ MAIN ------------

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


            # if already exists & not today → skip
            for m in db["movies"].values():

                if key in m["daily"] and d != today:
                    need = False
                    break


            if need:

                print("Fetch", ds)

                update_day(db, ds)

                time.sleep(SLEEP)

            d += datetime.timedelta(days=1)


        # rebuild
        rebuild_totals(db)
        finalize(db)

        save_year(db, year)

        print("Saved", year)



if __name__ == "__main__":
    main()
