from __future__ import annotations
import argparse
from datetime import datetime
from typing import List, Tuple, Dict
from .utils import log, parse_req_interval
from .scrape import login_and_scrape
from .db import get_conn, upsert_shifts, load_emp_shifts
from .solver import choose_k
from . import config

def run_login_scrape(db_path: str, url: str, user: str, pwd: str, date_str: str) -> None:
    dt = datetime.fromisoformat(date_str)
    shifts = login_and_scrape(url or config.BASE_URL, user, pwd, dt)
    conn = get_conn(db_path)
    upsert_shifts(conn, shifts)
    log("INFO", "db", f"stored {len(shifts)} shifts")

def run_solve(db_path: str, date_str: str, req: List[str], k: int) -> None:
    conn = get_conn(db_path)
    ymd = datetime.fromisoformat(date_str).date().isoformat()
    rows = load_emp_shifts(conn, ymd)

    # Build emp->intervals
    emp_map: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for name, st, et in rows:
        emp_map.setdefault(name, []).append((st, et))

    req_intervals = [parse_req_interval(s) for s in req]
    picked, miss = choose_k(emp_map, req_intervals, k)

    print("Selected employees:", ", ".join(picked) if picked else "(none)")
    if miss:
        print(f"Uncovered slots ({len(miss)}):")
        for t in miss:
            print("  ", t.isoformat(timespec="minutes"))
    else:
        print("All requested time covered.")

def main() -> None:
    ap = argparse.ArgumentParser(prog="care_shift_test")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a1 = sub.add_parser("scrape", help="login and scrape shifts into db")
    a1.add_argument("--db", default="care_shift.db")
    a1.add_argument("--url", default="")
    a1.add_argument("--user", required=True)
    a1.add_argument("--pwd", required=True)
    a1.add_argument("--date", required=True, help="YYYY-MM-DD")

    a2 = sub.add_parser("solve", help="select k employees to cover requested intervals")
    a2.add_argument("--db", default="care_shift.db")
    a2.add_argument("--date", required=True, help="YYYY-MM-DD")
    a2.add_argument("--k", type=int, required=True)
    a2.add_argument("--req", nargs="+", required=True, help='e.g. "2025-10-15 09:00-12:00" ...')

    args = ap.parse_args()
    if args.cmd == "scrape":
        run_login_scrape(args.db, args.url, args.user, args.pwd, args.date)
    elif args.cmd == "solve":
        run_solve(args.db, args.date, args.req, args.k)

if __name__ == "__main__":
    main()
