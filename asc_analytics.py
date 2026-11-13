#!/usr/bin/env python3
"""App Store Connect Analytics Reports: request, download, and summarize usage.

Missing data is not an error — ONGOING requests typically take 1–2 days
to produce the first instances. Usage, crashes, retention, and the
installs/deletions report only include users who opted in to share
analytics with developers (Apple's 5-user privacy threshold also applies).
App Store impressions, page views, and downloads count all devices.
"""
import csv, gzip, hashlib, io, json, os, time, datetime as dt
from urllib.parse import urljoin

import jwt, requests

B = os.path.dirname(os.path.abspath(__file__))
API = "https://api.appstoreconnect.apple.com/v1"
CACHE = os.path.join(B, "data", "cache", "ANALYTICS")
APPS = {"6805914932": "LaneSync", "6819824551": "StrideLab"}
GENERATING = "Generating, usually ready in 1–2 days"

# Report name fragments → logical kind. Prefer Standard over Detailed/Expanded.
REPORT_KINDS = [
    ("sessions", ("app sessions",)),
    ("crashes", ("app crashes",)),
    ("installs_deletes", ("installation and deletion", "installations and deletions")),
    ("discovery", ("discovery and engagement",)),
    ("downloads", ("app store downloads", "app downloads")),
    ("opt_in", ("opt-in", "opt in")),
]
SKIP_IF_STANDARD_EXISTS = ("detailed", "expanded")


class ApiError(Exception):
    def __init__(self, status, body):
        self.status = status
        errs = (body or {}).get("errors") or []
        detail = errs[0].get("detail") if errs else (body or {}).get("error")
        self.detail = detail or f"HTTP {status}"
        self.code = (errs[0].get("code") if errs else None) or ""
        super().__init__(self.detail)


def _headers_from_env():
    env = dict(l.strip().split("=", 1) for l in open(f"{B}/secrets/asc.env") if "=" in l)
    kp = env["ASC_KEY_PATH"]
    kp = kp if os.path.isabs(kp) else f"{B}/secrets/{os.path.basename(kp)}"
    if not os.path.exists(kp):
        kp = f"{B}/secrets/AuthKey_{env['ASC_KEY_ID']}.p8"
    now = int(time.time())
    token = jwt.encode(
        {"iss": env["ASC_ISSUER_ID"], "iat": now, "exp": now + 1200, "aud": "appstoreconnect-v1"},
        open(kp).read(),
        algorithm="ES256",
        headers={"kid": env["ASC_KEY_ID"], "typ": "JWT"},
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _request(method, path, headers, **kw):
    url = path if path.startswith("http") else urljoin(API + "/", path.lstrip("/"))
    r = requests.request(method, url, headers=headers, timeout=kw.pop("timeout", 60), **kw)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"error": f"non-JSON response ({r.status_code})"}
    if r.status_code >= 400:
        raise ApiError(r.status_code, body)
    return body


def _get_all(path, headers, params=None):
    items, included = [], []
    url, params = path, params
    while url:
        body = _request("GET", url, headers, params=params)
        params = None
        items.extend(body.get("data") or [])
        included.extend(body.get("included") or [])
        url = (body.get("links") or {}).get("next")
    return items, included


def _attrs(item):
    return item.get("attributes") or {}


def _role_help(status, detail):
    if status == 403:
        return (
            f"Permission denied (403): {detail} "
            "An Admin API key is required to request a new Analytics Report type "
            "for the first time. After an Admin has created the request, a key with "
            "the Sales and Reports or Finance role can download generated reports."
        )
    return f"ASC Analytics API error ({status}): {detail}"


def list_requests(headers, app_id):
    items, _ = _get_all(
        f"apps/{app_id}/analyticsReportRequests",
        headers,
        params={"limit": 200},
    )
    return items


def create_request(headers, app_id, access_type="ONGOING"):
    return _request(
        "POST",
        "analyticsReportRequests",
        headers,
        json={
            "data": {
                "type": "analyticsReportRequests",
                "attributes": {"accessType": access_type},
                "relationships": {"app": {"data": {"type": "apps", "id": str(app_id)}}},
            }
        },
    ).get("data")


