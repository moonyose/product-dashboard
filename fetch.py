#!/usr/bin/env python3
import json, os, time, gzip, io, csv, datetime as dt
from zoneinfo import ZoneInfo
import jwt, requests
B = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")
out = {"updated_at": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M %Z"), "errors": []}

def meta():
    tok = json.load(open(f"{B}/secrets/meta.json"))["access_token"]
    G = "https://graph.facebook.com/v20.0/"; ACT = "act_268221640"
    def get(path, **p):
        j = requests.get(G + path, params={"access_token": tok, **p}, timeout=30).json()
        if "error" in j: raise RuntimeError(j["error"].get("message"))
        return j
    acct = get(ACT, fields="timezone_name,currency")
    tz = ZoneInfo(acct["timezone_name"]); today = dt.datetime.now(tz).date()
    F = "spend,impressions,reach,clicks,inline_link_clicks,inline_link_click_ctr,ctr,cpm,frequency,actions"
    def norm(d):
        d = dict(d or {}); acts = {x["action_type"]: float(x["value"]) for x in d.pop("actions", [])}
        sp = float(d.get("spend") or 0); lc = int(d.get("inline_link_clicks") or 0); im = int(d.get("impressions") or 0)
        d.update(spend=sp, impressions=im, reach=int(d.get("reach") or 0), link_clicks=lc, all_clicks=int(d.get("clicks") or 0),
                 link_ctr=round(lc / im * 100, 3) if im else 0, cost_per_link_click=round(sp / lc, 2) if lc else None,
                 installs=int(acts.get("mobile_app_install", 0)),
                 video_views=int(acts.get("video_view", 0)))
        for k in ("clicks", "inline_link_clicks", "inline_link_click_ctr", "ctr"): d.pop(k, None)
        return d
    def ins(level="account", **p):
        return [norm(x) for x in get(f"{ACT}/insights", fields=F + (",campaign_id,campaign_name" if level == "campaign" else ""),
                                     level=level, use_unified_attribution_setting="true", limit=100, **p).get("data", [])]
    def rng(days): return json.dumps({"since": (today - dt.timedelta(days=days - 1)).isoformat(), "until": today.isoformat()})
    m = {"account_timezone": acct["timezone_name"], "currency": acct.get("currency"),
         "notes": "Days follow the ad account timezone. 7d/14d include today. Clicks/CTR = link clicks (Ads Manager 'Link clicks', 'CTR (link click-through rate)')."}
    for k, p in {"today": {"date_preset": "today"}, "yesterday": {"date_preset": "yesterday"},
                 "last_7d": {"time_range": rng(7)}, "lifetime": {"date_preset": "maximum"}}.items():
        d = ins(**p); m[k] = d[0] if d else norm({})
    m["daily_14d"] = ins(time_range=rng(14), time_increment=1)
    camps = {c["id"]: c for c in get(f"{ACT}/campaigns", fields="name,effective_status,lifetime_budget,daily_budget,budget_remaining,start_time,stop_time", limit=100).get("data", [])}
    def withc(rows):
        for r in rows:
            c = camps.get(r.get("campaign_id"), {})
            r["status"] = c.get("effective_status")
            if c.get("lifetime_budget"): r["lifetime_budget"] = int(c["lifetime_budget"]) / 100
            if c.get("daily_budget"): r["daily_budget"] = int(c["daily_budget"]) / 100
            if r.get("lifetime_budget"): r["budget_remaining"] = round(r["lifetime_budget"] - r["spend"], 2)
        return rows
    m["campaigns_7d"] = withc(ins("campaign", time_range=rng(7)))
    m["campaigns_lifetime"] = withc(ins("campaign", date_preset="maximum"))
    lb = [c for c in m["campaigns_lifetime"] if c.get("lifetime_budget")]
    if lb: m["lifetime"]["lifetime_budget"] = sum(c["lifetime_budget"] for c in lb); m["lifetime"]["budget_remaining"] = sum(c["budget_remaining"] for c in lb)
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
