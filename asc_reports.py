"""Cached App Store Connect daily SALES + SUBSCRIPTION_EVENT reports -> installs per day + trial cohorts.
Cache: data/cache/<KIND>/<YYYY-MM-DD>.tsv (report body) or .none (older 404 = no data). Not committed (see .gitignore)."""
import os, io, csv, gzip, json, time, datetime as dt, concurrent.futures as cf
from zoneinfo import ZoneInfo
import requests
B = os.path.dirname(os.path.abspath(__file__))
CACHE = f"{B}/data/cache"
START = dt.date(2026, 10, 11)
PT = ZoneInfo("America/Los_Angeles")
API = "https://api.appstoreconnect.apple.com"
KINDS = {"SALES": ("SALES", "SUMMARY", "1_1"), "SUBEVT": ("SUBSCRIPTION_EVENT", "SUMMARY", "1_3")}
FIRST = {"1", "1F", "1T", "F1", "1E", "1EP", "1EU"}; REDL = {"3", "3F", "3T", "F3"}; UPD = {"7", "7F", "7T", "F7"}

_MEMO = {}
def _fetch(H, vendor, kind, day, recent):
    if (kind, day) not in _MEMO: _MEMO[(kind, day)] = _fetch1(H, vendor, kind, day, recent)
    st, txt, cached = _MEMO[(kind, day)]; return st, txt, cached
def _fetch1(H, vendor, kind, day, recent):
    """Return (status, rows|None). status: 'ok' | 'none' | 'pending' | 'error:...'"""
    os.makedirs(f"{CACHE}/{kind}", exist_ok=True)
    p = f"{CACHE}/{kind}/{day}"
    if os.path.exists(p + ".tsv"): return "ok", open(p + ".tsv").read(), True
    if os.path.exists(p + ".none"): return "none", None, True
    rt, sub, ver = KINDS[kind]
    try:
        r = requests.get(f"{API}/v1/salesReports", headers=H, timeout=60, params={
            "filter[frequency]": "DAILY", "filter[reportType]": rt, "filter[reportSubType]": sub,
            "filter[vendorNumber]": vendor, "filter[reportDate]": day, "filter[version]": ver})
    except Exception as x: return f"error:{type(x).__name__}", None, False
    if r.status_code == 200:
        txt = gzip.decompress(r.content).decode()
        open(p + ".tsv.tmp", "w").write(txt); os.replace(p + ".tsv.tmp", p + ".tsv")
        return "ok", txt, False
    if r.status_code == 404:
        if recent: return "pending", None, False
        open(p + ".none", "w").write(""); return "none", None, False
    return f"error:HTTP {r.status_code}", None, False

def get_reports(H, vendor, kinds=("SALES", "SUBEVT"), start=START):
    """Fetch (cached) daily reports from start..yesterday(PT). Returns {kind:{day:(status,text)}}, stats."""
    t0 = time.time(); last = dt.datetime.now(PT).date() - dt.timedelta(days=1)
    days = [(start + dt.timedelta(i)).isoformat() for i in range((last - start).days + 1)]
    recent = {d for d in days if (last - dt.date.fromisoformat(d)).days < 2}
    jobs = [(k, d) for k in kinds for d in days]
    out = {k: {} for k in kinds}; hits = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for (k, d), (st, txt, cached) in zip(jobs, ex.map(lambda j: _fetch(H, vendor, j[0], j[1], j[1] in recent), jobs)):
            out[k][d] = (st, txt); hits += cached
    return out, {"days": len(days), "requests": len(jobs) - hits, "cache_hits": hits, "seconds": round(time.time() - t0, 2), "latest_requested": last.isoformat()}

def _rows(txt): return list(csv.DictReader(io.StringIO(txt), delimiter="\t")) if txt else []

def _add(d, dur):
    n, unit = (dur or "").split(" ", 1) if dur and " " in dur else (None, None)
    if not n or not n.isdigit(): return None
    n = int(n); u = unit.lower()
    if u.startswith("day"): return d + dt.timedelta(days=n)
    if u.startswith("week"): return d + dt.timedelta(weeks=n)
    if u.startswith("month") or u.startswith("year"):
        m = n * (12 if u.startswith("year") else 1); y, mo = divmod(d.month - 1 + m, 12)
        import calendar; y += d.year; mo += 1
        return d.replace(year=y, month=mo, day=min(d.day, calendar.monthrange(y, mo)[1]))
    return None