def ensure_request(headers, app_id):
    """Return (request_dict|None, created: bool, error: str|None)."""
    try:
        existing = list_requests(headers, app_id)
    except ApiError as e:
        return None, False, _role_help(e.status, e.detail)
    except Exception as e:
        return None, False, f"list analyticsReportRequests failed: {type(e).__name__}: {e}"

    def is_ongoing(item):
        a = _attrs(item)
        return a.get("accessType") == "ONGOING" and not a.get("stoppedDueToInactivity")

    live = [x for x in existing if is_ongoing(x)]
    if live:
        return live[0], False, None
    # Any stopped ONGOING requests are ignored; create a fresh one.
    try:
        created = create_request(headers, app_id, "ONGOING")
        return created, True, None
    except ApiError as e:
        # 409 conflict: a request already exists — re-list and use it.
        if e.status in (409, 422):
            try:
                existing = list_requests(headers, app_id)
                live = [x for x in existing if is_ongoing(x)] or existing
                if live:
                    return live[0], False, None
            except Exception:
                pass
        return None, False, _role_help(e.status, e.detail)
    except Exception as e:
        return None, False, f"create analyticsReportRequest failed: {type(e).__name__}: {e}"


def _kind_for(name):
    n = (name or "").strip().lower()
    if not n:
        return None
    for kind, frags in REPORT_KINDS:
        if any(f in n for f in frags):
            return kind
    return None


def _is_detailed(name):
    n = (name or "").lower()
    return any(s in n for s in SKIP_IF_STANDARD_EXISTS)


def pick_reports(reports):
    """Prefer Standard (or unnamed) over Detailed/Expanded for each kind."""
    by_kind = {}
    for r in reports:
        name = _attrs(r).get("name") or ""
        kind = _kind_for(name)
        if not kind:
            continue
        prev = by_kind.get(kind)
        if prev is None or (_is_detailed(_attrs(prev).get("name")) and not _is_detailed(name)):
            by_kind[kind] = r
    return by_kind


def list_reports(headers, request_id):
    items, _ = _get_all(
        f"analyticsReportRequests/{request_id}/reports",
        headers,
        params={"limit": 200},
    )
    return items


def list_instances(headers, report_id, granularity="DAILY"):
    items, _ = _get_all(
        f"analyticsReports/{report_id}/instances",
        headers,
        params={"limit": 200, "filter[granularity]": granularity},
    )
    items.sort(key=lambda x: _attrs(x).get("processingDate") or "")
    return items


def list_segments(headers, instance_id):
    items, _ = _get_all(
        f"analyticsReportInstances/{instance_id}/segments",
        headers,
        params={"limit": 200},
    )
    return items


def _cache_path(app_id, kind, processing_date, instance_id, idx):
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{app_id}_{kind}_{processing_date}_{instance_id}_{idx}")
    return os.path.join(CACHE, safe + ".tsv")


def download_segment(seg, cache_file):
    """Download a pre-signed gzip segment (no auth header). Return text."""
    url = _attrs(seg).get("url")
    checksum = (_attrs(seg).get("checksum") or "").strip()
    if not url:
        return ""
    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
        return open(cache_file, encoding="utf-8", errors="replace").read()
    raw = requests.get(url, timeout=120).content
    if checksum:
        # Apple documents a checksum; try sha256 then md5. Mismatch is not fatal.
        got = hashlib.sha256(raw).hexdigest()
        if got.lower() != checksum.lower():
            got_md5 = hashlib.md5(raw).hexdigest()
            if got_md5.lower() != checksum.lower():
                pass
    try:
        text = gzip.decompress(raw).decode("utf-8", errors="replace")
    except OSError:
        text = raw.decode("utf-8", errors="replace")
    os.makedirs(os.path.dirname(cache_file), exist_ok=True)
    tmp = cache_file + ".tmp"
    open(tmp, "w", encoding="utf-8").write(text)
    os.replace(tmp, cache_file)
    return text


def parse_table(text):
    """Parse tab- or comma-delimited text into a list of dicts. Skip repeated headers."""
    if not text or not text.strip():
        return []
    sample = text[:4096]
    dialect = csv.excel_tab
    if "\t" not in sample and "," in sample:
        dialect = csv.excel
    rows, header = [], None
    for rec in csv.reader(io.StringIO(text), dialect=dialect):
        if not rec or all(not (c or "").strip() for c in rec):
            continue
        if header is None or rec == header:
            if header is None:
                header = rec
            continue
        # A later segment may repeat the header row.
        if rec[0] == header[0] and rec == header:
            continue
        rows.append({header[i]: rec[i] if i < len(rec) else "" for i in range(len(header))})
    return rows


