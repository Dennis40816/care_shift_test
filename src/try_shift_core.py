from __future__ import annotations
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from care_shift_test.utils import log, discretize, slots_union
from care_shift_test.config import SLOT_MIN

DEFAULT_DB = "shifts.db"
DEFAULT_REQ_PATH = "shift_req.json"
DEFAULT_K = 3
DEFAULT_RELAX_MIN = 30
DEFAULT_SLOT_MIN = SLOT_MIN

DAY_LABELS = ["日", "一", "二", "三", "四", "五", "六"]


@dataclass
class CaseReq:
    case_id: str
    week_date: Optional[str]
    days: Dict[str, List[str]]
    specific: List[Dict[str, Any]]
    k: Optional[int]
    backup_strategy: Optional[Dict[str, Any]] = None
    slot_min: Optional[int] = None


@dataclass(frozen=True)
class Candidate:
    team: Tuple[str, ...]
    size: int
    workload_min: int
    tag: str = ""


@dataclass
class RunContext:
    conn: sqlite3.Connection
    week_any: date
    emp_intervals: Dict[str, List[Tuple[datetime, datetime]]]
    workload_min: Dict[str, int]
    _slot_cache: Dict[int, Dict[str, set[datetime]]] = field(default_factory=dict)
    _slot_lock: threading.Lock = field(default_factory=threading.Lock)

    def get_emp_slots(self, slot_min: int) -> Dict[str, set[datetime]]:
        with self._slot_lock:
            cache = self._slot_cache.get(slot_min)
            if cache is None:
                cache = {
                    emp: {slot for a, b in ivs for slot in discretize(a, b, slot_min)}
                    for emp, ivs in self.emp_intervals.items()
                }
                self._slot_cache[slot_min] = cache
                log("DEBUG", "try-shift", f"built emp-slots cache for slot_min={slot_min} (employees={len(cache)})")
        return cache

    def compute_missing(self, req_intervals: List[Tuple[datetime, datetime]], slot_min: int) -> List[datetime]:
        need = set(slots_union(req_intervals, slot_min))
        emp_slots = self.get_emp_slots(slot_min)
        possible: set[datetime] = set()
        for slots in emp_slots.values():
            possible |= (slots & need)
        return sorted(need - possible)

    def close(self) -> None:
        self.conn.close()


def _sunday_of(day: date) -> date:
    return day - timedelta(days=(day.weekday() + 1) % 7)


def _week_dates(week_any: date) -> Dict[str, date]:
    start = _sunday_of(week_any)
    keys = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    return {keys[i]: start + timedelta(days=i) for i in range(7)}


def _parse_range(range_text: str) -> Tuple[time, time]:
    start_text, end_text = range_text.split("-")
    start_time = datetime.strptime(start_text.strip(), "%H:%M").time()
    end_time = datetime.strptime(end_text.strip(), "%H:%M").time()
    if (end_time.hour, end_time.minute) <= (start_time.hour, start_time.minute):
        raise ValueError("end must be after start")
    return start_time, end_time


def _to_datetime(day: date, time_: time) -> datetime:
    return datetime(day.year, day.month, day.day, time_.hour, time_.minute)


def _inflate_intervals(
    days: Dict[str, List[str]],
    specific: List[Dict[str, Any]],
    week_any: date,
) -> List[Tuple[datetime, datetime]]:
    result: List[Tuple[datetime, datetime]] = []
    week_map = _week_dates(week_any)
    for key, ranges in (days or {}).items():
        day = week_map.get(key.lower())
        if not day:
            continue
        for range_text in ranges:
            start_time, end_time = _parse_range(range_text)
            result.append((_to_datetime(day, start_time), _to_datetime(day, end_time)))
    for item in (specific or []):
        date_text = item.get("date")
        if not date_text:
            continue
        day = datetime.fromisoformat(date_text).date()
        for range_text in item.get("ranges") or []:
            start_time, end_time = _parse_range(range_text)
            result.append((_to_datetime(day, start_time), _to_datetime(day, end_time)))
    return result


