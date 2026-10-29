"""Pull DOP + TM cost data from Google Sheets, encrypt it, and write site/ for GitHub Pages.

Env vars (set as GitHub Actions secrets):
  GOOGLE_SA_JSON  - full JSON key of a Google service account that has Viewer access to both sheets
  DASH_PASSWORD   - password people type to open the dashboard
Optional:
  MOCK_VALUES     - path to a JSON file of {range: values} used instead of Google (for local testing)
"""
import base64
import re
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

DOP_SHEET = "1AiI-an7F-dtpZ_BoTnrQY65Kokha8c3csloRSrSueuQ"  # Daily Shop PNL (DOP bot)
TM_SHEET = "1AnaHxF1yKUWAAVqy77PkYb4fWdVxcSdZ1bwdByHmNJk"   # All Shop Daily TM Cost
TIER_SHEET = "1YFEdwrN9E8LQlB63hWZU6kWg_99wUjmxP_kUsKmZQvg"  # Tier of shop (by target): Code / Shop Name / Tier
POS_SHEET = "1AZBZXU_dbRvTdUaE-J0WVcM62ZFkMTMOhsARdSNiyec"  # O2O POS Target (Tableau) - copy of Tableau crosstabs
ITERATIONS = 250_000
ROOT = Path(__file__).resolve().parent
HKT = ZoneInfo("Asia/Hong_Kong")


# ---------- parsing ----------
def num(s, pct_as_fraction=True):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    is_pct = s.endswith("%")
    try:
        n = float(s.replace("$", "").replace(",", "").replace("%", "").strip())
    except ValueError:
        return None
    return n / 100 if (is_pct and pct_as_fraction) else n


TEAM_KEYS = ["pos_gmv", "pos_gpa", "gp_pct", "pickup_gpa", "tm_cost", "office_cost", "disposal_cost",
             "rental_cost", "operating_cost", "wh_log_cost", "capex", "mkt_cost", "dop", "proj_dop"]


def parse_team(values, name_key):
    as_of = values[1][1] if len(values) > 1 and len(values[1]) > 1 else ""
    date_row = values[1] if len(values) > 1 else []
    dates = {k: (date_row[i + 1] if i + 1 < len(date_row) else "") for i, k in enumerate(TEAM_KEYS)}
    rows = []
    for r in values[2:]:
        if not r or not str(r[0]).strip():
            continue
        row = {name_key: r[0]}
        for i, k in enumerate(TEAM_KEYS):
            row[k] = num(r[i + 1]) if i + 1 < len(r) else None
        rows.append(row)
    return rows, as_of, dates


SHOP_COLS = ["shop_code", "shop_name", "am", "dm", "pos_gpa", "pos_gp_pct", "pickup_gpa", "tm_cost",
             "disposal_cost", "other_mkt_cost", "dop", "proj_dop"]


def parse_shops(values):
    rows = []
    for r in values[1:]:
        if len(r) < 3 or not r[0] or not r[2]:  # skip TOTAL / note lines (no AM)
            continue
        r = r + [""] * (12 - len(r))
        row = {"shop_code": r[0], "shop_name": r[1], "am": r[2], "dm": r[3]}
        for i, k in enumerate(SHOP_COLS[4:], start=4):
            row[k] = num(r[i])
        rows.append(row)
    return rows


TM_MAP = {"shop": 0, "shop_name": 1, "ss_n": 5, "ss_cost": 6, "assp_n": 7, "assp_cost": 8, "sa_n": 9,
          "sa_cost": 10, "pt_hours": 11, "pt_cost": 13, "ot_hours": 14, "ot_cost": 15, "os_hours": 16,
          "os_cost": 17, "transport": 18, "mgmt_overhead": 20, "ss_sa_with_oh": 21, "pt_levy": 27,
          "total_cost": 28, "pt_cost_budget": 29, "pt_levy_budget": 30, "roster_total_cost": 31}

# Which "TM Cost by Shop" tab to read per month (Vivian, 29/10/2026: use the V1 tabs — actual FT accrues daily). Months not listed: try "<…> V1" first, then the classic tab.
TM_TABS = {"202610": ("TM Cost by shop 202610 V1", "v1"), "202611": ("TM Cost by shop 202611 V1", "v1")}
TM_RANGE = {"classic": "A2:AF200", "v1": "A4:AX200"}


