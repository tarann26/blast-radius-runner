"""Image build step: precompute the replay caches, dependency index and parsed maps the
selector loads at run time (nothing here is stored in git)."""
from __future__ import annotations
import ast
import concurrent.futures
import csv
import gzip
import marshal
import os
import pickle
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

def _pick_fast_root() -> tuple[str, str]:
    if Path('/sqlglot-c/sqlglot').is_dir():
        return ('/sqlglot-c', 'c')
    return ('/sqlglot', 'py')

def _build_tpc_opt_master(sg_root: str, plan_out_dir: str) -> int:
    code = f'\nimport marshal, pickle, sys\nfrom pathlib import Path\nsys.path.insert(0, {sg_root!r})\nsys.path.insert(0, "/app")\nimport stage_store as rc\nimport tests.helpers as th\nfrom sqlglot import exp, parse_one\nfrom sqlglot.optimizer import optimize\nfrom sqlglot.planner import Plan\nfrom sqlglot.schema import ensure_schema\n\nast_out = Path("/app/tpc_opt_ast")\nast_out.mkdir(parents=True, exist_ok=True)\nplan_out = Path({plan_out_dir!r})\nplan_out.mkdir(parents=True, exist_ok=True)\ntpcds_sqls = list(th.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))\ns_ds = ensure_schema(th.TPCDS_SCHEMA)\ncnt = 0\nfor i, (args, sql, _) in enumerate(tpcds_sqls, 1):\n    if args.get("execute"):\n        opt = optimize(parse_one(sql), s_ds, leave_tables_isolated=True)\n        pk = rc.pack_expr(opt, exp)\n        if pk is not rc._UNPACKABLE:\n            (ast_out / f"{{i}}.ast").write_bytes(marshal.dumps(pk))\n        pl = Plan(opt)\n        (plan_out / f"{{i}}.pkl").write_bytes(pickle.dumps(pl, protocol=pickle.HIGHEST_PROTOCOL))\n        cnt += 1\nprint(cnt)\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'_build_tpc_opt_master({sg_root}) failed:\n{res.stderr}\n{res.stdout}')
    return int(res.stdout.strip().splitlines()[-1])

def _build_tpc_plans_from_ast(sg_root: str, plan_out_dir: str) -> int:
    code = f'\nimport marshal, pickle, sys\nfrom pathlib import Path\nsys.path.insert(0, {sg_root!r})\nsys.path.insert(0, "/app")\nimport stage_store as rc\nfrom sqlglot import exp\nfrom sqlglot.planner import Plan\n\nplan_out = Path({plan_out_dir!r})\nplan_out.mkdir(parents=True, exist_ok=True)\ncnt = 0\nfor p in sorted(Path("/app/tpc_opt_ast").glob("*.ast"), key=lambda x: int(x.stem)):\n    opt = rc.unpack_expr(marshal.loads(p.read_bytes()), exp)\n    pl = Plan(opt)\n    (plan_out / f"{{p.stem}}.pkl").write_bytes(pickle.dumps(pl, protocol=pickle.HIGHEST_PROTOCOL))\n    cnt += 1\nprint(cnt)\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'_build_tpc_plans_from_ast({sg_root}) failed:\n{res.stderr}\n{res.stdout}')
    return int(res.stdout.strip().splitlines()[-1])

def _build_tpc_pygen_fine(sg_root: Path, py_plan_dir: str) -> tuple[int, int, int]:
    code = f'\nimport marshal, pickle, sys\nfrom pathlib import Path\nsys.path.insert(0, {str(sg_root)!r})\nsys.path.insert(0, "/app")\nimport stage_store as rc, verifier\n\nfine_stack = []\nrestore_fine = rc._install_fine_spies(fine_stack)\ninv = {{}}\ncnt = 0\ntry:\n    for p in sorted(Path({py_plan_dir!r}).glob("*.pkl"), key=lambda x: int(x.stem)):\n        qi = int(p.stem)\n        plan = pickle.loads(p.read_bytes())\n        qf = set()\n        fine_stack.append(qf)\n        try:\n            verifier._hash_plan_py_code(plan)\n        finally:\n            fine_stack.pop()\n        for tok in qf:\n            inv.setdefault(tok, set()).add(qi)\n        cnt += 1\nfinally:\n    restore_fine()\n\npayload = {{tok: tuple(sorted(qs)) for tok, qs in inv.items()}}\nblob = marshal.dumps(payload)\nPath("/app/tpc_pygen_fine.marshal").write_bytes(blob)\nprint(f"{{cnt}} {{len(payload)}} {{len(blob)}}")\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'_build_tpc_pygen_fine failed:\n{res.stderr}\n{res.stdout}')
    parts = res.stdout.strip().splitlines()[-1].split()
    return (int(parts[0]), int(parts[1]), int(parts[2]))

