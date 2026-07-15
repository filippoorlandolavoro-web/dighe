
import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

import psycopg2
from fastapi import FastAPI
from psycopg2.extras import RealDictCursor

from aggregation import compute_annual_months, compute_complete_years

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


@app.get("/")
def root():
    return {"message": "Benvenuto nell'API dei dighe!"}


@app.get("/dams")
def get_dams():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM dighe;")
    dams = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return {"dams": dams}


@app.get("/reservoir-data/{nome_diga}")
def get_reservoir_data(
    nome_diga: str,
    year: Optional[int] = None,
    month: Optional[int] = None,
    limit: int = 30,
):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if year is not None and month is not None:
        cur.execute(
            f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
            "WHERE nome_diga = %s AND EXTRACT(YEAR FROM data) = %s AND EXTRACT(MONTH FROM data) = %s "
            "ORDER BY data ASC;",
            (nome_diga, year, month),
        )
    elif year is not None:
        cur.execute(
            f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
            "WHERE nome_diga = %s AND EXTRACT(YEAR FROM data) = %s "
            "ORDER BY data ASC;",
            (nome_diga, year),
        )
    else:
        cur.execute(
            f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
            "WHERE nome_diga = %s ORDER BY data DESC LIMIT %s;",
            (nome_diga, limit),
        )
    rows = [_row_to_dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    return {"reservoir_data": rows}


@app.get("/reservoir-data/{nome_diga}/annual")
def get_reservoir_annual(nome_diga: str, year: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        f"SELECT {_COLUMNS_SQL} FROM reservoir_data "
        "WHERE nome_diga = %s AND EXTRACT(YEAR FROM data) = %s ORDER BY data ASC;",
        (nome_diga, year),
    )
    rows = [_row_to_dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    months = compute_annual_months(rows)
    return {"nome_diga": nome_diga, "year": year, "months": months}


@app.get("/reservoir-data/{nome_diga}/complete")
def get_reservoir_complete(nome_diga: str):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        f"SELECT {_COLUMNS_SQL} FROM reservoir_data WHERE nome_diga = %s ORDER BY data ASC;",
        (nome_diga,),
    )
    rows = [_row_to_dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    start_year = 1998
    end_year = date.today().year
    years = compute_complete_years(rows, start_year, end_year)
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
