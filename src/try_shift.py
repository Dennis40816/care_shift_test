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


@dataclass
class CaseReq:
    case_id: str
    week_date: Optional[str]  # YYYY-MM-DD in the target week, optional
    days: Dict[str, List[str]]  # e.g., {"mon":["09:00-11:00", ...], ...}
    specific: List[Dict[str, Any]]  # [{"date":"YYYY-MM-DD","ranges":["HH:MM-HH:MM",...]}]
    k: Optional[int]
    backup_strategy: Optional[Dict[str, Any]] = None


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
    """Convert a days mapping and specific overrides into datetime intervals for the target week."""
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
    """Load employee intervals for the whole target week."""
    start = _sunday_of(week_any)
    end = start + timedelta(days=7)
    q = (
        "SELECT employee_name, date, start_min, end_min FROM shifts "
        "WHERE date >= ? AND date < ?"
    )
    rows = conn.execute(q, (start.isoformat(), end.isoformat())).fetchall()
    emp: Dict[str, List[Tuple[datetime, datetime]]] = {}
    for name, ds, sm, em in rows:
        d0 = datetime.fromisoformat(ds).date()
        st = datetime(d0.year, d0.month, d0.day) + timedelta(minutes=int(sm))
        et = datetime(d0.year, d0.month, d0.day) + timedelta(minutes=int(em))
        if et <= st:
            continue
        emp.setdefault(name or "", []).append((st, et))
    return emp


def _merge_slots(slots: Iterable[datetime]) -> List[Tuple[datetime, datetime]]:
    """Merge adjacent SLOT_MIN-spaced slots into intervals for readable output."""
    step = timedelta(minutes=SLOT_MIN)
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


def _format_intervals(iv: Iterable[Tuple[datetime, datetime]]) -> List[str]:
    return [f"{a.strftime('%Y-%m-%d %H:%M')} - {b.strftime('%H:%M')}" for a, b in iv]


