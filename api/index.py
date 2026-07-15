
from fastapi import FastAPI
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
import os
from typing import Optional

app = FastAPI()
DATABASE_URL = os.getenv("DATABASE_URL")

class ReservoirData(BaseModel):
    data: str
    nome_diga: str
    quota_slm_attuale: Optional[float]
    netto_mc_attuale: Optional[float]
    pioggia_mm_attuale: Optional[float]

def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

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
def get_reservoir_data(nome_diga: str, limit: int = 30):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT data, quota_slm_attuale, netto_mc_attuale, pioggia_mm_attuale FROM reservoir_data WHERE nome_diga = %s ORDER BY data DESC LIMIT %s;",
        (nome_diga, limit)
    )
    data = cur.fetchall()
    cur.close()
    conn.close()
    # Convert Decimal to float for JSON serialization
    for row in data:
        for key, value in row.items():
            if hasattr(value, 'normalize'):
                row[key] = float(value)
    return {"reservoir_data": data}
