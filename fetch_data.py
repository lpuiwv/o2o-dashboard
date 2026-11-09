"""Pull DOP + TM cost data from Google Sheets, encrypt it, and write site/ for GitHub Pages.

Env vars (set as GitHub Actions secrets):
  GOOGLE_SA_JSON  - full JSON key of a Google service account that has Viewer access to both sheets
  DASH_PASSWORD   - password people type to open the dashboard
Optional:
  MOCK_VALUES     - path to a JSON file of {range: values} used instead of Google (for local testing)
"""
import base64
import calendar
import re
import datetime as dt
import json
import os
import shutil
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

DOP_SHEET = "1AiI-an7F-dtpZ_BoTnrQY65Kokha8c3csloRSrSueuQ"  # Daily Shop PNL (DOP bot)
TM_SHEET = "1AnaHxF1yKUWAAVqy77PkYb4fWdVxcSdZ1bwdByHmNJk"   # All Shop Daily TM Cost
TIER_SHEET = "1YFEdwrN9E8LQlB63hWZU6kWg_99wUjmxP_kUsKmZQvg"  # Tier of shop (by target): Code / Shop Name / Tier
TARGET_SHEET = "1NjGFRTCmTU_0-9vvoIXFYVSVocJ4_0Fyhjbe-8qWNA8"  # O2O Shop Target 2026 Final_For PED OPS: "<Mon> TM cost target" (Vivian, 2/11/2026)
POS_SHEET = "1AZBZXU_dbRvTdUaE-J0WVcM62ZFkMTMOhsARdSNiyec"  # O2O POS Target (Tableau) - copy of Tableau crosstabs
PED_SHEET = "13N3VxU3CzrE8vs_rFSkEC41yAQj8ZdbhVOTZKrt4x9o"  # PED Disposal+Discount+GP (Disposal bot): 'Overall Summary' shop x SKU MTD (Vivian, 4/11/2026)
# Past months in the dashboard's month menu (Vivian, 9/11/2026): each is built into site/data_YYYYMM.enc.json. Their Tableau
# data is a one-off copy in the POS sheet tabs "<tab> (Mon YYYY)" + "Info (Mon YYYY)" (Oct 2026: written 9/11/2026, POS / Pickup
# target from Vivian's 'Target_Shop Version' sheet because Tableau returns the current month's target for any date range).
# The PED sheet's 'Overall Summary' / 'By Shop' only hold the current month, so past months have no Disposal / Fruit / UM data.
PAST_MONTHS = ["2026-10"]
POS_SFX = ""  # "" = current-month POS sheet tabs; e.g. " (Oct 2026)" while a past month is built


def pos_tab(name):
    return f"'{name}{POS_SFX}'"


def info_rows(get):
    return get(POS_SHEET, f"{pos_tab('Info')}!A1:B40")


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


# Summary header -> key (Vivian's sheet added columns over time, e.g. "TM Cost (Frontline) (Matthew Chu)" in Oct,
# "TM Full Month (Plan)" in Nov), so columns are matched by header name, not position.
def team_key(h):
    h = str(h).split("\n")[0].strip().lower()
    if h == "tm cost (frontline)":
        return "tm_cost"
    if h.startswith("tm full month") or "matthew" in h or h.startswith("tm cost (frontline) ("):
        return "tm_plan"
    for pre, k in (("pos gmv", "pos_gmv"), ("pos gpa", "pos_gpa"), ("gp%", "gp_pct"), ("pickup gpa", "pickup_gpa"),
                   ("office cost", "office_cost"), ("disposal cost", "disposal_cost"), ("rental cost", "rental_cost"),
                   ("operating cost", "operating_cost"), ("wh & log", "wh_log_cost"), ("capex", "capex"),
                   ("mkt cost", "mkt_cost"), ("proj. dop", "proj_dop"), ("dop", "dop")):
        if h.startswith(pre):
            return k
    return None


def parse_team(values, name_key):
    head = values[0] if values else []
    cols = {team_key(h): i for i, h in enumerate(head) if i and team_key(h)}
    if not {"pos_gmv", "tm_cost", "dop"} <= set(cols):  # no usable header: old fixed layout
        cols = {k: i + 1 for i, k in enumerate(TEAM_KEYS)}
    as_of = values[1][1] if len(values) > 1 and len(values[1]) > 1 else ""
    date_row = values[1] if len(values) > 1 else []
    dates = {k: (date_row[i] if i < len(date_row) else "") for k, i in cols.items()}
    rows = []
    for r in values[2:]:
        if not r or not str(r[0]).strip():
            continue
        row = {name_key: r[0]}
        for k in TEAM_KEYS + ["tm_plan"]:
            i = cols.get(k)
            row[k] = num(r[i]) if i is not None and i < len(r) else None
        rows.append(row)
    return rows, as_of, dates


SHOP_COLS = ["shop_code", "shop_name", "am", "dm", "pos_gpa", "pos_gp_pct", "pickup_gpa", "tm_cost",
             "disposal_cost", "other_mkt_cost", "dop", "proj_dop"]


def shop_key(h):
    h = str(h).split("\n")[0].strip().lower()
    for pre, k in (("shop code", "shop_code"), ("shop name", "shop_name"), ("am", "am"), ("dm", "dm"), ("pos gpa", "pos_gpa"),
                   ("pos gp%", "pos_gp_pct"), ("pickup gpa", "pickup_gpa"), ("planned", "tm_plan"), ("frontline tm", "tm_cost"),
                   ("tm cost", "tm_cost"), ("disposal", "disposal_cost"), ("other", "other_mkt_cost"), ("proj. dop", "proj_dop"),
                   ("dop", "dop")):
        if h == pre or (len(pre) > 3 and h.startswith(pre)):
            return k
    return None


PROJ_SHOP_KEYS = (("pos gpa", "proj_pos_gpa"), ("pickup gpa", "proj_pickup_gpa"), ("proj. pickup gmv", "proj_pickup_gmv"),
                  ("proj. pos gmv", "proj_pos_gmv"), ("planned", "tm_plan"), ("disposal", "proj_disposal_cost"),
                  ("other", "proj_other_mkt_cost"), ("proj. dop", "proj_dop"), ("open day", "open_day"),
                  ("pos as-of", "pos_asof"), ("pickup as-of", "pickup_asof"))


def parse_shops(values):
    """'DOP MTD - <Mon YYYY>'. From Nov 2026 the tab has two header rows: a group row ("To date" / "Projected (with
    weekday/weekend factor)") and the column names, where the projected block repeats names (POS GPA, Pickup GPA,
    Disposal…). Columns from the "Projected" group on are mapped to proj_* keys (Vivian, 4/11/2026: Proj. DOP was being
    read from the projected POS GPA column)."""
    hi = next((i for i, r in enumerate(values[:5]) if any(str(c).strip().lower() == "shop code" for c in r)), 0)
    head = values[hi] if values else []
    group = values[hi - 1] if hi > 0 else []
    proj_from = next((i for i, c in enumerate(group) if str(c).strip().lower().startswith("projected")), None)
    cols = {}
    for i, h in enumerate(head):
        if proj_from is not None and i >= proj_from:
            hh = str(h).split("\n")[0].strip().lower()
            k = next((key for pre, key in PROJ_SHOP_KEYS if hh.startswith(pre)), None)
        else:
            k = shop_key(h)
        if k and k not in cols:
            cols[k] = i
    if not {"shop_code", "am", "dop"} <= set(cols):  # old fixed layout
        cols = {k: i for i, k in enumerate(SHOP_COLS)}
    extra = [k for k, _ in ((v, None) for _, v in PROJ_SHOP_KEYS) if k in cols and k not in ("proj_dop", "tm_plan")]
    rows = []
    for r in values[hi + 1:]:
        g = lambda k: r[cols[k]] if k in cols and cols[k] < len(r) else ""
        if not g("shop_code") or not g("am") or str(g("shop_code")).strip().lower() == "shop code":  # skip TOTAL / note / header lines
            continue
        row = {"shop_code": g("shop_code"), "shop_name": g("shop_name"), "am": g("am"), "dm": g("dm")}
        for k in SHOP_COLS[4:] + ["tm_plan"] + extra:
            row[k] = num(g(k))
        rows.append(row)
    return rows


