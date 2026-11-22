"""Blast Radius selector entry point: read, plan, verify, guard, pack and print one JSON line per commit."""
from __future__ import annotations
import json
import marshal
import os
import sys
import time
from pathlib import Path
sys.dont_write_bytecode = True
REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr
from reach_map import ReachPlanner
from worker_pack import duration_ms, pack_workers
DATA_DIR = Path(os.environ.get('BLAST_DATA_DIR', '/data'))
SQLGLOT_ROOT = Path(os.environ.get('SQLGLOT_ROOT', '/sqlglot'))
MAPS_DIR = Path(os.environ.get('BLAST_MAPS_DIR', str(Path(__file__).resolve().parent / 'maps')))

def main() -> None:
    t0 = time.perf_counter()
    t_mono0 = time.monotonic()
    patch_paths = sorted((DATA_DIR / 'commits').glob('*.patch'))
    n_commits = len(patch_paths)
    default_budget = 155.0 if n_commits <= 45 else 240.0
    budget = float(os.environ.get('BLAST_BUDGET', default_budget))
    tests_list: list[dict] = json.loads((DATA_DIR / 'tests.json').read_text(encoding='utf-8'))['tests']
    num_to_test = {t['number']: t for t in tests_list}
    dur_by_num = {t['number']: duration_ms(t['seconds']) for t in tests_list}
    raw_core = Path('/app/maps_raw/core_maps.marshal')
    if raw_core.exists():
        maps = marshal.loads(raw_core.read_bytes())
    else:
        import zlib
        core_path = MAPS_DIR / 'core_maps.zlib'
        if not core_path.exists():
            core_path = MAPS_DIR / 'blast_maps.zlib'
        maps = marshal.loads(zlib.decompress(core_path.read_bytes()))
    t_load = time.perf_counter()
    engine = ReachPlanner(maps, tests_list, SQLGLOT_ROOT, subtests_dir=MAPS_DIR / 'subtests')
    plans = [engine.plan_patch(p.stem, p.read_text(encoding='utf-8', errors='replace')) for p in patch_paths]
    t_plan = time.perf_counter()
    if any((not p.is_noop for p in plans)):
        import unittest
        import verifier as _runner
        _runner.DEADLINE = t_mono0 + budget
        selected_by_commit = _runner.check_batch(plans, maps, num_to_test, dur_by_num, SQLGLOT_ROOT, engine=engine)
    else:
        selected_by_commit = {p.commit: [] for p in plans}
    t_verify = time.perf_counter()
    guarded = 0
    for p, path in zip(plans, patch_paths):
        try:
            if p.is_noop:
                continue
            from shared_state import extra_tests
            extra = extra_tests(path.read_text(encoding='utf-8', errors='replace'), getattr(p, 'candidates', None), num_to_test)
            if extra:
                chosen = set(selected_by_commit.get(p.commit, []))
                selected_by_commit[p.commit] = sorted(chosen | {n for n in extra if n in dur_by_num})
                guarded += 1
        except Exception as exc:
            print(f'[guard] {p.commit}: skipped ({exc!r})', file=sys.stderr)
    if guarded:
        print(f'[guard] {guarded} commits write shared state: candidates reported', file=sys.stderr)
    lines: list[str] = []
    total_sim_ms = 0
    for p in plans:
        chosen = selected_by_commit.get(p.commit, [])
        cost_ms, workers = pack_workers(chosen, dur_by_num)
        total_sim_ms += cost_ms
        lines.append(json.dumps({'commit': p.commit, 'workers': workers}, separators=(',', ':')))
    REAL_STDOUT.write('\n'.join(lines) + ('\n' if lines else ''))
    REAL_STDOUT.flush()
    t_end = time.perf_counter()
    grp_counts: dict[str, int] = {}
    for p in plans:
        grp_counts[p.group] = grp_counts.get(p.group, 0) + 1
    print(f'[blast] commits={len(plans)} groups={grp_counts} sim={total_sim_ms / 1000:.3f}s wall={t_end - t0:.3f}s (load={t_load - t0:.3f}s plan={t_plan - t_load:.3f}s verify={t_verify - t_plan:.3f}s)', file=sys.stderr)
    sys.stderr.flush()
    os._exit(0)
if __name__ == '__main__':
    main()