def tm_tab_candidates(d):
    key = f"{d:%Y%m}"
    if key in TM_TABS:
        return [TM_TABS[key]]
    return [(f"TM Cost by shop {key} V1", "v1"), (f"TM Cost by Shop {key}", "classic")]


def _groups(mgmt, ss, sa, pt, os_, ot, tr, total=None):
    g = {"mgmt": mgmt or 0, "ss": ss or 0, "sa": sa or 0, "pt": pt or 0, "os": os_ or 0, "ot": ot or 0, "tr": tr or 0}
    g["total"] = total if total is not None else sum(g.values())
    return g


def _classic_groups(row):
    """Classic tab: U = DM+AM+UM, V = SS+ASS+SA (with OH) split by basic cost; projection swaps PT for the roster PT (AD + AE)."""
    ssb = (row["ss_cost"] or 0) + (row["assp_cost"] or 0)
    sab = row["sa_cost"] or 0
    v = row["ss_sa_with_oh"] or 0
    ss = v * ssb / (ssb + sab) if ssb + sab else 0
    sa = v * sab / (ssb + sab) if ssb + sab else 0
    act = _groups(row["mgmt_overhead"], ss, sa, (row["pt_cost"] or 0) + (row["pt_levy"] or 0), row["os_cost"], row["ot_cost"], row["transport"])
    proj = _groups(row["mgmt_overhead"], ss, sa, (row["pt_cost_budget"] or 0) + (row["pt_levy_budget"] or 0), row["os_cost"], row["ot_cost"], row["transport"])
    return act, proj


def parse_tm(values):
    rows = []
    for r in values[1:]:
        if not r or not str(r[0]).strip():
            continue
        if str(r[0]).strip() == "合計":
            break
        if len(r) < 3 or not r[2]:
            continue
        r = r + [""] * (32 - len(r))
        row = {k: (r[i] if k in ("shop", "shop_name") else num(r[i])) for k, i in TM_MAP.items()}
        row["act"], row["proj"] = _classic_groups(row)
        rows.append(row)
    return rows


def parse_tm_v1(values):
    """'TM Cost by shop YYYYMM V1': header row then shops; left block = actual to date (ends "實數小計"),
    right block = full-month projection (ends "Projection 合計"). Columns found by header name."""
    if not values:
        return [], False
    head = [str(h).split("\n")[0].strip() for h in values[0]]
    cut = next(i for i, h in enumerate(head) if h.startswith("實數小計"))
    end = next((i for i, h in enumerate(head) if h.startswith("Projection 合計")), len(head) - 1)

    def find(prefixes, lo, hi):
        return [i for i in range(lo, hi + 1) for p in prefixes if head[i].startswith(p)]

    um_split = bool(find(["UM/SS/ASS 成本"], 0, cut))

    def block(r, lo, hi):
        v = lambda ps: sum(num(r[i]) or 0 for i in find(ps, lo, hi) if i < len(r))
        return _groups(v(["AM+DM 成本", "DM+AM+UM 成本"]), v(["UM/SS/ASS 成本", "SS 成本", "AS 成本"]), v(["SA 成本"]),
                       v(["PT 成本", "PT 7% 加成", "468PT $1,000"]), v(["外判成本"]), v(["OT 成本"]), v(["交通津貼"]),
                       v(["實數小計", "Projection 合計"]))

    rows = []
    for r in values[1:]:
        if not r or not str(r[0]).strip():
            continue
        code = str(r[0]).strip()
        if code in ("合計", "Total", "TOTAL", "Grand Total"):
            break
        r = list(r) + [""] * (len(head) - len(r))
        cnt = lambda ps: sum(num(r[i]) or 0 for i in find(ps, 0, cut))
        act, proj = block(r, 0, cut), block(r, cut + 1, end)
        rows.append({"shop": code, "shop_name": r[1], "ss_n": cnt(["SS/AS 人數", "SS 人數"]), "assp_n": cnt(["AS 人數"]),
                     "sa_n": cnt(["SA 人數"]), "um_n": cnt(["UM 人數"]), "pt_hours": cnt(["PT 時數"]), "os_hours": cnt(["外判時數"]),
                     "total_cost": act["total"], "roster_total_cost": proj["total"], "act": act, "proj": proj})
    return rows, um_split