EXCL_SHOPS = ("SWH", "CHE", "EPR")  # outlets + EPR, as in the sheet's 'Excl. Outlet & EPR' blocks
TEAM_SUM_KEYS = ("pos_gpa", "pickup_gpa", "tm_cost", "disposal_cost", "other_mkt_cost", "dop", "proj_pos_gmv", "proj_pos_gpa",
                 "proj_pickup_gpa", "proj_pickup_gmv", "tm_plan", "proj_disposal_cost", "proj_other_mkt_cost", "proj_dop")


def team_from_shops(shops, key, excl=False):
    """DM / AM rows summed from the 'DOP MTD' shop rows (same keys as the Summary rows, plus the projected block).
    POS GMV (MTD) is not in the tab: worked out per shop as POS GPA / POS GP% (approximate - GP% is rounded to 0.1%)."""
    use = [s for s in shops if not (excl and s["shop_code"] in EXCL_SHOPS)]
    out = []
    for name in sorted({s.get(key) for s in use if s.get(key)}):
        mine = [s for s in use if s.get(key) == name]
        r = {key: name}
        for k in TEAM_SUM_KEYS:
            vals = [s.get(k) for s in mine if s.get(k) is not None]
            r[k] = round(sum(vals), 2) if vals else None
        r["pos_gmv"] = round(sum((s["pos_gpa"] or 0) / s["pos_gp_pct"] for s in mine if s.get("pos_gp_pct")), 2)
        r["gp_pct"] = r["pos_gpa"] / r["pos_gmv"] if r["pos_gmv"] and r["pos_gpa"] is not None else None
        r["mkt_cost"] = r.pop("other_mkt_cost")  # Other & MKT costs (office, rental, operating, WH & log, capex, MKT)
        for k in ("office_cost", "rental_cost", "operating_cost", "wh_log_cost", "capex"):
            r[k] = None
        out.append(r)
    return out


def shops_as_of(shops, d):
    """'POS as-of' / 'Pickup as-of' columns hold the day of month the to-date block runs to."""
    day = lambda k: max((int(s[k]) for s in shops if s.get(k)), default=None)
    fmt = lambda n: f"{n:02d}/{d:%m/%Y}" if n else ""
    pos, pick = fmt(day("pos_asof")), fmt(day("pickup_asof"))
    pos = pos or pick
    return pos, {k: pos for k in ("pos_gmv", "pos_gpa", "gp_pct", "tm_cost", "disposal_cost", "mkt_cost", "dop")} | {"pickup_gpa": pick or pos}


def shops_adjusted(values):
    """'PROJECTED after Rebate, Claim supp, AST' line under the shop rows: company-wide Proj. DOP after those items."""
    for r in values or []:
        if r and str(r[0]).strip().upper().startswith("PROJECTED AFTER"):
            v = next((num(c) for c in reversed(r) if num(c) is not None), None)
            return {"label": str(r[0]).strip(), "proj_dop": v}
    return None


def fill_team_from_shops(team, shops, key, excl=False):
    """Fallback when the Summary tab has no Proj. DOP / planned TM column (the plain Nov 2026 tab; the "(update)" tab has
    both): use the sum of the shop rows (Vivian, 4/11/2026)."""
    use = [s for s in shops if not (excl and s["shop_code"] in EXCL_SHOPS)]
    for r in team:
        mine = [s for s in use if s.get(key) == r.get(key)]
        if not mine:
            continue
        if r.get("proj_dop") is None and any(s.get("proj_dop") is not None for s in mine):
            r["proj_dop"] = round(sum(s.get("proj_dop") or 0 for s in mine), 2)
        if r.get("tm_plan") is None and any(s.get("tm_plan") is not None for s in mine):
            r["tm_plan"] = round(sum(s.get("tm_plan") or 0 for s in mine), 2)


TM_MAP = {"shop": 0, "shop_name": 1, "ss_n": 5, "ss_cost": 6, "assp_n": 7, "assp_cost": 8, "sa_n": 9,
          "sa_cost": 10, "pt_hours": 11, "pt_cost": 13, "ot_hours": 14, "ot_cost": 15, "os_hours": 16,
          "os_cost": 17, "transport": 18, "mgmt_overhead": 20, "ss_sa_with_oh": 21, "pt_levy": 27,
          "total_cost": 28, "pt_cost_budget": 29, "pt_levy_budget": 30, "roster_total_cost": 31, "pt_rate": 12}

# From this month on, DM + AM cost is left out of TM cost (it sits in back-office cost) - Vivian, 29/10/2026
DMAM_EXCLUDED_FROM = "202611"

# Which "TM Cost by Shop" tab to read per month (Vivian, 29/10/2026: Oct = classic tab, Nov = V1). Months not listed: try "<…> V1" first, then the classic tab.
TM_TABS = {"202610": ("TM Cost by Shop 202610", "classic"), "202611": ("TM Cost by shop 202611 V1", "v1")}
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


def _classic_groups(row, ft_ratio=1.0):
    """Classic tab: U = DM+AM+UM, V = SS+ASS+SA (with OH) split by basic cost; projection swaps PT for the roster PT (AD + AE).
    The tab holds FULL-month FT pay, so actual FT = full month x ft_ratio (days so far / days in month); projection keeps the full month."""
    ssb = (row["ss_cost"] or 0) + (row["assp_cost"] or 0)
    sab = row["sa_cost"] or 0
    v = row["ss_sa_with_oh"] or 0
    ss = v * ssb / (ssb + sab) if ssb + sab else 0
    sa = v * sab / (ssb + sab) if ssb + sab else 0
    act = _groups((row["mgmt_overhead"] or 0) * ft_ratio, ss * ft_ratio, sa * ft_ratio, (row["pt_cost"] or 0) + (row["pt_levy"] or 0), row["os_cost"], row["ot_cost"], row["transport"])
    proj = _groups(row["mgmt_overhead"], ss, sa, (row["pt_cost_budget"] or 0) + (row["pt_levy_budget"] or 0), row["os_cost"], row["ot_cost"], row["transport"])
    return act, proj