def _build_pq_cj_tok(sg_root: str) -> tuple[int, int, int, int, int]:
    code = f'\nimport marshal, sqlite3, sys\nfrom pathlib import Path\nsys.path.insert(0, {sg_root!r})\nsys.path.insert(0, "/app")\nimport stage_store as rc, tests.helpers as th\nfrom sqlglot import Dialect, Dialects, exp, parse_one\nfrom sqlglot.optimizer import RULES, optimize\nfrom sqlglot.schema import ensure_schema\nfrom sqlglot.tokens import Tokenizer\n\n# 1. tpc_pq (deduplicated 14-stage optimizer checkpoints for all 57 TPC-DS execute queries)\ns_ds = ensure_schema(th.TPCDS_SCHEMA)\nout_pq = Path("/app/tpc_pq")\nout_pq.mkdir(parents=True, exist_ok=True)\nn_pq = 0\nfor i, (args, sql, _) in enumerate(th.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"), 1):\n    if args.get("execute"):\n        cur = parse_one(sql)\n        uniq_asts = []\n        sig_to_idx = {{}}\n        before_idxs = []\n        stage_sigs = []\n        ok = True\n        for rule in RULES:\n            bsig = rc.ast_struct_sig(cur, exp)\n            if bsig is None:\n                ok = False\n                break\n            bidx = sig_to_idx.get(bsig)\n            if bidx is None:\n                pk = rc.pack_expr(cur, exp)\n                if pk is rc._UNPACKABLE:\n                    ok = False\n                    break\n                bidx = len(uniq_asts)\n                uniq_asts.append(pk)\n                sig_to_idx[bsig] = bidx\n            before_idxs.append(bidx)\n            cur = optimize(cur, s_ds, rules=(rule,), leave_tables_isolated=True, copy=False)\n            asig = rc.ast_struct_sig(cur, exp)\n            if asig is None:\n                ok = False\n                break\n            stage_sigs.append(asig)\n        if ok:\n            fsig = stage_sigs[-1]\n            fidx = sig_to_idx.get(fsig)\n            if fidx is None:\n                pk = rc.pack_expr(cur, exp)\n                if pk is not rc._UNPACKABLE:\n                    fidx = len(uniq_asts)\n                    uniq_asts.append(pk)\n                    sig_to_idx[fsig] = fidx\n                else:\n                    ok = False\n        if ok:\n            (out_pq / f"{{i}}.ckp").write_bytes(\n                marshal.dumps((uniq_asts, tuple(before_idxs), tuple(stage_sigs), fidx))\n            )\n            (out_pq / f"{{i}}.ast").write_bytes(marshal.dumps(uniq_asts[before_idxs[2]]))\n            n_pq += 1\n\n# 2. cross_joins_pq (post-RULES[:6] ASTs for 48 test_conditional_cross_joins queries)\nschema = {{name: {{"a": "INT"}} for name in ("x", "y", "z")}}\nout_cj = {{}}\nfor criteria in (\n    "ON x.a = y.a",\n    "ON x.a < y.a",\n    "ON y.a = 3",\n    "ON x.a = y.a OR y.a IS NULL",\n    "ON FALSE",\n    "ON NULL",\n    "ON TRUE",\n    "USING(a)",\n):\n    queries = [\n        f"SELECT x.a, y.a FROM x CROSS JOIN y {{criteria}}",\n        f"SELECT x.a, y.a, z.a FROM x CROSS JOIN y {{criteria}} LEFT JOIN z ON y.a = z.a",\n    ]\n    if sqlite3.sqlite_version_info >= (3, 39, 0):\n        queries.append(\n            f"SELECT x.a, y.a, z.a FROM x CROSS JOIN y {{criteria}} RIGHT JOIN z ON y.a = z.a"\n        )\n    for sql in queries:\n        for dialect in ("sqlite", "mysql"):\n            ast6 = optimize(sql, schema=schema, dialect=dialect, rules=RULES[:6])\n            pk = rc.pack_expr(ast6, exp)\n            if pk is not rc._UNPACKABLE:\n                out_cj[(sql, dialect)] = pk\ncj_blob = marshal.dumps(out_cj)\nPath("/app/cross_joins_pq.marshal").write_bytes(cj_blob)\n\n# 3. tok_trie_cache (Tokenizer._KEYWORD_TRIE for all 34 baseline dialects)\ncache = {{}}\nall_tok_cls = {{Tokenizer}}\nfor d in Dialects:\n    di = Dialect.get_or_raise(d)\n    for c in di.tokenizer_class.__mro__:\n        if c.__module__.startswith("sqlglot.") and issubclass(c, Tokenizer):\n            all_tok_cls.add(c)\nfor c in all_tok_cls:\n    key = f"{{c.__module__}}.{{c.__qualname__}}"\n    trie = getattr(c, "_KEYWORD_TRIE", None)\n    if isinstance(trie, dict):\n        try:\n            marshal.dumps(trie)\n            cache[key] = trie\n        except Exception:\n            pass\ntok_blob = marshal.dumps(cache)\nPath("/app/tok_trie_cache.marshal").write_bytes(tok_blob)\nprint(f"{{n_pq}} {{len(out_cj)}} {{len(cj_blob)}} {{len(cache)}} {{len(tok_blob)}}")\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'_build_pq_cj_tok failed:\n{res.stderr}\n{res.stdout}')
    parts = res.stdout.strip().splitlines()[-1].split()
    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))

