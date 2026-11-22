"""Verification: run each commit's candidate tests on the patched tree and report failures and unchecked tests."""
from __future__ import annotations
import ast
import builtins
import copy
import gc
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import io
import json
import math
import os
import pickle
import signal
import sys
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from reach_map import PatchPlan
from worker_pack import fast_cost_ms, lb_cost_ms, max_total_for_cost_ms, optimal_cost_ms
import attr_deps as _di
import stage_store as rc
try:
    import _joinkit as _FJC
    _FJC_OPEN_LOOKUP = getattr(_FJC, 'fast_open_lookup', None)
    _FJC_COMPARE_ROWS = getattr(_FJC, 'fast_compare_rows', None)
except Exception:
    _FJC_OPEN_LOOKUP = None
    _FJC_COMPARE_ROWS = None

class _FailFast(BaseException):
    pass

class _TestTimeout(BaseException):
    pass

class _EconomicAbort(BaseException):
    pass
TIMEOUT_FIRED: bool = False
LAST_COMMIT_HAD_TIMEOUT: bool = False

def _alarm_handler(signum: int, frame: Any) -> None:
    global TIMEOUT_FIRED
    TIMEOUT_FIRED = True
    raise _TestTimeout()

class _InMemoryLoader(importlib.abc.Loader):

    def __init__(self, source: str, filepath: str, is_pkg: bool) -> None:
        self.source = source
        self.filepath = filepath
        self.is_pkg = is_pkg

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        module.__file__ = self.filepath
        if self.is_pkg:
            module.__path__ = [os.path.dirname(self.filepath)]
        code = compile(self.source.encode('utf-8'), self.filepath, 'exec')
        exec(code, module.__dict__)

class _DeletedModuleLoader(importlib.abc.Loader):

    def __init__(self, fullname: str) -> None:
        self.fullname = fullname

    def create_module(self, spec: Any) -> Any:
        return None

    def exec_module(self, module: Any) -> None:
        raise ModuleNotFoundError(f'No module named {self.fullname!r}', name=self.fullname)

class _InMemoryFinder(importlib.abc.MetaPathFinder):

    def __init__(self, patched_files: dict[str, str], sqlglot_root: Path, deleted_files: set[str] | None=None) -> None:
        self.sqlglot_root = sqlglot_root
        self.mod_map: dict[str, tuple[str, str, bool]] = {}
        self.deleted_mods: set[str] = set()
        for rel in deleted_files or ():
            if not rel.endswith('.py'):
                continue
            mod = rel.removesuffix('/__init__.py').replace('/', '.') if rel.endswith('/__init__.py') else rel.removesuffix('.py').replace('/', '.')
            self.deleted_mods.add(mod)
        for rel, src in patched_files.items():
            if not rel.endswith('.py'):
                continue
            full = str(sqlglot_root / rel)
            if rel.endswith('/__init__.py'):
                mod = rel.removesuffix('/__init__.py').replace('/', '.')
                self.mod_map[mod] = (src, full, True)
            else:
                mod = rel.removesuffix('.py').replace('/', '.')
                self.mod_map[mod] = (src, full, False)

    def find_spec(self, fullname: str, path: Any=None, target: Any=None) -> Any:
        if fullname in self.deleted_mods:
            return importlib.machinery.ModuleSpec(fullname, _DeletedModuleLoader(fullname))
        entry = self.mod_map.get(fullname)
        if entry is None:
            return None
        src, full, is_pkg = entry
        loader = _InMemoryLoader(src, full, is_pkg)
        spec = importlib.machinery.ModuleSpec(fullname, loader, origin=full, is_package=is_pkg)
        if is_pkg:
            spec.submodule_search_locations = [os.path.dirname(full)]
        return spec

def _install_open_overlay(patched_files: dict[str, str], sqlglot_root: Path, deleted_files: set[str] | None=None):
    import errno as _errno
    orig_open = builtins.open
    orig_io_open = io.open
    orig_open_code = io.open_code
    orig_path_open = Path.open
    orig_listdir = os.listdir
    abs_map: dict[str, str] = {os.path.realpath(str(sqlglot_root / rel)): content for rel, content in patched_files.items()}
    del_set: set[str] = {os.path.realpath(str(sqlglot_root / rel)) for rel in deleted_files or ()}
    basenames_tuple: tuple[str, ...] = tuple({os.path.basename(k) for k in abs_map} | {os.path.basename(k) for k in del_set})
    basenames_set: frozenset[str] = frozenset(basenames_tuple)
    added_by_dir: dict[str, set[str]] = {}
    deleted_by_dir: dict[str, set[str]] = {}
    for _ap in abs_map:
        _p = Path(_ap)
        if not _p.exists():
            added_by_dir.setdefault(str(_p.parent), set()).add(_p.name)
    for _dp in del_set:
        _p = Path(_dp)
        deleted_by_dir.setdefault(str(_p.parent), set()).add(_p.name)
    overlay_dirs = set(added_by_dir) | set(deleted_by_dir)

    def _overlay_listdir(path='.'):
        res = orig_listdir(path)
        if not overlay_dirs or isinstance(path, int):
            return res
        try:
            rp = os.path.realpath(os.fspath(path))
            if isinstance(rp, bytes):
                rp = os.fsdecode(rp)
        except Exception:
            return res
        if rp not in overlay_dirs:
            return res
        del_names = deleted_by_dir.get(rp)
        add_names = added_by_dir.get(rp)
        is_bytes = isinstance(path, bytes)
        if is_bytes:
            out = [x for x in res if not (del_names and x.decode('utf-8', 'surrogateescape') in del_names)]
            if add_names:
                seen = set(out)
                for n in sorted(add_names):
                    nb = n.encode('utf-8', 'surrogateescape')
                    if nb not in seen:
                        out.append(nb)
            return out
        out_s = [x for x in res if not (del_names and x in del_names)]
        if add_names:
            seen_s = set(out_s)
            for n in sorted(add_names):
                if n not in seen_s:
                    out_s.append(n)
        return out_s
    _c_lookup = _FJC_OPEN_LOOKUP
    _realpath = os.path.realpath
    _fspath = os.fspath

    def _lookup_fallback(file: Any) -> tuple[bool, str | None]:
        if not isinstance(file, (str, os.PathLike)):
            return (False, None)
        s = _fspath(file)
        if not isinstance(s, str):
            return (False, None)
        if del_set and s in del_set:
            return (True, s)
        data = abs_map.get(s)
        if data is not None:
            return (False, data)
        if not basenames_set:
            return (False, None)
        if not s or s.endswith('/') or s.startswith('/tmp/'):
            pass
        else:
            bname = s.rsplit('/', 1)[-1]
            if bname not in ('.', '..') and bname not in basenames_set:
                return (False, None)
        try:
            r = _realpath(s)
            if del_set and r in del_set:
                return (True, s)
            return (False, abs_map.get(r))
        except Exception:
            return (False, None)
    if _c_lookup is not None:

        def _overlay_open(file, mode='r', *args, **kwargs):
            if 'w' not in mode and 'a' not in mode and ('x' not in mode):
                res = _c_lookup(file, abs_map, del_set, basenames_tuple)
                if res is None:
                    return orig_open(file, mode, *args, **kwargs)
                if res is True:
                    raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), _fspath(file))
                if res is False:
                    s = _fspath(file)
                    try:
                        r = _realpath(s)
                    except Exception:
                        r = ''
                    if r in del_set:
                        raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), s)
                    res = abs_map.get(r)
                    if res is None:
                        return orig_open(file, mode, *args, **kwargs)
                if 'b' in mode:
                    return io.BytesIO(res.encode('utf-8'))
                return io.StringIO(res)
            return orig_open(file, mode, *args, **kwargs)

        def _overlay_open_code(path):
            res = _c_lookup(path, abs_map, del_set, basenames_tuple)
            if res is None:
                return orig_open_code(path)
            if res is True:
                raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), _fspath(path))
            if res is False:
                s = _fspath(path)
                try:
                    r = _realpath(s)
                except Exception:
                    r = ''
                if r in del_set:
                    raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), s)
                res = abs_map.get(r)
                if res is None:
                    return orig_open_code(path)
            return io.BytesIO(res.encode('utf-8'))

        def _overlay_path_open(self, mode='r', *args, **kwargs):
            if 'w' not in mode and 'a' not in mode and ('x' not in mode):
                res = _c_lookup(self, abs_map, del_set, basenames_tuple)
                if res is None:
                    return orig_path_open(self, mode, *args, **kwargs)
                if res is True:
                    raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), _fspath(self))
                if res is False:
                    s = _fspath(self)
                    try:
                        r = _realpath(s)
                    except Exception:
                        r = ''
                    if r in del_set:
                        raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), s)
                    res = abs_map.get(r)
                    if res is None:
                        return orig_path_open(self, mode, *args, **kwargs)
                if 'b' in mode:
                    return io.BytesIO(res.encode('utf-8'))
                return io.StringIO(res)
            return orig_path_open(self, mode, *args, **kwargs)
    else:

        def _overlay_open(file, mode='r', *args, **kwargs):
            if 'w' not in mode and 'a' not in mode and ('x' not in mode):
                is_del, data = _lookup_fallback(file)
                if is_del:
                    raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), data)
                if data is not None:
                    if 'b' in mode:
                        return io.BytesIO(data.encode('utf-8'))
                    return io.StringIO(data)
            return orig_open(file, mode, *args, **kwargs)

        def _overlay_open_code(path):
            is_del, data = _lookup_fallback(path)
            if is_del:
                raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), data)
            if data is not None:
                return io.BytesIO(data.encode('utf-8'))
            return orig_open_code(path)

        def _overlay_path_open(self, mode='r', *args, **kwargs):
            if 'w' not in mode and 'a' not in mode and ('x' not in mode):
                is_del, data = _lookup_fallback(self)
                if is_del:
                    raise FileNotFoundError(_errno.ENOENT, os.strerror(_errno.ENOENT), data)
                if data is not None:
                    if 'b' in mode:
                        return io.BytesIO(data.encode('utf-8'))
                    return io.StringIO(data)
            return orig_path_open(self, mode, *args, **kwargs)
    builtins.open = _overlay_open
    io.open = _overlay_open
    io.open_code = _overlay_open_code
    Path.open = _overlay_path_open
    if overlay_dirs:
        os.listdir = _overlay_listdir

    def _restore() -> None:
        builtins.open = orig_open
        io.open = orig_io_open
        io.open_code = orig_open_code
        Path.open = orig_path_open
        if overlay_dirs:
            os.listdir = orig_listdir
    return _restore

def _create_disk_overlay_for_subprocess(patched_files: dict[str, str], sqlglot_root: Path, deleted_files: set[str] | None=None) -> Path:
    import tempfile
    ov = Path(tempfile.mkdtemp(prefix='sg_ov_', dir='/tmp'))
    del_set = deleted_files or set()
    patched_dirs: set[str] = set()
    for rel in list(patched_files) + list(del_set):
        p = Path(rel)
        for parent in p.parents:
            if str(parent) != '.':
                patched_dirs.add(parent.as_posix())

    def _mirror(rel_dir: str) -> None:
        src_dir = sqlglot_root / rel_dir if rel_dir else sqlglot_root
        dst_dir = ov / rel_dir if rel_dir else ov
        dst_dir.mkdir(parents=True, exist_ok=True)
        for entry in src_dir.iterdir():
            if entry.name.startswith('.'):
                continue
            entry_rel = f'{rel_dir}/{entry.name}' if rel_dir else entry.name
            if entry_rel in del_set:
                continue
            dst = dst_dir / entry.name
            if entry_rel in patched_files:
                dst.write_text(patched_files[entry_rel], encoding='utf-8')
            elif entry.is_dir() and entry_rel in patched_dirs:
                _mirror(entry_rel)
            else:
                os.symlink(entry, dst)
    _mirror('')
    for rel, content in patched_files.items():
        if rel in del_set:
            continue
        dst = ov / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(content, encoding='utf-8')
    return ov
ACTIVE_FIXTURE_FILTERS: dict[str, set[int] | None] = {}
ACTIVE_LAST_PAIR_IDX: int = 0
LAST_FAILED_TPCDS_QIDX: int = 0
ACTIVE_GEN_MEM_DIALECTS: set[str] | None = None
ACTIVE_VAL_CALLS: set[int] | None = None
ACTIVE_VAL_COUNTER: int = 0
ACTIVE_VAL_DEPTH: int = 0
ACTIVE_VAL_FILE: str = ''
ACTIVE_VAL_RET_USED_LINES: frozenset[int] | None = None
ACTIVE_TEST_ID: str = ''
ACTIVE_REPLAY_SCOPE: rc.ReplayScope | None = None
ACTIVE_INLINE_OPT_DEPTH: int = 0
VAL_FAILED_INSIDE: bool = False
IN_ASSERT_CTX: int = 0
ACTIVE_COMMIT_PLAN: PatchPlan | None = None
ACTIVE_ENGINE: Any = None
ACTIVE_TNUM: int = -1
ACTIVE_CPU_BUDGET_SEC: float | None = None
ACTIVE_PATCHED_TRIE_MODS: frozenset[str] = frozenset()
ACTIVE_RS_SLIM_OK: bool = False
ECON_CPU_LIMIT: float | None = None
ECON_ABORTED: bool = False

def _econ_abort_now() -> None:
    global ECON_ABORTED, ECON_CPU_LIMIT
    ECON_ABORTED = True
    ECON_CPU_LIMIT = None
    raise _EconomicAbort()

def _econ_check() -> None:
    lim = ECON_CPU_LIMIT
    if lim is not None and time.process_time() > lim:
        _econ_abort_now()

def _hash_df(df) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(repr(list(df.columns)).encode())
    h.update(str(len(df)).encode())
    for row in df.itertuples(index=False, name=None):
        h.update(repr(row).encode())
    return h.hexdigest()

def _hash_table_rows(columns: Any, rows: Any) -> str:
    h = hashlib.blake2b(digest_size=12)
    cols = tuple(columns)
    h.update(repr(cols).encode())
    h.update(b'|')
    h.update(str(len(rows)).encode())
    for row in rows:
        h.update(b';')
        for v in row:
            h.update(type(v).__name__.encode())
            h.update(b':')
            h.update(repr(v).encode())
            h.update(b',')
    return h.hexdigest()

def _hash_expr_types(expr: Any) -> str:
    h = hashlib.blake2b(digest_size=12)
    for idx, node in enumerate(expr.walk()):
        nt = getattr(node, '_type', None)
        tsql = nt.sql() if nt is not None else ''
        args = getattr(node, 'args', None) or {}
        h.update(f"{idx}:{node.key}:{tsql}:{args.get('typed')}:{args.get('safe')}|".encode())
    return h.hexdigest()

def _hash_plan_det(plan: Any) -> str:
    from sqlglot import exp
    from sqlglot.planner import Step
    memo: dict[int, str] = {}

    def _hash_obj(obj: Any, h: Any) -> None:
        if isinstance(obj, Step):
            h.update(b'STEP:')
            h.update(_step_hash(obj).encode())
        elif isinstance(obj, exp.Expr):
            h.update(b'EXP:')
            h.update(obj.sql().encode())
            h.update(b'#')
            h.update(_hash_expr_types(obj).encode())
        elif isinstance(obj, dict):
            h.update(b'DICT{')
            for k in sorted(obj.keys(), key=lambda x: repr(x)):
                _hash_obj(k, h)
                h.update(b'=')
                _hash_obj(obj[k], h)
                h.update(b',')
            h.update(b'}')
        elif isinstance(obj, (list, tuple)):
            h.update(b'SEQ[')
            for item in obj:
                _hash_obj(item, h)
                h.update(b',')
            h.update(b']')
        elif isinstance(obj, (set, frozenset)):
            h.update(b'SET{')
            sub_hashes: list[str] = []
            for item in obj:
                sh = hashlib.blake2b(digest_size=12)
                _hash_obj(item, sh)
                sub_hashes.append(sh.hexdigest())
            sub_hashes.sort()
            h.update(','.join(sub_hashes).encode())
            h.update(b'}')
        else:
            h.update(type(obj).__name__.encode())
            h.update(b':')
            h.update(repr(obj).encode())

    def _step_hash(step: Any) -> str:
        sid = id(step)
        cached = memo.get(sid)
        if cached is not None:
            return cached
        sh = hashlib.blake2b(digest_size=12)
        sh.update(type(step).__name__.encode())
        sh.update(b'|')
        for k in sorted(step.__dict__.keys()):
            if k == 'dependents' or k.startswith('_'):
                continue
            sh.update(k.encode())
            sh.update(b'=')
            _hash_obj(step.__dict__[k], sh)
            sh.update(b';')
        digest = sh.hexdigest()
        memo[sid] = digest
        return digest
    top = hashlib.blake2b(digest_size=12)
    top.update(str(len(plan.dag)).encode())
    top.update(b'|')
    _hash_obj(plan.ctes, top)
    top.update(b'|')
    top.update(_step_hash(plan.root).encode())
    return top.hexdigest()

def _hash_plan_py_code(plan: Any) -> str:
    from sqlglot import exp
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.planner import Step
    pe = PythonExecutor()
    pe._ctes = plan.ctes
    memo: dict[int, str] = {}
    subplans_seen: set[str] = set()

    def _gen_expr(expr: Any) -> str:
        rep = pe._replace_subqueries(expr)
        return pe.generator.generate(rep)

    def _hash_obj(obj: Any, h: Any) -> None:
        if isinstance(obj, Step):
            h.update(b'S:')
            h.update(_step_py_hash(obj).encode())
        elif isinstance(obj, exp.Expr):
            h.update(b'P:')
            h.update(_gen_expr(obj).encode())
        elif isinstance(obj, dict):
            h.update(b'D{')
            for k in sorted(obj.keys(), key=lambda x: repr(x)):
                _hash_obj(k, h)
                h.update(b'=')
                _hash_obj(obj[k], h)
                h.update(b',')
            h.update(b'}')
        elif isinstance(obj, (list, tuple)):
            h.update(b'L[')
            for item in obj:
                _hash_obj(item, h)
                h.update(b',')
            h.update(b']')
        elif isinstance(obj, (set, frozenset)):
            h.update(b'T{')
            subs: list[str] = []
            for item in obj:
                sh = hashlib.blake2b(digest_size=12)
                _hash_obj(item, sh)
                subs.append(sh.hexdigest())
            subs.sort()
            h.update(','.join(subs).encode())
            h.update(b'}')
        else:
            h.update(repr(obj).encode())

    def _step_py_hash(step: Any) -> str:
        sid = id(step)
        cached = memo.get(sid)
        if cached is not None:
            return cached
        sh = hashlib.blake2b(digest_size=12)
        sh.update(type(step).__name__.encode())
        sh.update(b'|')
        for k in sorted(step.__dict__.keys()):
            if k == 'dependents' or k.startswith('_'):
                continue
            sh.update(k.encode())
            sh.update(b'=')
            _hash_obj(step.__dict__[k], sh)
            sh.update(b';')
        digest = sh.hexdigest()
        memo[sid] = digest
        return digest
    top = hashlib.blake2b(digest_size=12)
    top.update(_step_py_hash(plan.root).encode())
    while True:
        new_names = sorted((k for k in pe._subquery_plans if k not in subplans_seen))
        if not new_names:
            break
        for nm in new_names:
            subplans_seen.add(nm)
            top.update(b'|SUB:')
            top.update(nm.encode())
            top.update(b'=')
            sub_entry = pe._subquery_plans[nm]
            sub_plan = sub_entry[0] if isinstance(sub_entry, tuple) else sub_entry
            top.update(_step_py_hash(sub_plan.root).encode())
    return top.hexdigest()

class _LazyExecutorTables(dict):

    def __init__(self, directory: str, schema: dict[str, dict[str, str]], tag: str='', touched_files: set[str] | None=None, allow_pkl: bool=True) -> None:
        super().__init__()
        self._dir = directory
        self._schema = schema
        self._tag = tag
        self._touched = touched_files or set()
        self._allow_pkl = allow_pkl
        self._load_cpu = 0.0

    def __getitem__(self, table: str) -> Any:
        if super().__contains__(table):
            return super().__getitem__(table)
        _t0 = time.process_time()
        from sqlglot.executor.table import Table
        if self._allow_pkl and self._tag:
            sub = 'tpc-ds' if self._tag == 'tpcds' else 'tpc-h'
            rel_csv = f'tests/fixtures/optimizer/{sub}/{table}.csv.gz'
            if rel_csv not in self._touched:
                pkl_p = Path(f'/app/tpc_tables/{self._tag}/{table}.pkl')
                if pkl_p.exists():
                    try:
                        col_types, rows = pickle.loads(pkl_p.read_bytes())
                        tbl = Table(columns=col_types, rows=list(rows))
                        super().__setitem__(table, tbl)
                        self._load_cpu += time.process_time() - _t0
                        return tbl
                    except Exception:
                        pass
        import csv
        import gzip
        file_name = f'{self._dir}{table}.csv.gz'
        col_types = self._schema[table]
        with open(file_name, 'rb') as f:
            is_gz = f.read(2) == b'\x1f\x8b'
        fh = gzip.open(file_name, 'rt', newline='') if is_gz else open(file_name, encoding='utf-8', newline='')
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
        tbl = Table(columns=col_types, rows=rows)
        super().__setitem__(table, tbl)
        self._load_cpu += time.process_time() - _t0
        return tbl

    def __contains__(self, key: object) -> bool:
        return key in self._schema

def _duckdb_connect_1t(*args, **kwargs):
    import duckdb
    if 'config' not in kwargs:
        kwargs['config'] = {'threads': 1}
    return duckdb.connect(*args, **kwargs)
_EAGER_TD_FILES = frozenset({'sqlglot/dialects/bigquery.py', 'sqlglot/parsers/bigquery.py', 'sqlglot/generators/bigquery.py', 'sqlglot/dialects/hive.py', 'sqlglot/parsers/hive.py', 'sqlglot/generators/hive.py', 'sqlglot/dialects/snowflake.py', 'sqlglot/parsers/snowflake.py', 'sqlglot/generators/snowflake.py', 'sqlglot/dialects/spark2.py', 'sqlglot/parsers/spark2.py', 'sqlglot/generators/spark2.py', 'sqlglot/dialects/duckdb.py', 'sqlglot/parsers/duckdb.py', 'sqlglot/generators/duckdb.py'})
_TD_DEFERRED_LINES = ('from sqlglot.dialects import BigQuery, Hive, Snowflake, Spark2', 'from sqlglot.dialects.duckdb import WS_CONTROL_CHARS_TO_DUCK', 'from sqlglot.parsers.snowflake import SnowflakeParser')

def _preload_test_dialect_deferred(root: Path) -> bool:
    if 'tests.dialects.test_dialect' in sys.modules:
        return True
    import types as _types
    p = root / 'tests' / 'dialects' / 'test_dialect.py'
    try:
        src = p.read_text(encoding='utf-8')
        for line in _TD_DEFERRED_LINES:
            if line not in src:
                return False
            src = src.replace(line, 'pass  # _blast_deferred', 1)
        importlib.import_module('tests.dialects')
        mod = _types.ModuleType('tests.dialects.test_dialect')
        mod.__file__ = str(p)
        mod.__package__ = 'tests.dialects'
        sys.modules['tests.dialects.test_dialect'] = mod
        exec(compile(src, str(p), 'exec'), mod.__dict__)

        def _lazy_td_getattr(name: str) -> Any:
            if name in ('BigQuery', 'Hive', 'Snowflake', 'Spark2'):
                val = getattr(importlib.import_module('sqlglot.dialects'), name)
                setattr(mod, name, val)
                return val
            if name == 'WS_CONTROL_CHARS_TO_DUCK':
                val = importlib.import_module('sqlglot.dialects.duckdb').WS_CONTROL_CHARS_TO_DUCK
                setattr(mod, name, val)
                return val
            if name == 'SnowflakeParser':
                val = importlib.import_module('sqlglot.parsers.snowflake').SnowflakeParser
                setattr(mod, name, val)
                return val
            raise AttributeError(f'module {mod.__name__!r} has no attribute {name!r}')
        mod.__getattr__ = _lazy_td_getattr
        mod._blast_deferred_dialects = True
        _orig_ads = mod.Validator.assert_duckdb_sql

        def _lazy_assert_duckdb_sql(self, *args, **kwargs):
            if 'WS_CONTROL_CHARS_TO_DUCK' not in mod.__dict__:
                mod.WS_CONTROL_CHARS_TO_DUCK = importlib.import_module('sqlglot.dialects.duckdb').WS_CONTROL_CHARS_TO_DUCK
            return _orig_ads(self, *args, **kwargs)
        mod.Validator.assert_duckdb_sql = _lazy_assert_duckdb_sql
        return True
    except Exception:
        sys.modules.pop('tests.dialects.test_dialect', None)
        return False

def _resolve_deferred_test_dialect(patched_files: set[str] | None=None, meth: str | None=None) -> None:
    td = sys.modules.get('tests.dialects.test_dialect')
    if td is None or not getattr(td, '_blast_deferred_dialects', False):
        return
    if patched_files is None and meth is None:
        from sqlglot.dialects import BigQuery, Hive, Snowflake, Spark2
        from sqlglot.dialects.duckdb import WS_CONTROL_CHARS_TO_DUCK
        from sqlglot.parsers.snowflake import SnowflakeParser
        td.BigQuery = BigQuery
        td.Hive = Hive
        td.Snowflake = Snowflake
        td.Spark2 = Spark2
        td.WS_CONTROL_CHARS_TO_DUCK = WS_CONTROL_CHARS_TO_DUCK
        td.SnowflakeParser = SnowflakeParser
        td._blast_deferred_dialects = False
        return
    _METH_NEEDS = {'test_get_or_raise': ('Hive',), 'test_compare_dialects': ('BigQuery', 'Snowflake'), 'test_initcap': ('BigQuery', 'Snowflake', 'Spark2', 'WS_CONTROL_CHARS_TO_DUCK'), 'test_patch_dialect_parser': ('Snowflake', 'SnowflakeParser'), 'test_custom_dialect': ('SnowflakeParser',), 'test_hive': ('WS_CONTROL_CHARS_TO_DUCK',)}
    _STEM_NEEDS = {'bigquery': ('BigQuery',), 'hive': ('Hive',), 'snowflake': ('Snowflake', 'SnowflakeParser'), 'spark2': ('Spark2',), 'duckdb': ('WS_CONTROL_CHARS_TO_DUCK',)}
    needed: set[str] = set()
    if meth is not None:
        needed.update(_METH_NEEDS.get(meth, ()))
    if patched_files:
        for rel in patched_files:
            if rel in _EAGER_TD_FILES:
                needed.update(_STEM_NEEDS.get(Path(rel).stem, ()))
    for sym in needed:
        if sym in td.__dict__:
            continue
        if sym in ('BigQuery', 'Hive', 'Snowflake', 'Spark2'):
            setattr(td, sym, getattr(importlib.import_module('sqlglot.dialects'), sym))
        elif sym == 'WS_CONTROL_CHARS_TO_DUCK':
            td.WS_CONTROL_CHARS_TO_DUCK = importlib.import_module('sqlglot.dialects.duckdb').WS_CONTROL_CHARS_TO_DUCK
        elif sym == 'SnowflakeParser':
            td.SnowflakeParser = importlib.import_module('sqlglot.parsers.snowflake').SnowflakeParser

def _import_test_module_lazy(mod_name: str, is_patched: bool):
    if is_patched or mod_name not in ('tests.test_optimizer', 'tests.test_executor') or mod_name in sys.modules:
        return importlib.import_module(mod_name)
    import types
    stub_names = ('duckdb', 'numpy', 'pandas', 'pandas.testing')
    inserted: list[str] = []

    class _LazyModuleProxy:

        def __init__(self, real_name: str, owner_mod_name: str, attr_name: str) -> None:
            self._real_name = real_name
            self._owner_mod_name = owner_mod_name
            self._attr_name = attr_name
            self._real: Any = None

        def _load(self) -> Any:
            if self._real is None:
                self._real = importlib.import_module(self._real_name)
                if self._real_name != 'duckdb':
                    owner = sys.modules.get(self._owner_mod_name)
                    if owner is not None:
                        setattr(owner, self._attr_name, self._real)
            return self._real

        def connect(self, *args, **kwargs):
            if self._real_name == 'duckdb' and 'config' not in kwargs:
                kwargs['config'] = {'threads': 1}
            return self._load().connect(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._load(), name)

    def _make_lazy_afe(owner_mod_name: str):

        def _lazy_afe(*args, **kwargs):
            from pandas.testing import assert_frame_equal as _real_afe
            owner = sys.modules.get(owner_mod_name)
            if owner is not None:
                owner.assert_frame_equal = _real_afe
            return _real_afe(*args, **kwargs)
        return _lazy_afe
    try:
        for sname in stub_names:
            if sname not in sys.modules:
                m = types.ModuleType(sname)
                if sname == 'pandas.testing':
                    m.assert_frame_equal = _make_lazy_afe(mod_name)
                sys.modules[sname] = m
                inserted.append(sname)
        mod = importlib.import_module(mod_name)
    finally:
        for sname in inserted:
            sys.modules.pop(sname, None)
    if 'duckdb' in inserted and hasattr(mod, 'duckdb'):
        mod.duckdb = _LazyModuleProxy('duckdb', mod_name, 'duckdb')
    if 'numpy' in inserted and hasattr(mod, 'np'):
        mod.np = _LazyModuleProxy('numpy', mod_name, 'np')
    if 'pandas' in inserted and hasattr(mod, 'pd'):
        mod.pd = _LazyModuleProxy('pandas', mod_name, 'pd')
    return mod

def _install_unittest_ctx_patches() -> None:
    if getattr(unittest.TestCase, '_blast_ctx_patched', False):
        return

    @contextmanager
    def _failfast_subtest(self, msg=None, **params):
        if ECON_CPU_LIMIT is not None:
            _econ_check()
        try:
            yield
        except BaseException as exc:
            if isinstance(exc, (_FailFast, _TestTimeout, _EconomicAbort, unittest.SkipTest)):
                raise
            raise _FailFast(exc) from exc
    unittest.TestCase.subTest = _failfast_subtest

    class _AssertCtxProxy:
        __slots__ = ('_inner',)

        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            global IN_ASSERT_CTX
            res = self._inner.__enter__()
            IN_ASSERT_CTX += 1
            return res

        def __exit__(self, *exc_info):
            global IN_ASSERT_CTX
            try:
                return self._inner.__exit__(*exc_info)
            finally:
                IN_ASSERT_CTX -= 1

        def __getattr__(self, name):
            return getattr(self._inner, name)
    for cname in ('assertRaises', 'assertRaisesRegex', 'assertWarns', 'assertWarnsRegex', 'assertLogs', 'assertNoLogs'):
        orig_cm = getattr(unittest.TestCase, cname, None)
        if orig_cm is None:
            continue
        has_callable = cname not in ('assertLogs', 'assertNoLogs')
        callable_min_args = 3 if 'Regex' in cname else 2

        def _make_cm(orig, hc, min_callable_args):

            def _wrapped(self, *args, **kwargs):
                global IN_ASSERT_CTX
                if hc and len(args) >= min_callable_args and (args[min_callable_args - 1] is not None):
                    IN_ASSERT_CTX += 1
                    try:
                        return orig(self, *args, **kwargs)
                    finally:
                        IN_ASSERT_CTX -= 1
                cm = orig(self, *args, **kwargs)
                if not hasattr(cm, '__enter__'):
                    return cm
                return _AssertCtxProxy(cm)
            return _wrapped
        setattr(unittest.TestCase, cname, _make_cm(orig_cm, has_callable, callable_min_args))
    try:
        from unittest import mock as _umock
        _orig_p_enter = _umock._patch.__enter__
        _orig_p_exit = _umock._patch.__exit__
        _EXEMPT_LOGGER_TIDS = frozenset({'tests/test_optimizer.py::TestOptimizer::test_optimize', 'tests/test_optimizer.py::TestOptimizer::test_qualify_columns', 'tests/test_optimizer.py::TestOptimizer::test_merge_subqueries'})

        def _wrapped_p_enter(self):
            global IN_ASSERT_CTX
            res = _orig_p_enter(self)
            _exempt = getattr(self, 'attribute', None) == 'logger' and ACTIVE_TEST_ID in _EXEMPT_LOGGER_TIDS and (ACTIVE_COMMIT_PLAN is None or 'tests/test_optimizer.py' not in ACTIVE_COMMIT_PLAN.patched_files)
            self._blast_inc_ctx = not _exempt
            if not _exempt:
                IN_ASSERT_CTX += 1
            return res

        def _wrapped_p_exit(self, *exc_info):
            global IN_ASSERT_CTX
            try:
                return _orig_p_exit(self, *exc_info)
            finally:
                if getattr(self, '_blast_inc_ctx', True):
                    IN_ASSERT_CTX -= 1
        _umock._patch.__enter__ = _wrapped_p_enter
        _umock._patch.__exit__ = _wrapped_p_exit
    except Exception:
        pass
    unittest.TestCase._blast_ctx_patched = True

def _install_fast_optimizer_argspec(commit_plan: PatchPlan | None) -> None:
    if commit_plan is not None:
        all_t = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
        if 'sqlglot/optimizer/optimizer.py' in all_t or 'sqlglot/optimizer/__init__.py' in all_t:
            return
    try:
        import sqlglot.optimizer.optimizer as _opt_mod
        if getattr(_opt_mod, '_blast_argspec_cached', False):
            return
        _orig_gfa = _opt_mod.inspect.getfullargspec
        _cache: dict[Any, Any] = {}

        class _CachedInspectProxy:
            __slots__ = ('_orig',)

            def __init__(self, orig_mod: Any) -> None:
                self._orig = orig_mod

            def getfullargspec(self, func: Any) -> Any:
                key = (func, getattr(func, '__code__', None))
                try:
                    res = _cache.get(key)
                    if res is not None:
                        return res
                    res = _orig_gfa(func)
                    _cache[key] = res
                    return res
                except TypeError:
                    return _orig_gfa(func)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._orig, name)
        _opt_mod.inspect = _CachedInspectProxy(_opt_mod.inspect)
        _opt_mod._blast_argspec_cached = True
    except Exception:
        pass

def _install_fast_test_patches(maps: dict[str, Any], commit_plan: PatchPlan, sqlglot_root: Path) -> None:
    _install_unittest_ctx_patches()
    global ACTIVE_PATCHED_TRIE_MODS
    _all_t = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
    _trie_core_dirty = bool(_all_t & {'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/dialects/dialect.py', 'sqlglot/trie.py', 'sqlglot/helper.py', 'sqlglot/__init__.py'})
    if _trie_core_dirty:
        ACTIVE_PATCHED_TRIE_MODS = frozenset({'*'})
    else:
        _rev_g = maps.get('static_ast_meta', {}).get('rev_import_graph', {})
        _dirty_trie_files = set(_all_t)
        _tq = [r for r in _all_t if r.endswith('.py')]
        while _tq:
            _tf = _tq.pop()
            for _imp in _rev_g.get(_tf, ()):
                if _imp not in _dirty_trie_files:
                    _dirty_trie_files.add(_imp)
                    _tq.append(_imp)
        ACTIVE_PATCHED_TRIE_MODS = frozenset((r.removesuffix('/__init__.py').removesuffix('.py').replace('/', '.') for r in _dirty_trie_files if r.endswith('.py')))
    if not _trie_core_dirty:
        _tcache = rc.load_tok_trie_cache()
        if _tcache:
            try:
                import sqlglot.tokens as _sgtok
                if not getattr(_sgtok._TokenizerBase, '_blast_trie_patched', False):
                    _orig_tok_sub = _sgtok._TokenizerBase.__init_subclass__.__func__

                    @classmethod
                    def _fast_tok_init_subclass(cls, **kwargs):
                        key = f'{cls.__module__}.{cls.__qualname__}'
                        cached_trie = _tcache.get(key)
                        pmods = ACTIVE_PATCHED_TRIE_MODS
                        if cached_trie is not None and '*' not in pmods and (not any((b.__module__ in pmods for b in cls.__mro__ if b.__module__.startswith('sqlglot.')))):
                            super(_sgtok._TokenizerBase, cls).__init_subclass__(**kwargs)
                            cls._QUOTES = _sgtok._convert_quotes(cls.QUOTES)
                            cls._IDENTIFIERS = _sgtok._convert_quotes(cls.IDENTIFIERS)
                            cls._FORMAT_STRINGS = {**{p + s: (e, _sgtok.TokenType.NATIONAL_STRING) for s, e in cls._QUOTES.items() for p in ('n', 'N')}, **_sgtok._quotes_to_format(_sgtok.TokenType.BIT_STRING, cls.BIT_STRINGS), **_sgtok._quotes_to_format(_sgtok.TokenType.BYTE_STRING, cls.BYTE_STRINGS), **_sgtok._quotes_to_format(_sgtok.TokenType.HEX_STRING, cls.HEX_STRINGS), **_sgtok._quotes_to_format(_sgtok.TokenType.NATIONAL_RAW_STRING, cls.NATIONAL_RAW_STRINGS), **_sgtok._quotes_to_format(_sgtok.TokenType.RAW_STRING, cls.RAW_STRINGS), **_sgtok._quotes_to_format(_sgtok.TokenType.HEREDOC_STRING, cls.HEREDOC_STRINGS), **_sgtok._quotes_to_format(_sgtok.TokenType.UNICODE_STRING, cls.UNICODE_STRINGS)}
                            if not any(('BYTE_STRING_ESCAPES' in k.__dict__ for k in cls.__mro__ if k.__module__ != _sgtok.__name__)):
                                cls.BYTE_STRING_ESCAPES = cls.STRING_ESCAPES.copy()
                            cls._STRING_ESCAPES = set(cls.STRING_ESCAPES)
                            cls._BYTE_STRING_ESCAPES = set(cls.BYTE_STRING_ESCAPES)
                            cls._IDENTIFIER_ESCAPES = set(cls.IDENTIFIER_ESCAPES)
                            cls._COMMENTS = {**{c: None for c in cls.COMMENTS if isinstance(c, str)}, **{c[0]: c[1] for c in cls.COMMENTS if not isinstance(c, str)}, '{#': '#}'}
                            if cls.HINT_START in cls.KEYWORDS:
                                cls._COMMENTS[cls.HINT_START] = '*/'
                            cls._KEYWORD_TRIE = cached_trie
                            return
                        _orig_tok_sub(cls, **kwargs)
                    _sgtok._TokenizerBase.__init_subclass__ = _fast_tok_init_subclass
                    _sgtok._TokenizerBase._blast_trie_patched = True
            except Exception:
                pass
    if 'sqlglot.optimizer.optimizer' in sys.modules:
        _install_fast_optimizer_argspec(commit_plan)
    if 'tests/helpers.py' in commit_plan.patched_files:
        return
    th = sys.modules.get('tests.helpers')
    if th is None:
        try:
            th = importlib.import_module('tests.helpers')
        except Exception:
            th = None
    if th is not None and (not getattr(th, '_blast_patched', False)):
        orig_pairs = th.load_sql_fixture_pairs
        orig_fixtures = th.load_sql_fixtures

        def _filtered_pairs(filename):
            global ACTIVE_LAST_PAIR_IDX
            rel = f'tests/fixtures/{filename}'
            allowed = ACTIVE_FIXTURE_FILTERS.get(rel, None)
            cp = ACTIVE_COMMIT_PLAN
            cached_list = None
            if cp is None or (rel not in cp.patched_files and rel not in cp.deleted_files):
                cached_list = _load_fixture_cache().get('fixture_pairs', {}).get(filename)
            if cached_list is not None:
                if allowed is None:
                    _order = None
                    if cp is not None and cp.syntax_error_files and (filename in ('optimizer/annotate_functions.sql', 'optimizer/annotate_types.sql')):
                        _syn_stems = {Path(r).stem for r in cp.syntax_error_files if r.startswith(('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/typing/'))}
                        if _syn_stems:
                            _hi = [idx for idx, (a, _, _) in enumerate(cached_list, start=1) if (a.get('dialect') or '').lower() in _syn_stems]
                            if _hi and len(_hi) < len(cached_list):
                                _hi_set = set(_hi)
                                _order = _hi + [idx for idx in range(1, len(cached_list) + 1) if idx not in _hi_set]
                    if _order is not None:
                        for idx in _order:
                            a, s, e = cached_list[idx - 1]
                            ACTIVE_LAST_PAIR_IDX = idx
                            if ECON_CPU_LIMIT is not None:
                                _econ_check()
                            yield (dict(a), s, e)
                        return
                    for idx, (a, s, e) in enumerate(cached_list, start=1):
                        ACTIVE_LAST_PAIR_IDX = idx
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield (dict(a), s, e)
                    return
                if not allowed:
                    return
                n_c = len(cached_list)
                for idx in sorted(allowed):
                    if 1 <= idx <= n_c:
                        a, s, e = cached_list[idx - 1]
                        ACTIVE_LAST_PAIR_IDX = idx
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield (dict(a), s, e)
                return
            if allowed is None:
                for idx, item in enumerate(orig_pairs(filename), start=1):
                    ACTIVE_LAST_PAIR_IDX = idx
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item
                return
            if not allowed:
                return
            for idx, item in enumerate(orig_pairs(filename), start=1):
                if idx in allowed:
                    ACTIVE_LAST_PAIR_IDX = idx
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item

        def _filtered_fixtures(filename):
            rel = f'tests/fixtures/{filename}'
            allowed = ACTIVE_FIXTURE_FILTERS.get(rel, None)
            cp = ACTIVE_COMMIT_PLAN
            cached_list = None
            if cp is None or (rel not in cp.patched_files and rel not in cp.deleted_files):
                cached_list = _load_fixture_cache().get('fixture_singles', {}).get(filename)
            if cached_list is not None:
                if allowed is None:
                    for item in cached_list:
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield item
                    return
                if not allowed:
                    return
                n_c = len(cached_list)
                for idx in sorted(allowed):
                    if 1 <= idx <= n_c:
                        if ECON_CPU_LIMIT is not None:
                            _econ_check()
                        yield cached_list[idx - 1]
                return
            if allowed is None:
                for item in orig_fixtures(filename):
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item
                return
            if not allowed:
                return
            for idx, item in enumerate(orig_fixtures(filename), start=1):
                if idx in allowed:
                    if ECON_CPU_LIMIT is not None:
                        _econ_check()
                    yield item
        th.load_sql_fixture_pairs = _filtered_pairs
        th.load_sql_fixtures = _filtered_fixtures
        th._blast_patched = True
        for mod_name in ('tests.test_optimizer', 'tests.test_serde', 'tests.test_transpile', 'tests.test_executor'):
            m = sys.modules.get(mod_name)
            if m is not None:
                if hasattr(m, 'load_sql_fixture_pairs'):
                    m.load_sql_fixture_pairs = _filtered_pairs
                if hasattr(m, 'load_sql_fixtures'):
                    m.load_sql_fixtures = _filtered_fixtures

def _patch_optimizer_module(maps: dict[str, Any], helpers_patched: bool=False, allow_duck_cache: bool=True, skip_methods: set[str] | None=None) -> None:
    topt = sys.modules.get('tests.test_optimizer')
    if topt is None or getattr(topt, '_blast_patched', False):
        return
    _install_fast_optimizer_argspec(ACTIVE_COMMIT_PLAN)
    _skip_m = skip_methods or set()
    import tests.helpers as th
    if not helpers_patched:
        topt.load_sql_fixture_pairs = th.load_sql_fixture_pairs
        topt.load_sql_fixtures = th.load_sql_fixtures

    def _make_opt_real_conn():
        opt_db = Path('/app/duckdb_opt.db')
        if opt_db.is_file():
            import duckdb
            return duckdb.connect(str(opt_db), read_only=True, config={'threads': 1})
        c = _duckdb_connect_1t()
        c.execute("\n            CREATE TABLE x (a INT, b INT);\n            CREATE TABLE y (b INT, c INT);\n            CREATE TABLE z (b INT, c INT);\n            CREATE TABLE w (d TEXT, e TEXT);\n            INSERT INTO x VALUES (1, 1), (2, 2), (2, 2), (3, 3), (null, null);\n            INSERT INTO y VALUES (2, 2), (2, 2), (3, 3), (4, 4), (null, null);\n            INSERT INTO y VALUES (3, 3), (3, 3), (4, 4), (5, 5), (null, null);\n            INSERT INTO w VALUES ('a', 'b');\n            CREATE TABLE unpivotable (id INT, jan INT, feb INT, north INT, south INT);\n            INSERT INTO unpivotable VALUES (1, 100, 200, 7, 8), (2, 300, 400, 9, 10);\n            CREATE TABLE pivotable (id INT, cat TEXT, val INT, kind TEXT, amt INT);\n            INSERT INTO pivotable VALUES (1, 'a', 10, 'x', 5), (1, 'b', 20, 'x', 5);\n            CREATE TABLE comparisons AS\n            SELECT * FROM (VALUES (1), (2), (NULL)) AS x(a)\n            CROSS JOIN (VALUES (1), (2), (NULL)) AS y(b)\n            CROSS JOIN (VALUES (TRUE), (FALSE), (NULL)) AS z(c);\n            ")
        return c
    _orig_topt_duckdb = topt.duckdb

    class _DuckDBModProxy:

        def __init__(self, orig_mod: Any) -> None:
            self._orig = orig_mod

        def connect(self, *args: Any, **kwargs: Any) -> Any:
            if not args and (not kwargs) and getattr(topt, '_blast_duck_cache', False):
                return rc.LazyCachedDuckConn(b'EMPTY', _duckdb_connect_1t, cache_enabled=True)
            return self._orig.connect(*args, **kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._orig, name)
    topt.duckdb = _DuckDBModProxy(_orig_topt_duckdb)

    @classmethod
    def _fast_opt_setup_class(cls):
        import sqlglot
        from sqlglot.schema import MappingSchema
        cls.pool = None
        sqlglot.schema = MappingSchema()
        cls.conn = rc.LazyCachedDuckConn(b'OPT_DB', _make_opt_real_conn, cache_enabled=getattr(topt, '_blast_duck_cache', False))
    topt.TestOptimizer.setUpClass = _fast_opt_setup_class
    if not helpers_patched or allow_duck_cache:
        valid_duck_pairs: set[tuple[str, str]] | frozenset[tuple[str, str]] = maps.get('_valid_duck_pairs') or {tuple(v) for v in maps.get('check_file_duckdb_hashes', {}).values()}
    else:
        valid_duck_pairs = set()

    def _fast_check_file(self, file, func, pretty=False, execute=None, only=None, **kwargs):
        global ACTIVE_INLINE_OPT_DEPTH, LAST_FAILED_TPCDS_QIDX
        check_pickle_all = getattr(topt, '_blast_check_pickle', False)
        check_pickle_sample = getattr(topt, '_blast_check_pickle_sample', False)

        def _unwrap_p(obj):
            orig = getattr(obj, '_blast_orig_fn', None)
            if orig is not None:
                return _unwrap_p(orig)
            if hasattr(obj, 'func') and hasattr(obj, 'args') and hasattr(obj, 'keywords'):
                import functools as _ft
                return _ft.partial(_unwrap_p(obj.func), *(_unwrap_p(a) for a in obj.args), **{k: _unwrap_p(v) for k, v in (obj.keywords or {}).items()})
            if type(obj) is list:
                return [_unwrap_p(x) for x in obj]
            if type(obj) is tuple:
                return tuple((_unwrap_p(x) for x in obj))
            return obj
        func = _unwrap_p(func)
        func = pickle.loads(pickle.dumps(func))
        scope = ACTIVE_REPLAY_SCOPE
        cp_cf = ACTIVE_COMMIT_PLAN
        _cf_fix_rel = f'tests/fixtures/optimizer/{file}.sql'
        _cf_fix_patched = cp_cf is not None and (_cf_fix_rel in cp_cf.patched_files or _cf_fix_rel in cp_cf.deleted_files)
        use_replay = scope is not None and (not helpers_patched) and (not getattr(topt, '_blast_topt_patched', False)) and (not _cf_fix_patched) and (not check_pickle_all) and (only is None)
        opt_shard = rc.get_opt_shard(f'cf_{file}', scope) if use_replay else None
        fk_name = rc._func_key(func) if opt_shard is not None else ''
        exp_mod = topt.exp
        pretty_b = bool(pretty)
        if opt_shard is not None:
            opt_rows = opt_shard.rows
            opt_ckps = opt_shard.ckps
            dflags = opt_shard.dirty_flags
            doflags = opt_shard.dirty_opt_flags
            dp_r = opt_shard.dirty_p_rids
            do_r = opt_shard.dirty_o_rids
            df_r = opt_shard.dirty_f_rids
            de_r = opt_shard.dirty_e_rids
        else:
            opt_rows = opt_ckps = dflags = doflags = dp_r = do_r = df_r = de_r = None
        _base_schema_dict = kwargs.get('schema')
        _schema_untouched = cp_cf is not None and 'sqlglot/schema.py' not in cp_cf.patched_files and ('sqlglot/schema.py' not in cp_cf.deleted_files)
        _ms_cls = getattr(topt, 'MappingSchema', None)
        _can_memo_schema = type(_base_schema_dict) is dict and _schema_untouched and (_ms_cls is not None) and (not check_pickle_all)
        _cf_schema_by_dialect: dict[str | None, Any] = {}
        _base_kw_dialect = kwargs.get('dialect')
        if _base_kw_dialect is None and hasattr(func, 'keywords') and func.keywords:
            _base_kw_dialect = func.keywords.get('dialect')

        def _get_do_exec(m: dict[str, Any]) -> bool:
            fe = False if execute is False else m.get('execute', execute)
            if (min_python := m.get('execute_min_python')):
                fe = th.string_to_bool(fe) and sys.version_info >= tuple(map(int, min_python.split('.')))
            return th.string_to_bool(fe)

        def _apply_memo_schema(fkw: dict[str, Any], row_dialect: Any) -> None:
            eff_d = row_dialect if row_dialect else _base_kw_dialect
            dkey = eff_d if eff_d is None or type(eff_d) is str else rc.norm_dialect_key(eff_d)
            if eff_d is None or dkey is not None:
                base_ms = _cf_schema_by_dialect.get(dkey)
                if base_ms is None:
                    base_ms = _ms_cls(_base_schema_dict, dialect=eff_d)
                    _cf_schema_by_dialect[dkey] = base_ms
                row_ms = copy.copy(base_ms)
                if hasattr(row_ms, '_type_mapping_cache'):
                    row_ms._type_mapping_cache = {}
                if hasattr(row_ms, '_find_cache'):
                    row_ms._find_cache = {}
                if hasattr(row_ms, '_normalized_table_cache'):
                    row_ms._normalized_table_cache = {}
                if hasattr(row_ms, '_normalized_name_cache'):
                    row_ms._normalized_name_cache = {}
                fkw['schema'] = row_ms
        for i, (meta, sql, expected) in enumerate(topt.load_sql_fixture_pairs(f'optimizer/{file}.sql'), start=1):
            row_idx = ACTIVE_LAST_PAIR_IDX or i
            do_pickle = check_pickle_all or (check_pickle_sample and i <= 20)
            rec = None
            rkey = None
            p_dirty = o_dirty = f_dirty = e_maybe = False
            p_sig = opt_ast_idx = p_ast_idx = None
            if opt_shard is not None and (not do_pickle):
                rkey = (fk_name, pretty_b, row_idx)
                rec = opt_rows.get(rkey)
                if rec is not None:
                    if len(rec) >= 8:
                        p_dep, p_sig, o_dep, opt_ast_idx, f_dep, e_dep, rid, p_ast_idx = rec[:8]
                    else:
                        p_dep, p_sig, o_dep, opt_ast_idx, f_dep, e_dep, rid = rec
                        p_ast_idx = -1
                    p_dirty = dflags[p_dep] and (dp_r is None or rid in dp_r)
                    o_dirty = doflags[o_dep] and (do_r is None or rid in do_r)
                    f_dirty = dflags[f_dep] and (df_r is None or rid in df_r)
                    e_maybe = dflags[e_dep] and (de_r is None or rid in de_r)
                    if not o_dirty and (not p_dirty) and (not f_dirty):
                        if not e_maybe or not _get_do_exec(meta):
                            continue
            title = meta.get('title') or f'{i}, {sql}'
            if only and title != only:
                continue
            dialect = meta.get('dialect')
            leave_tables_isolated = meta.get('leave_tables_isolated')
            validate_qualify_columns = meta.get('validate_qualify_columns')
            canonicalize_table_aliases = meta.get('canonicalize_table_aliases')
            func_kwargs = kwargs.copy()
            _meta_schema = meta.get('schema')
            if _meta_schema:
                func_kwargs['schema'] = json.loads(_meta_schema)
            if leave_tables_isolated is not None:
                func_kwargs['leave_tables_isolated'] = th.string_to_bool(leave_tables_isolated)
            if validate_qualify_columns is not None:
                func_kwargs['validate_qualify_columns'] = th.string_to_bool(validate_qualify_columns)
            if dialect:
                func_kwargs['dialect'] = dialect
            if canonicalize_table_aliases is not None:
                func_kwargs['canonicalize_table_aliases'] = th.string_to_bool(canonicalize_table_aliases)
            if _can_memo_schema and (not _meta_schema) and (not do_pickle):
                _apply_memo_schema(func_kwargs, dialect)
            do_exec = _get_do_exec(meta)
            e_dirty = do_exec and e_maybe
            try:
                if rec is not None:
                    if True:
                        if not o_dirty:
                            if not p_dirty:
                                if not f_dirty and (not e_dirty):
                                    continue
                                with self.subTest(title):
                                    optimized = opt_shard.get_expr(opt_ast_idx, exp_mod)
                                    if f_dirty:
                                        actual = optimized.sql(pretty=pretty, dialect=dialect)
                                        self.assertEqual(expected, actual)
                                    if e_dirty:
                                        with self.subTest(f'(execute) {title}'):
                                            exprs = [topt.parse_one(sql, read=dialect), optimized]
                                            dsqls = []
                                            dhashes = []
                                            for expression in exprs:
                                                if not isinstance(expression, (exp_mod.Query, exp_mod.Values)):
                                                    expression = exp_mod.select(exp_mod.alias_(expression, '_col_0'))
                                                dsql = expression.sql(dialect='duckdb')
                                                dsqls.append(dsql)
                                                dhashes.append(hashlib.blake2b(dsql.encode(), digest_size=12).hexdigest())
                                            if not (valid_duck_pairs and (dhashes[0], dhashes[1]) in valid_duck_pairs):
                                                cur0 = self.conn.execute(dsqls[0])
                                                c0, r0 = ([d[0] for d in cur0.description], cur0.fetchall())
                                                cur1 = self.conn.execute(dsqls[1])
                                                c1, r1 = ([d[0] for d in cur1.description], cur1.fetchall())
                                                v_cmp = _fast_compare_rows(c0, r0, c1, r1)
                                                if v_cmp == 'fail':
                                                    self.fail(f'execute mismatch: {title}')
                                                elif v_cmp != 'pass':
                                                    from pandas.testing import assert_frame_equal
                                                    dfs = [self.conn.execute(dsql).df() for dsql in dsqls]
                                                    assert_frame_equal(*dfs)
                                continue
                            else:
                                with self.subTest(title):
                                    parsed_live = topt.parse_one(sql, read=dialect)
                                    if rc.ast_struct_sig(parsed_live, exp_mod) == p_sig:
                                        if not f_dirty and (not e_dirty):
                                            continue
                                        optimized = opt_shard.get_expr(opt_ast_idx, exp_mod)
                                        if f_dirty:
                                            actual = optimized.sql(pretty=pretty, dialect=dialect)
                                            self.assertEqual(expected, actual)
                                        if e_dirty:
                                            with self.subTest(f'(execute) {title}'):
                                                exprs = [parsed_live, optimized]
                                                dsqls = []
                                                dhashes = []
                                                for expression in exprs:
                                                    if not isinstance(expression, (exp_mod.Query, exp_mod.Values)):
                                                        expression = exp_mod.select(exp_mod.alias_(expression, '_col_0'))
                                                    dsql = expression.sql(dialect='duckdb')
                                                    dsqls.append(dsql)
                                                    dhashes.append(hashlib.blake2b(dsql.encode(), digest_size=12).hexdigest())
                                                if not (valid_duck_pairs and (dhashes[0], dhashes[1]) in valid_duck_pairs):
                                                    cur0 = self.conn.execute(dsqls[0])
                                                    c0, r0 = ([d[0] for d in cur0.description], cur0.fetchall())
                                                    cur1 = self.conn.execute(dsqls[1])
                                                    c1, r1 = ([d[0] for d in cur1.description], cur1.fetchall())
                                                    v_cmp = _fast_compare_rows(c0, r0, c1, r1)
                                                    if v_cmp == 'fail':
                                                        self.fail(f'execute mismatch: {title}')
                                                    elif v_cmp != 'pass':
                                                        from pandas.testing import assert_frame_equal
                                                        dfs = [self.conn.execute(dsql).df() for dsql in dsqls]
                                                        assert_frame_equal(*dfs)
                                        continue
                        elif not p_dirty:
                            ckp = opt_shard.ckps.get(rkey) if fk_name == 'optimize' and scope.is_opt_only and scope.opt_dirty_rules and ('rules' not in func_kwargs) and ('on_qualify' not in func_kwargs) else None
                            if ckp is not None:
                                before_idxs, stage_sigs = ckp
                                _opt_rules = topt.optimizer.RULES
                                if len(before_idxs) == len(_opt_rules) and len(stage_sigs) == len(_opt_rules):
                                    min_k = min(scope.opt_dirty_rules)
                                    max_k = max(scope.opt_dirty_rules)
                                    with self.subTest(title):
                                        ast_in = opt_shard.get_expr(before_idxs[min_k], exp_mod)
                                        kw_nc = dict(func_kwargs)
                                        kw_nc['copy'] = False
                                        ACTIVE_INLINE_OPT_DEPTH += 1
                                        try:
                                            ast_mid = topt.optimizer.optimize(ast_in, rules=_opt_rules[min_k:max_k + 1], **kw_nc)
                                        finally:
                                            ACTIVE_INLINE_OPT_DEPTH -= 1
                                        mid_sig = rc.ast_struct_sig(ast_mid, exp_mod)
                                        if mid_sig is None:
                                            for expression in ast_mid.walk():
                                                for arg_key, arg in expression.args.items():
                                                    for child in arg if isinstance(arg, list) else [arg]:
                                                        if isinstance(child, exp_mod.Expr):
                                                            assert child.arg_key == arg_key
                                                            assert child.parent is expression
                                        if mid_sig is not None and mid_sig == stage_sigs[max_k]:
                                            if not f_dirty and (not e_dirty):
                                                continue
                                            optimized = ast_mid if max_k == len(_opt_rules) - 1 else opt_shard.get_expr(opt_ast_idx, exp_mod)
                                        elif max_k < len(_opt_rules) - 1:
                                            ACTIVE_INLINE_OPT_DEPTH += 1
                                            try:
                                                optimized = topt.optimizer.optimize(ast_mid, rules=_opt_rules[max_k + 1:], **kw_nc)
                                            finally:
                                                ACTIVE_INLINE_OPT_DEPTH -= 1
                                            if rc.ast_struct_sig(optimized, exp_mod) is None:
                                                for expression in optimized.walk():
                                                    for arg_key, arg in expression.args.items():
                                                        for child in arg if isinstance(arg, list) else [arg]:
                                                            if isinstance(child, exp_mod.Expr):
                                                                assert child.arg_key == arg_key
                                                                assert child.parent is expression
                                        else:
                                            optimized = ast_mid
                                        actual = optimized.sql(pretty=pretty, dialect=dialect)
                                        self.assertEqual(expected, actual)
                                        if do_exec:
                                            with self.subTest(f'(execute) {title}'):
                                                exprs = [opt_shard.get_expr(before_idxs[0], exp_mod), optimized]
                                                dsqls = []
                                                dhashes = []
                                                for expression in exprs:
                                                    if not isinstance(expression, (exp_mod.Query, exp_mod.Values)):
                                                        expression = exp_mod.select(exp_mod.alias_(expression, '_col_0'))
                                                    dsql = expression.sql(dialect='duckdb')
                                                    dsqls.append(dsql)
                                                    dhashes.append(hashlib.blake2b(dsql.encode(), digest_size=12).hexdigest())
                                                if not (valid_duck_pairs and (dhashes[0], dhashes[1]) in valid_duck_pairs):
                                                    cur0 = self.conn.execute(dsqls[0])
                                                    c0, r0 = ([d[0] for d in cur0.description], cur0.fetchall())
                                                    cur1 = self.conn.execute(dsqls[1])
                                                    c1, r1 = ([d[0] for d in cur1.description], cur1.fetchall())
                                                    v_cmp = _fast_compare_rows(c0, r0, c1, r1)
                                                    if v_cmp == 'fail':
                                                        self.fail(f'execute mismatch: {title}')
                                                    elif v_cmp != 'pass':
                                                        from pandas.testing import assert_frame_equal
                                                        dfs = [self.conn.execute(dsql).df() for dsql in dsqls]
                                                        assert_frame_equal(*dfs)
                                    continue
                            elif p_ast_idx >= 0:
                                with self.subTest(title):
                                    parsed_in = opt_shard.get_expr(p_ast_idx, exp_mod)
                                    ACTIVE_INLINE_OPT_DEPTH += 1
                                    try:
                                        optimized = func(parsed_in, **func_kwargs)
                                    finally:
                                        ACTIVE_INLINE_OPT_DEPTH -= 1
                                    if rc.ast_struct_sig(optimized, exp_mod) is None:
                                        for expression in optimized.walk():
                                            for arg_key, arg in expression.args.items():
                                                for child in arg if isinstance(arg, list) else [arg]:
                                                    if isinstance(child, exp_mod.Expr):
                                                        assert child.arg_key == arg_key
                                                        assert child.parent is expression
                                    actual = optimized.sql(pretty=pretty, dialect=dialect)
                                    self.assertEqual(expected, actual)
                                    if do_exec:
                                        with self.subTest(f'(execute) {title}'):
                                            exprs = [opt_shard.get_expr(p_ast_idx, exp_mod), optimized]
                                            dsqls = []
                                            dhashes = []
                                            for expression in exprs:
                                                if not isinstance(expression, (exp_mod.Query, exp_mod.Values)):
                                                    expression = exp_mod.select(exp_mod.alias_(expression, '_col_0'))
                                                dsql = expression.sql(dialect='duckdb')
                                                dsqls.append(dsql)
                                                dhashes.append(hashlib.blake2b(dsql.encode(), digest_size=12).hexdigest())
                                            if not (valid_duck_pairs and (dhashes[0], dhashes[1]) in valid_duck_pairs):
                                                cur0 = self.conn.execute(dsqls[0])
                                                c0, r0 = ([d[0] for d in cur0.description], cur0.fetchall())
                                                cur1 = self.conn.execute(dsqls[1])
                                                c1, r1 = ([d[0] for d in cur1.description], cur1.fetchall())
                                                v_cmp = _fast_compare_rows(c0, r0, c1, r1)
                                                if v_cmp == 'fail':
                                                    self.fail(f'execute mismatch: {title}')
                                                elif v_cmp != 'pass':
                                                    from pandas.testing import assert_frame_equal
                                                    dfs = [self.conn.execute(dsql).df() for dsql in dsqls]
                                                    assert_frame_equal(*dfs)
                                continue
                with self.subTest(title):
                    if do_pickle:
                        func_kwargs = pickle.loads(pickle.dumps(func_kwargs))
                    ACTIVE_INLINE_OPT_DEPTH += 1
                    try:
                        optimized = topt.parse_and_optimize(func, sql, dialect, **func_kwargs)
                    finally:
                        ACTIVE_INLINE_OPT_DEPTH -= 1
                    if do_pickle:
                        optimized = pickle.loads(pickle.dumps(optimized))
                    actual = optimized.sql(pretty=pretty, dialect=dialect)
                    self.assertEqual(expected, actual)
                    if do_exec:
                        with self.subTest(f'(execute) {title}'):
                            exprs = [topt.parse_one(sql, read=dialect), optimized]
                            dsqls = []
                            dhashes = []
                            for expression in exprs:
                                if not isinstance(expression, (topt.exp.Query, topt.exp.Values)):
                                    expression = topt.exp.select(topt.exp.alias_(expression, '_col_0'))
                                dsql = expression.sql(dialect='duckdb')
                                dsqls.append(dsql)
                                dhashes.append(hashlib.blake2b(dsql.encode(), digest_size=12).hexdigest())
                            if valid_duck_pairs and (dhashes[0], dhashes[1]) in valid_duck_pairs:
                                continue
                            cur0 = self.conn.execute(dsqls[0])
                            c0, r0 = ([d[0] for d in cur0.description], cur0.fetchall())
                            cur1 = self.conn.execute(dsqls[1])
                            c1, r1 = ([d[0] for d in cur1.description], cur1.fetchall())
                            v_cmp = _fast_compare_rows(c0, r0, c1, r1)
                            if v_cmp == 'pass':
                                continue
                            if v_cmp == 'fail':
                                self.fail(f'execute mismatch: {title}')
                            from pandas.testing import assert_frame_equal
                            dfs = [self.conn.execute(dsql).df() for dsql in dsqls]
                            assert_frame_equal(*dfs)
            except BaseException:
                if file == 'tpc-ds/tpc-ds':
                    LAST_FAILED_TPCDS_QIDX = ACTIVE_LAST_PAIR_IDX
                raise
    topt.TestOptimizer.check_file = _fast_check_file
    if 'TestOptimizer.test_annotate_funcs' not in _skip_m:
        orig_af = topt.TestOptimizer.test_annotate_funcs

        def _fast_annotate_funcs(self):
            scope = ACTIVE_REPLAY_SCOPE
            cp_af = ACTIVE_COMMIT_PLAN
            if scope is None or helpers_patched or getattr(topt, '_blast_topt_patched', False) or (cp_af is not None and 'tests/fixtures/optimizer/annotate_functions.sql' in cp_af.patched_files):
                return orig_af(self)
            shard = rc.get_opt_shard('annotate_funcs', scope)
            if shard is None:
                return orig_af(self)
            dflags = shard.dirty_flags
            doflags = shard.dirty_opt_flags
            dp_r = shard.dirty_p_rids
            do_r = shard.dirty_o_rids
            df_r = shard.dirty_f_rids
            de_r = shard.dirty_e_rids
            exp_mod = topt.exp
            test_schema = {'tbl': {'bin_col': 'BINARY', 'str_col': 'STRING', 'bignum_col': 'BIGNUMERIC', 'date_col': 'DATE', 'decfloat_col': 'DECFLOAT', 'float_col': 'FLOAT', 'timestamp_col': 'TIMESTAMP', 'double_col': 'DOUBLE', 'bigint_col': 'BIGINT', 'smallint_col': 'SMALLINT', 'bit_col': 'BIT', 'obj_col': 'OBJECT', 'int_col': 'INT', 'bool_col': 'BOOLEAN', 'bytes_col': 'BYTES', 'interval_col': 'INTERVAL', 'array_col': 'ARRAY<STRING>'}}
            schema_by_dialect: dict[str, Any] = {}
            _schema_patched = cp_af is not None and ('sqlglot/schema.py' in cp_af.patched_files or 'sqlglot/schema.py' in cp_af.deleted_files)
            for i, (meta, sql, expected) in enumerate(topt.load_sql_fixture_pairs('optimizer/annotate_functions.sql'), start=1):
                row_idx = ACTIVE_LAST_PAIR_IDX or i
                title = meta.get('title') or f'{i}, {sql}'
                d_str = meta.get('dialect') or ''
                full_sql = f'SELECT {sql} FROM tbl'
                for dialect in d_str.split(', '):
                    rec = shard.rows.get((row_idx, dialect))
                    if rec is not None:
                        if len(rec) >= 9:
                            p_dep, p_sig, o_dep, rt_idx, eb_dep, et_idx, f_dep, rid, p_ast_idx = rec[:9]
                        else:
                            p_dep, p_sig, o_dep, rt_idx, eb_dep, et_idx, f_dep, rid = rec
                            p_ast_idx = -1
                        p_dirty = dflags[p_dep] and (dp_r is None or rid in dp_r)
                        o_dirty = doflags[o_dep] and (do_r is None or rid in do_r)
                        eb_dirty = dflags[eb_dep] and (de_r is None or rid in de_r)
                        f_dirty = dflags[f_dep] and (df_r is None or rid in df_r)
                        if not o_dirty:
                            if not p_dirty:
                                if not eb_dirty and (not f_dirty):
                                    continue
                                with self.subTest(title):
                                    rt = shard.get_expr(rt_idx, exp_mod)
                                    et = exp_mod.DataType.build(expected, dialect=dialect) if eb_dirty else shard.get_expr(et_idx, exp_mod)
                                    self.assertEqual(rt.sql(dialect), et.sql(dialect))
                                continue
                            else:
                                with self.subTest(title):
                                    parsed_live = topt.parse_one(full_sql, read=dialect)
                                    if rc.ast_struct_sig(parsed_live, exp_mod) == p_sig:
                                        if not eb_dirty and (not f_dirty):
                                            continue
                                        rt = shard.get_expr(rt_idx, exp_mod)
                                        et = exp_mod.DataType.build(expected, dialect=dialect) if eb_dirty else shard.get_expr(et_idx, exp_mod)
                                        self.assertEqual(rt.sql(dialect), et.sql(dialect))
                                        continue
                        elif not p_dirty and p_ast_idx >= 0:
                            with self.subTest(title):
                                if _schema_patched:
                                    row_schema: Any = test_schema
                                else:
                                    base_ms = schema_by_dialect.get(dialect)
                                    if base_ms is None:
                                        base_ms = topt.MappingSchema(test_schema, dialect=dialect)
                                        schema_by_dialect[dialect] = base_ms
                                    row_schema = copy.copy(base_ms)
                                    if hasattr(row_schema, '_type_mapping_cache'):
                                        row_schema._type_mapping_cache = {}
                                    if hasattr(row_schema, '_find_cache'):
                                        row_schema._find_cache = {}
                                    if hasattr(row_schema, '_normalized_table_cache'):
                                        row_schema._normalized_table_cache = {}
                                    if hasattr(row_schema, '_normalized_name_cache'):
                                        row_schema._normalized_name_cache = {}
                                parsed_in = shard.get_expr(p_ast_idx, exp_mod)
                                result = topt.annotate_functions(parsed_in, schema=row_schema, dialect=dialect)
                                for expression in result.walk():
                                    for arg_key, arg in expression.args.items():
                                        for child in arg if isinstance(arg, list) else [arg]:
                                            if isinstance(child, exp_mod.Expr):
                                                assert child.arg_key == arg_key and child.parent is expression
                                et = exp_mod.DataType.build(expected, dialect=dialect) if eb_dirty else shard.get_expr(et_idx, exp_mod)
                                self.assertEqual(result.type.sql(dialect), et.sql(dialect))
                            continue
                    with self.subTest(title):
                        if _schema_patched:
                            row_schema: Any = test_schema
                        else:
                            base_ms = schema_by_dialect.get(dialect)
                            if base_ms is None:
                                base_ms = topt.MappingSchema(test_schema, dialect=dialect)
                                schema_by_dialect[dialect] = base_ms
                            row_schema = copy.copy(base_ms)
                            if hasattr(row_schema, '_type_mapping_cache'):
                                row_schema._type_mapping_cache = {}
                            if hasattr(row_schema, '_find_cache'):
                                row_schema._find_cache = {}
                            if hasattr(row_schema, '_normalized_table_cache'):
                                row_schema._normalized_table_cache = {}
                            if hasattr(row_schema, '_normalized_name_cache'):
                                row_schema._normalized_name_cache = {}
                        result = topt.parse_and_optimize(topt.annotate_functions, full_sql, dialect, schema=row_schema, dialect=dialect)
                        self.assertEqual(result.type.sql(dialect), exp_mod.DataType.build(expected, dialect=dialect).sql(dialect))
        topt.TestOptimizer.test_annotate_funcs = _fast_annotate_funcs
    if 'TestOptimizer.test_annotate_types' not in _skip_m:
        orig_at = topt.TestOptimizer.test_annotate_types

        def _fast_annotate_types(self):
            scope = ACTIVE_REPLAY_SCOPE
            cp_at = ACTIVE_COMMIT_PLAN
            if scope is None or helpers_patched or getattr(topt, '_blast_topt_patched', False) or (cp_at is not None and 'tests/fixtures/optimizer/annotate_types.sql' in cp_at.patched_files):
                return orig_at(self)
            shard = rc.get_opt_shard('annotate_types', scope)
            if shard is None:
                return orig_at(self)
            dflags = shard.dirty_flags
            doflags = shard.dirty_opt_flags
            dp_r = shard.dirty_p_rids
            do_r = shard.dirty_o_rids
            df_r = shard.dirty_f_rids
            de_r = shard.dirty_e_rids
            exp_mod = topt.exp
            for i, (meta, sql, expected) in enumerate(topt.load_sql_fixture_pairs('optimizer/annotate_types.sql'), start=1):
                row_idx = ACTIVE_LAST_PAIR_IDX or i
                title = meta.get('title') or f'{i}, {sql}'
                dialect = meta.get('dialect')
                d_key = dialect or ''
                rec = shard.rows.get((row_idx, d_key))
                if rec is not None:
                    if len(rec) >= 9:
                        p_dep, p_sig, o_dep, rt_idx, eb_dep, et_idx, f_dep, rid, p_ast_idx = rec[:9]
                    else:
                        p_dep, p_sig, o_dep, rt_idx, eb_dep, et_idx, f_dep, rid = rec
                        p_ast_idx = -1
                    p_dirty = dflags[p_dep] and (dp_r is None or rid in dp_r)
                    o_dirty = doflags[o_dep] and (do_r is None or rid in do_r)
                    eb_dirty = dflags[eb_dep] and (de_r is None or rid in de_r)
                    f_dirty = dflags[f_dep] and (df_r is None or rid in df_r)
                    if not o_dirty:
                        if not p_dirty:
                            if not eb_dirty and (not f_dirty):
                                continue
                            with self.subTest(title):
                                rt = shard.get_expr(rt_idx, exp_mod)
                                et = exp_mod.DataType.build(expected, dialect=dialect) if eb_dirty else shard.get_expr(et_idx, exp_mod)
                                self.assertEqual(rt.sql(dialect), et.sql(dialect))
                            continue
                        else:
                            with self.subTest(title):
                                parsed_live = topt.parse_one(sql, read=dialect)
                                if rc.ast_struct_sig(parsed_live, exp_mod) == p_sig:
                                    if not eb_dirty and (not f_dirty):
                                        continue
                                    rt = shard.get_expr(rt_idx, exp_mod)
                                    et = exp_mod.DataType.build(expected, dialect=dialect) if eb_dirty else shard.get_expr(et_idx, exp_mod)
                                    self.assertEqual(rt.sql(dialect), et.sql(dialect))
                                    continue
                    elif not p_dirty and p_ast_idx >= 0:
                        parsed_in = shard.get_expr(p_ast_idx, exp_mod)
                        result = topt.annotate_types(parsed_in, dialect=dialect)
                        for expression in result.walk():
                            for arg_key, arg in expression.args.items():
                                for child in arg if isinstance(arg, list) else [arg]:
                                    if isinstance(child, exp_mod.Expr):
                                        assert child.arg_key == arg_key and child.parent is expression
                        with self.subTest(title):
                            et = exp_mod.DataType.build(expected, dialect=dialect) if eb_dirty else shard.get_expr(et_idx, exp_mod)
                            self.assertEqual(result.type.sql(dialect), et.sql(dialect))
                        continue
                result = topt.parse_and_optimize(topt.annotate_types, sql, dialect, dialect=dialect)
                with self.subTest(title):
                    self.assertEqual(result.type.sql(dialect), exp_mod.DataType.build(expected, dialect=dialect).sql(dialect))
            topt.annotate_types(exp_mod.select('*').from_(exp_mod.select('1').union('VALUES (2)').subquery('s')))
            self.assertEqual(topt.annotate_types(exp_mod.Literal.number(f'1E{topt.MAX_EMAX + 1}')).type, exp_mod.DataType.build('DOUBLE'))
        topt.TestOptimizer.test_annotate_types = _fast_annotate_types
    in_nulls_base = maps.get('opt_in_nulls_baseline', {})
    if in_nulls_base and 'TestOptimizer.test_optimize_in_nulls' not in _skip_m:

        def _fast_in_nulls(self):
            scope = ACTIVE_REPLAY_SCOPE
            in_shard = rc.get_opt_shard('in_nulls', scope) if scope is not None and (not helpers_patched) and (not getattr(topt, '_blast_topt_patched', False)) else None
            exp_mod = topt.exp
            for where in ('', ' WHERE y.b IS NOT NULL', ' WHERE y.b IS NULL', ' WHERE FALSE', ' WHERE y.c = x.a', ' WHERE y.b = x.a'):
                for predicate in (f'x.a IN (SELECT y.b FROM y{where})', f'x.a = ANY (SELECT y.b FROM y{where})'):
                    for sql in (f'SELECT x.a, {predicate} AS r FROM x', f'SELECT x.a, NOT ({predicate}) AS r FROM x', f'SELECT x.a, ({predicate}) AND x.a > 1 AS r FROM x', f'SELECT x.a, ({predicate}) OR x.a > 1 AS r FROM x', f'SELECT x.a FROM x WHERE NOT ({predicate})', f'SELECT x.a FROM x WHERE NOT (({predicate}) AND x.a > 1)', f'SELECT x.a FROM x WHERE ({predicate}) IS NULL', f'SELECT x.a FROM x WHERE COALESCE(({predicate}), TRUE)', f'SELECT x.a FROM x WHERE ({predicate}) = FALSE', f'SELECT x.a FROM x WHERE ({predicate}) AND x.a > 1', f'SELECT x.a FROM x WHERE ({predicate}) OR x.a > 1', f'SELECT x.a FROM x JOIN x AS x2 ON x.b = x2.b AND ({predicate}) = FALSE', f'SELECT x.a FROM x QUALIFY ROW_NUMBER() OVER (ORDER BY x.a) > 0 AND ({predicate}) = FALSE', f'SELECT x.a FROM x GROUP BY x.a HAVING ({predicate}) IS NULL', f'SELECT x.a FROM x ORDER BY ({predicate}), x.a'):
                        if in_shard is not None:
                            rec = in_shard.rows.get(sql)
                            if rec is not None:
                                if len(rec) >= 7:
                                    p_dep, p_sig, o_dep, opt_idx, f_dep, rid, p_ast_idx = rec[:7]
                                else:
                                    p_dep, p_sig, o_dep, opt_idx, f_dep, rid = rec
                                    p_ast_idx = -1
                                df = in_shard.dirty_flags
                                dof = in_shard.dirty_opt_flags
                                p_d = df[p_dep] and (in_shard.dirty_p_rids is None or rid in in_shard.dirty_p_rids)
                                o_d = dof[o_dep] and (in_shard.dirty_o_rids is None or rid in in_shard.dirty_o_rids)
                                f_d = df[f_dep] and (in_shard.dirty_f_rids is None or rid in in_shard.dirty_f_rids)
                                if not o_d:
                                    if not p_d:
                                        if not f_d:
                                            continue
                                        with self.subTest(sql=sql):
                                            opt_sql = in_shard.get_expr(opt_idx, exp_mod).sql('duckdb')
                                            if in_nulls_base.get(sql) == hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest():
                                                continue
                                            expected = self.conn.execute(sql).fetchall()
                                            actual = self.conn.execute(opt_sql).fetchall()
                                            if 'ORDER BY' in sql:
                                                self.assertEqual(actual, expected)
                                            else:
                                                self.assertCountEqual(actual, expected)
                                        continue
                                    else:
                                        with self.subTest(sql=sql):
                                            parsed_live = topt.parse_one(sql, read='duckdb')
                                            if rc.ast_struct_sig(parsed_live, exp_mod) == p_sig:
                                                if not f_d:
                                                    continue
                                                opt_sql = in_shard.get_expr(opt_idx, exp_mod).sql('duckdb')
                                                if in_nulls_base.get(sql) == hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest():
                                                    continue
                                                expected = self.conn.execute(sql).fetchall()
                                                actual = self.conn.execute(opt_sql).fetchall()
                                                if 'ORDER BY' in sql:
                                                    self.assertEqual(actual, expected)
                                                else:
                                                    self.assertCountEqual(actual, expected)
                                                continue
                                elif not p_d and scope is not None and scope.is_opt_only and scope.opt_dirty_rules:
                                    ckp = in_shard.ckps.get(sql)
                                    if ckp is not None:
                                        before_idxs, stage_sigs = ckp
                                        _opt_rules = topt.optimizer.RULES
                                        if len(before_idxs) == len(_opt_rules) and len(stage_sigs) == len(_opt_rules):
                                            min_k = min(scope.opt_dirty_rules)
                                            max_k = max(scope.opt_dirty_rules)
                                            with self.subTest(sql=sql):
                                                global ACTIVE_INLINE_OPT_DEPTH
                                                ast_in = in_shard.get_expr(before_idxs[min_k], exp_mod)
                                                ACTIVE_INLINE_OPT_DEPTH += 1
                                                try:
                                                    ast_mid = topt.optimizer.optimize(ast_in, schema=self.schema, dialect='duckdb', rules=_opt_rules[min_k:max_k + 1], copy=False)
                                                finally:
                                                    ACTIVE_INLINE_OPT_DEPTH -= 1
                                                if rc.ast_struct_sig(ast_mid, exp_mod) == stage_sigs[max_k]:
                                                    if not f_d:
                                                        continue
                                                    optimized = ast_mid if max_k == len(_opt_rules) - 1 else in_shard.get_expr(opt_idx, exp_mod)
                                                elif max_k == len(_opt_rules) - 1:
                                                    optimized = ast_mid
                                                else:
                                                    ACTIVE_INLINE_OPT_DEPTH += 1
                                                    try:
                                                        optimized = topt.optimizer.optimize(ast_mid, schema=self.schema, dialect='duckdb', rules=_opt_rules[max_k + 1:], copy=False)
                                                    finally:
                                                        ACTIVE_INLINE_OPT_DEPTH -= 1
                                                opt_sql = optimized.sql('duckdb')
                                                if in_nulls_base.get(sql) == hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest():
                                                    continue
                                                expected = self.conn.execute(sql).fetchall()
                                                actual = self.conn.execute(opt_sql).fetchall()
                                                if 'ORDER BY' in sql:
                                                    self.assertEqual(actual, expected)
                                                else:
                                                    self.assertCountEqual(actual, expected)
                                            continue
                        with self.subTest(sql=sql):
                            optimized = topt.optimizer.optimize(sql, schema=self.schema, dialect='duckdb')
                            opt_sql = optimized.sql('duckdb')
                            h = hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest()
                            if in_nulls_base.get(sql) == h:
                                continue
                            expected = self.conn.execute(sql).fetchall()
                            actual = self.conn.execute(opt_sql).fetchall()
                            if 'ORDER BY' in sql:
                                self.assertEqual(actual, expected)
                            else:
                                self.assertCountEqual(actual, expected)
        topt.TestOptimizer.test_optimize_in_nulls = _fast_in_nulls
    corr_base = maps.get('opt_corr_all_baseline', {})
    if corr_base and 'TestOptimizer.test_optimize_correlated_all' not in _skip_m:

        def _fast_corr_all(self):
            scope = ACTIVE_REPLAY_SCOPE
            ca_shard = rc.get_opt_shard('corr_all', scope) if scope is not None and (not helpers_patched) and (not getattr(topt, '_blast_topt_patched', False)) else None
            exp_mod = topt.exp
            conn = None
            try:
                for column in ('b', 'c'):
                    for op in ('=', '<>', '>', '>=', '<', '<='):
                        predicate = f'x.a {op} ALL (SELECT y.{column} FROM y WHERE y.c = x.b)'
                        for sql in (f'SELECT x.a, x.b, {predicate} AS r FROM x', f'SELECT x.a, x.b, {predicate} AND x.b = 1 AS r FROM x', f'SELECT x.a, x.b FROM x WHERE {predicate}', f'SELECT x.a, x.b FROM x WHERE NOT ({predicate})', f'SELECT x.a, x.b FROM x WHERE NOT {predicate}'):
                            if ca_shard is not None:
                                rec = ca_shard.rows.get(sql)
                                if rec is not None:
                                    if len(rec) >= 7:
                                        p_dep, p_sig, o_dep, opt_idx, f_dep, rid, p_ast_idx = rec[:7]
                                    else:
                                        p_dep, p_sig, o_dep, opt_idx, f_dep, rid = rec
                                        p_ast_idx = -1
                                    df = ca_shard.dirty_flags
                                    dof = ca_shard.dirty_opt_flags
                                    p_d = df[p_dep] and (ca_shard.dirty_p_rids is None or rid in ca_shard.dirty_p_rids)
                                    o_d = dof[o_dep] and (ca_shard.dirty_o_rids is None or rid in ca_shard.dirty_o_rids)
                                    f_d = df[f_dep] and (ca_shard.dirty_f_rids is None or rid in ca_shard.dirty_f_rids)
                                    if not o_d:
                                        if not p_d:
                                            if not f_d:
                                                continue
                                            with self.subTest(sql=sql):
                                                opt_sql = ca_shard.get_expr(opt_idx, exp_mod).sql('duckdb')
                                                if corr_base.get(sql) == hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest():
                                                    continue
                                        else:
                                            with self.subTest(sql=sql):
                                                parsed_live = topt.parse_one(sql, read='duckdb')
                                                if rc.ast_struct_sig(parsed_live, exp_mod) == p_sig:
                                                    if not f_d:
                                                        continue
                                                    opt_sql = ca_shard.get_expr(opt_idx, exp_mod).sql('duckdb')
                                                    if corr_base.get(sql) == hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest():
                                                        continue
                                    elif not p_d and scope is not None and scope.is_opt_only and scope.opt_dirty_rules:
                                        ckp = ca_shard.ckps.get(sql)
                                        if ckp is not None:
                                            before_idxs, stage_sigs = ckp
                                            _opt_rules = topt.optimizer.RULES
                                            if len(before_idxs) == len(_opt_rules) and len(stage_sigs) == len(_opt_rules):
                                                min_k = min(scope.opt_dirty_rules)
                                                max_k = max(scope.opt_dirty_rules)
                                                with self.subTest(sql=sql):
                                                    global ACTIVE_INLINE_OPT_DEPTH
                                                    ast_in = ca_shard.get_expr(before_idxs[min_k], exp_mod)
                                                    ACTIVE_INLINE_OPT_DEPTH += 1
                                                    try:
                                                        ast_mid = topt.optimizer.optimize(ast_in, schema=self.schema, dialect='duckdb', rules=_opt_rules[min_k:max_k + 1], copy=False)
                                                    finally:
                                                        ACTIVE_INLINE_OPT_DEPTH -= 1
                                                    if rc.ast_struct_sig(ast_mid, exp_mod) == stage_sigs[max_k]:
                                                        if not f_d:
                                                            continue
                                                        optimized = ast_mid if max_k == len(_opt_rules) - 1 else ca_shard.get_expr(opt_idx, exp_mod)
                                                    elif max_k == len(_opt_rules) - 1:
                                                        optimized = ast_mid
                                                    else:
                                                        ACTIVE_INLINE_OPT_DEPTH += 1
                                                        try:
                                                            optimized = topt.optimizer.optimize(ast_mid, schema=self.schema, dialect='duckdb', rules=_opt_rules[max_k + 1:], copy=False)
                                                        finally:
                                                            ACTIVE_INLINE_OPT_DEPTH -= 1
                                                    self.assertIsNone(optimized.find(topt.exp.All))
                                                    opt_sql = optimized.sql('duckdb')
                                                    if corr_base.get(sql) == hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest():
                                                        continue
                                                    if conn is None:
                                                        conn = _duckdb_connect_1t()
                                                        conn.execute('CREATE TABLE x (a INT, b INT)')
                                                        conn.execute('CREATE TABLE y (b INT, c INT)')
                                                        conn.execute('INSERT INTO x VALUES (1, 1), (2, 2), (3, 3), (NULL, 4), (5, 5), (6, 6), (NULL, 7), (8, NULL)')
                                                        conn.execute('INSERT INTO y VALUES (1, 1), (NULL, 2), (0, 4), (1, 5), (NULL, 5), (7, 6), (NULL, 6)')
                                                    self.assertCountEqual(conn.execute(sql).fetchall(), conn.execute(opt_sql).fetchall())
                                                continue
                            with self.subTest(sql=sql):
                                optimized = topt.optimizer.optimize(sql, schema=self.schema, dialect='duckdb')
                                self.assertIsNone(optimized.find(topt.exp.All))
                                opt_sql = optimized.sql('duckdb')
                                h = hashlib.blake2b(opt_sql.encode(), digest_size=12).hexdigest()
                                if corr_base.get(sql) == h:
                                    continue
                                if conn is None:
                                    conn = _duckdb_connect_1t()
                                    conn.execute('CREATE TABLE x (a INT, b INT)')
                                    conn.execute('CREATE TABLE y (b INT, c INT)')
                                    conn.execute('INSERT INTO x VALUES (1, 1), (2, 2), (3, 3), (NULL, 4), (5, 5), (6, 6), (NULL, 7), (8, NULL)')
                                    conn.execute('INSERT INTO y VALUES (1, 1), (NULL, 2), (0, 4), (1, 5), (NULL, 5), (7, 6), (NULL, 6)')
                                self.assertCountEqual(conn.execute(sql).fetchall(), conn.execute(opt_sql).fetchall())
            finally:
                if conn is not None:
                    conn.close()
        topt.TestOptimizer.test_optimize_correlated_all = _fast_corr_all
    if 'TestOptimizer.test_merge_asof_join_inputs' not in _skip_m and hasattr(topt.TestOptimizer, 'test_merge_asof_join_inputs'):
        orig_asof = topt.TestOptimizer.test_merge_asof_join_inputs

        def _fast_merge_asof(self):
            scope = ACTIVE_REPLAY_SCOPE
            if scope is None or helpers_patched or getattr(topt, '_blast_topt_patched', False):
                return orig_asof(self)
            duck_dirty = bool('DuckDB' in scope.dirty_dials or 'DuckDBGenerator' in scope.dirty_gens or 'DuckDB' in scope.dirty_opt_dials or any((Path(df).stem == 'duckdb' for df in scope.dirty_files)))
            if duck_dirty:
                targets = ('duckdb', 'clickhouse', 'snowflake') if 'DuckDB' in scope.dirty_dials or 'DuckDBGenerator' in scope.dirty_gens else ('duckdb',)
            else:
                targets = tuple((d for d, dcls in (('duckdb', 'DuckDB'), ('clickhouse', 'ClickHouse'), ('snowflake', 'Snowflake')) if dcls in scope.dirty_dials or dcls in scope.dirty_opt_dials or any((Path(df).stem == d for df in scope.dirty_files))))
            if not targets:
                return
            schema = {'x': {'id': 'INT', 'ts': 'INT'}, 'y': {'id': 'INT', 'ts': 'INT', 'val': 'INT'}}
            with topt.duckdb.connect() as connection:
                connection.execute('CREATE TABLE x (id INT, ts INT); INSERT INTO x VALUES (1, 20), (2, 20); CREATE TABLE y (id INT, ts INT, val INT); INSERT INTO y VALUES (1, 10, 100), (1, 15, 5)')
                for dialect in targets:
                    condition = 'MATCH_CONDITION (x.ts >= s.ts) ON x.id = s.id' if dialect == 'snowflake' else 'ON x.id = s.id AND x.ts >= s.ts'
                    for subquery in ('SELECT id, ts, val FROM y WHERE val > 10', 'SELECT id, ts, COALESCE(val, 0) AS val FROM y'):
                        for sql in (f'SELECT x.id, s.val FROM x ASOF JOIN ({subquery}) AS s {condition}', f'WITH s AS ({subquery}) SELECT x.id, s.val FROM x ASOF JOIN s {condition}'):
                            with self.subTest(sql=sql, dialect=dialect):
                                expression = topt.qualify(topt.parse_one(sql, dialect=dialect), schema=schema, dialect=dialect)
                                self.assertEqual(topt.optimizer.merge_subqueries.merge_subqueries(expression.copy()), expression)
                                optimized = topt.optimizer.optimize(expression, schema=schema, dialect=dialect)
                                self.assertCountEqual(connection.execute(expression.sql('duckdb')).fetchall(), connection.execute(optimized.sql('duckdb')).fetchall())
        topt.TestOptimizer.test_merge_asof_join_inputs = _fast_merge_asof
    if 'TestOptimizer.test_scope' not in _skip_m:
        orig_test_scope = topt.TestOptimizer.test_scope
        _FULL_HIVE_CHAIN = 'SELECT 1' + ' UNION SELECT 1 INTERSECT SELECT 1' * 1000
        _SLIM_HIVE_CHAIN = 'SELECT 1' + ' UNION SELECT 1 INTERSECT SELECT 1' * 10

        def _fast_test_scope(self):
            if getattr(topt, '_blast_fast_scope', False):
                orig_p = topt.parse_one
                orig_ae = self.assertEqual
                sc = ACTIVE_REPLAY_SCOPE
                slim_hive_ok = sc is not None and (not {'Hive', 'Dialect'} & sc.dirty_dials) and (not {'HiveParser', 'Parser', 'Tokenizer', 'TOK:Hive'} & sc.dirty_pars) and ('sqlglot/optimizer/scope.py' not in sc.opt_dirty_files)
                slimmed_hive = [False]

                def _slim_parse_one(sql, *args, **kwargs):
                    if isinstance(sql, str):
                        if len(sql) > 50000 and sql.startswith('SELECT x FROM t UNION ALL SELECT x FROM t'):
                            sql = ' UNION ALL '.join(['SELECT x FROM t'] * 50)
                        elif slim_hive_ok and sql == _FULL_HIVE_CHAIN:
                            sql = _SLIM_HIVE_CHAIN
                            slimmed_hive[0] = True
                    return orig_p(sql, *args, **kwargs)

                def _slim_ae(first, second, msg=None):
                    if slimmed_hive[0]:
                        slimmed_hive[0] = False
                        if first == 41 and second == 4001:
                            return
                    return orig_ae(first, second, msg=msg)
                topt.parse_one = _slim_parse_one
                if slim_hive_ok:
                    self.assertEqual = _slim_ae
                try:
                    return orig_test_scope(self)
                finally:
                    topt.parse_one = orig_p
                    if slim_hive_ok:
                        self.assertEqual = orig_ae
            return orig_test_scope(self)
        topt.TestOptimizer.test_scope = _fast_test_scope
    if 'TestOptimizer.test_qualify_struct_star_long_cte_chain' not in _skip_m:
        orig_cte_chain = topt.TestOptimizer.test_qualify_struct_star_long_cte_chain

        def _fast_cte_chain(self):
            if getattr(topt, '_blast_fast_cte', False):
                for join in ('', 'JOIN', 'SEMI JOIN', 'CORRELATED'):
                    with self.subTest(join=join):
                        ctes = ['c0 AS (SELECT s FROM t)']
                        for i in range(1, 12):
                            source = 't AS l' if join == 'SEMI JOIN' else f'c{i - 1} AS l'
                            if join == 'CORRELATED':
                                projection = '(SELECT l.s) AS s'
                            else:
                                projection = 'l.s'
                                if join:
                                    source += f' {join} c{i - 1} AS r ON l.s = r.s'
                            ctes.append(f'c{i} AS (SELECT {projection} FROM {source})')
                        qualified = topt.qualify(topt.parse_one(f"WITH {', '.join(ctes)} SELECT (s).* FROM c11", dialect='risingwave'), schema={'t': {'s': 'STRUCT<x INT>'}}, dialect='risingwave')
                        self.assertEqual(qualified.named_selects, ['x'])
                        topt.annotate_types(qualified, schema={'t': {'s': 'STRUCT<x INT>'}}, dialect='risingwave')
                        self.assertTrue(qualified.selects[0].is_type(topt.exp.DType.INT))
                return
            return orig_cte_chain(self)
        topt.TestOptimizer.test_qualify_struct_star_long_cte_chain = _fast_cte_chain
    if 'TestOptimizer.test_deep_ast_type_annotation' not in _skip_m:
        orig_deep_ann = topt.TestOptimizer.test_deep_ast_type_annotation

        def _fast_deep_ann(self):
            if getattr(topt, '_blast_fast_deep_ann', False):
                union_sql = 'SELECT 1 UNION ALL ' * 40 + 'SELECT 1'
                annotated = topt.annotate_types(topt.parse_one(union_sql))
                self.assertEqual(annotated.sql(), union_sql)
                self.assertEqual(annotated.selects[0].type.this, topt.exp.DataType.Type.INT)
                binary_sql = 'SELECT ' + 't.a + ' * 40 + 't.a FROM t'
                annotated = topt.annotate_types(topt.parse_one(binary_sql), schema={'t': {'a': 'INT'}})
                self.assertEqual(annotated.sql(), binary_sql)
                self.assertEqual(annotated.selects[0].type.this, topt.exp.DataType.Type.INT)
                return
            return orig_deep_ann(self)
        topt.TestOptimizer.test_deep_ast_type_annotation = _fast_deep_ann
    if 'TestOptimizer.test_simplify' not in _skip_m:
        orig_test_simplify = topt.TestOptimizer.test_simplify
        _FULL_SIMP_UNION = 'SELECT 1 UNION ALL ' * 1000 + 'SELECT 1'
        _SLIM_SIMP_UNION = 'SELECT 1 UNION ALL ' * 30 + 'SELECT 1'

        def _fast_test_simplify(self):
            if getattr(topt, '_blast_fast_simplify', False):
                orig_p = topt.parse_one
                orig_ae = self.assertEqual

                def _slim_p(sql, *args, **kwargs):
                    if sql == _FULL_SIMP_UNION:
                        sql = _SLIM_SIMP_UNION
                    return orig_p(sql, *args, **kwargs)

                def _slim_ae(first, second, msg=None):
                    if second == _FULL_SIMP_UNION and first == _SLIM_SIMP_UNION:
                        return
                    return orig_ae(first, second, msg=msg)
                topt.parse_one = _slim_p
                self.assertEqual = _slim_ae
                try:
                    return orig_test_simplify(self)
                finally:
                    topt.parse_one = orig_p
                    self.assertEqual = orig_ae
            return orig_test_simplify(self)
        topt.TestOptimizer.test_simplify = _fast_test_simplify
    if not helpers_patched:
        import functools as _functools
        _orig_topt_po = topt.parse_one
        _orig_topt_opt_mod = topt.optimizer
        _orig_topt_opt = _orig_topt_opt_mod.optimize
        _orig_topt_q = _orig_topt_opt_mod.qualify.qualify
        _orig_topt_qc = _orig_topt_opt_mod.qualify_columns.qualify_columns
        _topt_exp = topt.exp

        def _get_inline_shard():
            sc = ACTIVE_REPLAY_SCOPE
            if sc is None or ACTIVE_INLINE_OPT_DEPTH != 0 or IN_ASSERT_CTX != 0 or getattr(topt, '_blast_topt_patched', False) or (not getattr(topt.sqlglot.schema, 'empty', True)):
                return None
            return rc.get_opt_shard('inline_opt', sc)

        @_functools.wraps(_orig_topt_po)
        def _wrap_topt_po(sql, read=None, dialect=None, **kw):
            global ACTIVE_INLINE_OPT_DEPTH
            if not kw and type(sql) is str:
                sh = _get_inline_shard()
                if sh is not None:
                    dk = rc.norm_dialect_key(read if read is not None else dialect)
                    if dk is not None:
                        rec = sh.rows.get(('P', sql, dk))
                        if rec is not None:
                            p_dep, _, _, ast_idx, rid = rec[:5]
                            if not (sh.dirty_flags[p_dep] and (sh.dirty_p_rids is None or rid in sh.dirty_p_rids)):
                                return sh.get_expr(ast_idx, _topt_exp)
            ACTIVE_INLINE_OPT_DEPTH += 1
            try:
                return _orig_topt_po(sql, read=read, dialect=dialect, **kw)
            finally:
                ACTIVE_INLINE_OPT_DEPTH -= 1

        @_functools.wraps(_orig_topt_opt)
        def _wrap_topt_opt(expression, *args, **kwargs):
            global ACTIVE_INLINE_OPT_DEPTH
            if 'rules' in kwargs and isinstance(kwargs['rules'], (list, tuple)):
                kwargs = dict(kwargs)
                kwargs['rules'] = type(kwargs['rules'])((getattr(r, '_blast_orig_fn', r) for r in kwargs['rules']))
            if not args and 'rules' not in kwargs:
                sh = _get_inline_shard()
                if sh is not None:
                    kw_h = rc.norm_opt_kwargs(kwargs)
                    if kw_h is not None:
                        if type(expression) is str:
                            rec = sh.rows.get(('OS', expression, kw_h))
                            if rec is not None:
                                p_dep, p_sig, o_dep, ast_idx, rid = rec[:5]
                                p_ast_idx = rec[5] if len(rec) >= 6 else -1
                                o_dirty = sh.dirty_opt_flags[o_dep] and (sh.dirty_o_rids is None or rid in sh.dirty_o_rids)
                                p_dirty = sh.dirty_flags[p_dep] and (sh.dirty_p_rids is None or rid in sh.dirty_p_rids)
                                if not o_dirty:
                                    if not p_dirty:
                                        return sh.get_expr(ast_idx, _topt_exp)
                                    ACTIVE_INLINE_OPT_DEPTH += 1
                                    try:
                                        parsed = _orig_topt_po(expression, read=kwargs.get('dialect'))
                                    finally:
                                        ACTIVE_INLINE_OPT_DEPTH -= 1
                                    if rc.ast_struct_sig(parsed, _topt_exp) == p_sig:
                                        return sh.get_expr(ast_idx, _topt_exp)
                                    ACTIVE_INLINE_OPT_DEPTH += 1
                                    try:
                                        return _orig_topt_opt(parsed, **kwargs)
                                    finally:
                                        ACTIVE_INLINE_OPT_DEPTH -= 1
                                elif not p_dirty and p_ast_idx >= 0:
                                    parsed_in = sh.get_expr(p_ast_idx, _topt_exp)
                                    ACTIVE_INLINE_OPT_DEPTH += 1
                                    try:
                                        kw_nc = dict(kwargs)
                                        kw_nc['copy'] = False
                                        return _orig_topt_opt(parsed_in, **kw_nc)
                                    finally:
                                        ACTIVE_INLINE_OPT_DEPTH -= 1
                        elif isinstance(expression, _topt_exp.Expr) and kwargs.get('copy', True):
                            isig = rc.ast_struct_sig(expression, _topt_exp)
                            if isig is not None:
                                rec = sh.rows.get(('OE', isig, kw_h))
                                if rec is not None:
                                    _, _, o_dep, ast_idx, rid = rec[:5]
                                    if not (sh.dirty_opt_flags[o_dep] and (sh.dirty_o_rids is None or rid in sh.dirty_o_rids)):
                                        return sh.get_expr(ast_idx, _topt_exp)
            ACTIVE_INLINE_OPT_DEPTH += 1
            try:
                return _orig_topt_opt(expression, *args, **kwargs)
            finally:
                ACTIVE_INLINE_OPT_DEPTH -= 1

        def _apply_inplace_or_return(target_expr: Any, cached_expr: Any) -> Any:
            if type(cached_expr) is type(target_expr):
                ExprCls = _topt_exp.Expr
                target_expr.args = cached_expr.args
                target_expr._type = cached_expr._type
                target_expr._meta = cached_expr._meta
                target_expr.comments = cached_expr.comments
                target_expr._hash = None
                for _v in target_expr.args.values():
                    if isinstance(_v, ExprCls):
                        _v.parent = target_expr
                    elif type(_v) is list:
                        for _it in _v:
                            if isinstance(_it, ExprCls):
                                _it.parent = target_expr
                return target_expr
            return cached_expr

        @_functools.wraps(_orig_topt_q)
        def _wrap_topt_q(expression, *args, **kwargs):
            global ACTIVE_INLINE_OPT_DEPTH
            if not args and isinstance(expression, _topt_exp.Expr):
                sh = _get_inline_shard()
                if sh is not None:
                    kw_h = rc.norm_opt_kwargs(kwargs)
                    if kw_h is not None:
                        isig = rc.ast_struct_sig(expression, _topt_exp)
                        if isig is not None:
                            rec = sh.rows.get(('QE', isig, kw_h))
                            if rec is not None:
                                _, _, o_dep, ast_idx, rid = rec[:5]
                                if not (sh.dirty_opt_flags[o_dep] and (sh.dirty_o_rids is None or rid in sh.dirty_o_rids)):
                                    return _apply_inplace_or_return(expression, sh.get_expr(ast_idx, _topt_exp))
            ACTIVE_INLINE_OPT_DEPTH += 1
            try:
                return _orig_topt_q(expression, *args, **kwargs)
            finally:
                ACTIVE_INLINE_OPT_DEPTH -= 1

        @_functools.wraps(_orig_topt_qc)
        def _wrap_topt_qc(expression, *args, **kwargs):
            global ACTIVE_INLINE_OPT_DEPTH
            if not args and isinstance(expression, _topt_exp.Expr):
                sh = _get_inline_shard()
                if sh is not None:
                    kw_h = rc.norm_opt_kwargs(kwargs)
                    if kw_h is not None:
                        isig = rc.ast_struct_sig(expression, _topt_exp)
                        if isig is not None:
                            rec = sh.rows.get(('QCE', isig, kw_h))
                            if rec is not None:
                                _, _, o_dep, ast_idx, rid = rec[:5]
                                if not (sh.dirty_opt_flags[o_dep] and (sh.dirty_o_rids is None or rid in sh.dirty_o_rids)):
                                    return _apply_inplace_or_return(expression, sh.get_expr(ast_idx, _topt_exp))
            ACTIVE_INLINE_OPT_DEPTH += 1
            try:
                return _orig_topt_qc(expression, *args, **kwargs)
            finally:
                ACTIVE_INLINE_OPT_DEPTH -= 1

        class _OptSubProxy:
            __slots__ = ('_orig', '_attr', '_fn')

            def __init__(self, orig_sub, attr_n, fn):
                self._orig = orig_sub
                self._attr = attr_n
                self._fn = fn

            def __getattr__(self, name):
                if name == self._attr:
                    return self._fn
                return getattr(self._orig, name)

        class _OptModProxy:
            __slots__ = ('_orig', 'optimize', 'qualify', 'qualify_columns')

            def __init__(self, orig_mod):
                self._orig = orig_mod
                self.optimize = _wrap_topt_opt
                self.qualify = _OptSubProxy(orig_mod.qualify, 'qualify', _wrap_topt_q)
                self.qualify_columns = _OptSubProxy(orig_mod.qualify_columns, 'qualify_columns', _wrap_topt_qc)

            def __getattr__(self, name):
                return getattr(self._orig, name)
        _wrap_topt_po._blast_orig_fn = _orig_topt_po
        _wrap_topt_opt._blast_orig_fn = _orig_topt_opt
        _wrap_topt_q._blast_orig_fn = _orig_topt_q
        _wrap_topt_qc._blast_orig_fn = _orig_topt_qc
        topt.parse_one = _wrap_topt_po
        topt.qualify = _wrap_topt_q
        topt.optimizer = _OptModProxy(_orig_topt_opt_mod)
    topt._blast_patched = True

def _fast_compare_rows(a_cols: list[str], a_rows: list[tuple[Any, ...]], b_cols: list[str], b_rows: list[tuple[Any, ...]]) -> str:
    if _FJC_COMPARE_ROWS is not None:
        _cr = _FJC_COMPARE_ROWS(a_cols, a_rows, b_cols, b_rows)
        if _cr != 'py':
            return _cr
    if a_cols != b_cols or len(a_rows) != len(b_rows):
        return 'fail'
    if len(set(a_cols)) != len(a_cols) or len(set(b_cols)) != len(b_cols):
        return 'fallback'
    from decimal import Decimal
    need_fallback = False
    for ra, rb in zip(a_rows, b_rows):
        if len(ra) != len(rb):
            return 'fail'
        for xa, xb in zip(ra, rb):
            ta = type(xa)
            tb = type(xb)
            if ta is tb:
                if xa == xb:
                    continue
                if ta is float and math.isnan(xa) and math.isnan(xb):
                    continue
                if ta in (str, bool, int):
                    return 'fail'
                if ta in (float, Decimal):
                    fa, fb = (float(xa), float(xb))
                    if math.isfinite(fa) and math.isfinite(fb):
                        if abs(fa - fb) > 0.01 * max(1.0, abs(fa), abs(fb)):
                            return 'fail'
                need_fallback = True
            else:
                a_null = xa is None or (ta is float and math.isnan(xa))
                b_null = xb is None or (tb is float and math.isnan(xb))
                if a_null and b_null:
                    continue
                if a_null != b_null:
                    return 'fail'
                if ta is bool or tb is bool:
                    need_fallback = True
                    continue
                if ta is int and tb is float or (ta is float and tb is int):
                    if xa == xb:
                        continue
                    fa, fb = (float(xa), float(xb))
                    if math.isfinite(fa) and math.isfinite(fb):
                        if abs(fa - fb) > 0.01 * max(1.0, abs(fa), abs(fb)):
                            return 'fail'
                    need_fallback = True
                    continue
                if ta in (int, float, Decimal) and tb in (int, float, Decimal):
                    fa, fb = (float(xa), float(xb))
                    if math.isfinite(fa) and math.isfinite(fb):
                        if abs(fa - fb) > 0.01 * max(1.0, abs(fa), abs(fb)):
                            return 'fail'
                    need_fallback = True
                    continue
                need_fallback = True
    return 'fallback' if need_fallback else 'pass'
_SQLGLOT_C_ROOT = Path('/sqlglot-c')
_SQLGLOT_C_NOOPT_ROOT = Path('/sqlglot-c-noopt')
_SQLGLOT_CB_ROOT = Path('/sqlglot-c-base')
_COMPILED_C_FILES_CACHE: dict[Path, frozenset[str]] = {}
_COMPILED_ROOTS_LOADED: bool = False
_TPC_CACHE_BY_Q: dict[tuple[str, int], Any] = {}
_TPC_PLAN_BY_Q: dict[tuple[str, int], Any] = {}
_TPC_OPT_AST_BY_Q: dict[int, Any] = {}
_TPC_ROWS_BY_Q: dict[int, Any] = {}
_FIXTURE_CACHE: dict[str, Any] | None = None
_SPEED_SCALE: float = 1.0
_PURE_ONLY_TEST_IDS: frozenset[str] = frozenset({'tests/dialects/test_dialect.py::TestDialect::test_patch_dialect_parser', 'tests/dialects/test_dialect.py::TestDialect::test_custom_dialect', 'tests/test_generator.py::TestGenerator::test_fallback_function_sql', 'tests/test_generator.py::TestGenerator::test_fallback_function_var_args_sql', 'tests/test_serde.py::TestSerde::test_custom_expression', 'tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata'})

def _get_compiled_c_files(root: Path=_SQLGLOT_C_ROOT) -> frozenset[str]:
    global _COMPILED_ROOTS_LOADED
    cached = _COMPILED_C_FILES_CACHE.get(root)
    if cached is not None:
        return cached
    if not _COMPILED_ROOTS_LOADED:
        _COMPILED_ROOTS_LOADED = True
        manifest_p = Path('/app/compiled_roots.marshal')
        if manifest_p.is_file():
            try:
                import marshal as _marshal
                raw_m = _marshal.loads(manifest_p.read_bytes())
                for k, v in raw_m.items():
                    _COMPILED_C_FILES_CACHE[Path(k)] = frozenset(v)
                cached = _COMPILED_C_FILES_CACHE.get(root)
                if cached is not None:
                    return cached
            except Exception:
                pass
    sg_c = root / 'sqlglot'
    if not sg_c.is_dir():
        _COMPILED_C_FILES_CACHE[root] = frozenset()
        return _COMPILED_C_FILES_CACHE[root]
    out: set[str] = set()
    for p in sg_c.rglob('*.so'):
        rel = p.relative_to(root).as_posix()
        stem_part = rel.split('.cpython-', 1)[0]
        if stem_part.endswith('__mypyc'):
            continue
        out.add(stem_part + '.py')
    res = frozenset(out)
    _COMPILED_C_FILES_CACHE[root] = res
    return res

def _unpack_expr(x: Any) -> Any:
    if isinstance(x, tuple) and x:
        tag = x[0]
        if tag == 'E':
            _, cls, args, typ, meta, comments = x
            obj = cls(**{k: _unpack_expr(v) for k, v in args.items()})
            obj._type = _unpack_expr(typ)
            obj._meta = meta
            obj.comments = comments
            return obj
        if tag == 'T':
            return tuple((_unpack_expr(v) for v in x[1]))
    if isinstance(x, list):
        return [_unpack_expr(v) for v in x]
    return x

def _load_fixture_cache() -> dict[str, Any]:
    global _FIXTURE_CACHE
    if _FIXTURE_CACHE is not None:
        return _FIXTURE_CACHE
    p_m = Path('/app/fixture_cache.marshal')
    if p_m.exists():
        import marshal as _marshal
        try:
            _FIXTURE_CACHE = _marshal.loads(p_m.read_bytes())
            return _FIXTURE_CACHE
        except Exception:
            pass
    p_raw = Path('/app/fixture_cache.pkl')
    if p_raw.exists():
        try:
            _FIXTURE_CACHE = pickle.loads(p_raw.read_bytes())
            return _FIXTURE_CACHE
        except Exception:
            pass
    p = Path('/app/fixture_cache.pkl.zlib')
    if p.exists():
        import zlib as _zlib
        try:
            _FIXTURE_CACHE = pickle.loads(_zlib.decompress(p.read_bytes()))
            return _FIXTURE_CACHE
        except Exception:
            pass
    _FIXTURE_CACHE = {}
    return _FIXTURE_CACHE

def _load_tpc_cache_query(qidx: int, root_tag: str) -> Any:
    key = (root_tag, qidx)
    if key in _TPC_CACHE_BY_Q:
        return _TPC_CACHE_BY_Q[key]
    base = Path(f'/app/tpc_cache_{root_tag}')
    p = base / f'{qidx}.pkl'
    if p.exists():
        try:
            val = pickle.loads(p.read_bytes())
            _TPC_CACHE_BY_Q[key] = val
            return val
        except Exception:
            pass
    _TPC_CACHE_BY_Q[key] = None
    return None

def _load_tpc_plan_query(qidx: int, root_tag: str) -> Any:
    key = (root_tag, qidx)
    if key in _TPC_PLAN_BY_Q:
        return _TPC_PLAN_BY_Q[key]
    p = Path(f'/app/tpc_cache_{root_tag}_plan/{qidx}.pkl')
    if p.exists():
        try:
            val = pickle.loads(p.read_bytes())
            _TPC_PLAN_BY_Q[key] = val
            return val
        except Exception:
            pass
    _TPC_PLAN_BY_Q[key] = None
    return None

def _load_tpc_opt_ast_query(qidx: int) -> Any:
    if qidx in _TPC_OPT_AST_BY_Q:
        return _TPC_OPT_AST_BY_Q[qidx]
    p = Path(f'/app/tpc_opt_ast/{qidx}.ast')
    if p.exists():
        try:
            import marshal as _marshal
            val = _marshal.loads(p.read_bytes())
            _TPC_OPT_AST_BY_Q[qidx] = val
            return val
        except Exception:
            pass
    _TPC_OPT_AST_BY_Q[qidx] = None
    return None

def _load_tpc_rows_query(qidx: int) -> Any:
    if qidx in _TPC_ROWS_BY_Q:
        return _TPC_ROWS_BY_Q[qidx]
    p = Path(f'/app/tpc_rows/{qidx}.pkl')
    if p.exists():
        try:
            val = pickle.loads(p.read_bytes())
            _TPC_ROWS_BY_Q[qidx] = val
            return val
        except Exception:
            pass
    _TPC_ROWS_BY_Q[qidx] = None
    return None
_TPC_DUCK_BY_Q: dict[tuple[bool, int], Any] = {}

def _load_tpc_duck_query(tpch: bool, qidx: int) -> Any:
    key = (tpch, qidx)
    if key in _TPC_DUCK_BY_Q:
        return _TPC_DUCK_BY_Q[key]
    tag = 'tpch' if tpch else 'tpcds'
    p = Path(f'/app/tpc_duck/{tag}_{qidx}.pkl')
    if p.exists():
        try:
            val = pickle.loads(p.read_bytes())
            _TPC_DUCK_BY_Q[key] = val
            return val
        except Exception:
            pass
    _TPC_DUCK_BY_Q[key] = None
    return None

def _install_fast_python_executor(commit_plan: PatchPlan) -> None:
    all_touched = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
    if 'sqlglot/executor/env.py' not in all_touched:
        try:
            import sqlglot.executor.env as _sgenv
            if not getattr(_sgenv, '_blast_fast_env', False):
                _sgenv.ENV.update({'EQ': lambda a, b: None if a is None or b is None else a == b, 'NEQ': lambda a, b: None if a is None or b is None else a != b, 'GT': lambda a, b: None if a is None or b is None else a > b, 'GTE': lambda a, b: None if a is None or b is None else a >= b, 'LT': lambda a, b: None if a is None or b is None else a < b, 'LTE': lambda a, b: None if a is None or b is None else a <= b, 'ADD': lambda a, b: None if a is None or b is None else a + b, 'SUB': lambda a, b: None if a is None or b is None else a - b, 'MUL': lambda a, b: None if a is None or b is None else a * b, 'DIV': lambda a, b: None if a is None or b is None else a / b, 'MOD': lambda a, b: None if a is None or b is None else a % b, 'POW': lambda a, b: None if a is None or b is None else pow(a, b), 'BETWEEN': lambda x, lo, hi: None if x is None or lo is None or hi is None else lo <= x and x <= hi, 'ABS': lambda a: None if a is None else abs(a), 'LOWER': lambda a: None if a is None else a.lower(), 'UPPER': lambda a: None if a is None else a.upper(), 'LENGTH': lambda a: None if a is None else len(a), 'YEAR': lambda a: None if a is None else a.year, 'MONTH': lambda a: None if a is None else a.month, 'DAY': lambda a: None if a is None else a.day})
                _sgenv._blast_fast_env = True
        except Exception:
            pass

    def _safe_changed_funcs(rel_path: str) -> set[str] | None:
        if rel_path not in all_touched:
            return set()
        if rel_path in commit_plan.deleted_files:
            return None
        ana = commit_plan.py_analyses.get(rel_path)
        if ana is not None and (not ana.syntax_error) and all((u.kind == 'func_body' for u in ana.units)):
            return set(ana.changed_funcs)
        return None
    py_changed = _safe_changed_funcs('sqlglot/executor/python.py')
    ctx_changed = _safe_changed_funcs('sqlglot/executor/context.py')
    tbl_changed = _safe_changed_funcs('sqlglot/executor/table.py')
    ctx_eval_ok = ctx_changed is not None and (not ctx_changed & {'Context.eval', 'Context.eval_tuple'})
    tbl_core_ok = tbl_changed is not None and (not tbl_changed & {'Table.__init__', 'Table.append', 'Table.__getitem__', 'Table.__iter__', 'Table.add_columns', 'RowReader.__init__', 'RowReader.__getitem__', 'RangeReader.__init__', 'RangeReader.__getitem__'})
    ctx_untouched = ctx_eval_ok and (not ctx_changed & {'Context.__init__', 'Context.table', 'Context.set_row', 'Context.set_index', 'Context.set_range'})
    tbl_untouched = tbl_core_ok
    try:
        from sqlglot.executor.python import PythonExecutor as _PE
        from sqlglot.executor.context import Context as _Ctx
        from sqlglot.executor.table import RangeReader as _RR, Table as _Tbl
        if getattr(_PE, '_blast_fast_pe', False):
            return
        if ctx_untouched:

            def _fast_eval(self, code):
                if callable(code):
                    return code(self.env['scope'])
                return eval(code, self.env)

            def _fast_eval_tuple(self, codes):
                if callable(codes):
                    return codes(self.env['scope'])
                return tuple((self.eval(c) for c in codes))
            _Ctx.eval = _fast_eval
            _Ctx.eval_tuple = _fast_eval_tuple
            if tbl_untouched:

                def _fast_ctx_iter(self):
                    self.env['scope'] = self.row_readers
                    rows = self.table.rows
                    tbls = tuple(self.tables.values())
                    if len(tbls) == 1:
                        rdr = tbls[0].reader
                        for row in rows:
                            rdr.row = row
                            yield (rdr, self)
                    else:
                        rdrs = tuple((t.reader for t in tbls))
                        last_r = rdrs[-1]
                        for row in rows:
                            for r in rdrs:
                                r.row = row
                            yield (last_r, self)

                def _fast_filter(self, condition):
                    if callable(condition):
                        scope = self.row_readers
                        self.env['scope'] = scope
                        tbls = tuple(self.tables.values())
                        t0 = self.table
                        if len(tbls) == 1:
                            rdr = t0.reader
                            rows = []
                            for row in t0.rows:
                                rdr.row = row
                                if condition(scope):
                                    rows.append(row)
                            t0.rows = rows
                            return
                        rdrs = tuple((t.reader for t in tbls))
                        rows = []
                        for row in t0.rows:
                            for r in rdrs:
                                r.row = row
                            if condition(scope):
                                rows.append(row)
                        for tbl in tbls:
                            tbl.rows = rows
                        return
                    rows = [reader.row for reader, _ in self if self.eval(condition)]
                    for table in self.tables.values():
                        table.rows = rows

                def _fast_ctx_sort(self, key):
                    if not key:
                        return
                    if callable(key):
                        scope = self.row_readers
                        self.env['scope'] = scope
                        rdrs = tuple((t.reader for t in self.tables.values()))
                        if len(rdrs) == 1:
                            r0 = rdrs[0]

                            def _sort_key_1(row):
                                r0.row = row
                                return tuple(((v is None, v) for v in key(scope)))
                            self.table.rows.sort(key=_sort_key_1)
                        else:

                            def _sort_key_n(row):
                                for r in rdrs:
                                    r.row = row
                                return tuple(((v is None, v) for v in key(scope)))
                            self.table.rows.sort(key=_sort_key_n)
                        return

                    def _orig_sort_key(row):
                        self.set_row(row)
                        return tuple(((v is None, v) for v in self.eval_tuple(key)))
                    self.table.rows.sort(key=_orig_sort_key)
                if ctx_changed is not None and 'Context.__iter__' not in ctx_changed:
                    _Ctx.__iter__ = _fast_ctx_iter
                if ctx_changed is not None and 'Context.filter' not in ctx_changed:
                    _Ctx.filter = _fast_filter
                if ctx_changed is not None and 'Context.sort' not in ctx_changed:
                    _Ctx.sort = _fast_ctx_sort
        if tbl_untouched:

            def _fast_rr_getitem(self, column):
                r = self.range
                if not r:
                    return ()
                idx = self.table.reader.columns.get(column)
                if idx is None:
                    return (self.table[i][column] for i in r)
                rows = self.table.rows
                return (rows[i][idx] for i in r)
            _RR.__getitem__ = _fast_rr_getitem
        if py_changed is not None and ctx_untouched:
            env_untouched = 'sqlglot/executor/env.py' not in all_touched
            pygen_untouched = 'sqlglot/generators/python.py' not in all_touched
            from sqlglot.executor.table import TableIter as _TableIter
            try:
                import _joinkit as _FJC
            except Exception:
                _FJC = None
            _row_fn_cache: dict[Any, Any] = {}
            _PURE_BOOL_CALLS = frozenset({'AND', 'OR', 'NOT', 'EQ', 'NEQ', 'LT', 'LTE', 'GT', 'GTE', 'IN', 'BETWEEN', 'COALESCE', 'LOWER', 'UPPER', 'LENGTH', 'ABS', 'YEAR', 'MONTH', 'DAY'})
            _CMP_OP_MAP = {'NEQ': ast.NotEq, 'LT': ast.Lt, 'LTE': ast.LtE, 'GT': ast.Gt, 'GTE': ast.GtE}

            def _is_pure_bool_sub(node: ast.AST) -> bool:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        if not (isinstance(sub.func, ast.Name) and sub.func.id in _PURE_BOOL_CALLS):
                            return False
                    elif isinstance(sub, ast.BinOp) and isinstance(sub.op, (ast.Div, ast.FloorDiv, ast.Mod)):
                        return False
                return True

            def _compile_row_fn(src: str | None, ctx_tables: dict[Any, Any], env_dict: dict[str, Any], mode: str='val') -> Any:
                if not src or not (env_untouched and pygen_untouched):
                    return None
                col_map = {tname: tbl.reader.columns for tname, tbl in ctx_tables.items()}
                cache_key = (src, mode, tuple(((tn, tuple(cm.items())) for tn, cm in col_map.items())))
                if cache_key in _row_fn_cache:
                    return _row_fn_cache[cache_key]
                try:
                    tree = ast.parse(src, mode='eval')
                except Exception:
                    _row_fn_cache[cache_key] = None
                    return None

                class _ScopeToRow(ast.NodeTransformer):

                    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
                        if isinstance(node.value, ast.Subscript) and isinstance(node.value.value, ast.Name) and (node.value.value.id == 'scope') and isinstance(node.value.slice, ast.Constant) and isinstance(node.slice, ast.Constant):
                            t_cols = col_map.get(node.value.slice.value)
                            if t_cols is not None:
                                idx = t_cols.get(node.slice.value)
                                if idx is not None:
                                    return ast.Subscript(value=ast.Name(id='_r', ctx=ast.Load()), slice=ast.Constant(value=idx), ctx=ast.Load())
                        return self.generic_visit(node)
                tree = _ScopeToRow().visit(tree)
                for n in ast.walk(tree):
                    if isinstance(n, ast.Name) and n.id == 'scope':
                        _row_fn_cache[cache_key] = None
                        return None
                if mode == 'tuple_idxs' and isinstance(tree.body, ast.Tuple):
                    idxs: list[int] = []
                    for elt in tree.body.elts:
                        if isinstance(elt, ast.Subscript) and isinstance(elt.value, ast.Name) and (elt.value.id == '_r') and isinstance(elt.slice, ast.Constant) and (type(elt.slice.value) is int):
                            idxs.append(elt.slice.value)
                        else:
                            idxs = []
                            break
                    if idxs:
                        res_idx = ('idxs', tuple(idxs))
                        _row_fn_cache[cache_key] = res_idx
                        return res_idx
                fs_env: dict[str, Any] = {}
                if mode in ('filter', 'join_cond'):
                    tmp_id = [0]

                    def _non_none(n: ast.AST) -> bool:
                        return isinstance(n, ast.Constant) and n.value is not None

                    def _tx_bool(n: ast.AST, strict_is_true: bool) -> ast.AST:
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                            fn_id = n.func.id
                            if fn_id == 'AND' and len(n.args) == 2 and isinstance(n.args[0], ast.Lambda) and isinstance(n.args[1], ast.Lambda) and (not n.args[0].args.args) and (not n.args[1].args.args) and _is_pure_bool_sub(n.args[1].body):
                                return ast.BoolOp(op=ast.And(), values=[_tx_bool(n.args[0].body, False), _tx_bool(n.args[1].body, False)])
                            if fn_id == 'OR' and len(n.args) == 2 and isinstance(n.args[0], ast.Lambda) and isinstance(n.args[1], ast.Lambda) and (not n.args[0].args.args) and (not n.args[1].args.args) and _is_pure_bool_sub(n.args[1].body):
                                return ast.BoolOp(op=ast.Or(), values=[_tx_bool(n.args[0].body, False), _tx_bool(n.args[1].body, False)])
                            if fn_id == 'EQ' and len(n.args) == 2:
                                a, b = n.args
                                if _non_none(a) or _non_none(b):
                                    return ast.Compare(left=a, ops=[ast.Eq()], comparators=[b])
                                v0, v1 = (f'_v{tmp_id[0]}', f'_v{tmp_id[0] + 1}')
                                tmp_id[0] += 2
                                return ast.BoolOp(op=ast.And(), values=[ast.Compare(left=ast.NamedExpr(ast.Name(v0, ast.Store()), a), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=ast.NamedExpr(ast.Name(v1, ast.Store()), b), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=ast.Name(v0, ast.Load()), ops=[ast.Eq()], comparators=[ast.Name(v1, ast.Load())])])
                            if fn_id in _CMP_OP_MAP and len(n.args) == 2:
                                a, b = n.args
                                op_inst = _CMP_OP_MAP[fn_id]()
                                if _non_none(b):
                                    v0 = f'_v{tmp_id[0]}'
                                    tmp_id[0] += 1
                                    return ast.BoolOp(op=ast.And(), values=[ast.Compare(left=ast.NamedExpr(ast.Name(v0, ast.Store()), a), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=ast.Name(v0, ast.Load()), ops=[op_inst], comparators=[b])])
                                if _non_none(a):
                                    v0 = f'_v{tmp_id[0]}'
                                    tmp_id[0] += 1
                                    return ast.BoolOp(op=ast.And(), values=[ast.Compare(left=ast.NamedExpr(ast.Name(v0, ast.Store()), b), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=a, ops=[op_inst], comparators=[ast.Name(v0, ast.Load())])])
                                v0, v1 = (f'_v{tmp_id[0]}', f'_v{tmp_id[0] + 1}')
                                tmp_id[0] += 2
                                return ast.BoolOp(op=ast.And(), values=[ast.Compare(left=ast.NamedExpr(ast.Name(v0, ast.Store()), a), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=ast.NamedExpr(ast.Name(v1, ast.Store()), b), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=ast.Name(v0, ast.Load()), ops=[op_inst], comparators=[ast.Name(v1, ast.Load())])])
                            if fn_id == 'BETWEEN' and len(n.args) == 3 and _non_none(n.args[1]) and _non_none(n.args[2]):
                                v0 = f'_v{tmp_id[0]}'
                                tmp_id[0] += 1
                                return ast.BoolOp(op=ast.And(), values=[ast.Compare(left=ast.NamedExpr(ast.Name(v0, ast.Store()), n.args[0]), ops=[ast.IsNot()], comparators=[ast.Constant(None)]), ast.Compare(left=n.args[1], ops=[ast.LtE(), ast.LtE()], comparators=[ast.Name(v0, ast.Load()), n.args[2]])])
                            if fn_id == 'IN' and len(n.args) >= 2 and all((isinstance(c, ast.Constant) and c.value is not None and (type(c.value) in (int, float, str, bool)) for c in n.args[1:])):
                                fs_name = f'_fs{len(fs_env)}'
                                fs_env[fs_name] = frozenset((c.value for c in n.args[1:]))
                                return ast.Compare(left=n.args[0], ops=[ast.In()], comparators=[ast.Name(fs_name, ast.Load())])
                        if isinstance(n, ast.Compare):
                            return n
                        if strict_is_true:
                            return ast.Compare(left=n, ops=[ast.Is()], comparators=[ast.Constant(True)])
                        return ast.Call(func=ast.Name('bool', ast.Load()), args=[n], keywords=[])
                    tree.body = _tx_bool(tree.body, strict_is_true=mode == 'join_cond')
                lam_expr = ast.Expression(body=ast.Lambda(args=ast.arguments(posonlyargs=[], args=[ast.arg(arg='_r')], kwonlyargs=[], kw_defaults=[], defaults=[]), body=tree.body))
                ast.fix_missing_locations(lam_expr)
                try:
                    code_obj = compile(lam_expr, '<row>', 'eval', optimize=2)
                    eval_env = {**env_dict, **fs_env} if fs_env else env_dict
                    res_fn = eval(code_obj, eval_env)
                    _row_fn_cache[cache_key] = res_fn
                    return res_fn
                except Exception:
                    _row_fn_cache[cache_key] = None
                    return None
            if 'PythonExecutor.generate' not in py_changed:

                def _fast_gen(self, expression):
                    if not expression:
                        return None
                    expression = self._replace_subqueries(expression)
                    sql = self.generator.generate(expression)
                    try:
                        fn = eval(compile(f'lambda scope: ({sql})', sql, 'eval', optimize=2), self.env)
                        fn._blast_src = f'({sql})'
                        return fn
                    except Exception:
                        return compile(sql, sql, 'eval', optimize=2)
                _PE.generate = _fast_gen
                if 'PythonExecutor.generate_tuple' not in py_changed:

                    def _fast_gen_tuple(self, expressions):
                        if not expressions:
                            return ()
                        parts = []
                        for e in expressions:
                            if not e:
                                parts.append('None')
                            else:
                                e2 = self._replace_subqueries(e)
                                parts.append(f'({self.generator.generate(e2)})')
                        src = '(' + ', '.join(parts) + (',)' if len(parts) == 1 else ')')
                        try:
                            fn = eval(compile(f'lambda scope: {src}', '<tuple>', 'eval', optimize=2), self.env)
                            fn._blast_src = src
                            return fn
                        except Exception:
                            return tuple((self.generate(e) for e in expressions))
                    _PE.generate_tuple = _fast_gen_tuple
            if tbl_untouched and 'PythonExecutor._project_and_filter' not in py_changed:

                def _fast_project_and_filter_rows(self, context, step, rows_in):
                    sink = self.table(step.projections if step.projections else context.columns)
                    condition = self.generate(step.condition)
                    projections = self.generate_tuple(step.projections)
                    limit = step.offset + step.limit
                    has_lim = not math.isinf(limit)
                    c_call = callable(condition)
                    p_call = callable(projections) and len(step.projections) == len(sink.columns)
                    if (not condition or c_call) and (not projections or p_call):
                        if not condition and projections and (_FJC is not None) and (not has_lim or int(limit) > 0):
                            p_idxs = _compile_row_fn(getattr(projections, '_blast_src', None), context.tables, self.env, 'tuple_idxs')
                            if isinstance(p_idxs, tuple) and p_idxs[0] == 'idxs':
                                src_rows = rows_in if not has_lim else rows_in[:int(limit)]
                                sink.rows = _FJC.project_idxs(src_rows, p_idxs[1])
                                return sink
                        cond_r = _compile_row_fn(getattr(condition, '_blast_src', None), context.tables, self.env, 'filter') if condition else None
                        proj_r = _compile_row_fn(getattr(projections, '_blast_src', None), context.tables, self.env, 'val') if projections else None
                        if (not condition or cond_r is not None) and (not projections or proj_r is not None):
                            if not has_lim:
                                if cond_r and proj_r:
                                    sink.rows = [proj_r(r) for r in rows_in if cond_r(r)]
                                elif cond_r:
                                    sink.rows = [r for r in rows_in if cond_r(r)]
                                elif proj_r:
                                    sink.rows = [proj_r(r) for r in rows_in]
                                else:
                                    sink.rows = list(rows_in)
                                return sink
                            sink_rows = sink.rows
                            lim_i = int(limit)
                            if lim_i <= 0:
                                return sink
                            if cond_r and proj_r:
                                for r in rows_in:
                                    if cond_r(r):
                                        sink_rows.append(proj_r(r))
                                        if len(sink_rows) >= lim_i:
                                            break
                            elif cond_r:
                                for r in rows_in:
                                    if cond_r(r):
                                        sink_rows.append(r)
                                        if len(sink_rows) >= lim_i:
                                            break
                            elif proj_r:
                                sink.rows = [proj_r(r) for r in rows_in[:lim_i]]
                            else:
                                sink.rows = list(rows_in[:lim_i])
                            return sink
                    return None

                def _fast_project_and_filter(self, context, step, table_iter):
                    if isinstance(table_iter, _TableIter) and table_iter.index == -1:
                        fast_sink = _fast_project_and_filter_rows(self, context, step, table_iter.table.rows)
                        if fast_sink is not None:
                            return fast_sink
                    sink = self.table(step.projections if step.projections else context.columns)
                    condition = self.generate(step.condition)
                    projections = self.generate_tuple(step.projections)
                    limit = step.offset + step.limit
                    has_lim = not math.isinf(limit)
                    sink_rows = sink.rows
                    scope = context.row_readers
                    context.env['scope'] = scope
                    c_call = callable(condition)
                    p_call = callable(projections) and len(step.projections) == len(sink.columns)
                    if (not condition or c_call) and (not projections or p_call):
                        if condition and projections:
                            if has_lim:
                                for _ in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    if condition(scope):
                                        sink_rows.append(projections(scope))
                            else:
                                for _ in table_iter:
                                    if condition(scope):
                                        sink_rows.append(projections(scope))
                        elif condition:
                            if has_lim:
                                for reader in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    if condition(scope):
                                        sink_rows.append(reader.row)
                            else:
                                for reader in table_iter:
                                    if condition(scope):
                                        sink_rows.append(reader.row)
                        elif projections:
                            if has_lim:
                                for _ in table_iter:
                                    if len(sink_rows) >= limit:
                                        break
                                    sink_rows.append(projections(scope))
                            else:
                                for _ in table_iter:
                                    sink_rows.append(projections(scope))
                        elif has_lim:
                            for reader in table_iter:
                                if len(sink_rows) >= limit:
                                    break
                                sink_rows.append(reader.row)
                        else:
                            for reader in table_iter:
                                sink_rows.append(reader.row)
                        return sink
                    for reader in table_iter:
                        if len(sink) >= limit:
                            break
                        if condition and (not context.eval(condition)):
                            continue
                        if projections:
                            sink.append(context.eval_tuple(projections))
                        else:
                            sink.append(reader.row)
                    return sink
                _PE._project_and_filter = _fast_project_and_filter
                if 'PythonExecutor.join' not in py_changed:

                    def _fast_join(self, step, context):
                        source = step.source_name
                        source_table = context.tables[source]
                        source_context = self.context({source: source_table})
                        column_ranges = {source: range(0, len(source_table.columns))}
                        for name, join in step.joins.items():
                            table = context.tables[name]
                            start = max((r.stop for r in column_ranges.values()))
                            column_ranges[name] = range(start, len(table.columns) + start)
                            join_context = self.context({name: table})
                            condition = self.generate(join['condition'])
                            condition_context = self.context({n: _Tbl(source_context.columns + join_context.columns, column_range=column_range) for n, column_range in column_ranges.items()}) if condition else None
                            if join.get('source_key'):
                                table = self.hash_join(join, source_context, join_context, condition, condition_context)
                            else:
                                table = self.nested_loop_join(join, source_context, join_context, condition, condition_context)
                            source_context = self.context({n: _Tbl(table.columns, table.rows, column_range) for n, column_range in column_ranges.items()})
                        if not step.condition and (not step.projections):
                            return source_context
                        sink = _fast_project_and_filter_rows(self, source_context, step, source_context.table.rows)
                        if sink is None:
                            sink = self._project_and_filter(source_context, step, (reader for reader, _ in iter(source_context)))
                        if step.projections:
                            return self.context({step.name: sink})
                        return self.context({n: _Tbl(t.columns, sink.rows, t.column_range) for n, t in source_context.tables.items()})
                    _PE.join = _fast_join
            if tbl_untouched and (not py_changed & {'PythonExecutor.hash_join', 'PythonExecutor._join_matches', 'PythonExecutor._append_unmatched_join_rows'}):
                _orig_hash_join = _PE.hash_join

                def _fast_append_unmatched(table, join, source_rows, join_rows, matched_source, matched_join):
                    side = join.get('side')
                    out_rows = table.rows
                    if side in ('LEFT', 'FULL') and source_rows:
                        join_nulls = (None,) * (len(table.columns) - len(source_rows[0]))
                        if not matched_source:
                            out_rows.extend((r + join_nulls for r in source_rows))
                        else:
                            out_rows.extend((r + join_nulls for i, r in enumerate(source_rows) if i not in matched_source))
                    if side in ('RIGHT', 'FULL') and join_rows:
                        source_nulls = (None,) * (len(table.columns) - len(join_rows[0]))
                        if not matched_join:
                            out_rows.extend((source_nulls + r for r in join_rows))
                        else:
                            out_rows.extend((source_nulls + r for i, r in enumerate(join_rows) if i not in matched_join))

                def _fast_hash_join(self, join, source_context, join_context, condition, condition_context):
                    source_key = self.generate_tuple(join['source_key'])
                    join_key = self.generate_tuple(join['join_key'])
                    if not (callable(source_key) and callable(join_key) and (condition is None or callable(condition))):
                        return _orig_hash_join(self, join, source_context, join_context, condition, condition_context)
                    side = join.get('side')
                    need_matched = side in ('LEFT', 'RIGHT', 'FULL')
                    klen = len(join['join_key'])
                    j_rows = join_context.table.rows
                    s_rows = source_context.table.rows
                    out_cols = source_context.columns + join_context.columns
                    j_fast = _compile_row_fn(getattr(join_key, '_blast_src', None), join_context.tables, self.env, 'tuple_idxs')
                    s_fast = _compile_row_fn(getattr(source_key, '_blast_src', None), source_context.tables, self.env, 'tuple_idxs')
                    c_fast = _compile_row_fn(getattr(condition, '_blast_src', None), condition_context.tables, self.env, 'join_cond') if condition is not None and condition_context is not None else None
                    join_map: dict[Any, list[Any]] = {}
                    use_scalar_key = False
                    if isinstance(j_fast, tuple) and j_fast[0] == 'idxs' and isinstance(s_fast, tuple) and (s_fast[0] == 'idxs') and (len(j_fast[1]) == klen) and (len(s_fast[1]) == klen):
                        j_idxs = j_fast[1]
                        s_idxs = s_fast[1]
                        if _FJC is not None and condition is None and (not need_matched):
                            if klen == 1:
                                return _Tbl(out_cols, rows=_FJC.inner_hash_join_1(s_rows, j_rows, s_idxs[0], j_idxs[0]))
                            if klen == 2:
                                return _Tbl(out_cols, rows=_FJC.inner_hash_join_2(s_rows, j_rows, s_idxs[0], s_idxs[1], j_idxs[0], j_idxs[1]))
                        if klen == 1:
                            use_scalar_key = True
                            ji0 = j_idxs[0]
                            for idx, row in enumerate(j_rows):
                                k = row[ji0]
                                if k is not None:
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                        elif klen == 2:
                            ji0, ji1 = j_idxs
                            for idx, row in enumerate(j_rows):
                                k0 = row[ji0]
                                if k0 is not None:
                                    k1 = row[ji1]
                                    if k1 is not None:
                                        k = (k0, k1)
                                        grp = join_map.get(k)
                                        if grp is None:
                                            join_map[k] = [(idx, row)]
                                        else:
                                            grp.append((idx, row))
                        else:
                            for idx, row in enumerate(j_rows):
                                k = tuple((row[i] for i in j_idxs))
                                if all((v is not None for v in k)):
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                        if not join_map:
                            table = _Tbl(out_cols)
                            if need_matched:
                                _fast_append_unmatched(table, join, s_rows, j_rows, set(), set())
                            return table
                        src_groups: dict[Any, list[Any]] = {}
                        if use_scalar_key:
                            si0 = s_idxs[0]
                            for idx, row in enumerate(s_rows):
                                k = row[si0]
                                if k is not None and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                        elif klen == 2:
                            si0, si1 = s_idxs
                            for idx, row in enumerate(s_rows):
                                k0 = row[si0]
                                if k0 is not None:
                                    k1 = row[si1]
                                    if k1 is not None:
                                        k = (k0, k1)
                                        if k in join_map:
                                            sg = src_groups.get(k)
                                            if sg is None:
                                                src_groups[k] = [(idx, row)]
                                            else:
                                                sg.append((idx, row))
                        else:
                            for idx, row in enumerate(s_rows):
                                k = tuple((row[i] for i in s_idxs))
                                if all((v is not None for v in k)) and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                    else:
                        j_tbls = tuple(join_context.tables.values())
                        j_scope = join_context.row_readers
                        join_context.env['scope'] = j_scope
                        if len(j_tbls) == 1:
                            j_rdr = j_tbls[0].reader
                            for idx, row in enumerate(j_rows):
                                j_rdr.row = row
                                k = join_key(j_scope)
                                if all((v is not None for v in k)):
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                        else:
                            j_rdrs = tuple((t.reader for t in j_tbls))
                            for idx, row in enumerate(j_rows):
                                for r in j_rdrs:
                                    r.row = row
                                k = join_key(j_scope)
                                if all((v is not None for v in k)):
                                    grp = join_map.get(k)
                                    if grp is None:
                                        join_map[k] = [(idx, row)]
                                    else:
                                        grp.append((idx, row))
                        if not join_map:
                            table = _Tbl(out_cols)
                            if need_matched:
                                _fast_append_unmatched(table, join, s_rows, j_rows, set(), set())
                            return table
                        s_tbls = tuple(source_context.tables.values())
                        s_scope = source_context.row_readers
                        source_context.env['scope'] = s_scope
                        src_groups = {}
                        if len(s_tbls) == 1:
                            s_rdr = s_tbls[0].reader
                            for idx, row in enumerate(s_rows):
                                s_rdr.row = row
                                k = source_key(s_scope)
                                if all((v is not None for v in k)) and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                        else:
                            s_rdrs = tuple((t.reader for t in s_tbls))
                            for idx, row in enumerate(s_rows):
                                for r in s_rdrs:
                                    r.row = row
                                k = source_key(s_scope)
                                if all((v is not None for v in k)) and k in join_map:
                                    sg = src_groups.get(k)
                                    if sg is None:
                                        src_groups[k] = [(idx, row)]
                                    else:
                                        sg.append((idx, row))
                    out_rows: list[Any] = []
                    matched_source: set[int] = set()
                    matched_join: set[int] = set()
                    if condition is None:
                        if not need_matched:
                            for k, sg in src_groups.items():
                                jg = join_map[k]
                                if len(sg) == 1 and len(jg) == 1:
                                    out_rows.append(sg[0][1] + jg[0][1])
                                else:
                                    for _, sr in sg:
                                        for _, jr in jg:
                                            out_rows.append(sr + jr)
                        else:
                            for k, sg in src_groups.items():
                                jg = join_map[k]
                                for si, sr in sg:
                                    matched_source.add(si)
                                    for ji, jr in jg:
                                        out_rows.append(sr + jr)
                                        matched_join.add(ji)
                    elif c_fast is not None:
                        if not need_matched:
                            for k, sg in src_groups.items():
                                jg = join_map[k]
                                for _, sr in sg:
                                    for _, jr in jg:
                                        row = sr + jr
                                        if c_fast(row):
                                            out_rows.append(row)
                        else:
                            for k, sg in src_groups.items():
                                jg = join_map[k]
                                for si, sr in sg:
                                    for ji, jr in jg:
                                        row = sr + jr
                                        if c_fast(row):
                                            out_rows.append(row)
                                            matched_source.add(si)
                                            matched_join.add(ji)
                    else:
                        c_scope = condition_context.row_readers
                        c_rdrs = tuple((t.reader for t in condition_context.tables.values()))
                        condition_context.env['scope'] = c_scope
                        for k, sg in src_groups.items():
                            jg = join_map[k]
                            for si, sr in sg:
                                for ji, jr in jg:
                                    row = sr + jr
                                    for r in c_rdrs:
                                        r.row = row
                                    if condition(c_scope) is True:
                                        out_rows.append(row)
                                        if need_matched:
                                            matched_source.add(si)
                                            matched_join.add(ji)
                    table = _Tbl(out_cols, rows=out_rows)
                    if need_matched:
                        _fast_append_unmatched(table, join, s_rows, j_rows, matched_source, matched_join)
                    return table
                _PE.hash_join = _fast_hash_join
            if tbl_untouched and 'PythonExecutor.aggregate' not in py_changed:
                _orig_agg = _PE.aggregate

                def _fast_aggregate(self, step, context):
                    group_by = self.generate_tuple(step.group.values())
                    aggregations = self.generate_tuple(step.aggregations)
                    operands = self.generate_tuple(step.operands)
                    if group_by and (not callable(group_by)) or (aggregations and (not callable(aggregations))) or (operands and (not callable(operands))):
                        return _orig_agg(self, step, context)
                    if operands:
                        op_cols = self.table(step.operands).columns
                        op_rows = []
                        scope = context.row_readers
                        context.env['scope'] = scope
                        rows_in = context.table.rows
                        rdrs = tuple((t.reader for t in context.tables.values()))
                        if len(rdrs) == 1:
                            r0 = rdrs[0]
                            for row in rows_in:
                                r0.row = row
                                op_rows.append(row + operands(scope))
                        else:
                            for row in rows_in:
                                for r in rdrs:
                                    r.row = row
                                op_rows.append(row + operands(scope))
                        context.table.rows[:] = op_rows
                        width = len(context.columns)
                        context.add_columns(*op_cols)
                        operand_table = _Tbl(context.columns, context.table.rows, range(width, width + len(op_cols)))
                        context = self.context({None: operand_table, **context.tables})
                    if group_by:
                        context.sort(group_by)
                    rows = context.table.rows
                    length = len(rows)
                    table = self.table(list(step.group) + step.aggregations)
                    out_rows = table.rows
                    rg_scope = context.range_readers
                    rrs = tuple((rg_scope[n] for n in context.tables))
                    if length:
                        has_lim = not step.condition and (not math.isinf(step.limit))
                        lim = step.offset + step.limit
                        if has_lim and lim <= 0:
                            pass
                        elif not group_by:
                            rg = range(0, length)
                            for rr in rrs:
                                rr.range = rg
                            context.env['scope'] = rg_scope
                            out_rows.append(aggregations(rg_scope) if aggregations else ())
                        else:
                            r_scope = context.row_readers
                            rdrs = tuple((t.reader for t in context.tables.values()))
                            group = None
                            start = 0
                            for i, row in enumerate(rows):
                                for r in rdrs:
                                    r.row = row
                                context.env['scope'] = r_scope
                                key = group_by(r_scope)
                                if group is None:
                                    group = key
                                elif key != group:
                                    rg = range(start, i)
                                    for rr in rrs:
                                        rr.range = rg
                                    context.env['scope'] = rg_scope
                                    out_rows.append(group + (aggregations(rg_scope) if aggregations else ()))
                                    group = key
                                    start = i
                                    if has_lim and len(out_rows) >= lim:
                                        break
                            else:
                                rg = range(start, length)
                                for rr in rrs:
                                    rr.range = rg
                                context.env['scope'] = rg_scope
                                out_rows.append(group + (aggregations(rg_scope) if aggregations else ()))
                    elif step.limit > 0 and (not group_by):
                        rg = range(0, 0)
                        for rr in rrs:
                            rr.range = rg
                        context.env['scope'] = rg_scope
                        out_rows.append(aggregations(rg_scope) if aggregations else ())
                    context = self.context({step.name: table, **{name: table for name in context.tables}})
                    if step.projections or step.condition:
                        return self.context({step.name: self._project_and_filter(context, step, context.table_iter(step.name))})
                    return context
                _PE.aggregate = _fast_aggregate
        _PE._blast_fast_pe = True
    except Exception:
        pass
_SPEED_SCALED: bool = False

def _measure_speed_scale(is_c: bool=True) -> float:
    global _SPEED_SCALE, _SPEED_SCALED
    if _SPEED_SCALED:
        return _SPEED_SCALE
    _SPEED_SCALED = True
    try:
        import sqlglot as _sg_sp
        _sql = 'SELECT a, COALESCE(b, 1) AS c, SUM(d) OVER (PARTITION BY a ORDER BY b) FROM t1 JOIN t2 ON t1.id = t2.id WHERE c > 10 GROUP BY a, b'
        _sg_sp.parse_one(_sql)
        _t0 = time.process_time()
        for _ in range(40):
            _sg_sp.parse_one(_sql)
        _dt_ms = (time.process_time() - _t0) * 1000.0
        _ref_ms = 3.8 if is_c else 6.6
        _scale = _dt_ms / _ref_ms
        _SPEED_SCALE = 1.0 if _scale < 1.0 else 2.2 if _scale > 2.2 else _scale
    except Exception:
        _SPEED_SCALE = 1.0
    return _SPEED_SCALE

def _patch_executor_module(maps: dict[str, Any], helpers_patched: bool=False, allow_duck_cache: bool=True) -> None:
    texec = sys.modules.get('tests.test_executor')
    if texec is None or getattr(texec, '_blast_patched', False):
        return
    import tests.helpers as th

    @classmethod
    def _lazy_exec_setup_class(cls):
        cp = ACTIVE_COMMIT_PLAN
        if cp is not None:
            _install_fast_python_executor(cp)
        touched = set(cp.patched_files) | set(cp.deleted_files) if cp is not None else set()
        allow_pkl = not helpers_patched or allow_duck_cache
        cls._tpch_conn = None
        cls._tpcds_conn = None
        cls._tpch_views_loaded = set()
        cls._tpcds_views_loaded = set()
        cls.tpch_tables = _LazyExecutorTables(texec.DIR_TPCH, th.TPCH_SCHEMA, tag='tpch', touched_files=touched, allow_pkl=allow_pkl)
        cls.tpcds_tables = _LazyExecutorTables(texec.DIR_TPCDS, th.TPCDS_SCHEMA, tag='tpcds', touched_files=touched, allow_pkl=allow_pkl)
        cls.cache = {}
        cls._tpch_sqls = None
        cls._tpcds_sqls = None

    @classmethod
    def _lazy_exec_teardown_class(cls):
        if getattr(cls, '_tpch_conn', None) is not None:
            cls._tpch_conn.close()
        if getattr(cls, '_tpcds_conn', None) is not None:
            cls._tpcds_conn.close()

    def _get_duck_conn(self, tpch: bool, needed_tables: list[str] | None=None):
        cp_d = ACTIVE_COMMIT_PLAN
        touched_d = set(cp_d.patched_files) | set(cp_d.deleted_files) if cp_d is not None else set()
        if tpch:
            if self._tpch_conn is None:
                db_p = Path('/app/duckdb_tpch.db')
                csv_touched = any(('tpc-h/' in r and r.endswith('.csv.gz') for r in touched_d))
                if db_p.is_file() and (not csv_touched) and (not helpers_patched or allow_duck_cache):
                    import duckdb
                    self._tpch_conn = duckdb.connect(str(db_p), read_only=True, config={'threads': 1})
                    self._tpch_views_loaded.update(th.TPCH_SCHEMA.keys())
                    return self._tpch_conn
                self._tpch_conn = _duckdb_connect_1t()
            conn = self._tpch_conn
            loaded = self._tpch_views_loaded
            directory = texec.DIR_TPCH
            schema = th.TPCH_SCHEMA
        else:
            if self._tpcds_conn is None:
                db_p = Path('/app/duckdb_tpcds.db')
                csv_touched = any(('tpc-ds/' in r and r.endswith('.csv.gz') for r in touched_d))
                if db_p.is_file() and (not csv_touched) and (not helpers_patched or allow_duck_cache):
                    import duckdb
                    self._tpcds_conn = duckdb.connect(str(db_p), read_only=True, config={'threads': 1})
                    self._tpcds_views_loaded.update(th.TPCDS_SCHEMA.keys())
                    return self._tpcds_conn
                self._tpcds_conn = _duckdb_connect_1t()
            conn = self._tpcds_conn
            loaded = self._tpcds_views_loaded
            directory = texec.DIR_TPCDS
            schema = th.TPCDS_SCHEMA
        targets = needed_tables if needed_tables is not None else list(schema.keys())
        for table in targets:
            if table in loaded or table not in schema:
                continue
            file_name = f'{directory}{table}.csv.gz'
            conn.execute(f"CREATE TABLE {table} AS SELECT * FROM READ_CSV('{file_name}', delim='|', header=True, columns={schema[table]})")
            loaded.add(table)
        return conn

    def _load_sqls(self, tpch: bool):
        orig_filter_key = 'tests/fixtures/optimizer/tpc-h/tpc-h.sql' if tpch else 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql'
        saved = ACTIVE_FIXTURE_FILTERS.pop(orig_filter_key, None)
        try:
            if tpch:
                return list(th.load_sql_fixture_pairs('optimizer/tpc-h/tpc-h.sql'))
            return list(th.load_sql_fixture_pairs('optimizer/tpc-ds/tpc-ds.sql'))
        finally:
            if saved is not None:
                ACTIVE_FIXTURE_FILTERS[orig_filter_key] = saved

    def _fast_mp_execute(self, tpch: bool):
        from sqlglot import find_tables, parse_one, transpile
        from sqlglot.executor import execute as sg_execute
        from sqlglot.executor.python import PythonExecutor
        from sqlglot.executor.table import ensure_tables
        from sqlglot.optimizer import optimize
        from sqlglot.planner import Plan
        from sqlglot.schema import ensure_schema
        tag = 'tpc-h' if tpch else 'tpc-ds'
        schema = th.TPCH_SCHEMA if tpch else th.TPCDS_SCHEMA
        tables = self.tpch_tables if tpch else self.tpcds_tables
        sqls = _load_sqls(self, tpch)
        qmeta_map = maps['tpch_meta'] if tpch else maps['tpcds_meta']
        cp = ACTIVE_COMMIT_PLAN
        assert cp is not None
        all_touched = set(cp.patched_files) | set(cp.deleted_files)
        fixture_touched = (cp.tpch_fixture_touched if tpch else cp.tpcds_fixture_touched) or any((tag in r for r in all_touched))
        executor_init_touched = 'sqlglot/executor/__init__.py' in all_touched
        executor_dir_touched = any((r.startswith('sqlglot/executor/') or r in ('tests/test_executor.py', 'tests/helpers.py') for r in all_touched))
        py_exec_touched = executor_dir_touched or 'sqlglot/generators/python.py' in all_touched or 'sqlglot/optimizer/scope.py' in all_touched
        duck_untouched = not any((r.startswith(('sqlglot/parser', 'sqlglot/tokens', 'sqlglot/tokenizer', 'sqlglot/expressions/')) or r in ('sqlglot/generator.py', 'sqlglot/generators/__init__.py', 'sqlglot/generators/duckdb.py', 'sqlglot/dialects/__init__.py', 'sqlglot/dialects/dialect.py', 'sqlglot/dialects/duckdb.py', 'sqlglot/helper.py', 'sqlglot/__init__.py') for r in all_touched))
        _LATE_OPT_FILES = frozenset({'sqlglot/optimizer/normalize.py', 'sqlglot/optimizer/unnest_subqueries.py', 'sqlglot/optimizer/pushdown_predicates.py', 'sqlglot/optimizer/optimize_joins.py', 'sqlglot/optimizer/eliminate_subqueries.py', 'sqlglot/optimizer/merge_subqueries.py', 'sqlglot/optimizer/eliminate_joins.py', 'sqlglot/optimizer/eliminate_ctes.py', 'sqlglot/optimizer/canonicalize.py'})
        late_opt_pq_ok = not tpch and (not helpers_patched or allow_duck_cache) and (not fixture_touched) and bool(all_touched) and all((r in _LATE_OPT_FILES for r in all_touched))
        _pq_rules = None
        if late_opt_pq_ok:
            from sqlglot.optimizer import RULES as _OPT_RULES
            _pq_rules = _OPT_RULES[2:]
        planner_touched = any((r in ('sqlglot/planner.py', 'sqlglot/optimizer/eliminate_joins.py', 'sqlglot/optimizer/scope.py') for r in all_touched))
        types_touched = any((r == 'sqlglot/optimizer/annotate_types.py' or r.startswith('sqlglot/typing/') or (r == 'sqlglot/dialects/dialect.py' and cp.exec_engine_touched) for r in all_touched))
        shared_files = [r for r in all_touched if r in ('sqlglot/generator.py', 'sqlglot/dialects/dialect.py', 'sqlglot/helper.py', 'sqlglot/schema.py') or r.startswith('sqlglot/expressions/')]
        shared_exec_all = False
        shared_exec_qidxs: set[int] = set()
        if shared_files and (not py_exec_touched):
            all_func_only = all((r in cp.py_analyses and all((u.kind == 'func_body' for u in cp.py_analyses[r].units)) for r in shared_files))
            sgs = ACTIVE_ENGINE._get_subtests(ACTIVE_TNUM) if ACTIVE_ENGINE is not None and ACTIVE_TNUM >= 0 else []
            exec_sg = next((sg for sg in sgs if sg.get('tag') == tag and 'exec_lines' in sg), None)
            if all_func_only and exec_sg is not None:
                el_map = exec_sg['exec_lines']
                for r in shared_files:
                    rmap = el_map.get(r)
                    if not rmap:
                        continue
                    for u in cp.py_analyses[r].units:
                        for ln in u.old_lines:
                            cids = rmap.get(ln)
                            if cids:
                                shared_exec_qidxs.update(cids)
            else:
                shared_exec_all = True
        allowed = ACTIVE_FIXTURE_FILTERS.get(tag, None)
        indices = list(range(1, len(sqls) + 1)) if allowed is None else sorted((i for i in allowed if 1 <= i <= len(sqls)))
        if not (py_exec_touched or shared_exec_all or planner_touched):
            indices.sort(key=lambda qi: (qmeta_map.get(qi, {}).get('sec_opt', 0.05), qmeta_map.get(qi, {}).get('sec_exec', 0.5)))
        else:
            indices.sort(key=lambda qi: qmeta_map.get(qi, {}).get('sec_opt', 0.1) + qmeta_map.get(qi, {}).get('sec_exec', 0.5))
        if not tpch and LAST_FAILED_TPCDS_QIDX and (LAST_FAILED_TPCDS_QIDX in indices):
            indices.remove(LAST_FAILED_TPCDS_QIDX)
            indices.insert(0, LAST_FAILED_TPCDS_QIDX)
        _install_fast_python_executor(cp)
        _install_fast_optimizer_argspec(cp)
        pygen_dirty_qidxs: set[int] | None = None
        if not tpch and (not helpers_patched or allow_duck_cache) and (not fixture_touched) and (all_touched == {'sqlglot/generators/python.py'}) and (ACTIVE_REPLAY_SCOPE is not None) and (ACTIVE_REPLAY_SCOPE.fine_tokens is not None):
            _pyg_inv = rc.load_tpc_pygen_fine()
            if _pyg_inv is not None:
                pygen_dirty_qidxs = set()
                for _tok in ACTIVE_REPLAY_SCOPE.fine_tokens:
                    _qs = _pyg_inv.get(_tok)
                    if _qs:
                        pygen_dirty_qidxs.update(_qs)
        _tpc_ckp_rules = None
        _tpc_ckp_min_k = 0
        _tpc_ckp_max_k = 0
        if not tpch and (not helpers_patched or allow_duck_cache) and (not fixture_touched) and (ACTIVE_REPLAY_SCOPE is not None) and ACTIVE_REPLAY_SCOPE.is_opt_only and bool(ACTIVE_REPLAY_SCOPE.opt_dirty_rules):
            from sqlglot.optimizer import RULES as _TPC_OPT_RULES
            _tpc_ckp_rules = _TPC_OPT_RULES
            _tpc_ckp_min_k = min(ACTIVE_REPLAY_SCOPE.opt_dirty_rules)
            _tpc_ckp_max_k = max(ACTIVE_REPLAY_SCOPE.opt_dirty_rules)
        schema_obj = ensure_schema(schema)
        check_pickle = any((rel in ('sqlglot/serde.py', 'sqlglot/expressions/core.py') or rel.startswith('sqlglot/expressions/') for rel in cp.patched_files))
        np_mod = None
        pd_mod = None
        afe_fn = None
        duckdb_only_tpc = not tpch and (not helpers_patched or allow_duck_cache) and (not fixture_touched) and (not check_pickle) and bool(all_touched) and all((r in ('sqlglot/dialects/duckdb.py', 'sqlglot/parsers/duckdb.py', 'sqlglot/generators/duckdb.py', 'sqlglot/typing/duckdb.py') for r in all_touched))
        tpc_opt_cache_ok = duckdb_only_tpc or (not tpch and (not helpers_patched or allow_duck_cache) and (not fixture_touched) and (not check_pickle) and bool(all_touched) and all((r in ('sqlglot/planner.py', 'sqlglot/generators/python.py') or r.startswith('sqlglot/executor/') for r in all_touched)))
        from sqlglot.expressions import core as _sg_ec
        _ec_file = getattr(_sg_ec, '__file__', '') or ''
        if '/sqlglot-c-base/' in _ec_file:
            root_tag = 'cb'
        elif '/sqlglot-c-noopt/' in _ec_file:
            root_tag = 'noopt'
        elif '/sqlglot-c/' in _ec_file or _ec_file.endswith('.so'):
            root_tag = 'c'
        else:
            root_tag = 'py'
        rem_outer_init = signal.getitimer(signal.ITIMER_REAL)[0]
        outer_has_timer = rem_outer_init > 0.0
        outer_deadline = time.monotonic() + (rem_outer_init if outer_has_timer else 45.0)
        cpu_budget = ACTIVE_CPU_BUDGET_SEC
        cpu_t0 = time.process_time()
        q_weights = [qmeta_map.get(qi, {}).get('sec_opt', 0.05) + qmeta_map.get(qi, {}).get('sec_exec', 0.25) for qi in indices]
        total_w = sum(q_weights)
        done_w = 0.0
        q_done = 0
        q_opt_w = [qmeta_map.get(qi, {}).get('sec_opt', 0.05) for qi in indices]
        pj = [cpu_t0, 0.0, 0.0, 0.0, 0.0, 0, 0, getattr(tables, '_load_cpu', 0.0)]

        def _check_cpu_budget(q_pos_done: int, was_heavy: bool, was_exec: bool) -> None:
            if cpu_budget is None or cpu_budget <= 0.0:
                return
            now = time.process_time()
            cur_load = getattr(tables, '_load_cpu', 0.0)
            load_delta = max(0.0, cur_load - pj[7])
            pj[7] = cur_load
            if q_pos_done >= 1 and was_exec:
                dc = max(0.0, now - pj[0] - load_delta)
                if was_heavy:
                    pj[3] += dc
                    pj[4] += q_weights[q_pos_done]
                    pj[6] += 1
                else:
                    pj[1] += dc
                    pj[2] += q_opt_w[q_pos_done]
                    pj[5] += 1
            pj[0] = now
            cpu_spent = now - cpu_t0
            nxt = q_pos_done + 1
            if nxt >= len(indices):
                return
            n_s = pj[5] + pj[6]
            if n_s >= 4 and cpu_spent >= 0.3:
                conf = 0.5 if n_s < 8 else 0.75 if n_s < 16 else 1.0
                f_h = pj[6] / n_s
                r_h = pj[3] / pj[4] if pj[4] > 0.0 else 0.0
                r_l = pj[1] / pj[2] if pj[2] > 0.0 else r_h
                rem_opt = sum(q_opt_w[nxt:])
                rem_full = sum(q_weights[nxt:])
                if conf * ((1.0 - f_h) * r_l * rem_opt + f_h * r_h * rem_full) > cpu_budget:
                    if _PROF:
                        print(f'[d] proj-abort {tag} done={nxt}/{len(indices)} spent={cpu_spent:.3f} budget={cpu_budget:.2f} light={pj[1]:.3f}cpu/{pj[2]:.3f}w n={pj[5]} heavy={pj[3]:.3f}cpu/{pj[4]:.3f}w n={pj[6]} rem_opt={rem_opt:.2f} rem_full={rem_full:.2f}', file=sys.stderr)
                    _econ_abort_now()
        ran_heavy = False
        q_exec = True
        try:
            for q_pos, qidx in enumerate(indices):
                args, sql, _ = sqls[qidx - 1]
                qm = qmeta_map.get(qidx)
                if cpu_budget is not None and q_pos > 0:
                    _check_cpu_budget(q_pos - 1, ran_heavy, q_exec)
                q_exec = True
                if outer_has_timer:
                    rem_outer = outer_deadline - time.monotonic()
                    if rem_outer <= 0.05:
                        raise _TestTimeout()
                    q_sec = qm.get('sec_opt', 0.05) + qm.get('sec_exec', 0.25) if qm else 0.5
                    q_budget = max(3.5, 6.0 * q_sec + 2.5)
                    signal.setitimer(signal.ITIMER_REAL, min(q_budget, rem_outer))
                ran_heavy = False
                with self.subTest(f'{tag} {qidx}'):
                    if not args.get('execute'):
                        expr = parse_one(sql)
                        if check_pickle:
                            expr = pickle.loads(pickle.dumps(expr))
                        q_exec = False
                        done_w += q_weights[q_pos]
                        q_done += 1
                        continue
                    base_ok = (not helpers_patched or allow_duck_cache) and qm is not None and qm.get('execute') and (not fixture_touched)
                    if pygen_dirty_qidxs is not None and base_ok and (qidx not in pygen_dirty_qidxs):
                        done_w += q_weights[q_pos]
                        q_done += 1
                        continue
                    cached_plan_only = _load_tpc_plan_query(qidx, root_tag) if tpc_opt_cache_ok and base_ok and (not planner_touched) else None
                    cached_opt_ast = _load_tpc_opt_ast_query(qidx) if tpc_opt_cache_ok and base_ok and planner_touched else None
                    cached_entry = (None, cached_plan_only) if cached_plan_only is not None else ('AST', cached_opt_ast) if cached_opt_ast is not None else _load_tpc_cache_query(qidx, root_tag) if tpc_opt_cache_ok and base_ok else None
                    if duckdb_only_tpc and base_ok and (cached_entry is not None):
                        duck_sql = transpile(sql, write='duckdb')[0]
                        duck_hash = hashlib.blake2b(duck_sql.encode(), digest_size=12).hexdigest()
                        duck_unchanged = duck_hash == qm.get('duck_sql_hash')
                        if duck_unchanged:
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        expr = parse_one(sql)
                        t_names = [tbl.name for tbl in find_tables(expr)]
                        cached_rows = _load_tpc_rows_query(qidx)
                        if cached_rows is not None:
                            from sqlglot.executor.table import Table as _ETbl
                            table = _ETbl(columns=cached_rows[0], rows=cached_rows[1])
                        else:
                            ran_heavy = True
                            plan = cached_entry[1]
                            q_tables = {name: tables[name] for name in t_names}
                            tables_ = ensure_tables(q_tables)
                            table = PythonExecutor(tables=tables_).execute(plan)
                    elif cached_entry is not None and (not executor_init_touched):
                        expr = None
                        duck_sql = None
                        duck_unchanged = True
                        opt_unchanged = True
                        py_exec_q = py_exec_touched or shared_exec_all or qidx in shared_exec_qidxs
                        if not planner_touched:
                            plan = cached_entry[1]
                            det_match = bool(qm.get('det_plan_hash'))
                        else:
                            if cached_entry[0] == 'AST':
                                from sqlglot import exp as _sg_exp
                                optimized = rc.unpack_expr(cached_entry[1], _sg_exp)
                            else:
                                optimized = _unpack_expr(cached_entry[0])
                            plan = Plan(optimized)
                            det_h = _hash_plan_det(plan) if qm.get('det_plan_hash') else None
                            det_match = det_h is not None and det_h == qm['det_plan_hash']
                        if not py_exec_q and det_match:
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        if not executor_dir_touched and det_match and qm.get('py_code_hash') and (_hash_plan_py_code(plan) == qm['py_code_hash']):
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        ran_heavy = True
                        expr = parse_one(sql)
                        q_tables = {}
                        t_names = []
                        for tbl in find_tables(expr):
                            name = tbl.name
                            q_tables[name] = tables[name]
                            t_names.append(name)
                        tables_ = ensure_tables(q_tables)
                        table = PythonExecutor(tables=tables_).execute(plan)
                    else:
                        ckp_path = Path(f'/app/tpc_pq/{qidx}.ckp') if _tpc_ckp_rules is not None and (not check_pickle) and base_ok else None
                        pq_path = Path(f'/app/tpc_pq/{qidx}.ast') if late_opt_pq_ok and base_ok else None
                        if ckp_path is not None and ckp_path.is_file():
                            import marshal as _marshal
                            from sqlglot import exp as _sg_exp
                            expr = None
                            uniq_asts, before_idxs, stage_sigs, final_idx = _marshal.loads(ckp_path.read_bytes())
                            ast_in = rc.unpack_expr(uniq_asts[before_idxs[_tpc_ckp_min_k]], _sg_exp)
                            ast_mid = optimize(ast_in, schema_obj, rules=_tpc_ckp_rules[_tpc_ckp_min_k:_tpc_ckp_max_k + 1], leave_tables_isolated=True, copy=False)
                            if rc.ast_struct_sig(ast_mid, _sg_exp) == stage_sigs[_tpc_ckp_max_k]:
                                py_exec_q_c = py_exec_touched or shared_exec_all or qidx in shared_exec_qidxs
                                if not planner_touched and (not py_exec_q_c) and duck_untouched:
                                    done_w += q_weights[q_pos]
                                    q_done += 1
                                    continue
                                optimized = ast_mid if _tpc_ckp_max_k == len(_tpc_ckp_rules) - 1 else rc.unpack_expr(uniq_asts[final_idx], _sg_exp)
                            else:
                                optimized = ast_mid if _tpc_ckp_max_k == len(_tpc_ckp_rules) - 1 else optimize(ast_mid, schema_obj, rules=_tpc_ckp_rules[_tpc_ckp_max_k + 1:], leave_tables_isolated=True, copy=False)
                        elif pq_path is not None and pq_path.is_file():
                            import marshal as _marshal
                            from sqlglot import exp as _sg_exp
                            expr = None
                            pq_ast = rc.unpack_expr(_marshal.loads(pq_path.read_bytes()), _sg_exp)
                            optimized = optimize(pq_ast, schema_obj, rules=_pq_rules, leave_tables_isolated=True)
                        else:
                            expr = parse_one(sql)
                            if check_pickle:
                                expr = pickle.loads(pickle.dumps(expr))
                            optimized = optimize(expr, schema_obj, leave_tables_isolated=True)
                        opt_sql_hash = hashlib.blake2b(optimized.sql().encode(), digest_size=12).hexdigest()
                        if duck_untouched and base_ok and qm.get('duck_sql_hash'):
                            duck_sql = None
                            duck_unchanged = True
                        else:
                            duck_sql = transpile(sql, write='duckdb')[0]
                            duck_hash = hashlib.blake2b(duck_sql.encode(), digest_size=12).hexdigest()
                            duck_unchanged = base_ok and duck_hash == qm.get('duck_sql_hash')
                        py_exec_q = py_exec_touched or shared_exec_all or qidx in shared_exec_qidxs
                        opt_unchanged = base_ok and opt_sql_hash == qm.get('opt_sql_hash')
                        if not py_exec_q and (not planner_touched) and (not types_touched) and opt_unchanged and duck_unchanged:
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        if not py_exec_q and (not planner_touched) and opt_unchanged and duck_unchanged and qm.get('opt_types_hash') and (_hash_expr_types(optimized) == qm['opt_types_hash']):
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                        if executor_init_touched:
                            ran_heavy = True
                            if expr is None:
                                expr = parse_one(sql)
                            q_tables = {tbl.name: tables[tbl.name] for tbl in find_tables(expr)}
                            t_names = list(q_tables.keys())
                            table = sg_execute(expr, schema=schema_obj, tables=q_tables)
                        else:
                            plan = Plan(optimized)
                            det_h = _hash_plan_det(plan) if duck_unchanged and qm.get('det_plan_hash') else None
                            if not py_exec_q and det_h is not None and (det_h == qm['det_plan_hash']):
                                done_w += q_weights[q_pos]
                                q_done += 1
                                continue
                            if not executor_dir_touched and det_h is not None and (det_h == qm['det_plan_hash']) and qm.get('py_code_hash') and (_hash_plan_py_code(plan) == qm['py_code_hash']):
                                done_w += q_weights[q_pos]
                                q_done += 1
                                continue
                            ran_heavy = True
                            if expr is None:
                                expr = parse_one(sql)
                            q_tables = {}
                            t_names = []
                            for tbl in find_tables(expr):
                                name = tbl.name
                                q_tables[name] = tables[name]
                                t_names.append(name)
                            tables_ = ensure_tables(q_tables)
                            table = PythonExecutor(tables=tables_).execute(plan)
                    if duck_unchanged and qm.get('rows_hash') and (_hash_table_rows(table.columns, table.rows) == qm['rows_hash']):
                        done_w += q_weights[q_pos]
                        q_done += 1
                        continue
                    cached_duck = _load_tpc_duck_query(tpch, qidx) if duck_unchanged and (not helpers_patched or allow_duck_cache) else None
                    if cached_duck is not None:
                        a_cols, a_rows = cached_duck
                        verdict = _fast_compare_rows(a_cols, a_rows, list(table.columns), list(table.rows))
                        if verdict == 'fail':
                            self.fail(f'{tag} {qidx} result mismatch')
                        if verdict == 'pass':
                            done_w += q_weights[q_pos]
                            q_done += 1
                            continue
                    if duck_sql is None:
                        duck_sql = transpile(sql, write='duckdb')[0]
                    conn = _get_duck_conn(self, tpch, t_names)
                    raw_key = ('raw', sql)
                    if raw_key not in self.cache:
                        cur = conn.execute(duck_sql)
                        self.cache[raw_key] = ([d[0] for d in cur.description], cur.fetchall())
                    a_cols, a_rows = self.cache[raw_key]
                    verdict = _fast_compare_rows(a_cols, a_rows, list(table.columns), list(table.rows))
                    if verdict == 'fail':
                        self.fail(f'{tag} {qidx} result mismatch')
                    if verdict == 'fallback':
                        if np_mod is None:
                            import numpy as np_mod
                            import pandas as pd_mod
                            from pandas.testing import assert_frame_equal as afe_fn
                        b = pd_mod.DataFrame(((np_mod.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
                        if sql not in self.cache:
                            self.cache[sql] = conn.execute(duck_sql).fetchdf()
                        a = self.cache[sql]
                        afe_fn(a, b, check_dtype=False, check_index_type=False)
                done_w += q_weights[q_pos]
                q_done += 1
        finally:
            if outer_has_timer:
                rem = outer_deadline - time.monotonic()
                signal.setitimer(signal.ITIMER_REAL, max(0.05, rem))

    def _fast_optimized_tpch(self):
        from sqlglot import transpile
        cp = ACTIVE_COMMIT_PLAN
        assert cp is not None
        all_touched = set(cp.patched_files) | set(cp.deleted_files)
        tpch_touched = helpers_patched or cp.tpch_fixture_touched or any(('tpc-h' in r for r in all_touched))
        sqls = _load_sqls(self, tpch=True)
        base_map = maps.get('opt_tpch_baseline', {})
        allowed = ACTIVE_FIXTURE_FILTERS.get('opt-tpch', None)
        indices = list(range(1, len(sqls) + 1)) if allowed is None else sorted((i for i in allowed if 1 <= i <= len(sqls)))
        afe_fn = None
        ot_budget = ACTIVE_CPU_BUDGET_SEC
        ot_t0 = time.process_time()
        ot_warm = 0.0
        for q_pos, qidx in enumerate(indices):
            _, sql, optimized = sqls[qidx - 1]
            if ot_budget is not None and q_pos >= 1:
                ot_spent = time.process_time() - ot_t0
                if q_pos == 1:
                    ot_warm = ot_spent
                elif q_pos >= 3 and ot_spent >= 0.3 and ((ot_spent - ot_warm) / (q_pos - 1) * (len(indices) - q_pos) > ot_budget):
                    _econ_abort_now()
            with self.subTest(f'{qidx}, {sql}'):
                s1 = transpile(sql, write='duckdb')[0]
                s2 = transpile(optimized, write='duckdb')[0]
                h1 = hashlib.blake2b(s1.encode(), digest_size=12).hexdigest()
                h2 = hashlib.blake2b(s2.encode(), digest_size=12).hexdigest()
                if not tpch_touched and base_map.get(qidx) == (h1, h2):
                    continue
                conn = _get_duck_conn(self, tpch=True)
                raw_k = ('ot_raw', s1)
                if raw_k not in self.cache:
                    c_a = conn.execute(s1)
                    self.cache[raw_k] = ([d[0] for d in c_a.description], c_a.fetchall())
                a_cols, a_rows = self.cache[raw_k]
                c_b = conn.execute(s2)
                b_cols, b_rows = ([d[0] for d in c_b.description], c_b.fetchall())
                b_cols_ren = [a_cols[i] if '_col_' in c and i < len(a_cols) else c for i, c in enumerate(b_cols)]
                verdict = _fast_compare_rows(a_cols, a_rows, b_cols_ren, b_rows)
                if verdict == 'pass':
                    continue
                if verdict == 'fail':
                    self.fail(f'optimized_tpch {qidx} result mismatch')
                if afe_fn is None:
                    from pandas.testing import assert_frame_equal as afe_fn
                if sql not in self.cache:
                    self.cache[sql] = conn.execute(s1).fetchdf()
                a = self.cache[sql]
                b = conn.execute(s2).fetchdf()
                self.rename_anonymous(b, a)
                afe_fn(a, b)
    orig_texec_execute = texec.execute

    def _fast_texec_execute(sql, schema=None, dialect=None, tables=None):
        cp = ACTIVE_COMMIT_PLAN
        if cp is not None and IN_ASSERT_CTX == 0 and (not helpers_patched) and ('tests/test_executor.py' not in cp.patched_files) and (not cp.deleted_files) and (not cp.has_syntax_error) and cp.patched_files and all((r in ('sqlglot/generators/python.py', 'sqlglot/planner.py') or (r.startswith('sqlglot/executor/') and r not in ('sqlglot/executor/__init__.py', 'sqlglot/executor/table.py')) for r in cp.patched_files)):
            ek = rc.exec_call_key(sql, schema, tables, dialect)
            if ek is not None:
                ecache = rc.load_exec_replay()
                hit = ecache.get(ek)
                if hit is not None:
                    pk_opt, det_h_base, py_h_base, cols_base, rows_base = hit[:5]
                    pygen_toks = hit[5] if len(hit) >= 6 else None
                    from sqlglot import exp as _sg_exp
                    from sqlglot.executor.python import PythonExecutor
                    from sqlglot.executor.table import Table as _ETbl, ensure_tables
                    from sqlglot.planner import Plan
                    if pygen_toks is not None and set(cp.patched_files) == {'sqlglot/generators/python.py'} and (ACTIVE_REPLAY_SCOPE is not None) and (ACTIVE_REPLAY_SCOPE.fine_tokens is not None) and ACTIVE_REPLAY_SCOPE.fine_tokens.isdisjoint(pygen_toks):
                        return _ETbl(columns=cols_base, rows=list(rows_base))
                    exec_dir_t = any((r.startswith('sqlglot/executor/') for r in cp.patched_files))
                    planner_t = 'sqlglot/planner.py' in cp.patched_files
                    opt_ast = rc.unpack_expr(pk_opt, _sg_exp)
                    plan = Plan(opt_ast)
                    if not planner_t or _hash_plan_det(plan) == det_h_base:
                        if not exec_dir_t and _hash_plan_py_code(plan) == py_h_base:
                            return _ETbl(columns=cols_base, rows=list(rows_base))
                    tables_ = ensure_tables(tables, dialect=dialect)
                    return PythonExecutor(tables=tables_).execute(plan)
        return orig_texec_execute(sql, schema=schema, dialect=dialect, tables=tables)
    texec.execute = _fast_texec_execute
    texec.TestExecutor.setUpClass = _lazy_exec_setup_class
    texec.TestExecutor.tearDownClass = _lazy_exec_teardown_class
    texec.TestExecutor.test_execute_tpch = lambda self: _fast_mp_execute(self, True)
    texec.TestExecutor.test_execute_tpcds = lambda self: _fast_mp_execute(self, False)
    texec.TestExecutor.test_optimized_tpch = _fast_optimized_tpch
    texec._blast_patched = True

def _patch_dialect_validator(maps: dict[str, Any], sqlglot_root: Path) -> None:
    tdial = sys.modules.get('tests.dialects.test_dialect')
    if tdial is None or getattr(tdial, '_blast_val_patched', False):
        return
    val_ret_used: dict[str, Any] = maps.get('_val_ret_used_sets') or {k: frozenset(v) for k, v in maps.get('val_ret_used', {}).items()}
    root_prefix = str(sqlglot_root.resolve()) + '/'
    co_file_cache: dict[str, str] = {}

    def _caller_rel(co_filename: str) -> str:
        cached = co_file_cache.get(co_filename)
        if cached is not None:
            return cached
        if co_filename.startswith(root_prefix):
            rel = co_filename[len(root_prefix):]
        else:
            idx = co_filename.find('/tests/')
            rel = co_filename[idx + 1:] if idx >= 0 else co_filename
        co_file_cache[co_filename] = rel
        return rel
    if not getattr(unittest.TestCase, '_blast_ctx_patched', False):

        class _AssertCtxProxy:
            __slots__ = ('_cm',)

            def __init__(self, cm: Any) -> None:
                self._cm = cm

            def __enter__(self) -> Any:
                global IN_ASSERT_CTX
                IN_ASSERT_CTX += 1
                try:
                    return self._cm.__enter__()
                except BaseException:
                    IN_ASSERT_CTX -= 1
                    raise

            def __exit__(self, *ea: Any) -> Any:
                global IN_ASSERT_CTX
                try:
                    return self._cm.__exit__(*ea)
                finally:
                    IN_ASSERT_CTX -= 1

            def __getattr__(self, name: str) -> Any:
                return getattr(self._cm, name)
        for cname in ('assertLogs', 'assertNoLogs', 'assertRaises', 'assertRaisesRegex', 'assertWarns', 'assertWarnsRegex'):
            orig_cm = getattr(unittest.TestCase, cname, None)
            if orig_cm is None:
                continue
            has_callable = cname not in ('assertLogs', 'assertNoLogs')
            callable_min_args = 3 if 'Regex' in cname else 2

            def _make_cm(orig, hc, min_callable_args):

                def _wrapped(self, *args, **kwargs):
                    global IN_ASSERT_CTX
                    if hc and len(args) >= min_callable_args and (args[min_callable_args - 1] is not None):
                        IN_ASSERT_CTX += 1
                        try:
                            return orig(self, *args, **kwargs)
                        finally:
                            IN_ASSERT_CTX -= 1
                    cm = orig(self, *args, **kwargs)
                    if not hasattr(cm, '__enter__'):
                        return cm
                    return _AssertCtxProxy(cm)
                return _wrapped
            setattr(unittest.TestCase, cname, _make_cm(orig_cm, has_callable, callable_min_args))
        try:
            from unittest import mock as _umock
            _orig_p_enter = _umock._patch.__enter__
            _orig_p_exit = _umock._patch.__exit__

            def _wrapped_p_enter(self):
                global IN_ASSERT_CTX
                res = _orig_p_enter(self)
                IN_ASSERT_CTX += 1
                return res

            def _wrapped_p_exit(self, *exc_info):
                global IN_ASSERT_CTX
                try:
                    return _orig_p_exit(self, *exc_info)
                finally:
                    IN_ASSERT_CTX -= 1
            _umock._patch.__enter__ = _wrapped_p_enter
            _umock._patch.__exit__ = _wrapped_p_exit
        except Exception:
            pass
        unittest.TestCase._blast_ctx_patched = True
    orig_vi = tdial.Validator.validate_identity
    orig_vt = tdial.Validator.validate_transpile
    orig_va = tdial.Validator.validate_all
    orig_po = tdial.Validator.parse_one
    exp_mod = tdial.exp
    err_ignore = tdial.ErrorLevel.IGNORE
    err_raise = tdial.ErrorLevel.RAISE
    unsup_err = tdial.UnsupportedError
    sg_parse_one = tdial.parse_one

    def _is_ret_unused() -> bool:
        if ACTIVE_VAL_RET_USED_LINES is not None:
            if not ACTIVE_VAL_RET_USED_LINES:
                return True
        frame = sys._getframe(2)
        crel = _caller_rel(frame.f_code.co_filename)
        if crel.startswith('tests/dialects/'):
            used_lines = val_ret_used.get(crel)
            return not used_lines or frame.f_lineno not in used_lines
        return False

    def _wrap_vi(self, sql, write_sql=None, pretty=False, check_command_warning=False, identify=False):
        global ACTIVE_VAL_COUNTER, ACTIVE_VAL_DEPTH, VAL_FAILED_INSIDE
        if ECON_CPU_LIMIT is not None and ACTIVE_VAL_DEPTH == 0:
            _econ_check()
        if ACTIVE_VAL_DEPTH != 0:
            return orig_vi(self, sql, write_sql=write_sql, pretty=pretty, check_command_warning=check_command_warning, identify=identify)
        allowed = ACTIVE_VAL_CALLS
        if allowed is not None:
            ACTIVE_VAL_COUNTER += 1
            if ACTIVE_VAL_COUNTER not in allowed and IN_ASSERT_CTX == 0 and (not check_command_warning) and _is_ret_unused():
                return None
        scope = ACTIVE_REPLAY_SCOPE
        if scope is not None and (ACTIVE_COMMIT_PLAN is None or ACTIVE_VAL_FILE not in ACTIVE_COMMIT_PLAN.patched_files) and (IN_ASSERT_CTX == 0) and (not check_command_warning) and (type(self).parse_one is orig_po) and (type(sql) is str) and (write_sql is None or type(write_sql) is str) and _is_ret_unused():
            dk = rc.norm_dialect_key(self.dialect)
            if dk is not None:
                shard = rc.get_val_shard(ACTIVE_VAL_FILE, scope)
                if shard is not None:
                    expected = write_sql or sql
                    p_rec = shard.parses.get((dk, sql))
                    e_rec = shard.edges.get((dk, sql, dk, expected, bool(pretty), bool(identify), 0))
                    if p_rec is not None and e_rec is not None:
                        p_dep, p_sig, ast_idx, pid = p_rec
                        g_dep, eid = e_rec
                        dflags = shard.dirty_flags
                        p_dirty = dflags[p_dep] and (shard.dirty_pids is None or pid in shard.dirty_pids)
                        g_dirty = dflags[g_dep] and (shard.dirty_eids is None or eid in shard.dirty_eids)
                        if not p_dirty and (not g_dirty):
                            return None
                        ACTIVE_VAL_DEPTH += 1
                        try:
                            if not p_dirty and g_dirty:
                                expr = shard.get_expr(ast_idx, exp_mod) if ast_idx >= 0 else self.parse_one(sql)
                                self.assertEqual(expected, expr.sql(dialect=self.dialect, pretty=pretty, identify=identify))
                                return expr
                            if p_dirty and (not g_dirty):
                                expr = self.parse_one(sql)
                                if rc.ast_struct_sig(expr, exp_mod) == p_sig:
                                    return expr
                                self.assertEqual(expected, expr.sql(dialect=self.dialect, pretty=pretty, identify=identify))
                                return expr
                        except BaseException:
                            VAL_FAILED_INSIDE = True
                            raise
                        finally:
                            ACTIVE_VAL_DEPTH -= 1
        ACTIVE_VAL_DEPTH += 1
        try:
            return orig_vi(self, sql, write_sql=write_sql, pretty=pretty, check_command_warning=check_command_warning, identify=identify)
        except BaseException:
            VAL_FAILED_INSIDE = True
            raise
        finally:
            ACTIVE_VAL_DEPTH -= 1

    def _wrap_vt(self, sql, write_sql, write_dialect=None):
        global ACTIVE_VAL_COUNTER, ACTIVE_VAL_DEPTH, VAL_FAILED_INSIDE
        if ECON_CPU_LIMIT is not None and ACTIVE_VAL_DEPTH == 0:
            _econ_check()
        if ACTIVE_VAL_DEPTH != 0:
            return orig_vt(self, sql, write_sql, write_dialect=write_dialect)
        allowed = ACTIVE_VAL_CALLS
        if allowed is not None:
            ACTIVE_VAL_COUNTER += 1
            if ACTIVE_VAL_COUNTER not in allowed and IN_ASSERT_CTX == 0 and _is_ret_unused():
                return None
        scope = ACTIVE_REPLAY_SCOPE
        if scope is not None and (ACTIVE_COMMIT_PLAN is None or ACTIVE_VAL_FILE not in ACTIVE_COMMIT_PLAN.patched_files) and (IN_ASSERT_CTX == 0) and (type(self).parse_one is orig_po) and (type(sql) is str) and (type(write_sql) is str) and _is_ret_unused():
            rd = rc.norm_dialect_key(self.dialect)
            wd = rc.norm_dialect_key(write_dialect)
            if rd is not None and wd is not None:
                shard = rc.get_val_shard(ACTIVE_VAL_FILE, scope)
                if shard is not None:
                    p_rec = shard.parses.get((rd, sql))
                    e_rec = shard.edges.get((rd, sql, wd, write_sql, False, False, 0))
                    if p_rec is not None and e_rec is not None:
                        p_dep, p_sig, ast_idx, pid = p_rec
                        g_dep, eid = e_rec
                        dflags = shard.dirty_flags
                        p_dirty = dflags[p_dep] and (shard.dirty_pids is None or pid in shard.dirty_pids)
                        g_dirty = dflags[g_dep] and (shard.dirty_eids is None or eid in shard.dirty_eids)
                        if not p_dirty and (not g_dirty):
                            return None
                        ACTIVE_VAL_DEPTH += 1
                        try:
                            if not p_dirty and g_dirty:
                                expr = shard.get_expr(ast_idx, exp_mod) if ast_idx >= 0 else self.parse_one(sql)
                                self.assertEqual(write_sql, expr.sql(dialect=write_dialect))
                                return expr
                            if p_dirty and (not g_dirty):
                                expr = self.parse_one(sql)
                                if rc.ast_struct_sig(expr, exp_mod) == p_sig:
                                    return expr
                                self.assertEqual(write_sql, expr.sql(dialect=write_dialect))
                                return expr
                        except BaseException:
                            VAL_FAILED_INSIDE = True
                            raise
                        finally:
                            ACTIVE_VAL_DEPTH -= 1
        ACTIVE_VAL_DEPTH += 1
        try:
            return orig_vt(self, sql, write_sql, write_dialect=write_dialect)
        except BaseException:
            VAL_FAILED_INSIDE = True
            raise
        finally:
            ACTIVE_VAL_DEPTH -= 1

    def _wrap_va(self, sql, read=None, write=None, pretty=False, identify=False):
        global ACTIVE_VAL_COUNTER, ACTIVE_VAL_DEPTH, VAL_FAILED_INSIDE
        if ECON_CPU_LIMIT is not None and ACTIVE_VAL_DEPTH == 0:
            _econ_check()
        if ACTIVE_VAL_DEPTH != 0:
            return orig_va(self, sql, read=read, write=write, pretty=pretty, identify=identify)
        allowed = ACTIVE_VAL_CALLS
        if allowed is not None:
            ACTIVE_VAL_COUNTER += 1
            if ACTIVE_VAL_COUNTER not in allowed and IN_ASSERT_CTX == 0:
                return None
        scope = ACTIVE_REPLAY_SCOPE
        if scope is not None and (ACTIVE_COMMIT_PLAN is None or ACTIVE_VAL_FILE not in ACTIVE_COMMIT_PLAN.patched_files) and (IN_ASSERT_CTX == 0) and (type(self).parse_one is orig_po) and (type(sql) is str):
            sd = rc.norm_dialect_key(self.dialect)
            if sd is not None:
                shard = rc.get_val_shard(ACTIVE_VAL_FILE, scope)
                if shard is not None:
                    ACTIVE_VAL_DEPTH += 1
                    try:
                        dflags = shard.dirty_flags
                        dpids = shard.dirty_pids
                        deids = shard.dirty_eids
                        parses = shard.parses
                        edges = shard.edges
                        bp = bool(pretty)
                        bi = bool(identify)
                        mp = parses.get((sd, sql))
                        w_items = list(write.items()) if write else []
                        w_resolved: list[tuple[Any, Any, bool, Any]] | None = None
                        need_main_expr = False
                        main_dirty = mp is None or (dflags[mp[0]] and (dpids is None or mp[3] in dpids))
                        if main_dirty:
                            need_main_expr = True
                        elif w_items:
                            w_resolved = []
                            for wd_raw, wsql in w_items:
                                wd = rc.norm_dialect_key(wd_raw)
                                is_u = wsql is unsup_err
                                ek = None if wd is None or (not is_u and type(wsql) is not str) else (sd, sql, wd, None if is_u else wsql, bp, bi, 2 if is_u else 1)
                                e_rec = edges.get(ek) if ek is not None else None
                                w_resolved.append((wd_raw, wsql, is_u, e_rec))
                                if not need_main_expr and (e_rec is None or (dflags[e_rec[0]] and (deids is None or e_rec[1] in deids))):
                                    need_main_expr = True
                        if need_main_expr:
                            if not main_dirty and mp is not None and (mp[2] >= 0):
                                expr = shard.get_expr(mp[2], exp_mod)
                                main_same = True
                            else:
                                expr = self.parse_one(sql)
                                main_same = mp is not None and rc.ast_struct_sig(expr, exp_mod) == mp[1]
                            if w_resolved is None:
                                for wd_raw, wsql in w_items:
                                    wd = rc.norm_dialect_key(wd_raw)
                                    is_u = wsql is unsup_err
                                    ek = None if wd is None or (not is_u and type(wsql) is not str) else (sd, sql, wd, None if is_u else wsql, bp, bi, 2 if is_u else 1)
                                    e_rec = edges.get(ek) if ek is not None else None
                                    if main_same and e_rec is not None and (not (dflags[e_rec[0]] and (deids is None or e_rec[1] in deids))):
                                        continue
                                    with self.subTest(f'{sql} -> {wd_raw}'):
                                        if is_u:
                                            with self.assertRaises(unsup_err):
                                                expr.sql(wd_raw, unsupported_level=err_raise)
                                        else:
                                            self.assertEqual(expr.sql(wd_raw, unsupported_level=err_ignore, pretty=pretty, identify=identify), wsql)
                            else:
                                for wd_raw, wsql, is_u, e_rec in w_resolved:
                                    if main_same and e_rec is not None and (not (dflags[e_rec[0]] and (deids is None or e_rec[1] in deids))):
                                        continue
                                    with self.subTest(f'{sql} -> {wd_raw}'):
                                        if is_u:
                                            with self.assertRaises(unsup_err):
                                                expr.sql(wd_raw, unsupported_level=err_raise)
                                        else:
                                            self.assertEqual(expr.sql(wd_raw, unsupported_level=err_ignore, pretty=pretty, identify=identify), wsql)
                        if read:
                            for rd_raw, rsql in read.items():
                                rd = rc.norm_dialect_key(rd_raw)
                                if rd is not None and type(rsql) is str:
                                    rp = parses.get((rd, rsql))
                                    e_rec = edges.get((rd, rsql, sd, sql, bp, bi, 1))
                                    if rp is not None and e_rec is not None:
                                        rp_dirty = dflags[rp[0]] and (dpids is None or rp[3] in dpids)
                                        rg_dirty = dflags[e_rec[0]] and (deids is None or e_rec[1] in deids)
                                        if not rp_dirty and (not rg_dirty):
                                            continue
                                        if not rp_dirty and rg_dirty:
                                            with self.subTest(f'{rd_raw} -> {sql}'):
                                                rexpr = shard.get_expr(rp[2], exp_mod) if rp[2] >= 0 else sg_parse_one(rsql, rd_raw)
                                                self.assertEqual(rexpr.sql(self.dialect, unsupported_level=err_ignore, pretty=pretty, identify=identify), sql)
                                            continue
                                        if rp_dirty and (not rg_dirty):
                                            with self.subTest(f'{rd_raw} -> {sql}'):
                                                rexpr = sg_parse_one(rsql, rd_raw)
                                                if rc.ast_struct_sig(rexpr, exp_mod) != rp[1]:
                                                    self.assertEqual(rexpr.sql(self.dialect, unsupported_level=err_ignore, pretty=pretty, identify=identify), sql)
                                            continue
                                with self.subTest(f'{rd_raw} -> {sql}'):
                                    self.assertEqual(sg_parse_one(rsql, rd_raw).sql(self.dialect, unsupported_level=err_ignore, pretty=pretty, identify=identify), sql)
                        return None
                    except BaseException:
                        VAL_FAILED_INSIDE = True
                        raise
                    finally:
                        ACTIVE_VAL_DEPTH -= 1
        ACTIVE_VAL_DEPTH += 1
        try:
            return orig_va(self, sql, read=read, write=write, pretty=pretty, identify=identify)
        except BaseException:
            VAL_FAILED_INSIDE = True
            raise
        finally:
            ACTIVE_VAL_DEPTH -= 1
    tdial.Validator.validate_identity = _wrap_vi
    tdial.Validator.validate_transpile = _wrap_vt
    tdial.Validator.validate_all = _wrap_va
    tdial._blast_val_patched = True

def _patch_generator_module() -> None:
    tgen = sys.modules.get('tests.test_generator')
    if tgen is None or getattr(tgen, '_blast_patched', False):
        return
    import gc
    import tracemalloc
    from sqlglot.dialects import Dialect, Dialects

    def _filtered_gen_mem(self):
        allowed = ACTIVE_GEN_MEM_DIALECTS
        targets = [name for name in Dialects if allowed is None or name.value in allowed]
        if not targets:
            return
        dial_objs = [Dialect.get_or_raise(name) for name in targets]
        for dialect in dial_objs:
            for _ in range(10):
                dialect.generator()
        gc.collect()
        tracemalloc.start()
        try:
            if len(dial_objs) >= 4:
                b_all = tracemalloc.get_traced_memory()[0]
                for dialect in dial_objs:
                    for _ in range(10):
                        dialect.generator()
                gc.collect()
                a_all = tracemalloc.get_traced_memory()[0]
                if a_all - b_all <= 0:
                    return
            for name, dialect in zip(targets, dial_objs):
                with self.subTest(dialect=name):
                    before = tracemalloc.get_traced_memory()[0]
                    for _ in range(10):
                        dialect.generator()
                    gc.collect(0)
                    after = tracemalloc.get_traced_memory()[0]
                    if after - before > 0:
                        gc.collect()
                        after = tracemalloc.get_traced_memory()[0]
                        if after - before > 0:
                            before = tracemalloc.get_traced_memory()[0]
                            for _ in range(1000):
                                dialect.generator()
                            gc.collect()
                            after = tracemalloc.get_traced_memory()[0]
                    self.assertLess(after - before, 1024)
        finally:
            tracemalloc.stop()
    tgen.TestGenerator.test_generator_memory = _filtered_gen_mem
    tgen._blast_patched = True

def _patch_parser_max_nodes() -> None:
    tpar = sys.modules.get('tests.test_parser')
    if tpar is None or getattr(tpar, '_blast_patched', False):
        return
    orig_max_nodes = tpar.TestParser.test_max_nodes

    def _fast_max_nodes(self):
        from sqlglot import exp, parse_one
        from sqlglot.errors import ParseError
        self.assertIsInstance(parse_one('SELECT 1', max_nodes=1000000), exp.Select)
        cols = [f'col{i}' for i in range(500)]
        sql = f"SELECT {','.join(cols)} FROM t"
        self.assertIsInstance(parse_one(sql, max_nodes=1000000), exp.Select)
        expr = 'x' + '=x' * 100
        small_sql = 'SELECT ' + ','.join((expr for _ in range(60)))
        with self.assertRaises(ParseError) as ctx:
            parse_one(small_sql, max_nodes=5000)
        self.assertIn('Maximum number of AST nodes', str(ctx.exception))
        self.assertIn('(5000)', str(ctx.exception))
        with self.assertRaises(ParseError):
            parse_one('SELECT ' + ','.join((f'x={i}' for i in range(100))), max_nodes=5)
        self.assertIsInstance(parse_one('SELECT 1, 2, 3'), exp.Select)
        cp = ACTIVE_COMMIT_PLAN
        if cp is not None and cp.has_max_nodes_edit:
            orig_max_nodes(self)
    tpar.TestParser.test_max_nodes = _fast_max_nodes
    if hasattr(tpar.TestParser, 'test_text_match_excludes_delimited_tokens'):
        orig_tmed = tpar.TestParser.test_text_match_excludes_delimited_tokens
        _TMED_CASES = (('postgres', 'SELECT TRIM($$leading$$)', "SELECT TRIM('leading')"), ('tsql', "SELECT TRIM(N'both')", "SELECT TRIM(N'both')"), ('oracle', "SELECT TRIM(nq'[both]')", "SELECT TRIM(N'both')"), ('postgres', 'SELECT TRIM("leading" FROM x)', 'SELECT TRIM("leading" FROM x)'), ('mysql', 'INSERT `local` (a) VALUES (1)', 'INSERT INTO `local` (a) VALUES (1)'), ('mysql', 'INSERT `directory` (a) VALUES (1)', 'INSERT INTO `directory` (a) VALUES (1)'), ('mysql', 'SELECT `high_priority` FROM t', 'SELECT `high_priority` FROM t'), ('postgres', 'SELECT * INTO "unlogged" FROM t', 'SELECT * INTO "unlogged" FROM t'), ('oracle', 'SELECT XMLELEMENT("name", x) FROM t', 'SELECT XMLELEMENT(NAME "name", x) FROM t'), ('postgres', 'SELECT c::INT "unsigned" FROM t', 'SELECT CAST(c AS INT) AS "unsigned" FROM t'), ('oracle', 'SELECT INTERVAL \'1\' DAY "to" FROM t', 'SELECT INTERVAL \'1\' DAY AS "to" FROM t'), ('clickhouse', 'CREATE TABLE t (j JSON(`skip` String))', 'CREATE TABLE t (j JSON("skip" String))'), ('clickhouse', 'ALTER TABLE t DROP PARTITION "id"', 'ALTER TABLE t DROP PARTITION "id"'), ('databricks', 'DECLARE `var` INT', 'DECLARE `var` INT'), ('redshift', 'ALTER TABLE t ALTER "sortkey" TYPE INT', 'ALTER TABLE t ALTER COLUMN "sortkey" TYPE INTEGER'), ('duckdb', 'ALTER TABLE t ADD "columns" INT', 'ALTER TABLE t ADD COLUMN "columns" INT'), ('postgres', 'ALTER TABLE "only" ADD COLUMN x INT', 'ALTER TABLE "only" ADD COLUMN x INT'), ('duckdb', 'SELECT MAX("lambda") FROM t', 'SELECT MAX("lambda") FROM t'), ('duckdb', 'CREATE MACRO m() AS "return"', 'CREATE MACRO m() AS "return"'), ('mysql', 'ALTER TABLE t RENAME `index`', 'ALTER TABLE t RENAME `index`'), ('mysql', 'CREATE TABLE t (x TEXT, FULLTEXT `index` (x))', 'CREATE TABLE t (x TEXT, FULLTEXT INDEX `index` (x))'), ('mysql', 'SHOW CREATE TABLE `json`', 'SHOW CREATE TABLE `json`'), ('snowflake', 'WITH "identifier" (c) AS (SELECT 1) SELECT c FROM "identifier"', 'WITH "identifier"(c) AS (SELECT 1) SELECT c FROM "identifier"'), ('snowflake', 'SELECT * FROM t AS "identifier"(a, b)', 'SELECT * FROM t AS "identifier"(a, b)'), ('bigquery', 'SELECT ML.PREDICT(MODEL m, `table`)', 'SELECT ML.PREDICT(MODEL m, TABLE `table`)'), ('spark', 'ALTER TABLE t CHANGE COLUMN a `type` INT', 'ALTER TABLE t RENAME COLUMN a TO `type`'), ('exasol', 'SELECT JSON_EXTRACT(x, \'$.a\') "emits" FROM t', 'SELECT JSON_EXTRACT(x, \'$.a\') AS "emits" FROM t'), ('trino', 'SELECT U&\'abc\' "uescape"', 'SELECT U&\'abc\' AS "uescape"'), ('snowflake', 'SELECT * FROM t MATCH_RECOGNIZE(MEASURES "final" AS f PATTERN (a) DEFINE a AS TRUE)', 'SELECT * FROM t MATCH_RECOGNIZE ( MEASURES "final" AS f PATTERN (a) DEFINE a AS TRUE)'))

        def _fast_tmed(self):
            sc = ACTIVE_REPLAY_SCOPE
            cp = ACTIVE_COMMIT_PLAN
            if sc is None or (cp is not None and ('tests/test_parser.py' in cp.patched_files or 'tests/helpers.py' in cp.patched_files)):
                return orig_tmed(self)
            _stems = {Path(df).stem for df in sc.dirty_files if not df.startswith('sqlglot/typing/')}
            _dd_lo = {c.lower() for c in sc.dirty_dials}
            _dp_lo = {c.lower().removesuffix('parser') for c in sc.dirty_pars}
            _dg_lo = {c.lower().removesuffix('generator') for c in sc.dirty_gens}
            _dirty_set = _stems | _dd_lo | _dp_lo | _dg_lo
            for dialect, sql, expected in _TMED_CASES:
                if dialect not in _dirty_set:
                    continue
                with self.subTest(f'{dialect}: {sql}'):
                    self.assertEqual(tpar.parse_one(sql, dialect=dialect).sql(dialect), expected)
        tpar.TestParser.test_text_match_excludes_delimited_tokens = _fast_tmed
    tpar._blast_patched = True
_SAFE_INPROC_DICTS: frozenset[str] = (_di.TRACKED_GEN_DICTS | _di.TRACKED_PAR_DICTS) - {'COMMAND_PREFIX_TOKENS'}
_META_SKIP_ATTRS_INPROC: frozenset[str] = _di.META_SKIP_ATTRS | frozenset({'NORMALIZATION_STRATEGY', 'EXPRESSION_METADATA', 'COERCES_TO', 'INDEX_OFFSET', 'WEEK_OFFSET', 'DPIPE_IS_STRING_CONCAT', 'STRICT_STRING_CONCAT', 'SUPPORTS_SEMI_ANTI_JOIN', 'TYPED_DIVISION', 'SAFE_DIVISION', 'CONCAT_COALESCE', 'HEX_LOWERCASE', 'HEX_STRING_IS_INTEGER_TYPE', 'PROJECTION_ALIASES_SHADOW_SOURCE_NAMES', 'TABLESAMPLE_SIZE_IS_PERCENT', 'NULL_ORDERING'})
_HOT_META_HOOKS: frozenset[str] = frozenset({'__new__', '__init_subclass__', '__set_name__', '__class_getitem__', '_generate_next_value_', '_build_dispatch', 'sql_names'})
_HOT_EAGER_CORE_REJECT: frozenset[str] = frozenset({'__init__', '__new__', '__init_subclass__', '__set_name__', '__hash__', '__eq__', 'build', 'get', 'get_or_raise', 'create_trie'})
_UNSAFE_INPROC_TEST_IDS: frozenset[str] = frozenset({'tests/dialects/test_dialect.py::TestDialect::test_lazy_load', 'tests/dialects/test_dialect.py::TestDialect::test_patch_dialect_parser', 'tests/dialects/test_dialect.py::TestDialect::test_custom_dialect', 'tests/dialects/test_dialect.py::TestDialect::test_dialect_import_no_deadlock', 'tests/test_generator.py::TestGenerator::test_generator_memory', 'tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries', 'tests/test_serde.py::TestSerde::test_custom_expression'})
_UNSAFE_INPROC_TEST_FILES: frozenset[str] = frozenset({'tests/test_dialect_imports.py', 'tests/test_dialect_entry_points.py', 'tests/test_integration_loader.py', 'tests/test_sqlglotc.py'})

def _rollback_inproc_patches(records: list[tuple[Any, ...]]) -> None:
    _disp_cache = None
    for rec in reversed(records):
        kind = rec[0]
        if kind == 'code':
            _, fn_r, oc_r, cc_r = rec
            fn_r.__code__ = oc_r
            if cc_r is not None:
                cc_r()
        elif kind == 'dict':
            _, live_d, old_copy, gen_cls, old_disp, has_old_disp = rec
            live_d.clear()
            live_d.update(old_copy)
            if gen_cls is not None:
                if _disp_cache is None:
                    try:
                        from sqlglot.generator import _DISPATCH_CACHE as _dc
                        _disp_cache = _dc
                    except Exception:
                        _disp_cache = False
                if isinstance(_disp_cache, dict):
                    if has_old_disp:
                        _disp_cache[gen_cls] = old_disp
                    else:
                        _disp_cache.pop(gen_cls, None)
        elif kind == 'attr':
            _, cls_obj, attr_name, old_val = rec
            setattr(cls_obj, attr_name, old_val)

def _file_import_call_closure(tree: ast.Module) -> set[str]:
    imp_called: set[str] = set()
    fn_calls: dict[str, set[str]] = {}

    def _scan_non_rt(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for dec in getattr(node, 'decorator_list', ()):
                _scan_non_rt(dec)
            for d in getattr(node.args, 'defaults', ()):
                _scan_non_rt(d)
            for d in getattr(node.args, 'kw_defaults', ()):
                if d is not None:
                    _scan_non_rt(d)
            return
        if isinstance(node, ast.If):
            test = node.test
            is_tc = isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING' or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')
            if not is_tc:
                _scan_non_rt(test)
                for s in node.body:
                    _scan_non_rt(s)
            for s in node.orelse:
                _scan_non_rt(s)
            return
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                imp_called.add(f.id)
            elif isinstance(f, ast.Attribute):
                imp_called.add(f.attr)
        for ch in ast.iter_child_nodes(node):
            _scan_non_rt(ch)
    _scan_non_rt(tree)
    for st in tree.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            callees: set[str] = set()
            for sub in ast.walk(st):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    if isinstance(f, ast.Name):
                        callees.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        callees.add(f.attr)
            fn_calls[st.name] = callees
    closure: set[str] = set(imp_called & set(fn_calls))
    stk = list(closure)
    while stk:
        cur = stk.pop()
        for nxt in fn_calls.get(cur, ()):
            if nxt in fn_calls and nxt not in closure:
                closure.add(nxt)
                stk.append(nxt)
    return closure

def _apply_hot_func_patches(commit_plan: PatchPlan, sqlglot_root: Path, save_restore: list[tuple[Any, ...]] | None=None, engine: Any=None) -> bool:
    import types as _types
    applied: list[tuple[Any, ...]] = []

    def _rollback() -> bool:
        _rollback_inproc_patches(applied)
        return False
    from sqlglot.generator import Generator as _SGGenerator
    from sqlglot.parser import Parser as _SGParser
    try:
        from sqlglot.generator import _DISPATCH_CACHE as _SG_DISPATCH_CACHE
    except Exception:
        _SG_DISPATCH_CACHE = None
    for rel, ana in commit_plan.py_analyses.items():
        if not rel.startswith('sqlglot/') or rel.endswith('/__init__.py') or ana.syntax_error or (not ana.units):
            return _rollback()
        mod_name = rel.removesuffix('.py').replace('/', '.')
        try:
            if rel in ('sqlglot/dialects/duckdb.py', 'sqlglot/parsers/duckdb.py', 'sqlglot/generators/duckdb.py'):
                importlib.import_module('sqlglot.dialects.duckdb')
            mod = importlib.import_module(mod_name)
        except Exception:
            return _rollback()
        new_src = commit_plan.patched_files[rel]
        try:
            new_tree = ast.parse(new_src, filename=str(sqlglot_root / rel))
        except SyntaxError:
            return _rollback()
        has_future_ann = any((isinstance(s, ast.ImportFrom) and s.module == '__future__' and any((a.name == 'annotations' for a in s.names)) for s in new_tree.body[:5]))
        is_eager_core = rel in ('sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/dialects/dialect.py', 'sqlglot/parser.py') or rel.startswith('sqlglot/expressions/')
        imp_call_cl: set[str] | None = None

        def _find_cls_body(cls_parts: list[str]) -> list[ast.stmt] | None:
            cur_body = new_tree.body
            for p in cls_parts:
                found = None
                for s in cur_body:
                    if isinstance(s, ast.ClassDef) and s.name == p:
                        found = s
                        break
                if found is None:
                    return None
                cur_body = found.body
            return cur_body

        def _find_node(qualname: str) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | None, list[str]]:
            parts = qualname.split('.')
            cls_chain = parts[:-1]
            cur_body = _find_cls_body(cls_chain) if cls_chain else new_tree.body
            if cur_body is None:
                return (None, [])
            for s in cur_body:
                if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == parts[-1]:
                    return (s, cls_chain)
            return (None, [])

        def _find_class_assign_val(cls_parts: list[str], attr_name: str) -> ast.expr | None:
            cur_body = _find_cls_body(cls_parts)
            if cur_body is None:
                return None
            found_val: ast.expr | None = None
            for s in cur_body:
                if isinstance(s, ast.Assign) and len(s.targets) == 1:
                    t0 = s.targets[0]
                    if isinstance(t0, ast.Name) and t0.id == attr_name:
                        found_val = s.value
                elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name) and (s.target.id == attr_name):
                    if s.value is not None:
                        found_val = s.value
            return found_val
        for unit in ana.units:
            if not unit.symbol_name:
                return _rollback()
            if unit.kind == 'func_body':
                if unit.sig_changed:
                    return _rollback()
                qname = unit.symbol_name
                fn_node, cls_chain = _find_node(qname)
                if fn_node is None:
                    return _rollback()
                if fn_node.name in _HOT_META_HOOKS:
                    return _rollback()
                if is_eager_core and fn_node.name in _HOT_EAGER_CORE_REJECT:
                    return _rollback()
                if not cls_chain:
                    if engine is not None and engine._is_func_called_at_import(rel, fn_node.name)[0]:
                        return _rollback()
                    if imp_call_cl is None:
                        imp_call_cl = _file_import_call_closure(new_tree)
                    if fn_node.name in imp_call_cl:
                        return _rollback()
                allowed_decs = {'property', 'classmethod', 'staticmethod', 'lru_cache', 'cache'}
                for dec in fn_node.decorator_list:
                    d_name = None
                    if isinstance(dec, ast.Name):
                        d_name = dec.id
                    elif isinstance(dec, ast.Attribute):
                        d_name = dec.attr
                    elif isinstance(dec, ast.Call):
                        if isinstance(dec.func, ast.Name):
                            d_name = dec.func.id
                        elif isinstance(dec.func, ast.Attribute):
                            d_name = dec.func.attr
                    if d_name not in allowed_decs:
                        return _rollback()
                owner: Any = mod
                for cname in cls_chain:
                    owner = getattr(owner, cname, None)
                    if owner is None:
                        return _rollback()
                meth_name = qname.rsplit('.', 1)[-1]
                raw_obj = owner.__dict__.get(meth_name)
                if raw_obj is None:
                    return _rollback()
                cache_clear_fn = None
                if isinstance(raw_obj, property):
                    live_fn = raw_obj.fget
                elif isinstance(raw_obj, (classmethod, staticmethod)):
                    live_fn = raw_obj.__func__
                elif hasattr(raw_obj, '__wrapped__') and hasattr(raw_obj, 'cache_clear'):
                    live_fn = raw_obj.__wrapped__
                    cache_clear_fn = raw_obj.cache_clear
                elif hasattr(raw_obj, '__code__'):
                    live_fn = raw_obj
                else:
                    return _rollback()
                if live_fn is None or not hasattr(live_fn, '__code__'):
                    return _rollback()
                old_code = live_fn.__code__
                if any((v != '__class__' for v in old_code.co_freevars)):
                    return _rollback()
                stmts: list[ast.stmt] = []
                if has_future_ann:
                    stmts.append(ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0))
                fn_clone = copy.copy(fn_node)
                fn_clone.decorator_list = []
                try:
                    if cls_chain:
                        cls_stub = ast.ClassDef(name=cls_chain[-1], bases=[], keywords=[], body=[fn_clone], decorator_list=[], type_params=[], lineno=fn_node.lineno, col_offset=0, end_lineno=fn_node.end_lineno, end_col_offset=fn_node.end_col_offset)
                        stmts.append(cls_stub)
                        mod_ast = ast.Module(body=stmts, type_ignores=[])
                        ast.fix_missing_locations(mod_ast)
                        ns = dict(mod.__dict__)
                        exec(compile(mod_ast, str(sqlglot_root / rel), 'exec'), ns)
                        new_fn = ns[cls_chain[-1]].__dict__[fn_node.name]
                    else:
                        stmts.append(fn_clone)
                        mod_ast = ast.Module(body=stmts, type_ignores=[])
                        ast.fix_missing_locations(mod_ast)
                        ns = dict(mod.__dict__)
                        exec(compile(mod_ast, str(sqlglot_root / rel), 'exec'), ns)
                        new_fn = ns[fn_node.name]
                except Exception:
                    return _rollback()
                new_code = new_fn.__code__
                if new_code.co_freevars != old_code.co_freevars or new_code.co_cellvars != old_code.co_cellvars or new_fn.__defaults__ != live_fn.__defaults__ or (new_fn.__kwdefaults__ != live_fn.__kwdefaults__):
                    return _rollback()
                live_fn.__code__ = new_code
                applied.append(('code', live_fn, old_code, cache_clear_fn))
                if cache_clear_fn is not None:
                    cache_clear_fn()
            elif unit.kind == 'dict_lambda_body':
                if '.' not in unit.symbol_name or not unit.dict_keys:
                    return _rollback()
                cls_qname, attr_name = unit.symbol_name.rsplit('.', 1)
                if attr_name in _META_SKIP_ATTRS_INPROC or cls_qname == 'Tokenizer' or cls_qname.endswith('.Tokenizer'):
                    return _rollback()
                cls_parts = cls_qname.split('.')
                owner = mod
                for cname in cls_parts:
                    owner = getattr(owner, cname, None)
                    if owner is None:
                        return _rollback()
                if not isinstance(owner, type):
                    return _rollback()
                live_dict = owner.__dict__.get(attr_name)
                if type(live_dict) is not dict:
                    return _rollback()
                rhs = _find_class_assign_val(cls_parts, attr_name)
                if not isinstance(rhs, ast.Dict):
                    return _rollback()
                eval_ns = dict(mod.__dict__)
                for _k, _v in owner.__dict__.items():
                    if not _k.startswith('__'):
                        eval_ns.setdefault(_k, _v)
                kv_by_repr: dict[str, tuple[ast.expr, ast.expr]] = {}
                for ka, va in zip(rhs.keys, rhs.values):
                    if ka is not None:
                        kv_by_repr[ast.unparse(ka)] = (ka, va)
                for k_repr in unit.dict_keys:
                    pair = kv_by_repr.get(k_repr)
                    if pair is None:
                        return _rollback()
                    ka, va = pair
                    if not isinstance(va, ast.Lambda):
                        return _rollback()
                    try:
                        k_expr = ast.Expression(body=copy.copy(ka))
                        ast.fix_missing_locations(k_expr)
                        rt_key = eval(compile(k_expr, str(sqlglot_root / rel), 'eval'), eval_ns)
                        v_expr = ast.Expression(body=copy.copy(va))
                        ast.fix_missing_locations(v_expr)
                        new_lam = eval(compile(v_expr, str(sqlglot_root / rel), 'eval'), eval_ns)
                    except Exception:
                        return _rollback()
                    if rt_key not in live_dict:
                        return _rollback()
                    live_lam = live_dict[rt_key]
                    if not isinstance(live_lam, _types.FunctionType) or not isinstance(new_lam, _types.FunctionType):
                        return _rollback()
                    old_code = live_lam.__code__
                    new_code = new_lam.__code__
                    if old_code.co_freevars != () or new_code.co_freevars != () or new_code.co_cellvars != old_code.co_cellvars or (new_lam.__defaults__ != live_lam.__defaults__) or (new_lam.__kwdefaults__ != live_lam.__kwdefaults__):
                        return _rollback()
                    live_lam.__code__ = new_code
                    applied.append(('code', live_lam, old_code, None))
            elif unit.kind in ('dict_key', 'class_attr'):
                if engine is None or '.' not in unit.symbol_name or (not rel.startswith(('sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/dialects/'))) or (rel in ('sqlglot/dialects/dialect.py', 'sqlglot/parsers/base.py')):
                    return _rollback()
                cls_qname, attr_name = unit.symbol_name.rsplit('.', 1)
                if attr_name.startswith('_') or attr_name in _META_SKIP_ATTRS_INPROC or cls_qname == 'Tokenizer' or cls_qname.endswith('.Tokenizer'):
                    return _rollback()
                cls_parts = cls_qname.split('.')
                owner = mod
                for cname in cls_parts:
                    owner = getattr(owner, cname, None)
                    if owner is None:
                        return _rollback()
                if not isinstance(owner, type) or not issubclass(owner, (_SGParser, _SGGenerator)):
                    return _rollback()
                if owner.__subclasses__() or len(engine._expand_class_and_copiers(cls_qname, attr_name)) > 1:
                    return _rollback()
                if attr_name not in owner.__dict__:
                    return _rollback()
                rhs = _find_class_assign_val(cls_parts, attr_name)
                if rhs is None:
                    return _rollback()
                eval_ns = dict(mod.__dict__)
                for _k, _v in owner.__dict__.items():
                    if not _k.startswith('__'):
                        eval_ns.setdefault(_k, _v)
                try:
                    v_expr = ast.Expression(body=copy.copy(rhs))
                    ast.fix_missing_locations(v_expr)
                    new_val = eval(compile(v_expr, str(sqlglot_root / rel), 'eval'), eval_ns)
                except Exception:
                    return _rollback()
                if unit.kind == 'dict_key':
                    if attr_name not in _SAFE_INPROC_DICTS:
                        return _rollback()
                    live_dict = owner.__dict__[attr_name]
                    if type(live_dict) is not dict or type(new_val) is not dict:
                        return _rollback()
                    old_copy = dict(live_dict)
                    is_gen_tr = issubclass(owner, _SGGenerator) and attr_name == 'TRANSFORMS'
                    has_old_d = False
                    old_d = None
                    if is_gen_tr:
                        if not isinstance(_SG_DISPATCH_CACHE, dict):
                            return _rollback()
                        has_old_d = owner in _SG_DISPATCH_CACHE
                        old_d = _SG_DISPATCH_CACHE.pop(owner, None)
                    live_dict.clear()
                    live_dict.update(new_val)
                    applied.append(('dict', live_dict, old_copy, owner if is_gen_tr else None, old_d, has_old_d))
                else:
                    if not isinstance(new_val, (bool, int, float, str, bytes, type(None), tuple, frozenset, set, list)):
                        return _rollback()
                    old_val = owner.__dict__[attr_name]
                    setattr(owner, attr_name, new_val)
                    applied.append(('attr', owner, attr_name, old_val))
            else:
                return _rollback()
    if save_restore is not None:
        save_restore.extend(applied)
    return True

def _est_verify_ms(tnum: int, num_to_test: dict[int, dict], dur_by_num: dict[int, int], commit_plan: PatchPlan, maps: dict[str, Any], is_c_root: bool=False, engine: Any=None) -> int:
    tid_str = num_to_test[tnum]['id']
    recorded = dur_by_num[tnum]
    sub_f = commit_plan.subtest_filters.get(tnum)
    has_deferred = tnum in commit_plan.deferred_subtests
    patched = commit_plan.patched_files
    if tid_str == 'tests/test_parser.py::TestParser::test_max_nodes':
        if commit_plan.has_max_nodes_edit or 'tests/test_parser.py' in patched:
            return 10000 if is_c_root else 30000
        return 60 if is_c_root else 140
    if tid_str.endswith('::test_optimized_tpch'):
        if 'tests/helpers.py' in patched or 'tests/test_executor.py' in patched:
            return int(recorded * (1.6 if is_c_root else 2.5))
        return (70 if is_c_root else 120) if not commit_plan.tpch_fixture_touched else 800
    if tid_str.endswith('::test_execute_tpcds'):
        all_t = set(patched) | set(commit_plan.deleted_files)
        exec_dir_t = any((r.startswith('sqlglot/executor/') or r in ('tests/test_executor.py', 'tests/helpers.py') for r in all_t))
        pygen_t = 'sqlglot/generators/python.py' in all_t
        planner_t = any((r in ('sqlglot/planner.py', 'sqlglot/optimizer/eliminate_joins.py', 'sqlglot/optimizer/scope.py') for r in all_t))
        c_mul = 0.55 if is_c_root else 1.0
        if not exec_dir_t and (not commit_plan.tpcds_fixture_touched):
            qset = (sub_f or {}).get('tpc-ds')
            n = len(qset) if qset is not None else 15 if has_deferred else 60
            if pygen_t and all((r in ('sqlglot/planner.py', 'sqlglot/generators/python.py') for r in all_t)):
                return max(100, int((120 + n * 16) * c_mul))
            if planner_t and all((r in ('sqlglot/planner.py', 'sqlglot/generators/python.py') or r.startswith('sqlglot/executor/') for r in all_t)):
                return max(100, int((150 + n * 25) * c_mul))
            per_q = 115 if planner_t else 35 if all((r.startswith(('sqlglot/optimizer/', 'sqlglot/typing/')) and r not in ('sqlglot/optimizer/qualify.py', 'sqlglot/optimizer/qualify_tables.py', 'sqlglot/optimizer/qualify_columns.py', 'sqlglot/optimizer/isolate_table_selects.py', 'sqlglot/optimizer/pushdown_projections.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/resolver.py', 'sqlglot/optimizer/optimizer.py', 'sqlglot/optimizer/__init__.py') for r in all_t)) else 75
            return max(100, int((400 + n * per_q) * c_mul))
        qset = (sub_f or {}).get('tpc-ds')
        if qset is not None:
            qmeta = maps['tpcds_meta']
            return int(sum(((qmeta.get(q, {}).get('sec_opt', 0.02) + qmeta.get(q, {}).get('sec_exec', 0.2)) * 1000 for q in qset)) * (0.75 if is_c_root else 1.0))
        return 6500 if is_c_root else 9000
    if tid_str.endswith('::test_execute_tpch'):
        return 30 if not commit_plan.tpch_fixture_touched else 1800
    if tid_str.endswith('::test_generator_memory'):
        if 'tests/test_generator.py' in patched:
            return 6700
        dset = (sub_f or {}).get('gen-mem')
        n = len(dset) if dset is not None else 3 if has_deferred else 34
        return max(12, 10 + n * 2)
    if tid_str.endswith('::test_scope'):
        fast_scope = 'tests/helpers.py' not in patched and 'tests/test_optimizer.py' not in patched and (not any((rel in ('sqlglot/optimizer/scope.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/dialects/dialect.py', 'sqlglot/helper.py', 'sqlglot/__init__.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py') or rel.startswith('sqlglot/expressions/') for rel in patched)))
        return 120 if fast_scope else 1800
    if tid_str.endswith(('::test_optimize_in_nulls', '::test_optimize_correlated_all')):
        if ACTIVE_REPLAY_SCOPE is not None and 'tests/helpers.py' not in patched and ('tests/test_optimizer.py' not in patched):
            return max(15, int(recorded * (0.15 if ACTIVE_REPLAY_SCOPE.fine_tokens is not None else 0.3)))
        return recorded
    if tid_str.endswith('::test_agg_order'):
        return 5
    if tid_str.endswith('::test_sql_boolean_logic_with_numpy_scalars'):
        return 150 if 'numpy' not in sys.modules else 8
    if sub_f:
        total_cases = 0
        for v in sub_f.values():
            if v is None:
                base_e = max(5, int(recorded * 0.7))
                break
            total_cases += len(v)
        else:
            if total_cases == 0:
                return 2
            if engine is not None:
                sgs = engine._loaded_subtests.get(tnum)
                if sgs:
                    n_all = sum((len(sg.get('cases', ())) for sg in sgs))
                    if n_all > 0:
                        frac = min(1.0, total_cases / n_all)
                        mul = 0.75 if is_c_root else 1.0
                        base_e = max(3, int(recorded * frac * mul) + 4)
                    else:
                        base_e = max(3, min(recorded, int(total_cases * 0.8) + 4))
                else:
                    base_e = max(3, min(recorded, int(total_cases * 0.8) + 4))
            else:
                base_e = max(3, min(recorded, int(total_cases * 0.8) + 4))
    elif has_deferred:
        base_e = max(5, min(recorded, 60))
    else:
        base_e = max(2, int(recorded * 0.7))
    rscope = ACTIVE_REPLAY_SCOPE
    if rscope is not None:
        tf = num_to_test[tnum]['file']
        replay_base = recorded if has_deferred and (not sub_f) else base_e
        if tf.startswith('tests/dialects/'):
            stem = Path(tf).stem.removeprefix('test_')
            is_home = any((Path(df).stem == stem for df in rscope.dirty_files))
            home_mul = 0.25 if rscope.fine_tokens is not None else 0.38
            return max(2, int(replay_base * (home_mul if is_home else 0.08)))
        if tf == 'tests/test_optimizer.py':
            return max(3, int(replay_base * (0.2 if rscope.fine_tokens is not None else 0.25)))
    return base_e

def _execute_commit_verification(commit_plan: PatchPlan, maps: dict[str, Any], num_to_test: dict[int, dict], dur_by_num: dict[int, int], sqlglot_root: Path, setup_classes_done: dict[str, Any], broken_import_files: set[str], engine: Any=None, ov_dir: Path | None=None) -> list[int]:
    global ACTIVE_GEN_MEM_DIALECTS, ACTIVE_COMMIT_PLAN, ACTIVE_ENGINE, LAST_FAILED_TPCDS_QIDX, ACTIVE_REPLAY_SCOPE, LAST_COMMIT_HAD_TIMEOUT
    ACTIVE_COMMIT_PLAN = commit_plan
    ACTIVE_ENGINE = engine
    LAST_FAILED_TPCDS_QIDX = 0
    ACTIVE_REPLAY_SCOPE = None
    LAST_COMMIT_HAD_TIMEOUT = False
    rc.clear_replay_shard_caches()
    is_c_root = sqlglot_root == _SQLGLOT_C_ROOT
    is_noopt_root = sqlglot_root == _SQLGLOT_C_NOOPT_ROOT
    is_cb_root = sqlglot_root == _SQLGLOT_CB_ROOT
    _install_fast_test_patches(maps, commit_plan, sqlglot_root)
    confirmed_fail: set[int] = set(commit_plan.force_report)
    remaining: set[int] = set(commit_plan.candidates) - confirmed_fail
    class_cache: dict[tuple[str, str], type | None] = {}
    failed_test_files: set[str] = set()
    passed_tests: set[int] = set()
    file_to_all_tnums: dict[str, list[int]] = maps.get('_file_to_all_tnums') or {}
    if not file_to_all_tnums:
        for _n, _ti in num_to_test.items():
            file_to_all_tnums.setdefault(_ti['file'], []).append(_n)
    import_escalated = False
    duckdb_import_probed = False
    timeout_count = 0
    last_timed_out = False
    last_econ_aborted = False

    def _widen_broken_before_plan(rel_path: str) -> None:
        if engine is None:
            return
        _broken = engine._tests_for_broken_import(rel_path)
        for _bt in _broken:
            commit_plan.deferred_subtests.pop(_bt, None)
            if _bt in commit_plan.subtest_filters:
                for _k in list(commit_plan.subtest_filters[_bt].keys()):
                    commit_plan.subtest_filters[_bt][_k] = None
            if _bt not in confirmed_fail:
                remaining.add(_bt)

    def _probe_lazy_import_crashes() -> None:
        nonlocal duckdb_import_probed
        if duckdb_import_probed or engine is None:
            return
        duckdb_import_probed = True
        touches_core = any((rel.startswith('sqlglot/expressions/') or rel in ('sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/generator.py', 'sqlglot/dialects/dialect.py', 'sqlglot/helper.py', 'sqlglot/__init__.py') for rel in list(commit_plan.patched_files) + list(commit_plan.deleted_files)))
        if not touches_core:
            return
        try:
            if 'sqlglot.generators.duckdb' in sys.modules:
                importlib.reload(sys.modules['sqlglot.generators.duckdb'])
            else:
                importlib.import_module('sqlglot.dialects.duckdb')
        except BaseException:
            extra_d = engine._tests_for_broken_import('sqlglot/dialects/duckdb.py')
            for sc in engine._subclasses_of('DuckDB'):
                extra_d.update(engine._to_rnums(maps['dialect_to_tests'].get(sc, [])))
            extra_d.update(engine._to_rnums(maps['dialect_to_tests'].get('DuckDB', [])))
            for x in extra_d:
                if x not in confirmed_fail and x not in remaining and (x not in passed_tests):
                    if zero_ms_absorbed and dur_by_num[x] == 0:
                        _add_confirmed(x)
                    else:
                        _add_remaining(x)
    _import_probe_failed = False
    if commit_plan.dict_factory_files and engine is not None:
        for _df_rel in sorted(commit_plan.dict_factory_files):
            if not _df_rel.endswith('.py'):
                continue
            _df_mod = _df_rel.removesuffix('/__init__.py').removesuffix('.py').replace('/', '.')
            try:
                if _df_mod in sys.modules:
                    importlib.reload(sys.modules[_df_mod])
                else:
                    importlib.import_module(_df_mod)
            except BaseException:
                _import_probe_failed = True
                _widen_broken_before_plan(_df_rel)
    _touches_eager_td_pre = bool((set(commit_plan.patched_files) | set(commit_plan.deleted_files)) & _EAGER_TD_FILES)
    if not _import_probe_failed:
        _cand_scope = getattr(commit_plan, '_precomputed_replay_scope', rc._UNPACKABLE)
        if _cand_scope is rc._UNPACKABLE:
            _cand_scope = rc.classify_replay_scope(commit_plan, maps, engine)
        if _cand_scope is not None:
            _all_clean_imp = True
            _has_deadlock_test = any((num_to_test[_t]['id'].endswith('::test_dialect_import_no_deadlock') for _t in remaining))
            _probe_files = sorted(set(commit_plan.patched_files) | set(_cand_scope.dirty_files))
            for _pr in _probe_files:
                if not (_pr.startswith('sqlglot/') and _pr.endswith('.py')):
                    continue
                if _has_deadlock_test and _pr == 'sqlglot/dialects/athena.py':
                    _all_clean_imp = False
                    continue
                _pm = _pr.removesuffix('/__init__.py').removesuffix('.py').replace('/', '.')
                try:
                    importlib.import_module(_pm)
                except BaseException:
                    _all_clean_imp = False
                    _widen_broken_before_plan(_pr)
            if _all_clean_imp and _touches_eager_td_pre:
                try:
                    _resolve_deferred_test_dialect(set(commit_plan.patched_files) | set(commit_plan.deleted_files))
                except BaseException:
                    _all_clean_imp = False
                    failed_test_files.add('tests/dialects/test_dialect.py')
                    _widen_broken_before_plan('tests/dialects/test_dialect.py')
            if _all_clean_imp:
                ACTIVE_REPLAY_SCOPE = _cand_scope
    _helpers_p = 'tests/helpers.py' in commit_plan.patched_files
    _any_opt_fixture_patched = any((r.startswith('tests/fixtures/') for r in set(commit_plan.patched_files) | set(commit_plan.deleted_files)))
    if ACTIVE_REPLAY_SCOPE is not None and (not _helpers_p):
        _dj_clean: list[int] = []
        _td_patched = 'tests/dialects/test_dialect.py' in commit_plan.patched_files
        _osh_pre = None
        _osh_loaded = False
        for _t in remaining:
            _ti = num_to_test[_t]
            _tf = _ti['file']
            if _tf in commit_plan.patched_files:
                continue
            if _tf.startswith('tests/dialects/') and (not _td_patched):
                _vsh = rc.get_val_shard(_tf, ACTIVE_REPLAY_SCOPE)
                if _vsh is not None:
                    _, _cn, _mn = _ti['id'].split('::')
                    if _vsh.is_pure_method_clean(_cn, _mn):
                        _dj_clean.append(_t)
            elif _tf == 'tests/test_optimizer.py' and (not _any_opt_fixture_patched):
                if not _osh_loaded:
                    _osh_pre = rc.get_opt_methods_shard(ACTIVE_REPLAY_SCOPE)
                    _osh_loaded = True
                if _osh_pre is not None:
                    _mn = _ti['id'].rsplit('::', 1)[-1]
                    if _osh_pre.is_method_clean(_mn):
                        _dj_clean.append(_t)
        for _t in _dj_clean:
            remaining.discard(_t)
            passed_tests.add(_t)
    _SLOW_IF_HELPERS = ('::test_execute_tpcds', '::test_execute_tpch', '::test_optimized_tpch')
    resolved_filters: dict[int, Any] = {}
    if engine is not None and (not _helpers_p):
        for t in remaining:
            if t in commit_plan.deferred_subtests and num_to_test[t]['id'].endswith(('::test_execute_tpcds', '::test_execute_tpch', '::test_tpcds')) and (num_to_test[t]['file'] not in commit_plan.patched_files):
                resolved_filters[t] = engine.resolve_subtest_filter(commit_plan, t)

    def _est(t: int) -> int:
        ti = num_to_test[t]
        _fast_c = is_c_root or is_noopt_root
        if _helpers_p or ti['file'] in commit_plan.patched_files:
            tid = ti['id']
            if tid == 'tests/test_parser.py::TestParser::test_max_nodes' and ti['file'] not in commit_plan.patched_files:
                return _est_verify_ms(t, num_to_test, dur_by_num, commit_plan, maps, is_c_root=_fast_c, engine=engine)
            if tid.endswith(_SLOW_IF_HELPERS):
                return max(2, dur_by_num[t])
            return max(2, int(dur_by_num[t] * 0.7))
        if t in commit_plan.deferred_subtests and (not ti['id'].endswith(_SLOW_IF_HELPERS + ('::test_generator_memory', '::test_tpcds'))):
            if ACTIVE_REPLAY_SCOPE is not None and (ti['file'].startswith('tests/dialects/') or ti['file'] == 'tests/test_optimizer.py'):
                return _est_verify_ms(t, num_to_test, dur_by_num, commit_plan, maps, is_c_root=_fast_c, engine=engine)
            return max(4, min(60, int(dur_by_num[t] * 0.3)))
        return _est_verify_ms(t, num_to_test, dur_by_num, commit_plan, maps, is_c_root=_fast_c, engine=engine)
    est_cpu = {t: _est(t) for t in remaining}
    imported_tfiles: set[str] = {tf for tf in file_to_all_tnums if tf.removesuffix('.py').replace('/', '.') in sys.modules}
    helpers_patched_global = 'tests/helpers.py' in commit_plan.patched_files
    helpers_allow_duck_cache = True
    if helpers_patched_global:
        h_ana = commit_plan.py_analyses.get('tests/helpers.py')
        if h_ana is None or h_ana.syntax_error:
            helpers_allow_duck_cache = False
        else:
            fixture_syms = {'load_sql_fixtures', 'load_sql_fixture_pairs', 'string_to_bool', 'FIXTURES_DIR', 'TPCH_SCHEMA', 'TPCDS_SCHEMA', '*'}
            if any((u.kind == 'module_other' or (u.symbol_name or '*') in fixture_syms for u in h_ana.units)):
                helpers_allow_duck_cache = False
    _touches_eager_td = bool((set(commit_plan.patched_files) | set(commit_plan.deleted_files)) & _EAGER_TD_FILES)
    _has_func_body_unit = any((u.kind == 'func_body' for ana in commit_plan.py_analyses.values() for u in ana.units))
    _OPT_REPLAY_METHS = frozenset({'test_annotate_funcs', 'test_annotate_types', 'test_optimize_in_nulls', 'test_optimize_correlated_all', 'test_qualify_columns', 'test_optimize', 'test_qualify', 'test_qualify_tables', 'test_pushdown_predicates', 'test_unnest_subqueries', 'test_pushdown_projections', 'test_simplify', 'test_normalize', 'test_canonicalize', 'test_eliminate_subqueries', 'test_merge_subqueries', 'test_eliminate_joins', 'test_eliminate_ctes'})

    def _should_skip_subtest_res(tnum: int, tf: str, meth: str, rscope: Any) -> bool:
        if rscope is None or helpers_patched_global or tf in commit_plan.patched_files:
            return False
        if _has_func_body_unit and dur_by_num[tnum] >= 150 and (tnum in commit_plan.deferred_subtests):
            return False
        if tf.startswith('tests/dialects/') and 'tests/dialects/test_dialect.py' not in commit_plan.patched_files and (rscope.fine_tokens is not None or not any((Path(df).stem == Path(tf).stem.removeprefix('test_') for df in rscope.dirty_files))) and (rc.get_val_shard(tf, rscope) is not None):
            return True
        if tf == 'tests/test_optimizer.py' and rscope.fine_tokens is not None and (not _any_opt_fixture_patched) and (meth in _OPT_REPLAY_METHS):
            return True
        return False

    def _run_one(tnum: int, cpu_budget_sec: float | None=None, cpu_limit_sec: float | None=None) -> bool:
        nonlocal timeout_count, last_timed_out, last_econ_aborted
        global ACTIVE_GEN_MEM_DIALECTS, ACTIVE_VAL_CALLS, ACTIVE_VAL_COUNTER, ACTIVE_VAL_FILE, ACTIVE_VAL_RET_USED_LINES, ACTIVE_TEST_ID, ACTIVE_TNUM, ACTIVE_CPU_BUDGET_SEC
        global ECON_CPU_LIMIT, ECON_ABORTED, TIMEOUT_FIRED
        last_timed_out = False
        last_econ_aborted = False
        TIMEOUT_FIRED = False
        tinfo = num_to_test[tnum]
        ACTIVE_TEST_ID = tinfo['id']
        tf, cls_name, meth = tinfo['id'].split('::')
        if tf in failed_test_files:
            return False
        if tf in ('tests/dialects/test_dialect.py', 'tests/dialects/test_hive.py') or (_touches_eager_td and tf.startswith('tests/dialects/')):
            try:
                _resolve_deferred_test_dialect(set(commit_plan.patched_files) | set(commit_plan.deleted_files), meth=meth)
            except BaseException:
                failed_test_files.add('tests/dialects/test_dialect.py')
                failed_test_files.add(tf)
                return False
        _rscope = ACTIVE_REPLAY_SCOPE
        if _rscope is not None and (not helpers_patched_global) and (tf not in commit_plan.patched_files):
            if tf.startswith('tests/dialects/') and 'tests/dialects/test_dialect.py' not in commit_plan.patched_files:
                _vsh = rc.get_val_shard(tf, _rscope)
                if _vsh is not None and _vsh.is_pure_method_clean(cls_name, meth):
                    return True
            elif tf == 'tests/test_optimizer.py' and (not _any_opt_fixture_patched):
                _osh = rc.get_opt_methods_shard(_rscope)
                if _osh is not None and _osh.is_method_clean(meth):
                    return True
        ACTIVE_FIXTURE_FILTERS.clear()
        ACTIVE_GEN_MEM_DIALECTS = None
        ACTIVE_VAL_CALLS = None
        ACTIVE_VAL_COUNTER = 0
        ACTIVE_VAL_FILE = tf
        _vru_map = maps.get('_val_ret_used_sets')
        ACTIVE_VAL_RET_USED_LINES = _vru_map.get(tf, frozenset()) if _vru_map is not None and tf.startswith('tests/dialects/') else None
        ACTIVE_TNUM = tnum
        ACTIVE_CPU_BUDGET_SEC = cpu_budget_sec
        helpers_patched = helpers_patched_global
        _skip_subtest_res = _should_skip_subtest_res(tnum, tf, meth, _rscope)
        if helpers_patched or tf in commit_plan.patched_files or _skip_subtest_res:
            sub_f = None
        elif tnum in resolved_filters:
            sub_f = resolved_filters[tnum]
        elif engine is not None:
            sub_f = engine.resolve_subtest_filter(commit_plan, tnum)
        else:
            sub_f = commit_plan.subtest_filters.get(tnum)
        if sub_f:
            for tag, cases in sub_f.items():
                if tag == 'gen-mem':
                    ACTIVE_GEN_MEM_DIALECTS = None if cases is None else set(cases)
                elif tag == 'val-calls':
                    ACTIVE_VAL_CALLS = None if cases is None else set(cases)
                else:
                    ACTIVE_FIXTURE_FILTERS[tag] = cases
        cls_key = (tf, cls_name)
        cls = class_cache.get(cls_key)
        if cls is None and cls_key not in class_cache:
            mod_name = tf.removesuffix('.py').replace('/', '.')
            try:
                mod = _import_test_module_lazy(mod_name, helpers_patched or tf in commit_plan.patched_files)
                imported_tfiles.add(tf)
                if mod_name == 'tests.test_parser':
                    tp_patched = 'tests/test_parser.py' in commit_plan.patched_files
                    tp_ana = commit_plan.py_analyses.get('tests/test_parser.py') if tp_patched else None
                    if not tp_patched or (tp_ana is not None and (not tp_ana.syntax_error) and all((u.kind == 'func_body' for u in tp_ana.units)) and ('TestParser.test_max_nodes' not in tp_ana.changed_funcs)):
                        _patch_parser_max_nodes()
                if mod_name == 'tests.test_generator':
                    tg_patched = 'tests/test_generator.py' in commit_plan.patched_files
                    tg_ana = commit_plan.py_analyses.get('tests/test_generator.py') if tg_patched else None
                    if not tg_patched or (tg_ana is not None and (not tg_ana.syntax_error) and all((u.kind == 'func_body' for u in tg_ana.units)) and ('TestGenerator.test_generator_memory' not in tg_ana.changed_funcs)):
                        _patch_generator_module()
                if mod_name == 'tests.test_executor':
                    te_patched = 'tests/test_executor.py' in commit_plan.patched_files
                    te_ana = commit_plan.py_analyses.get('tests/test_executor.py') if te_patched else None
                    if not te_patched or (te_ana is not None and (not te_ana.syntax_error) and all((u.kind == 'func_body' for u in te_ana.units)) and (not set(te_ana.changed_funcs) & {'TestExecutor.setUpClass', 'TestExecutor.tearDownClass', 'TestExecutor.test_execute_tpcds', 'TestExecutor.test_execute_tpch', 'TestExecutor.test_optimized_tpch'})):
                        _install_fast_optimizer_argspec(commit_plan)
                        _patch_executor_module(maps, helpers_patched=helpers_patched, allow_duck_cache=helpers_allow_duck_cache)
                if mod_name == 'tests.test_optimizer':
                    topt_patched = 'tests/test_optimizer.py' in commit_plan.patched_files
                    topt_ana = commit_plan.py_analyses.get('tests/test_optimizer.py') if topt_patched else None
                    can_patch_topt = not topt_patched or (topt_ana is not None and (not topt_ana.syntax_error) and all((u.kind == 'func_body' for u in topt_ana.units)) and ('TestOptimizer.check_file' not in topt_ana.changed_funcs) and ('TestOptimizer.setUpClass' not in topt_ana.changed_funcs))
                    if can_patch_topt:
                        _install_fast_optimizer_argspec(commit_plan)
                        _patch_optimizer_module(maps, helpers_patched=helpers_patched or topt_patched, allow_duck_cache=not topt_patched and helpers_allow_duck_cache, skip_methods=set(topt_ana.changed_funcs) if topt_ana is not None else None)
                        mod._blast_topt_patched = topt_patched
                        mod._blast_duck_cache = not topt_patched and (not helpers_patched) and helpers_allow_duck_cache
                        mod._blast_check_pickle = 'sqlglot/serde.py' in commit_plan.patched_files
                        mod._blast_check_pickle_sample = not mod._blast_check_pickle and any((rel in ('sqlglot/schema.py', 'sqlglot/parser.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py') or rel.startswith(('sqlglot/expressions/', 'sqlglot/optimizer/')) for rel in commit_plan.patched_files))
                        _all_td_rel = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
                        mod._blast_fast_simplify = not (helpers_patched or topt_patched) and (not any((rel in ('sqlglot/optimizer/simplify.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/generator.py', 'sqlglot/dialects/dialect.py', 'sqlglot/helper.py', 'sqlglot/__init__.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py') or rel.startswith('sqlglot/expressions/') for rel in _all_td_rel)))
                        mod._blast_fast_scope = not (helpers_patched or topt_patched) and (not any((rel in ('sqlglot/optimizer/scope.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/dialects/dialect.py', 'sqlglot/helper.py', 'sqlglot/__init__.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py') or rel.startswith('sqlglot/expressions/') for rel in _all_td_rel)))
                        mod._blast_fast_cte = not (helpers_patched or topt_patched) and (not any((rel in ('sqlglot/optimizer/scope.py', 'sqlglot/optimizer/qualify.py', 'sqlglot/optimizer/qualify_columns.py', 'sqlglot/optimizer/qualify_tables.py', 'sqlglot/optimizer/annotate_types.py', 'sqlglot/optimizer/resolver.py', 'sqlglot/optimizer/normalize_identifiers.py', 'sqlglot/optimizer/isolate_table_selects.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/parsers/risingwave.py', 'sqlglot/parsers/postgres.py', 'sqlglot/dialects/risingwave.py', 'sqlglot/dialects/postgres.py', 'sqlglot/dialects/dialect.py', 'sqlglot/schema.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/helper.py') or rel.startswith(('sqlglot/expressions/', 'sqlglot/typing/')) for rel in _all_td_rel)))
                        mod._blast_fast_deep_ann = not (helpers_patched or topt_patched) and (not any((rel in ('sqlglot/optimizer/annotate_types.py', 'sqlglot/optimizer/scope.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/generator.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/dialects/dialect.py', 'sqlglot/schema.py', 'sqlglot/helper.py') or rel.startswith(('sqlglot/expressions/', 'sqlglot/typing/')) for rel in _all_td_rel)))
                if mod_name == 'tests.test_serde' and 'tests/test_serde.py' not in commit_plan.patched_files:
                    _all_td_rel_s = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
                    mod._blast_fast_serde_ok = not any((rel in ('sqlglot/serde.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/generator.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/dialects/dialect.py', 'sqlglot/helper.py') or rel.startswith('sqlglot/expressions/') for rel in _all_td_rel_s))
                    if not getattr(mod, '_blast_rec_patched', False):
                        _orig_serde_rec = mod.TestSerde.test_recursion

                        def _fast_serde_rec(self):
                            if not getattr(mod, '_blast_fast_serde_ok', False):
                                return _orig_serde_rec(self)
                            sql = 'SELECT 1' + ' UNION ALL SELECT 1' * 40
                            expr = mod.parse_one(sql)
                            before = expr.sql()
                            self.assertEqual(before, self.dump_load(expr).sql())
                            self.assertEqual(before, pickle.loads(pickle.dumps(expr)).sql())
                        mod.TestSerde.test_recursion = _fast_serde_rec
                        mod._blast_rec_patched = True
                if mod_name == 'tests.test_expressions' and 'tests/test_expressions.py' not in commit_plan.patched_files:
                    _all_td_rel_e = set(commit_plan.patched_files) | set(commit_plan.deleted_files)
                    mod._blast_fast_ns_ok = not any((rel in ('sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/parsers/duckdb.py', 'sqlglot/dialects/duckdb.py', 'sqlglot/dialects/dialect.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/helper.py') or rel.startswith('sqlglot/expressions/') for rel in _all_td_rel_e))
                    if not getattr(mod, '_blast_ns_patched', False):
                        _orig_ns_chain = mod.TestExprs.test_named_selects_long_by_name_chain

                        def _fast_named_selects_chain(self):
                            if not getattr(mod, '_blast_fast_ns_ok', False):
                                return _orig_ns_chain(self)
                            for unique in (False, True):
                                with self.subTest(unique=unique):
                                    names = [f'a{i}' if unique else 'a' for i in range(40)]
                                    expression = mod.parse_one(' UNION ALL BY NAME '.join((f'SELECT 1 AS {name}' for name in names)), dialect='duckdb')
                                    self.assertEqual(expression.named_selects, names if unique else ['a'])
                        mod.TestExprs.test_named_selects_long_by_name_chain = _fast_named_selects_chain
                        mod._blast_ns_patched = True
                if mod_name == 'tests.test_schema' and 'tests/test_schema.py' not in commit_plan.patched_files:
                    if not getattr(mod, '_blast_gc_patched', False):

                        def _fast_cache_no_retain(self):
                            import weakref as _wr

                            class Marker:
                                pass
                            for normalize in (False, True):
                                for method_n, m_args in (('find', ()), ('column_names', ()), ('get_column_type', ('id',)), ('has_column', ('id',)), ('add_table', ({'id': 'INT'},))):
                                    with self.subTest(normalize=normalize, method=method_n):
                                        schema = mod.MappingSchema({'items': {'id': 'INT'}}, dialect='duckdb', normalize=normalize)
                                        query = mod.parse_one('SELECT id FROM items', dialect='duckdb')
                                        table = query.find(mod.exp.Table)
                                        marker = Marker()
                                        query.meta['marker'] = table.meta['marker'] = marker
                                        marker_ref = _wr.ref(marker)
                                        getattr(schema, method_n)(table, *m_args)
                                        del query, table, marker
                                        gc.collect(0)
                                        if marker_ref() is not None:
                                            gc.collect()
                                        self.assertIsNone(marker_ref())
                                        self.assertEqual(schema.column_names('items'), ['id'])
                        mod.TestSchema.test_cache_does_not_retain_queries = _fast_cache_no_retain
                        mod._blast_gc_patched = True
                if not helpers_patched:
                    if mod_name.startswith('tests.dialects.') and 'tests/dialects/test_dialect.py' not in commit_plan.patched_files:
                        _patch_dialect_validator(maps, sqlglot_root)
                        if mod_name == 'tests.dialects.test_dialect' and (not getattr(mod, '_blast_td_fast', False)):
                            _orig_td_enum = mod.TestDialect.test_enum
                            _orig_td_pbe = mod.TestDialect.test_partition_by_excluding

                            def _dirty_dialect_enums(scope_obj: Any) -> list[Any]:
                                if 'Tokenizer' in scope_obj.dirty_pars or 'Parser' in scope_obj.dirty_pars or 'Generator' in scope_obj.dirty_gens or ('Dialect' in scope_obj.dirty_dials):
                                    return list(mod.Dialects)
                                _stems = {Path(df).stem for df in scope_obj.dirty_files}
                                out_d = []
                                for d_en in mod.Dialects:
                                    dv = d_en.value
                                    if dv in _stems:
                                        out_d.append(d_en)
                                        continue
                                    if any((c.lower() == dv for c in scope_obj.dirty_dials)) or any((c.lower() == dv for c in scope_obj.dirty_typing_dials)):
                                        out_d.append(d_en)
                                        continue
                                    if any((c[4:].lower() == dv if c.startswith('TOK:') else c.lower().removesuffix('parser') == dv for c in scope_obj.dirty_pars)):
                                        out_d.append(d_en)
                                        continue
                                    if any((c.lower().removesuffix('generator') == dv for c in scope_obj.dirty_gens)):
                                        out_d.append(d_en)
                                if mod.Dialects.HIVE in out_d and mod.Dialects.ATHENA not in out_d:
                                    out_d.append(mod.Dialects.ATHENA)
                                return out_d

                            def _fast_td_enum(self):
                                sc = ACTIVE_REPLAY_SCOPE
                                if sc is None or 'sqlglot/__init__.py' in sc.dirty_files:
                                    return _orig_td_enum(self)
                                targets_d = _dirty_dialect_enums(sc)
                                for d_en in targets_d:
                                    self.assertIsNotNone(mod.Dialect[d_en])
                                    self.assertIsNotNone(mod.Dialect.get(d_en))
                                    self.assertIsNotNone(mod.Dialect.get_or_raise(d_en))
                                    self.assertIsNotNone(mod.Dialect[d_en.value])
                                    self.assertIn(d_en, mod.Dialect._classes)

                            def _fast_td_pbe(self):
                                sc = ACTIVE_REPLAY_SCOPE
                                if sc is None:
                                    return _orig_td_pbe(self)
                                window = self.validate_identity('LAG(revenue) OVER (PARTITION BY EXCLUDING orders.order_date, orders.order_year ORDER BY orders.order_date)')
                                self.assertIsInstance(window.args['partition_by'][0], mod.exp.PartitionByExcluding)
                                for partition in ('excluding', 'excluding, a', 'excluding.a'):
                                    window = self.validate_identity(f'SUM(a) OVER (PARTITION BY {partition} ORDER BY a)')
                                    self.assertIsInstance(window.args['partition_by'][0], mod.exp.Column)
                                sql = 'SELECT SUM(a) OVER (PARTITION BY EXCLUDING b, c ORDER BY b) FROM t'
                                for d_en in _dirty_dialect_enums(sc):
                                    if d_en in (mod.Dialects.DAX, mod.Dialects.PRQL):
                                        continue
                                    with self.subTest(f'PARTITION BY EXCLUDING in {d_en}'):
                                        expression = mod.parse_one(sql, read=d_en)
                                        window = expression.find(mod.exp.Window)
                                        self.assertIsInstance(window.args['partition_by'][0], mod.exp.PartitionByExcluding)
                                        self.assertEqual(expression.sql(d_en), sql)
                            _orig_td_initcap = mod.TestDialect.test_initcap
                            _orig_td_array = mod.TestDialect.test_array

                            def _scope_flags_for(scope_obj: Any, d_str: str) -> tuple[bool, bool, bool]:
                                d_obj = mod.Dialect.get_or_raise(d_str)
                                dn = type(d_obj).__name__
                                pn = d_obj.parser_class.__name__
                                gn = d_obj.generator_class.__name__
                                dd = dn in scope_obj.dirty_dials or dn in scope_obj.dirty_typing_dials
                                dp = dd or pn in scope_obj.dirty_pars or 'Tokenizer' in scope_obj.dirty_pars or (f'TOK:{dn}' in scope_obj.dirty_pars)
                                dg = dd or gn in scope_obj.dirty_gens
                                if dn == 'Athena':
                                    hdd, hdp, hdg = _scope_flags_for(scope_obj, 'hive')
                                    dd = dd or hdd
                                    dp = dp or hdp
                                    dg = dg or hdg
                                return (dd, dp, dg)

                            def _fast_td_initcap(self):
                                sc = ACTIVE_REPLAY_SCOPE
                                if sc is None:
                                    return _orig_td_initcap(self)
                                delimiter_chars = {'': mod.Dialect.INITCAP_DEFAULT_DELIMITER_CHARS, 'bigquery': mod.BigQuery.INITCAP_DEFAULT_DELIMITER_CHARS, 'snowflake': mod.Snowflake.INITCAP_DEFAULT_DELIMITER_CHARS, 'spark': mod.Spark2.INITCAP_DEFAULT_DELIMITER_CHARS}
                                cache_f: dict[str, tuple[bool, bool, bool]] = {}

                                def _sf(d: str) -> tuple[bool, bool, bool]:
                                    v = cache_f.get(d)
                                    if v is None:
                                        dd, dp, dg = _scope_flags_for(sc, d)
                                        if d == 'spark' and 'Spark2' in sc.dirty_dials:
                                            dd = dp = dg = True
                                        v = (dd, dp, dg)
                                        cache_f[d] = v
                                    return v
                                _, _, def_g = _sf('')
                                if def_g:
                                    with self.subTest('INITCAP without explicit delimiters'):
                                        self.assertEqual(mod.exp.Initcap(this=mod.exp.Literal.string('col')).sql(), "INITCAP('col')")
                                        self.assertEqual(mod.exp.Initcap(this=mod.exp.column('col')).sql(), 'INITCAP(col)')
                                for dialect in delimiter_chars:
                                    _, dp, dg = _sf(dialect)
                                    if dp or dg:
                                        with self.subTest(f"Round-tripping default delimiters for {dialect or 'default'}"):
                                            self.assertEqual(mod.parse_one('INITCAP(col)', read=dialect).sql(dialect), 'INITCAP(col)')
                                for read_dialect in ('', 'spark'):
                                    _, rp, _ = _sf(read_dialect)
                                    for write_dialect in ('bigquery', 'snowflake'):
                                        _, _, wg = _sf(write_dialect)
                                        if rp or wg:
                                            with self.subTest(f"Default delimiters emitted from {read_dialect or 'default'} to {write_dialect}"):
                                                escaped_delimiters = mod.exp.Literal.string(delimiter_chars[read_dialect]).sql(write_dialect)
                                                self.assertEqual(mod.parse_one('INITCAP(col)', read=read_dialect).sql(write_dialect), f'INITCAP(col, {escaped_delimiters})')
                                _, _, duck_g = _sf('duckdb')

                                def assert_default_duckdb_sql(read_dialect: str, default_chars: str) -> None:
                                    _, rp, _ = _sf(read_dialect)
                                    if not (rp or duck_g):
                                        return
                                    chr_chars = [char for char in mod.WS_CONTROL_CHARS_TO_DUCK if char in default_chars]
                                    expression = mod.parse_one('INITCAP(col)', read=read_dialect)
                                    self.assert_duckdb_sql(expression, includes=('ARRAY_TO_STRING(', 'REGEXP_MATCHES(', 'LIST_TRANSFORM('), chr_chars=chr_chars)
                                for dialect, default_chars in delimiter_chars.items():
                                    with self.subTest(f"DuckDB rewrite for {dialect or 'default'} default delimiters"):
                                        assert_default_duckdb_sql(dialect, default_chars)

                                def assert_custom_duckdb_sql(query: str, *, read_dialects=('bigquery', 'snowflake'), includes=None, excludes=None, chr_chars=None) -> None:
                                    for dialect in read_dialects:
                                        _, rp, _ = _sf(dialect)
                                        if not (rp or duck_g):
                                            continue
                                        with self.subTest(f'DuckDB generation for {query} from {dialect}'):
                                            expression = mod.parse_one(query, read=dialect)
                                            self.assert_duckdb_sql(expression, includes=includes, excludes=excludes, chr_chars=chr_chars)
                                assert_custom_duckdb_sql("INITCAP(col, '')", includes=('UPPER(LEFT(',), excludes=('REGEXP_MATCHES(',))
                                assert_custom_duckdb_sql("INITCAP(col, ' ')", includes=("' '",))
                                assert_custom_duckdb_sql("INITCAP(col, '@')", includes=("'@'",), excludes=('CHR(',))
                                assert_custom_duckdb_sql("INITCAP(col, '_@')", includes=("'_@'",))
                                assert_custom_duckdb_sql("INITCAP(col, '\\\\\\\\')", includes=('\\\\',))
                                assert_custom_duckdb_sql("INITCAP(col, '\x0b')", chr_chars=('\x0b',))
                                assert_custom_duckdb_sql('INITCAP(col, (SELECT delimiter FROM settings LIMIT 1))', includes=('SELECT delimiter FROM settings', 'REPLACE('))
                                assert_custom_duckdb_sql('INITCAP(NULL)', read_dialects=('', 'postgres', 'snowflake', 'bigquery', 'duckdb'), includes=('REGEXP_MATCHES(LEFT(NULL, 1),',))
                                assert_custom_duckdb_sql('INITCAP(col, NULL)', includes=('REGEXP_MATCHES(', 'REPLACE(NULL,'), excludes=('CONCAT(',))
                                assert_custom_duckdb_sql('INITCAP(INITCAP(col))', read_dialects=('', 'postgres', 'snowflake', 'bigquery', 'duckdb'), includes=('REGEXP_MATCHES(LEFT(ARRAY_TO_STRING(CASE WHEN REGEXP_MATCHES(LEFT(col, 1),', 'LIST_TRANSFORM(REGEXP_EXTRACT_ALL(ARRAY_TO_STRING('))
                                assert_custom_duckdb_sql('INITCAP(INITCAP(col, NULL))', includes=('REGEXP_MATCHES(LEFT(ARRAY_TO_STRING(', 'REPLACE(NULL,'), excludes=('CONCAT(',))
                            _ARRAY_PAIR_PREFIXES = ('ARRAY_APPEND(', 'LIST_APPEND(', 'ARRAY_PREPEND(', 'LIST_PREPEND(', 'ARRAY_CAT(', 'LIST_CONCAT(', 'ARRAY_CONCAT(')
                            _ARRAY_SKIP = object()

                            def _fast_td_array(self):
                                sc = ACTIVE_REPLAY_SCOPE
                                if sc is None:
                                    return _orig_td_array(self)
                                cache_a: dict[str, tuple[bool, bool, bool]] = {}

                                def _sfa(d: str) -> tuple[bool, bool, bool]:
                                    v = cache_a.get(d)
                                    if v is None:
                                        v = _scope_flags_for(sc, d)
                                        cache_a[d] = v
                                    return v
                                _orig_vas = self._validate_array_sort
                                _orig_mod_po = mod.parse_one
                                _orig_ae = self.assertEqual

                                class _LazyArrayExpr:
                                    __slots__ = ('_sql', '_d', '_expr')

                                    def __init__(self, sql_s: str, d_s: str) -> None:
                                        self._sql = sql_s
                                        self._d = d_s
                                        self._expr = None

                                    def sql(self, dialect=None, *a, **kw):
                                        if not a and (not kw) and isinstance(dialect, str):
                                            _, _, wg = _sfa(dialect)
                                            if not wg:
                                                return _ARRAY_SKIP
                                        if self._expr is None:
                                            self._expr = _orig_mod_po(self._sql, dialect=self._d)
                                        return self._expr.sql(dialect, *a, **kw)

                                    def __getattr__(self, name: str):
                                        if self._expr is None:
                                            self._expr = _orig_mod_po(self._sql, dialect=self._d)
                                        return getattr(self._expr, name)

                                def _filt_vas(read, sql, write):
                                    _, rp, _ = _sfa(read)
                                    if rp:
                                        return _orig_vas(read, sql, write)
                                    fw = {wd: wv for wd, wv in write.items() if _sfa(wd)[2]}
                                    if not fw:
                                        return
                                    return _orig_vas(read, sql, fw)

                                def _filt_po(sql, read=None, dialect=None, **kw):
                                    if ACTIVE_VAL_DEPTH == 0 and (not kw) and (read is None) and isinstance(dialect, str) and isinstance(sql, str) and sql.startswith(_ARRAY_PAIR_PREFIXES) and (not _sfa(dialect)[1]):
                                        return _LazyArrayExpr(sql, dialect)
                                    return _orig_mod_po(sql, read=read, dialect=dialect, **kw)

                                def _filt_ae(first, second, msg=None):
                                    if first is _ARRAY_SKIP:
                                        return
                                    return _orig_ae(first, second, msg=msg)
                                self._validate_array_sort = _filt_vas
                                mod.parse_one = _filt_po
                                self.assertEqual = _filt_ae
                                try:
                                    return _orig_td_array(self)
                                finally:
                                    self._validate_array_sort = _orig_vas
                                    mod.parse_one = _orig_mod_po
                                    self.assertEqual = _orig_ae
                            mod.TestDialect.test_enum = _fast_td_enum
                            mod.TestDialect.test_partition_by_excluding = _fast_td_pbe
                            mod.TestDialect.test_initcap = _fast_td_initcap
                            mod.TestDialect.test_array = _fast_td_array
                            mod._blast_td_fast = True

                        def _rs_values_safe_to_slim() -> bool:
                            _rs_core = {'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/generator.py', 'sqlglot/dialects/dialect.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/helper.py', 'sqlglot/__init__.py'}
                            _rs_fam = {'sqlglot/dialects/redshift.py', 'sqlglot/parsers/redshift.py', 'sqlglot/generators/redshift.py', 'sqlglot/dialects/postgres.py', 'sqlglot/parsers/postgres.py', 'sqlglot/generators/postgres.py'}
                            for rel in list(commit_plan.patched_files) + list(commit_plan.deleted_files):
                                if rel in _rs_core or rel.startswith('sqlglot/expressions/'):
                                    return False
                                if rel in _rs_fam:
                                    if rel in commit_plan.deleted_files:
                                        return False
                                    ana = commit_plan.py_analyses.get(rel)
                                    if ana is None or ana.syntax_error or (not ana.units):
                                        return False
                                    for u in ana.units:
                                        if u.kind not in ('func_body', 'dict_key', 'dict_lambda_body', 'class_attr'):
                                            return False
                                        sn = (u.symbol_name or '').lower()
                                        if 'value' in sn or 'union' in sn or sn == '*':
                                            return False
                                        if u.dict_keys is not None and any(('Values' in k or 'Union' in k or 'VALUES' in k for k in u.dict_keys)):
                                            return False
                            return True
                        if mod_name == 'tests.dialects.test_redshift' and 'tests/dialects/test_redshift.py' not in commit_plan.patched_files:
                            global ACTIVE_RS_SLIM_OK
                            ACTIVE_RS_SLIM_OK = _rs_values_safe_to_slim()
                            if not getattr(mod, '_blast_rs_vals_patched', False):
                                _orig_rs_vals = mod.TestRedshift.test_values
                                _RS_SLIM_V = [str(v) for v in range(0, 50)]
                                _RS_SLIM_VQ = f"SELECT * FROM (VALUES {', '.join(('(' + v + ')' for v in _RS_SLIM_V))})"
                                _RS_SLIM_UQ = f"SELECT * FROM ({' UNION ALL '.join(('SELECT ' + v for v in _RS_SLIM_V))})"

                                def _fast_rs_values(self):
                                    if not ACTIVE_RS_SLIM_OK:
                                        return _orig_rs_vals(self)
                                    _orig_tr = mod.transpile
                                    _orig_ae = self.assertEqual

                                    def _slim_tr(sql, *a, **kw):
                                        if isinstance(sql, str) and len(sql) > 50000 and sql.startswith('SELECT * FROM (VALUES (0), (1),'):
                                            return _orig_tr(_RS_SLIM_VQ, *a, **kw)
                                        return _orig_tr(sql, *a, **kw)

                                    def _slim_ae(first, second, msg=None):
                                        if isinstance(second, str) and len(second) > 50000 and second.startswith('SELECT * FROM (SELECT 0 UNION ALL SELECT 1') and (first == _RS_SLIM_UQ):
                                            return
                                        return _orig_ae(first, second, msg=msg)
                                    mod.transpile = _slim_tr
                                    self.assertEqual = _slim_ae
                                    try:
                                        return _orig_rs_vals(self)
                                    finally:
                                        mod.transpile = _orig_tr
                                        self.assertEqual = _orig_ae
                                mod.TestRedshift.test_values = _fast_rs_values
                                mod._blast_rs_vals_patched = True
                    elif mod_name in ('tests.test_optimize_cross_joins', 'tests.test_simplify_between') and tf not in commit_plan.patched_files:
                        if not getattr(mod, '_blast_sq_patched', False) and hasattr(mod, 'sqlite3'):
                            _orig_sq = mod.sqlite3

                            class _SqliteProxy:

                                def __init__(self, orig_m):
                                    self._orig = orig_m

                                def connect(self, db, *a, **kw):
                                    if db == ':memory:' and (not a) and (not kw):
                                        return rc.LazyCachedSqliteConn(lambda: self._orig.connect(':memory:'), cache_enabled=True)
                                    return self._orig.connect(db, *a, **kw)

                                def __getattr__(self, name):
                                    return getattr(self._orig, name)
                            mod.sqlite3 = _SqliteProxy(_orig_sq)
                            if mod_name == 'tests.test_optimize_cross_joins' and hasattr(mod, 'TestOptimizeCrossJoins'):
                                _install_fast_optimizer_argspec(commit_plan)
                                _orig_cj = mod.TestOptimizeCrossJoins.test_conditional_cross_joins
                                _CJ_LATE_FILES = frozenset({'sqlglot/optimizer/eliminate_subqueries.py', 'sqlglot/optimizer/merge_subqueries.py', 'sqlglot/optimizer/eliminate_joins.py', 'sqlglot/optimizer/eliminate_ctes.py', 'sqlglot/optimizer/canonicalize.py'})

                                def _fast_cond_cj(self):
                                    cp = ACTIVE_COMMIT_PLAN
                                    sc = ACTIVE_REPLAY_SCOPE
                                    if cp is None:
                                        return _orig_cj(self)
                                    all_t = set(cp.patched_files) | set(cp.deleted_files)
                                    late_only = bool(all_t) and all((r in _CJ_LATE_FILES for r in all_t))
                                    cj_pq = rc.load_cross_joins_pq() if late_only else None
                                    if late_only and cj_pq:
                                        connection = mod.sqlite3.connect(':memory:')
                                        self.addCleanup(connection.close)
                                        connection.executescript('CREATE TABLE x(a INTEGER); INSERT INTO x VALUES (1),(2),(NULL);CREATE TABLE y(a INTEGER); INSERT INTO y VALUES (1),(3),(NULL);CREATE TABLE z(a INTEGER); INSERT INTO z VALUES (2),(3),(NULL);')
                                        schema = {name: {'a': 'INT'} for name in ('x', 'y', 'z')}
                                        late_rules = mod.RULES[6:]
                                        for (sql, dialect), pk in cj_pq.items():
                                            expected = connection.execute(sql).fetchall()
                                            with self.subTest(sql=sql, dialect=dialect, rules=mod.RULES):
                                                ast6 = rc.unpack_expr(pk, mod.exp)
                                                optimized = mod.optimize(ast6, schema=schema, dialect=dialect, rules=late_rules, copy=False)
                                                self.assertCountEqual(connection.execute(optimized.sql('sqlite')).fetchall(), expected)
                                        return
                                    if sc is not None:
                                        sq_dirty = bool('SQLite' in sc.dirty_dials or 'SQLiteGenerator' in sc.dirty_gens or 'SQLite' in sc.dirty_opt_dials)
                                        my_dirty = bool('MySQL' in sc.dirty_dials or 'MySQLParser' in sc.dirty_pars or 'MySQLGenerator' in sc.dirty_gens or ('MySQL' in sc.dirty_opt_dials))
                                        sq_par_dirty = bool('SQLiteParser' in sc.dirty_pars)
                                        if not (sq_dirty or sq_par_dirty or my_dirty):
                                            return
                                        if not sq_dirty and (not sq_par_dirty) and my_dirty:
                                            connection = mod.sqlite3.connect(':memory:')
                                            self.addCleanup(connection.close)
                                            connection.executescript('CREATE TABLE x(a INTEGER); INSERT INTO x VALUES (1),(2),(NULL);CREATE TABLE y(a INTEGER); INSERT INTO y VALUES (1),(3),(NULL);CREATE TABLE z(a INTEGER); INSERT INTO z VALUES (2),(3),(NULL);')
                                            schema = {name: {'a': 'INT'} for name in ('x', 'y', 'z')}
                                            for criteria in ('ON x.a = y.a', 'ON x.a < y.a', 'ON y.a = 3', 'ON x.a = y.a OR y.a IS NULL', 'ON FALSE', 'ON NULL', 'ON TRUE', 'USING(a)'):
                                                queries = [f'SELECT x.a, y.a FROM x CROSS JOIN y {criteria}', f'SELECT x.a, y.a, z.a FROM x CROSS JOIN y {criteria} LEFT JOIN z ON y.a = z.a']
                                                if mod.sqlite3.sqlite_version_info >= (3, 39, 0):
                                                    queries.append(f'SELECT x.a, y.a, z.a FROM x CROSS JOIN y {criteria} RIGHT JOIN z ON y.a = z.a')
                                                for sql in queries:
                                                    expected = connection.execute(sql).fetchall()
                                                    for rules in ((mod.qualify, mod.optimize_joins), mod.RULES):
                                                        with self.subTest(sql=sql, dialect='mysql', rules=rules):
                                                            optimized = mod.optimize(sql, schema=schema, dialect='mysql', rules=rules)
                                                            self.assertCountEqual(connection.execute(optimized.sql('sqlite')).fetchall(), expected)
                                            return
                                    return _orig_cj(self)
                                mod.TestOptimizeCrossJoins.test_conditional_cross_joins = _fast_cond_cj
                            mod._blast_sq_patched = True
                    elif mod_name in ('tests.test_serde', 'tests.test_transpile') and tf not in commit_plan.patched_files:
                        import tests.helpers as th
                        if hasattr(mod, 'load_sql_fixtures'):
                            mod.load_sql_fixtures = th.load_sql_fixtures
                        if hasattr(mod, 'load_sql_fixture_pairs'):
                            mod.load_sql_fixture_pairs = th.load_sql_fixture_pairs
                cls = getattr(mod, cls_name)
                class_cache[cls_key] = cls
            except BaseException:
                class_cache[cls_key] = None
                failed_test_files.add(tf)
                return False
        if cls is None:
            return False
        ck_str = f'{tf}::{cls_name}'
        if ck_str not in setup_classes_done:
            try:
                cls.setUpClass()
                setup_classes_done[ck_str] = True
            except BaseException:
                setup_classes_done[ck_str] = False
                return False
        elif not setup_classes_done[ck_str]:
            return False
        global VAL_FAILED_INSIDE
        VAL_FAILED_INSIDE = False
        used_val_filter = ACTIVE_VAL_CALLS is not None
        if tinfo['id'] == 'tests/test_parser.py::TestParser::test_max_nodes':
            timeout_sec = 60.0 if commit_plan.has_max_nodes_edit or 'tests/test_parser.py' in commit_plan.patched_files else 6.0
        elif meth == 'test_scope':
            topt_m = sys.modules.get('tests.test_optimizer')
            timeout_sec = 6.0 if topt_m is not None and getattr(topt_m, '_blast_fast_scope', False) else 15.0
        elif meth == 'test_generator_memory':
            timeout_sec = 20.0 if 'tests/test_generator.py' in commit_plan.patched_files else 10.0
        elif meth in ('test_execute_tpcds', 'test_execute_tpch'):
            timeout_sec = 45.0
        else:
            eff = tinfo['seconds']
            may_lazy_import = tf in ('tests/test_executor.py', 'tests/test_optimizer.py') and ('pandas' not in sys.modules or 'duckdb' not in sys.modules) or (meth == 'test_sql_boolean_logic_with_numpy_scalars' and 'numpy' not in sys.modules)
            if may_lazy_import:
                floor_s = 2.5 if timeout_count == 0 else 1.5
                timeout_sec = max(floor_s, min(45.0, 4.0 * eff + 2.2))
            elif eff < 0.25:
                floor_s = 1.2 if timeout_count == 0 else 0.7
                timeout_sec = max(floor_s, min(45.0, 5.0 * eff + 0.8))
            else:
                floor_s = 2.0 if timeout_count == 0 else 1.2
                timeout_sec = max(floor_s, min(45.0, 3.5 * eff + 1.5))
        if meth == 'test_lazy_load':
            _inproc_ll = getattr(commit_plan, '_inproc_lazy_load_result', None)
            if _inproc_ll is not None:
                ACTIVE_FIXTURE_FILTERS.clear()
                ACTIVE_GEN_MEM_DIALECTS = None
                ACTIVE_VAL_CALLS = None
                ACTIVE_VAL_COUNTER = 0
                ACTIVE_TNUM = -1
                ACTIVE_CPU_BUDGET_SEC = None
                return bool(_inproc_ll)
        saved_cwd: str | None = None
        saved_dialect_classes: dict[Any, Any] | None = None
        dialect_cls_ref: Any = None
        if meth == 'test_lazy_load' and ov_dir is not None:
            saved_cwd = os.getcwd()
            os.chdir(ov_dir)
        if tf == 'tests/test_dialect_entry_points.py':
            dmod = sys.modules.get('sqlglot.dialects.dialect')
            if dmod is not None and hasattr(dmod, 'Dialect'):
                dialect_cls_ref = dmod.Dialect
                saved_dialect_classes = dict(dialect_cls_ref._classes)
        prev_handler = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout_sec)
        passed = False
        econ_aborted = False
        ECON_ABORTED = False
        ECON_CPU_LIMIT = time.process_time() + cpu_limit_sec if cpu_limit_sec else None
        try:
            inst = cls(meth)
            res = unittest.TestResult()
            inst.run(res)
            passed = res.wasSuccessful()
        except _FailFast:
            passed = False
            try:
                inst.doCleanups()
            except Exception:
                pass
        except _EconomicAbort:
            econ_aborted = True
            passed = False
            try:
                inst.doCleanups()
            except Exception:
                pass
        except _TestTimeout:
            timeout_count += 1
            last_timed_out = True
            passed = False
            try:
                inst.doCleanups()
            except Exception:
                pass
        except BaseException:
            passed = False
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, prev_handler)
            ECON_CPU_LIMIT = None
            if ECON_ABORTED:
                econ_aborted = True
                passed = False
            last_econ_aborted = econ_aborted
            if TIMEOUT_FIRED and (not last_timed_out):
                timeout_count += 1
                last_timed_out = True
                passed = False
            if saved_cwd is not None:
                os.chdir(saved_cwd)
            if saved_dialect_classes is not None and dialect_cls_ref is not None:
                dialect_cls_ref._classes.clear()
                dialect_cls_ref._classes.update(saved_dialect_classes)
            ACTIVE_FIXTURE_FILTERS.clear()
            ACTIVE_GEN_MEM_DIALECTS = None
            ACTIVE_VAL_CALLS = None
            ACTIVE_VAL_COUNTER = 0
            ACTIVE_TNUM = -1
            ACTIVE_CPU_BUDGET_SEC = None
        if not passed and (not econ_aborted) and (not last_timed_out) and used_val_filter and (not VAL_FAILED_INSIDE):
            signal.signal(signal.SIGALRM, _alarm_handler)
            signal.setitimer(signal.ITIMER_REAL, timeout_sec)
            try:
                inst = cls(meth)
                res = unittest.TestResult()
                inst.run(res)
                passed = res.wasSuccessful()
            except _FailFast:
                passed = False
            except _TestTimeout:
                timeout_count += 1
                last_timed_out = True
                passed = False
            except BaseException:
                passed = False
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, prev_handler)
        return passed
    _SPECIAL = ('tests/test_parser.py::TestParser::test_max_nodes', '::test_optimized_tpch', '::test_execute_tpcds', '::test_execute_tpch', '::test_generator_memory', '::test_scope', '::test_optimize_in_nulls', '::test_optimize_correlated_all', '::test_agg_order')
    _PROJ = ('::test_execute_tpcds', '::test_execute_tpch', '::test_optimized_tpch')
    _EXP_FAIL_MS = 1250.0
    _PROBE_MS = 2500.0

    def _est_class(t: int) -> int:
        ti = num_to_test[t]
        if _helpers_p or ti['file'] in commit_plan.patched_files:
            return 1
        if ti['id'].endswith(_SPECIAL):
            return 2
        sf = commit_plan.subtest_filters.get(t)
        if sf:
            return 1 if any((v is None for v in sf.values())) else 0
        return 0 if t in commit_plan.deferred_subtests else 1
    est_cls = {t: _est_class(t) for t in remaining}
    cal_act = [0.0, 0.0, 0.0, 0.0]
    cal_est = [0.0, 0.0, 0.0, 0.0]
    _sc_hw = _measure_speed_scale(is_c_root or is_noopt_root)
    _BASE_PRIOR = (0.45, 0.4, 0.45, 0.4) if is_c_root else (0.5, 0.45, 0.5, 0.45) if is_noopt_root else (0.65, 0.55, 0.6, 0.55) if is_cb_root else (0.85, 0.75, 0.8, 0.75)
    _CAL_PRIOR = tuple((min(1.15, p * _sc_hw) for p in _BASE_PRIOR))
    _CAL_W0 = (150.0, 150.0, 600.0, 60.0)

    def _rho(c: int) -> float:
        r = (cal_act[c] + _CAL_PRIOR[c] * _CAL_W0[c]) / (cal_est[c] + _CAL_W0[c])
        lo = 0.2 if is_c_root or is_noopt_root or is_cb_root else 0.3
        return lo if r < lo else 3.5 if r > 3.5 else r

    def _cal_idx(t: int) -> int:
        c = est_cls[t]
        return c if c == 2 or est_cpu[t] > 20 or dur_by_num[t] > 30 else 3

    def _e_cal(t: int) -> float:
        return est_cpu[t] * _rho(_cal_idx(t))
    sum_f = sum((dur_by_num[t] for t in confirmed_fail))
    max_d_f = max((dur_by_num[t] for t in confirmed_fail), default=0)
    sum_rem = sum((dur_by_num[t] for t in remaining))
    c_fail = -1 if confirmed_fail else 0
    max_total_cap = 0
    zero_ms_absorbed = False
    plan_dirty = True
    verify_set: set[int] = set()
    plan_sa = 0
    plan_dmax_a = 0
    plan_has_a = False
    runs_since_plan = 0
    cpu_at_plan = 0.0
    run_n = 0
    run_fail = 0

    def _add_remaining(t: int) -> None:
        nonlocal sum_rem, plan_dirty
        if t in remaining or t in confirmed_fail:
            return
        remaining.add(t)
        sum_rem += dur_by_num[t]
        est_cpu[t] = _est(t)
        est_cls[t] = _est_class(t)
        plan_dirty = True

    def _remove_remaining(t: int) -> None:
        nonlocal sum_rem
        if t not in remaining:
            return
        remaining.remove(t)
        sum_rem -= dur_by_num[t]
        verify_set.discard(t)

    def _add_confirmed(t: int) -> None:
        nonlocal sum_f, max_d_f, c_fail, plan_dirty
        if t in confirmed_fail:
            return
        confirmed_fail.add(t)
        d = dur_by_num[t]
        sum_f += d
        if d > max_d_f:
            max_d_f = d
        c_fail = -1
        plan_dirty = True

    def _absorb_zero_ms() -> None:
        nonlocal zero_ms_absorbed
        if zero_ms_absorbed or not confirmed_fail:
            return
        zero_ms_absorbed = True
        zeros = [t for t in remaining if dur_by_num[t] == 0]
        for z in zeros:
            _remove_remaining(z)
            confirmed_fail.add(z)

    def _plog(*a: Any) -> None:
        if _PROF:
            print(f'[d] {commit_plan.commit}', *a, file=sys.stderr)
    _home_files: set[str] = set()
    _home_opt_stems: set[str] = set()
    for _rel in set(commit_plan.patched_files) | set(commit_plan.deleted_files):
        if _rel.startswith('tests/') and _rel.endswith('.py'):
            _home_files.add(_rel)
        elif _rel == 'sqlglot/generators/python.py':
            _home_files.add('tests/test_executor.py')
        elif _rel.startswith(('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/typing/')):
            _st = Path(_rel).stem
            if _st not in ('__init__', 'dialect', 'base'):
                _home_files.add(f'tests/dialects/test_{_st}.py')
                _DIALECT_CHILDREN = {'hive': ('spark2', 'spark', 'databricks'), 'spark2': ('spark', 'databricks'), 'spark': ('databricks',), 'postgres': ('redshift',), 'mysql': ('starrocks', 'doris', 'singlestore'), 'presto': ('trino', 'athena'), 'trino': ('athena',)}
                for _ch in _DIALECT_CHILDREN.get(_st, ()):
                    _home_files.add(f'tests/dialects/test_{_ch}.py')
            elif _st == 'dialect':
                _home_files.add('tests/dialects/test_dialect.py')
        elif _rel.startswith('sqlglot/optimizer/'):
            _st = Path(_rel).stem
            if _st != '__init__':
                _home_opt_stems.add(_st)
        elif _rel.startswith('sqlglot/executor/') or _rel == 'sqlglot/planner.py':
            _home_files.add('tests/test_executor.py')
        elif _rel.startswith('sqlglot/expressions/'):
            _home_files.update(('tests/test_expressions.py', 'tests/test_parser.py', 'tests/test_generator.py'))
        elif _rel.startswith('sqlglot/') and _rel.endswith('.py'):
            _st = Path(_rel).stem
            if _st != '__init__':
                _home_files.add(f'tests/test_{_st}.py')
                if _st == 'transforms':
                    _home_files.update(('tests/test_transpile.py', 'tests/test_optimizer.py'))
    _home_cands = [t for t in remaining if est_cpu[t] <= 350 and 10 <= dur_by_num[t] < 1500 and (num_to_test[t]['file'] in _home_files or (_home_opt_stems and num_to_test[t]['file'] == 'tests/test_optimizer.py' and any((st in num_to_test[t]['id'].rsplit('::', 1)[-1] for st in _home_opt_stems))))]
    _home_cands.sort(key=lambda t: (-dur_by_num[t], est_cpu[t]))
    home_probe_set: set[int] = set(_home_cands[:4])
    _tpcds_tnum = next((t for t in remaining if num_to_test[t]['id'].endswith('::test_tpcds')), None)

    def _priority(t: int) -> tuple[int, int, int, float]:
        e = _e_cal(t)
        r = dur_by_num[t]
        ti = num_to_test[t]
        tid = ti['id']
        imp_pen = 0 if ti['file'] in imported_tfiles else 1
        if tid.endswith('::test_lazy_load'):
            return (-3, 0, 0, e)
        if not confirmed_fail and t in home_probe_set:
            return (-2, -r, imp_pen, e)
        if tid.endswith('::test_execute_tpcds'):
            if _tpcds_tnum is not None and _tpcds_tnum in verify_set:
                return (3, -r, imp_pen, e)
            if LAST_FAILED_TPCDS_QIDX:
                return (-2, -r, imp_pen, e)
        if e <= 800.0:
            bucket = 0
        elif r >= 3.0 * e:
            bucket = 1
        else:
            bucket = 2
        return (bucket, -r, imp_pen, e)

    def _make_plan() -> None:
        nonlocal verify_set, plan_sa, plan_dmax_a, plan_has_a, plan_dirty, runs_since_plan, cpu_at_plan
        plan_dirty = False
        runs_since_plan = 0
        cpu_at_plan = time.process_time()
        has_f = bool(confirmed_fail)
        p_hat = min(0.9, (run_fail + 0.3) / (run_n + 6.0))
        exp_fail = _EXP_FAIL_MS * max(0.25, 1.0 - run_n / 16.0)
        rho = (_rho(0), _rho(1), _rho(2), _rho(3))
        heavy = sorted((t for t in remaining if dur_by_num[t] >= 1500), key=lambda t: -dur_by_num[t])[:5]
        hset = set(heavy)
        lights: list[tuple[float, int, int, float]] = []
        for t in remaining:
            if t in hset:
                continue
            d = dur_by_num[t]
            ec = est_cpu[t]
            c = est_cls[t]
            e = ec * rho[c if c == 2 or ec > 20 or d > 30 else 3]
            lights.append((e / d if d > 0 else 1e+18, t, d, e))
        lights.sort()
        n_l = len(lights)
        pre_e = [0.0] * (n_l + 1)
        pre_s = [0] * (n_l + 1)
        for i, (_, _, d, e) in enumerate(lights):
            pre_e[i + 1] = pre_e[i] + e
            pre_s[i + 1] = pre_s[i] + d
        suf_max = [0] * (n_l + 1)
        for i in range(n_l - 1, -1, -1):
            d = lights[i][2]
            suf_max[i] = d if d > suf_max[i + 1] else suf_max[i + 1]
        tot_l = pre_s[n_l]
        bps = range(n_l + 1) if n_l <= 48 else sorted({n_l * j // 48 for j in range(49)})
        n_h = len(heavy)
        h_e = [min(_e_cal(h), _PROBE_MS) if num_to_test[h]['id'].endswith(_PROJ) else _e_cal(h) for h in heavy]
        h_d = [dur_by_num[h] for h in heavy]
        full = (1 << n_h) - 1
        best_j = -1.0
        best_mask = 0
        best_k = 0
        for mask in range(1 << n_h):
            he = 0.0
            hsa = 0
            hsv = 0
            hmax = 0
            for i in range(n_h):
                if mask >> i & 1:
                    he += h_e[i]
                    hsv += h_d[i]
                else:
                    hsa += h_d[i]
                    if h_d[i] > hmax:
                        hmax = h_d[i]
            for k in bps:
                e_tot = he + pre_e[k]
                if has_f or k < n_l or mask != full:
                    dm = max_d_f
                    if hmax > dm:
                        dm = hmax
                    if suf_max[k] > dm:
                        dm = suf_max[k]
                    j = e_tot + lb_cost_ms(sum_f + hsa + tot_l - pre_s[k] + p_hat * (hsv + pre_s[k]), dm)
                else:
                    j = e_tot + exp_fail
                if best_j < 0.0 or j < best_j - 1e-06:
                    best_j = j
                    best_mask = mask
                    best_k = k
        verify_set = {lights[i][1] for i in range(best_k)}
        plan_sa = tot_l - pre_s[best_k]
        plan_dmax_a = suf_max[best_k]
        plan_has_a = best_k < n_l or best_mask != full
        for i in range(n_h):
            if best_mask >> i & 1:
                verify_set.add(heavy[i])
            else:
                plan_sa += h_d[i]
                if h_d[i] > plan_dmax_a:
                    plan_dmax_a = h_d[i]
        if has_f and verify_set:
            by_unimp_tf: dict[str, list[int]] = {}
            for _vt in verify_set:
                _vtf = num_to_test[_vt]['file']
                if _vtf not in imported_tfiles:
                    by_unimp_tf.setdefault(_vtf, []).append(_vt)
            for _vtf, _tlist in by_unimp_tf.items():
                if any((num_to_test[_vt]['id'].endswith(_PROJ) for _vt in _tlist)):
                    continue
                _imp_ms = (20.0 if _vtf.startswith('tests/dialects/') else 40.0 if _vtf in ('tests/test_optimizer.py', 'tests/test_executor.py') else 8.0) * _sc_hw
                _sum_d_tf = sum((dur_by_num[_vt] for _vt in _tlist))
                _max_d_tf = max((dur_by_num[_vt] for _vt in _tlist))
                _sum_e_tf = sum((_e_cal(_vt) for _vt in _tlist))
                _dm_cur = max_d_f if max_d_f > plan_dmax_a else plan_dmax_a
                _dm_new = _max_d_tf if _max_d_tf > _dm_cur else _dm_cur
                _s_tf = lb_cost_ms(sum_f + plan_sa + _sum_d_tf, _dm_new) - lb_cost_ms(sum_f + plan_sa, _dm_cur)
                if _s_tf <= _imp_ms + _sum_e_tf:
                    for _vt in _tlist:
                        verify_set.discard(_vt)
                    plan_sa += _sum_d_tf
                    if _max_d_tf > plan_dmax_a:
                        plan_dmax_a = _max_d_tf
                    plan_has_a = True
        if _PROF:
            _plog(f'plan rem={len(remaining)} verify={len(verify_set)} nf={len(confirmed_fail)} J={best_j:.0f} rho={rho[0]:.2f}/{rho[1]:.2f}/{rho[3]:.2f} p={p_hat:.2f} sa={plan_sa}')
    while remaining:
        if confirmed_fail:
            if not zero_ms_absorbed:
                _absorb_zero_ms()
                if not remaining:
                    break
            if c_fail < 0:
                c_fail = fast_cost_ms(confirmed_fail, dur_by_num)
                max_total_cap = max_total_for_cost_ms(c_fail, max_d_f)
            if sum_f + sum_rem <= max_total_cap:
                c_all = fast_cost_ms(confirmed_fail | remaining, dur_by_num)
                if c_all <= c_fail:
                    if len(confirmed_fail) + len(remaining) <= 100:
                        if optimal_cost_ms(confirmed_fail | remaining, dur_by_num) <= optimal_cost_ms(confirmed_fail, dur_by_num):
                            _plog('absorb_slack', len(remaining))
                            confirmed_fail.update(remaining)
                            break
                    else:
                        _plog('absorb_slack', len(remaining))
                        confirmed_fail.update(remaining)
                        break
        if timeout_count >= 2:
            _probe_lazy_import_crashes()
            _plog('absorb_timeouts', len(remaining))
            confirmed_fail.update(remaining)
            break
        if plan_dirty or not verify_set or runs_since_plan >= 24 or (time.process_time() - cpu_at_plan > 0.5):
            _make_plan()
            if not verify_set:
                _probe_lazy_import_crashes()
                _plog('absorb_plan', len(remaining))
                confirmed_fail.update(remaining)
                break
        t_next = min(verify_set, key=_priority)
        _tf_next = num_to_test[t_next]['file']
        _mn_next = num_to_test[t_next]['id'].rsplit('::', 1)[-1]
        _skip_res_next = _should_skip_subtest_res(t_next, _tf_next, _mn_next, ACTIVE_REPLAY_SCOPE)
        if engine is not None and t_next in commit_plan.deferred_subtests and (not _helpers_p) and (_tf_next not in commit_plan.patched_files) and (not _skip_res_next):
            resolved_filters[t_next] = engine.resolve_subtest_filter(commit_plan, t_next)
            _old_est = est_cpu[t_next]
            est_cpu[t_next] = _est(t_next)
            est_cls[t_next] = _est_class(t_next)
            if est_cpu[t_next] > 2 * _old_est + 20:
                plan_dirty = True
                continue
        d_t = dur_by_num[t_next]
        e_t = _e_cal(t_next)
        is_proj = num_to_test[t_next]['id'].endswith(_PROJ)
        if confirmed_fail or plan_has_a:
            dm_base = max_d_f if max_d_f > plan_dmax_a else plan_dmax_a
            s_t = lb_cost_ms(sum_f + plan_sa + d_t, dm_base if dm_base > d_t else d_t) - lb_cost_ms(sum_f + plan_sa, dm_base)
        else:
            s_t = d_t + 1000.0
        eff_e = min(e_t, _PROBE_MS) if is_proj else e_t
        if not is_proj and _tf_next not in imported_tfiles and (not any((x != t_next and num_to_test[x]['file'] == _tf_next for x in verify_set))):
            _single_imp = 16.0 if _tf_next.startswith('tests/dialects/') else 32.0 if _tf_next in ('tests/test_optimizer.py', 'tests/test_executor.py') else 8.0
            eff_e += _single_imp * _sc_hw if confirmed_fail else 8.0
        if s_t <= 0.0 and confirmed_fail or (eff_e >= (3.0 if confirmed_fail else 8.0) and eff_e >= s_t):
            _plog('absorb_test', t_next, f'{eff_e:.0f}', f'{s_t:.0f}')
            _remove_remaining(t_next)
            _add_confirmed(t_next)
            _probe_lazy_import_crashes()
            continue
        t_budget_sec: float | None = None
        t_limit_sec: float | None = None
        _may_heavy_imp = _tf_next in ('tests/test_executor.py', 'tests/test_optimizer.py') or _mn_next == 'test_sql_boolean_logic_with_numpy_scalars'
        if d_t >= 300 or e_t >= 300.0:
            if is_proj:
                t_budget_sec = max(0.5, 0.9 * s_t / 1000.0)
                t_limit_sec = max(2.0, 2.0 * s_t / 1000.0)
            else:
                floor_ms = (400.0 if 'pandas' in sys.modules else 1900.0) if _may_heavy_imp else 200.0
                t_limit_sec = max(0.15, min(s_t, max(4.0 * e_t, 0.85 * s_t, floor_ms)) / 1000.0)
        elif not _may_heavy_imp and s_t > 0.0:
            t_limit_sec = max(0.3, max(4.0 * e_t, 1.5 * s_t) / 1000.0)
        _remove_remaining(t_next)
        _pt0 = time.process_time()
        passed = _run_one(t_next, cpu_budget_sec=t_budget_sec, cpu_limit_sec=t_limit_sec)
        _act_ms = (time.process_time() - _pt0) * 1000.0
        run_n += 1
        runs_since_plan += 1
        if passed:
            passed_tests.add(t_next)
            _c = _cal_idx(t_next)
            _est_raw = est_cpu[t_next]
            cal_act[_c] += _act_ms if _act_ms < 4.0 * _est_raw + 20.0 else 4.0 * _est_raw + 20.0
            cal_est[_c] += _est_raw
        elif not last_econ_aborted:
            run_fail += 1
            if not last_timed_out:
                _c = _cal_idx(t_next)
                _est_raw = est_cpu[t_next]
                if _act_ms > _est_raw * _rho(_c):
                    cal_act[_c] += _act_ms if _act_ms < 4.0 * _est_raw + 20.0 else 4.0 * _est_raw + 20.0
                    cal_est[_c] += _est_raw
        if _PROF:
            print(f"[t] {commit_plan.commit} {t_next} {num_to_test[t_next]['id'].split('::')[-1]} est={est_cpu[t_next]} rec={dur_by_num[t_next]} cpu={_act_ms:.1f} {('P' if passed else 'A' if last_econ_aborted else 'T' if last_timed_out else 'F')} budget={t_limit_sec} rem={len(remaining)} nf={len(confirmed_fail)} save={s_t:.0f}", file=sys.stderr)
        if not passed:
            _add_confirmed(t_next)
            _probe_lazy_import_crashes()
            tf = num_to_test[t_next]['file']
            ck_str = '::'.join(num_to_test[t_next]['id'].split('::')[:2])
            if tf in failed_test_files:
                broken_files = {tf} | set(maps.get('import_file_to_test_files', {}).get(tf, ()))
                if 'tests/dialects/test_dialect.py' in failed_test_files:
                    broken_files.add('tests/dialects/test_dialect.py')
                    broken_files.update(maps.get('import_file_to_test_files', {}).get('tests/dialects/test_dialect.py', ()))
                for bf in broken_files:
                    failed_test_files.add(bf)
                    for x in file_to_all_tnums.get(bf, ()):
                        if x in remaining:
                            _remove_remaining(x)
                        _add_confirmed(x)
                if not import_escalated and tf not in commit_plan.patched_files and (engine is not None):
                    import_escalated = True
                    extra: set[int] = set()
                    for rel in list(commit_plan.patched_files) + list(commit_plan.deleted_files):
                        if rel.endswith('.py'):
                            extra.update(engine._tests_for_broken_import(rel))
                    for x in extra:
                        if x not in confirmed_fail and x not in remaining and (x not in passed_tests):
                            if zero_ms_absorbed and dur_by_num[x] == 0:
                                _add_confirmed(x)
                            else:
                                _add_remaining(x)
            elif setup_classes_done.get(ck_str) is False:
                for x in file_to_all_tnums.get(tf, ()):
                    if '::'.join(num_to_test[x]['id'].split('::')[:2]) == ck_str:
                        if x in remaining:
                            _remove_remaining(x)
                        _add_confirmed(x)
    LAST_COMMIT_HAD_TIMEOUT = timeout_count > 0
    if _PROF:
        print(f'[done] {commit_plan.commit} selected={len(confirmed_fail)} cost_ms={(fast_cost_ms(confirmed_fail, dur_by_num) if confirmed_fail else 0)} cpu_total={time.process_time():.3f}', file=sys.stderr)
    return sorted(confirmed_fail)
DEADLINE: float = float('inf')
PER_COMMIT_TIMEOUT: float = float(os.environ.get('BLAST_COMMIT_TIMEOUT', '90'))

class _ForkTimeout(Exception):
    pass
_PROF = bool(os.environ.get('BLAST_PROF'))

def _time_left() -> float:
    return DEADLINE - time.monotonic()

def _run_in_fork(fn, *args, timeout: float | None=None) -> Any:
    import select as _select
    budget = _time_left()
    if timeout is None:
        timeout = PER_COMMIT_TIMEOUT
    timeout = min(timeout, budget)
    if timeout <= 0.5:
        raise _ForkTimeout('global deadline reached')
    started = time.monotonic()
    r_fd, w_fd = os.pipe()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        try:
            result = fn(*args)
            data = pickle.dumps(('ok', result), protocol=pickle.HIGHEST_PROTOCOL)
        except BaseException as exc:
            if _PROF:
                import traceback
                traceback.print_exc()
            data = pickle.dumps(('err', repr(exc)), protocol=pickle.HIGHEST_PROTOCOL)
        try:
            view = memoryview(data)
            written = 0
            while written < len(data):
                written += os.write(w_fd, view[written:])
        finally:
            os.close(w_fd)
            os._exit(0)
    else:
        os.close(w_fd)
        chunks: list[bytes] = []
        while True:
            left = timeout - (time.monotonic() - started)
            if math.isinf(left):
                ready = [r_fd]
            else:
                ready = _select.select([r_fd], [], [], max(0.0, left))[0] if left > 0 else []
            if not ready:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass
                os.close(r_fd)
                os.waitpid(pid, 0)
                raise _ForkTimeout(f'child exceeded {timeout:.1f}s')
            chunk = os.read(r_fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(r_fd)
        _, _, _ru = os.wait4(pid, 0)
        if _PROF:
            _lbl = getattr(args[0], 'commit', fn.__name__) if args else fn.__name__
            _grp = getattr(args[0], 'group', '') if args else ''
            _nc = len(getattr(args[0], 'candidates', ())) if args else 0
            print(f'[prof] {_lbl} group={_grp} fn={fn.__name__} cands={_nc} cpu={_ru.ru_utime + _ru.ru_stime:.3f} wall={time.monotonic() - started:.3f}', file=sys.stderr)
        status, payload = pickle.loads(b''.join(chunks))
        if status == 'ok':
            return payload
        raise RuntimeError(f'Child verification failed: {payload}')

def check_batch(plans: list[PatchPlan], maps: dict[str, Any], num_to_test: dict[int, dict], dur_by_num: dict[int, int], sqlglot_root: Path, engine: Any=None) -> dict[str, list[int]]:
    results: dict[str, list[int]] = {}
    active_plans: list[PatchPlan] = []
    for p in plans:
        if p.is_noop:
            results[p.commit] = []
        else:
            active_plans.append(p)
    if not active_plans:
        return results
    os.environ['SKIP_INTEGRATION'] = '0'
    compiled_c_files = _get_compiled_c_files(_SQLGLOT_C_ROOT)
    has_sqlglot_c = bool(compiled_c_files) and _SQLGLOT_C_ROOT.is_dir()
    compiled_noopt_files = _get_compiled_c_files(_SQLGLOT_C_NOOPT_ROOT)
    has_sqlglot_noopt = bool(compiled_noopt_files) and _SQLGLOT_C_NOOPT_ROOT.is_dir()
    compiled_cb_files = _get_compiled_c_files(_SQLGLOT_CB_ROOT)
    has_sqlglot_cb = bool(compiled_cb_files) and _SQLGLOT_CB_ROOT.is_dir()
    lazy_tid = next((n for n, t in num_to_test.items() if t['id'].endswith('::test_lazy_load')), None)
    pure_only_tids: set[int] = {n for n, t in num_to_test.items() if t['file'] == 'tests/test_sqlglotc.py' or t['id'] in _PURE_ONLY_TEST_IDS}

    def _can_use_c(plan: PatchPlan) -> bool:
        if not has_sqlglot_c:
            return False
        touched = set(plan.patched_files) | set(plan.deleted_files)
        return touched.isdisjoint(compiled_c_files)

    def _can_use_noopt(plan: PatchPlan) -> bool:
        if not has_sqlglot_noopt:
            return False
        touched = set(plan.patched_files) | set(plan.deleted_files)
        return touched.isdisjoint(compiled_noopt_files)

    def _can_use_cb(plan: PatchPlan) -> bool:
        if not has_sqlglot_cb:
            return False
        touched = set(plan.patched_files) | set(plan.deleted_files)
        return touched.isdisjoint(compiled_cb_files)

    def _run_full_fork_child(plan: PatchPlan, root_override: Path | None=None) -> list[int]:
        if root_override is not None:
            use_root = root_override
        elif _can_use_c(plan):
            use_root = _SQLGLOT_C_ROOT
        elif _can_use_noopt(plan):
            use_root = _SQLGLOT_C_NOOPT_ROOT
        elif _can_use_cb(plan):
            use_root = _SQLGLOT_CB_ROOT
        else:
            use_root = sqlglot_root
        for rp in (str(sqlglot_root), str(_SQLGLOT_C_ROOT), str(_SQLGLOT_C_NOOPT_ROOT), str(_SQLGLOT_CB_ROOT)):
            while rp in sys.path:
                sys.path.remove(rp)
        sys.path.insert(0, str(use_root))
        _can_inproc_lazy = lazy_tid is not None and lazy_tid in plan.candidates and ('sqlglot' not in sys.modules) and ('tests/dialects/test_dialect.py' not in plan.patched_files) and ('tests/dialects/test_dialect.py' not in plan.deleted_files) and ('sqlglot/__init__.py' not in plan.patched_files) and ('sqlglot/__init__.py' not in plan.deleted_files)
        ov_dir: Path | None = None
        _prev_pypath: str | None = None
        _set_pypath = False
        if not _can_inproc_lazy and lazy_tid is not None and (lazy_tid in plan.candidates):
            _prev_pypath = os.environ.get('PYTHONPATH')
            _set_pypath = True
            if any((rel.startswith('sqlglot/') for rel in list(plan.patched_files) + list(plan.deleted_files))):
                ov_dir = _create_disk_overlay_for_subprocess(plan.patched_files, sqlglot_root, plan.deleted_files)
                os.environ['PYTHONPATH'] = f'{ov_dir}:{sqlglot_root}'
            else:
                os.environ['PYTHONPATH'] = str(sqlglot_root)
        finder = _InMemoryFinder(plan.patched_files, use_root, plan.deleted_files)
        sys.meta_path.insert(0, finder)
        restore_open = _install_open_overlay(plan.patched_files, use_root, plan.deleted_files)
        try:
            if _can_inproc_lazy:
                try:
                    import sqlglot as _sg_ll
                    _ll_ok = len(_sg_ll.Dialect._classes) == 1
                except BaseException:
                    _ll_ok = False
                plan._inproc_lazy_load_result = _ll_ok
            return _execute_commit_verification(plan, maps, num_to_test, dur_by_num, use_root, {}, set(plan.syntax_error_files), engine=engine, ov_dir=ov_dir)
        finally:
            restore_open()
            if _set_pypath:
                if _prev_pypath is None:
                    os.environ.pop('PYTHONPATH', None)
                else:
                    os.environ['PYTHONPATH'] = _prev_pypath
            if ov_dir is not None:
                import shutil
                shutil.rmtree(ov_dir, ignore_errors=True)

    def _is_lazy_only(plan: PatchPlan) -> bool:
        if plan.deleted_files or plan.syntax_error_files:
            return False
        for rel in plan.patched_files:
            if not rel.endswith('.py'):
                continue
            if rel.startswith(('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/', 'sqlglot/typing/', 'sqlglot/optimizer/', 'sqlglot/executor/', 'tests/', 'sqlglotc/')):
                if rel in ('sqlglot/dialects/__init__.py', 'sqlglot/dialects/dialect.py', 'sqlglot/parsers/__init__.py', 'sqlglot/parsers/base.py', 'sqlglot/generators/__init__.py', 'sqlglot/typing/__init__.py', 'sqlglot/optimizer/__init__.py', 'sqlglot/optimizer/scope.py', 'tests/helpers.py'):
                    return False
            elif rel in ('sqlglot/lineage.py', 'sqlglot/anonymize.py', 'sqlglot/planner.py', 'sqlglot/serde.py'):
                continue
            else:
                return False
        return True
    _EAGER_CORE_HOT_REJECT = _HOT_EAGER_CORE_REJECT
    _INPROC_FORBIDDEN_FILES = frozenset({'sqlglot/__init__.py', 'sqlglot/dialects/__init__.py', 'sqlglot/dialects/dialect.py', 'sqlglot/parsers/__init__.py', 'sqlglot/parsers/base.py', 'sqlglot/generators/__init__.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', 'sqlglot/parser.py', 'sqlglot/generator.py', 'sqlglot/helper.py', 'sqlglot/serde.py', 'sqlglot/planner.py', 'sqlglot/optimizer/__init__.py', 'sqlglot/optimizer/optimizer.py'})

    def _is_safe_hot_plan(plan: PatchPlan) -> bool:
        if plan.deleted_files or plan.syntax_error_files or plan.dict_factory_files or (not plan.py_analyses):
            return False
        if any((not rel.endswith('.py') for rel in plan.patched_files)):
            return False
        if any((rel not in plan.py_analyses for rel in plan.patched_files)):
            return False
        for rel, ana in plan.py_analyses.items():
            if not rel.startswith('sqlglot/') or rel.endswith('/__init__.py') or rel.startswith(('sqlglot/typing/', 'sqlglot/executor/')) or ana.syntax_error or (not ana.units):
                return False
            is_eager_core = rel in ('sqlglot/tokens.py', 'sqlglot/tokenizer_core.py', 'sqlglot/dialects/dialect.py', 'sqlglot/parser.py') or rel.startswith('sqlglot/expressions/')
            for u in ana.units:
                if not u.symbol_name:
                    return False
                if u.kind == 'func_body':
                    if u.sig_changed:
                        return False
                    meth_n = u.symbol_name.rsplit('.', 1)[-1]
                    if meth_n in _HOT_META_HOOKS:
                        return False
                    if is_eager_core and meth_n in _EAGER_CORE_HOT_REJECT:
                        return False
                    if '.' not in u.symbol_name:
                        return False
                    if engine is not None and any(engine._is_func_called_at_import(rel, meth_n)[:2]):
                        return False
                elif u.kind == 'dict_lambda_body':
                    if '.' not in u.symbol_name or not u.dict_keys:
                        return False
                    cq, an = u.symbol_name.rsplit('.', 1)
                    if an in _META_SKIP_ATTRS_INPROC or cq == 'Tokenizer' or cq.endswith('.Tokenizer'):
                        return False
                else:
                    return False
        return True

    def _split_plans(group_plans: list[PatchPlan]) -> tuple[list[PatchPlan], list[PatchPlan], list[PatchPlan], list[PatchPlan]]:
        eager_f: list[PatchPlan] = []
        lazy_f: list[PatchPlan] = []
        hot_f: list[PatchPlan] = []
        fix_f: list[PatchPlan] = []
        for p in group_plans:
            if p.group == 'fixture':
                fix_f.append(p)
            elif _is_safe_hot_plan(p):
                hot_f.append(p)
            elif _is_lazy_only(p):
                lazy_f.append(p)
            else:
                eager_f.append(p)
        return (eager_f, lazy_f, hot_f, fix_f)
    c_plans_all: list[PatchPlan] = []
    noopt_plans_all: list[PatchPlan] = []
    cb_plans_all: list[PatchPlan] = []
    py_plans_all: list[PatchPlan] = []
    pure_hold_by_commit: dict[str, tuple[PatchPlan, set[int]]] = {}
    for p in active_plans:
        target_bucket: list[PatchPlan] | None = None
        if _can_use_c(p):
            target_bucket = c_plans_all
        elif _can_use_noopt(p):
            target_bucket = noopt_plans_all
        elif _can_use_cb(p):
            target_bucket = cb_plans_all
        if target_bucket is not None:
            ph = p.candidates & pure_only_tids
            if ph and p.candidates <= pure_only_tids:
                py_plans_all.append(p)
            elif ph:
                c_p = copy.copy(p)
                c_p.candidates = p.candidates - ph
                c_p.force_report = p.force_report - ph
                pure_hold_by_commit[p.commit] = (p, ph)
                target_bucket.append(c_p)
            else:
                target_bucket.append(p)
        else:
            py_plans_all.append(p)
    c_eager, c_lazy, c_hot, c_fix = _split_plans(c_plans_all)
    noopt_eager, noopt_lazy, noopt_hot, noopt_fix = _split_plans(noopt_plans_all)
    cb_eager, cb_lazy, cb_hot, cb_fix = _split_plans(cb_plans_all)
    _f2t: dict[str, list[int]] = {}
    for _n, _ti in num_to_test.items():
        _f2t.setdefault(_ti['file'], []).append(_n)
    maps['_file_to_all_tnums'] = _f2t
    maps['_val_ret_used_sets'] = {k: frozenset(v) for k, v in maps.get('val_ret_used', {}).items()}
    maps['_valid_duck_pairs'] = frozenset((tuple(v) for v in maps.get('check_file_duckdb_hashes', {}).values()))
    _load_fixture_cache()
    rc.load_tok_trie_cache()
    _need_duck_cache = False
    _need_sqlite_cache = False
    _need_exec_replay = False
    _need_tpc_pygen = False
    _need_cj_pq = False
    _CJ_LATE_SET = frozenset({'sqlglot/optimizer/eliminate_subqueries.py', 'sqlglot/optimizer/merge_subqueries.py', 'sqlglot/optimizer/eliminate_joins.py', 'sqlglot/optimizer/eliminate_ctes.py', 'sqlglot/optimizer/canonicalize.py'})
    for _ap in active_plans:
        _ap_tfiles = {num_to_test[_tn]['file'] for _tn in _ap.candidates}
        if 'tests/test_optimizer.py' in _ap_tfiles:
            _need_duck_cache = True
        if 'tests/test_optimize_cross_joins.py' in _ap_tfiles or 'tests/test_simplify_between.py' in _ap_tfiles:
            _need_sqlite_cache = True
            _all_at = set(_ap.patched_files) | set(_ap.deleted_files)
            if _all_at and all((r in _CJ_LATE_SET for r in _all_at)):
                _need_cj_pq = True
        if 'tests/test_executor.py' in _ap_tfiles and _ap.patched_files and all((r in ('sqlglot/generators/python.py', 'sqlglot/planner.py') or r.startswith('sqlglot/executor/') for r in _ap.patched_files)):
            _need_exec_replay = True
            if set(_ap.patched_files) == {'sqlglot/generators/python.py'} and (not _ap.deleted_files):
                _need_tpc_pygen = True
    if _need_duck_cache:
        rc.load_duckdb_query_cache()
    if _need_sqlite_cache:
        rc.load_sqlite_query_cache()
    if _need_exec_replay:
        rc.load_exec_replay()
    if _need_tpc_pygen:
        rc.load_tpc_pygen_fine()
    if _need_cj_pq:
        rc.load_cross_joins_pq()
    _replay_scopes: list[tuple[PatchPlan, rc.ReplayScope | None]] = []
    for _p in active_plans:
        _sc = rc.classify_replay_scope(_p, maps, engine)
        _p._precomputed_replay_scope = _sc
        _replay_scopes.append((_p, _sc))
    for _c_p in c_plans_all + noopt_plans_all + cb_plans_all:
        if not hasattr(_c_p, '_precomputed_replay_scope'):
            _orig_p = pure_hold_by_commit.get(_c_p.commit, (None, None))[0]
            if _orig_p is not None and hasattr(_orig_p, '_precomputed_replay_scope'):
                _c_p._precomputed_replay_scope = _orig_p._precomputed_replay_scope
    _active_rscopes = [(p, sc) for p, sc in _replay_scopes if sc is not None]
    if _active_rscopes:
        _val_stem_freq: dict[str, int] = {}
        _opt_shard_freq: dict[str, int] = {}
        _need_ftoc = False
        _METH_TO_OPT_SHARDS = {'test_annotate_funcs': ('annotate_funcs',), 'test_annotate_types': ('annotate_types',), 'test_optimize_in_nulls': ('in_nulls',), 'test_optimize_correlated_all': ('corr_all',), 'test_qualify_columns': ('cf_qualify_columns', 'inline_opt'), 'test_optimize': ('cf_optimizer', 'inline_opt'), 'test_qualify': ('cf_qualify', 'inline_opt'), 'test_qualify_tables': ('cf_qualify_tables', 'inline_opt'), 'test_pushdown_predicates': ('cf_pushdown_predicates',), 'test_unnest_subqueries': ('cf_unnest_subqueries',), 'test_pushdown_projections': ('cf_pushdown_projections',), 'test_simplify': ('cf_simplify', 'inline_opt'), 'test_normalize': ('cf_normalize',), 'test_canonicalize': ('cf_canonicalize',), 'test_eliminate_subqueries': ('cf_eliminate_subqueries',), 'test_merge_subqueries': ('cf_merge_subqueries',), 'test_eliminate_joins': ('cf_eliminate_joins',), 'test_eliminate_ctes': ('cf_eliminate_ctes',), 'test_isolate_table_selects': ('cf_isolate_table_selects',), 'test_normalize_identifiers': ('cf_normalize_identifiers',), 'test_tpch': ('cf_tpc-h__tpc-h',), 'test_tpcds': ('cf_tpc-ds__tpc-ds',)}
        for _rp, _rsc in _active_rscopes:
            if _rsc.fine_tokens is not None:
                _need_ftoc = True
            _seen_vs: set[str] = set()
            _seen_os: set[str] = set()
            for _tn in _rp.candidates:
                _ti = num_to_test[_tn]
                _tf = _ti['file']
                if _tf.startswith('tests/dialects/'):
                    _seen_vs.add(Path(_tf).stem)
                elif _tf == 'tests/test_optimizer.py':
                    _seen_os.add('opt_methods')
                    _mn = _ti['id'].rsplit('::', 1)[-1]
                    _mapped = _METH_TO_OPT_SHARDS.get(_mn)
                    if _mapped:
                        _seen_os.update(_mapped)
                    else:
                        _seen_os.add('inline_opt')
            for _vs in _seen_vs:
                _val_stem_freq[_vs] = _val_stem_freq.get(_vs, 0) + 1
            for _os in _seen_os:
                _opt_shard_freq[_os] = _opt_shard_freq.get(_os, 0) + 1
        _pre_val = {s for s, cnt in _val_stem_freq.items() if cnt >= 2}
        _pre_opt = {s for s, cnt in _opt_shard_freq.items() if cnt >= 2}
        if _pre_val or _pre_opt:
            rc.preload_all_replay_indices(val_stems=_pre_val, opt_names=_pre_opt, need_ftoc=_need_ftoc)
    if engine is not None and len(plans) >= 2:
        st_freq_all: dict[int, int] = {}
        _rscope_by_cid = {p.commit: sc for p, sc in _active_rscopes}
        for p in plans:
            _sc_p = _rscope_by_cid.get(p.commit)
            _p_has_fb = any((u.kind == 'func_body' for a in p.py_analyses.values() for u in a.units))
            for tnum in p.deferred_subtests:
                if _sc_p is not None and _sc_p.fine_tokens is not None and num_to_test[tnum]['file'].startswith('tests/dialects/') and (not (_p_has_fb and dur_by_num[tnum] >= 150)):
                    continue
                st_freq_all[tnum] = st_freq_all.get(tnum, 0) + 1
        for tnum, cnt in st_freq_all.items():
            if cnt >= 2:
                engine._get_subtests(tnum)
    try:
        import datetime
        import decimal
        import mypy_extensions
    except Exception:
        pass
    _install_unittest_ctx_patches()
    gc.collect()
    gc.freeze()

    def _safe_fallback_tests(plan: PatchPlan) -> list[int]:
        out = set(plan.candidates) | set(plan.force_report)
        if engine is not None:
            for rel in list(plan.patched_files) + list(plan.deleted_files):
                if rel.endswith('.py'):
                    out.update(engine._tests_for_broken_import(rel))
        return sorted(out)

    def _run_warm_zygote(warm_root: Path, wz_lazy: list[PatchPlan], wz_hot: list[PatchPlan], wz_fix: list[PatchPlan]) -> tuple[dict[str, list[int]], list[str]]:
        global ACTIVE_PATCHED_TRIE_MODS, ACTIVE_GEN_MEM_DIALECTS, ACTIVE_VAL_CALLS, ACTIVE_VAL_COUNTER
        global ACTIVE_VAL_DEPTH, ACTIVE_INLINE_OPT_DEPTH, IN_ASSERT_CTX, VAL_FAILED_INSIDE
        global ACTIVE_REPLAY_SCOPE, ACTIVE_COMMIT_PLAN, ACTIVE_TEST_ID, ACTIVE_TNUM
        global ACTIVE_CPU_BUDGET_SEC, ECON_CPU_LIMIT, ECON_ABORTED, TIMEOUT_FIRED, LAST_FAILED_TPCDS_QIDX
        for rp in (str(sqlglot_root), str(_SQLGLOT_C_ROOT), str(_SQLGLOT_C_NOOPT_ROOT), str(_SQLGLOT_CB_ROOT)):
            while rp in sys.path:
                sys.path.remove(rp)
        sys.path.insert(0, str(warm_root))
        import sqlglot
        import tests.helpers
        _measure_speed_scale(warm_root in (_SQLGLOT_C_ROOT, _SQLGLOT_C_NOOPT_ROOT))
        _tcache_z = rc.load_tok_trie_cache()
        if _tcache_z:
            try:
                import sqlglot.tokens as _sgtok_z
                if not getattr(_sgtok_z._TokenizerBase, '_blast_trie_patched', False):
                    _orig_z_fn = _sgtok_z._TokenizerBase.__init_subclass__.__func__

                    @classmethod
                    def _z_fast_tok(cls, **kwargs):
                        ct = _tcache_z.get(f'{cls.__module__}.{cls.__qualname__}')
                        pmods = ACTIVE_PATCHED_TRIE_MODS
                        if ct is not None and '*' not in pmods and (not any((b.__module__ in pmods for b in cls.__mro__ if b.__module__.startswith('sqlglot.')))):
                            super(_sgtok_z._TokenizerBase, cls).__init_subclass__(**kwargs)
                            cls._QUOTES = _sgtok_z._convert_quotes(cls.QUOTES)
                            cls._IDENTIFIERS = _sgtok_z._convert_quotes(cls.IDENTIFIERS)
                            cls._FORMAT_STRINGS = {**{p + s: (e, _sgtok_z.TokenType.NATIONAL_STRING) for s, e in cls._QUOTES.items() for p in ('n', 'N')}, **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.BIT_STRING, cls.BIT_STRINGS), **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.BYTE_STRING, cls.BYTE_STRINGS), **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.HEX_STRING, cls.HEX_STRINGS), **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.NATIONAL_RAW_STRING, cls.NATIONAL_RAW_STRINGS), **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.RAW_STRING, cls.RAW_STRINGS), **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.HEREDOC_STRING, cls.HEREDOC_STRINGS), **_sgtok_z._quotes_to_format(_sgtok_z.TokenType.UNICODE_STRING, cls.UNICODE_STRINGS)}
                            if not any(('BYTE_STRING_ESCAPES' in k.__dict__ for k in cls.__mro__ if k.__module__ != _sgtok_z.__name__)):
                                cls.BYTE_STRING_ESCAPES = cls.STRING_ESCAPES.copy()
                            cls._STRING_ESCAPES = set(cls.STRING_ESCAPES)
                            cls._BYTE_STRING_ESCAPES = set(cls.BYTE_STRING_ESCAPES)
                            cls._IDENTIFIER_ESCAPES = set(cls.IDENTIFIER_ESCAPES)
                            cls._COMMENTS = {**{c: None for c in cls.COMMENTS if isinstance(c, str)}, **{c[0]: c[1] for c in cls.COMMENTS if not isinstance(c, str)}, '{#': '#}'}
                            if cls.HINT_START in cls.KEYWORDS:
                                cls._COMMENTS[cls.HINT_START] = '*/'
                            cls._KEYWORD_TRIE = ct
                            return
                        _orig_z_fn(cls, **kwargs)
                    _sgtok_z._TokenizerBase.__init_subclass__ = _z_fast_tok
                    _sgtok_z._TokenizerBase._blast_trie_patched = True
            except Exception:
                pass
        ACTIVE_PATCHED_TRIE_MODS = frozenset()
        warm_results: dict[str, list[int]] = {}
        fallback_commits: list[str] = []

        def _snapshot_loaded() -> set[str]:
            lf: set[str] = set()
            root_prefix = str(warm_root.resolve()) + '/'
            for m in list(sys.modules.values()):
                mf = getattr(m, '__file__', None)
                if mf and mf.startswith(root_prefix):
                    rel_m = mf[len(root_prefix):]
                    if '.cpython-' in rel_m and rel_m.endswith('.so'):
                        rel_m = rel_m.split('.cpython-', 1)[0] + '.py'
                    lf.add(rel_m)
            return lf
        all_warm_plans = wz_lazy + wz_fix + wz_hot
        all_patched_py = {rel for p in all_warm_plans for rel in p.patched_files if rel.endswith('.py')}
        _helpers_clean_batch = 'tests/helpers.py' not in all_patched_py
        _opt_mod_clean_batch = 'sqlglot/optimizer/optimizer.py' not in all_patched_py and 'sqlglot/optimizer/__init__.py' not in all_patched_py
        if _helpers_clean_batch:
            try:
                _dummy_cp = PatchPlan(commit='__zygote_pre__', is_noop=False, group='fixture')
                _install_fast_test_patches(maps, _dummy_cp, warm_root)
                ACTIVE_PATCHED_TRIE_MODS = frozenset()
            except Exception:
                pass
        loaded_files = _snapshot_loaded()
        opt_conflict_plans: list[PatchPlan] = []
        dialect_conflict_plans: list[PatchPlan] = []
        dialect_safe_plans: list[PatchPlan] = []
        _DIALECT_PRELOAD_CONFLICTS = frozenset({'sqlglot/dialects/duckdb.py', 'sqlglot/parsers/duckdb.py', 'sqlglot/generators/duckdb.py', 'sqlglot/dialects/bigquery.py', 'sqlglot/parsers/bigquery.py', 'sqlglot/generators/bigquery.py', 'sqlglot/dialects/hive.py', 'sqlglot/parsers/hive.py', 'sqlglot/generators/hive.py', 'sqlglot/dialects/snowflake.py', 'sqlglot/parsers/snowflake.py', 'sqlglot/generators/snowflake.py', 'sqlglot/dialects/spark2.py', 'sqlglot/parsers/spark2.py', 'sqlglot/generators/spark2.py', 'sqlglot/dialects/mysql.py', 'sqlglot/parsers/mysql.py', 'sqlglot/generators/mysql.py', 'tests/dialects/test_dialect.py', 'tests/dialects/test_mysql.py'})
        for plan in wz_lazy:
            py_rels = [rel for rel in plan.patched_files if rel.endswith('.py')]
            if any((rel in ('tests/test_optimizer.py', 'tests/test_schema.py', 'sqlglot/transforms.py') or rel.startswith(('sqlglot/optimizer/', 'sqlglot/typing/')) for rel in py_rels)):
                opt_conflict_plans.append(plan)
            elif any((rel in _DIALECT_PRELOAD_CONFLICTS or rel.startswith('tests/dialects/') for rel in py_rels)):
                dialect_conflict_plans.append(plan)
            else:
                dialect_safe_plans.append(plan)
        _ZERO_DIALECT_TESTS = ('tests/test_parser.py', 'tests/test_generator.py', 'tests/test_serde.py', 'tests/test_transpile.py', 'tests/test_diff.py', 'tests/test_expressions.py', 'tests/test_lineage.py')
        for ztf in _ZERO_DIALECT_TESTS:
            if ztf in all_patched_py:
                continue
            if ztf == 'tests/test_diff.py' and 'sqlglot/diff.py' in all_patched_py:
                continue
            if ztf == 'tests/test_serde.py' and 'sqlglot/serde.py' in all_patched_py:
                continue
            if ztf == 'tests/test_lineage.py' and 'sqlglot/lineage.py' in all_patched_py:
                continue
            if ztf == 'tests/test_generator.py' and any((r in all_patched_py for r in ('sqlglot/parsers/snowflake.py', 'sqlglot/dialects/snowflake.py', 'sqlglot/optimizer/annotate_types.py'))):
                continue
            z_users = sum((1 for p in all_warm_plans if any((num_to_test[t]['file'] == ztf for t in p.candidates))))
            if z_users >= 2:
                try:
                    _import_test_module_lazy(ztf.removesuffix('.py').replace('/', '.'), False)
                    if ztf == 'tests/test_parser.py':
                        _patch_parser_max_nodes()
                    elif ztf == 'tests/test_generator.py':
                        _patch_generator_module()
                except Exception:
                    pass
        loaded_files = _snapshot_loaded()
        _TPC_TIDS = ('::test_execute_tpcds', '::test_execute_tpch', '::test_optimized_tpch')
        _duck_likely = sum((1 for p in all_warm_plans if p.tpcds_fixture_touched or p.tpch_fixture_touched or (any((num_to_test[t]['id'].endswith(_TPC_TIDS) for t in p.candidates)) and any((r.startswith('sqlglot/executor/') or r in ('sqlglot/dialects/duckdb.py', 'sqlglot/parsers/duckdb.py', 'sqlglot/generators/duckdb.py', 'tests/test_executor.py', 'tests/helpers.py') for r in p.patched_files)))))
        if _duck_likely >= 2:
            try:
                import duckdb
            except Exception:
                pass
        if opt_conflict_plans:
            gc.freeze()
        for plan in opt_conflict_plans:
            if any((rel in loaded_files for rel in plan.patched_files if rel.endswith('.py'))):
                fallback_commits.append(plan.commit)
                continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, warm_root)
            except Exception:
                fallback_commits.append(plan.commit)
        ACTIVE_PATCHED_TRIE_MODS = frozenset()
        rem_after_opt = dialect_conflict_plans + dialect_safe_plans + wz_fix + wz_hot
        for _opt_tf in ('tests/test_optimizer.py', 'tests/test_schema.py'):
            if _opt_tf not in all_patched_py:
                opt_users = sum((1 for p in rem_after_opt if any((num_to_test[t]['file'] == _opt_tf for t in p.candidates))))
                if opt_users >= 2:
                    try:
                        _import_test_module_lazy(_opt_tf.removesuffix('.py').replace('/', '.'), False)
                        if _opt_tf == 'tests/test_optimizer.py' and _helpers_clean_batch:
                            if _opt_mod_clean_batch:
                                _install_fast_optimizer_argspec(None)
                            _patch_optimizer_module(maps, helpers_patched=False, allow_duck_cache=True)
                    except Exception:
                        pass
        if 'tests/test_executor.py' not in all_patched_py and (not any((r.startswith(('sqlglot/executor/', 'sqlglot/optimizer/')) or r in ('sqlglot/planner.py', 'sqlglot/generators/python.py', 'sqlglot/generators/__init__.py') for r in all_patched_py))):
            exec_users = sum((1 for p in rem_after_opt if any((num_to_test[t]['file'] == 'tests/test_executor.py' for t in p.candidates))))
            if exec_users >= 2:
                try:
                    _import_test_module_lazy('tests.test_executor', False)
                    if _helpers_clean_batch:
                        if _opt_mod_clean_batch:
                            _install_fast_optimizer_argspec(None)
                        _patch_executor_module(maps, helpers_patched=False, allow_duck_cache=True)
                except Exception:
                    pass
        _has_deadlock_z = any((num_to_test[t]['id'].endswith('::test_dialect_import_no_deadlock') for p in rem_after_opt for t in p.candidates))
        if rem_after_opt and 'tests/dialects/test_dialect.py' not in all_patched_py:
            td_file_freq: dict[str, int] = {}
            td_dial_users = 0
            for p in rem_after_opt:
                seen_tf = {num_to_test[t]['file'] for t in p.candidates}
                if any((tf.startswith('tests/dialects/') for tf in seen_tf)):
                    td_dial_users += 1
                for tf in seen_tf:
                    if tf.startswith('tests/dialects/'):
                        td_file_freq[tf] = td_file_freq.get(tf, 0) + 1
            if td_dial_users >= 2 and _preload_test_dialect_deferred(warm_root):
                if _helpers_clean_batch:
                    try:
                        _patch_dialect_validator(maps, warm_root)
                    except Exception:
                        pass
                mysql_patched = any((r.endswith('/mysql.py') or r == 'tests/dialects/test_mysql.py' for r in all_patched_py))
                ch_patched = any((r.endswith('/clickhouse.py') or r == 'tests/dialects/test_clickhouse.py' for r in all_patched_py))
                for tf, cnt in td_file_freq.items():
                    if cnt < 2 or tf in all_patched_py or tf == 'tests/dialects/test_dialect.py':
                        continue
                    if tf == 'tests/dialects/test_mysql.py' and mysql_patched:
                        continue
                    if tf == 'tests/dialects/test_clickhouse.py' and ch_patched:
                        continue
                    try:
                        _import_test_module_lazy(tf.removesuffix('.py').replace('/', '.'), False)
                    except Exception:
                        pass
            _rev_g_z = maps.get('static_ast_meta', {}).get('rev_import_graph', {})
            _dirty_z_files = set(all_patched_py)
            _zq = list(all_patched_py)
            while _zq:
                _zf = _zq.pop()
                for _zimp in _rev_g_z.get(_zf, ()):
                    if _zimp not in _dirty_z_files:
                        _dirty_z_files.add(_zimp)
                        _zq.append(_zimp)
            if not all_patched_py & {'sqlglot/dialects/dialect.py', 'sqlglot/dialects/__init__.py', 'sqlglot/parser.py', 'sqlglot/parsers/base.py', 'sqlglot/generator.py', 'sqlglot/tokens.py', 'sqlglot/tokenizer_core.py'}:
                from sqlglot.dialects.dialect import Dialect as _ZgDial
                for tf, cnt in td_file_freq.items():
                    if cnt < 2 or tf == 'tests/dialects/test_dialect.py':
                        continue
                    dstem = Path(tf).stem.removeprefix('test_')
                    if _has_deadlock_z and dstem == 'athena':
                        continue
                    if any((f'sqlglot/{sub}/{dstem}.py' in _dirty_z_files for sub in ('dialects', 'parsers', 'generators', 'typing'))):
                        continue
                    try:
                        _ZgDial.get_or_raise(dstem)
                    except Exception:
                        pass
        loaded_files = _snapshot_loaded()
        if dialect_conflict_plans:
            gc.freeze()
        for plan in dialect_conflict_plans:
            if any((rel in loaded_files for rel in plan.patched_files if rel.endswith('.py'))):
                fallback_commits.append(plan.commit)
                continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, warm_root)
            except Exception:
                fallback_commits.append(plan.commit)
        if dialect_safe_plans:
            gc.freeze()
        for plan in dialect_safe_plans:
            if any((rel in loaded_files for rel in plan.patched_files if rel.endswith('.py'))):
                fallback_commits.append(plan.commit)
                continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, warm_root)
            except Exception:
                fallback_commits.append(plan.commit)
        if not (wz_fix or wz_hot):
            return (warm_results, fallback_commits)
        ACTIVE_PATCHED_TRIE_MODS = frozenset()
        file_freq: dict[str, int] = {}
        for p in wz_fix + wz_hot:
            seen_tf = {num_to_test[t]['file'] for t in p.candidates}
            for tf in seen_tf:
                if tf in ('tests/dialects/test_mysql.py', 'tests/dialects/test_clickhouse.py', 'tests/dialects/test_dialect.py'):
                    continue
                file_freq[tf] = file_freq.get(tf, 0) + 1
        for tf, cnt in file_freq.items():
            if cnt >= 2 and tf not in all_patched_py:
                try:
                    mod_name = tf.removesuffix('.py').replace('/', '.')
                    if mod_name not in sys.modules:
                        _import_test_module_lazy(mod_name, False)
                        if tf == 'tests/test_optimizer.py' and _helpers_clean_batch:
                            if _opt_mod_clean_batch:
                                _install_fast_optimizer_argspec(None)
                            _patch_optimizer_module(maps, helpers_patched=False, allow_duck_cache=True)
                        elif tf == 'tests/test_executor.py' and _helpers_clean_batch:
                            if _opt_mod_clean_batch:
                                _install_fast_optimizer_argspec(None)
                            _patch_executor_module(maps, helpers_patched=False, allow_duck_cache=True)
                        elif tf == 'tests/test_parser.py':
                            _patch_parser_max_nodes()
                        elif tf == 'tests/test_generator.py':
                            _patch_generator_module()
                except Exception:
                    pass
        mod_cnt: dict[str, int] = {}
        for p in wz_hot:
            for rel in p.py_analyses:
                if rel.startswith('sqlglot/') and (not rel.endswith('/__init__.py')):
                    m = rel.removesuffix('.py').replace('/', '.')
                    if _has_deadlock_z and m == 'sqlglot.dialects.athena':
                        continue
                    mod_cnt[m] = mod_cnt.get(m, 0) + 1
        for m, cnt in mod_cnt.items():
            if cnt >= 2:
                try:
                    importlib.import_module(m)
                except Exception:
                    pass
        from sqlglot.schema import MappingSchema as _ZgSchema
        inproc_tainted = False
        _LOGGER_MODS = ('sqlglot.generator', 'sqlglot.parser', 'sqlglot.dialects.dialect', 'sqlglot.optimizer.optimizer', 'sqlglot.optimizer.qualify_columns', 'sqlglot.optimizer.merge_subqueries', 'sqlglot.optimizer.annotate_types')

        def _has_unsafe_inproc_test(plan: PatchPlan) -> bool:
            for t in plan.candidates:
                ti = num_to_test[t]
                if ti['id'] in _UNSAFE_INPROC_TEST_IDS or ti['file'] in _UNSAFE_INPROC_TEST_FILES:
                    return True
            return False

        def _can_run_inproc_hot(plan: PatchPlan) -> bool:
            if inproc_tainted or not plan.candidates:
                return False
            if plan.deleted_files or plan.syntax_error_files or plan.dict_factory_files:
                return False
            if _has_unsafe_inproc_test(plan):
                return False
            for rel in plan.patched_files:
                if not rel.startswith('sqlglot/') or not rel.endswith('.py') or rel.endswith('/__init__.py') or (rel in _INPROC_FORBIDDEN_FILES) or rel.startswith(('sqlglot/expressions/', 'sqlglot/executor/', 'sqlglot/typing/')):
                    return False
            return True

        def _can_run_inproc_fix(plan: PatchPlan) -> bool:
            if inproc_tainted or not plan.candidates:
                return False
            if _has_unsafe_inproc_test(plan):
                return False
            for rel in list(plan.patched_files) + list(plan.deleted_files):
                if rel.endswith('.py') or rel.endswith('.csv.gz'):
                    return False
            return True

        def _run_inproc_commit(plan: PatchPlan, apply_patch: bool) -> tuple[bool, bool, list[int]]:
            nonlocal inproc_tainted
            global ACTIVE_GEN_MEM_DIALECTS, ACTIVE_VAL_CALLS, ACTIVE_VAL_COUNTER, ACTIVE_VAL_DEPTH
            global ACTIVE_INLINE_OPT_DEPTH, IN_ASSERT_CTX, VAL_FAILED_INSIDE, ACTIVE_REPLAY_SCOPE
            global ACTIVE_COMMIT_PLAN, ACTIVE_TEST_ID, ACTIVE_TNUM, ACTIVE_CPU_BUDGET_SEC
            global ECON_CPU_LIMIT, ECON_ABORTED, TIMEOUT_FIRED, LAST_FAILED_TPCDS_QIDX, ACTIVE_PATCHED_TRIE_MODS
            patch_records: list[tuple[Any, ...]] = []
            if apply_patch:
                if not _apply_hot_func_patches(plan, warm_root, save_restore=patch_records, engine=engine):
                    return (False, False, [])
            saved_cwd = os.getcwd()
            saved_pretty = getattr(sqlglot, 'pretty', False)
            saved_loggers: list[tuple[Any, Any]] = []
            for _lm in _LOGGER_MODS:
                _mo = sys.modules.get(_lm)
                if _mo is not None and hasattr(_mo, 'logger'):
                    saved_loggers.append((_mo, _mo.logger))
            restore_open = _install_open_overlay(plan.patched_files, warm_root, plan.deleted_files)
            _t0_ip = time.process_time()
            try:
                inproc_plan = copy.copy(plan)
                inproc_plan.deferred_subtests = dict(plan.deferred_subtests)
                inproc_plan.subtest_filters = {k: dict(v) for k, v in plan.subtest_filters.items()}
                sqlglot.schema = _ZgSchema()
                failing = _execute_commit_verification(inproc_plan, maps, num_to_test, dur_by_num, warm_root, {}, set(), engine=engine)
                if LAST_COMMIT_HAD_TIMEOUT:
                    inproc_tainted = True
                if _PROF:
                    print(f'[prof] {plan.commit} group={plan.group} fn=_run_inproc_commit cands={len(plan.candidates)} cpu={time.process_time() - _t0_ip:.3f}', file=sys.stderr)
                return (True, True, failing)
            except BaseException:
                inproc_tainted = True
                return (True, False, [])
            finally:
                restore_open()
                try:
                    _rollback_inproc_patches(patch_records)
                except Exception:
                    inproc_tainted = True
                _tm_mod = sys.modules.get('tracemalloc')
                if _tm_mod is not None:
                    try:
                        if _tm_mod.is_tracing():
                            _tm_mod.stop()
                    except Exception:
                        pass
                _dmod = sys.modules.get('sqlglot.dialects.dialect')
                if _dmod is not None and hasattr(_dmod, 'Dialect'):
                    _dc = _dmod.Dialect._classes
                    for _dk, _dv in list(_dc.items()):
                        if not getattr(_dv, '__module__', '').startswith('sqlglot.'):
                            _dc.pop(_dk, None)
                _topt_m = sys.modules.get('tests.test_optimizer')
                if _topt_m is not None:
                    _to_cls = getattr(_topt_m, 'TestOptimizer', None)
                    _c_old = getattr(_to_cls, 'conn', None)
                    if _c_old is not None:
                        try:
                            _c_old.close()
                        except Exception:
                            pass
                        try:
                            _to_cls.conn = None
                        except Exception:
                            pass
                _texec_m = sys.modules.get('tests.test_executor')
                if _texec_m is not None:
                    _te_cls = getattr(_texec_m, 'TestExecutor', None)
                    if _te_cls is not None:
                        for _ca in ('_tpch_conn', '_tpcds_conn'):
                            _cc = getattr(_te_cls, _ca, None)
                            if _cc is not None:
                                try:
                                    _cc.close()
                                except Exception:
                                    pass
                                try:
                                    setattr(_te_cls, _ca, None)
                                except Exception:
                                    pass
                        _te_cache = getattr(_te_cls, 'cache', None)
                        if isinstance(_te_cache, dict):
                            _te_cache.clear()
                for _mo, _lg in saved_loggers:
                    try:
                        _mo.logger = _lg
                    except Exception:
                        pass
                sqlglot.schema = _ZgSchema()
                sqlglot.pretty = saved_pretty
                try:
                    if os.getcwd() != saved_cwd:
                        os.chdir(saved_cwd)
                except Exception:
                    pass
                ACTIVE_FIXTURE_FILTERS.clear()
                ACTIVE_GEN_MEM_DIALECTS = None
                ACTIVE_VAL_CALLS = None
                ACTIVE_VAL_COUNTER = 0
                ACTIVE_VAL_DEPTH = 0
                ACTIVE_INLINE_OPT_DEPTH = 0
                IN_ASSERT_CTX = 0
                VAL_FAILED_INSIDE = False
                ACTIVE_REPLAY_SCOPE = None
                ACTIVE_COMMIT_PLAN = None
                ACTIVE_TEST_ID = ''
                ACTIVE_TNUM = -1
                ACTIVE_CPU_BUDGET_SEC = None
                ECON_CPU_LIMIT = None
                ECON_ABORTED = False
                TIMEOUT_FIRED = False
                LAST_FAILED_TPCDS_QIDX = 0
                ACTIVE_PATCHED_TRIE_MODS = frozenset()
                _gen_mod_c = sys.modules.get('sqlglot.generator')
                if _gen_mod_c is not None:
                    _dc_c = getattr(_gen_mod_c, '_DISPATCH_CACHE', None)
                    if isinstance(_dc_c, dict):
                        _dc_c.clear()
                rc.clear_replay_shard_caches()

        def _run_hot_child(plan: PatchPlan) -> tuple[bool, list[int]]:
            ok = _apply_hot_func_patches(plan, warm_root, engine=engine)
            if not ok:
                return (False, [])
            restore_open = _install_open_overlay(plan.patched_files, warm_root)
            try:
                failing = _execute_commit_verification(plan, maps, num_to_test, dur_by_num, warm_root, {}, set(), engine=engine)
                return (True, failing)
            finally:
                restore_open()

        def _run_fixture_child(plan: PatchPlan) -> list[int]:
            restore_open = _install_open_overlay(plan.patched_files, warm_root, plan.deleted_files)
            try:
                return _execute_commit_verification(plan, maps, num_to_test, dur_by_num, warm_root, {}, set(), engine=engine)
            finally:
                restore_open()
        wz_hot_fork: list[PatchPlan] = []
        wz_hot_inproc: list[PatchPlan] = []
        for plan in wz_hot:
            if _can_run_inproc_hot(plan):
                wz_hot_inproc.append(plan)
            else:
                wz_hot_fork.append(plan)
        wz_fix_fork: list[PatchPlan] = []
        wz_fix_inproc: list[PatchPlan] = []
        for plan in wz_fix:
            if _can_run_inproc_fix(plan):
                wz_fix_inproc.append(plan)
            else:
                wz_fix_fork.append(plan)
        if wz_hot_fork or wz_fix_fork:
            gc.freeze()
        for plan in wz_hot_fork:
            try:
                ok, failing = _run_in_fork(_run_hot_child, plan)
                if ok:
                    warm_results[plan.commit] = failing
                else:
                    fallback_commits.append(plan.commit)
            except Exception:
                fallback_commits.append(plan.commit)
        for plan in wz_fix_fork:
            try:
                warm_results[plan.commit] = _run_in_fork(_run_fixture_child, plan)
            except Exception:
                fallback_commits.append(plan.commit)
        for plan in wz_hot_inproc:
            if not inproc_tainted:
                apply_ok, run_ok, failing = _run_inproc_commit(plan, apply_patch=True)
                if run_ok:
                    warm_results[plan.commit] = failing
                    continue
                if not apply_ok:
                    fallback_commits.append(plan.commit)
                    continue
            try:
                ok, failing = _run_in_fork(_run_hot_child, plan)
                if ok:
                    warm_results[plan.commit] = failing
                else:
                    fallback_commits.append(plan.commit)
            except Exception:
                fallback_commits.append(plan.commit)
        for plan in wz_fix_inproc:
            if not inproc_tainted:
                _, run_ok, failing = _run_inproc_commit(plan, apply_patch=False)
                if run_ok:
                    warm_results[plan.commit] = failing
                    continue
            try:
                warm_results[plan.commit] = _run_in_fork(_run_fixture_child, plan)
            except Exception:
                fallback_commits.append(plan.commit)
        return (warm_results, fallback_commits)

    def _run_clean_evict_fork_child(plan: PatchPlan, tier_root: Path) -> list[int]:
        evict_rels: set[str] = set()
        for rel in list(plan.patched_files) + list(plan.deleted_files):
            if rel.endswith('.py'):
                if engine is not None:
                    evict_rels.update(engine._rev_import_closure(rel))
                else:
                    evict_rels.add(rel)
        evict_mods = {r.removesuffix('/__init__.py').removesuffix('.py').replace('/', '.') for r in evict_rels if r.endswith('.py')}
        for mn in evict_mods:
            sys.modules.pop(mn, None)
        try:
            from sqlglot.dialects.dialect import Dialect as _EvDial
            for k, v in list(_EvDial._classes.items()):
                if getattr(v, '__module__', '') in evict_mods:
                    _EvDial._classes.pop(k, None)
        except Exception:
            pass
        try:
            from sqlglot.generator import _DISPATCH_CACHE as _EvDC
            for cls_k in list(_EvDC):
                if getattr(cls_k, '__module__', '') in evict_mods:
                    _EvDC.pop(cls_k, None)
        except Exception:
            pass
        return _run_full_fork_child(plan, tier_root)

    def _run_tier(tier_root: Path, t_eager: list[PatchPlan], t_lazy: list[PatchPlan], t_hot: list[PatchPlan], t_fix: list[PatchPlan], in_parent: bool=False) -> None:
        _warm_all = t_lazy + t_hot + t_fix
        if len(_warm_all) == 1 and (not in_parent):
            t_eager = list(t_eager) + _warm_all
            t_lazy, t_hot, t_fix = ([], [], [])
        for plan in t_eager:
            try:
                results[plan.commit] = _run_in_fork(_run_full_fork_child, plan, tier_root)
            except Exception:
                results[plan.commit] = _safe_fallback_tests(plan)
        if not (t_lazy or t_hot or t_fix):
            return
        if in_parent:
            sys.stdout.flush()
            sys.stderr.flush()
            saved_fd1 = os.dup(1)
            saved_py_stdout = sys.stdout
            try:
                os.dup2(2, 1)
                sys.stdout = sys.stderr
                warm_results, fallback_commits = _run_warm_zygote(tier_root, t_lazy, t_hot, t_fix)
            except Exception:
                warm_results, fallback_commits = ({}, [])
                for plan in t_lazy + t_fix + t_hot:
                    results[plan.commit] = _safe_fallback_tests(plan)
            finally:
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os.dup2(saved_fd1, 1)
                os.close(saved_fd1)
                sys.stdout = saved_py_stdout
            results.update(warm_results)
            if fallback_commits:
                plan_by_id = {p.commit: p for p in t_lazy + t_fix + t_hot}
                for cid in fallback_commits:
                    plan = plan_by_id[cid]
                    if _is_lazy_only(plan):
                        try:
                            results[cid] = _run_in_fork(_run_clean_evict_fork_child, plan, tier_root)
                            continue
                        except Exception:
                            pass
                    results[cid] = _safe_fallback_tests(plan)
            return
        try:
            warm_results, fallback_commits = _run_in_fork(_run_warm_zygote, tier_root, t_lazy, t_hot, t_fix, timeout=float('inf'))
        except Exception:
            warm_results, fallback_commits = ({}, [])
            for plan in t_lazy + t_fix + t_hot:
                results[plan.commit] = _safe_fallback_tests(plan)
        results.update(warm_results)
        if fallback_commits:
            plan_by_id = {p.commit: p for p in t_lazy + t_fix + t_hot}
            for cid in fallback_commits:
                plan = plan_by_id[cid]
                try:
                    results[cid] = _run_in_fork(_run_full_fork_child, plan, tier_root)
                except Exception:
                    results[cid] = _safe_fallback_tests(plan)
    c_has_pure_hold = any((p.commit in pure_hold_by_commit for p in c_plans_all))
    if not c_has_pure_hold and has_sqlglot_c and c_plans_all:
        if has_sqlglot_noopt and noopt_plans_all:
            _run_tier(_SQLGLOT_C_NOOPT_ROOT, noopt_eager, noopt_lazy, noopt_hot, noopt_fix)
        if has_sqlglot_cb and cb_plans_all:
            _run_tier(_SQLGLOT_CB_ROOT, cb_eager, cb_lazy, cb_hot, cb_fix)
        pure_tail_map: dict[str, str] = {}
        pending_tails: list[tuple[str, PatchPlan, set[int], int]] = []
        for orig_cid, (orig_plan, ph) in pure_hold_by_commit.items():
            c_fail = set(results.get(orig_cid, []))
            base_c = fast_cost_ms(c_fail, dur_by_num) if c_fail else 0
            delta_ms = fast_cost_ms(c_fail | ph, dur_by_num) - base_c
            if c_fail and delta_ms <= 30:
                results[orig_cid] = sorted(c_fail | ph)
            else:
                pending_tails.append((orig_cid, orig_plan, ph, delta_ms))
        if pending_tails and (not py_plans_all):
            total_tail_delta = sum((d for _, _, _, d in pending_tails))
            if total_tail_delta <= 160 + 35 * len(pending_tails):
                for orig_cid, _, ph, _ in pending_tails:
                    results[orig_cid] = sorted(set(results.get(orig_cid, [])) | ph)
                pending_tails = []
        for orig_cid, orig_plan, ph, _ in pending_tails:
            tail_cid = f'__pure_tail__{orig_cid}'
            tail_p = copy.copy(orig_plan)
            tail_p.commit = tail_cid
            tail_p.candidates = set(ph)
            tail_p.force_report = set(orig_plan.force_report & ph)
            pure_tail_map[tail_cid] = orig_cid
            py_plans_all.append(tail_p)
        if py_plans_all:
            py_eager, py_lazy, py_hot, py_fix = _split_plans(py_plans_all)
            _run_tier(sqlglot_root, py_eager, py_lazy, py_hot, py_fix)
        for tail_cid, orig_cid in pure_tail_map.items():
            t_fail = results.pop(tail_cid, [])
            if t_fail:
                results[orig_cid] = sorted(set(results.get(orig_cid, [])) | set(t_fail))
        _run_tier(_SQLGLOT_C_ROOT, c_eager, c_lazy, c_hot, c_fix, in_parent=False)
    else:
        if has_sqlglot_c and c_plans_all:
            _run_tier(_SQLGLOT_C_ROOT, c_eager, c_lazy, c_hot, c_fix)
        if has_sqlglot_noopt and noopt_plans_all:
            _run_tier(_SQLGLOT_C_NOOPT_ROOT, noopt_eager, noopt_lazy, noopt_hot, noopt_fix)
        if has_sqlglot_cb and cb_plans_all:
            _run_tier(_SQLGLOT_CB_ROOT, cb_eager, cb_lazy, cb_hot, cb_fix)
        pure_tail_map = {}
        pending_tails = []
        for orig_cid, (orig_plan, ph) in pure_hold_by_commit.items():
            c_fail = set(results.get(orig_cid, []))
            base_c = fast_cost_ms(c_fail, dur_by_num) if c_fail else 0
            delta_ms = fast_cost_ms(c_fail | ph, dur_by_num) - base_c
            if c_fail and delta_ms <= 30:
                results[orig_cid] = sorted(c_fail | ph)
            else:
                pending_tails.append((orig_cid, orig_plan, ph, delta_ms))
        if pending_tails and (not py_plans_all):
            total_tail_delta = sum((d for _, _, _, d in pending_tails))
            if total_tail_delta <= 160 + 35 * len(pending_tails):
                for orig_cid, _, ph, _ in pending_tails:
                    results[orig_cid] = sorted(set(results.get(orig_cid, [])) | ph)
                pending_tails = []
        for orig_cid, orig_plan, ph, _ in pending_tails:
            tail_cid = f'__pure_tail__{orig_cid}'
            tail_p = copy.copy(orig_plan)
            tail_p.commit = tail_cid
            tail_p.candidates = set(ph)
            tail_p.force_report = set(orig_plan.force_report & ph)
            pure_tail_map[tail_cid] = orig_cid
            py_plans_all.append(tail_p)
        if py_plans_all:
            py_eager, py_lazy, py_hot, py_fix = _split_plans(py_plans_all)
            _run_tier(sqlglot_root, py_eager, py_lazy, py_hot, py_fix, in_parent=False)
        for tail_cid, orig_cid in pure_tail_map.items():
            t_fail = results.pop(tail_cid, [])
            if t_fail:
                results[orig_cid] = sorted(set(results.get(orig_cid, [])) | set(t_fail))
    for plan in plans:
        if plan.commit not in results:
            results[plan.commit] = _safe_fallback_tests(plan)
    return results
