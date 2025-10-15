from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Tuple
from .utils import discretize, slots_union
from .config import SLOT_MIN

# Greedy set cover on discrete slots
def choose_k(employees: Dict[str, List[Tuple[datetime, datetime]]],
             req_intervals: Iterable[Tuple[datetime, datetime]],
             k: int) -> Tuple[List[str], List[datetime]]:
    need = set(slots_union(req_intervals, SLOT_MIN))
    picked: List[str] = []
    covered: set[datetime] = set()

    # Precompute emp -> slots
    emp_slots: Dict[str, set[datetime]] = {}
    for emp, ivs in employees.items():
        s = set()
        for (a, b) in ivs:
            s.update(discretize(a, b, SLOT_MIN))
        emp_slots[emp] = s

    while len(picked) < k and covered != need:
        best_emp = None
        best_gain = 0
        for emp, slots in emp_slots.items():
            if emp in picked:
                continue
            gain = len((slots & need) - covered)
            if gain > best_gain:
                best_gain = gain
                best_emp = emp
        if not best_emp:
            break
        picked.append(best_emp)
        covered |= (emp_slots[best_emp] & need)

    missing = sorted(need - covered)
    return picked, missing