def parse_tm(values, ft_ratio=1.0):
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
        row["act"], row["proj"] = _classic_groups(row, ft_ratio)
        row["total_cost"] = row["act"]["total"]
        row["am"] = str(r[2]).strip()
        rate = row["pt_rate"] or 0
        row["proj_n"] = {"pt_hours": (row["pt_cost_budget"] or 0) / rate if rate else None, "os_hours": row["os_hours"],
                         "ot_hours": row["ot_hours"], "ss_n": (row["ss_n"] or 0) + (row["assp_n"] or 0), "sa_n": row["sa_n"]}
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
    # "<Mon> TM Cost (excl. DMAM)" / "<Mon> TM Target (excl. DMAM)" (renamed 3/11/2026) and the matching "… DEF …" column
    is_tgt = lambda h: "excl" in h and "DMAM" in h and ("TM Cost" in h or "TM Target" in h)
    tgt_i = [i for i, h in enumerate(head) if is_tgt(h) and "DEF" not in h]
    def_i = [i for i, h in enumerate(head) if is_tgt(h) and "DEF" in h]

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
        cnt = lambda ps, lo=0, hi=cut: sum(num(r[i]) or 0 for i in find(ps, lo, hi))
        act, proj = block(r, 0, cut), block(r, cut + 1, end)
        proj_n = {"pt_hours": cnt(["PT 時數"], cut + 1, end), "os_hours": cnt(["外判時數"], cut + 1, end),
                  "ot_hours": cnt(["OT 時數"], cut + 1, end), "ss_n": cnt(["SS/AS 人數", "SS 人數", "AS 人數"], cut + 1, end),
                  "sa_n": cnt(["SA 人數"], cut + 1, end), "um_n": cnt(["UM 人數"], cut + 1, end)}
        rows.append({"shop": code, "shop_name": r[1], "ss_n": cnt(["SS/AS 人數", "SS 人數"]), "assp_n": cnt(["AS 人數"]),
                     "sa_n": cnt(["SA 人數"]), "um_n": cnt(["UM 人數"]), "pt_hours": cnt(["PT 時數"]), "os_hours": cnt(["外判時數"]),
                     "total_cost": act["total"], "roster_total_cost": proj["total"], "act": act, "proj": proj,
                     "am": str(r[2]).strip(), "proj_n": proj_n,
                     # "<Mon> TM Cost (excl. DMAM)" = target TM cost; "… DEF …" = target − projection (Vivian, 2/11/2026)
                     "target_tm": next((num(r[i]) for i in tgt_i), None), "target_def": next((num(r[i]) for i in def_i), None)})
    return rows, um_split


# Layout written by the daily Tableau refresh: "Shop Monthly Target" view (NOT the DOP version)
POS_SHOP_COLS = ["dm", "am", "shop_code", "shop_name", "target", "takeaway_gmv", "delivery_gmv", "liteapp_gmv",
                 "pos_gmv", "current_ach", "proj_gmv", "proj_ach"]
POS_AM_COLS = ["am", "target", "pos_gmv", "current_ach", "proj_ach"]
# From Nov 2026 Tableau changed the POS GMV formula (Vivian, 3/11/2026): webcart no longer counted x2; Total POS GMV =
# Takeaway (incl. MKT fund & RM discount) + POS Delivery GMV + Frozen SKU GMV (x1.5) + Redemption (x1.2/x1.5).
POS_SHOP_COLS_NOV = ["dm", "am", "shop_code", "shop_name", "target", "takeaway_gmv", "delivery_gmv", "frozen_gmv",
                     "redemption_gmv", "pos_gmv", "current_ach", "proj_gmv", "proj_ach"]


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
        raw = get(POS_SHEET, f"{pos_tab('POS by shop')}!A1:M200")
        layout = "nov" if raw and any("frozen" in str(h).lower() for h in raw[0]) else "old"
        shops, total = parse_pos(raw, POS_SHOP_COLS_NOV if layout == "nov" else POS_SHOP_COLS, {"dm", "am", "shop_code", "shop_name"})
        am, am_total = parse_pos(get(POS_SHEET, f"{pos_tab('AM achievement')}!A1:E30"), POS_AM_COLS, {"am"})
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
        return {"shops": shops, "total": total or am_total, "am": am, "layout": layout, "period": info.get("Period", ""), "exported_at": info.get("Exported at", ""), "tableau_updated": info.get("Tableau last update", "")}
    except Exception as e:
        print(f"POS target sheet not read: {e}", file=sys.stderr)
        return None


MD_COLS = ["shop_code", "shop_name", "gmv", "net_sale", "md", "promo", "md_rate", "qty"]


DISP_COLS = ["shop_code", "shop_name", "sku_gmv", "disp_qty", "dmg_qty", "miss_qty", "st_short_qty", "st_extra_qty",
             "disp_cost", "dmg_cost", "miss_cost", "st_cost", "total", "rate"]
DISP_TYPE_COLS = ["pos_type", "gmv", "qty", "disp_qty", "dmg_qty", "disp_cost", "dmg_cost", "total", "rate"]


def fetch_disposal(get):
    """Disposal by shop and by POS type, copied from Tableau ('Shop View (Disposal)' / 'Disposal') into the POS sheet
    by the daily scheduled task (Vivian, 2/11/2026). Optional."""
    try:
        shops, _ = parse_pos(get(POS_SHEET, f"{pos_tab('Disposal')}!A1:N200"), DISP_COLS, {"shop_code", "shop_name"})
        try:
            types, _ = parse_pos(get(POS_SHEET, f"{pos_tab('Disposal by type')}!A1:I40"), DISP_TYPE_COLS, {"pos_type"})
        except Exception:
            types = []
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
        shops = [x for x in shops if x.get("shop_code") and x["shop_code"] != "All"]
        if not shops:
            return None
        return {"shops": shops, "types": [t for t in types if t.get("pos_type") and t["pos_type"] != "All"],
                "period": info.get("Disposal period", ""), "exported_at": info.get("Disposal exported at", ""),
                "tableau_updated": info.get("Disposal Tableau last update", "")}
    except Exception as e:
        print(f"Disposal tab not read: {e}", file=sys.stderr)
        return None


PICKUP_COLS = ["dm", "am", "shop_code", "shop_name", "target", "non_senior", "senior_webcart", "senior_non_webcart",
               "pickup_gmv", "current_ach", "proj_gmv", "proj_ach"]


def fetch_pickup(get):
    """Shop pick-up GMV vs target, copied from Tableau "Shop Pick-up" into the POS sheet's 'Pickup by shop' tab by the
    daily scheduled task (Vivian, 4/11/2026). Optional."""
    try:
        rows, _ = parse_pos(get(POS_SHEET, f"{pos_tab('Pickup by shop')}!A1:L200"), PICKUP_COLS, {"dm", "am", "shop_code", "shop_name"})
        rows = [r for r in rows if r.get("shop_code") and r["shop_code"] != "All"]
        if not rows:
            return None
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
        return {"shops": rows, "period": info.get("Pickup period", ""), "exported_at": info.get("Pickup exported at", "")}
    except Exception as e:
        print(f"Pickup tab not read: {e}", file=sys.stderr)
        return None


