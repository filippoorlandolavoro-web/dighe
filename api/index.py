
import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

import psycopg2
from fastapi import FastAPI
from psycopg2.extras import RealDictCursor

app = FastAPI()
DATABASE_URL = os.getenv("DATABASE_URL")

RESERVOIR_FIELDS = [
    "quota_slm_attuale", "quota_slm_precedente", "quota_max_slm",
    "lordo_mc_attuale", "lordo_mc_precedente", "lordo_max_mc",
    "netto_mc_attuale", "netto_mc_precedente",
    "var_giorno_attuale", "pioggia_mm_attuale", "neve_cm_attuale",
]
_COLUMNS_SQL = ", ".join(["data"] + RESERVOIR_FIELDS)

FORECAST_PATH = Path(__file__).parent / "precipitation_forecast.json"
_forecast_data_cache = None


def _load_forecast_data():
    # Lazy-loaded (not at import time) so a missing/unbundled file only
    # breaks the forecast endpoint, not every route in the app.
    global _forecast_data_cache
    if _forecast_data_cache is None:
        with open(FORECAST_PATH, "r", encoding="utf-8") as f:
            _forecast_data_cache = json.load(f)
    return _forecast_data_cache


def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def _to_float(value):
    return float(value) if hasattr(value, "normalize") else value


def _row_to_dict(row):
    result = {}
    for key, value in row.items():
        result[key] = value.isoformat() if key == "data" else _to_float(value)
    return result


