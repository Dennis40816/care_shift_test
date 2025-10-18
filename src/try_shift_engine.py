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

# ======== Tunables / Defaults ========
DEFAULT_DB = "shifts.db"
DEFAULT_REQ_PATH = "shift_req.json"
DEFAULT_K = 3
DEFAULT_RELAX_MIN = 30
DEFAULT_SLOT_MIN = SLOT_MIN
MAX_SEEDS_DEFAULT = 30
MISSING_PREVIEW_LIMIT = 20
TAG_BASELINE = "baseline"
TAG_ENUM_ALL = "enum-all"

# Python's date.weekday(): Monday=0 .. Sunday=6
DAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"]


# ======== Data ========

@dataclass
class CaseReq:
    case_id: str
    week_date: Optional[str]
    days: Dict[str, List[str]]
    specific: List[Dict[str, Any]]
    k: Optional[int]
    backup_strategy: Optional[Dict[str, Any]] = None
    slot_min: Optional[int] = None
    enumerate_all: Optional[bool] = None
    max_team_size: Optional[int] = None
    include_supersets: Optional[bool] = None


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

    def get_emp_busy_slots(self, slot_min: int) -> Dict[str, set[datetime]]:
        with self._slot_lock:
            cache = self._slot_cache.get(slot_min)
            if cache is None:
                cache = {
                    emp: {slot for a, b in ivs for slot in discretize(a, b, slot_min)}
                    for emp, ivs in self.emp_intervals.items()
                }
                self._slot_cache[slot_min] = cache
                log("DEBUG", "try-shift", f"built emp-busy cache slot={slot_min} employees={len(cache)}")
        return cache

    def compute_missing(self, req_intervals: List[Tuple[datetime, datetime]], slot_min: int) -> List[datetime]:
        need = set(slots_union(req_intervals, slot_min))
        emp_busy = self.get_emp_busy_slots(slot_min)
        possible: set[datetime] = set()
        for busy in emp_busy.values():
            possible |= (need - busy)
        return sorted(need - possible)

    def close(self) -> None:
        self.conn.close()


# ======== Engine ========