def _build_tpc_rows(sg_root: str, plan_dir: str) -> int:
    code = f'\nimport pickle, sys\nfrom pathlib import Path\nsys.path.insert(0, {sg_root!r})\nsys.path.insert(0, "/app")\nimport verifier, reach_map as ce, tests.helpers as th\nfrom sqlglot.executor.python import PythonExecutor\nfrom sqlglot.executor.table import Table, ensure_tables\n\nverifier._install_fast_python_executor(ce.PatchPlan(commit="cache", is_noop=False))\ntables = {{}}\nfor tname in th.TPCDS_SCHEMA:\n    ct, rs = pickle.loads(Path(f"/app/tpc_tables/tpcds/{{tname}}.pkl").read_bytes())\n    tables[tname] = Table(columns=ct, rows=list(rs))\nout = Path("/app/tpc_rows")\nout.mkdir(parents=True, exist_ok=True)\ncnt = 0\nfor p in sorted(Path({plan_dir!r}).glob("*.pkl"), key=lambda x: int(x.stem)):\n    qi = int(p.stem)\n    plan = pickle.loads(p.read_bytes())\n    tbl = PythonExecutor(tables=ensure_tables(tables)).execute(plan)\n    (out / f"{{qi}}.pkl").write_bytes(pickle.dumps((tuple(tbl.columns), list(tbl.rows)), protocol=pickle.HIGHEST_PROTOCOL))\n    cnt += 1\nprint(cnt)\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'_build_tpc_rows failed:\n{res.stderr}\n{res.stdout}')
    return int(res.stdout.strip().splitlines()[-1])