def _fetch_all(sql, params, dict_rows=True):
    # try/finally on both conn and cur so a failure mid-query (transient
    # Neon error, bad row, etc.) never leaks the connection - Neon's
    # connection cap is small and Vercel instances are reused across
    # invocations, so leaked connections accumulate quickly.
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor) if dict_rows else conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Aggregation logic (kept in this file, not a separate module, so Vercel's
# Python bundler has no cross-file import to worry about).
#
# Server-side port of the aggregation logic from the web dashboard's
# script.js. Comments reference the original script.js function names for
# traceability while porting (getAnnualData, getCompleteData,
# calculateYearlyEstimateFromPartialData, interpolateData).
#
# Two deliberate deviations from script.js, agreed with the project owner:
# 1. Volume (lordo/netto) averages use their own valid-month count, instead
#    of reusing quota's valid-month count (script.js bug: a month with a
#    valid quota but a null volume silently contributed 0 to the average).
# 2. A month/year is considered "valid" if ANY of quota/lordo/netto is
#    present, not only quota. script.js's quota-only check misclassifies
#    dams that stopped reporting quota_slm_attuale but still report
#    lordo_mc_attuale in later years (see README_PROGETTO.md) as having no
#    data at all.
# ---------------------------------------------------------------------------

LEVEL_FIELDS = ["quota_slm_attuale", "lordo_mc_attuale", "netto_mc_attuale"]

_YEAR_FIELDS = LEVEL_FIELDS + [
    "quota_max_slm",
    "lordo_max_mc",
    "pioggia_mm_attuale",
    "neve_cm_attuale",
]


def _row_month(row):
    return int(row["data"][5:7])


def _row_year(row):
    return int(row["data"][0:4])


def _empty_month(month):
    return {
        "month": month,
        "quota_slm_attuale": None,
        "lordo_mc_attuale": None,
        "netto_mc_attuale": None,
        "quota_max_slm": None,
        "lordo_max_mc": None,
        "pioggia_mm_attuale": None,
        "neve_cm_attuale": None,
        "has_data": False,
    }


def monthly_snapshots(rows_for_year):
    """Port of getMonthlyData + the per-month reduction in getAnnualData.

    For each month: takes the LAST day's row as the level/volume/max snapshot
    (these are instantaneous values, not summed), and SUMS pioggia/neve over
    the days present in the month (these are cumulative daily values).
    """
    by_month = {m: [] for m in range(1, 13)}
    for row in rows_for_year:
        by_month[_row_month(row)].append(row)

    snapshots = []
    for month in range(1, 13):
        days = sorted(by_month[month], key=lambda r: r["data"])
        if not days:
            snapshots.append(_empty_month(month))
            continue
        last = days[-1]
        total_pioggia = sum(d["pioggia_mm_attuale"] or 0 for d in days)
        total_neve = sum(d["neve_cm_attuale"] or 0 for d in days)
        snapshots.append({
            "month": month,
            "quota_slm_attuale": last["quota_slm_attuale"],
            "lordo_mc_attuale": last["lordo_mc_attuale"],
            "netto_mc_attuale": last["netto_mc_attuale"],
            "quota_max_slm": last["quota_max_slm"],
            "lordo_max_mc": last["lordo_max_mc"],
            "pioggia_mm_attuale": total_pioggia,
            "neve_cm_attuale": total_neve,
            "has_data": True,
        })
    return snapshots


def compute_annual_months(rows_for_year):
    return monthly_snapshots(rows_for_year)


def _month_is_valid(month_snapshot):
    return any(month_snapshot[f] is not None for f in LEVEL_FIELDS)


def _average_field(items, field):
    values = [i[field] for i in items if i[field] is not None]
    if not values:
        return None
    return sum(values) / len(values)


def _max_field(items, field):
    values = [i[field] for i in items if i[field] is not None]
    return max(values) if values else None


def _year_stats_from_snapshots(snapshots):
    """One year's 12 monthly snapshots -> aggregated year values, an overall
    status (drives rain/snow classification) and a per-field status for each
    of quota/lordo/netto, before cross-year interpolation.

    A single shared status cannot correctly describe all three level fields:
    a dam can have 12 valid quota months and 0 valid lordo months in the same
    year (see README_PROGETTO.md - some dams switched from reporting quota to
    reporting only lordo over time), so quota/lordo/netto are each classified
    independently from their own valid-month count.
    """
    valid_months = [s for s in snapshots if _month_is_valid(s)]
    n_valid = len(valid_months)

    result = {field: None for field in _YEAR_FIELDS}
    field_status = {}

    for field in LEVEL_FIELDS:
        field_valid_months = [s for s in snapshots if s[field] is not None]
        n_field_valid = len(field_valid_months)
        if n_field_valid == 0:
            field_status[field] = "missing"
        elif n_field_valid >= 6:
            field_status[field] = "actual"
        else:
            field_status[field] = "estimated"
        result[field] = _average_field(snapshots, field)

    # Capacity fields follow their own field's status (max is only ever
    # reported alongside real readings for that field) - script.js
    # deliberately never estimates max values ("Non stimiamo i valori massimi").
    if field_status["quota_slm_attuale"] == "actual":
        result["quota_max_slm"] = _max_field(
            [s for s in snapshots if s["quota_slm_attuale"] is not None], "quota_max_slm")
    if field_status["lordo_mc_attuale"] == "actual":
        result["lordo_max_mc"] = _max_field(
            [s for s in snapshots if s["lordo_mc_attuale"] is not None], "lordo_max_mc")

    if n_valid == 0:
        overall_status = "missing"
    elif n_valid >= 6:
        # "actual": full-year sum for rain/snow (missing months count as 0).
        overall_status = "actual"
        result["pioggia_mm_attuale"] = sum(s["pioggia_mm_attuale"] or 0 for s in snapshots)
        result["neve_cm_attuale"] = sum(s["neve_cm_attuale"] or 0 for s in snapshots)
    else:
        # "estimated" (1-5 valid months): extrapolate rain/snow from the
        # average of the valid months x12.
        overall_status = "estimated"
        avg_pioggia = _average_field(valid_months, "pioggia_mm_attuale")
        avg_neve = _average_field(valid_months, "neve_cm_attuale")
        result["pioggia_mm_attuale"] = avg_pioggia * 12 if avg_pioggia is not None else None
        result["neve_cm_attuale"] = avg_neve * 12 if avg_neve is not None else None

    return result, overall_status, field_status


def _interpolate_years(years, field):
    """Linear interpolation across years with a null value for `field`, only
    when both a previous and a next non-null year exist (port of
    interpolateData). Returns the set of indices that got filled in.
    """
    values = [y[field] for y in years]
    filled = list(values)
    interpolated_indices = set()
    for i, value in enumerate(values):
        if value is not None:
            continue
        prev_index = i - 1
        while prev_index >= 0 and values[prev_index] is None:
            prev_index -= 1
        next_index = i + 1
        while next_index < len(values) and values[next_index] is None:
            next_index += 1
        if prev_index >= 0 and next_index < len(values):
            prev_value = values[prev_index]
            next_value = values[next_index]
            steps = next_index - prev_index
            step_value = (next_value - prev_value) / steps
            filled[i] = prev_value + step_value * (i - prev_index)
            interpolated_indices.add(i)
    for i, year in enumerate(years):
        year[field] = filled[i]
    return interpolated_indices


_FIELD_STATUS_KEY = {
    "quota_slm_attuale": "quota_status",
    "lordo_mc_attuale": "lordo_status",
    "netto_mc_attuale": "netto_status",
}


def compute_complete_years(rows, start_year, end_year):
    """Full 1998-today aggregation for the "Completa" view.

    Returns one entry per year with quota/lordo/netto/max/pioggia/neve, an
    overall `status` (drives rain/snow classification), and a per-field
    `quota_status`/`lordo_status`/`netto_status` - each independently
    "actual" | "estimated" | "interpolated" | "missing" - since the three
    level fields can have different data availability in the same year.
    """
    rows_by_year = {y: [] for y in range(start_year, end_year + 1)}
    for row in rows:
        y = _row_year(row)
        if y in rows_by_year:
            rows_by_year[y].append(row)

    years = []
    for y in range(start_year, end_year + 1):
        snapshots = monthly_snapshots(rows_by_year[y])
        stats, status, field_status = _year_stats_from_snapshots(snapshots)
        years.append({
            "year": y,
            "status": status,
            "quota_status": field_status["quota_slm_attuale"],
            "lordo_status": field_status["lordo_mc_attuale"],
            "netto_status": field_status["netto_mc_attuale"],
            **stats,
        })

    interpolated = {field: set() for field in LEVEL_FIELDS}
    for field in LEVEL_FIELDS:
        interpolated[field] = _interpolate_years(years, field)

    for field in LEVEL_FIELDS:
        status_key = _FIELD_STATUS_KEY[field]
        for i in interpolated[field]:
            if years[i][status_key] == "missing":
                years[i][status_key] = "interpolated"

    # The overall status (used for rain/snow) rides along with quota's,
    # since quota is what the "Completa" view leans on most by default.
    for i in interpolated["quota_slm_attuale"]:
        if years[i]["status"] == "missing":
            years[i]["status"] = "interpolated"

    return years


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def root():
    return {"message": "Benvenuto nell'API dei dighe!"}


@app.get("/dams")
def get_dams():
    rows = _fetch_all("SELECT nome_diga FROM dighe ORDER BY nome_diga;", (), dict_rows=False)
    return {"dams": [row[0] for row in rows]}


@app.get("/reservoir-data/{nome_diga}")
def get_reservoir_data(
    nome_diga: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
    limit: int = 30,
):
    if year is not None and month is not None:
        rows = _fetch_all(
            f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
            "WHERE nome_diga = %s AND EXTRACT(YEAR FROM data) = %s AND EXTRACT(MONTH FROM data) = %s "
            "ORDER BY data ASC;",
            (nome_diga, year, month),
        )
    elif year is not None:
        rows = _fetch_all(
            f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
            "WHERE nome_diga = %s AND EXTRACT(YEAR FROM data) = %s "
            "ORDER BY data ASC;",
            (nome_diga, year),
        )
    else:
        # No date filter: "most recent N readings", newest first - a
        # different, intentional convention from the year/month branches
        # above (which return chronological ASC order for charting). The
        # Android app always passes year+month, so it never hits this branch.
        rows = _fetch_all(
            f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
            "WHERE nome_diga = %s ORDER BY data DESC LIMIT %s;",
            (nome_diga, limit),
        )
    return {"reservoir_data": [_row_to_dict(row) for row in rows]}


@app.get("/reservoir-data/{nome_diga}/annual")
def get_reservoir_annual(nome_diga: str, year: int):
    rows = _fetch_all(
        f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
        "WHERE nome_diga = %s AND EXTRACT(YEAR FROM data) = %s ORDER BY data ASC;",
        (nome_diga, year),
    )
    months = compute_annual_months([_row_to_dict(row) for row in rows])
    return {"nome_diga": nome_diga, "year": year, "months": months}


@app.get("/reservoir-data/{nome_diga}/complete")
def get_reservoir_complete(nome_diga: str):
    rows = _fetch_all(
        f"SELECT {_COLUMNS_SQL} FROM reservoir_data WHERE nome_diga = %s ORDER BY data ASC;",
        (nome_diga,),
    )
    start_year = 1998
    end_year = date.today().year
    years = compute_complete_years([_row_to_dict(row) for row in rows], start_year, end_year)
    return {
        "nome_diga": nome_diga,
        "start_year": start_year,
        "end_year": end_year,
        "years": years,
    }


@app.get("/precipitation-forecast/{nome_diga}")
def get_precipitation_forecast(
    nome_diga: str, year: Optional[int] = None, month: Optional[int] = None
):
    dam_forecast = _load_forecast_data().get(nome_diga, {})
    result = []
    for year_str, year_data in dam_forecast.items():
        if year is not None and int(year_str) != year:
            continue
        for month_str, month_data in year_data.items():
            if month is not None and int(month_str) != month:
                continue
            for day_str, mm in month_data.items():
                iso_date = f"{int(year_str):04d}-{int(month_str):02d}-{int(day_str):02d}"
                result.append({"data": iso_date, "pioggia_mm": mm})
    result.sort(key=lambda r: r["data"])
    return {"nome_diga": nome_diga, "forecast": result}
