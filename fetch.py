#!/usr/bin/env python3
import json, os, time, gzip, io, csv, datetime as dt
from zoneinfo import ZoneInfo
import jwt, requests
B = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
out = {"updated_at": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z"), "errors": []}

def meta():
    tok = json.load(open(f"{B}/secrets/meta.json"))["access_token"]
    url = "https://graph.facebook.com/v20.0/act_268221640/insights"
    f = "spend,impressions,clicks,ctr,cpc"
    def q(**p):
        r = requests.get(url, params={"access_token": tok, "fields": f, **p}, timeout=30)
        j = r.json()
        if "error" in j: raise RuntimeError(j["error"].get("message"))
        return j.get("data", [])
    m = {}
    for k, p in {"today": "today", "yesterday": "yesterday", "last_7d": "last_7d"}.items():
        d = q(date_preset=p); m[k] = d[0] if d else {"spend": "0", "impressions": "0", "clicks": "0"}
    m["daily_14d"] = q(date_preset="last_14d", time_increment=1)
    m["campaigns_7d"] = q(date_preset="last_7d", level="campaign", fields=f + ",campaign_name")
    return m

def asc():
    env = dict(l.strip().split("=", 1) for l in open(f"{B}/secrets/asc.env") if "=" in l)
    kp = env["ASC_KEY_PATH"]; kp = kp if os.path.isabs(kp) else f"{B}/secrets/{os.path.basename(kp)}"
    if not os.path.exists(kp): kp = f"{B}/secrets/AuthKey_{env['ASC_KEY_ID']}.p8"
    now = int(time.time())
    t = jwt.encode({"iss": env["ASC_ISSUER_ID"], "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
                   open(kp).read(), algorithm="ES256", headers={"kid": env["ASC_KEY_ID"], "typ": "JWT"})
    H = {"Authorization": f"Bearer {t}"}
    apps = {"6805914932": "LaneSync - Sprint Timer", "6819824551": "StrideLab - Sprint Analysis", "6812496973": "Miami Beach Transit Tracker"}
    res = {"apps": [], "sales": None}
    for aid, name in apps.items():
        e = {"id": aid, "name": name}
        try:
            r = requests.get(f"https://api.appstoreconnect.apple.com/v1/apps/{aid}/appStoreVersions",
                             headers=H, params={"limit": 3}, timeout=30).json()
            if "errors" in r: e["error"] = r["errors"][0].get("detail")
            elif r["data"]:
                v = r["data"][0]["attributes"]
                e.update(version=v.get("versionString"), state=v.get("appStoreState") or v.get("appVersionState"),
                         created=v.get("createdDate"))
        except Exception as x: e["error"] = str(x)
        res["apps"].append(e)
    vendor = env.get("ASC_VENDOR_NUMBER")
    if not vendor:
        res["sales_note"] = "Vendor number not available via API; add ASC_VENDOR_NUMBER to secrets/asc.env"
        return res
    days = []
    for back in range(1, 8):
        d = (dt.date.today() - dt.timedelta(days=back)).isoformat()
        r = requests.get("https://api.appstoreconnect.apple.com/v1/salesReports", headers=H, timeout=60, params={
            "filter[frequency]": "DAILY", "filter[reportType]": "SALES", "filter[reportSubType]": "SUMMARY",
            "filter[vendorNumber]": vendor, "filter[reportDate]": d, "filter[version]": "1_1"})
        if r.status_code != 200: continue
        rows = list(csv.DictReader(io.StringIO(gzip.decompress(r.content).decode()), delimiter="\t"))
        per = {}
        for row in rows:
            a = row.get("Apple Identifier"); ty = row.get("Product Type Identifier", ""); u = int(float(row.get("Units") or 0))
            p = per.setdefault(a, {"downloads": 0, "subscriptions": 0, "title": row.get("Title")})
            if ty.startswith("IA") or ty.startswith("IAY"): p["subscriptions"] += u
            elif ty in ("1", "1F", "1T", "F1"): p["downloads"] += u
        days.append({"date": d, "apps": per})
    res["sales"] = days
    if not days: res["sales_note"] = "No daily SALES reports returned for last 7 days"
    return res

for k, fn in (("meta", meta), ("asc", asc)):
    try: out[k] = fn()
    except Exception as x: out[k] = None; out["errors"].append(f"{k}: {x}")
out.update(json.load(open(f"{B}/data/manual.json")))
json.dump(out, open(f"{B}/docs/data.json", "w"), indent=1)
print("errors:", out["errors"])
