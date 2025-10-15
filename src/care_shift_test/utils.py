from __future__ import annotations
from datetime import datetime, timedelta
from dateutil import parser
from typing import Iterable, List, Tuple

# Simple logger: [LEVEL] func msg
def log(level: str, func: str, msg: str) -> None:
    print(f"[{level}] {func} {msg}")

def parse_req_interval(s: str) -> Tuple[datetime, datetime]:
    # Example: "2025-10-15 09:00-12:00"
    # Split date and timespan
    date_part, span = s.split()
    t1, t2 = span.split("-")
    start = parser.parse(f"{date_part} {t1}")
    end = parser.parse(f"{date_part} {t2}")
    if end <= start:
        raise ValueError("end must be after start")
    return start, end

def discretize(start: datetime, end: datetime, minutes: int) -> List[datetime]:
    # Generate left-closed time grid [start, end) by fixed minutes
    out: List[datetime] = []
    cur = start
    step = timedelta(minutes=minutes)
    while cur < end:
        out.append(cur)
        cur += step
    return out

def slots_union(intervals: Iterable[Tuple[datetime, datetime]], minutes: int) -> List[datetime]:
    # Merge to unique time slots
    seen = set()
    for s, e in intervals:
        for t in discretize(s, e, minutes):
            seen.add(t)
    return sorted(seen)