def _num(v):
    if v is None or v == "":
        return 0
    try:
        return int(float(str(v).replace(",", "").strip()))
    except ValueError:
        return 0


def _date(row):
    return (row.get("Date") or row.get("date") or "").strip()[:10]


def _get(row, *names):
    lower = {k.lower().strip(): v for k, v in row.items()}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    # fuzzy: ignore punctuation/spaces
    def norm(s):
        return "".join(ch for ch in s.lower() if ch.isalnum())
    nmap = {norm(k): v for k, v in row.items()}
    for n in names:
        if norm(n) in nmap:
            return nmap[norm(n)]
    return ""


def _event(row):
    return (_get(row, "Event") or "").strip().lower()


def summarize_sessions(rows):
    """Additive sessions/duration; unique devices are an estimate (see note)."""
    by_day = {}
    for row in rows:
        d = _date(row)
        if not d:
            continue
        day = by_day.setdefault(d, {"sessions": 0, "duration_sec": 0, "unique_sum": 0, "unique_max": 0, "rows": 0,
                                    "by_cohort": {}})
        sess = _num(_get(row, "Sessions"))
        dur = _num(_get(row, "Total Session Duration", "Total Session Duration (Seconds)"))
        uniq = _num(_get(row, "Unique Devices", "Unique Counts", "Unique Device"))
        day["sessions"] += sess
        day["duration_sec"] += dur
        day["unique_sum"] += uniq
        day["unique_max"] = max(day["unique_max"], uniq)
        day["rows"] += 1
        cohort = (_get(row, "App Download Date") or "").strip()[:10]
        if cohort:
            c = day["by_cohort"].setdefault(cohort, {"sessions": 0, "unique_sum": 0})
            c["sessions"] += sess
            c["unique_sum"] += uniq
    for d, day in by_day.items():
        # One row → exact. Several dimension rows → unique_sum is an upper bound.
        day["active_devices"] = day["unique_sum"] if day["rows"] <= 1 else day["unique_max"]
        day["active_devices_upper"] = day["unique_sum"]
        day.pop("rows", None)
    return by_day


def compute_retention(session_by_day):
    """Approximate day-1 / day-7 retention from App Download Date × session date.

    Apple's official retention view is not in the Analytics Reports API.
    This uses Unique Devices (upper-bound if dimensions overlap) for cohorts
    that have enough opted-in users to clear Apple's privacy threshold.
    Returns None for a rate when it cannot be computed reliably.
    """
    # cohort C → {offset_days: unique_sum}
    cohorts = {}
    for d, day in session_by_day.items():
        for c, vals in (day.get("by_cohort") or {}).items():
            try:
                offset = (dt.date.fromisoformat(d) - dt.date.fromisoformat(c)).days
            except ValueError:
                continue
            if offset < 0:
                continue
            bag = cohorts.setdefault(c, {})
            bag[offset] = bag.get(offset, 0) + vals.get("unique_sum", 0)

    def avg_rate(offset):
        rates = []
        for c, bag in cohorts.items():
            # Denominator: devices from this cohort seen on day 0 (first open / install day).
            # If day 0 is missing, skip — we don't have a stable cohort size.
            denom = bag.get(0) or 0
            num = bag.get(offset) or 0
            if denom >= 5 and offset in bag:
                rates.append(num / denom)
        if not rates:
            return None
        return round(sum(rates) / len(rates) * 100, 1)

    d1, d7 = avg_rate(1), avg_rate(7)
    if d1 is None and d7 is None:
        return None
    return {"day1_pct": d1, "day7_pct": d7, "source": "app sessions, App Download Date, opted-in users"}


def summarize_crashes(rows):
    by_day = {}
    for row in rows:
        d = _date(row)
        if not d:
            continue
        day = by_day.setdefault(d, {"crashes": 0, "unique_devices": 0})
        day["crashes"] += _num(_get(row, "Crashes"))
        day["unique_devices"] += _num(_get(row, "Unique Devices"))
    return by_day


def summarize_installs_deletes(rows):
    by_day = {}
    for row in rows:
        d = _date(row)
        if not d:
            continue
        ev = _event(row)
        day = by_day.setdefault(d, {"installs": 0, "deletions": 0})
        n = _num(_get(row, "Counts", "Count"))
        if ev in ("delete", "deletion", "deletes"):
            day["deletions"] += n
        elif ev in ("install", "installation", "installs"):
            day["installs"] += n
    return by_day