def _build_tpc_tables(sg_root: Path, th) -> tuple[int, int]:
    total_files = 0
    total_bytes = 0
    for tag, sub, schema in [('tpcds', 'tests/fixtures/optimizer/tpc-ds/', th.TPCDS_SCHEMA), ('tpch', 'tests/fixtures/optimizer/tpc-h/', th.TPCH_SCHEMA)]:
        out_dir = Path('/app/tpc_tables') / tag
        out_dir.mkdir(parents=True, exist_ok=True)
        for table, col_types in schema.items():
            fn = sg_root / f'{sub}{table}.csv.gz'
            if not fn.exists():
                continue
            with open(fn, 'rb') as f:
                is_gz = f.read(2) == b'\x1f\x8b'
            fh = gzip.open(fn, 'rt', newline='') if is_gz else open(fn, encoding='utf-8', newline='')
            with fh:
                reader = csv.reader(fh, delimiter='|')
                next(reader)
                rows = []
                ctypes: list[type] = []
                for row in reader:
                    if not ctypes:
                        for v in row:
                            try:
                                ctypes.append(type(ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(tuple((None if t is not str and v == '' else t(v) for t, v in zip(ctypes, row))))
            blob = pickle.dumps((col_types, rows), protocol=pickle.HIGHEST_PROTOCOL)
            (out_dir / f'{table}.pkl').write_bytes(blob)
            total_files += 1
            total_bytes += len(blob)
    return (total_files, total_bytes)

def _build_duckdb_files_subproc(sg_root: Path) -> None:
    code = f"""\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, {str(sg_root)!r})\nimport duckdb\nimport tests.helpers as th\n\nsg_root = Path({str(sg_root)!r})\nfor db_name, sub, schema in [\n    ("/app/duckdb_tpcds.db", "tests/fixtures/optimizer/tpc-ds/", th.TPCDS_SCHEMA),\n    ("/app/duckdb_tpch.db", "tests/fixtures/optimizer/tpc-h/", th.TPCH_SCHEMA),\n]:\n    p = Path(db_name)\n    if p.exists():\n        p.unlink()\n    conn = duckdb.connect(str(p), config={{"threads": 1}})\n    try:\n        for table, col_types in schema.items():\n            fn = sg_root / f"{{sub}}{{table}}.csv.gz"\n            if fn.exists():\n                conn.execute(\n                    f"CREATE TABLE {{table}} AS SELECT * FROM READ_CSV('{{fn}}', delim='|', header=True, columns={{col_types}})"\n                )\n        conn.execute("CHECKPOINT")\n    finally:\n        conn.close()\n\nopt_p = Path("/app/duckdb_opt.db")\nif opt_p.exists():\n    opt_p.unlink()\nconn = duckdb.connect(str(opt_p), config={{"threads": 1}})\ntry:\n    conn.execute(\n        '''\n        CREATE TABLE x (a INT, b INT);\n        CREATE TABLE y (b INT, c INT);\n        CREATE TABLE z (b INT, c INT);\n        CREATE TABLE w (d TEXT, e TEXT);\n        INSERT INTO x VALUES (1, 1), (2, 2), (2, 2), (3, 3), (null, null);\n        INSERT INTO y VALUES (2, 2), (2, 2), (3, 3), (4, 4), (null, null);\n        INSERT INTO y VALUES (3, 3), (3, 3), (4, 4), (5, 5), (null, null);\n        INSERT INTO w VALUES ('a', 'b');\n        CREATE TABLE unpivotable (id INT, jan INT, feb INT, north INT, south INT);\n        INSERT INTO unpivotable VALUES (1, 100, 200, 7, 8), (2, 300, 400, 9, 10);\n        CREATE TABLE pivotable (id INT, cat TEXT, val INT, kind TEXT, amt INT);\n        INSERT INTO pivotable VALUES (1, 'a', 10, 'x', 5), (1, 'b', 20, 'x', 5);\n        CREATE TABLE comparisons AS\n        SELECT * FROM (VALUES (1), (2), (NULL)) AS x(a)\n        CROSS JOIN (VALUES (1), (2), (NULL)) AS y(b)\n        CROSS JOIN (VALUES (TRUE), (FALSE), (NULL)) AS z(c);\n        CHECKPOINT;\n        '''\n    )\nfinally:\n    conn.close()\n"""
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'_build_duckdb_files_subproc failed:\n{res.stderr}\n{res.stdout}')

def _build_local_io_bundle(sg_root: Path) -> tuple[int, int, int, int, int, int, int, int, int, int, int]:
    raw_root = Path('/app/maps_raw')
    raw_root.mkdir(parents=True, exist_ok=True)
    maps_src = Path('/app/maps')
    n_raw = 0
    if (maps_src / 'core_maps.zlib').is_file():
        core_obj = marshal.loads(zlib.decompress((maps_src / 'core_maps.zlib').read_bytes()))
        sm = core_obj.get('static_ast_meta')
        if isinstance(sm, dict) and (not sm.get('_preconverted')):
            sm['rev_import_graph'] = {k: frozenset(v) for k, v in sm['rev_import_graph'].items()}
            cdg = sm['class_dep_graph']
            sm['class_dep_graph'] = {'class_children': {k: frozenset(v) for k, v in cdg['class_children'].items()}, 'attr_copiers': {tuple(k): frozenset((tuple(x) for x in v)) for k, v in cdg['attr_copiers'].items()}, 'class_aliases': {k: frozenset(v) for k, v in cdg['class_aliases'].items()}, 'class_copiers': {k: frozenset(v) for k, v in cdg['class_copiers'].items()}, 'attr_mutators': {k: frozenset(v) for k, v in cdg['attr_mutators'].items()}, 'all_class_body_refs': frozenset(cdg['all_class_body_refs'])}
            sm['non_runtime_info'] = {k: (frozenset(v[0]), frozenset(v[1])) for k, v in sm['non_runtime_info'].items()}
            sm['import_time_called'] = {k: frozenset(v) for k, v in sm['import_time_called'].items()}
            sm['import_time_refs'] = {k: frozenset(v) for k, v in sm['import_time_refs'].items()}
            sm['_preconverted'] = True
        for _sk in ('dialect_meta_attrs', 'expr_meta_attrs', 'pure_enums'):
            if _sk in core_obj and (not isinstance(core_obj[_sk], frozenset)):
                core_obj[_sk] = frozenset(core_obj[_sk])
        if 'strict_enum_attrs' in core_obj and isinstance(core_obj['strict_enum_attrs'], dict):
            core_obj['strict_enum_attrs'] = {k: frozenset(v) for k, v in core_obj['strict_enum_attrs'].items()}
        (raw_root / 'core_maps.marshal').write_bytes(marshal.dumps(core_obj))
        n_raw += 1
    if (maps_src / 'dict_maps.zlib').is_file():
        (raw_root / 'dict_maps.marshal').write_bytes(zlib.decompress((maps_src / 'dict_maps.zlib').read_bytes()))
        n_raw += 1
    if (maps_src / 'cmp_agree.zlib').is_file():
        cmp_obj = frozenset(marshal.loads(zlib.decompress((maps_src / 'cmp_agree.zlib').read_bytes())))
        (raw_root / 'cmp_agree.marshal').write_bytes(marshal.dumps(cmp_obj))
        n_raw += 1
    for sub in ('subtests', 'line_cov'):
        sdir = maps_src / sub
        if sdir.is_dir():
            ddir = raw_root / sub
            ddir.mkdir(parents=True, exist_ok=True)
            for zp in sdir.glob('*.zlib'):
                (ddir / (zp.stem + '.marshal')).write_bytes(zlib.decompress(zp.read_bytes()))
                n_raw += 1
    compiled_roots: dict[str, list[str]] = {}
    for root_str in ('/sqlglot-c', '/sqlglot-c-noopt', '/sqlglot-c-base'):
        r_p = Path(root_str)
        sg_c = r_p / 'sqlglot'
        if sg_c.is_dir():
            out_py: list[str] = []
            for p in sg_c.rglob('*.so'):
                rel = p.relative_to(r_p).as_posix()
                stem_part = rel.split('.cpython-', 1)[0]
                if stem_part.endswith('__mypyc'):
                    continue
                out_py.append(stem_part + '.py')
            compiled_roots[root_str] = sorted(out_py)
    Path('/app/compiled_roots.marshal').write_bytes(marshal.dumps(compiled_roots))
    import diff_scan as pa
    n_ast_f, n_ast_u, ast_bytes = pa.build_base_ast_index(sg_root)
    import reach_map as ce
    core_maps_loaded = marshal.loads((raw_root / 'core_maps.marshal').read_bytes())
    eng = ce.ReachPlanner(core_maps_loaded, [], sg_root)
    gi_out: dict[str, Any] = {}
    for attr, readers in core_maps_loaded.get('attr_readers', {}).items():
        if 1 <= len(readers) <= 4:
            gi_out[attr] = eng._guarded_isinstance_exprs(attr, readers)
    gi_blob = marshal.dumps(gi_out)
    Path('/app/guarded_isinstance.marshal').write_bytes(gi_blob)
    sys.path.insert(0, str(sg_root))
    import tests.helpers as th
    fixtures_root = sg_root / 'tests/fixtures'
    fixture_pairs: dict[str, list[tuple[dict, str, str]]] = {}
    fixture_singles: dict[str, list[str]] = {}
    for sql_file in sorted(fixtures_root.rglob('*.sql')):
        rel_under = str(sql_file.relative_to(fixtures_root))
        try:
            singles = list(th.load_sql_fixtures(rel_under))
            if singles:
                fixture_singles[rel_under] = singles
        except Exception:
            pass
        try:
            pairs = list(th.load_sql_fixture_pairs(rel_under))
            if pairs:
                fixture_pairs[rel_under] = pairs
        except Exception:
            pass
    fix_blob = marshal.dumps({'fixture_pairs': fixture_pairs, 'fixture_singles': fixture_singles})
    Path('/app/fixture_cache.marshal').write_bytes(fix_blob)
    n_tbl, tbl_bytes = _build_tpc_tables(sg_root, th)
    return (n_raw, len(fixture_pairs), len(fixture_singles), len(fix_blob), n_tbl, tbl_bytes, n_ast_f, n_ast_u, ast_bytes, len(gi_out), len(gi_blob))

def _run_dep_index_subproc(sg_root: Path, n_workers: int) -> tuple[int, int, int, int, int]:
    code = f'\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, "/app")\nimport attr_deps as di\nres = di.build_dep_index(Path({str(sg_root)!r}), n_workers={n_workers})\nprint(" ".join(str(x) for x in res))\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'build_dep_index failed:\n{res.stderr}\n{res.stdout}')
    for ln in res.stdout.strip().splitlines()[:-1]:
        print(ln, flush=True)
    parts = res.stdout.strip().splitlines()[-1].split()
    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]))