def fetch_offline_tab(get):
    """POS offline from Tableau "POS Takeaway by Shop by SKU Sales" (Vivian, 6/11/2026): the daily Tableau task (09:52) sums the
    view into the POS sheet's 'POS offline by shop' (shop x pos_category_type) and 'POS offline by SKU' (SKU, all shops) tabs.
    The full shop x SKU list (~10k rows) stays in Tableau: the dashboard links to its CSV export. Optional."""
    try:
        sh = get(POS_SHEET, f"{pos_tab('POS offline by shop')}!A1:H1000")
        sk = get(POS_SHEET, f"{pos_tab('POS offline by SKU')}!A1:I1500")
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
    except Exception as e:
        print(f"POS offline tabs not read: {e}", file=sys.stderr)
        return None
    pad = lambda r, n: list(r) + [""] * (n - len(r))
    shops = []
    for r in (sh or [])[1:]:
        r = pad(r, 8)
        if str(r[2]).strip():
            shops.append({"dm": str(r[0]).strip(), "am": str(r[1]).strip(), "shop": str(r[2]).strip(), "shop_name": str(r[3]).strip(),
                          "cat": str(r[4]).strip(), "gmv": num(r[5]) or 0, "qty": num(r[6]) or 0, "refund": num(r[7]) or 0})
    skus = []
    for r in (sk or [])[1:]:
        r = pad(r, 9)
        if str(r[0]).strip():
            skus.append({"sku": str(r[0]).strip(), "name": str(r[1]).strip(), "cat": str(r[2]).strip(), "brand": str(r[3]).strip(),
                         "sub": str(r[4]).strip(), "gmv": num(r[5]) or 0, "qty": num(r[6]) or 0, "refund": num(r[7]) or 0, "shops": num(r[8]) or 0})
    if not shops:
        return None
    return {"shops": shops, "skus": skus, "period": info.get("POS offline period", ""), "exported_at": info.get("POS offline exported at", "")}


def fetch_offline_sku(get):
    """POS offline by shop x SKU from Tableau "POS Takeaway by Shop by SKU Sales", written daily by the 09:52 task into the POS
    sheet tab 'POS offline by shop by SKU' (A:M = DM, AM, Shop code, Shop name, POS category, Sub cat 2, Sub cat 3, Brand, SKU,
    SKU name, Total POS GMV, QTY, Refund value; Vivian, 9/11/2026: dashboard users have no Tableau login). Returned as a compact
    payload for its own encrypted file (site/offsku*.enc.json), downloaded as an Excel-friendly CSV from the POS offline tab."""
    try:
        vals = get(POS_SHEET, f"{pos_tab('POS offline by shop by SKU')}!A1:M40000")
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
    except Exception as e:
        print(f"POS offline by shop by SKU not read: {e}", file=sys.stderr)
        return None
    shops, skus, rows = {}, {}, []
    for r in (vals or [])[1:]:
        r = list(r) + [""] * (13 - len(r))
        shop, sku = str(r[2]).strip(), str(r[8]).strip()
        if not shop or not sku:
            continue
        shops.setdefault(shop, [str(r[3]).strip(), str(r[0]).strip(), str(r[1]).strip()])
        skus.setdefault(sku, [str(r[9]).strip(), str(r[4]).strip(), str(r[5]).strip(), str(r[6]).strip(), str(r[7]).strip()])
        rows.append([shop, sku, round(num(r[10]) or 0, 2), num(r[11]) or 0, round(num(r[12]) or 0, 2)])
    if not rows:
        return None
    return {"shops": shops, "skus": skus, "rows": rows, "period": info.get("POS offline by SKU period", ""),
            "exported_at": info.get("POS offline by SKU exported at", "")}


PED_SHOP_COLS = {"dm": "dm", "am": "am", "um": "um/sm", "shop_code": "shop code", "shop_name": "shop name",
                 "shop_cost": "shop disposal cost", "shop_rate": "shop disposal rate", "st_cost": "stocktake disposal cost",
                 "st_rate": "stocktake disposal rate", "dam_cost": "stock in dam + uom cost", "dam_rate": "stock in dam + uom rate",
                 "total": "total disposal cost", "rate": "dop (overall disposal) rate", "gmv": "gmv"}


def ped_disposal(get, cats):
    """Disposal tab from the PED sheet (Vivian, 4/11/2026): 'By Shop' (per shop shop-disposal / stocktake / stock-in-dam + UOM
    cost and rate, Total Disposal Cost, DOP (Overall Disposal) Rate, GMV), the target matrix 'Disposal Rate Target'!A1:E6
    (category x Overall / Stocktake / Shop / Stock in Damage + UOM) and per-shop category totals from 'Overall Summary'."""
    try:
        vals = get(PED_SHEET, "'By Shop'!A1:P200")
    except Exception as e:
        print(f"PED By Shop not read: {e}", file=sys.stderr)
        return None
    hi = next((i for i, r in enumerate(vals[:5]) if any(str(c).strip().lower() == "shop code" for c in r)), None)
    if hi is None:
        return None
    head = [str(h).strip().lower() for h in vals[hi]]
    idx = {k: head.index(n) for k, n in PED_SHOP_COLS.items() if n in head}
    text = {"dm", "am", "um", "shop_code", "shop_name"}
    shops = []
    for r in vals[hi + 1:]:
        g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else ""
        code = str(g("shop_code")).strip()
        if not code or code.lower() == "shop code" or not str(g("am")).strip():  # skip blank / repeated header rows
            continue
        shops.append({k: (str(g(k)).replace("\t", "").strip() if k in text else (abs(num(g(k)) or 0) if k.endswith("cost") or k == "total" else num(g(k))))
                      for k in PED_SHOP_COLS})
    targets = {}
    try:
        for r in get(PED_SHEET, "'Disposal Rate Target'!A1:E6")[1:]:
            if r and str(r[0]).strip():
                r = list(r) + [""] * 5
                targets[str(r[0]).strip()] = {"overall": num(r[1]), "stocktake": num(r[2]), "shop": num(r[3]), "dam": num(r[4])}
    except Exception as e:
        print(f"PED disposal targets not read: {e}", file=sys.stderr)
    return {"shops": shops, "targets": targets, "cats": cats} if shops else None