def sub_meta(H):
    """Subscription prices (per territory) + intro offers, cached for 24h in data/cache/subs_meta.json."""
    p = f"{CACHE}/subs_meta.json"
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 86400: return json.load(open(p))
    meta = {}
    for aid in ("6805914932", "6819824551"):
        for g in requests.get(f"{API}/v1/apps/{aid}/subscriptionGroups", headers=H, timeout=30).json().get("data", []):
            for s in requests.get(f"{API}/v1/subscriptionGroups/{g['id']}/subscriptions", headers=H, timeout=30).json().get("data", []):
                a = s["attributes"]; m = meta[s["id"]] = {"app": aid, "name": a.get("name"), "product_id": a.get("productId"), "period": a.get("subscriptionPeriod"), "prices": {}, "intro": []}
                pr = requests.get(f"{API}/v1/subscriptions/{s['id']}/prices", headers=H, timeout=60, params={"include": "subscriptionPricePoint,territory", "limit": 200}).json()
                inc = {x["id"]: x["attributes"] for x in pr.get("included", []) if x["type"] == "subscriptionPricePoints"}
                for x in pr.get("data", []):
                    terr = x["relationships"]["territory"]["data"]["id"]; pp = inc.get(x["relationships"]["subscriptionPricePoint"]["data"]["id"], {})
                    if x["attributes"].get("startDate") is None or terr not in m["prices"]:
                        m["prices"][terr] = {"price": pp.get("customerPrice"), "proceeds": pp.get("proceeds")}
                io_ = requests.get(f"{API}/v1/subscriptions/{s['id']}/introductoryOffers", headers=H, timeout=60, params={"limit": 200}).json()
                m["intro"] = sorted({f"{x['attributes'].get('offerMode')} {x['attributes'].get('duration')}" for x in io_.get("data", [])})
    os.makedirs(CACHE, exist_ok=True); json.dump(meta, open(p, "w")); return meta

