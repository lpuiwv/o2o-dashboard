"""Pull DOP + TM cost data from Google Sheets, encrypt it, and write site/ for GitHub Pages.

Env vars (set as GitHub Actions secrets):
  GOOGLE_SA_JSON  - full JSON key of a Google service account that has Viewer access to both sheets
  DASH_PASSWORD   - password people type to open the dashboard
Optional:
  MOCK_VALUES     - path to a JSON file of {range: values} used instead of Google (for local testing)
"""
import base64
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
          "total_cost": 28, "roster_total_cost": 31}


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
        rows.append({k: (r[i] if k in ("shop", "shop_name") else num(r[i])) for k, i in TM_MAP.items()})
    return rows


# ---------- fetching ----------
def tabs_for(d):
    mon = d.strftime("%b %Y")  # e.g. "Oct 2026"
    return {
        "dm": (DOP_SHEET, f"'Summary - {mon}'!A1:O5"),
        "am": (DOP_SHEET, f"'Summary - {mon}'!A8:O15"),
        "dm_ex": (DOP_SHEET, f"'Summary - {mon}'!A18:O22"),
        "am_ex": (DOP_SHEET, f"'Summary - {mon}'!A25:O32"),
        "shops": (DOP_SHEET, f"'DOP MTD - {mon}'!A1:L150"),
        "tm": (TM_SHEET, f"'TM Cost by Shop {d:%Y%m}'!A2:AF200"),
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
    out = {k: get(*v) for k, v in t.items() if not k.startswith("_")}
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
    tm = parse_tm(raw["tm"])
    if not dm or not shops:
        raise SystemExit("Sheet returned no rows - check the tab names and service-account access")
    return {
        "month": raw["_month"],
        "as_of": as_of,
        "dates": dates,
        "tm_budget": fetch_budget(get, raw["_date"]),
        "dm_ex": dm_ex, "am_ex": am_ex,
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
