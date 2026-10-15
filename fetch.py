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
    os.makedirs(f"{B}/docs/icons", exist_ok=True)
    for e in res["apps"]:
        url = None
        try:
            j = requests.get(f"https://itunes.apple.com/lookup?id={e['id']}", timeout=20).json()
            if j.get("resultCount"): url = j["results"][0].get("artworkUrl512"); e["public"] = True
            if not url:
                bj = requests.get(f"https://api.appstoreconnect.apple.com/v1/apps/{e['id']}/builds", headers=H,
                                  params={"limit": 1, "fields[builds]": "iconAssetToken"}, timeout=30).json()
                tok = (bj.get("data") or [{}])[0].get("attributes", {}).get("iconAssetToken")
                if tok: url = tok["templateUrl"].replace("{w}", "256").replace("{h}", "256").replace("{f}", "png")
            if url:
                open(f"{B}/docs/icons/app_{e['id']}.png", "wb").write(requests.get(url, timeout=30).content)
                e["icon"] = f"icons/app_{e['id']}.png"
        except Exception as x: e["icon_error"] = str(x)
    vendor = env.get("ASC_VENDOR_NUMBER")
    if not vendor:
        res["sales_note"] = "Vendor number not available via API; add ASC_VENDOR_NUMBER to secrets/asc.env"
        return res
    days = []
    for back in range(1, 12):
        if len(days) >= 7: break
        d = (dt.date.today() - dt.timedelta(days=back)).isoformat()
        r = requests.get("https://api.appstoreconnect.apple.com/v1/salesReports", headers=H, timeout=60, params={
            "filter[frequency]": "DAILY", "filter[reportType]": "SALES", "filter[reportSubType]": "SUMMARY",
            "filter[vendorNumber]": vendor, "filter[reportDate]": d, "filter[version]": "1_1"})
        if r.status_code != 200: continue
        rows = list(csv.DictReader(io.StringIO(gzip.decompress(r.content).decode()), delimiter="\t"))
        sku2app = {row["SKU"]: row["Apple Identifier"] for row in rows if not row.get("Parent Identifier", "").strip()}
        per = {aid: {"downloads": 0, "redownloads": 0, "new_subs": 0, "new_subs_paid": 0, "new_subs_free": 0, "renewals": 0, "proceeds": 0.0} for aid in apps}
        for row in rows:
            ty = row.get("Product Type Identifier", "").strip(); u = int(float(row.get("Units") or 0))
            parent = row.get("Parent Identifier", "").strip()
            aid = sku2app.get(parent, parent) if parent else row["Apple Identifier"]
            p = per.setdefault(aid, {"downloads": 0, "redownloads": 0, "new_subs": 0, "new_subs_paid": 0, "new_subs_free": 0, "renewals": 0, "proceeds": 0.0})
            p["proceeds"] += u * float(row.get("Developer Proceeds") or 0)
            if ty in ("1", "1F", "1T", "F1", "1E", "1EP", "1EU"): p["downloads"] += u
            elif ty in ("3", "3F", "3T", "F3"): p["redownloads"] += u
            elif ty.startswith("IA") or ty.startswith("FI"):
                sub = row.get("Subscription", "").strip()
                if sub == "Renewal": p["renewals"] += u
                else:
                    p["new_subs"] += u
                    if float(row.get("Customer Price") or 0) > 0: p["new_subs_paid"] += u
                    else: p["new_subs_free"] += u
        days.append({"date": d, "apps": per})
    days.reverse()
    res["sales"] = days
    if not days: res["sales_note"] = "No daily SALES reports returned for last 7 days"
    return res

for k, fn in (("meta", meta), ("asc", asc)):
    try: out[k] = fn()
    except Exception as x: out[k] = None; out["errors"].append(f"{k}: {x}")
out.update(json.load(open(f"{B}/data/manual.json")))
for g in out.get("roblox", {}).get("games", []):
    a = g.get("ads") or {}
    g["ads_playtime_per_play_min"] = round(a["ad_playtime_h"] * 60 / a["plays"], 2) if a.get("plays") and a.get("ad_playtime_h") is not None else None
try:
    ids = ",".join(str(g["universe_id"]) for g in out["roblox"]["games"])
    j = requests.get(f"https://thumbnails.roblox.com/v1/games/icons?universeIds={ids}&size=256x256&format=Png&isCircular=false", timeout=30).json()
    os.makedirs(f"{B}/docs/icons", exist_ok=True)
    m = {str(x["targetId"]): x.get("imageUrl") for x in j.get("data", [])}
    for g in out["roblox"]["games"]:
        u = m.get(str(g["universe_id"]))
        if u:
            open(f"{B}/docs/icons/rbx_{g['universe_id']}.png", "wb").write(requests.get(u, timeout=30).content)
            g["icon"] = f"icons/rbx_{g['universe_id']}.png"
except Exception as x: out["errors"].append(f"roblox icons: {x}")
json.dump(out, open(f"{B}/docs/data.json", "w"), indent=1)
print("errors:", out["errors"])