def summarize_discovery(rows):
    by_day = {}
    for row in rows:
        d = _date(row)
        if not d:
            continue
        ev = _event(row)
        page = (_get(row, "Page Type") or "").strip().lower()
        n = _num(_get(row, "Counts", "Count"))
        day = by_day.setdefault(d, {"impressions": 0, "page_views": 0, "product_page_views": 0, "taps": 0})
        if ev == "impression":
            day["impressions"] += n
        elif ev in ("page view", "pageview", "page_view"):
            day["page_views"] += n
            if "product page" in page:
                day["product_page_views"] += n
        elif ev == "tap":
            day["taps"] += n
    return by_day


def summarize_downloads(rows):
    by_day = {}
    for row in rows:
        d = _date(row)
        if not d:
            continue
        typ = (_get(row, "Download Type") or "").strip().lower()
        n = _num(_get(row, "Counts", "Count"))
        day = by_day.setdefault(d, {"first_time": 0, "redownloads": 0, "updates": 0, "downloads": 0})
        if "first" in typ:
            day["first_time"] += n
            day["downloads"] += n
        elif "redownload" in typ:
            day["redownloads"] += n
            day["downloads"] += n
        elif "update" in typ:
            day["updates"] += n
        else:
            # Unknown types don't join the "downloads" funnel total.
            pass
    return by_day


def summarize_opt_in(rows):
    by_day = {}
    for row in rows:
        d = _date(row)
        if not d:
            continue
        down = _num(_get(row, "Downloading Users", "DownloadingUsers"))
        opt = _num(_get(row, "Users Opting-In", "Users Opting In", "UsersOptingIn"))
        by_day[d] = {"downloading_users": down, "users_opting_in": opt}
    return by_day


SUMMARIZERS = {
    "sessions": summarize_sessions,
    "crashes": summarize_crashes,
    "installs_deletes": summarize_installs_deletes,
    "discovery": summarize_discovery,
    "downloads": summarize_downloads,
    "opt_in": summarize_opt_in,
}


def _fetch_kind(headers, app_id, kind, report, keep_days=21):
    instances = list_instances(headers, report["id"], "DAILY")
    out = {}
    # Keep the most recent keep_days instances (processingDate).
    recent = instances[-keep_days:]
    for inst in recent:
        pdate = _attrs(inst).get("processingDate") or ""
        try:
            segs = list_segments(headers, inst["id"])
        except ApiError:
            continue
        rows = []
        for i, seg in enumerate(segs):
            cache = _cache_path(app_id, kind, pdate, inst["id"], i)
            try:
                text = download_segment(seg, cache)
            except Exception:
                continue
            rows.extend(parse_table(text))
        if not rows:
            continue
        daily = SUMMARIZERS[kind](rows)
        # Merge in case a table contains multiple dates (shouldn't, but be safe).
        for d, vals in daily.items():
            if d not in out:
                out[d] = vals
            else:
                # Later instance for same date can be a correction; prefer later.
                out[d] = vals
    return out, [_attrs(i).get("processingDate") for i in instances]


def _window_stats(session_by_day, days=7):
    dates = sorted(session_by_day)
    if not dates:
        return None, None
    window = dates[-days:]
    sessions = sum(session_by_day[d]["sessions"] for d in window)
    duration = sum(session_by_day[d]["duration_sec"] for d in window)
    actives = [session_by_day[d]["active_devices"] for d in window]
    latest = session_by_day[window[-1]]["active_devices"]
    avg_active = round(sum(actives) / len(actives), 1) if actives else 0
    avg_sec = round(duration / sessions, 1) if sessions else 0
    return window, {
        "sessions": sessions,
        "active_devices_latest": latest,
        "avg_daily_active_devices": avg_active,
        "avg_session_sec": avg_sec,
        "total_session_duration_sec": duration,
    }


def _sum_field(by_day, field, window):
    return sum((by_day.get(d) or {}).get(field, 0) for d in window)