# Layout written by the daily Tableau refresh: "Shop Monthly Target" view (NOT the DOP version)
POS_SHOP_COLS = ["dm", "am", "shop_code", "shop_name", "target", "takeaway_gmv", "delivery_gmv", "liteapp_gmv",
                 "pos_gmv", "current_ach", "proj_gmv", "proj_ach"]
POS_AM_COLS = ["am", "target", "pos_gmv", "current_ach", "proj_ach"]


def parse_pos(values, cols, text_cols, total_label="Grand Total"):
    rows, total = [], None
    for r in values[1:]:
        if not r or not str(r[0]).strip():
            continue
        r = r + [""] * (len(cols) - len(r))
        row = {c: (str(r[i]).strip() if c in text_cols else num(r[i])) for i, c in enumerate(cols)}
        if str(r[0]).strip() == total_label:
            total = row
        else:
            rows.append(row)
    return rows, total


def fetch_pos(get):
    """POS GMV vs target, copied from Tableau into the POS sheet (optional)."""
    try:
        shops, total = parse_pos(get(POS_SHEET, "'POS by shop'!A1:L200"), POS_SHOP_COLS, {"dm", "am", "shop_code", "shop_name"})
        am, am_total = parse_pos(get(POS_SHEET, "'AM achievement'!A1:E30"), POS_AM_COLS, {"am"})
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in get(POS_SHEET, "'Info'!A1:B10") if r}
        return {"shops": shops, "total": total or am_total, "am": am, "period": info.get("Period", ""), "exported_at": info.get("Exported at", ""), "tableau_updated": info.get("Tableau last update", "")}
    except Exception as e:
        print(f"POS target sheet not read: {e}", file=sys.stderr)
        return None


MD_COLS = ["shop_code", "shop_name", "gmv", "net_sale", "md", "promo", "md_rate", "qty"]


def fetch_md(get):
    """Manual Discount by shop, copied from Tableau into the POS sheet's 'Manual Discount' tab (optional)."""
    try:
        shops, _ = parse_pos(get(POS_SHEET, "'Manual Discount'!A1:H200"), MD_COLS, {"shop_code", "shop_name"})
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in get(POS_SHEET, "'Info'!A1:B12") if r}
        return {"shops": [s for s in shops if s.get("shop_code")], "period": info.get("MD period", ""),
                "exported_at": info.get("MD exported at", ""), "tableau_updated": info.get("MD Tableau last update", "")}
    except Exception as e:
        print(f"Manual Discount tab not read: {e}", file=sys.stderr)
        return None


def parse_dop_target(values):
    """'Oct Target' tab: two blocks (AM, DM) of name / Target / Proj. DOP / Project Achievement / Difference."""
    out = {"am": [], "dm": [], "note": ""}
    block = None
    for r in values:
        if not r:
            continue
        r = list(r) + [""] * (8 - len(r))
        first = str(r[0]).strip()
        if "Target for AM" in first:
            block = "am"; continue
        if "Target for DM" in first:
            block = "dm"; continue
        if first == "Grand Total":
            if block:
                out[block + "_total"] = num(r[1])
            block = None; continue
        if len(r) > 3 and "actual" in str(r[3]) and not out["note"]:
            out["note"] = str(r[3]).strip()
        if block and first:
            target, proj = num(r[1]), num(r[3])
            diff = num(r[7])
            if diff is None and target is not None and proj is not None:
                diff = proj - target  # the sheet dropped its Difference column; work it out
            out[block].append({"name": first, "target": target, "proj_dop": proj, "achievement": num(r[5]), "difference": diff})
    return out


def parse_tm_daily(values):
    """'每日 PT 外判 YYYYMM' rows 2-4: day labels (d/m), per-day headers, TOTAL row. Single-day figures, not cumulative."""
    if len(values) < 3:
        return []
    labels, heads, total = values[0], values[1], values[2]
    want = {"PT 時數": "pt_hours", "PT 成本": "pt_cost", "PT 7% 加成": "pt_levy", "外判時數": "os_hours",
            "外判成本": "os_cost", "總成本": "total", "預算總成本": "budget_total"}
    starts = [i for i, v in enumerate(labels) if re.fullmatch(r"\d{1,2}/\d{1,2}", str(v).strip())]
    days = []
    for n, st in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else st + 13
        row = {"date": str(labels[st]).strip()}
        for i in range(st, min(end, len(heads))):
            key = want.get(str(heads[i]).strip())
            if key and key not in row:
                row[key] = num(total[i]) if i < len(total) else None
        if row.get("total") is not None:
            days.append(row)
    return days


