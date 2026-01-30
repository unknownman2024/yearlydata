import requests
import json
import datetime
import time

# ------------ CONFIG ------------

START_YEAR = 2023
TIMEOUT = 25
SLEEP = 0.3


# ------------ FETCH HELPERS ------------

def is_more_than_one_month_old(date_str):

    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    now = datetime.datetime.utcnow()

    return (now - d).days > 31


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


# ------------ CORE ------------

def build_year(year):

    print(f"\nBuilding {year}")

    db = {
        "year": year,
        "last_updated": "",
        "movies": {}
    }


    def ensure_movie(name):

        if name not in db["movies"]:

            db["movies"][name] = {

                "daily": {},

                "totals": {
                    "gross": 0,
                    "sold": 0,
                    "shows": 0,
                    "occSum": 0,
                    "days": 0
                },

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


    start = datetime.date(year, 1, 1)
    end = datetime.date(year, 12, 31)

    today = datetime.date.today()

    if year == today.year:
        end = today


    d = start

    while d <= end:

        ds = d.strftime("%Y-%m-%d")

        print(year, ds)

        day = fetch_day(ds)

        if not day:
            d += datetime.timedelta(days=1)
            continue


        if "last_updated" in day:
            db["last_updated"] = day["last_updated"]


        for name, data in day["movies"].items():

            M = ensure_movie(name)

            key = ds.replace("-", "")

            # daily
            M["daily"][key] = [
                data["gross"],
                data["sold"],
                data["shows"],
                data["occupancy"]
            ]


            # totals
            t = M["totals"]

            t["gross"] += data["gross"]
            t["sold"] += data["sold"]
            t["shows"] += data["shows"]
            t["occSum"] += data["occupancy"]
            t["days"] += 1


            # cities / states
            for x in data["details"]:

                add_stat(M["cityMap"], x["city"], x)
                add_stat(M["stateMap"], x["state"], x)


            # chains
            for x in data["Chain_details"]:

                add_stat(M["chainMap"], x["chain"], x)


        time.sleep(SLEEP)

        d += datetime.timedelta(days=1)


    # ------------ FINALIZE ------------

    def build_top(map_obj):

        arr = []

        for k, v in map_obj.items():

            avg = round(v["occSum"] / v["days"], 2)

            arr.append([
                k,
                v["gross"],
                v["sold"],
                v["shows"],
                avg
            ])

        arr.sort(key=lambda x: x[1], reverse=True)

        return arr[:5]


    for m in db["movies"].values():

        t = m["totals"]

        if t["days"]:
            t["avgOcc"] = round(t["occSum"] / t["days"], 2)
        else:
            t["avgOcc"] = 0


        del t["occSum"]
        del t["days"]

        m["topCities"] = build_top(m["cityMap"])
        m["topStates"] = build_top(m["stateMap"])
        m["topChains"] = build_top(m["chainMap"])

        del m["cityMap"]
        del m["stateMap"]
        del m["chainMap"]


    # ------------ SAVE ------------

    with open(f"{year}.json", "w", encoding="utf-8") as f:

        json.dump(
            db,
            f,
            ensure_ascii=False,
            separators=(",", ":")
        )


    print("Saved", year)



# ------------ MAIN ------------

def main():

    now = datetime.date.today().year

    for y in range(START_YEAR, now + 1):

        build_year(y)



if __name__ == "__main__":
    main()