def summarize_app(headers, app_id, app_name, request):
    rec = {
        "id": app_id,
        "name": app_name,
        "status": "generating",
        "note": GENERATING,
        "request_id": request.get("id") if request else None,
        "access_type": _attrs(request).get("accessType") if request else None,
        "stopped_due_to_inactivity": bool(_attrs(request).get("stoppedDueToInactivity")) if request else None,
        "window": None,
        "opt_in_rate_pct": None,
        "sessions_7d": None,
        "active_devices_latest": None,
        "avg_daily_active_devices": None,
        "avg_session_sec": None,
        "retention": None,
        "deletions_7d": None,
        "installs_7d": None,
        "crashes_7d": None,
        "funnel": None,
        "daily": [],
        "reports_found": [],
        "opt_in_note": (
            "Sessions, active devices, retention, crashes, and deletions include only "
            "users who opted in to share analytics with developers. Impressions, "
            "page views, and downloads count all devices."
        ),
    }
    if not request:
        rec["status"] = "no_request"
        rec["note"] = "No analytics report request is available for this app."
        return rec
    try:
        reports = list_reports(headers, request["id"])
    except ApiError as e:
        rec["status"] = "error"
        rec["note"] = _role_help(e.status, e.detail)
        rec["permission_error"] = e.status == 403
        return rec

    picked = pick_reports(reports)
    rec["reports_found"] = [
        {"kind": k, "name": _attrs(r).get("name"), "category": _attrs(r).get("category")}
        for k, r in picked.items()
    ]

    if not picked:
        # Request exists but Apple has not attached reports yet.
        rec["status"] = "generating"
        rec["note"] = GENERATING
        return rec

    data = {}
    instance_dates = {}
    for kind, report in picked.items():
        try:
            daily, dates = _fetch_kind(headers, app_id, kind, report)
        except ApiError as e:
            rec.setdefault("kind_errors", []).append(f"{kind}: {_role_help(e.status, e.detail)}")
            continue
        except Exception as e:
            rec.setdefault("kind_errors", []).append(f"{kind}: {type(e).__name__}: {e}")
            continue
        data[kind] = daily
        instance_dates[kind] = [x for x in dates if x]

    any_rows = any(data.get(k) for k in data)
    any_instances = any(instance_dates.values())
    if not any_rows and not any_instances:
        rec["status"] = "generating"
        rec["note"] = GENERATING
        return rec
    if not any_rows:
        rec["status"] = "generating"
        rec["note"] = GENERATING
        return rec

    # Align the 7-day window to the most recent date that appears in any report.
    all_dates = sorted({d for daily in data.values() for d in daily})
    window = all_dates[-7:]
    rec["window"] = {"start": window[0], "end": window[-1], "days": len(window)}

    sess = data.get("sessions") or {}
    wstats_window, wstats = _window_stats({d: sess[d] for d in window if d in sess}, days=len(window))
    if wstats:
        rec["sessions_7d"] = wstats["sessions"]
        rec["active_devices_latest"] = wstats["active_devices_latest"]
        rec["avg_daily_active_devices"] = wstats["avg_daily_active_devices"]
        rec["avg_session_sec"] = wstats["avg_session_sec"]
    rec["retention"] = compute_retention(sess)

    inst = data.get("installs_deletes") or {}
    rec["deletions_7d"] = _sum_field(inst, "deletions", window) if inst else None
    rec["installs_7d"] = _sum_field(inst, "installs", window) if inst else None

    crashes = data.get("crashes") or {}
    rec["crashes_7d"] = _sum_field(crashes, "crashes", window) if crashes else None

    disc = data.get("discovery") or {}
    dls = data.get("downloads") or {}
    if disc or dls:
        rec["funnel"] = {
            "impressions": _sum_field(disc, "impressions", window) if disc else None,
            "page_views": _sum_field(disc, "page_views", window) if disc else None,
            "product_page_views": _sum_field(disc, "product_page_views", window) if disc else None,
            "taps": _sum_field(disc, "taps", window) if disc else None,
            "downloads": _sum_field(dls, "downloads", window) if dls else None,
            "first_time_downloads": _sum_field(dls, "first_time", window) if dls else None,
            "redownloads": _sum_field(dls, "redownloads", window) if dls else None,
        }
        # Conversion rates on additive counts (all devices).
        fun = rec["funnel"]
        if fun.get("impressions") and fun.get("page_views") is not None:
            fun["impression_to_page_view_pct"] = round(fun["page_views"] / fun["impressions"] * 100, 2)
        if fun.get("page_views") and fun.get("first_time_downloads") is not None:
            fun["page_view_to_download_pct"] = round(fun["first_time_downloads"] / fun["page_views"] * 100, 2)
        elif fun.get("impressions") and fun.get("first_time_downloads") is not None:
            fun["impression_to_download_pct"] = round(fun["first_time_downloads"] / fun["impressions"] * 100, 2)

    opt = data.get("opt_in") or {}
    if opt:
        down = _sum_field(opt, "downloading_users", window)
        oin = _sum_field(opt, "users_opting_in", window)
        rec["opt_in_rate_pct"] = round(oin / down * 100, 1) if down else None
        rec["opt_in_users_7d"] = oin
        rec["opt_in_downloaders_7d"] = down

    daily = []
    for d in window:
        s = sess.get(d) or {}
        i = inst.get(d) or {}
        c = crashes.get(d) or {}
        g = disc.get(d) or {}
        l = dls.get(d) or {}
        o = opt.get(d) or {}
        daily.append({
            "date": d,
            "sessions": s.get("sessions"),
            "active_devices": s.get("active_devices"),
            "avg_session_sec": round(s["duration_sec"] / s["sessions"], 1) if s.get("sessions") else None,
            "deletions": i.get("deletions"),
            "installs": i.get("installs"),
            "crashes": c.get("crashes"),
            "impressions": g.get("impressions"),
            "page_views": g.get("page_views"),
            "product_page_views": g.get("product_page_views"),
            "downloads": l.get("downloads"),
            "first_time_downloads": l.get("first_time"),
            "opt_in_rate_pct": (
                round(o["users_opting_in"] / o["downloading_users"] * 100, 1)
                if o.get("downloading_users") else None
            ),
        })
    rec["daily"] = daily
    rec["status"] = "ready"
    rec["note"] = None
    return rec