def _relax_intervals(intervals: List[Tuple[datetime, datetime]], minutes: int) -> List[Tuple[datetime, datetime]]:
    if minutes <= 0:
        return intervals
    delta = timedelta(minutes=minutes)
    return [(start - delta, end + delta) for start, end in intervals]


def _merge_slots(slots: Iterable[datetime], slot_min: int) -> List[Tuple[datetime, datetime]]:
    step = timedelta(minutes=slot_min)
    merged: List[Tuple[datetime, datetime]] = []
    current_start: Optional[datetime] = None
    previous: Optional[datetime] = None
    for slot in sorted(slots):
        if current_start is None:
            current_start = slot
            previous = slot
            continue
        if slot == previous + step:
            previous = slot
        else:
            merged.append((current_start, previous + step))
            current_start = slot
            previous = slot
    if current_start is not None and previous is not None:
        merged.append((current_start, previous + step))
    return merged


def _format_intervals(intervals: Iterable[Tuple[datetime, datetime]]) -> List[str]:
    def sort_key(interval: Tuple[datetime, datetime]) -> Tuple[int, datetime, datetime]:
        start, end = interval
        return ((start.weekday() + 1) % 7, start, end)

    formatted: List[str] = []
    for start, end in sorted(intervals, key=sort_key):
        label = DAY_LABELS[start.weekday()]
        formatted.append(f"({label}) {start.strftime('%Y-%m-%d %H:%M')} - {end.strftime('%H:%M')}")
    return formatted


def _load_emp_intervals(conn: sqlite3.Connection, week_any: date) -> Dict[str, List[Tuple[datetime, datetime]]]:
    start = _sunday_of(week_any)
    end = start + timedelta(days=7)
    query = (
        "SELECT employee_name, date, start_min, end_min FROM shifts "
        "WHERE date >= ? AND date < ?"
    )
    intervals: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for name, day_text, start_min, end_min in conn.execute(query, (start.isoformat(), end.isoformat())):
        day = datetime.fromisoformat(day_text).date()
        start_dt = datetime(day.year, day.month, day.day) + timedelta(minutes=int(start_min))
        end_dt = datetime(day.year, day.month, day.day) + timedelta(minutes=int(end_min))
        if end_dt <= start_dt:
            continue
        intervals.setdefault(name or "", []).append((start_dt, end_dt))
    return intervals


def _validate_intervals(case: CaseReq, intervals: List[Tuple[datetime, datetime]], slot_min: int) -> None:
    min_delta = timedelta(minutes=slot_min)
    short = [interval for interval in intervals if (interval[1] - interval[0]) < min_delta]
    if short:
        log("ERROR", "try-shift", f"{case.case_id}: requirement shorter than slot_min={slot_min}")
        raise ValueError("requirement interval shorter than slot_min")


def _greedy_pick(
    ctx: RunContext,
    req_intervals: List[Tuple[datetime, datetime]],
    slot_min: int,
    k: int,
    seed: Optional[List[str]] = None,
) -> Tuple[List[str], List[datetime]]:
    emp_slots = ctx.get_emp_slots(slot_min)
    need = set(slots_union(req_intervals, slot_min))
    picked: List[str] = []
    covered: set[datetime] = set()
    if seed:
        for emp in seed:
            if emp in emp_slots:
                picked.append(emp)
                covered |= (emp_slots[emp] & need)
    while len(picked) < k and covered != need:
        best_emp: Optional[str] = None
        best_gain = 0
        best_load: Optional[int] = None
        for emp, slots in emp_slots.items():
            if emp in picked:
                continue
            gain = len((slots & need) - covered)
            if gain <= 0:
                continue
            load = ctx.workload_min.get(emp, 0)
            if gain > best_gain or (gain == best_gain and (best_load is None or load < best_load)):
                best_gain = gain
                best_emp = emp
                best_load = load
        if not best_emp:
            break
        picked.append(best_emp)
        covered |= (emp_slots[best_emp] & need)
    return picked, sorted(need - covered)