def fetch_ped(get):
    """'Overall Summary' of the PED Disposal+Discount+GP sheet (one row per shop x SKU, month to date; pipeline runs
    daily 12:45 with T-1 data). Returns (summary, sku_payload):
    summary = {fruit: {shop: {disc, gmv}}, um: {shop: UM}, as_of}; sku_payload = compact shop x SKU rows for the
    POS offline tab (written to a separate encrypted file, loaded only when that tab opens). Optional."""
    try:
        vals = get(PED_SHEET, "'Overall Summary'!A1:AF25000")
    except Exception as e:
        print(f"PED Overall Summary not read: {e}", file=sys.stderr)
        return None, None
    if not vals:
        return None, None
    head = [str(h).strip().lower() for h in vals[0]]
    col = lambda name: head.index(name) if name in head else None
    c = {k: col(n) for k, n in (("dm", "dm"), ("am", "am"), ("um", "um / sm"), ("shop", "shop code"), ("sku", "product code"),
                                ("name", "sku name"), ("cat", "pos cat"), ("gmv", "total pos gmv"), ("qty", "quantity"),
                                ("disc", "discount amount"), ("disp", "total disposal cost"), ("disp_qty", "total disposal qty"),
                                ("c_shop", "shop disposal cost"), ("c_st", "monthly stocktake cost"), ("c_dam", "stock in dam cost"),
                                ("c_uom", "uom missing cost"))}
    if c["shop"] is None or c["sku"] is None:
        return None, None
    g = lambda r, k: r[c[k]] if c[k] is not None and c[k] < len(r) else ""
    fruit, um, names, rows, cats = {}, {}, {}, [], {}
    for r in vals[1:]:
        shop, sku = str(g(r, "shop")).strip(), str(g(r, "sku")).strip()
        if not shop or not sku:
            continue
        cat = str(g(r, "cat")).strip()
        gmv, qty, disc = num(g(r, "gmv")) or 0, num(g(r, "qty")) or 0, num(g(r, "disc")) or 0
        disp, dq = abs(num(g(r, "disp")) or 0), abs(num(g(r, "disp_qty")) or 0)
        if str(g(r, "um")).strip():
            um.setdefault(shop, str(g(r, "um")).replace("\t", "").strip())
        if cat.lower().startswith("fruit"):
            f = fruit.setdefault(shop, {"disc": 0, "gmv": 0})
            f["disc"] += disc
            f["gmv"] += gmv
        # disposal by category per shop: [GMV, shop disposal, stocktake, stock-in-dam + UOM missing, total] (costs are negative in the sheet)
        cs = cats.setdefault(shop, {}).setdefault(cat or "Other", [0, 0, 0, 0, 0])
        for i, v in enumerate((gmv, abs(num(g(r, "c_shop")) or 0), abs(num(g(r, "c_st")) or 0),
                               abs(num(g(r, "c_dam")) or 0) + abs(num(g(r, "c_uom")) or 0), disp)):
            cs[i] += v
        names.setdefault(sku, str(g(r, "name")).strip())
        if gmv or qty or disc or disp or dq:
            rows.append([shop, sku, cat, round(gmv, 2), round(qty, 2), round(disc, 2), round(dq, 2), round(disp, 2)])
    as_of = ""
    try:
        t = get(PED_SHEET, "'Disposal Rate Target'!A10:A10")
        as_of = str(t[0][0]).strip() if t and t[0] else ""
    except Exception:
        pass
    teams = {}
    for r in vals[1:]:
        shop = str(g(r, "shop")).strip()
        if shop and shop not in teams:
            teams[shop] = [str(g(r, "dm")).strip(), str(g(r, "am")).strip()]
    for f in fruit.values():
        f["disc"], f["gmv"] = round(f["disc"], 2), round(f["gmv"], 2)
    for d in cats.values():
        for k in d:
            d[k] = [round(x, 2) for x in d[k]]
    gmv_all = {shop: round(sum(v[0] for v in d.values()), 2) for shop, d in cats.items()}
    return ({"fruit": fruit, "um": um, "as_of": as_of, "disp": ped_disposal(get, cats), "gmv": gmv_all},
            {"cols": ["shop", "sku", "cat", "gmv", "qty", "disc", "disp_qty", "disp_cost"], "rows": rows, "names": names,
             "teams": teams, "um": um, "as_of": as_of})


RED_COLS = ["dm", "am", "shop_code", "sku_id", "sku_name", "mult", "start", "end", "gmv_before", "redemption", "gmv_after"]


def fetch_redemption(get):
    """Redemption (with x1.2 & x1.5) by SKU per shop, copied from Tableau into the POS sheet's 'Redemption by SKU' tab
    by the daily scheduled task (Vivian, 3/11/2026). Optional."""
    try:
        rows, _ = parse_pos(get(POS_SHEET, f"{pos_tab('Redemption by SKU')}!A1:K6000"), RED_COLS,
                            {"dm", "am", "shop_code", "sku_id", "sku_name", "start", "end"})
        rows = [r for r in rows if r.get("sku_id") and r.get("shop_code") and "All" not in (r["sku_id"], r["shop_code"], r["am"])]
        if not rows:
            return None
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
        return {"rows": rows, "period": info.get("Redemption period", ""), "exported_at": info.get("Redemption exported at", "")}
    except Exception as e:
        print(f"Redemption tab not read: {e}", file=sys.stderr)
        return None


def fetch_md(get):
    """Manual Discount by shop, copied from Tableau into the POS sheet's 'Manual Discount' tab (optional)."""
    try:
        shops, _ = parse_pos(get(POS_SHEET, f"{pos_tab('Manual Discount')}!A1:H200"), MD_COLS, {"shop_code", "shop_name"})
        info = {str(r[0]).strip(): (r[1] if len(r) > 1 else "") for r in info_rows(get) if r}
        return {"shops": [s for s in shops if s.get("shop_code")], "period": info.get("MD period", ""),
                "exported_at": info.get("MD exported at", ""), "tableau_updated": info.get("MD Tableau last update", "")}
    except Exception as e:
        print(f"Manual Discount tab not read: {e}", file=sys.stderr)
        return None


def fetch_md_raw(get, d, ped):
    """Manual Discount from the PED sheet's 'Discount Raw - <Mon YYYY>' tab (Vivian, 5/11/2026): one row per discounted
    POS line; keep only Promotion Label = MANUAL. Per shop: manual discount (all categories) and Fruits only
    (pos_category_type starting with "fruit"). GMV for the MD rate comes from the same sheet's 'Overall Summary'
    (Total POS GMV, all categories / Fruits). Returns None if the tab is missing (then the Tableau tab is used)."""
    tab = f"Discount Raw - {d:%b %Y}"
    try:
        vals = get(PED_SHEET, f"'{tab}'!A1:R100000")
    except Exception as e:
        print(f"{tab} not read: {e}", file=sys.stderr)
        return None
    if not vals:
        return None
    head = [str(h).strip().lower() for h in vals[0]]
    col = lambda n: head.index(n) if n in head else None
    c = {k: col(n) for k, n in (("shop", "shop code"), ("name", "store name"), ("date", "date"), ("amt", "discount amount"),
                                ("label", "promotion label"), ("cat", "pos_category_type"), ("am", "am"), ("qty", "quantity"))}
    if c["shop"] is None or c["amt"] is None or c["label"] is None:
        print(f"{tab}: columns not found", file=sys.stderr)
        return None
    g = lambda r, k: r[c[k]] if c[k] is not None and c[k] < len(r) else ""
    shops, dates, labels, skipped = {}, [], {}, 0
    for r in vals[1:]:
        shop = str(g(r, "shop")).strip()
        if not shop:
            continue
        lab = str(g(r, "label")).strip().upper()
        labels[lab] = labels.get(lab, 0) + 1
        if lab != "MANUAL":
            skipped += 1
            continue
        amt = num(g(r, "amt")) or 0
        o = shops.setdefault(shop, {"shop_code": shop, "shop_name": str(g(r, "name")).strip(), "am": str(g(r, "am")).strip(),
                                    "md": 0, "lines": 0, "qty": 0, "fruit_md": 0, "fruit_lines": 0})
        o["md"] += amt; o["lines"] += 1; o["qty"] += num(g(r, "qty")) or 0
        if str(g(r, "cat")).strip().lower().startswith("fruit"):
            o["fruit_md"] += amt; o["fruit_lines"] += 1
        dt_ = str(g(r, "date")).strip()
        if dt_:
            dates.append(dt_)
    ped = ped or {}
    gmv, fruit = ped.get("gmv") or {}, ped.get("fruit") or {}
    for code in set(gmv) | set(fruit):  # shops with sales but no manual discount
        shops.setdefault(code, {"shop_code": code, "shop_name": "", "am": "", "md": 0, "lines": 0, "qty": 0, "fruit_md": 0, "fruit_lines": 0})
    for code, o in shops.items():
        o["gmv"] = gmv.get(code)
        o["fruit_gmv"] = (fruit.get(code) or {}).get("gmv")
        for k in ("md", "fruit_md", "qty"):
            o[k] = round(o[k], 2)
    fmt = lambda x: f"{x[8:10]}/{x[5:7]}/{x[0:4]}" if len(x) >= 10 else x
    period = f"{fmt(min(dates))} - {fmt(max(dates))}" if dates else ""
    return {"shops": sorted(shops.values(), key=lambda o: o["shop_code"]), "period": period, "source": "raw", "tab": tab,
            "labels": labels, "skipped": skipped, "gmv_as_of": ped.get("as_of", ""), "exported_at": "", "tableau_updated": ""}


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