def build(headers, apps=None):
    """Ensure an ONGOING request per app, then download and summarize.

    Returns (payload, errors). Missing reports are not treated as errors.
    Permission failures are recorded on the payload and in errors.
    """
    apps = apps or APPS
    errors = []
    payload = {
        "apps": {},
        "requests": {},
        "generated_at": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "disclaimer": (
            "App Analytics usage, crashes, and retention only count users who opted "
            "in to sharing analytics with developers. Low-volume days may be blank "
            "because Apple omits rows with fewer than five users."
        ),
    }
    for app_id, name in apps.items():
        req, created, err = ensure_request(headers, app_id)
        payload["requests"][app_id] = {
            "name": name,
            "request_id": req.get("id") if req else None,
            "created_this_run": created,
            "access_type": _attrs(req).get("accessType") if req else None,
            "stopped_due_to_inactivity": bool(_attrs(req).get("stoppedDueToInactivity")) if req else None,
            "error": err,
        }
        if err:
            errors.append(f"{name} analytics: {err}")
            payload["apps"][app_id] = {
                "id": app_id,
                "name": name,
                "status": "forbidden" if "403" in err or "Permission denied" in err else "error",
                "note": err,
                "permission_error": "403" in err or "Permission denied" in err,
                "request_id": req.get("id") if req else None,
            }
            # If we have no request, skip the download attempt.
            if not req:
                continue
        try:
            payload["apps"][app_id] = summarize_app(headers, app_id, name, req)
        except ApiError as e:
            msg = _role_help(e.status, e.detail)
            payload["apps"][app_id] = {
                "id": app_id, "name": name, "status": "error", "note": msg,
                "permission_error": e.status == 403, "request_id": req.get("id") if req else None,
            }
            errors.append(f"{name} analytics: {msg}")
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            payload["apps"][app_id] = {
                "id": app_id, "name": name, "status": "error", "note": msg,
                "request_id": req.get("id") if req else None,
            }
            errors.append(f"{name} analytics: {msg}")
    return payload, errors


def main():
    """Ensure requests and print a secret-free status summary."""
    headers = _headers_from_env()
    payload, errors = build(headers, APPS)
    # Never print tokens, headers, or env. Status only.
    print("analytics status")
    for app_id, info in (payload.get("requests") or {}).items():
        print(
            f"  {info.get('name')} ({app_id}): "
            f"request_id={info.get('request_id')} "
            f"created_this_run={info.get('created_this_run')} "
            f"access_type={info.get('access_type')} "
            f"stopped={info.get('stopped_due_to_inactivity')} "
            f"error={info.get('error')!s}"
        )
    for app_id, rec in (payload.get("apps") or {}).items():
        print(
            f"  summary {rec.get('name')}: status={rec.get('status')} "
            f"note={rec.get('note')!s} reports={len(rec.get('reports_found') or [])} "
            f"window={rec.get('window')}"
        )
    if errors:
        print("errors:")
        for e in errors:
            print(" ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