def _generate_candidates(
    ctx: RunContext,
    req_intervals: List[Tuple[datetime, datetime]],
    slot_min: int,
    k: int,
    *,
    tag: str = "baseline",
    max_seeds: int = 12,
) -> List[Candidate]:
    need = set(slots_union(req_intervals, slot_min))
    emp_slots = ctx.get_emp_slots(slot_min)
    cover_sizes: List[Tuple[int, int, str]] = []
    for emp, slots in emp_slots.items():
        coverage = len(slots & need)
        if coverage > 0:
            cover_sizes.append((-coverage, ctx.workload_min.get(emp, 0), emp))
    cover_sizes.sort()
    seeds: List[List[str]] = [[]]
    for _, _, emp in cover_sizes[:max_seeds]:
        seeds.append([emp])
    log("DEBUG", "try-shift", f"gen-cands tag={tag} seeds={len(seeds)} k={k} slot={slot_min}")
    seen: set[Tuple[str, ...]] = set()
    candidates: List[Candidate] = []
    for seed in seeds:
        picked, missing = _greedy_pick(ctx, req_intervals, slot_min, k, seed)
        if not missing:
            team = tuple(sorted(picked))
            if team not in seen:
                seen.add(team)
                load = sum(ctx.workload_min.get(emp, 0) for emp in team)
                candidates.append(Candidate(team, len(team), load, tag))
    candidates.sort(key=lambda c: (c.size, c.workload_min, c.team))
    log("INFO", "try-shift", f"gen-cands tag={tag} slot={slot_min} produced={len(candidates)}")
    return candidates


def _build_busy_map(ctx: RunContext, slot_min: int) -> Dict[str, set[datetime]]:
    return {
        emp: {slot for start, end in ctx.emp_intervals.get(emp, []) for slot in discretize(start, end, slot_min)}
        for emp in ctx.emp_intervals.keys()
    }


def _filter_conflicts(
    ctx: RunContext,
    candidates: List[Candidate],
    slot_min: int,
    need_slots_baseline: set[datetime],
    need_slots_relaxed: Optional[set[datetime]],
) -> List[Candidate]:
    busy_map = _build_busy_map(ctx, slot_min)
    filtered: List[Candidate] = []
    for cand in candidates:
        need_slots = need_slots_baseline
        if cand.tag and cand.tag != "baseline" and need_slots_relaxed is not None:
            need_slots = need_slots_relaxed
        conflict = any(busy_map.get(emp, set()) & need_slots for emp in cand.team)
        if conflict:
            log("WARN", "try-shift", f"drop team {cand.team} due to busy conflict")
            continue
        filtered.append(cand)
    return filtered


def _format_candidate_lines(
    ctx: RunContext,
    cand: Candidate,
    slot_min: int,
    need_slots_baseline: set[datetime],
    need_slots_relaxed: Optional[set[datetime]],
) -> List[str]:
    need_slots = need_slots_baseline
    if cand.tag and cand.tag != "baseline" and need_slots_relaxed is not None:
        need_slots = need_slots_relaxed
    emp_slots = ctx.get_emp_slots(slot_min)
    entries: List[Tuple[datetime, str, List[datetime]]] = []
    for emp in cand.team:
        covered = sorted(slot for slot in emp_slots.get(emp, set()) if slot in need_slots)
        earliest = covered[0] if covered else datetime.max
        entries.append((earliest, emp, covered))
    remaining = set(need_slots)
    lines: List[str] = []
    for _, emp, covered in sorted(entries, key=lambda item: (item[0], item[1])):
        unique = [slot for slot in covered if slot in remaining]
        if not unique:
            continue
        for slot in unique:
            remaining.discard(slot)
        merged = _merge_slots(unique, slot_min)
        for interval in _format_intervals(merged):
            lines.append(f"     - {emp}: {interval}")
    return lines