def run_try_shift(
    db_path: str = DEFAULT_DB,
    req_path: str = DEFAULT_REQ_PATH,
    week_date: Optional[str] = None,
) -> str:
    log("INFO", "try-shift", f"load req from {req_path}")
    try:
        text = Path(req_path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
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
        case = CaseReq(
            case_id=cid,
            week_date=case_data.get("week_date") or None,
            days=case_data.get("days") or {},
            specific=case_data.get("specific") or [],
            k=case_data.get("k"),
            backup_strategy=case_data.get("backup_strategy") or None,
        )
        cases.append(case)
        day_segments = sum(len(v) for v in case.days.values())
        log(
            "DEBUG",
            "try-shift",
            f"parsed case[{idx}] id={cid} day_ranges={day_segments} specific={len(case.specific)} k={case.k}",
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
        emp_slots: Dict[str, set[datetime]] = {
            emp: {slot for a, b in ivs for slot in discretize(a, b, SLOT_MIN)}
            for emp, ivs in emp_intervals.items()
        }
        log("DEBUG", "try-shift", f"precomputed slots for {len(emp_slots)} employees")

        def greedy_pick(
            req_intervals: List[Tuple[datetime, datetime]],
            k: int,
            seed: Optional[List[str]] = None,
        ) -> Tuple[List[str], List[datetime]]:
            need = set(slots_union(req_intervals, SLOT_MIN))
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
            max_seeds: int = 12,
            *,
            tag: str = "baseline",
        ) -> List[Candidate]:
            need = set(slots_union(req_intervals, SLOT_MIN))
            cover_sizes: List[Tuple[int, int, str]] = []
            for emp, slots in emp_slots.items():
                coverage = len(slots & need)
                if coverage > 0:
                    cover_sizes.append((-coverage, workload_min.get(emp, 0), emp))
            cover_sizes.sort()
            seeds: List[List[str]] = [[]]
            for _, _, emp in cover_sizes[:max_seeds]:
                seeds.append([emp])
            log("DEBUG", "try-shift", f"gen-cands tag={tag} seeds={len(seeds)} k={k}")
            seen: set[Tuple[str, ...]] = set()
            candidates: List[Candidate] = []
            for seed in seeds:
                picked, missing = greedy_pick(req_intervals, k, seed=seed)
                if not missing:
                    team = tuple(sorted(picked))
                    if team not in seen:
                        seen.add(team)
                        total_load = sum(workload_min.get(x, 0) for x in team)
                        candidates.append(Candidate(team, len(team), total_load, tag))
            candidates.sort(key=lambda c: (c.size, c.workload_min, c.team))
            log("INFO", "try-shift", f"gen-cands tag={tag} produced={len(candidates)}")
            return candidates

        def compute_missing(req_intervals: List[Tuple[datetime, datetime]]) -> List[datetime]:
            need = set(slots_union(req_intervals, SLOT_MIN))
            possible: set[datetime] = set()
            for slots in emp_slots.values():
                possible |= (slots & need)
            return sorted(need - possible)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = f"shift_candidates_{ts}.txt"

        def process_case(index: int, case: CaseReq) -> List[str]:
            lines: List[str] = []
            log("INFO", "try-shift", f"case {index}/{len(cases)}: {case.case_id}")
            ca_week = datetime.fromisoformat((case.week_date or global_week)).date()
            req_intervals = _inflate_intervals(case.days, case.specific, ca_week)
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

            baseline = generate_candidates(req_intervals, use_k, tag="baseline")
            need_slots_baseline = set(slots_union(req_intervals, SLOT_MIN))
            all_candidates: List[Candidate] = list(baseline)
            forced_relax_minutes: Optional[int] = None

            if case.backup_strategy and case.backup_strategy.get("type") == "relax_minutes":
                try:
                    forced_relax_minutes = int(case.backup_strategy.get("minutes", DEFAULT_RELAX_MIN))
                except Exception:
                    forced_relax_minutes = DEFAULT_RELAX_MIN
                relaxed_intervals = _relax_intervals(req_intervals, forced_relax_minutes)
                relaxed_candidates = generate_candidates(
                    relaxed_intervals, use_k, tag=f"relax+{forced_relax_minutes}m"
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

            lines.append(f"== {case.case_id} (k<={use_k}) ==")
            if all_candidates:
                lines.append(f"Candidates ({len(all_candidates)}):")
                need_slots_relaxed = None
                if forced_relax_minutes is not None:
                    need_slots_relaxed = set(
                        slots_union(_relax_intervals(req_intervals, forced_relax_minutes), SLOT_MIN)
                    )
                for idx, cand in enumerate(all_candidates, start=1):
                    tag_suffix = f" [{cand.tag}]" if cand.tag and cand.tag != "baseline" else ""
                    lines.append(
                        f"  {idx}) k={cand.size} [{', '.join(cand.team)}] total_load={cand.workload_min}min{tag_suffix}"
                    )
                    need_slots_for_team = need_slots_baseline
                    if cand.tag and cand.tag != "baseline" and need_slots_relaxed is not None:
                        need_slots_for_team = need_slots_relaxed
                    for emp in cand.team:
                        covered_slots = sorted(
                            slot for slot in emp_slots.get(emp, set()) if slot in need_slots_for_team
                        )
                        merged = _merge_slots(covered_slots)
                        for interval in _format_intervals(merged):
                            lines.append(f"     - {emp}: {interval}")
            else:
                lines.append("Candidates (0): none")

            if not all_candidates:
                missing_slots = compute_missing(req_intervals)
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
                    relaxed_intervals, use_k, tag=f"relax+{relax_min}m"
                )
                lines.append(f"After relax +{relax_min}m:")
                if relaxed_candidates:
                    lines.append(f"Candidates ({len(relaxed_candidates)}):")
                    for idx, cand in enumerate(relaxed_candidates, start=1):
                        lines.append(
                            f"  {idx}) k={cand.size} [{', '.join(cand.team)}] total_load={cand.workload_min}min [{cand.tag}]"
                        )
                else:
                    lines.append("Candidates (0): none")
            else:
                lines.append("All requested time covered.")
            lines.append("")
            return lines

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

        out_path = f"shift_candidates_{ts}.txt"
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