class TryShiftEngine:
    def __init__(self, db_path: str = DEFAULT_DB, req_path: str = DEFAULT_REQ_PATH, week_date: Optional[str] = None):
        self.db_path = db_path
        self.req_path = req_path
        self.week_date = week_date
        self.week_any: Optional[date] = None
        self.cases: List[CaseReq] = []
        self.ctx: Optional[RunContext] = None

    # ---------- Helpers (static) ----------
    @staticmethod
    def _sunday_of(day: date) -> date:
        return day - timedelta(days=(day.weekday() + 1) % 7)

    @staticmethod
    def _week_dates(week_any: date) -> Dict[str, date]:
        start = TryShiftEngine._sunday_of(week_any)
        keys = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
        return {keys[i]: start + timedelta(days=i) for i in range(7)}

    @staticmethod
    def _parse_range(range_text: str) -> Tuple[time, time]:
        start_text, end_text = range_text.split("-")
        st = datetime.strptime(start_text.strip(), "%H:%M").time()
        et = datetime.strptime(end_text.strip(), "%H:%M").time()
        if (et.hour, et.minute) <= (st.hour, st.minute):
            raise ValueError("end must be after start")
        return st, et

    @staticmethod
    def _to_datetime(day: date, t: time) -> datetime:
        return datetime(day.year, day.month, day.day, t.hour, t.minute)

    @staticmethod
    def _inflate_intervals(days: Dict[str, List[str]], specific: List[Dict[str, Any]], week_any: date) -> List[Tuple[datetime, datetime]]:
        result: List[Tuple[datetime, datetime]] = []
        week_map = TryShiftEngine._week_dates(week_any)
        for key, ranges in (days or {}).items():
            day = week_map.get(key.lower())
            if not day:
                continue
            for text in ranges:
                st, et = TryShiftEngine._parse_range(text)
                result.append((TryShiftEngine._to_datetime(day, st), TryShiftEngine._to_datetime(day, et)))
        for item in (specific or []):
            day_text = item.get("date")
            if not day_text:
                continue
            d = datetime.fromisoformat(day_text).date()
            for text in item.get("ranges") or []:
                st, et = TryShiftEngine._parse_range(text)
                result.append((TryShiftEngine._to_datetime(d, st), TryShiftEngine._to_datetime(d, et)))
        return result

    @staticmethod
    def _relax_intervals(intervals: List[Tuple[datetime, datetime]], minutes: int) -> List[Tuple[datetime, datetime]]:
        if minutes <= 0:
            return intervals
        delta = timedelta(minutes=minutes)
        return [(s - delta, e + delta) for s, e in intervals]

    @staticmethod
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

    @staticmethod
    def _format_intervals(intervals: Iterable[Tuple[datetime, datetime]]) -> List[str]:
        def sort_key(iv: Tuple[datetime, datetime]) -> Tuple[int, datetime, datetime]:
            s, e = iv
            return ((s.weekday() + 1) % 7, s, e)

        lines: List[str] = []
        for s, e in sorted(intervals, key=sort_key):
            label = DAY_LABELS[s.weekday()]
            lines.append(f"({label}) {s.strftime('%Y-%m-%d %H:%M')} - {e.strftime('%H:%M')}")
        return lines

    # ---------- IO / Context ----------
    @staticmethod
    def _load_emp_intervals(conn: sqlite3.Connection, week_any: date) -> Dict[str, List[Tuple[datetime, datetime]]]:
        start = TryShiftEngine._sunday_of(week_any)
        end = start + timedelta(days=7)
        q = (
            "SELECT employee_name, date, start_min, end_min FROM shifts "
            "WHERE date >= ? AND date < ?"
        )
        intervals: Dict[str, List[Tuple[datetime, datetime]]] = {}
        for name, day_text, start_min, end_min in conn.execute(q, (start.isoformat(), end.isoformat())):
            d = datetime.fromisoformat(day_text).date()
            st = datetime(d.year, d.month, d.day) + timedelta(minutes=int(start_min))
            et = datetime(d.year, d.month, d.day) + timedelta(minutes=int(end_min))
            if et <= st:
                continue
            intervals.setdefault(name or "", []).append((st, et))
        return intervals

    @staticmethod
    def _resolve_case_week(global_week: date, case: CaseReq) -> date:
        return datetime.fromisoformat(case.week_date).date() if case.week_date else global_week

    @staticmethod
    def _auto_k(case: CaseReq) -> Tuple[int, bool, int]:
        base_k = int(case.k or DEFAULT_K)
        bump = any(len(r) > 2 for r in (case.days or {}).values())
        use_k = max(base_k, 4) if bump else base_k
        return base_k, bump, use_k

    # ---------- Parsing ----------
    def parse_request(self) -> Tuple[date, List[CaseReq]]:
        text = Path(self.req_path).read_text(encoding="utf-8")
        config = json.loads(text)
        global_week_text = config.get("week_date") or self.week_date or date.today().isoformat()
        week_any = datetime.fromisoformat(global_week_text).date()
        raw_cases = config.get("cases", []) or []
        cases: List[CaseReq] = []
        for idx, c in enumerate(raw_cases, 1):
            cid = c.get("case_id") or c.get("name") or f"case-{idx}"
            slot_min = int(c["slot_min"]) if c.get("slot_min") is not None else None
            if slot_min is not None and slot_min <= 0:
                raise ValueError("slot_min must be positive")
            cases.append(
                CaseReq(
                    case_id=cid,
                    week_date=c.get("week_date") or None,
                    days=c.get("days") or {},
                    specific=c.get("specific") or [],
                    k=c.get("k"),
                    backup_strategy=c.get("backup_strategy") or None,
                    slot_min=slot_min,
                    enumerate_all=c.get("enumerate_all"),
                    max_team_size=c.get("max_team_size"),
                    include_supersets=c.get("include_supersets"),
                )
            )
        if not cases:
            raise RuntimeError("no cases in shift_req.json")
        self.week_any, self.cases = week_any, cases
        return week_any, cases

    def load_context(self, week_any: date) -> RunContext:
        conn = sqlite3.connect(self.db_path)
        emp_intervals = TryShiftEngine._load_emp_intervals(conn, week_any)
        workload_min = {
            emp: sum(int((e - s).total_seconds() // 60) for s, e in iv)
            for emp, iv in emp_intervals.items()
        }
        log("INFO", "try-shift", f"loaded {len(emp_intervals)} employees' intervals for the week")
        self.ctx = RunContext(conn=conn, week_any=week_any, emp_intervals=emp_intervals, workload_min=workload_min)
        return self.ctx

    # ---------- Solver primitives ----------
    def _need_slots(self, intervals: List[Tuple[datetime, datetime]], slot_min: int) -> set[datetime]:
        return set(slots_union(intervals, slot_min))

    def _emp_free_map(self, need: set[datetime], slot_min: int) -> Dict[str, set[datetime]]:
        assert self.ctx is not None
        busy = self.ctx.get_emp_busy_slots(slot_min)
        return {e: (need - b) for e, b in busy.items()}

    def _rank_emps(self, free_map: Dict[str, set[datetime]]) -> List[str]:
        assert self.ctx is not None
        emps = [e for e, s in free_map.items() if s]
        return sorted(emps, key=lambda e: (self.ctx.workload_min.get(e, 0), e))

    def _minimal_team(self, need: set[datetime], slot_min: int, team_list: List[str]) -> List[str]:
        assert self.ctx is not None
        busy = self.ctx.get_emp_busy_slots(slot_min)
        team: List[str] = list(dict.fromkeys(team_list))
        def union_free(members: List[str]) -> set[datetime]:
            u: set[datetime] = set()
            for e in members:
                u |= (need - busy.get(e, set()))
            return u
        changed = True
        while changed and len(team) > 1:
            changed = False
            for i in range(len(team)):
                others = team[:i] + team[i+1:]
                if union_free(others) >= need:
                    del team[i]
                    changed = True
                    break
        return team

    def _greedy_pick(self, req: List[Tuple[datetime, datetime]], slot_min: int, k: int, seed: Optional[List[str]] = None) -> Tuple[List[str], List[datetime]]:
        assert self.ctx is not None
        emp_busy = self.ctx.get_emp_busy_slots(slot_min)
        need = self._need_slots(req, slot_min)
        picked: List[str] = []
        covered: set[datetime] = set()
        if seed:
            for emp in seed:
                if emp in emp_busy:
                    picked.append(emp)
                    covered |= (need - emp_busy[emp])
        while len(picked) < k and covered != need:
            best_emp: Optional[str] = None
            best_gain = 0
            best_load: Optional[int] = None
            for emp, busy in emp_busy.items():
                if emp in picked:
                    continue
                gain = len(((need - busy) - covered))
                if gain <= 0:
                    continue
                load = self.ctx.workload_min.get(emp, 0)
                if gain > best_gain or (gain == best_gain and (best_load is None or load < best_load)):
                    best_gain = gain
                    best_emp = emp
                    best_load = load
            if not best_emp:
                break
            picked.append(best_emp)
            covered |= (need - emp_busy[best_emp])
        return picked, sorted(need - covered)

    def _generate_candidates(self, req: List[Tuple[datetime, datetime]], slot_min: int, k: int, *, tag: str = TAG_BASELINE, max_seeds: int = MAX_SEEDS_DEFAULT) -> List[Candidate]:
        need = self._need_slots(req, slot_min)
        free_map = self._emp_free_map(need, slot_min)
        ordered = self._rank_emps(free_map)
        seeds: List[List[str]] = [[]] + [[emp] for emp in ordered[:max_seeds]]
        log("DEBUG", "try-shift", f"gen-cands tag={tag} seeds={len(seeds)} k={k} slot={slot_min}")
        seen: set[Tuple[str, ...]] = set()
        out: List[Candidate] = []
        for seed in seeds:
            picked, missing = self._greedy_pick(req, slot_min, k, seed)
            if not missing:
                minimal = self._minimal_team(need, slot_min, picked)
                team = tuple(sorted(minimal))
                if team not in seen:
                    seen.add(team)
                    load = sum(self.ctx.workload_min.get(emp, 0) for emp in team)  # type: ignore
                    out.append(Candidate(team, len(team), load, tag))
        out.sort(key=lambda c: (c.size, c.workload_min, c.team))
        log("INFO", "try-shift", f"gen-cands tag={tag} slot={slot_min} produced={len(out)}")
        return out

    def _enumerate_all(self, req: List[Tuple[datetime, datetime]], slot_min: int, *, max_team_size: Optional[int] = None, include_supersets: bool = False, tag: str = TAG_ENUM_ALL) -> List[Candidate]:
        need = self._need_slots(req, slot_min)
        if not need:
            return []
        assert self.ctx is not None
        busy = self.ctx.get_emp_busy_slots(slot_min)
        free_map: Dict[str, set[datetime]] = {e: (need - b) for e, b in busy.items()}
        free_map = {e: s for e, s in free_map.items() if s}
        if not free_map:
            return []
        ordered = self._rank_emps(free_map)
        index_of = {e: i for i, e in enumerate(ordered)}
        by_slot: Dict[datetime, List[str]] = {}
        for e, slots in free_map.items():
            for s in slots:
                by_slot.setdefault(s, []).append(e)
        for emps in by_slot.values():
            emps.sort(key=lambda e: (self.ctx.workload_min.get(e, 0), e))

        results: List[Candidate] = []
        seen: set[Tuple[str, ...]] = set()

        def dfs(uncovered: set[datetime], min_idx: int, team: List[str]) -> None:
            if not uncovered:
                final = team
                if not include_supersets:
                    final = self._minimal_team(need, slot_min, team)
                key = tuple(sorted(final))
                if key not in seen:
                    seen.add(key)
                    load = sum(self.ctx.workload_min.get(e, 0) for e in final)  # type: ignore
                    results.append(Candidate(key, len(key), load, tag))
                return
            if max_team_size is not None and len(team) >= max_team_size:
                return
            s = min(uncovered, key=lambda x: len([e for e in by_slot.get(x, []) if index_of[e] >= min_idx]))
            candidates = [e for e in by_slot.get(s, []) if index_of[e] >= min_idx]
            for e in candidates:
                idx = index_of[e]
                gain = free_map[e] & uncovered
                if not gain:
                    continue
                dfs(uncovered - gain, idx + 1, team + [e])

        dfs(set(need), 0, [])
        results.sort(key=lambda c: (c.size, c.workload_min, c.team))
        log("INFO", "try-shift", f"enum-all produced={len(results)}")
        return results

    # ---------- High-level ----------
    def _forced_relax(self, case: CaseReq) -> Optional[int]:
        if case.backup_strategy and case.backup_strategy.get("type") == "relax_minutes":
            try:
                return int(case.backup_strategy.get("minutes", DEFAULT_RELAX_MIN))
            except Exception:
                return DEFAULT_RELAX_MIN
        return None

    def _format_candidate_lines(self, cand: Candidate, slot_min: int, need_slots_baseline: set[datetime], need_slots_relaxed: Optional[set[datetime]]) -> List[str]:
        assert self.ctx is not None
        need_slots = need_slots_relaxed if (cand.tag and cand.tag != TAG_BASELINE and need_slots_relaxed is not None) else need_slots_baseline
        emp_busy = self.ctx.get_emp_busy_slots(slot_min)
        entries: List[Tuple[datetime, str, List[datetime]]] = []
        for emp in cand.team:
            covered = sorted(slot for slot in (need_slots - emp_busy.get(emp, set())))
            earliest = covered[0] if covered else datetime.max
            entries.append((earliest, emp, covered))
        remaining = set(need_slots)
        collected: List[Tuple[datetime, datetime, str]] = []
        for _, emp, covered in sorted(entries, key=lambda x: (x[0], x[1])):
            unique = [slot for slot in covered if slot in remaining]
            if not unique:
                continue
            for s in unique:
                remaining.discard(s)
            merged = TryShiftEngine._merge_slots(unique, slot_min)
            for st, et in merged:
                collected.append((st, et, emp))
        # Sort all intervals by time, regardless of employee, then format
        collected.sort(key=lambda t: (t[0], t[1], t[2]))
        lines: List[str] = []
        for st, et, emp in collected:
            label = DAY_LABELS[st.weekday()]
            lines.append(
                f"     {st.strftime('%Y-%m-%d')} ({label}) {st.strftime('%H:%M')} - {et.strftime('%H:%M')}: {emp}"
            )
        return lines

    def _process_case(self, case: CaseReq) -> List[str]:
        assert self.week_any is not None and self.ctx is not None
        lines: List[str] = []
        slot_min = case.slot_min or DEFAULT_SLOT_MIN
        log("INFO", "try-shift", f"{case.case_id}: slot_min={slot_min} minutes")

        case_week = TryShiftEngine._resolve_case_week(self.week_any, case)
        req_intervals = TryShiftEngine._inflate_intervals(case.days, case.specific, case_week)
        day_segments = sum(len(v) for v in (case.days or {}).values())
        log("DEBUG", "try-shift", f"{case.case_id}: intervals={len(req_intervals)} (day_ranges={day_segments}, specific={len(case.specific)})")
        # minimal validation
        min_delta = timedelta(minutes=slot_min)
        short = [iv for iv in req_intervals if (iv[1] - iv[0]) < min_delta]
        if short:
            raise ValueError("requirement interval shorter than slot_min")

        base_k, bump, use_k = TryShiftEngine._auto_k(case)
        log("INFO", "try-shift", f"{case.case_id}: k={use_k} (base={base_k}, auto_bump={bump})")

        need_slots_baseline = self._need_slots(req_intervals, slot_min)
        need_slots_relaxed: Optional[set[datetime]] = None

        if case.enumerate_all:
            all_candidates = self._enumerate_all(
                req_intervals,
                slot_min,
                max_team_size=case.max_team_size,
                include_supersets=bool(case.include_supersets),
                tag=TAG_ENUM_ALL,
            )
            lines.append(f"== {case.case_id} (slot={slot_min}m, enumerate_all) ==")
        else:
            all_candidates = self._generate_candidates(req_intervals, slot_min, use_k, tag=TAG_BASELINE)
            relax_min = self._forced_relax(case)
            if relax_min is not None:
                relaxed_intervals = TryShiftEngine._relax_intervals(req_intervals, relax_min)
                relaxed_candidates = self._generate_candidates(relaxed_intervals, slot_min, use_k, tag=f"relax+{relax_min}m")
                seen = {c.team for c in all_candidates}
                added = 0
                for c in relaxed_candidates:
                    if c.team not in seen:
                        all_candidates.append(c)
                        seen.add(c.team)
                        added += 1
                log("INFO", "try-shift", f"{case.case_id}: forced relax +{relax_min}m adds {added}/{len(relaxed_candidates)} unique candidates")
                need_slots_relaxed = set(slots_union(relaxed_intervals, slot_min))
            lines.append(f"== {case.case_id} (slot={slot_min}m, k<={use_k}) ==")

        if all_candidates:
            lines.append(f"Candidates ({len(all_candidates)}):")
            for idx, cand in enumerate(all_candidates, start=1):
                tag_suffix = f" [{cand.tag}]" if cand.tag and cand.tag != TAG_BASELINE else ""
                lines.append(f"  {idx}) k={cand.size} slot={slot_min}m weekly_load={cand.workload_min}min{tag_suffix}")
                lines.extend(self._format_candidate_lines(cand, slot_min, need_slots_baseline, need_slots_relaxed))
        else:
            lines.append("Candidates (0): none")

        if not all_candidates:
            missing = self.ctx.compute_missing(req_intervals, slot_min)
            lines.append(f"Uncovered slots ({len(missing)}):")
            for s in missing[:MISSING_PREVIEW_LIMIT]:
                lines.append("  " + s.isoformat(timespec="minutes"))
        else:
            lines.append("All requested time covered.")
        lines.append("")
        return lines

    def run(self) -> str:
        log("INFO", "try-shift", f"load req from {self.req_path}")
        week_any, cases = self.parse_request()
        self.load_context(week_any)
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            # Decide output directory
            import os
            out_dir_env = os.getenv("SHIFT_OUT_DIR")
            if out_dir_env:
                out_dir = Path(out_dir_env)
            else:
                base = Path.cwd()
                out_dir = base / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(out_dir / f"shift_candidates_{ts}.txt")
            lines: List[str] = []
            for i, case in enumerate(cases, 1):
                log("INFO", "try-shift", f"case {i}/{len(cases)}: {case.case_id}")
                lines.extend(self._process_case(case))
            content = "\n".join(lines)
            Path(out_path).write_text(content, encoding="utf-8")
            log("INFO", "try-shift", f"written results -> {out_path} ({len(content)} bytes, lines={len(lines)})")
            return out_path
        finally:
            assert self.ctx is not None
            self.ctx.close()


# Backward-compatible function
def run_try_shift(db_path: str = DEFAULT_DB, req_path: str = DEFAULT_REQ_PATH, week_date: Optional[str] = None) -> str:
    return TryShiftEngine(db_path=db_path, req_path=req_path, week_date=week_date).run()
