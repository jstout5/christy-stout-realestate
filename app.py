"""
ChristyStout.com — Real Estate Site
Christy Stout · Bluegrass Property Exchange · Lexington, KY
"""
import os, json, re, time, smtplib
from pathlib import Path
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import requests as req

load_dotenv()
app = Flask(__name__)

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD")
CHRISTY_EMAIL = "criggs4568@gmail.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.redfin.com/",
}

# Redfin region IDs for Central KY cities
REGIONS = {
    "Lexington":    {"id": "11746", "type": "6"},
    "Richmond":     {"id": "16725", "type": "6"},
    "Georgetown":   {"id": "6083",  "type": "6"},
    "Versailles":   {"id": "19003", "type": "6", "poly": "-84.78 38.09,-84.68 38.09,-84.68 38.02,-84.78 38.02,-84.78 38.09"},
    "Nicholasville": {"id": "14000", "type": "6"},
    "Winchester":   {"id": "21000", "type": "6"},
}

# Simple cache
_cache = {}
CACHE_TTL = 600  # 10 min

def cached(key, fn, ttl=CACHE_TTL):
    now = time.time()
    if key in _cache and now - _cache[key][0] < ttl:
        return _cache[key][1]
    data = fn()
    _cache[key] = (now, data)
    return data


def redfin_search(region_id, region_type=None, poly=None,
                  min_beds=0, min_baths=0, min_price=50000,
                  max_price=0, sort="price-asc", num=50):
    params = {
        "al": 1, "num_homes": num, "ord": sort,
        "page_number": 1, "sf": "1,2,3,5,6,7",
        "start": 0, "status": 9, "uipt": "1,2,3", "v": 8,
        "min_price": min_price,
    }
    if min_beds:  params["min_beds"] = min_beds
    if min_baths: params["min_baths"] = min_baths
    if max_price: params["max_price"] = max_price
    if poly:
        params["poly"] = poly
    else:
        params["region_id"]   = region_id
        params["region_type"] = region_type or "6"

    try:
        r = req.get("https://www.redfin.com/stingray/api/gis",
                    params=params, headers=HEADERS, timeout=15)
        text = r.text[4:] if r.text.startswith("{}&&") else r.text
        homes = json.loads(text).get("payload", {}).get("homes", [])
        results = []
        for h in homes:
            price  = (h.get("price") or {}).get("value", 0) or 0
            sqft   = (h.get("sqFt") or {}).get("value", 0) or 0
            beds   = h.get("beds") or 0
            baths  = h.get("baths") or 0
            if price <= 0: continue
            ppsf   = (h.get("pricePerSqFt") or {}).get("value", 0) or (round(price/sqft) if sqft else 0)
            street = (h.get("streetLine") or {}).get("value", "")
            city_s = h.get("city", ""); state_s = h.get("state", ""); zip_s = str(h.get("zip",""))
            yr     = (h.get("yearBuilt") or {}).get("value")
            days   = (h.get("dom") or {}).get("value", 0) or 0
            url    = "https://www.redfin.com" + (h.get("url") or "")
            results.append({
                "address": f"{street}, {city_s}, {state_s} {zip_s}".strip(", "),
                "city": city_s, "state": state_s, "zip": zip_s,
                "price": price, "beds": int(beds), "baths": float(baths or 0),
                "sqft": int(sqft), "ppsf": int(ppsf),
                "year_built": yr, "days_on": int(days), "url": url,
            })
        return results
    except Exception:
        return []


# ── API ROUTES ────────────────────────────────────────────────────────────────