def fill_dop_target(t, dm_ex, am_ex, force=False):
    """'<Mon> Target' tab of Nov 2026 has Proj. DOP blank (so its achievement shows 200% / 0%): use the dashboard's own
    Proj. DOP (sum of shops, excl. outlets & EPR - the targets exclude outlets) and work out achievement and difference
    with the sheet's rule: negative target -> target / projection, otherwise projection / target (Vivian, 4/11/2026)."""
    if not t:
        return t
    for block, team in (("dm", dm_ex), ("am", am_ex)):
        proj = {r.get(block): r.get("proj_dop") for r in team}
        for r in t.get(block, []):
            if (force or r.get("proj_dop") is None) and proj.get(r["name"]) is not None:
                p, tg = proj[r["name"]], r.get("target")
                r["proj_dop"] = p
                r["proj_from"] = "dashboard"
                if tg is not None:
                    r["difference"] = p - tg
                    r["achievement"] = (tg / p if tg < 0 else p / tg) if p and tg else None
    return t


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
    u = re.search(r"Last updated[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})", head)  # 「🕒 Last updated: 2026-11-05 10:47 HKT」 in A1
    return {"tm_version": v.group(1) if v else "", "tm_daily_generated": g.group(1) if g else "",
            "tm_sheet_updated": u.group(1) if u else ""}


def tm_actual_day(raw):
    """Day of month the TM cost sheet's actual block runs to: 「實數（截至 1/11）」 in A1, else 「版本：YYYY-MM-DD」."""
    head = cell_text(raw.get("tm_head"))
    m = re.search(r"截至\s*(\d{1,2})/(\d{1,2})", head)
    if m:
        return int(m.group(1))
    m = re.search(r"版本[：:]\s*\d{4}-\d{2}-(\d{2})", head)
    return int(m.group(1)) if m else None


def classic_ft_ratio(raw):
    """Days so far / days in month for the classic tab's FT accrual: day of its "版本：YYYY-MM-DD" date (else today), capped at month end."""
    import calendar
    d = raw["_date"]
    m = re.search(r"版本[：:]\s*(\d{4})-(\d{2})-(\d{2})", cell_text(raw.get("tm_head")))
    if m and (int(m.group(1)), int(m.group(2))) == (d.year, d.month):
        day = int(m.group(3))
    else:
        today = dt.datetime.now(HKT).date()
        day = today.day if (today.year, today.month) == (d.year, d.month) else calendar.monthrange(d.year, d.month)[1]
    return day / calendar.monthrange(d.year, d.month)[1]


def fetch_tiers(get):
    """Shop code -> tier label (optional)."""
    try:
        rows = get(TIER_SHEET, "Sheet1!A1:C300")
        return {str(r[0]).strip(): str(r[2]).strip() for r in rows[1:] if len(r) > 2 and str(r[0]).strip() and str(r[2]).strip()}
    except Exception as e:
        print(f"Tier sheet not read: {e}", file=sys.stderr)
        return {}


def parse_tm_target(values):
    """'<Mon> TM cost target': header row with DM, AM, UM, Code, Shop Name, "Target TM Cost/POS GMV (6.9%…" (a %).
    The % is TM cost ÷ (POS GPA + Pickup GPA, i.e. pickup GMV × 6.9%). Returns {code: {dm, am, um, name, pct}}."""
    out, cols = {}, None
    for r in values:
        cells = [str(c).split("\n")[0].strip() for c in r]
        if cols is None:
            if "Code" in cells:
                low = [c.lower() for c in cells]
                pct = next((i for i, c in enumerate(low) if c.startswith("target tm cost")), None)
                cols = {"dm": cells.index("DM") if "DM" in cells else 0, "am": cells.index("AM") if "AM" in cells else 1,
                        "um": cells.index("UM") if "UM" in cells else None, "code": cells.index("Code"),
                        "name": cells.index("Shop Name") if "Shop Name" in cells else cells.index("Code") + 1, "pct": pct}
            continue
        g = lambda k: cells[cols[k]] if cols.get(k) is not None and cols[k] < len(cells) else ""
        if not g("code"):
            if out:
                break
            continue
        p = num(g("pct"))
        if p is None:
            continue
        out[g("code")] = {"dm": g("dm"), "am": g("am"), "um": g("um").replace("\t", "").strip(), "name": g("name"), "pct": p}
    return out


def parse_tm_target_summary(values):
    """'<Mon> TM cost target' columns I:L (Vivian, 3/11/2026): pivot of DM / AM / UM / "TM Cost %" target, with
    "<AM> Total", "<DM> Total" and "Grand Total" rows. Found by the "TM Cost %" header, so it still works if it moves.
    Returns {dm: {name: pct}, am: {name: pct}, um: {name: pct}, total: pct} or None."""
    head_r = col = None
    for ri, r in enumerate(values[:10]):
        cells = [str(c).strip().lower() for c in r]
        hit = [i for i, c in enumerate(cells) if c == "tm cost %"]
        if hit and hit[0] >= 3:
            head_r, col = ri, hit[0]
            break
    if col is None:
        return None
    out = {"dm": {}, "am": {}, "um": {}, "total": None}
    dm = am = ""
    for r in values[head_r + 1:]:
        r = list(r) + [""] * (col + 1 - len(r))
        d, a, u = (str(x).replace("\t", "").strip() for x in r[col - 3:col])
        p = num(r[col])
        if d == "Grand Total":
            out["total"] = p
            break
        if d.endswith(" Total"):
            out["dm"][d[:-6].strip()] = p
            continue
        if a.endswith(" Total"):
            out["am"][a[:-6].strip()] = p
            continue
        dm, am = d or dm, a or am
        if u and p is not None:
            out["um"][u] = p
    return out if (out["dm"] or out["am"] or out["total"] is not None) else None


def parse_shop_gpa_target(values):
    """'<Mon>' tab of the target sheet: Code, "<Mon> Pick up Shop version" (pickup GMV target), "GPA" (POS GPA target).
    Returns {code: POS GPA target + pickup GMV target × 6.9%}."""
    if not values:
        return {}
    head = [str(h).replace("\n", " ").strip().lower() for h in values[0]]
    try:
        ci, gi = head.index("code"), head.index("gpa")
    except ValueError:
        return {}
    pi = next((i for i, h in enumerate(head) if "pick up" in h and "shop" in h), None)
    out = {}
    for r in values[1:]:
        if len(r) <= ci or not str(r[ci]).strip():
            continue
        gpa = num(r[gi]) if gi < len(r) else None
        pk = num(r[pi]) if pi is not None and pi < len(r) else None
        if gpa is None and pk is None:
            continue
        out[str(r[ci]).strip()] = (gpa or 0) + (pk or 0) * 0.069  # blank GPA (SWH, CHE, MSM) = 0, as the sheet does
    return out


def parse_raw_gpa(values):
    """DOP bot 'Raw - <Mon YYYY>': per shop per day -> {date: {shop: [tm_cost, pos_gpa + pickup_gpa]}} (cols K, H, E)."""
    out = {}
    for r in values:
        if len(r) < 11 or not str(r[0]).strip() or not str(r[1]).strip():
            continue
        tm, gpa, pk = num(r[10]), num(r[7]), num(r[4])
        out.setdefault(str(r[0]).strip(), {})[str(r[1]).strip()] = [tm or 0, (gpa or 0) + (pk or 0), tm is not None]
    return dict(sorted(out.items()))


