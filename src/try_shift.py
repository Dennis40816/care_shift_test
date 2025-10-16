from __future__ import annotations
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from care_shift_test.utils import log, discretize, slots_union
from care_shift_test.config import SLOT_MIN


# --- Tunables ---
DEFAULT_DB = "shifts.db"
DEFAULT_REQ_PATH = "shift_req.json"
DEFAULT_K = 3
DEFAULT_RELAX_MIN = 30
DEFAULT_SLOT_MIN = SLOT_MIN

DAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]


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


def _sunday_of(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _week_dates(week_any: date) -> Dict[str, date]:
    start = _sunday_of(week_any)
    names = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    return {names[i]: start + timedelta(days=i) for i in range(7)}


def _parse_range(r: str) -> Tuple[time, time]:
    a, b = r.split("-")
    t1 = datetime.strptime(a.strip(), "%H:%M").time()
    t2 = datetime.strptime(b.strip(), "%H:%M").time()
    if (t2.hour, t2.minute) <= (t1.hour, t1.minute):
        raise ValueError("end must be after start in range")
    return t1, t2


def _to_dt(d: date, tt: time) -> datetime:
    return datetime(d.year, d.month, d.day, tt.hour, tt.minute)


def _inflate_intervals(
    days: Dict[str, List[str]],
    specific: List[Dict[str, Any]],
    week_any: date,
) -> List[Tuple[datetime, datetime]]:
    out: List[Tuple[datetime, datetime]] = []
    week = _week_dates(week_any)
    for key, ranges in (days or {}).items():
        day = week.get(key.lower())
        if not day:
            continue
        for r in ranges:
            t1, t2 = _parse_range(r)
            out.append((_to_dt(day, t1), _to_dt(day, t2)))
    for item in (specific or []):
        ds = item.get("date")
        if not ds:
            continue
        dd = datetime.fromisoformat(ds).date()
        for r in item.get("ranges") or []:
            t1, t2 = _parse_range(r)
            out.append((_to_dt(dd, t1), _to_dt(dd, t2)))
    return out


def _relax_intervals(intervals: List[Tuple[datetime, datetime]], minutes: int) -> List[Tuple[datetime, datetime]]:
    if minutes <= 0:
        return intervals
    delta = timedelta(minutes=minutes)
    return [(a - delta, b + delta) for (a, b) in intervals]


def _load_emp_intervals(conn: sqlite3.Connection, week_any: date) -> Dict[str, List[Tuple[datetime, datetime]]]:
    """Load employee availability intervals for the entire target week."""
    start = _sunday_of(week_any)
    end = start + timedelta(days=7)
    query = (
        "SELECT employee_name, date, start_min, end_min FROM shifts "
        "WHERE date >= ? AND date < ?"
    )
    rows = conn.execute(query, (start.isoformat(), end.isoformat())).fetchall()
    emp: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for name, ds, sm, em in rows:
        base_date = datetime.fromisoformat(ds).date()
        start_dt = datetime(base_date.year, base_date.month, base_date.day) + timedelta(minutes=int(sm))
        end_dt = datetime(base_date.year, base_date.month, base_date.day) + timedelta(minutes=int(em))
        if end_dt <= start_dt:
            continue
        emp.setdefault(name or "", []).append((start_dt, end_dt))
    return emp


def _merge_slots(slots: Iterable[datetime], minutes: int) -> List[Tuple[datetime, datetime]]:
    step = timedelta(minutes=minutes)
    out: List[Tuple[datetime, datetime]] = []
    cur_start: Optional[datetime] = None
    prev: Optional[datetime] = None
    for s in sorted(slots):
        if cur_start is None:
            cur_start = s
            prev = s
            continue
        if s == prev + step:
            prev = s
        else:
            out.append((cur_start, prev + step))
            cur_start = s
            prev = s
    if cur_start is not None and prev is not None:
        out.append((cur_start, prev + step))
    return out


def _format_intervals(iv: Iterable[Tuple[datetime, datetime]], minutes: int) -> List[str]:
    def sort_key(interval: Tuple[datetime, datetime]) -> Tuple[int, datetime, datetime]:
        start, end = interval
        return ((start.weekday() + 1) % 7, start, end)

    formatted: List[str] = []
    for a, b in sorted(iv, key=sort_key):
        label = DAY_LABELS[a.weekday()]
        formatted.append(f"({label}) {a.strftime('%Y-%m-%d %H:%M')} - {b.strftime('%H:%M')}")
    return formatted


def run_try_shift(
    db_path: str = DEFAULT_DB,
    req_path: str = DEFAULT_REQ_PATH,
    week_date: Optional[str] = None,
) -> str:
    log("INFO", "try-shift", f"load req from {req_path}")
    try:
        text = Path(req_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        log("ERROR", "try-shift", f"request file not found: {req_path}")
        raise
    log("DEBUG", "try-shift", f"req size={len(text)} bytes")
    try:
        config = json.loads(text)
    except json.JSONDecodeError as exc:
        log("ERROR", "try-shift", f"invalid JSON: {exc}")
        raise

    global_week = config.get("week_date") or week_date or date.today().isoformat()
    log("INFO", "try-shift", f"global week_date={global_week}")
    try:
        week_any = datetime.fromisoformat(global_week).date()
    except ValueError as exc:
        log("ERROR", "try-shift", f"invalid week_date '{global_week}': {exc}")
        raise

    raw_cases = config.get("cases", []) or []
    log("INFO", "try-shift", f"cases={len(raw_cases)}")
    cases: List[CaseReq] = []
    for idx, case_data in enumerate(raw_cases, 1):
        cid = case_data.get("case_id") or case_data.get("name") or f"case-{idx}"
        slot_min_val = case_data.get("slot_min")
        slot_min: Optional[int]
        if slot_min_val is None:
            slot_min = None
        else:
            try:
                slot_min = int(slot_min_val)
            except (TypeError, ValueError):
                log("ERROR", "try-shift", f"case {cid}: invalid slot_min {slot_min_val}")
                raise
            if slot_min <= 0:
                log("ERROR", "try-shift", f"case {cid}: slot_min must be > 0")
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
        day_segments = sum(len(v) for v in case.days.values())
        log(
            "DEBUG",
            "try-shift",
            f"parsed case[{idx}] id={cid} day_ranges={day_segments} specific={len(case.specific)} k={case.k} slot_min={case.slot_min}",
        )

    if not cases:
        msg = "no cases in shift_req.json"
        log("ERROR", "try-shift", msg)
        raise RuntimeError(msg)

    log("INFO", "try-shift", f"load DB {db_path}")
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        log("ERROR", "try-shift", f"cannot open DB {db_path}: {exc}")
        raise

    try:
        emp_intervals = _load_emp_intervals(conn, week_any)
        log("INFO", "try-shift", f"loaded {len(emp_intervals)} employees' intervals for the week")

        workload_min: Dict[str, int] = {
            emp: sum(int((b - a).total_seconds() // 60) for a, b in ivs)
            for emp, ivs in emp_intervals.items()
        }

        emp_slots_cache: Dict[int, Dict[str, set[datetime]]] = {}

        def get_emp_slots(slot_min: int) -> Dict[str, set[datetime]]:
            """Return employees' FREE slots in the target week, not busy ones.

            We treat rows in table 'shifts' as BUSY intervals. So free = week_grid - busy.
            """
            key = ("free", slot_min)
            cache = emp_slots_cache.get(key)
            if cache is not None:
                return cache

            # build week grid: [week_start 00:00, week_end 00:00) by slot_min
            week_start = _sunday_of(week_any)
            grid_start = datetime(week_start.year, week_start.month, week_start.day, 0, 0)
            grid_end = grid_start + timedelta(days=7)
            step = timedelta(minutes=slot_min)

            all_slots: set[datetime] = set()
            cur = grid_start
            while cur < grid_end:
                all_slots.add(cur)
                cur += step

            # busy slots from DB intervals
            busy_map: Dict[str, set[datetime]] = {
                emp: {slot for a, b in ivs for slot in discretize(a, b, slot_min)}
                for emp, ivs in emp_intervals.items()
            }

            # free = all - busy
            free_map: Dict[str, set[datetime]] = {
                emp: (all_slots - busy_slots) for emp, busy_slots in busy_map.items()
            }

            emp_slots_cache[key] = free_map
            log("DEBUG", "try-shift",
                f"built emp FREE-slots for slot_min={slot_min} (employees={len(free_map)})")
            return free_map

        def greedy_pick(
            req_intervals: List[Tuple[datetime, datetime]],
            k: int,
            slot_min: int,
            seed: Optional[List[str]] = None,
        ) -> Tuple[List[str], List[datetime]]:
            emp_slots = get_emp_slots(slot_min)
            need = set(slots_union(req_intervals, slot_min))
            picked: List[str] = []
            covered: set[datetime] = set()
            if seed:
                for s in seed:
                    if s in emp_slots:
                        picked.append(s)
                        covered |= (emp_slots[s] & need)
            while len(picked) < k and covered != need:
                best_emp = None
                best_gain = 0
                best_load = None
                for emp, slots in emp_slots.items():
                    if emp in picked:
                        continue
                    gain = len((slots & need) - covered)
                    if gain <= 0:
                        continue
                    wl = workload_min.get(emp, 0)
                    if gain > best_gain or (gain == best_gain and (best_load is None or wl < best_load)):
                        best_gain = gain
                        best_emp = emp
                        best_load = wl
                if not best_emp:
                    break
                picked.append(best_emp)
                covered |= (emp_slots[best_emp] & need)
            return picked, sorted(need - covered)

        def generate_candidates(
            req_intervals: List[Tuple[datetime, datetime]],
            k: int,
            slot_min: int,
            *,
            tag: str = "baseline",
            max_seeds: int = 12,
        ) -> List[Candidate]:
            emp_slots = get_emp_slots(slot_min)
            need = set(slots_union(req_intervals, slot_min))
            cover_sizes: List[Tuple[int, int, str]] = []
            for emp, slots in emp_slots.items():
                coverage = len(slots & need)
                if coverage > 0:
                    cover_sizes.append((-coverage, workload_min.get(emp, 0), emp))
            cover_sizes.sort()
            seeds: List[List[str]] = [[]]
            for _, _, emp in cover_sizes[:max_seeds]:
                seeds.append([emp])
            log("DEBUG", "try-shift", f"gen-cands tag={tag} seeds={len(seeds)} k={k} slot={slot_min}")
            seen: set[Tuple[str, ...]] = set()
            candidates: List[Candidate] = []
            for seed in seeds:
                picked, missing = greedy_pick(req_intervals, k, slot_min, seed=seed)
                if not missing:
                    team = tuple(sorted(picked))
                    if team not in seen:
                        seen.add(team)
                        total_load = sum(workload_min.get(x, 0) for x in team)
                        candidates.append(Candidate(team, len(team), total_load, tag))
            candidates.sort(key=lambda c: (c.size, c.workload_min, c.team))
            log(
                "INFO",
                "try-shift", f"gen-cands tag={tag} slot={slot_min} produced={len(candidates)}",
            )
            return candidates

        def compute_missing(req_intervals: List[Tuple[datetime, datetime]], slot_min: int) -> List[datetime]:
            emp_slots = get_emp_slots(slot_min)
            need = set(slots_union(req_intervals, slot_min))
            possible: set[datetime] = set()
            for slots in emp_slots.values():
                possible |= (slots & need)
            return sorted(need - possible)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"shift_candidates_{ts}.txt"

        def process_case(index: int, case: CaseReq) -> List[str]:
            lines: List[str] = []
            log("INFO", "try-shift", f"case {index}/{len(cases)}: {case.case_id}")
            slot_min = case.slot_min or DEFAULT_SLOT_MIN
            log("INFO", "try-shift", f"{case.case_id}: slot_min={slot_min} minutes")
            ca_week = datetime.fromisoformat((case.week_date or global_week)).date()
            req_intervals = _inflate_intervals(case.days, case.specific, ca_week)
            if any((b - a) < timedelta(minutes=slot_min) for a, b in req_intervals):
                log(
                    "ERROR",
                    "try-shift", f"{case.case_id}: requirement shorter than slot_min={slot_min}",
                )
                raise ValueError("requirement interval shorter than slot_min")
            day_segments = sum(len(v) for v in (case.days or {}).values())
            log(
                "DEBUG",
                "try-shift",
                f"{case.case_id}: intervals={len(req_intervals)} (day_ranges={day_segments}, specific={len(case.specific)})",
            )

            base_k = int(case.k or DEFAULT_K)
            bump = any(len(r) > 2 for r in (case.days or {}).values())
            use_k = max(base_k, 4) if bump else base_k
            log("INFO", "try-shift", f"{case.case_id}: k={use_k} (base={base_k}, auto_bump={bump})")

            baseline = generate_candidates(req_intervals, use_k, slot_min, tag="baseline")
            need_slots_baseline = set(slots_union(req_intervals, slot_min))
            all_candidates: List[Candidate] = list(baseline)
            forced_relax_minutes: Optional[int] = None

            if case.backup_strategy and case.backup_strategy.get("type") == "relax_minutes":
                try:
                    forced_relax_minutes = int(case.backup_strategy.get("minutes", DEFAULT_RELAX_MIN))
                except Exception:
                    forced_relax_minutes = DEFAULT_RELAX_MIN
                relaxed_intervals = _relax_intervals(req_intervals, forced_relax_minutes)
                relaxed_candidates = generate_candidates(
                    relaxed_intervals,
                    use_k,
                    slot_min,
                    tag=f"relax+{forced_relax_minutes}m",
                )
                seen_teams = {cand.team for cand in all_candidates}
                added = 0
                for cand in relaxed_candidates:
                    if cand.team not in seen_teams:
                        all_candidates.append(cand)
                        seen_teams.add(cand.team)
                        added += 1
                log(
                    "INFO",
                    "try-shift",
                    f"{case.case_id}: forced relax +{forced_relax_minutes}m adds {added}/{len(relaxed_candidates)} unique candidates",
                )

            all_candidates.sort(key=lambda c: (c.size, c.workload_min, c.team))
            log("INFO", "try-shift", f"{case.case_id}: total candidates={len(all_candidates)}")

            lines.append(f"== {case.case_id} (slot={slot_min}m, k<={use_k}) ==")
            if all_candidates:
                # compute relaxed-need and build per-employee FREE slots
                need_slots_relaxed = None
                if forced_relax_minutes is not None:
                    need_slots_relaxed = set(
                        slots_union(_relax_intervals(req_intervals, forced_relax_minutes), slot_min)
                    )
                emp_slots_for_case = get_emp_slots(slot_min)

                # --- conflict filtering (busy vs needed) ---
                # build BUSY map from DB intervals using the same slot granularity
                busy_map = {
                    emp: {
                        slot
                        for a, b in emp_intervals.get(emp, [])
                        for slot in discretize(a, b, slot_min)
                    }
                    for emp in emp_intervals.keys()
                }

                def has_conflict(team: Tuple[str, ...], need_slots: set[datetime]) -> bool:
                    """return True if any team member is busy on any needed slot"""
                    return any((busy_map.get(emp, set()) & need_slots) for emp in team)

                filtered: List[Candidate] = []
                for cand in all_candidates:
                    need_slots_for_team = (
                        need_slots_baseline
                        if (not cand.tag or cand.tag == "baseline")
                        else need_slots_relaxed
                    )
                    if need_slots_for_team is None:
                        need_slots_for_team = need_slots_baseline
                    if has_conflict(cand.team, need_slots_for_team):
                        log("WARN", "try-shift", f"{case.case_id}: drop team {cand.team} due to busy conflict")
                        continue
                    filtered.append(cand)
                all_candidates = filtered
                # --- end conflict filtering ---

                lines.append(f"Candidates ({len(all_candidates)}):")
                for idx, cand in enumerate(all_candidates, start=1):
                    tag_suffix = f" [{cand.tag}]" if cand.tag and cand.tag != "baseline" else ""
                    lines.append(
                        f"  {idx}) k={cand.size} slot={slot_min}m weekly_load={cand.workload_min}min{tag_suffix}"
                    )
                    need_slots_for_team = need_slots_baseline
                    if cand.tag and cand.tag != "baseline" and need_slots_relaxed is not None:
                        need_slots_for_team = need_slots_relaxed
                    emp_entries: List[Tuple[datetime, str, List[datetime]]] = []
                    for emp in cand.team:
                        covered_slots = sorted(
                            slot
                            for slot in emp_slots_for_case.get(emp, set())
                            if slot in need_slots_for_team
                        )
                        earliest = covered_slots[0] if covered_slots else datetime.max
                        emp_entries.append((earliest, emp, covered_slots))
                    remaining_slots = set(need_slots_for_team)
                    for _, emp_name, covered_slots in sorted(emp_entries, key=lambda item: (item[0], item[1])):
                        unique_slots = [slot for slot in covered_slots if slot in remaining_slots]
                        if not unique_slots:
                            continue
                        for slot in unique_slots:
                            remaining_slots.discard(slot)
                        merged = _merge_slots(unique_slots, slot_min)
                        formatted = _format_intervals(merged, slot_min)
                        for interval in formatted:
                            lines.append(f"     - {emp_name}: {interval}")
            else:
                lines.append("Candidates (0): none")


            if not all_candidates:
                missing_slots = compute_missing(req_intervals, slot_min)
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
                relaxed_candidates = generate_candidates(
                    relaxed_intervals,
                    use_k,
                    slot_min,
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

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"shift_candidates_{ts}.txt"

        results: Dict[int, List[str]] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(cases))) as executor:
            future_map = {
                executor.submit(process_case, idx, case): idx
                for idx, case in enumerate(cases, start=1)
            }
            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    log("ERROR", "try-shift", f"case {idx} failed: {exc}")
                    results[idx] = [f"== case-{idx} ==", f"ERROR: {exc}", ""]

        out_lines: List[str] = []
        for idx in range(1, len(cases) + 1):
            out_lines.extend(results.get(idx, [f"== case-{idx} ==", "ERROR: missing result", ""]))

        content = "\n".join(out_lines)
        Path(out_path).write_text(content, encoding="utf-8")
        log(
            "INFO",
            "try-shift",
            f"written results -> {out_path} ({len(content)} bytes, lines={len(out_lines)})",
        )
        return out_path
    finally:
        conn.close()


if __name__ == "__main__":
    run_try_shift()