def _process_case(ctx: RunContext, global_week: date, case: CaseReq) -> List[str]:
    lines: List[str] = []
    slot_min = case.slot_min or DEFAULT_SLOT_MIN
    log("INFO", "try-shift", f"{case.case_id}: slot_min={slot_min} minutes")
    case_week = datetime.fromisoformat(case.week_date).date() if case.week_date else global_week
    req_intervals = _inflate_intervals(case.days, case.specific, case_week)
    day_segments = sum(len(v) for v in (case.days or {}).values())
    log(
        "DEBUG",
        "try-shift",
        f"{case.case_id}: intervals={len(req_intervals)} (day_ranges={day_segments}, specific={len(case.specific)})",
    )
    _validate_intervals(case, req_intervals, slot_min)

    base_k = int(case.k or DEFAULT_K)
    bump = any(len(ranges) > 2 for ranges in (case.days or {}).values())
    use_k = max(base_k, 4) if bump else base_k
    log("INFO", "try-shift", f"{case.case_id}: k={use_k} (base={base_k}, auto_bump={bump})")

    baseline = _generate_candidates(ctx, req_intervals, slot_min, use_k, tag="baseline")
    need_slots_baseline = set(slots_union(req_intervals, slot_min))
    all_candidates: List[Candidate] = list(baseline)
    forced_relax_minutes: Optional[int] = None

    if case.backup_strategy and case.backup_strategy.get("type") == "relax_minutes":
        try:
            forced_relax_minutes = int(case.backup_strategy.get("minutes", DEFAULT_RELAX_MIN))
        except Exception:
            forced_relax_minutes = DEFAULT_RELAX_MIN
        relaxed_intervals = _relax_intervals(req_intervals, forced_relax_minutes)
        relaxed_candidates = _generate_candidates(
            ctx,
            relaxed_intervals,
            slot_min,
            use_k,
            tag=f"relax+{forced_relax_minutes}m",
        )
        seen = {cand.team for cand in all_candidates}
        added = 0
        for cand in relaxed_candidates:
            if cand.team not in seen:
                all_candidates.append(cand)
                seen.add(cand.team)
                added += 1
        log(
            "INFO",
            "try-shift",
            f"{case.case_id}: forced relax +{forced_relax_minutes}m adds {added}/{len(relaxed_candidates)} unique candidates",
        )

    need_slots_relaxed = (
        set(slots_union(_relax_intervals(req_intervals, forced_relax_minutes), slot_min))
        if forced_relax_minutes is not None
        else None
    )
    all_candidates = _filter_conflicts(ctx, all_candidates, slot_min, need_slots_baseline, need_slots_relaxed)
    lines.append(f"== {case.case_id} (slot={slot_min}m, k<={use_k}) ==")

    if all_candidates:
        lines.append(f"Candidates ({len(all_candidates)}):")
        for idx, cand in enumerate(all_candidates, start=1):
            tag_suffix = f" [{cand.tag}]" if cand.tag and cand.tag != "baseline" else ""
            lines.append(
                f"  {idx}) k={cand.size} slot={slot_min}m weekly_load={cand.workload_min}min{tag_suffix}"
            )
            lines.extend(
                _format_candidate_lines(ctx, cand, slot_min, need_slots_baseline, need_slots_relaxed)
            )
    else:
        lines.append("Candidates (0): none")

    if not all_candidates:
        missing_slots = ctx.compute_missing(req_intervals, slot_min)
        lines.append(f"Uncovered slots ({len(missing_slots)}):")
        for slot in missing_slots[:20]:
            lines.append("  " + slot.isoformat(timespec="minutes"))
        try:
            relax_input = input(
                f"No full cover for {case.case_id}. Relax each range by minutes [default {DEFAULT_RELAX_MIN}]: "
            ).strip()
            relax_min = int(relax_input) if relax_input else DEFAULT_RELAX_MIN
        except Exception:
            relax_min = DEFAULT_RELAX_MIN
        log("INFO", "try-shift", f"{case.case_id}: interactive relax minutes={relax_min}")
        relaxed_intervals = _relax_intervals(req_intervals, relax_min)
        log("INFO", "try-shift", f"{case.case_id}: retry with relax {relax_min}m")
        relaxed_candidates = _generate_candidates(
            ctx,
            relaxed_intervals,
            slot_min,
            use_k,
            tag=f"relax+{relax_min}m",
        )
        lines.append(f"After relax +{relax_min}m:")
        if relaxed_candidates:
            lines.append(f"Candidates ({len(relaxed_candidates)}):")
            for idx, cand in enumerate(relaxed_candidates, start=1):
                lines.append(
                    f"  {idx}) k={cand.size} slot={slot_min}m weekly_load={cand.workload_min}min [{cand.tag}]"
                )
        else:
            lines.append("Candidates (0): none")
    else:
        lines.append("All requested time covered.")

    lines.append("")
    return lines