def tm_vs_target(target, raw_rows, tm_rows, d, gpa_target=None, tm_to=None):
    """Per shop (Vivian, 2/11/2026): TM cost figures from the TM cost sheet ('TM Cost by shop YYYYMM V1': actual to date,
    full-month projection, "<Mon> TM Cost (excl. DMAM)" target, DEF = target − projection); target % from the
    "<Mon> TM cost target" tab. GPA (POS GPA + Pickup GPA) from DOP 'Raw' for the same days as the TM actual
    (tm_to = day of month in the sheet's 「截至 D/M」); projected GPA = month-to-date run rate."""
    if not target:
        return None
    days = parse_raw_gpa(raw_rows)
    if tm_to:
        use = [k for k in days if int(k[8:10]) <= tm_to]
    else:  # fall back to the days that have TM cost in Raw
        tm_days = [k for k, v in days.items() if any(x[2] and x[0] for x in v.values())]
        last = max(tm_days) if tm_days else None
        use = [k for k in days if last and k <= last]
    gdays = sorted(days)  # all days with GPA, for the run rate
    dim = calendar.monthrange(d.year, d.month)[1]
    tmr = {r["shop"]: r for r in tm_rows}
    shops = []
    for code, t in target.items():
        r = tmr.get(code, {})
        gpa = sum(days[k].get(code, [0, 0])[1] for k in use)
        if tm_to and use and len(use) < tm_to:  # Raw (2pm) is behind the TM sheet: scale GPA to the same number of days
            gpa = gpa / len(use) * tm_to
        gall = sum(days[k].get(code, [0, 0])[1] for k in gdays)
        pgpa = gall / len(gdays) * dim if gdays else None
        tm = r.get("total_cost") if r else sum(days[k].get(code, [0, 0])[0] for k in use)
        tgt_tm = r.get("target_tm")
        if tgt_tm is None and (gpa_target or {}).get(code) is not None:
            tgt_tm = t["pct"] * gpa_target[code]
        shops.append({"shop_code": code, "shop_name": t["name"], "dm": t["dm"], "am": t["am"], "um": t["um"],
                      "target_pct": t["pct"], "tm": round(tm, 2) if tm is not None else None, "gpa": round(gpa, 2),
                      "proj_tm": r.get("roster_total_cost"), "proj_gpa": round(pgpa, 2) if pgpa is not None else None,
                      "target_tm": round(tgt_tm, 2) if tgt_tm is not None else None, "target_def": r.get("target_def")})
    end = f"{d:%Y-%m}-{tm_to:02d}" if tm_to else (use[-1] if use else "")
    return {"shops": shops, "days": tm_to or len(use), "gpa_days": len(use), "from": f"{d:%Y-%m}-01", "to": end,
            "gpa_to": use[-1] if use else "", "days_in_month": dim, "tm_source": "sheet" if tm_rows and "target_tm" in tm_rows[0] else "raw"}