CUR = {"USA": "$", "HKG": "HK$", "GBR": "£", "CAN": "CA$", "AUS": "A$", "JPN": "¥"}
def build(H, vendor, apps):
    """apps: {apple_id: name}. Returns dict for data.json plus list of error strings."""
    errs = []
    rep, stats = get_reports(H, vendor)
    for k, v in rep.items():
        bad = sorted(d for d, (s, _) in v.items() if s.startswith("error"))
        if bad: errs.append(f"asc_reports {k}: fetch failed for {', '.join(bad)} ({v[bad[0]][0]})")
    days = sorted(rep["SALES"])
    # ---- installs
    inst = {a: {"name": n, "first_time": [], "redownloads": [], "updates": [], "launch": None} for a, n in apps.items()}
    types = {}
    for d in days:
        st, txt = rep["SALES"][d]; per = {a: [0, 0, 0] for a in apps}
        for r in _rows(txt):
            t = r.get("Product Type Identifier", "").strip(); a = r.get("Apple Identifier"); u = int(float(r.get("Units") or 0))
            if a not in per: continue
            i = 0 if t in FIRST else 1 if t in REDL else 2 if t in UPD else None
            if i is None: continue
            per[a][i] += u; types[t] = types.get(t, 0) + u
        for a in apps:
            avail = st in ("ok", "none")
            for i, k in enumerate(("first_time", "redownloads", "updates")): inst[a][k].append(per[a][i] if avail else None)
            if per[a][0] and not inst[a]["launch"]: inst[a]["launch"] = d
    for a in apps: inst[a]["total_first_time"] = sum(x or 0 for x in inst[a]["first_time"]); inst[a]["total_redownloads"] = sum(x or 0 for x in inst[a]["redownloads"])
    # ---- cohorts
    try: meta = sub_meta(H)
    except Exception as x: meta = {}; errs.append(f"asc_reports subscription metadata: {x}")
    iso = json.load(open(f"{B}/data/iso2to3.json"))
    coh = {}; other = []
    ev_days = sorted(d for d, (s, _) in rep["SUBEVT"].items() if s == "ok")
    for d in ev_days:
        for r in _rows(rep["SUBEVT"][d][1]):
            ev = r.get("Event", "").strip(); q = int(float(r.get("Quantity") or 0) or 0)
            start = (r.get("Original Start Date") or r.get("Event Date") or d).strip()
            key = (start, r.get("App Apple ID"), r.get("Subscription Apple ID"))
            if ev.startswith("Start ") and "Offer" in ev:
                kind = "intro trial" if "Introductory" in ev else "offer code" if "Offer Code" in ev else ev[6:].lower()
                free = r.get("Subscription Offer Type", "").strip() == "Free Trial"
                ck = key + (kind, r.get("Subscription Offer Name", "").strip(), r.get("Subscription Offer Duration", "").strip())
                c = coh.setdefault(ck, {"start": start, "app_id": key[1], "app": apps.get(key[1], r.get("App Name")), "subscription_id": key[2],
                    "product": r.get("Subscription Name"), "period": r.get("Standard Subscription Duration"), "type": kind, "free": free,
                    "offer_name": ck[4] or None, "offer_code": r.get("Promotional Offer ID", "").strip() or None, "trial_length": ck[5] or None,
                    "count": 0, "countries": {}, "conversions": 0, "cancellations": 0, "events": []})
                c["count"] += q; cc = r.get("Country", "").strip(); c["countries"][cc] = c["countries"].get(cc, 0) + q
            else: other.append((d, ev, q, key, r))
    for d, ev, q, key, r in other:
        tgt = [c for c in coh.values() if (c["start"], c["app_id"], c["subscription_id"]) == key]
        rec = {"report_date": d, "event_date": r.get("Event Date"), "event": ev, "quantity": q, "country": r.get("Country")}
        if tgt:
            c = tgt[0]; c["events"].append(rec)
            el = ev.lower()
            if "paid subscription from" in el or el.startswith("renew") or "convert" in el: c["conversions"] += q
            elif "cancel" in el or "refund" in el or "opt-out" in el or "auto-renew off" in el: c["cancellations"] += q
    today = dt.datetime.now(PT).date(); lst = []
    for c in sorted(coh.values(), key=lambda c: (c["start"], c["app"] or "")):
        fc = _add(dt.date.fromisoformat(c["start"]), c["trial_length"]) if c["free"] else dt.date.fromisoformat(c["start"])
        c["expected_first_charge"] = fc.isoformat() if fc else None
        c["expected_in_reports"] = (fc + dt.timedelta(days=1)).isoformat() if fc else None
        m = meta.get(c["subscription_id"], {}); us = m.get("prices", {}).get("USA", {})
        c["price_usd"] = float(us["price"]) if us.get("price") else None
        locs = []
        for cc in c["countries"]:
            t3 = iso.get(cc); lp = m.get("prices", {}).get(t3, {}).get("price") if t3 else None
            if lp and t3 != "USA": locs.append(f"{CUR.get(t3, t3 + ' ')}{float(lp):g}")
        c["price_local"] = ", ".join(locs) or None
        c["status"] = "converted" if c["conversions"] else "cancelled" if c["cancellations"] >= c["count"] else "partly cancelled" if c["cancellations"] else "in trial" if fc and fc > today else "due / awaiting report"
        lst.append(c)
    up = [c for c in lst if c["expected_first_charge"] and c["status"] in ("in trial", "partly cancelled", "due / awaiting report") and c["expected_first_charge"] >= (today - dt.timedelta(days=2)).isoformat()]
    nxt = min(up, key=lambda c: c["expected_first_charge"], default=None)
    return {"range": [days[0] if days else None, days[-1] if days else None], "dates": days,
            "sales_status": {d: rep["SALES"][d][0] for d in days}, "event_report_days": ev_days,
            "pending_days": sorted({d for k in rep for d, (s, _) in rep[k].items() if s == "pending"}),
            "installs": inst, "product_types_counted": sorted(FIRST), "product_types_seen": types,
            "cohorts": lst, "next_conversion": {k: nxt[k] for k in ("start", "app", "product", "type", "count", "expected_first_charge", "expected_in_reports", "trial_length")} if nxt else None,
            "subscriptions": {k: {"app": v["app"], "name": v["name"], "period": v["period"], "price_usd": v["prices"].get("USA", {}).get("price"), "intro_offers": v["intro"]} for k, v in meta.items()},
            "stats": stats,
            "notes": "Installs = first-time downloads (product types 1F/1/1T/F1/1E*); re-downloads (3F) and updates (7F) separate. 404 for the 2 most recent days = not published yet (retried next run); older 404 = no activity. Expected first charge = trial start + offer duration (estimate; assumes auto-renew on)."}, errs