def parse_request(req_path: str, override_week: Optional[str] = None) -> Tuple[date, List[CaseReq]]:
    text = Path(req_path).read_text(encoding="utf-8")
    config = json.loads(text)
    global_week_text = config.get("week_date") or override_week or date.today().isoformat()
    week_any = datetime.fromisoformat(global_week_text).date()
    raw_cases = config.get("cases", []) or []
    cases: List[CaseReq] = []
    for idx, case_data in enumerate(raw_cases, 1):
        cid = case_data.get("case_id") or case_data.get("name") or f"case-{idx}"
        slot_min_val = case_data.get("slot_min")
        slot_min = None
        if slot_min_val is not None:
            slot_min = int(slot_min_val)
            if slot_min <= 0:
                raise ValueError("slot_min must be positive")
        case = CaseReq(
            case_id=cid,
            week_date=case_data.get("week_date") or None,
            days=case_data.get("days") or {},
            specific=case_data.get("specific") or [],
            k=case_data.get("k"),
            backup_strategy=case_data.get("backup_strategy") or None,
            slot_min=slot_min,
        )
        cases.append(case)
    if not cases:
        raise RuntimeError("no cases in shift_req.json")
    return week_any, cases


def load_context(db_path: str, week_any: date) -> RunContext:
    conn = sqlite3.connect(db_path)
    emp_intervals = _load_emp_intervals(conn, week_any)
    workload_min = {
        emp: sum(int((end - start).total_seconds() // 60) for start, end in intervals)
        for emp, intervals in emp_intervals.items()
    }
    log("INFO", "try-shift", f"loaded {len(emp_intervals)} employees' intervals for the week")
    return RunContext(conn=conn, week_any=week_any, emp_intervals=emp_intervals, workload_min=workload_min)


def run_try_shift(
    db_path: str = DEFAULT_DB,
    req_path: str = DEFAULT_REQ_PATH,
    week_date: Optional[str] = None,
) -> str:
    log("INFO", "try-shift", f"load req from {req_path}")
    week_any, cases = parse_request(req_path, week_date)
    log("INFO", "try-shift", f"global week_date={week_any.isoformat()}")
    ctx = load_context(db_path, week_any)
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"shift_candidates_{ts}.txt"
        all_lines: List[str] = []
        for idx, case in enumerate(cases, 1):
            log("INFO", "try-shift", f"case {idx}/{len(cases)}: {case.case_id}")
            all_lines.extend(_process_case(ctx, week_any, case))
        content = "\n".join(all_lines)
        Path(out_path).write_text(content, encoding="utf-8")
        log(
            "INFO",
            "try-shift",
            f"written results -> {out_path} ({len(content)} bytes, lines={len(all_lines)})",
        )
        return out_path
    finally:
        ctx.close()