# ---------- fetching ----------
def tabs_for(d):
    mon = d.strftime("%b %Y")  # e.g. "Oct 2026"
    return {
        "dm": (DOP_SHEET, f"'Summary - {mon}'!A1:T5"),
        "am": (DOP_SHEET, f"'Summary - {mon}'!A8:T15"),
        "dm_ex": (DOP_SHEET, f"'Summary - {mon}'!A18:T22"),
        "am_ex": (DOP_SHEET, f"'Summary - {mon}'!A25:T32"),
        "shops": (DOP_SHEET, f"'DOP MTD - {mon}'!A1:V150"),
        "dop_target": (DOP_SHEET, f"'{d:%b} Target'!A1:H40"),
        "commission": (DOP_SHEET, f"'{d:%b} Target %'!A1:K20"),
        "tm_daily_head": (TM_SHEET, f"'每日 PT 外判 {d:%Y%m}'!A1"),
        "tm_daily": (TM_SHEET, f"'每日 PT 外判 {d:%Y%m}'!A2:ZZ4"),
        "raw": (DOP_SHEET, f"'Raw - {mon}'!A2:K4000"),
        "tm_target": (TARGET_SHEET, f"'{d:%b} TM cost target'!A1:Y200"),
        "gpa_target": (TARGET_SHEET, f"'{d:%b}'!A1:I200"),
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
        # Sheets API allows 60 reads / minute: on a 429 wait and retry (the past-month build doubles the reads; 9/11/2026)
        for i in range(5):
            try:
                return svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute().get("values", [])
            except Exception as e:
                if i < 4 and ("RATE_LIMIT_EXCEEDED" in str(e) or "HttpError 429" in str(e)):
                    time.sleep(20 * (i + 1))
                    continue
                raise
    return get


def fetch_month(get, d):
    t = tabs_for(d)
    # The DOP bot keeps a corrected "Summary - <Mon YYYY> (update)" tab (with Proj. DOP; Vivian, 4/11/2026): use it when it exists
    upd = {k: (t[k][0], t[k][1].replace("'!", " (update)'!", 1)) for k in ("dm", "am", "dm_ex", "am_ex")}
    try:
        if get(*upd["dm"]):
            t.update(upd)
    except Exception:
        pass
    optional = ("dop_target", "tm_daily", "raw", "commission", "tm_daily_head", "tm_target", "gpa_target")
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


def fetch_all(get=None, past=None):
    """past = last day of a past month (PAST_MONTHS) -> that month's dashboard; None -> the current month."""
    get = get or make_getter()
    today = dt.datetime.now(HKT).date()
    if past:
        raw = fetch_month(get, past)
    else:
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
    dop_src = "summary"
    if any(s.get("proj_dop") is not None for s in shops):
        # From 5/11/2026 the DOP tab is built from 'DOP MTD - <Mon YYYY>' (Vivian: more accurate than the Summary tabs):
        # DM / AM rows = sum of the shop rows; To-date block = actual, Projected block = full-month projection.
        dm, am = team_from_shops(shops, "dm"), team_from_shops(shops, "am")
        dm_ex, am_ex = team_from_shops(shops, "dm", True), team_from_shops(shops, "am", True)
        as_of, dates = shops_as_of(shops, raw["_date"])
        dop_src = "dop_mtd"
    else:
        for team, key, ex in ((dm, "dm", False), (am, "am", False), (dm_ex, "dm", True), (am_ex, "am", True)):
            fill_team_from_shops(team, shops, key, ex)
    ft_ratio = None
    if raw["_tm_kind"] == "v1":
        tm, um_split = parse_tm_v1(raw["tm"])
    else:
        ft_ratio = classic_ft_ratio(raw)
        tm, um_split = parse_tm(raw["tm"], ft_ratio), False
    if not dm or not shops:
        raise SystemExit("Sheet returned no rows - check the tab names and service-account access")
    excl_dmam = f"{raw['_date']:%Y%m}" >= DMAM_EXCLUDED_FROM
    if excl_dmam:
        for r in tm:
            for blk in ("act", "proj"):
                r[blk]["total"] -= r[blk]["mgmt"]
                r[blk]["mgmt"] = 0
            r["total_cost"], r["roster_total_cost"] = r["act"]["total"], r["proj"]["total"]
    if past:
        # PED 'Overall Summary' / 'By Shop' only hold the current month: no Disposal / Fruit / UM data for a past month.
        # Manual discount = PED 'Discount Raw - <Mon YYYY>' (MANUAL lines); GMV for the MD rate = Tableau Store GMV of that month.
        ped, sku = None, None
        md_t = fetch_md(get)
        md = fetch_md_raw(get, raw["_date"], {"gmv": {s["shop_code"]: s.get("gmv") for s in (md_t or {}).get("shops", [])}}) or md_t
        if md and md.get("source") == "raw":
            md["gmv_src"] = "tableau"
            md["period"] = (md_t or {}).get("period") or md["period"]
    else:
        ped, sku = fetch_ped(get)
        md = fetch_md_raw(get, raw["_date"], ped) or fetch_md(get)
    return {
        "month": raw["_month"],
        "as_of": as_of,
        "dates": dates,
        "tm_budget": fetch_budget(get, raw["_date"]),
        "dm_ex": dm_ex, "am_ex": am_ex,
        "pos": fetch_pos(get),
        "md": md,
        "disposal": None if past else fetch_disposal(get),
        "redemption": fetch_redemption(get),
        "pickup": fetch_pickup(get),
        "offline_t": fetch_offline_tab(get), "_offsku": fetch_offline_sku(get),
        "ped": ped, "_sku": sku,
        "tiers": fetch_tiers(get),
        "tm_daily": parse_tm_daily(raw.get("tm_daily") or []),
        "tm_by_day": parse_tm_by_day(raw.get("raw") or []),
        "dop_target": fill_dop_target(parse_dop_target(raw.get("dop_target") or []), dm_ex, am_ex, force=dop_src == "dop_mtd"),
        "dop_src": dop_src, "dop_tab": f"DOP MTD - {raw['_date']:%b %Y}", "dop_adj": shops_adjusted(raw["shops"]),
        "commission": parse_commission(raw.get("commission") or []),
        "stamps": tm_stamps(raw),
        "tm_target_pct": parse_tm_target_summary(raw.get("tm_target") or []),
        "tm_target": tm_vs_target(parse_tm_target(raw.get("tm_target") or []), raw.get("raw") or [], tm, raw["_date"],
                                  parse_shop_gpa_target(raw.get("gpa_target") or []), tm_actual_day(raw)),
        "tm_tab": raw["_tm_tab"], "tm_um_split": um_split, "tm_ft_ratio": ft_ratio, "tm_excl_dmam": excl_dmam,
        "_tm_month": f"{raw['_date']:%Y%m}", "month_key": f"{raw['_date']:%Y%m}", "past": bool(past),
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


def decrypt(pkg: dict, password: str) -> dict:
    b = lambda s: base64.b64decode(s)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b(pkg["salt"]), iterations=pkg["iter"]).derive(password.encode())
    return json.loads(AESGCM(key).decrypt(b(pkg["iv"]), b(pkg["ct"]), None))


# Daily snapshot of the PROJECTED month TM cost per shop, kept (encrypted with the dashboard password) in
# history/tm_proj.enc.json in the repo; the workflow commits it after each run. One snapshot per HKT day (last run wins).
HISTORY = ROOT / "history" / "tm_proj.enc.json"
HISTORY_DAYS = 70


def snapshot(tm):
    shops = {}
    for r in tm:
        p, n = r["proj"], r.get("proj_n") or {}
        shops[r["shop"]] = {"t": round(p["total"], 2), "pt": round(p["pt"], 2), "os": round(p["os"], 2), "ot": round(p["ot"], 2),
                            "tr": round(p["tr"], 2), "ft": round(p["mgmt"] + p["ss"] + p["sa"], 2),
                            "pt_h": n.get("pt_hours"), "os_h": n.get("os_hours"), "ot_h": n.get("ot_hours"),
                            "ss_n": n.get("ss_n"), "sa_n": n.get("sa_n")}
    return {"total": round(sum(v["t"] for v in shops.values()), 2), "shops": shops}


def update_history(data, password, save=True):
    hist = {}
    if HISTORY.exists():
        try:
            hist = decrypt(json.loads(HISTORY.read_text(encoding="utf-8")), password)
        except Exception as e:  # password changed or file damaged -> start again
            print(f"TM history not readable ({e}); starting a new one", file=sys.stderr)
    month, today = data.pop("_tm_month"), dt.datetime.now(HKT).strftime("%Y-%m-%d")
    if not save:  # past month: show its saved snapshots, change nothing
        data["tm_hist"] = dict(sorted(hist.get(month, {}).items()))
        return
    hist.setdefault(month, {})[today] = snapshot(data["tm"])
    keep = sorted({d for m in hist.values() for d in m})[-HISTORY_DAYS:]
    hist = {m: {d: v for d, v in days.items() if d in keep} for m, days in hist.items()}
    hist = {m: days for m, days in hist.items() if days}
    HISTORY.parent.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(encrypt(hist, password)), encoding="utf-8")
    data["tm_hist"] = dict(sorted(hist.get(month, {}).items()))


def main():
    password = os.environ.get("DASH_PASSWORD", "")
    if len(password) < 10:
        raise SystemExit("DASH_PASSWORD secret missing or shorter than 10 characters")
    global POS_SFX
    get = make_getter()
    data = fetch_all(get)
    sku = data.pop("_sku", None)
    offsku = data.pop("_offsku", None)
    data["offsku"] = bool(offsku)
    update_history(data, password)
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    # Month menu (Vivian, 9/11/2026): one encrypted file per past month; a failing past month never stops the current build
    months = [{"key": "current", "label": data["month"]}]
    for ym in PAST_MONTHS:
        y, m = map(int, ym.split("-"))
        last = dt.date(y, m, calendar.monthrange(y, m)[1])
        if f"{last:%Y%m}" == data["month_key"]:
            continue
        POS_SFX = f" ({last:%b %Y})"
        try:
            pdata = fetch_all(get, past=last)
            pdata.pop("_sku", None)
            osk = pdata.pop("_offsku", None)
            pdata["offsku"] = bool(osk)
            if osk:
                (site / f"offsku_{last:%Y%m}.enc.json").write_text(json.dumps(encrypt(osk, password)), encoding="utf-8")
            update_history(pdata, password, save=False)
            (site / f"data_{last:%Y%m}.enc.json").write_text(json.dumps(encrypt(pdata, password)), encoding="utf-8")
            months.append({"key": f"{last:%Y%m}", "label": pdata["month"]})
            print(f"OK past month: {pdata['month']}, {len(pdata['shops'])} shops")
        except (Exception, SystemExit) as e:
            print(f"Past month {ym} not built: {e!r}", file=sys.stderr)
        finally:
            POS_SFX = ""
    data["months"] = months
    if offsku:  # Tableau POS offline shop x SKU (download only): own encrypted file, loaded when the button is pressed
        (site / "offsku.enc.json").write_text(json.dumps(encrypt(offsku, password)), encoding="utf-8")
    if sku:  # POS offline shop x SKU rows: own encrypted file, loaded only when that tab opens
        (site / "sku.enc.json").write_text(json.dumps(encrypt(sku, password)), encoding="utf-8")
    (site / "data.enc.json").write_text(json.dumps(encrypt(data, password)), encoding="utf-8")
    shutil.copy(ROOT / "index.html", site / "index.html")
    shutil.copy(ROOT / "d3.min.js", site / "d3.min.js")
    (site / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
    print(f"OK: {data['month']}, as of {data['as_of']}, {len(data['shops'])} shops, {len(data['tm'])} TM rows")


if __name__ == "__main__":
    main()