@app.route("/api/market-stats")
def market_stats():
    def fetch():
        stats = {}
        for city, info in REGIONS.items():
            try:
                homes = redfin_search(
                    info["id"], info.get("type"),
                    poly=info.get("poly"), num=100
                )
                if not homes: continue
                prices = [h["price"] for h in homes]
                ppsfs  = [h["ppsf"] for h in homes if h["ppsf"]]
                days   = [h["days_on"] for h in homes if h["days_on"]]
                stats[city] = {
                    "count":    len(homes),
                    "median":   sorted(prices)[len(prices)//2],
                    "low":      min(prices),
                    "high":     max(prices),
                    "avg_ppsf": round(sum(ppsfs)/len(ppsfs)) if ppsfs else 0,
                    "avg_dom":  round(sum(days)/len(days)) if days else 0,
                }
            except Exception:
                continue
        return stats
    return jsonify({"stats": cached("market_stats", fetch, ttl=900)})


@app.route("/api/listings")
def get_listings():
    city      = request.args.get("city", "Lexington")
    min_beds  = int(request.args.get("beds", 0) or 0)
    min_baths = float(request.args.get("baths", 0) or 0)
    min_price = int(request.args.get("min_price", 50000) or 50000)
    max_price = int(request.args.get("max_price", 0) or 0)
    sort_by   = request.args.get("sort", "price-asc")

    info = REGIONS.get(city, REGIONS["Lexington"])
    key  = f"listings_{city}_{min_beds}_{min_baths}_{min_price}_{max_price}_{sort_by}"

    def fetch():
        return redfin_search(
            info["id"], info.get("type"),
            poly=info.get("poly"),
            min_beds=min_beds, min_baths=int(min_baths),
            min_price=min_price, max_price=max_price,
            sort=sort_by, num=24,
        )
    return jsonify({"listings": cached(key, fetch)})


@app.route("/api/best-deals")
def best_deals():
    """Best value homes — lowest $/sqft with decent size across all areas."""
    def fetch():
        all_homes = []
        for city, info in list(REGIONS.items())[:3]:  # top 3 cities
            homes = redfin_search(info["id"], info.get("type"),
                                  poly=info.get("poly"),
                                  min_beds=2, min_price=80000, num=100)
            for h in homes:
                if h["sqft"] >= 900 and h["ppsf"] > 0:
                    h["city_label"] = city
                    all_homes.append(h)
        all_homes.sort(key=lambda x: x["ppsf"])
        return all_homes[:8]
    return jsonify({"deals": cached("best_deals", fetch, ttl=900)})


@app.route("/api/new-listings")
def new_listings():
    """Homes listed in the last 7 days."""
    def fetch():
        homes = redfin_search("11746", "6", sort="days-asc", num=50)
        return [h for h in homes if h["days_on"] <= 7][:8]
    return jsonify({"listings": cached("new_listings", fetch, ttl=600)})


@app.route("/api/contact", methods=["POST"])
def contact():
    data    = request.get_json()
    name    = (data.get("name") or "").strip()
    email   = (data.get("email") or "").strip()
    phone   = (data.get("phone") or "").strip()
    ctype   = (data.get("type") or "").strip()
    message = (data.get("message") or "").strip()

    if not name or not email:
        return jsonify({"error": "Name and email required"}), 400

    if GMAIL_USER and GMAIL_PASS:
        try:
            body = f"""New inquiry from ChristyStout.com

Name:    {name}
Email:   {email}
Phone:   {phone}
Looking: {ctype}

{message}"""
            msg = MIMEMultipart()
            msg["From"]    = GMAIL_USER
            msg["To"]      = CHRISTY_EMAIL
            msg["Subject"] = f"ChristyStout.com — {ctype or 'Inquiry'} from {name}"
            msg.attach(MIMEText(body, "plain"))
            with smtplib.SMTP("smtp.gmail.com", 587) as s:
                s.ehlo(); s.starttls(); s.login(GMAIL_USER, GMAIL_PASS)
                s.sendmail(GMAIL_USER, [CHRISTY_EMAIL], msg.as_string())
        except Exception as e:
            print(f"Email error: {e}")

    return jsonify({"status": "sent"})


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True, port=5052)