def _run_val_replay_subproc(sg_root: Path, n_workers: int) -> tuple[int, int, int]:
    code = f'\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, "/app")\nimport stage_store as rc\nres = rc.build_val_replay(Path({str(sg_root)!r}), n_workers={n_workers})\nprint(" ".join(str(x) for x in res))\n'
    res = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'build_val_replay failed:\n{res.stderr}\n{res.stdout}')
    parts = res.stdout.strip().splitlines()[-1].split()
    return (int(parts[0]), int(parts[1]), int(parts[2]))

def main() -> None:
    t0 = time.perf_counter()
    sg_root = Path('/sqlglot')
    fast_root_str, master_tag = _pick_fast_root()
    master_plan_dir = f'/app/tpc_cache_{master_tag}_plan'
    n_cpus = min(4, max(1, os.cpu_count() or 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        f_duck = pool.submit(_build_duckdb_files_subproc, sg_root)
        f_master = pool.submit(_build_tpc_opt_master, fast_root_str, master_plan_dir)
        f_pq = pool.submit(_build_pq_cj_tok, fast_root_str)
        io_res = _build_local_io_bundle(sg_root)
        f_duck.result()
        n_master = f_master.result()
        n_pq, n_cj, cj_bytes, n_tok, tok_bytes = f_pq.result()
    n_raw, n_fix_p, n_fix_s, fix_bytes, n_tbl, tbl_bytes, n_ast_f, n_ast_u, ast_bytes, n_gi, gi_bytes = io_res
    t_p1 = time.perf_counter() - t0
    import stage_store as rc
    opt_done = threading.Event()

    def _run_opt_replay():
        try:
            return rc.build_opt_replay_and_duckdb_cache(sg_root)
        finally:
            opt_done.set()

    def _run_secondary_pipeline():
        other_roots: list[tuple[str, str, str]] = []
        for tag, r_str in (('py', '/sqlglot'), ('c', '/sqlglot-c'), ('noopt', '/sqlglot-c-noopt'), ('cb', '/sqlglot-c-base')):
            if tag == master_tag:
                continue
            if Path(r_str, 'sqlglot').is_dir():
                other_roots.append((tag, r_str, f'/app/tpc_cache_{tag}_plan'))

        def _build_other_plans_and_pygen() -> tuple[dict[str, int], tuple[int, int, int]]:
            out_cnt: dict[str, int] = {master_tag: n_master}
            for tag, r_str, pdir in other_roots:
                out_cnt[tag] = _build_tpc_plans_from_ast(r_str, pdir)
            pygen_res = _build_tpc_pygen_fine(sg_root, '/app/tpc_cache_py_plan')
            return (out_cnt, pygen_res)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as sub_pool:
            f_exec = sub_pool.submit(rc.build_exec_and_sqlite_cache, sg_root)
            f_rows = sub_pool.submit(_build_tpc_rows, fast_root_str, master_plan_dir)
            f_plans = sub_pool.submit(_build_other_plans_and_pygen)
            exec_res = f_exec.result()
            n_rows_r = f_rows.result()
            plan_counts, pygen_res = f_plans.result()
        dep_w = n_cpus if opt_done.is_set() else max(1, n_cpus - 1)
        dep_res = _run_dep_index_subproc(sg_root, dep_w)
        val_res = _run_val_replay_subproc(sg_root, n_cpus)
        return (exec_res, n_rows_r, plan_counts, pygen_res, dep_res, val_res)
    t_p2 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p2_pool:
        f_opt = p2_pool.submit(_run_opt_replay)
        f_sec = p2_pool.submit(_run_secondary_pipeline)
        orows, obytes, dq, dbytes = f_opt.result()
        exec_res, n_rows, plan_counts, pygen_res, dep_res, val_res = f_sec.result()
    erows, ebytes, sq, sbytes = exec_res
    n_pyg_q, n_pyg_toks, pyg_bytes = pygen_res
    d_seen, d_keep, d_attrs, d_dicts, d_bytes = dep_res
    vp, ve, vb = val_res
    print(f"[prebuild] raw_maps={n_raw}, base_ast={n_ast_f}f/{n_ast_u}u ({ast_bytes / 1024:.0f} KB), guarded_is={n_gi} ({gi_bytes / 1024:.1f} KB), tok_tries={n_tok} ({tok_bytes / 1024:.0f} KB), fixtures={n_fix_p}p/{n_fix_s}s ({fix_bytes / 1024:.1f} KB), tpc_tables={n_tbl} ({tbl_bytes / 1024:.1f} KB), tpc_plans py={plan_counts.get('py', 0)} c={plan_counts.get('c', 0)} noopt={plan_counts.get('noopt', 0)} cb={plan_counts.get('cb', 0)} rows={n_rows} pq={n_pq} cj={n_cj} ({cj_bytes / 1024:.0f} KB) pygen={n_pyg_q}q/{n_pyg_toks}t ({pyg_bytes / 1024:.1f} KB), attr_deps={d_seen}t/{d_keep}k ({d_bytes / 1024:.0f} KB), val_replay={vp}p/{ve}e ({vb / 1024:.0f} KB), opt_replay={orows}r ({obytes / 1024:.0f} KB), duckdb={dq}q ({dbytes / 1024:.1f} KB), exec_replay={erows}r ({ebytes / 1024:.0f} KB), sqlite={sq}q ({sbytes / 1024:.1f} KB) [p1={t_p1:.1f}s p2={time.perf_counter() - t_p2:.1f}s] total={time.perf_counter() - t0:.1f}s", flush=True)
if __name__ == '__main__':
    main()
