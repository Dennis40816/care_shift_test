from __future__ import annotations
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Tuple

@dataclass
class Shift:
    emp_name: str
    start: datetime
    end: datetime

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS employees(
      id INTEGER PRIMARY KEY,
      name TEXT UNIQUE NOT NULL
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS shifts(
      id INTEGER PRIMARY KEY,
      emp_id INTEGER NOT NULL,
      start_ts TEXT NOT NULL,
      end_ts TEXT NOT NULL,
      UNIQUE(emp_id, start_ts, end_ts),
      FOREIGN KEY(emp_id) REFERENCES employees(id)
    );
    """)
    return conn

def upsert_shifts(conn: sqlite3.Connection, shifts: Iterable[Shift]) -> None:
    cur = conn.cursor()
    for s in shifts:
        cur.execute("INSERT OR IGNORE INTO employees(name) VALUES (?);", (s.emp_name,))
        cur.execute("SELECT id FROM employees WHERE name=?;", (s.emp_name,))
        emp_id = cur.fetchone()[0]
        cur.execute(
            "INSERT OR IGNORE INTO shifts(emp_id, start_ts, end_ts) VALUES (?,?,?);",
            (emp_id, s.start.isoformat(), s.end.isoformat()),
        )
    conn.commit()

def load_emp_shifts(conn: sqlite3.Connection, date_ymd: str) -> List[Tuple[str, datetime, datetime]]:
    # Load all shifts on target date as (emp_name, start, end)
    q = """
    SELECT e.name, s.start_ts, s.end_ts
    FROM shifts s
    JOIN employees e ON e.id = s.emp_id
    WHERE date(s.start_ts)=? OR date(s.end_ts)=?;
    """
    rows = conn.execute(q, (date_ymd, date_ymd)).fetchall()
    out: List[Tuple[str, datetime, datetime]] = []
    for name, st, et in rows:
        out.append((name, datetime.fromisoformat(st), datetime.fromisoformat(et)))
    return out