def parse_tm_by_day(values):
    """DOP bot 'Raw - <Mon YYYY>': one row per shop per day; column K = TM Cost (Frontline), the shop's full daily TM cost.
    Returns {date: {shop_code: cost}}."""
    out = {}
    for r in values:
        if len(r) < 11 or not str(r[0]).strip() or not str(r[1]).strip():
            continue
        v = num(r[10])
        if v is None:
            continue
        out.setdefault(str(r[0]).strip(), {})[str(r[1]).strip()] = v
    return dict(sorted(out.items()))


def parse_commission(values):
    """'<Mon> Target %' tab: row 1 = first names (Dick, Jeri, ...), then one row per commission level
    (DOP threshold per person, last column = commission %, e.g. 120% ... 50%). Rows with '<' / '>' (the 0% line) are skipped."""
    if len(values) < 3:
        return {}
    names = [str(x).strip() for x in values[0] if str(x).strip()]
    levels = []
    for r in values[2:]:
        if not r or len(r) <= len(names):
            continue
        pct = num(r[len(names)])
        if pct is None or pct <= 0 or any(("<" in str(c) or ">" in str(c)) for c in r[:len(names)]):
            continue
        levels.append({"pct": pct, "dop": [num(c) for c in r[:len(names)]]})
    out = {}
    for i, n in enumerate(names):
        out[n] = sorted(({"pct": l["pct"], "dop": l["dop"][i]} for l in levels if i < len(l["dop"]) and l["dop"][i] is not None), key=lambda x: -x["pct"])
    return out


def cell_text(values):
    return str(values[0][0]) if values and values[0] else ""


def tm_stamps(raw):
    """Update stamps written in the TM cost sheet's own headers."""
    head, daily = cell_text(raw.get("tm_head")), cell_text(raw.get("tm_daily_head"))
    v = re.search(r"版本[：:]\s*(\d{4}-\d{2}-\d{2})", head) or re.search(r"截至\s*(\d{1,2}/\d{1,2})", head)
    g = re.search(r"產生[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})", daily)
    return {"tm_version": v.group(1) if v else "", "tm_daily_generated": g.group(1) if g else ""}


def fetch_tiers(get):
    """Shop code -> tier label (optional)."""
    try:
        rows = get(TIER_SHEET, "Sheet1!A1:C300")
        return {str(r[0]).strip(): str(r[2]).strip() for r in rows[1:] if len(r) > 2 and str(r[0]).strip() and str(r[2]).strip()}
    except Exception as e:
        print(f"Tier sheet not read: {e}", file=sys.stderr)
        return {}


# ---------- fetching ----------
def tabs_for(d):
    mon = d.strftime("%b %Y")  # e.g. "Oct 2026"
    return {
        "dm": (DOP_SHEET, f"'Summary - {mon}'!A1:O5"),
        "am": (DOP_SHEET, f"'Summary - {mon}'!A8:O15"),
        "dm_ex": (DOP_SHEET, f"'Summary - {mon}'!A18:O22"),
        "am_ex": (DOP_SHEET, f"'Summary - {mon}'!A25:O32"),
        "shops": (DOP_SHEET, f"'DOP MTD - {mon}'!A1:L150"),
        "dop_target": (DOP_SHEET, f"'{d:%b} Target'!A1:H40"),
        "commission": (DOP_SHEET, f"'{d:%b} Target %'!A1:K20"),
        "tm_daily_head": (TM_SHEET, f"'每日 PT 外判 {d:%Y%m}'!A1"),
        "tm_daily": (TM_SHEET, f"'每日 PT 外判 {d:%Y%m}'!A2:ZZ4"),
        "raw": (DOP_SHEET, f"'Raw - {mon}'!A2:K4000"),
        "_month": d.strftime("%B %Y"),
    }


def make_getter():
    mock = os.environ.get("MOCK_VALUES")
    if mock:
        data = json.loads(Path(mock).read_text(encoding="utf-8"))

        def get(sheet_id, rng):
            if rng not in data:
                raise KeyError(rng)
            return data[rng]
        return get

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(os.environ["GOOGLE_SA_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    def get(sheet_id, rng):
        return svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute().get("values", [])
    return get


def fetch_month(get, d):
    t = tabs_for(d)
    optional = ("dop_target", "tm_daily", "raw", "commission", "tm_daily_head")
    out = {k: get(*v) for k, v in t.items() if not k.startswith("_") and k not in optional}
    for k in optional:
        try:
            out[k] = get(*t[k])
        except Exception as e:
            print(f"{k} not read: {e}", file=sys.stderr)
            out[k] = []
    last = None
    for tab, kind in tm_tab_candidates(d):
        try:
            out["tm"] = get(TM_SHEET, f"'{tab}'!{TM_RANGE[kind]}")
            out["_tm_kind"], out["_tm_tab"] = kind, tab
            break
        except Exception as e:
            last = e
    else:
        raise last
    try:
        out["tm_head"] = get(TM_SHEET, f"'{out['_tm_tab']}'!A1")
    except Exception:
        out["tm_head"] = []
    out["_month"] = t["_month"]
    out["_date"] = d
    return out


def fetch_budget(get, d):
    """Monthly frontline TM budget from the 'TM Cost Budget ' tab (optional)."""
    try:
        v = get(TM_SHEET, "'TM Cost Budget '!A1:M3")
        head, budget = v[0], v[2]
        for i, h in enumerate(head):
            if i and str(h).strip().startswith(d.strftime("%b")):
                return num(budget[i]) if i < len(budget) else None
    except Exception as e:
        print(f"Budget not read: {e}", file=sys.stderr)
    return None


def fetch_all():
    get = make_getter()
    today = dt.datetime.now(HKT).date()
    try:
        raw = fetch_month(get, today)
    except Exception as e:  # new month's tabs not created yet -> use last month
        print(f"This month's tabs not ready ({e}); falling back to last month", file=sys.stderr)
        raw = fetch_month(get, today.replace(day=1) - dt.timedelta(days=1))
    dm, as_of, dates = parse_team(raw["dm"], "dm")
    am, _, _ = parse_team(raw["am"], "am")
    dm_ex, _, _ = parse_team(raw["dm_ex"], "dm")
    am_ex, _, _ = parse_team(raw["am_ex"], "am")
    shops = parse_shops(raw["shops"])
    if raw["_tm_kind"] == "v1":
        tm, um_split = parse_tm_v1(raw["tm"])
    else:
        tm, um_split = parse_tm(raw["tm"]), False
    if not dm or not shops:
        raise SystemExit("Sheet returned no rows - check the tab names and service-account access")
    return {
        "month": raw["_month"],
        "as_of": as_of,
        "dates": dates,
        "tm_budget": fetch_budget(get, raw["_date"]),
        "dm_ex": dm_ex, "am_ex": am_ex,
        "pos": fetch_pos(get),
        "md": fetch_md(get),
        "tiers": fetch_tiers(get),
        "tm_daily": parse_tm_daily(raw.get("tm_daily") or []),
        "tm_by_day": parse_tm_by_day(raw.get("raw") or []),
        "dop_target": parse_dop_target(raw.get("dop_target") or []),
        "commission": parse_commission(raw.get("commission") or []),
        "stamps": tm_stamps(raw),
        "tm_tab": raw["_tm_tab"], "tm_um_split": um_split,
        "generated_at": dt.datetime.now(HKT).strftime("%Y-%m-%d %H:%M HKT"),
        "dm": dm, "am": am, "shops": shops, "tm": tm,
    }


# ---------- encryption (AES-256-GCM, key from PBKDF2-SHA256) ----------
def encrypt(payload: dict, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload, ensure_ascii=False).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def main():
    password = os.environ.get("DASH_PASSWORD", "")
    if len(password) < 10:
        raise SystemExit("DASH_PASSWORD secret missing or shorter than 10 characters")
    data = fetch_all()
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    (site / "data.enc.json").write_text(json.dumps(encrypt(data, password)), encoding="utf-8")
    shutil.copy(ROOT / "index.html", site / "index.html")
    shutil.copy(ROOT / "d3.min.js", site / "d3.min.js")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    print(f"OK: {data['month']}, as of {data['as_of']}, {len(data['shops'])} shops, {len(data['tm'])} TM rows")


if __name__ == "__main__":
    main()
