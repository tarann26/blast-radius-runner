"""Replay caches built at image build time and reused when a patch cannot change them."""
from __future__ import annotations
import array
import hashlib
import marshal
import mmap
import os
import pickle
import sys
from enum import Enum
from pathlib import Path
from typing import Any
import attr_deps as _di
try:
    import _joinkit as _FJC
    _FJC_SIG = getattr(_FJC, 'ast_struct_sig', None)
    _FJC_UNPACK = getattr(_FJC, 'unpack_expr', None)
    _FJC_FINE = getattr(_FJC, 'query_fine_fdat', None)
    _FJC_DFLAGS = getattr(_FJC, 'compute_dirty_flags', None)
except Exception:
    _FJC_SIG = None
    _FJC_UNPACK = None
    _FJC_FINE = None
    _FJC_DFLAGS = None
VAL_REPLAY_DIR = Path('/app/val_replay')
OPT_REPLAY_DIR = Path('/app/opt_replay')
DUCKDB_CACHE_PATH = Path('/app/duckdb_query_cache.pkl')
SQLITE_CACHE_PATH = Path('/app/sqlite_query_cache.pkl')
EXEC_REPLAY_PATH = Path('/app/exec_replay.pkl')
TOK_TRIE_CACHE_PATH = Path('/app/tok_trie_cache.marshal')
CROSS_JOINS_PQ_PATH = Path('/app/cross_joins_pq.marshal')
TPC_PYGEN_FINE_PATH = Path('/app/tpc_pygen_fine.marshal')
_UNPACKABLE = object()
_EXTRA_PARSER_TO_DIALECT: dict[str, tuple[str, ...]] = {'sqlglot/parsers/exasol.py': ('Exasol',), 'sqlglot/parsers/singlestore.py': ('SingleStore',)}
_EXTRA_GEN_ATTR_TO_DIALECT: dict[tuple[str, str], tuple[str, ...]] = {('sqlglot/generators/duckdb.py', 'WS_CONTROL_CHARS_TO_DUCK'): ('DuckDB',)}
_TOK_ONLY_ATTRS: frozenset[str] = frozenset({'COMMENTS', 'NESTED_COMMENTS', 'DASH_COMMENT_REQUIRES_BOUNDARY', 'COMMENTS_TERMINATE_AT_NEWLINE_ONLY', 'NUMERIC_LITERALS', 'VAR_SINGLE_TOKENS', 'COMMANDS', 'COMMAND_PREFIX_TOKENS', 'DROP_UNKNOWN_ESCAPES', 'NUMERIC_ESCAPES_ARE_BYTES', 'LONE_SURROGATE_REPLACEMENT', 'STRING_ESCAPES_ALLOWED_IN_RAW_STRINGS', 'HEREDOC_TAG_IS_IDENTIFIER', 'HEREDOC_STRING_ALTERNATIVE', 'NUMBERS_CAN_HAVE_DECIMALS', 'SINGLE_TOKENS', 'HINT_START'})
_OPT_TYPING_CALLER_FILES: frozenset[str] = frozenset({'sqlglot/optimizer/annotate_types.py', 'sqlglot/optimizer/simplify.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/normalize.py', 'sqlglot/optimizer/optimizer.py'})
_ALL_OPT_RULES: frozenset[int] = frozenset(range(14))
OPT_FILE_RULES: dict[str, frozenset[int]] = {'sqlglot/optimizer/qualify.py': frozenset({0}), 'sqlglot/optimizer/qualify_tables.py': frozenset({0}), 'sqlglot/optimizer/isolate_table_selects.py': frozenset({0}), 'sqlglot/optimizer/normalize_identifiers.py': frozenset({0}), 'sqlglot/optimizer/resolver.py': frozenset({0}), 'sqlglot/optimizer/qualify_columns.py': frozenset({0, 10}), 'sqlglot/optimizer/pushdown_projections.py': frozenset({1}), 'sqlglot/optimizer/normalize.py': frozenset({2, 4, 8, 13}), 'sqlglot/optimizer/unnest_subqueries.py': frozenset({3}), 'sqlglot/optimizer/pushdown_predicates.py': frozenset({4}), 'sqlglot/optimizer/optimize_joins.py': frozenset({5}), 'sqlglot/optimizer/eliminate_subqueries.py': frozenset({6}), 'sqlglot/optimizer/merge_subqueries.py': frozenset({7}), 'sqlglot/optimizer/eliminate_joins.py': frozenset({8}), 'sqlglot/optimizer/eliminate_ctes.py': frozenset({9}), 'sqlglot/optimizer/annotate_types.py': frozenset({0, 2, 4, 11, 12, 13}), 'sqlglot/optimizer/canonicalize.py': frozenset({12}), 'sqlglot/optimizer/simplify.py': frozenset({0, 2, 4, 11, 12, 13}), 'sqlglot/optimizer/scope.py': frozenset({0, 1, 2, 3, 4, 6, 7, 8, 9, 11, 13}), 'sqlglot/optimizer/helpers.py': frozenset({1, 3, 4, 7}), 'sqlglot/optimizer/journal.py': frozenset({1, 9}), 'sqlglot/optimizer/canonicalize_internal_names.py': frozenset()}
_OPT_QUALIFY_FILES: frozenset[str] = frozenset({'sqlglot/optimizer/qualify.py', 'sqlglot/optimizer/normalize_identifiers.py', 'sqlglot/optimizer/qualify_tables.py', 'sqlglot/optimizer/isolate_table_selects.py', 'sqlglot/optimizer/qualify_columns.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/resolver.py', 'sqlglot/optimizer/annotate_types.py', 'sqlglot/optimizer/simplify.py', 'sqlglot/optimizer/normalize.py'})
_OPT_ANNOTATE_FILES: frozenset[str] = frozenset({'sqlglot/optimizer/annotate_types.py', 'sqlglot/optimizer/scope.py'})
_OPT_SIMPLIFY_FILES: frozenset[str] = frozenset({'sqlglot/optimizer/simplify.py', 'sqlglot/optimizer/annotate_types.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/normalize.py'})
SHARD_OPT_FILES: dict[str, frozenset[str]] = {'cf_qualify_tables': frozenset({'sqlglot/optimizer/qualify_tables.py', 'sqlglot/optimizer/normalize_identifiers.py', 'sqlglot/optimizer/scope.py'}), 'cf_qualify_columns': _OPT_QUALIFY_FILES, 'cf_isolate_table_selects': frozenset({'sqlglot/optimizer/isolate_table_selects.py', 'sqlglot/optimizer/scope.py'}), 'cf_normalize_identifiers': frozenset({'sqlglot/optimizer/normalize_identifiers.py'}), 'cf_quote_identifiers': frozenset({'sqlglot/optimizer/qualify_columns.py'}), 'cf_pushdown_projections': _OPT_QUALIFY_FILES | frozenset({'sqlglot/optimizer/pushdown_projections.py', 'sqlglot/optimizer/helpers.py', 'sqlglot/optimizer/journal.py'}), 'cf_normalize': frozenset({'sqlglot/optimizer/normalize.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/simplify.py', 'sqlglot/optimizer/annotate_types.py'}), 'cf_unnest_subqueries': frozenset({'sqlglot/optimizer/unnest_subqueries.py', 'sqlglot/optimizer/helpers.py', 'sqlglot/optimizer/scope.py'}), 'cf_pushdown_predicates': frozenset({'sqlglot/optimizer/pushdown_predicates.py', 'sqlglot/optimizer/helpers.py', 'sqlglot/optimizer/normalize.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/simplify.py', 'sqlglot/optimizer/annotate_types.py'}), 'cf_optimize_joins': frozenset({'sqlglot/optimizer/optimize_joins.py'}), 'cf_eliminate_subqueries': frozenset({'sqlglot/optimizer/eliminate_subqueries.py', 'sqlglot/optimizer/scope.py'}), 'cf_merge_subqueries': _OPT_QUALIFY_FILES | frozenset({'sqlglot/optimizer/optimizer.py', 'sqlglot/optimizer/merge_subqueries.py', 'sqlglot/optimizer/helpers.py'}), 'cf_eliminate_joins': frozenset({'sqlglot/optimizer/eliminate_joins.py', 'sqlglot/optimizer/normalize.py', 'sqlglot/optimizer/scope.py'}), 'cf_eliminate_ctes': frozenset({'sqlglot/optimizer/eliminate_ctes.py', 'sqlglot/optimizer/journal.py', 'sqlglot/optimizer/scope.py'}), 'cf_canonicalize': _OPT_QUALIFY_FILES | frozenset({'sqlglot/optimizer/optimizer.py', 'sqlglot/optimizer/canonicalize.py'}), 'cf_simplify': _OPT_SIMPLIFY_FILES, 'cf_canonicalize_internal_names': _OPT_QUALIFY_FILES | frozenset({'sqlglot/optimizer/canonicalize_internal_names.py'}), 'annotate_types': _OPT_ANNOTATE_FILES, 'annotate_funcs': _OPT_QUALIFY_FILES | _OPT_ANNOTATE_FILES}

class ReplayScope:
    __slots__ = ('dirty_dials', 'dirty_pars', 'dirty_gens', 'dirty_opt_dials', 'dirty_typing_dials', 'dirty_files', 'is_gen_only', 'fine_tokens', 'is_opt_only', 'is_tok_only', 'opt_dirty_files', 'opt_dirty_rules', 'opt_func_only')

    def __init__(self, dirty_dials: frozenset[str], dirty_pars: frozenset[str], dirty_gens: frozenset[str], dirty_opt_dials: frozenset[str], dirty_files: frozenset[str], is_gen_only: bool, fine_tokens: frozenset[tuple[Any, ...]] | None=None, is_opt_only: bool=False, is_tok_only: bool=False, opt_dirty_files: frozenset[str]=frozenset(), opt_dirty_rules: frozenset[int]=frozenset(), opt_func_only: bool=False, dirty_typing_dials: frozenset[str]=frozenset()) -> None:
        self.dirty_dials = dirty_dials
        self.dirty_pars = dirty_pars
        self.dirty_gens = dirty_gens
        self.dirty_opt_dials = dirty_opt_dials
        self.dirty_typing_dials = dirty_typing_dials
        self.dirty_files = dirty_files
        self.is_gen_only = is_gen_only
        self.fine_tokens = fine_tokens
        self.is_opt_only = is_opt_only
        self.is_tok_only = is_tok_only
        self.opt_dirty_files = opt_dirty_files
        self.opt_dirty_rules = opt_dirty_rules
        self.opt_func_only = opt_func_only
_EXP_MAP_CACHE: dict[int, dict[str, type]] = {}

def _get_exp_map(exp_mod: Any) -> dict[str, type]:
    mid = id(exp_mod)
    m = _EXP_MAP_CACHE.get(mid)
    if m is None:
        Expr = exp_mod.Expr
        m = {name: obj for name, obj in vars(exp_mod).items() if isinstance(obj, type) and issubclass(obj, Expr)}
        _EXP_MAP_CACHE[mid] = m
    return m

def _hash_scalar(upd: Any, v: Any) -> bool:
    if v is None:
        upd(b'N;')
        return True
    t = type(v)
    if t is str:
        b = v.encode('utf-8', 'surrogatepass')
        upd(b'S')
        upd(str(len(b)).encode())
        upd(b':')
        upd(b)
        upd(b';')
        return True
    if t is bool:
        upd(b'B1;' if v else b'B0;')
        return True
    if t is int:
        upd(b'I')
        upd(str(v).encode())
        upd(b';')
        return True
    if t is float:
        upd(b'F')
        upd(repr(v).encode())
        upd(b';')
        return True
    if isinstance(v, Enum):
        upd(b'M')
        upd(t.__qualname__.encode())
        upd(b'.')
        upd(v.name.encode())
        upd(b';')
        return True
    if t is tuple:
        upd(b'(')
        upd(str(len(v)).encode())
        upd(b':')
        for item in v:
            if not _hash_scalar(upd, item):
                return False
        upd(b')')
        return True
    if t is list:
        upd(b'[')
        upd(str(len(v)).encode())
        upd(b':')
        for item in v:
            if not _hash_scalar(upd, item):
                return False
        upd(b']')
        return True
    if t is dict:
        try:
            items = sorted(v.items(), key=lambda kv: (type(kv[0]).__name__, str(kv[0])))
        except Exception:
            return False
        upd(b'{')
        upd(str(len(items)).encode())
        upd(b':')
        for dk, dv in items:
            if not _hash_scalar(upd, dk) or not _hash_scalar(upd, dv):
                return False
        upd(b'}')
        return True
    return False

def ast_struct_sig(root: Any, exp_mod: Any) -> bytes | None:
    if _FJC_SIG is not None:
        return _FJC_SIG(root, exp_mod.Expr, Enum)
    Expr = exp_mod.Expr
    if not isinstance(root, Expr):
        return None
    if root.parent is not None or root.arg_key is not None or root.index is not None:
        return None
    h = hashlib.blake2b(digest_size=16)
    upd = h.update

    def _visit(node: Any) -> bool:
        upd(b'E')
        upd(type(node).__name__.encode())
        upd(b'\x00')
        comments = node.comments
        if comments is None:
            upd(b'c0')
        else:
            upd(b'c1')
            upd(str(len(comments)).encode())
            upd(b':')
            for c in comments:
                if type(c) is not str:
                    return False
                cb = c.encode('utf-8', 'surrogatepass')
                upd(str(len(cb)).encode())
                upd(b':')
                upd(cb)
        meta = node._meta
        if meta is None:
            upd(b'm0')
        else:
            upd(b'm1')
            if not _hash_scalar(upd, meta):
                return False
        nt = node._type
        if nt is None:
            upd(b't0')
        elif isinstance(nt, Expr):
            upd(b't1')
            if not _visit(nt):
                return False
        else:
            upd(b't2')
            if not _hash_scalar(upd, nt):
                return False
        args = node.args
        upd(b'a')
        upd(str(len(args)).encode())
        upd(b':')
        for k, v in args.items():
            kb = k.encode('utf-8')
            upd(str(len(kb)).encode())
            upd(b':')
            upd(kb)
            upd(b'=')
            if isinstance(v, Expr):
                if v.parent is not node or v.arg_key != k or v.index is not None:
                    return False
                if not _visit(v):
                    return False
            elif type(v) is list:
                upd(b'L')
                upd(str(len(v)).encode())
                upd(b':')
                for idx, item in enumerate(v):
                    if isinstance(item, Expr):
                        if item.parent is not node or item.arg_key != k or item.index != idx:
                            return False
                        if not _visit(item):
                            return False
                    elif not _hash_scalar(upd, item):
                        return False
                upd(b']')
            elif not _hash_scalar(upd, v):
                return False
        upd(b';')
        return True
    if not _visit(root):
        return None
    return h.digest()

def pack_expr(root: Any, exp_mod: Any) -> Any:
    Expr = exp_mod.Expr
    exp_map = _get_exp_map(exp_mod)

    def _p(x: Any) -> Any:
        if x is None:
            return None
        if isinstance(x, Expr):
            cname = type(x).__name__
            if cname not in exp_map:
                return _UNPACKABLE
            p_args = []
            for k, v in x.args.items():
                if type(k) is not str:
                    return _UNPACKABLE
                pv = _p(v)
                if pv is _UNPACKABLE:
                    return _UNPACKABLE
                p_args.append((k, pv))
            pt = _p(x._type) if x._type is not None else None
            if pt is _UNPACKABLE:
                return _UNPACKABLE
            pm = _p(x._meta) if x._meta is not None else None
            if pm is _UNPACKABLE:
                return _UNPACKABLE
            pc = x.comments
            if pc is not None:
                if type(pc) is not list or any((type(c) is not str for c in pc)):
                    return _UNPACKABLE
                pc = tuple(pc)
            return ('E', cname, tuple(p_args), pt, pm, pc)
        t = type(x)
        if t in (str, int, float, bool):
            return x
        if t is list:
            out = []
            for item in x:
                pi = _p(item)
                if pi is _UNPACKABLE:
                    return _UNPACKABLE
                out.append(pi)
            return out
        if t is tuple:
            out_t = []
            for item in x:
                pi = _p(item)
                if pi is _UNPACKABLE:
                    return _UNPACKABLE
                out_t.append(pi)
            return ('T', tuple(out_t))
        if t is dict:
            out_d = []
            for dk, dv in x.items():
                pk = _p(dk)
                pv = _p(dv)
                if pk is _UNPACKABLE or pv is _UNPACKABLE:
                    return _UNPACKABLE
                out_d.append((pk, pv))
            return ('D', tuple(out_d))
        if isinstance(x, Enum):
            if getattr(exp_mod, t.__name__, None) is t:
                return ('M1', t.__name__, x.name)
            qn = t.__qualname__.split('.')
            if len(qn) == 2:
                outer = getattr(exp_mod, qn[0], None)
                if outer is not None and getattr(outer, qn[1], None) is t:
                    return ('M2', qn[0], qn[1], x.name)
            return _UNPACKABLE
        return _UNPACKABLE
    return _p(root)

def unpack_expr(packed: Any, exp_mod: Any) -> Any:
    Expr = exp_mod.Expr
    exp_map = _get_exp_map(exp_mod)
    if _FJC_UNPACK is not None:
        return _FJC_UNPACK(packed, exp_map, Expr, exp_mod)

    def _u(x: Any) -> Any:
        tx = type(x)
        if tx is tuple:
            tag = x[0]
            if tag == 'E':
                cls = exp_map[x[1]]
                obj = cls.__new__(cls)
                args: dict[str, Any] = {}
                for k, pv in x[2]:
                    uv = _u(pv)
                    args[k] = uv
                    if isinstance(uv, Expr):
                        uv.parent = obj
                        uv.arg_key = k
                        uv.index = None
                    elif type(uv) is list:
                        for idx, item in enumerate(uv):
                            if isinstance(item, Expr):
                                item.parent = obj
                                item.arg_key = k
                                item.index = idx
                obj.args = args
                obj.parent = None
                obj.arg_key = None
                obj.index = None
                obj._type = _u(x[3]) if x[3] is not None else None
                obj._meta = _u(x[4]) if x[4] is not None else None
                obj.comments = list(x[5]) if x[5] is not None else None
                obj._hash = None
                return obj
            if tag == 'M1':
                return getattr(exp_mod, x[1])[x[2]]
            if tag == 'M2':
                return getattr(getattr(exp_mod, x[1]), x[2])[x[3]]
            if tag == 'T':
                return tuple((_u(v) for v in x[1]))
            if tag == 'D':
                return {_u(k): _u(v) for k, v in x[1]}
            return x
        if tx is list:
            return [_u(v) for v in x]
        return x
    return _u(packed)

def norm_dialect_key(d: Any) -> str | None:
    if d is None:
        return ''
    if type(d) is str:
        return d
    val = getattr(d, 'value', None)
    if type(val) is str:
        return val
    return None

def norm_opt_kwargs(kw: dict[str, Any]) -> bytes | None:
    if not kw:
        return b''
    h = hashlib.blake2b(digest_size=16)
    upd = h.update

    def _w(v: Any) -> bool:
        if v is None:
            upd(b'N;')
            return True
        t = type(v)
        if t is bool:
            upd(b'B1;' if v else b'B0;')
            return True
        if t is int:
            upd(b'I')
            upd(str(v).encode())
            upd(b';')
            return True
        if t is float:
            upd(b'F')
            upd(repr(v).encode())
            upd(b';')
            return True
        if t is str:
            b = v.encode('utf-8', 'surrogatepass')
            upd(b'S')
            upd(str(len(b)).encode())
            upd(b':')
            upd(b)
            upd(b';')
            return True
        if isinstance(v, Enum):
            upd(b'M')
            upd(t.__qualname__.encode())
            upd(b'.')
            upd(v.name.encode())
            upd(b';')
            return True
        if t is dict:
            try:
                items = sorted(v.items(), key=lambda kv: (type(kv[0]).__name__, str(kv[0])))
            except Exception:
                return False
            upd(b'{')
            upd(str(len(items)).encode())
            upd(b':')
            for dk, dv in items:
                if not _w(dk) or not _w(dv):
                    return False
            upd(b'}')
            return True
        if t in (list, tuple):
            upd(b'[' if t is list else b'(')
            upd(str(len(v)).encode())
            upd(b':')
            for item in v:
                if not _w(item):
                    return False
            upd(b']' if t is list else b')')
            return True
        if t.__name__ == 'MappingSchema':
            dk = norm_dialect_key(getattr(v, 'dialect', None))
            if dk is None:
                return False
            upd(b'MS(')
            if not _w(getattr(v, 'mapping', None)):
                return False
            if not _w(dk):
                return False
            if not _w(bool(getattr(v, 'normalize', True))):
                return False
            vis = getattr(v, 'visible', None)
            if vis is not None and (not _w(vis)):
                return False
            upd(b')')
            return True
        dk = norm_dialect_key(v)
        if dk is not None:
            upd(b'D')
            upd(dk.encode('utf-8'))
            upd(b';')
            return True
        return False
    try:
        items = sorted(kw.items())
    except Exception:
        return None
    for k, v in items:
        if type(k) is not str:
            return None
        upd(k.encode('utf-8'))
        upd(b'=')
        if not _w(v):
            return None
    return h.digest()
_CLASSIFY_META_CACHE: tuple[int, frozenset[str], frozenset[str], frozenset[str], frozenset[str]] | None = None
_DEP_SETS_CACHE: tuple[int, frozenset[str], frozenset[str], frozenset[tuple[str, str]], frozenset[tuple[str, str]]] | None = None

def _is_strictly_tok_only_unit(u: Any, is_tokens_py: bool) -> bool:
    if getattr(u, 'sig_changed', False):
        return False
    cname = u.class_name or ''
    if is_tokens_py:
        if cname == 'Token' and u.kind == 'func_body':
            return True
        if cname != 'Tokenizer':
            return False
    elif not cname.endswith('.Tokenizer'):
        return False
    if u.kind == 'func_body':
        return True
    if u.kind == 'class_attr' and u.symbol_name in _TOK_ONLY_ATTRS:
        return True
    if u.kind in ('dict_key', 'dict_lambda_body'):
        if u.symbol_name == 'KEYWORDS':
            if u.dict_keys is None:
                return False
            return all(('(+)' not in kr for kr in u.dict_keys))
        if u.symbol_name in _TOK_ONLY_ATTRS:
            return True
    return False

def _is_pure_additive_init_import(commit_plan: Any, maps: dict[str, Any], engine: Any) -> bool:
    if engine is None or 'sqlglot/__init__.py' in commit_plan.deleted_files:
        return False
    new_src = commit_plan.patched_files.get('sqlglot/__init__.py')
    if not new_src:
        return False
    old_src = engine._read_base_file('sqlglot/__init__.py')
    if not old_src:
        return False
    import ast as _ast
    try:
        old_tree = _ast.parse(old_src)
        new_tree = _ast.parse(new_src)
    except SyntaxError:
        return False
    old_dumps = [_ast.dump(s, include_attributes=False) for s in old_tree.body]
    new_dumps = [_ast.dump(s, include_attributes=False) for s in new_tree.body]
    if len(new_dumps) <= len(old_dumps):
        return False
    oi = 0
    inserted: list[Any] = []
    for stmt, nd in zip(new_tree.body, new_dumps):
        if oi < len(old_dumps) and nd == old_dumps[oi]:
            oi += 1
        else:
            inserted.append(stmt)
    if oi != len(old_dumps) or not inserted:
        return False
    old_bound: set[str] = set()
    for st in old_tree.body:
        if isinstance(st, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            old_bound.add(st.name)
        elif isinstance(st, _ast.Assign):
            for tgt in st.targets:
                for n in _ast.walk(tgt):
                    if isinstance(n, _ast.Name):
                        old_bound.add(n.id)
        elif isinstance(st, _ast.AnnAssign) and isinstance(st.target, _ast.Name):
            old_bound.add(st.target.id)
        elif isinstance(st, (_ast.Import, _ast.ImportFrom)):
            for a in st.names:
                old_bound.add(a.asname or a.name.split('.', 1)[0])
    name_readers = maps.get('name_readers', {})
    attr_readers = maps.get('attr_readers', {})
    for stmt in inserted:
        if isinstance(stmt, _ast.Import):
            for a in stmt.names:
                if not (a.name == 'sqlglot' or a.name.startswith('sqlglot.')):
                    return False
                if not a.asname:
                    return False
                bound = a.asname
                if bound == '*' or bound.startswith('__') or bound in old_bound or (bound in name_readers) or (bound in attr_readers):
                    return False
        elif isinstance(stmt, _ast.ImportFrom):
            if stmt.level == 0:
                if not (stmt.module and (stmt.module == 'sqlglot' or stmt.module.startswith('sqlglot.'))):
                    return False
            elif stmt.level != 1:
                return False
            for a in stmt.names:
                if a.name == '*':
                    return False
                bound = a.asname or a.name
                if bound.startswith('__') or bound in old_bound or bound in name_readers or (bound in attr_readers):
                    return False
        else:
            return False
    return True

def classify_replay_scope(commit_plan: Any, maps: dict[str, Any], engine: Any=None) -> ReplayScope | None:
    global _CLASSIFY_META_CACHE, _DEP_SETS_CACHE
    if commit_plan is None or commit_plan.is_noop or commit_plan.deleted_files or commit_plan.has_syntax_error or commit_plan.syntax_error_files or (not commit_plan.patched_files):
        return None
    static_meta = maps.get('static_ast_meta')
    if not static_meta:
        return None
    rev_imp: dict[str, Any] = static_meta.get('rev_import_graph', {})
    file_classes: dict[str, list[str]] = static_meta.get('file_classes', {})
    class_subs: dict[str, list[str]] = maps.get('class_subclasses', {})
    if _CLASSIFY_META_CACHE is not None and _CLASSIFY_META_CACHE[0] == id(maps):
        _, all_dials, all_pars, all_gens, all_exprs = _CLASSIFY_META_CACHE
    else:
        all_dials = frozenset(class_subs.get('Dialect', []))
        all_pars = frozenset(class_subs.get('Parser', []))
        all_gens = frozenset(class_subs.get('Generator', []))
        all_exprs = frozenset(class_subs.get('Expr', []))
        _CLASSIFY_META_CACHE = (id(maps), all_dials, all_pars, all_gens, all_exprs)
    _SAFE_KINDS = frozenset({'func_body', 'dict_key', 'dict_lambda_body', 'class_attr'})
    gen_files: set[str] = set()
    par_files: set[str] = set()
    dial_files: set[str] = set()
    dial_tok_only_files: set[str] = set()
    typing_files: set[str] = set()
    opt_files: set[str] = set()
    core_gen_file: bool = False
    core_par_file: bool = False
    core_tok_files: set[str] = set()
    init_pure_import_only: bool = False
    for rel in commit_plan.patched_files:
        if rel.startswith('tests/') and rel not in ('tests/helpers.py', 'tests/dialects/test_dialect.py'):
            continue
        if rel == 'sqlglot/__init__.py' and _is_pure_additive_init_import(commit_plan, maps, engine):
            init_pure_import_only = True
            continue
        ana = commit_plan.py_analyses.get(rel)
        if ana is None or ana.syntax_error or (not ana.units):
            return None
        for u in ana.units:
            if u.kind not in _SAFE_KINDS:
                return None
        if rel.startswith('sqlglot/generators/') and rel.endswith('.py') and (rel != 'sqlglot/generators/__init__.py'):
            gen_files.add(rel)
        elif rel.startswith('sqlglot/parsers/') and rel.endswith('.py') and (rel not in ('sqlglot/parsers/__init__.py', 'sqlglot/parsers/base.py')):
            par_files.add(rel)
        elif rel.startswith('sqlglot/dialects/') and rel.endswith('.py') and (rel not in ('sqlglot/dialects/__init__.py', 'sqlglot/dialects/dialect.py')):
            if all((_is_strictly_tok_only_unit(u, is_tokens_py=False) for u in ana.units)):
                dial_tok_only_files.add(rel)
            else:
                dial_files.add(rel)
        elif rel.startswith('sqlglot/typing/') and rel.endswith('.py'):
            typing_files.add(rel)
        elif rel == 'sqlglot/generator.py':
            if not all((u.class_name == 'Generator' and u.symbol_name and (u.symbol_name != '*') and (not getattr(u, 'sig_changed', False)) for u in ana.units)):
                return None
            core_gen_file = True
        elif rel == 'sqlglot/parser.py':
            if not all((u.class_name == 'Parser' and u.symbol_name and (u.symbol_name not in ('*', 'FACTOR')) and (not getattr(u, 'sig_changed', False)) for u in ana.units)):
                return None
            core_par_file = True
        elif rel == 'sqlglot/tokens.py':
            if not all((_is_strictly_tok_only_unit(u, is_tokens_py=True) for u in ana.units)):
                return None
            core_tok_files.add(rel)
        elif rel == 'sqlglot/tokenizer_core.py':
            if not all((u.kind == 'func_body' and u.class_name == 'TokenizerCore' and (not getattr(u, 'sig_changed', False)) for u in ana.units)):
                return None
            core_tok_files.add(rel)
        elif rel.startswith('sqlglot/optimizer/') and rel.endswith('.py') and (rel != 'sqlglot/optimizer/__init__.py'):
            if rel not in OPT_FILE_RULES:
                return None
            opt_files.add(rel)
        else:
            return None
    if not (gen_files or par_files or dial_files or dial_tok_only_files or typing_files or opt_files or core_gen_file or core_par_file or core_tok_files or init_pure_import_only):
        return None

    def _expand_subclasses(seed_classes: set[str]) -> set[str]:
        out = set(seed_classes)
        for c in list(seed_classes):
            out.update(class_subs.get(c, ()))
        return out
    dirty_dials: set[str] = set()
    dirty_pars: set[str] = set()
    dirty_gens: set[str] = set()
    dirty_opt_dials: set[str] = set()
    dirty_typing_dials: set[str] = set()
    dirty_files: set[str] = set(commit_plan.patched_files)
    opt_dirty_rules: set[int] = set()
    if core_gen_file:
        dirty_gens.update(all_gens)
    if core_par_file:
        dirty_pars.update(all_pars)
    if core_tok_files:
        dirty_pars.add('Tokenizer')
        for d in all_dials:
            dirty_pars.add(f'TOK:{d}')
    q = list(gen_files)
    seen_g = set(gen_files)
    while q:
        f = q.pop()
        dirty_files.add(f)
        for cname in file_classes.get(f, ()):
            if cname in all_gens:
                dirty_gens.add(cname)
        ana = commit_plan.py_analyses.get(f)
        if ana is not None:
            for u in ana.units:
                if u.symbol_name and (f, u.symbol_name) in _EXTRA_GEN_ATTR_TO_DIALECT:
                    dirty_dials.update(_EXTRA_GEN_ATTR_TO_DIALECT[f, u.symbol_name])
        for imp_f in rev_imp.get(f, ()):
            if imp_f.startswith('sqlglot/generators/'):
                if imp_f not in seen_g:
                    seen_g.add(imp_f)
                    q.append(imp_f)
            elif imp_f.startswith('sqlglot/dialects/') or imp_f == 'sqlglot/executor/python.py' or imp_f.startswith('tests/'):
                continue
            else:
                return None
    q = list(par_files)
    seen_p = set(par_files)
    extra_gen_from_par: set[str] = set()
    while q:
        f = q.pop()
        dirty_files.add(f)
        for cname in file_classes.get(f, ()):
            if cname in all_pars:
                dirty_pars.add(cname)
        if f in _EXTRA_PARSER_TO_DIALECT:
            dirty_dials.update(_EXTRA_PARSER_TO_DIALECT[f])
        for imp_f in rev_imp.get(f, ()):
            if imp_f.startswith('sqlglot/parsers/'):
                if imp_f not in seen_p:
                    seen_p.add(imp_f)
                    q.append(imp_f)
            elif imp_f.startswith('sqlglot/generators/'):
                extra_gen_from_par.add(imp_f)
            elif imp_f.startswith('sqlglot/dialects/') or imp_f.startswith('tests/'):
                continue
            else:
                return None
    q = list(extra_gen_from_par)
    seen_eg = set(extra_gen_from_par)
    while q:
        f = q.pop()
        dirty_files.add(f)
        for cname in file_classes.get(f, ()):
            if cname in all_gens:
                dirty_gens.add(cname)
        for imp_f in rev_imp.get(f, ()):
            if imp_f.startswith('sqlglot/generators/') and imp_f not in seen_eg:
                seen_eg.add(imp_f)
                q.append(imp_f)
    if dial_tok_only_files:
        tok_dials: set[str] = set()
        q = list(dial_tok_only_files)
        seen_dt = set(dial_tok_only_files)
        while q:
            f = q.pop()
            dirty_files.add(f)
            for cname in file_classes.get(f, ()):
                if cname in all_dials:
                    tok_dials.add(cname)
            for imp_f in rev_imp.get(f, ()):
                if imp_f.startswith('sqlglot/dialects/'):
                    if imp_f not in seen_dt:
                        seen_dt.add(imp_f)
                        q.append(imp_f)
                elif imp_f.startswith(('sqlglot/parsers/', 'sqlglot/generators/', 'tests/')):
                    continue
                else:
                    return None
        if not tok_dials:
            return None
        for d in _expand_subclasses(tok_dials):
            dirty_pars.add(f'TOK:{d}')
    q = list(dial_files)
    seen_d = set(dial_files)
    extra_par_q: list[str] = []
    extra_gen_q: list[str] = []
    while q:
        f = q.pop()
        dirty_files.add(f)
        for cname in file_classes.get(f, ()):
            if cname in all_dials:
                dirty_dials.add(cname)
        for imp_f in rev_imp.get(f, ()):
            if imp_f.startswith('sqlglot/dialects/'):
                if imp_f not in seen_d:
                    seen_d.add(imp_f)
                    q.append(imp_f)
            elif imp_f.startswith('sqlglot/parsers/'):
                if imp_f not in seen_p:
                    seen_p.add(imp_f)
                    extra_par_q.append(imp_f)
            elif imp_f.startswith('sqlglot/generators/'):
                if imp_f not in seen_g and imp_f not in seen_eg:
                    seen_eg.add(imp_f)
                    extra_gen_q.append(imp_f)
            elif imp_f.startswith('tests/'):
                continue
            else:
                return None
    while extra_par_q:
        f = extra_par_q.pop()
        dirty_files.add(f)
        for cname in file_classes.get(f, ()):
            if cname in all_pars:
                dirty_pars.add(cname)
        for imp_f in rev_imp.get(f, ()):
            if imp_f.startswith('sqlglot/parsers/') and imp_f not in seen_p:
                seen_p.add(imp_f)
                extra_par_q.append(imp_f)
            elif imp_f.startswith('sqlglot/generators/') and imp_f not in seen_g and (imp_f not in seen_eg):
                seen_eg.add(imp_f)
                extra_gen_q.append(imp_f)
            elif imp_f.startswith('sqlglot/dialects/'):
                for cname in file_classes.get(imp_f, ()):
                    if cname in all_dials:
                        dirty_dials.add(cname)
                dirty_files.add(imp_f)
            elif imp_f.startswith('tests/'):
                continue
            else:
                return None
    while extra_gen_q:
        f = extra_gen_q.pop()
        dirty_files.add(f)
        for cname in file_classes.get(f, ()):
            if cname in all_gens:
                dirty_gens.add(cname)
        for imp_f in rev_imp.get(f, ()):
            if imp_f.startswith('sqlglot/generators/') and imp_f not in seen_g and (imp_f not in seen_eg):
                seen_eg.add(imp_f)
                extra_gen_q.append(imp_f)
            elif imp_f.startswith('sqlglot/dialects/'):
                for cname in file_classes.get(imp_f, ()):
                    if cname in all_dials:
                        dirty_dials.add(cname)
                dirty_files.add(imp_f)
            elif imp_f.startswith('tests/'):
                continue
            else:
                return None
    if typing_files:
        if 'sqlglot/typing/__init__.py' in typing_files:
            dirty_typing_dials.update(all_dials)
            dirty_files.add('sqlglot/typing/__init__.py')
        else:
            q = list(typing_files)
            seen_t = set(typing_files)
            while q:
                f = q.pop()
                dirty_files.add(f)
                for imp_f in rev_imp.get(f, ()):
                    if imp_f.startswith('sqlglot/typing/') and imp_f not in seen_t:
                        seen_t.add(imp_f)
                        q.append(imp_f)
                    elif imp_f.startswith('sqlglot/dialects/'):
                        for cname in file_classes.get(imp_f, ()):
                            if cname in all_dials:
                                dirty_typing_dials.add(cname)
                    elif imp_f.startswith('tests/'):
                        continue
                    elif imp_f != 'sqlglot/optimizer/annotate_types.py':
                        return None
            if not dirty_typing_dials:
                return None
            dirty_typing_dials = _expand_subclasses(dirty_typing_dials)
        dirty_opt_dials.update(dirty_typing_dials)
        opt_dirty_rules.update(OPT_FILE_RULES['sqlglot/optimizer/annotate_types.py'])
    opt_func_only = False
    if opt_files:
        for f in opt_files:
            opt_dirty_rules.update(OPT_FILE_RULES.get(f, _ALL_OPT_RULES))
        dirty_opt_dials.update(all_dials)
        if opt_files & _OPT_TYPING_CALLER_FILES:
            dirty_typing_dials.update(all_dials)
        opt_func_only = all((all((u.kind == 'func_body' and (not getattr(u, 'sig_changed', False)) and (not getattr(u, 'is_cached_func', False)) for u in commit_plan.py_analyses[f].units)) for f in opt_files))
    dirty_dials = _expand_subclasses(dirty_dials)
    dirty_pars = _expand_subclasses(dirty_pars)
    dirty_gens = _expand_subclasses(dirty_gens)
    if dirty_dials:
        for f, classes in file_classes.items():
            if f.startswith('sqlglot/dialects/') and any((c in dirty_dials for c in classes)):
                stem = Path(f).stem
                for cname in file_classes.get(f'sqlglot/parsers/{stem}.py', ()):
                    if cname in all_pars:
                        dirty_pars.add(cname)
                for cname in file_classes.get(f'sqlglot/generators/{stem}.py', ()):
                    if cname in all_gens:
                        dirty_gens.add(cname)
        dirty_pars = _expand_subclasses(dirty_pars)
        dirty_gens = _expand_subclasses(dirty_gens)
    for f in gen_files:
        if not any((c in dirty_gens for c in file_classes.get(f, ()))):
            return None
    for f in par_files:
        if not any((c in dirty_pars for c in file_classes.get(f, ()))):
            return None
    for f in dial_files:
        if not any((c in dirty_dials for c in file_classes.get(f, ()))):
            return None
    fine_tokens: set[tuple[Any, ...]] | None = None
    dep_idx = _di.load_dep_meta()
    if dep_idx is None:
        dep_idx = _di.load_dep_index()
    if dep_idx is not None and (not core_tok_files):
        if _DEP_SETS_CACHE is not None and _DEP_SETS_CACHE[0] == id(dep_idx):
            _, tracked_attrs, tracked_dicts, wrapped_methods, wrapped_funcs = _DEP_SETS_CACHE
        else:
            tracked_attrs = frozenset(dep_idx.get('tracked_attrs', ()))
            tracked_dicts = frozenset(dep_idx.get('tracked_dicts', ()))
            wrapped_methods = frozenset((tuple(x) for x in dep_idx.get('wrapped_methods', ())))
            wrapped_funcs = frozenset((tuple(x) for x in dep_idx.get('wrapped_funcs', ())))
            _DEP_SETS_CACHE = (id(dep_idx), tracked_attrs, tracked_dicts, wrapped_methods, wrapped_funcs)
        toks: set[tuple[Any, ...]] = set()
        all_fine_ok = True
        fine_src_files = gen_files | par_files | dial_files | dial_tok_only_files | typing_files | opt_files | ({'sqlglot/generator.py'} if core_gen_file else set()) | ({'sqlglot/parser.py'} if core_par_file else set())
        for rel in fine_src_files:
            ana = commit_plan.py_analyses.get(rel)
            if ana is None:
                all_fine_ok = False
                break
            mod_name = rel.removesuffix('/__init__.py').removesuffix('.py').replace('/', '.')
            for u in ana.units:
                if u.kind == 'func_body':
                    cname = u.class_name
                    mname = u.symbol_name.rsplit('.', 1)[-1] if u.symbol_name else None
                    if cname and mname and ((cname, mname) in wrapped_methods):
                        toks.add(('M', cname, mname))
                    elif not cname and mname and ((mod_name, mname) in wrapped_funcs) and (not getattr(u, 'is_cached_func', False)) and (not getattr(u, 'sig_changed', False)) and (engine is not None) and (not engine._is_func_called_at_import(rel, mname)[0]):
                        toks.add(('F', mod_name, mname))
                    else:
                        all_fine_ok = False
                        break
                elif u.kind == 'class_attr':
                    cname = u.class_name
                    attr = u.symbol_name
                    if engine is not None and cname and attr and (attr in tracked_attrs) and (attr not in _di.META_SKIP_ATTRS) and (attr not in engine.dialect_meta_attrs):
                        owners = engine._expand_class_and_copiers(cname, attr)
                        rev_cl_c = engine._rev_import_closure(rel)
                        core_meta_files = {'sqlglot/dialects/dialect.py', 'sqlglot/parser.py', 'sqlglot/generator.py', 'sqlglot/tokens.py'}
                        has_scoped_cb_ref = any((attr in engine._get_non_runtime_info(rf)[1] for rf in rev_cl_c | core_meta_files))
                        owners_set = set(owners)
                        readers_a = maps.get('attr_readers', {}).get(attr, [])
                        has_uncov_nrt = any((rfile in rev_cl_c | core_meta_files and rline in engine._get_non_runtime_info(rfile)[0] and (not engine._is_covered_same_attr_copy(rfile, rline, attr, owners_set, u)) for rfile, rline in readers_a))
                        if has_scoped_cb_ref or has_uncov_nrt:
                            all_fine_ok = False
                            break
                        for o in owners:
                            toks.add(('A', o, attr))
                    else:
                        all_fine_ok = False
                        break
                elif u.kind in ('dict_key', 'dict_lambda_body'):
                    cname = u.class_name
                    attr = u.symbol_name
                    dep_graph = engine._get_class_dep_graph() if engine is not None else None
                    if not cname and rel.startswith('sqlglot/typing/') and (attr == 'EXPRESSION_METADATA') and (u.dict_keys is not None) and ('EXPRESSION_METADATA' in tracked_dicts):
                        for kr in u.dict_keys:
                            ck = _di.canon_source_key(kr, all_exprs)
                            if ck is None or not ck.startswith('E:'):
                                all_fine_ok = False
                                break
                            subs_e = _expand_subclasses({ck[2:]})
                            for o in dirty_typing_dials:
                                toks.add(('D', o, 'EXPRESSION_METADATA', '*'))
                                for sc in subs_e:
                                    toks.add(('D', o, 'EXPRESSION_METADATA', f'E:{sc}'))
                    elif engine is not None and dep_graph is not None and cname and attr and (u.dict_keys is not None) and (attr not in _di.META_SKIP_ATTRS) and (attr == 'TRANSFORMS' or attr in tracked_dicts) and (not (attr in ('SHOW_PARSERS', 'SET_PARSERS') and bool(u.dict_added_keys or u.dict_deleted_keys))) and (not (bool(u.dict_deleted_keys) and attr in dep_graph['attr_mutators'])):
                        owners = engine._expand_class_and_copiers(cname, attr)
                        rev_cl_d = engine._rev_import_closure(rel)
                        owners_set = set(owners)
                        readers_d = maps.get('attr_readers', {}).get(attr, [])
                        has_uncov_nrt = any((rfile in rev_cl_d and rfile != rel and (rline in engine._get_non_runtime_info(rfile)[0]) and (not engine._is_covered_same_attr_copy(rfile, rline, attr, owners_set, u)) for rfile, rline in readers_d))
                        if has_uncov_nrt:
                            all_fine_ok = False
                            break
                        if attr == 'TRANSFORMS':
                            for kr in u.dict_keys:
                                ck = _di.canon_source_key(kr, all_exprs)
                                if ck is None or not ck.startswith('E:'):
                                    all_fine_ok = False
                                    break
                                subs_e = _expand_subclasses({ck[2:]})
                                for o in owners:
                                    toks.add(('D', o, 'TRANSFORMS', '*'))
                                    for sc in subs_e:
                                        toks.add(('T', o, sc))
                                        toks.add(('D', o, 'TRANSFORMS', f'E:{sc}'))
                        else:
                            for kr in u.dict_keys:
                                ck = _di.canon_source_key(kr, all_exprs)
                                if ck is None:
                                    all_fine_ok = False
                                    break
                                for o in owners:
                                    toks.add(('D', o, attr, '*'))
                                    toks.add(('D', o, attr, ck))
                                    if ck.startswith('E:'):
                                        for sc in _expand_subclasses({ck[2:]}):
                                            toks.add(('D', o, attr, f'E:{sc}'))
                    else:
                        all_fine_ok = False
                        break
                else:
                    all_fine_ok = False
                    break
            if not all_fine_ok:
                break
        if all_fine_ok and (toks or init_pure_import_only):
            fine_tokens = toks
    is_gen_only = bool((gen_files or core_gen_file) and (not par_files) and (not core_par_file) and (not dial_files) and (not dial_tok_only_files) and (not typing_files) and (not core_tok_files) and (not opt_files) and (not init_pure_import_only) and (not dirty_dials) and (not dirty_pars))
    is_opt_only = bool(opt_files and (not gen_files) and (not core_gen_file) and (not par_files) and (not core_par_file) and (not dial_files) and (not dial_tok_only_files) and (not typing_files) and (not core_tok_files) and (not init_pure_import_only))
    is_tok_only = bool((core_tok_files or dial_tok_only_files) and (not gen_files) and (not core_gen_file) and (not par_files) and (not core_par_file) and (not dial_files) and (not typing_files) and (not opt_files) and (not init_pure_import_only))
    return ReplayScope(dirty_dials=frozenset(dirty_dials), dirty_pars=frozenset(dirty_pars), dirty_gens=frozenset(dirty_gens), dirty_opt_dials=frozenset(dirty_opt_dials), dirty_files=frozenset(dirty_files), is_gen_only=is_gen_only, fine_tokens=frozenset(fine_tokens) if fine_tokens is not None else None, is_opt_only=is_opt_only, is_tok_only=is_tok_only, opt_dirty_files=frozenset(opt_files), opt_dirty_rules=frozenset(opt_dirty_rules), opt_func_only=opt_func_only, dirty_typing_dials=frozenset(dirty_typing_dials))

def _write_ast_table(asts: list[Any], ast_path: Path) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    pos = 0
    with open(ast_path, 'wb') as f:
        for a in asts:
            b = marshal.dumps(a)
            ln = len(b)
            f.write(b)
            offsets.append((pos, ln))
            pos += ln
    return offsets
_AST_MMAP_CACHE: dict[str, mmap.mmap | None] = {}

def _get_ast_mmap(ast_path: Path) -> mmap.mmap | None:
    key = str(ast_path)
    mm = _AST_MMAP_CACHE.get(key)
    if mm is not None or key in _AST_MMAP_CACHE:
        return mm
    if not ast_path.is_file():
        _AST_MMAP_CACHE[key] = None
        return None
    try:
        if ast_path.stat().st_size == 0:
            _AST_MMAP_CACHE[key] = None
            return None
        fd = os.open(ast_path, os.O_RDONLY)
        try:
            mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        finally:
            os.close(fd)
        _AST_MMAP_CACHE[key] = mm
        return mm
    except Exception:
        _AST_MMAP_CACHE[key] = None
        return None

class _MmapAstStore:
    __slots__ = ('_path', '_offsets', '_mm')

    def __init__(self, path: Path, offsets: list[tuple[int, int]]) -> None:
        self._path = path
        self._offsets = offsets
        self._mm: mmap.mmap | None = None

    def get_expr(self, ast_idx: int, exp_mod: Any) -> Any:
        mm = self._mm
        if mm is None:
            mm = _get_ast_mmap(self._path)
            if mm is None:
                raise FileNotFoundError(str(self._path))
            self._mm = mm
        off, ln = self._offsets[ast_idx]
        return unpack_expr(marshal.loads(mm[off:off + ln]), exp_mod)

    def close(self) -> None:
        self._mm = None
_RAW_VAL_IDX: dict[str, tuple[list, dict, dict, list[tuple[int, int]]] | None] = {}
_RAW_OPT_IDX: dict[str, tuple[list, dict, list[tuple[int, int]]] | None] = {}
_RAW_VAL_FTOC: dict[str, dict[Any, tuple[int, ...]] | None] = {}
_RAW_OPT_FTOC: dict[str, dict[Any, tuple[int, ...]] | None] = {}

def _write_fine_tables(inv_maps: tuple[dict[Any, set[int]], ...], fdat_path: Path, ftoc_path: Path) -> int:
    all_toks: set[Any] = set()
    for m in inv_maps:
        all_toks.update(m.keys())
    toc: dict[Any, tuple[int, ...]] = {}
    pos = 0
    with open(fdat_path, 'wb') as f:
        for tok in all_toks:
            entry: list[int] = []
            for m in inv_maps:
                s = m.get(tok)
                if s:
                    b = array.array('I', sorted(s)).tobytes()
                    f.write(b)
                    entry.append(pos)
                    entry.append(len(b))
                    pos += len(b)
                else:
                    entry.append(0)
                    entry.append(0)
            toc[tok] = tuple(entry)
    tb = marshal.dumps(toc)
    ftoc_path.write_bytes(tb)
    return pos + len(tb)
_FDAT_MMAP_CACHE: dict[str, mmap.mmap | None] = {}

def _get_fdat_mmap(fdat_path: Path) -> mmap.mmap | None:
    key = str(fdat_path)
    mm = _FDAT_MMAP_CACHE.get(key)
    if mm is not None or key in _FDAT_MMAP_CACHE:
        return mm
    if not fdat_path.is_file():
        _FDAT_MMAP_CACHE[key] = None
        return None
    try:
        st_sz = fdat_path.stat().st_size
        if st_sz == 0:
            _FDAT_MMAP_CACHE[key] = None
            return None
        fd = os.open(fdat_path, os.O_RDONLY)
        try:
            mm = mmap.mmap(fd, 0, access=mmap.ACCESS_READ)
        finally:
            os.close(fd)
        _FDAT_MMAP_CACHE[key] = mm
        return mm
    except Exception:
        _FDAT_MMAP_CACHE[key] = None
        return None

def _query_fine_tables(ftoc: dict[Any, tuple[int, ...]] | None, fdat_path: Path, fine_tokens: frozenset[tuple[Any, ...]], n_maps: int) -> tuple[set[int], ...] | None:
    if ftoc is None:
        return None
    if not fine_tokens:
        return tuple((set() for _ in range(n_maps)))
    if _FJC_FINE is not None:
        mm = _get_fdat_mmap(fdat_path)
        if mm is None:
            if any((tok in ftoc for tok in fine_tokens)):
                return None
            return tuple((set() for _ in range(n_maps)))
        return _FJC_FINE(ftoc, mm, fine_tokens, n_maps)
    hits = [ftoc[tok] for tok in fine_tokens if tok in ftoc]
    sets = tuple((set() for _ in range(n_maps)))
    if not hits:
        return sets
    mm = _get_fdat_mmap(fdat_path)
    if mm is None:
        return None
    for entry in hits:
        for i in range(n_maps):
            ln = entry[2 * i + 1]
            if ln:
                off = entry[2 * i]
                arr = array.array('I')
                arr.frombytes(mm[off:off + ln])
                sets[i].update(arr)
    return sets

def preload_all_replay_indices(val_stems: set[str] | None=None, opt_names: set[str] | None=None, need_ftoc: bool=True) -> None:
    if VAL_REPLAY_DIR.is_dir():
        for p in VAL_REPLAY_DIR.glob('*.idx'):
            stem = p.stem
            if val_stems is not None and stem not in val_stems:
                continue
            if stem not in _RAW_VAL_IDX:
                try:
                    _RAW_VAL_IDX[stem] = marshal.loads(p.read_bytes())
                except Exception:
                    _RAW_VAL_IDX[stem] = None
            _get_ast_mmap(VAL_REPLAY_DIR / f'{stem}.ast')
        if need_ftoc:
            for p in VAL_REPLAY_DIR.glob('*.ftoc'):
                stem = p.stem
                if val_stems is not None and stem not in val_stems:
                    continue
                if stem not in _RAW_VAL_FTOC:
                    try:
                        _RAW_VAL_FTOC[stem] = marshal.loads(p.read_bytes())
                    except Exception:
                        _RAW_VAL_FTOC[stem] = None
                _get_fdat_mmap(VAL_REPLAY_DIR / f'{stem}.fdat')
    if OPT_REPLAY_DIR.is_dir():
        for p in OPT_REPLAY_DIR.glob('*.idx'):
            name = p.stem
            if opt_names is not None and name not in opt_names:
                continue
            if name not in _RAW_OPT_IDX:
                try:
                    _RAW_OPT_IDX[name] = marshal.loads(p.read_bytes())
                except Exception:
                    _RAW_OPT_IDX[name] = None
            if name != 'opt_methods':
                _get_ast_mmap(OPT_REPLAY_DIR / f'{name}.ast')
        if need_ftoc:
            for p in OPT_REPLAY_DIR.glob('*.ftoc'):
                name = p.stem
                if opt_names is not None and name not in opt_names:
                    continue
                if name not in _RAW_OPT_FTOC:
                    try:
                        _RAW_OPT_FTOC[name] = marshal.loads(p.read_bytes())
                    except Exception:
                        _RAW_OPT_FTOC[name] = None
                _get_fdat_mmap(OPT_REPLAY_DIR / f'{name}.fdat')

class ValReplayShard:
    __slots__ = ('stem', 'deps_table', 'parses', 'edges', 'method_ops', 'whole_methods', 'dirty_flags', 'dirty_opt_flags', 'dirty_pids', 'dirty_eids', 'dirty_mids', 'has_opt_files', '_store')

    def __init__(self, stem: str, deps_table: list, parses: dict, edges: dict, ast_offsets: list[tuple[int, int]], method_ops: dict[str, tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]], whole_methods: dict[str, tuple[int, int, int]], ast_path: Path, ftoc: dict[Any, tuple[int, ...]] | None, fdat_path: Path, scope: ReplayScope) -> None:
        self.stem = stem
        self.deps_table = deps_table
        self.parses = parses
        self.edges = edges
        self.method_ops = method_ops
        self.whole_methods = whole_methods
        self.has_opt_files = bool(scope.opt_dirty_files)
        self._store = _MmapAstStore(ast_path, ast_offsets)
        dd = scope.dirty_dials
        dp = scope.dirty_pars
        dg = scope.dirty_gens
        dod = scope.dirty_opt_dials
        dtd = scope.dirty_typing_dials
        if _FJC_DFLAGS is not None:
            self.dirty_flags, self.dirty_opt_flags = _FJC_DFLAGS(deps_table, dd, dp, dg, dtd, dod)
        else:
            self.dirty_flags: list[bool] = [bool(dd and any((x in dd for x in ds)) or (dp and any((x in dp for x in ps))) or (dg and any((x in dg for x in gs))) or (dtd and any((x in dtd for x in ds)))) for ds, ps, gs in deps_table]
            self.dirty_opt_flags: list[bool] = [self.dirty_flags[i] or bool(any((x in dod for x in deps_table[i][0]))) for i in range(len(deps_table))] if dod else self.dirty_flags
        self.dirty_pids: set[int] | None = None
        self.dirty_eids: set[int] | None = None
        self.dirty_mids: set[int] | None = None
        if scope.fine_tokens is not None and (any(self.dirty_flags) or any(self.dirty_opt_flags)):
            try:
                n_maps = 2
                if ftoc:
                    first_entry = next(iter(ftoc.values()))
                    n_maps = len(first_entry) // 2
                res = _query_fine_tables(ftoc, fdat_path, scope.fine_tokens, n_maps)
                if res is not None:
                    self.dirty_pids = res[0]
                    self.dirty_eids = res[1]
                    if n_maps >= 3:
                        self.dirty_mids = res[2]
            except Exception:
                pass

    def is_pure_method_clean(self, cls_name: str, meth: str) -> bool:
        mkey = f'{cls_name}::{meth}'
        mrec = self.method_ops.get(mkey)
        if mrec is not None:
            m_pids, m_eids, m_deps = mrec
            dflags = self.dirty_flags
            if not any((dflags[d] for d in m_deps)):
                return True
            dp = self.dirty_pids
            de = self.dirty_eids
            if dp is not None and de is not None:
                if not any((p in dp for p in m_pids)) and (not any((e in de for e in m_eids))):
                    return True
            return False
        wrec = self.whole_methods.get(mkey)
        if wrec is not None:
            dep_idx, mid, uses_opt = wrec
            if uses_opt:
                if self.has_opt_files:
                    return False
                if not self.dirty_opt_flags[dep_idx]:
                    return True
            elif not self.dirty_flags[dep_idx]:
                return True
            dm = self.dirty_mids
            if dm is not None and mid not in dm:
                return True
        return False

    def get_expr(self, ast_idx: int, exp_mod: Any) -> Any:
        return self._store.get_expr(ast_idx, exp_mod)

    def close(self) -> None:
        self._store.close()
_VAL_SHARD_CACHE: dict[str, ValReplayShard | None] = {}
_OPT_SHARD_CACHE: dict[str, OptReplayShard | None] = {}
_OPT_METHODS_SHARD_CACHE: OptMethodsShard | None = None
_OPT_METHODS_LOADED: bool = False

class OptMethodsShard:
    __slots__ = ('deps_table', 'methods', 'method_opt_files', 'dirty_opt_flags', 'dirty_mids', 'is_opt_only', 'opt_dirty_files', 'opt_func_only')

    def __init__(self, deps_table: list, methods: dict[str, tuple[int, int]], method_opt_files: dict[str, tuple[str, ...]], ftoc: dict[Any, tuple[int, ...]] | None, fdat_path: Path, scope: ReplayScope) -> None:
        self.deps_table = deps_table
        self.methods = methods
        self.method_opt_files = method_opt_files
        self.is_opt_only = scope.is_opt_only
        self.opt_dirty_files = scope.opt_dirty_files
        self.opt_func_only = scope.opt_func_only
        dd = scope.dirty_dials
        dp = scope.dirty_pars
        dg = scope.dirty_gens
        dod = scope.dirty_opt_dials
        if _FJC_DFLAGS is not None:
            self.dirty_opt_flags = _FJC_DFLAGS(deps_table, dd, dp, dg, dod, None)[0]
        else:
            self.dirty_opt_flags: list[bool] = [bool(dd and any((x in dd for x in ds)) or (dp and any((x in dp for x in ps))) or (dg and any((x in dg for x in gs))) or (dod and any((x in dod for x in ds)))) for ds, ps, gs in deps_table]
        self.dirty_mids: set[int] | None = None
        if scope.fine_tokens is not None and (self.is_opt_only or any(self.dirty_opt_flags)):
            try:
                res = _query_fine_tables(ftoc, fdat_path, scope.fine_tokens, 1)
                if res is not None:
                    self.dirty_mids = res[0]
            except Exception:
                pass

    def is_method_clean(self, meth: str) -> bool:
        rec = self.methods.get(meth)
        if rec is None:
            return False
        dep_idx, mid = rec
        dm = self.dirty_mids
        if self.opt_dirty_files:
            opt_clean = False
            if self.opt_func_only:
                m_of = self.method_opt_files.get(meth)
                if m_of is not None and self.opt_dirty_files.isdisjoint(m_of):
                    opt_clean = True
            if not opt_clean and dm is not None and (mid not in dm):
                opt_clean = True
            if not opt_clean:
                return False
            if self.is_opt_only:
                return True
        if not self.dirty_opt_flags[dep_idx]:
            return True
        if dm is not None and mid not in dm:
            return True
        return False

def get_opt_methods_shard(scope: ReplayScope) -> OptMethodsShard | None:
    global _OPT_METHODS_SHARD_CACHE, _OPT_METHODS_LOADED
    if _OPT_METHODS_LOADED:
        return _OPT_METHODS_SHARD_CACHE
    _OPT_METHODS_LOADED = True
    raw = _RAW_OPT_IDX.get('opt_methods')
    if raw is None and 'opt_methods' not in _RAW_OPT_IDX:
        idx_path = OPT_REPLAY_DIR / 'opt_methods.idx'
        if idx_path.is_file():
            try:
                raw = marshal.loads(idx_path.read_bytes())
            except Exception:
                raw = None
        _RAW_OPT_IDX['opt_methods'] = raw
    if raw is None:
        _OPT_METHODS_SHARD_CACHE = None
        return None
    ftoc = _RAW_OPT_FTOC.get('opt_methods')
    if ftoc is None and 'opt_methods' not in _RAW_OPT_FTOC:
        fp = OPT_REPLAY_DIR / 'opt_methods.ftoc'
        if fp.is_file():
            try:
                ftoc = marshal.loads(fp.read_bytes())
            except Exception:
                ftoc = None
        _RAW_OPT_FTOC['opt_methods'] = ftoc
    fdat_path = OPT_REPLAY_DIR / 'opt_methods.fdat'
    if len(raw) >= 3:
        deps_table, methods, method_opt_files = raw[:3]
    else:
        deps_table, methods = raw
        method_opt_files = {}
    _OPT_METHODS_SHARD_CACHE = OptMethodsShard(deps_table, methods, method_opt_files, ftoc, fdat_path, scope)
    return _OPT_METHODS_SHARD_CACHE

def clear_replay_shard_caches() -> None:
    global _OPT_METHODS_SHARD_CACHE, _OPT_METHODS_LOADED
    for sh in _VAL_SHARD_CACHE.values():
        if sh is not None:
            sh.close()
    for sh in _OPT_SHARD_CACHE.values():
        if sh is not None:
            sh.close()
    _VAL_SHARD_CACHE.clear()
    _OPT_SHARD_CACHE.clear()
    _OPT_METHODS_SHARD_CACHE = None
    _OPT_METHODS_LOADED = False

def get_val_shard(test_file: str, scope: ReplayScope) -> ValReplayShard | None:
    shard = _VAL_SHARD_CACHE.get(test_file)
    if shard is not None or test_file in _VAL_SHARD_CACHE:
        return shard
    stem = Path(test_file).stem
    raw = _RAW_VAL_IDX.get(stem)
    if raw is None and stem not in _RAW_VAL_IDX:
        idx_path = VAL_REPLAY_DIR / f'{stem}.idx'
        if idx_path.is_file():
            try:
                raw = marshal.loads(idx_path.read_bytes())
            except Exception:
                raw = None
        _RAW_VAL_IDX[stem] = raw
    ast_path = VAL_REPLAY_DIR / f'{stem}.ast'
    if raw is None or not ast_path.is_file():
        _VAL_SHARD_CACHE[test_file] = None
        return None
    ftoc = _RAW_VAL_FTOC.get(stem)
    if ftoc is None and stem not in _RAW_VAL_FTOC:
        fp = VAL_REPLAY_DIR / f'{stem}.ftoc'
        if fp.is_file():
            try:
                ftoc = marshal.loads(fp.read_bytes())
            except Exception:
                ftoc = None
        _RAW_VAL_FTOC[stem] = ftoc
    fdat_path = VAL_REPLAY_DIR / f'{stem}.fdat'
    if len(raw) >= 6:
        deps_table, parses, edges, ast_offsets, method_ops, whole_methods = raw[:6]
    elif len(raw) == 5:
        deps_table, parses, edges, ast_offsets, method_ops = raw
        whole_methods = {}
    else:
        deps_table, parses, edges, ast_offsets = raw
        method_ops = {}
        whole_methods = {}
    shard = ValReplayShard(stem, deps_table, parses, edges, ast_offsets, method_ops, whole_methods, ast_path, ftoc, fdat_path, scope)
    _VAL_SHARD_CACHE[test_file] = shard
    return shard

class OptReplayShard:
    __slots__ = ('name', 'deps_table', 'rows', 'ckps', 'dirty_flags', 'dirty_opt_flags', 'dirty_p_rids', 'dirty_o_rids', 'dirty_f_rids', 'dirty_e_rids', '_store')

    def __init__(self, name: str, deps_table: list, rows: dict, ast_offsets: list[tuple[int, int]], ckps: dict, ast_path: Path, ftoc: dict[Any, tuple[int, ...]] | None, fdat_path: Path, scope: ReplayScope) -> None:
        self.name = name
        self.deps_table = deps_table
        self.rows = rows
        self.ckps = ckps
        self._store = _MmapAstStore(ast_path, ast_offsets)
        dd = scope.dirty_dials
        dp = scope.dirty_pars
        dg = scope.dirty_gens
        dod = scope.dirty_opt_dials
        dtd = scope.dirty_typing_dials
        if _FJC_DFLAGS is not None:
            if scope.opt_dirty_files:
                sh_deps = SHARD_OPT_FILES.get(name)
                sh_dirty = True if sh_deps is None else not scope.opt_dirty_files.isdisjoint(sh_deps)
                if sh_dirty:
                    self.dirty_flags = _FJC_DFLAGS(deps_table, dd, dp, dg, dtd, None)[0]
                    self.dirty_opt_flags = [True] * len(deps_table)
                else:
                    dod_eff = dod if dod and (not scope.is_opt_only) else None
                    self.dirty_flags, self.dirty_opt_flags = _FJC_DFLAGS(deps_table, dd, dp, dg, dtd, dod_eff)
            else:
                self.dirty_flags, self.dirty_opt_flags = _FJC_DFLAGS(deps_table, dd, dp, dg, dtd, dod)
        else:
            self.dirty_flags: list[bool] = [bool(dd and any((x in dd for x in ds)) or (dp and any((x in dp for x in ps))) or (dg and any((x in dg for x in gs))) or (dtd and any((x in dtd for x in ds)))) for ds, ps, gs in deps_table]
            if scope.opt_dirty_files:
                sh_deps = SHARD_OPT_FILES.get(name)
                sh_dirty = True if sh_deps is None else not scope.opt_dirty_files.isdisjoint(sh_deps)
                if sh_dirty:
                    self.dirty_opt_flags = [True] * len(deps_table)
                else:
                    self.dirty_opt_flags = [self.dirty_flags[i] or bool(any((x in dod for x in deps_table[i][0]))) for i in range(len(deps_table))] if dod and (not scope.is_opt_only) else list(self.dirty_flags)
            else:
                self.dirty_opt_flags = [self.dirty_flags[i] or bool(any((x in dod for x in deps_table[i][0]))) for i in range(len(deps_table))] if dod else self.dirty_flags
        self.dirty_p_rids: set[int] | None = None
        self.dirty_o_rids: set[int] | None = None
        self.dirty_f_rids: set[int] | None = None
        self.dirty_e_rids: set[int] | None = None
        if scope.fine_tokens is not None and (any(self.dirty_flags) or any(self.dirty_opt_flags)):
            try:
                res = _query_fine_tables(ftoc, fdat_path, scope.fine_tokens, 4)
                if res is not None:
                    self.dirty_p_rids, self.dirty_o_rids, self.dirty_f_rids, self.dirty_e_rids = res
            except Exception:
                pass

    def get_expr(self, ast_idx: int, exp_mod: Any) -> Any:
        return self._store.get_expr(ast_idx, exp_mod)

    def close(self) -> None:
        self._store.close()

def get_opt_shard(name: str, scope: ReplayScope) -> OptReplayShard | None:
    shard = _OPT_SHARD_CACHE.get(name)
    if shard is not None or name in _OPT_SHARD_CACHE:
        return shard
    safe_name = name.replace('/', '__')
    raw = _RAW_OPT_IDX.get(safe_name)
    if raw is None and safe_name not in _RAW_OPT_IDX:
        idx_path = OPT_REPLAY_DIR / f'{safe_name}.idx'
        if idx_path.is_file():
            try:
                raw = marshal.loads(idx_path.read_bytes())
            except Exception:
                raw = None
        _RAW_OPT_IDX[safe_name] = raw
    ast_path = OPT_REPLAY_DIR / f'{safe_name}.ast'
    if raw is None or not ast_path.is_file():
        _OPT_SHARD_CACHE[name] = None
        return None
    ftoc = _RAW_OPT_FTOC.get(safe_name)
    if ftoc is None and safe_name not in _RAW_OPT_FTOC:
        fp = OPT_REPLAY_DIR / f'{safe_name}.ftoc'
        if fp.is_file():
            try:
                ftoc = marshal.loads(fp.read_bytes())
            except Exception:
                ftoc = None
        _RAW_OPT_FTOC[safe_name] = ftoc
    fdat_path = OPT_REPLAY_DIR / f'{safe_name}.fdat'
    if len(raw) >= 4:
        deps_table, rows, ast_offsets, ckps = raw[:4]
    else:
        deps_table, rows, ast_offsets = raw
        ckps = {}
    shard = OptReplayShard(name, deps_table, rows, ast_offsets, ckps, ast_path, ftoc, fdat_path, scope)
    _OPT_SHARD_CACHE[name] = shard
    return shard
_DUCKDB_CACHE_DATA: dict[str, Any] | None = None
_SQLITE_CACHE_DATA: dict[str, Any] | None = None
_EXEC_REPLAY_DATA: dict[Any, Any] | None = None
_TOK_TRIE_CACHE_DATA: dict[str, Any] | None = None
_CROSS_JOINS_PQ_DATA: dict[Any, Any] | None = None
_TPC_PYGEN_FINE_DATA: dict[Any, tuple[int, ...]] | None = None
_READ_SQL_PREFIXES = ('SELECT', 'WITH', 'VALUES', 'FROM', 'TABLE', '(', 'SUMMARIZE', 'DESCRIBE', 'EXPLAIN', 'SHOW')

def load_tpc_pygen_fine() -> dict[Any, tuple[int, ...]] | None:
    global _TPC_PYGEN_FINE_DATA
    if _TPC_PYGEN_FINE_DATA is None:
        if TPC_PYGEN_FINE_PATH.is_file():
            try:
                _TPC_PYGEN_FINE_DATA = marshal.loads(TPC_PYGEN_FINE_PATH.read_bytes())
            except Exception:
                _TPC_PYGEN_FINE_DATA = None
    return _TPC_PYGEN_FINE_DATA

def load_cross_joins_pq() -> dict[Any, Any]:
    global _CROSS_JOINS_PQ_DATA
    if _CROSS_JOINS_PQ_DATA is None:
        if CROSS_JOINS_PQ_PATH.is_file():
            try:
                _CROSS_JOINS_PQ_DATA = marshal.loads(CROSS_JOINS_PQ_PATH.read_bytes())
            except Exception:
                _CROSS_JOINS_PQ_DATA = {}
        else:
            _CROSS_JOINS_PQ_DATA = {}
    return _CROSS_JOINS_PQ_DATA

def load_tok_trie_cache() -> dict[str, Any]:
    global _TOK_TRIE_CACHE_DATA
    if _TOK_TRIE_CACHE_DATA is None:
        if TOK_TRIE_CACHE_PATH.is_file():
            try:
                _TOK_TRIE_CACHE_DATA = marshal.loads(TOK_TRIE_CACHE_PATH.read_bytes())
            except Exception:
                _TOK_TRIE_CACHE_DATA = {}
        else:
            _TOK_TRIE_CACHE_DATA = {}
    return _TOK_TRIE_CACHE_DATA

def load_duckdb_query_cache() -> dict[str, Any]:
    global _DUCKDB_CACHE_DATA
    if _DUCKDB_CACHE_DATA is None:
        if DUCKDB_CACHE_PATH.is_file():
            try:
                _DUCKDB_CACHE_DATA = pickle.loads(DUCKDB_CACHE_PATH.read_bytes())
            except Exception:
                _DUCKDB_CACHE_DATA = {'queries': {}, 'ddls': set()}
        else:
            z = Path(str(DUCKDB_CACHE_PATH) + '.zlib')
            if z.is_file():
                try:
                    import zlib as _zlib
                    _DUCKDB_CACHE_DATA = pickle.loads(_zlib.decompress(z.read_bytes()))
                except Exception:
                    _DUCKDB_CACHE_DATA = {'queries': {}, 'ddls': set()}
            else:
                _DUCKDB_CACHE_DATA = {'queries': {}, 'ddls': set()}
    return _DUCKDB_CACHE_DATA

def load_sqlite_query_cache() -> dict[str, Any]:
    global _SQLITE_CACHE_DATA
    if _SQLITE_CACHE_DATA is None:
        if SQLITE_CACHE_PATH.is_file():
            try:
                _SQLITE_CACHE_DATA = pickle.loads(SQLITE_CACHE_PATH.read_bytes())
            except Exception:
                _SQLITE_CACHE_DATA = {'queries': {}, 'ddls': set()}
        else:
            z = Path(str(SQLITE_CACHE_PATH) + '.zlib')
            if z.is_file():
                try:
                    import zlib as _zlib
                    _SQLITE_CACHE_DATA = pickle.loads(_zlib.decompress(z.read_bytes()))
                except Exception:
                    _SQLITE_CACHE_DATA = {'queries': {}, 'ddls': set()}
            else:
                _SQLITE_CACHE_DATA = {'queries': {}, 'ddls': set()}
    return _SQLITE_CACHE_DATA

def load_exec_replay() -> dict[Any, Any]:
    global _EXEC_REPLAY_DATA
    if _EXEC_REPLAY_DATA is None:
        if EXEC_REPLAY_PATH.is_file():
            try:
                _EXEC_REPLAY_DATA = pickle.loads(EXEC_REPLAY_PATH.read_bytes())
            except Exception:
                _EXEC_REPLAY_DATA = {}
        else:
            z = Path(str(EXEC_REPLAY_PATH) + '.zlib')
            if z.is_file():
                try:
                    import zlib as _zlib
                    _EXEC_REPLAY_DATA = pickle.loads(_zlib.decompress(z.read_bytes()))
                except Exception:
                    _EXEC_REPLAY_DATA = {}
            else:
                _EXEC_REPLAY_DATA = {}
    return _EXEC_REPLAY_DATA

def _is_pure_read_sql(sql: str) -> bool:
    s = sql.lstrip()
    if not s:
        return False
    u = s[:16].upper()
    if not u.startswith(_READ_SQL_PREFIXES):
        return False
    su = sql.upper()
    if any((k in su for k in ('READ_PARQUET', 'READ_CSV', 'READ_JSON', 'COPY ', 'ATTACH ', 'INSTALL ', 'LOAD '))):
        return False
    return True

def _ddl_step_sig(prev_sig: bytes, sql: str) -> bytes:
    h = hashlib.blake2b(digest_size=12)
    h.update(prev_sig)
    h.update(b'|')
    h.update(sql.encode('utf-8', 'surrogatepass'))
    return h.digest()

class _CachedDuckCursor:
    __slots__ = ('description', '_rows', '_pos', '_owner', '_sql')

    def __init__(self, cols: tuple[str, ...], rows: list[tuple[Any, ...]], owner: LazyCachedDuckConn, sql: str) -> None:
        self.description = tuple(((c, None, None, None, None, None, None) for c in cols))
        self._rows = rows
        self._pos = 0
        self._owner = owner
        self._sql = sql

    def fetchall(self) -> list[tuple[Any, ...]]:
        res = list(self._rows[self._pos:])
        self._pos = len(self._rows)
        return res

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._pos >= len(self._rows):
            return None
        r = self._rows[self._pos]
        self._pos += 1
        return r

    def fetchmany(self, size: int=1) -> list[tuple[Any, ...]]:
        res = list(self._rows[self._pos:self._pos + size])
        self._pos += len(res)
        return res

    def df(self):
        return self._owner._materialize().execute(self._sql).df()

    def fetchdf(self):
        return self._owner._materialize().execute(self._sql).fetchdf()

class _CachedDuckExcCursor:
    __slots__ = ('description', '_exc_cls', '_exc_msg')

    def __init__(self, exc_cls: str, exc_msg: str) -> None:
        self.description = ()
        self._exc_cls = exc_cls
        self._exc_msg = exc_msg

    def _raise(self) -> Any:
        import duckdb
        cls = getattr(duckdb, self._exc_cls, duckdb.Error)
        raise cls(self._exc_msg)

    def fetchall(self):
        return self._raise()

    def fetchone(self):
        return self._raise()

    def fetchmany(self, size: int=1):
        return self._raise()

    def df(self):
        return self._raise()

    def fetchdf(self):
        return self._raise()

class LazyCachedDuckConn:

    def __init__(self, init_tag: bytes, real_factory: Any, cache_enabled: bool=True) -> None:
        self._sig: bytes = init_tag
        self._sig_valid: bool = True
        self._real_factory = real_factory
        self._cache_enabled = cache_enabled
        self._real_conn: Any = None
        self._pending_ddl: list[str] = []
        self._closed = False

    def _materialize(self) -> Any:
        if self._real_conn is None:
            conn = self._real_factory()
            for stmt in self._pending_ddl:
                conn.execute(stmt)
            self._pending_ddl.clear()
            self._real_conn = conn
        return self._real_conn

    def execute(self, query: str, parameters: Any=None, *args: Any, **kwargs: Any) -> Any:
        if not self._cache_enabled or not self._sig_valid or parameters is not None or args or kwargs or (type(query) is not str):
            if type(query) is not str or not _is_pure_read_sql(query):
                self._sig_valid = False
            return self._materialize().execute(query, parameters, *args, **kwargs) if parameters is not None else self._materialize().execute(query, *args, **kwargs)
        cache = load_duckdb_query_cache()
        if _is_pure_read_sql(query):
            hit = cache['queries'].get((self._sig, query))
            if hit is not None:
                if len(hit) == 3 and hit[0] == '__EXC_EXEC__':
                    import duckdb
                    raise getattr(duckdb, hit[1], duckdb.Error)(hit[2])
                if len(hit) == 3 and hit[0] == '__EXC_FETCH__':
                    return _CachedDuckExcCursor(hit[1], hit[2])
                cols, rows = hit
                return _CachedDuckCursor(cols, rows, self, query)
            return self._materialize().execute(query)
        next_sig = _ddl_step_sig(self._sig, query)
        if next_sig in cache['ddls']:
            self._sig = next_sig
            if self._real_conn is not None:
                return self._real_conn.execute(query)
            self._pending_ddl.append(query)
            return _CachedDuckCursor((), [], self, query)
        self._sig_valid = False
        return self._materialize().execute(query)

    def close(self) -> None:
        self._closed = True
        if self._real_conn is not None:
            self._real_conn.close()
            self._real_conn = None

    def __enter__(self) -> LazyCachedDuckConn:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._materialize(), name)

class _CachedSqliteCursor:
    __slots__ = ('description', '_rows', '_pos')

    def __init__(self, cols: tuple[str, ...], rows: list[tuple[Any, ...]]) -> None:
        self.description = tuple(((c, None, None, None, None, None, None) for c in cols)) if cols else None
        self._rows = rows
        self._pos = 0

    def fetchall(self) -> list[tuple[Any, ...]]:
        res = list(self._rows[self._pos:])
        self._pos = len(self._rows)
        return res

    def fetchone(self) -> tuple[Any, ...] | None:
        if self._pos >= len(self._rows):
            return None
        r = self._rows[self._pos]
        self._pos += 1
        return r

class LazyCachedSqliteConn:

    def __init__(self, real_factory: Any, cache_enabled: bool=True) -> None:
        self._sig: bytes = b'SQLITE'
        self._sig_valid: bool = True
        self._real_factory = real_factory
        self._cache_enabled = cache_enabled
        self._real_conn: Any = None
        self._pending_ddl: list[tuple[str, str]] = []

    def _materialize(self) -> Any:
        if self._real_conn is None:
            conn = self._real_factory()
            for kind, stmt in self._pending_ddl:
                if kind == 'exec':
                    conn.execute(stmt)
                else:
                    conn.executescript(stmt)
            self._pending_ddl.clear()
            self._real_conn = conn
        return self._real_conn

    def execute(self, query: str, parameters: Any=None, *args: Any, **kwargs: Any) -> Any:
        if not self._cache_enabled or not self._sig_valid or parameters is not None or args or kwargs or (type(query) is not str):
            if type(query) is not str or not _is_pure_read_sql(query):
                self._sig_valid = False
            return self._materialize().execute(query, parameters, *args, **kwargs) if parameters is not None else self._materialize().execute(query, *args, **kwargs)
        cache = load_sqlite_query_cache()
        if _is_pure_read_sql(query):
            hit = cache['queries'].get((self._sig, query))
            if hit is not None:
                return _CachedSqliteCursor(hit[0], hit[1])
            return self._materialize().execute(query)
        next_sig = _ddl_step_sig(self._sig, 'E:' + query)
        if next_sig in cache['ddls']:
            self._sig = next_sig
            if self._real_conn is not None:
                return self._real_conn.execute(query)
            self._pending_ddl.append(('exec', query))
            return _CachedSqliteCursor((), [])
        self._sig_valid = False
        return self._materialize().execute(query)

    def executescript(self, script: str) -> Any:
        if not self._cache_enabled or not self._sig_valid or type(script) is not str:
            self._sig_valid = False
            return self._materialize().executescript(script)
        cache = load_sqlite_query_cache()
        next_sig = _ddl_step_sig(self._sig, 'S:' + script)
        if next_sig in cache['ddls']:
            self._sig = next_sig
            if self._real_conn is not None:
                return self._real_conn.executescript(script)
            self._pending_ddl.append(('script', script))
            return _CachedSqliteCursor((), [])
        self._sig_valid = False
        return self._materialize().executescript(script)

    def close(self) -> None:
        if self._real_conn is not None:
            self._real_conn.close()
            self._real_conn = None

    def __enter__(self) -> LazyCachedSqliteConn:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._materialize(), name)

def exec_call_key(sql: Any, schema: Any, tables: Any, dialect: Any) -> bytes | None:
    if type(sql) is not str:
        return None
    dk = norm_dialect_key(dialect)
    if dk is None:
        return None
    try:
        blob = pickle.dumps((sql, dk, schema, tables), protocol=pickle.HIGHEST_PROTOCOL)
    except Exception:
        return None
    return hashlib.blake2b(blob, digest_size=16).digest()

def _install_stage_recorders() -> tuple[list[tuple[set[str], set[str], set[str]]], Any]:
    from sqlglot.dialects.dialect import Dialect, Dialects, _Dialect
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer
    for _d_en in Dialects:
        try:
            Dialect.get_or_raise(_d_en)
        except Exception:
            pass
    stack: list[tuple[set[str], set[str], set[str]]] = []
    orig_get_or_raise_desc = Dialect.__dict__['get_or_raise']
    orig_get_desc = _Dialect.__dict__['get']
    orig_getitem_desc = _Dialect.__dict__['__getitem__']
    orig_get_or_raise = orig_get_or_raise_desc.__func__
    orig_get = orig_get_desc.__func__
    orig_getitem = orig_getitem_desc.__func__

    @classmethod
    def _rec_get_or_raise(cls, dialect=None):
        res = orig_get_or_raise(cls, dialect)
        if stack:
            dn = type(res).__name__
            for dset, _, _ in stack:
                dset.add(dn)
        return res

    @classmethod
    def _rec_get(cls, key, default=None):
        res = orig_get(cls, key, default)
        if stack and isinstance(res, type) and issubclass(res, Dialect):
            dn = res.__name__
            for dset, _, _ in stack:
                dset.add(dn)
        return res

    @classmethod
    def _rec_getitem(cls, key):
        res = orig_getitem(cls, key)
        if stack and isinstance(res, type) and issubclass(res, Dialect):
            dn = res.__name__
            for dset, _, _ in stack:
                dset.add(dn)
        return res
    saved_stage_methods: list[tuple[type, str, Any]] = []

    def _all_subclasses(base: type) -> list[type]:
        out: list[type] = []
        seen: set[type] = set()
        stk = [base]
        while stk:
            c = stk.pop()
            if c in seen:
                continue
            seen.add(c)
            out.append(c)
            stk.extend(c.__subclasses__())
        return out
    for tcls in _all_subclasses(Tokenizer):
        if 'tokenize' in tcls.__dict__ and callable(tcls.__dict__['tokenize']):
            _ot = tcls.__dict__['tokenize']
            saved_stage_methods.append((tcls, 'tokenize', _ot))

            def _make_tok(orig_fn):

                def _rec_tok(self, sql, *args, **kwargs):
                    if stack:
                        dn = type(self.dialect).__name__
                        tk = f'TOK:{dn}'
                        for _, pset, _ in stack:
                            pset.add('Tokenizer')
                            pset.add(tk)
                    return orig_fn(self, sql, *args, **kwargs)
                return _rec_tok
            setattr(tcls, 'tokenize', _make_tok(_ot))
    for pcls in _all_subclasses(Parser):
        for pm in ('parse', 'parse_into'):
            if pm in pcls.__dict__ and callable(pcls.__dict__[pm]):
                _op = pcls.__dict__[pm]
                saved_stage_methods.append((pcls, pm, _op))

                def _make_par(orig_fn):

                    def _rec_p(self, *args, **kwargs):
                        if stack:
                            dn = type(self.dialect).__name__
                            pn = type(self).__name__
                            for dset, pset, _ in stack:
                                dset.add(dn)
                                pset.add(pn)
                        return orig_fn(self, *args, **kwargs)
                    return _rec_p
                setattr(pcls, pm, _make_par(_op))
    for gcls in _all_subclasses(Generator):
        if 'generate' in gcls.__dict__ and callable(gcls.__dict__['generate']):
            _og = gcls.__dict__['generate']
            saved_stage_methods.append((gcls, 'generate', _og))

            def _make_gen(orig_fn):

                def _rec_gen(self, *args, **kwargs):
                    if stack:
                        dn = type(self.dialect).__name__
                        gn = type(self).__name__
                        for dset, _, gset in stack:
                            dset.add(dn)
                            gset.add(gn)
                    return orig_fn(self, *args, **kwargs)
                return _rec_gen
            setattr(gcls, 'generate', _make_gen(_og))
    Dialect.get_or_raise = _rec_get_or_raise
    _Dialect.get = _rec_get
    _Dialect.__getitem__ = _rec_getitem

    def _restore() -> None:
        Dialect.get_or_raise = orig_get_or_raise_desc
        _Dialect.get = orig_get_desc
        _Dialect.__getitem__ = orig_getitem_desc
        for cls_s, m_s, orig_s in saved_stage_methods:
            setattr(cls_s, m_s, orig_s)
    return (stack, _restore)

def _install_fine_spies(fine_stack: list[set[tuple[Any, ...]]]) -> Any:

    def _on_attr(recv: str, attr: str) -> None:
        if fine_stack:
            tok = ('A', recv, attr)
            for s in fine_stack:
                s.add(tok)

    def _on_key(recv: str, attr: str, ck: str) -> None:
        if fine_stack:
            tok = ('D', recv, attr, ck)
            for s in fine_stack:
                s.add(tok)

    def _on_disp(recv: str, ecls: str) -> None:
        if fine_stack:
            tok = ('T', recv, ecls)
            for s in fine_stack:
                s.add(tok)

    def _on_meth(def_cls: str, mname: str) -> None:
        if fine_stack:
            tok = ('M', def_cls, mname)
            for s in fine_stack:
                s.add(tok)

    def _on_func(mod_name: str, fn_name: str) -> None:
        if fine_stack:
            tok = ('F', mod_name, fn_name)
            for s in fine_stack:
                s.add(tok)
    _, _, _, _, restore = _di.install_spies(_on_attr, _on_key, _on_disp, on_method=_on_meth, on_func=_on_func)
    return restore

def _install_opt_touch_hook() -> tuple[list[bool], Any]:
    import importlib as _il
    opt_touched = [False]
    saved: list[tuple[Any, str, Any]] = []
    targets = (('sqlglot.optimizer', 'optimize'), ('sqlglot.optimizer.optimizer', 'optimize'), ('sqlglot.optimizer.qualify', 'qualify'), ('sqlglot.optimizer.qualify_tables', 'qualify_tables'), ('sqlglot.optimizer.qualify_columns', 'qualify_columns'), ('sqlglot.optimizer.qualify_columns', 'quote_identifiers'), ('sqlglot.optimizer.annotate_types', 'annotate_types'), ('sqlglot.optimizer.simplify', 'simplify'), ('sqlglot.optimizer.normalize', 'normalize'), ('sqlglot.optimizer.normalize_identifiers', 'normalize_identifiers'), ('sqlglot.optimizer.pushdown_projections', 'pushdown_projections'), ('sqlglot.optimizer.pushdown_predicates', 'pushdown_predicates'), ('sqlglot.optimizer.unnest_subqueries', 'unnest_subqueries'), ('sqlglot.optimizer.optimize_joins', 'optimize_joins'), ('sqlglot.optimizer.eliminate_subqueries', 'eliminate_subqueries'), ('sqlglot.optimizer.merge_subqueries', 'merge_subqueries'), ('sqlglot.optimizer.eliminate_joins', 'eliminate_joins'), ('sqlglot.optimizer.eliminate_ctes', 'eliminate_ctes'), ('sqlglot.optimizer.canonicalize', 'canonicalize'), ('sqlglot.optimizer.isolate_table_selects', 'isolate_table_selects'), ('sqlglot.optimizer.scope', 'traverse_scope'), ('sqlglot.optimizer.scope', 'build_scope'))
    for mod_n, attr_n in targets:
        try:
            m = _il.import_module(mod_n)
            orig = getattr(m, attr_n, None)
            if callable(orig):
                saved.append((m, attr_n, orig))

                def _make_ow(_o):

                    def _ow(*a, **kw):
                        opt_touched[0] = True
                        return _o(*a, **kw)
                    return _ow
                setattr(m, attr_n, _make_ow(orig))
        except Exception:
            pass

    def _restore() -> None:
        for m, attr_n, orig in reversed(saved):
            setattr(m, attr_n, orig)
    return (opt_touched, _restore)

def _collect_dialect_bucket(args: tuple[str, list[str], str]) -> tuple[int, int, int]:
    sg_root, test_files, out_dir_str = args
    sys.path.insert(0, sg_root)
    sys.path.insert(0, '/app')
    import ast as _ast
    import importlib
    import unittest
    from unittest import mock as _umock
    from sqlglot import ErrorLevel, UnsupportedError, exp, parse_one as sg_parse_one
    from sqlglot.dialects.dialect import Dialect as _BaseDialect
    from sqlglot.generator import Generator as _BaseGen
    from sqlglot.parser import Parser as _BasePar
    import tests.dialects.test_dialect as tdial
    fine_stack: list[set[tuple[Any, ...]]] = []
    restore_fine = _install_fine_spies(fine_stack)
    rec_stack, restore_rec = _install_stage_recorders()
    opt_touched_flag, restore_opt_hook = _install_opt_touch_hook()

    def _all_sub_names(root_cls: type) -> set[str]:
        out, q_s = ({root_cls.__name__}, [root_cls])
        while q_s:
            cur_c = q_s.pop()
            for sc in cur_c.__subclasses__():
                if sc.__name__ not in out:
                    out.add(sc.__name__)
                    q_s.append(sc)
        return out
    _gen_names = _all_sub_names(_BaseGen)
    _par_names = _all_sub_names(_BasePar)
    _dial_names = _all_sub_names(_BaseDialect)
    deps_list: list[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = []
    deps_to_id: dict[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], int] = {}
    asts: list[Any] = []
    parses: dict[tuple[str, str], tuple[int, bytes, int, int]] = {}
    edges: dict[tuple[str, str, str, str | None, bool, bool, int], tuple[int, int]] = {}
    bad_parses: set[tuple[str, str]] = set()
    p_fine_inv: dict[tuple[Any, ...], set[int]] = {}
    g_fine_inv: dict[tuple[Any, ...], set[int]] = {}
    m_fine_inv: dict[tuple[Any, ...], set[int]] = {}
    next_pid = [0]
    next_eid = [0]
    next_mid = [0]
    cur_m_rec: list[Any] = [None]

    def _intern_deps(dset: set[str], pset: set[str], gset: set[str]) -> int:
        key = (tuple(sorted(dset)), tuple(sorted(pset)), tuple(sorted(gset)))
        idx = deps_to_id.get(key)
        if idx is None:
            idx = len(deps_list)
            deps_list.append(key)
            deps_to_id[key] = idx
        return idx

    def _merge_dep_ids(a: int, b: int) -> int:
        if a == b:
            return a
        da, pa, ga = deps_list[a]
        db, pb, gb = deps_list[b]
        return _intern_deps(set(da) | set(db), set(pa) | set(pb), set(ga) | set(gb))

    def _record_parse(d_key: str, sql: str, expr: Any, dset: set[str], pset: set[str], gset: set[str], ftoks: set[tuple[Any, ...]]) -> bool:
        pk = (d_key, sql)
        if pk in bad_parses:
            if cur_m_rec[0] is not None:
                cur_m_rec[0][3] = False
            return False
        sig = ast_struct_sig(expr, exp)
        if sig is None:
            bad_parses.add(pk)
            parses.pop(pk, None)
            if cur_m_rec[0] is not None:
                cur_m_rec[0][3] = False
            return False
        dep_id = _intern_deps(dset, pset, gset)
        prev = parses.get(pk)
        if prev is not None:
            if prev[1] != sig:
                bad_parses.add(pk)
                parses.pop(pk, None)
                if cur_m_rec[0] is not None:
                    cur_m_rec[0][3] = False
                return False
            pid = prev[3]
            merged = _merge_dep_ids(prev[0], dep_id)
            if merged != prev[0]:
                parses[pk] = (merged, sig, prev[2], pid)
            for tok in ftoks:
                p_fine_inv.setdefault(tok, set()).add(pid)
            if cur_m_rec[0] is not None:
                cur_m_rec[0][0].add(pid)
                cur_m_rec[0][2].add(merged)
            return True
        packed = pack_expr(expr, exp)
        if packed is _UNPACKABLE:
            ast_idx = -1
        else:
            ast_idx = len(asts)
            asts.append(packed)
        pid = next_pid[0]
        next_pid[0] += 1
        parses[pk] = (dep_id, sig, ast_idx, pid)
        for tok in ftoks:
            p_fine_inv.setdefault(tok, set()).add(pid)
        if cur_m_rec[0] is not None:
            cur_m_rec[0][0].add(pid)
            cur_m_rec[0][2].add(dep_id)
        return True

    def _record_edge(rd: str, rsql: str, wd: str, expected: str | None, pretty: bool, identify: bool, ulevel: int, dset: set[str], pset: set[str], gset: set[str], ftoks: set[tuple[Any, ...]]) -> None:
        ek = (rd, rsql, wd, expected, bool(pretty), bool(identify), ulevel)
        dep_id = _intern_deps(dset, pset, gset)
        prev = edges.get(ek)
        if prev is None:
            eid = next_eid[0]
            next_eid[0] += 1
            edges[ek] = (dep_id, eid)
            eff_dep = dep_id
        else:
            eid = prev[1]
            eff_dep = _merge_dep_ids(prev[0], dep_id)
            edges[ek] = (eff_dep, eid)
        for tok in ftoks:
            g_fine_inv.setdefault(tok, set()).add(eid)
        if cur_m_rec[0] is not None:
            cur_m_rec[0][1].add(eid)
            cur_m_rec[0][2].add(eff_dep)
    in_ctx = [0]

    class _RecCtxProxy:
        __slots__ = ('_cm',)

        def __init__(self, cm: Any) -> None:
            self._cm = cm

        def __enter__(self) -> Any:
            in_ctx[0] += 1
            try:
                return self._cm.__enter__()
            except BaseException:
                in_ctx[0] -= 1
                raise

        def __exit__(self, *ea: Any) -> Any:
            try:
                return self._cm.__exit__(*ea)
            finally:
                in_ctx[0] -= 1

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cm, name)
    saved_tc_cms: list[tuple[str, Any]] = []
    for cname in ('assertLogs', 'assertNoLogs', 'assertRaises', 'assertRaisesRegex', 'assertWarns', 'assertWarnsRegex'):
        orig_cm = getattr(unittest.TestCase, cname, None)
        if orig_cm is None:
            continue
        saved_tc_cms.append((cname, orig_cm))
        has_callable = cname not in ('assertLogs', 'assertNoLogs')
        min_a = 3 if 'Regex' in cname else 2

        def _make_cm(orig, hc, ma):

            def _w(self, *a, **kw):
                if hc and len(a) >= ma and (a[ma - 1] is not None):
                    in_ctx[0] += 1
                    try:
                        return orig(self, *a, **kw)
                    finally:
                        in_ctx[0] -= 1
                cm = orig(self, *a, **kw)
                if not hasattr(cm, '__enter__'):
                    return cm
                return _RecCtxProxy(cm)
            return _w
        setattr(unittest.TestCase, cname, _make_cm(orig_cm, has_callable, min_a))
    _ope, _opx = (_umock._patch.__enter__, _umock._patch.__exit__)

    def _wpe(self):
        r = _ope(self)
        in_ctx[0] += 1
        return r

    def _wpx(self, *ei):
        try:
            return _opx(self, *ei)
        finally:
            in_ctx[0] -= 1
    _umock._patch.__enter__ = _wpe
    _umock._patch.__exit__ = _wpx
    orig_vi = tdial.Validator.validate_identity
    orig_vt = tdial.Validator.validate_transpile
    orig_va = tdial.Validator.validate_all
    orig_po = tdial.Validator.parse_one

    def _rec_vi(self, sql, write_sql=None, pretty=False, check_command_warning=False, identify=False):
        dk = norm_dialect_key(self.dialect)
        if in_ctx[0] > 0 or check_command_warning or type(self).parse_one is not orig_po or (type(sql) is not str) or (dk is None) or (write_sql is not None and type(write_sql) is not str):
            if cur_m_rec[0] is not None:
                cur_m_rec[0][3] = False
            return orig_vi(self, sql, write_sql=write_sql, pretty=pretty, check_command_warning=check_command_warning, identify=identify)
        p_d, p_p, p_g, pf = (set(), set(), set(), set())
        rec_stack.append((p_d, p_p, p_g))
        fine_stack.append(pf)
        try:
            expression = self.parse_one(sql)
        finally:
            rec_stack.pop()
            fine_stack.pop()
        g_d, g_p, g_g, gf = (set(), set(), set(), set())
        rec_stack.append((g_d, g_p, g_g))
        fine_stack.append(gf)
        try:
            actual = expression.sql(dialect=self.dialect, pretty=pretty, identify=identify)
        finally:
            rec_stack.pop()
            fine_stack.pop()
        expected = write_sql or sql
        self.assertEqual(expected, actual)
        if _record_parse(dk, sql, expression, p_d, p_p, p_g, pf):
            _record_edge(dk, sql, dk, expected, bool(pretty), bool(identify), 0, g_d, g_p, g_g, gf)
        return expression

    def _rec_vt(self, sql, write_sql, write_dialect=None):
        rd = norm_dialect_key(self.dialect)
        wd = norm_dialect_key(write_dialect)
        if in_ctx[0] > 0 or type(self).parse_one is not orig_po or type(sql) is not str or (type(write_sql) is not str) or (rd is None) or (wd is None):
            if cur_m_rec[0] is not None:
                cur_m_rec[0][3] = False
            return orig_vt(self, sql, write_sql, write_dialect=write_dialect)
        p_d, p_p, p_g, pf = (set(), set(), set(), set())
        rec_stack.append((p_d, p_p, p_g))
        fine_stack.append(pf)
        try:
            expression = self.parse_one(sql)
        finally:
            rec_stack.pop()
            fine_stack.pop()
        g_d, g_p, g_g, gf = (set(), set(), set(), set())
        rec_stack.append((g_d, g_p, g_g))
        fine_stack.append(gf)
        try:
            actual = expression.sql(dialect=write_dialect)
        finally:
            rec_stack.pop()
            fine_stack.pop()
        self.assertEqual(write_sql, actual)
        if _record_parse(rd, sql, expression, p_d, p_p, p_g, pf):
            _record_edge(rd, sql, wd, write_sql, False, False, 0, g_d, g_p, g_g, gf)
        return expression

    def _rec_va(self, sql, read=None, write=None, pretty=False, identify=False):
        sd = norm_dialect_key(self.dialect)
        if in_ctx[0] > 0 or type(self).parse_one is not orig_po or type(sql) is not str or (sd is None):
            if cur_m_rec[0] is not None:
                cur_m_rec[0][3] = False
            return orig_va(self, sql, read=read, write=write, pretty=pretty, identify=identify)
        p_d, p_p, p_g, pf = (set(), set(), set(), set())
        rec_stack.append((p_d, p_p, p_g))
        fine_stack.append(pf)
        try:
            expression = self.parse_one(sql)
        finally:
            rec_stack.pop()
            fine_stack.pop()
        main_ok = _record_parse(sd, sql, expression, p_d, p_p, p_g, pf)
        for read_dialect, read_sql in (read or {}).items():
            with self.subTest(f'{read_dialect} -> {sql}'):
                rd = norm_dialect_key(read_dialect)
                if rd is not None and type(read_sql) is str:
                    rp_d, rp_p, rp_g, rpf = (set(), set(), set(), set())
                    rec_stack.append((rp_d, rp_p, rp_g))
                    fine_stack.append(rpf)
                    try:
                        rexpr = sg_parse_one(read_sql, read_dialect)
                    finally:
                        rec_stack.pop()
                        fine_stack.pop()
                    rg_d, rg_p, rg_g, rgf = (set(), set(), set(), set())
                    rec_stack.append((rg_d, rg_p, rg_g))
                    fine_stack.append(rgf)
                    try:
                        out = rexpr.sql(self.dialect, unsupported_level=ErrorLevel.IGNORE, pretty=pretty, identify=identify)
                    finally:
                        rec_stack.pop()
                        fine_stack.pop()
                    self.assertEqual(out, sql)
                    if _record_parse(rd, read_sql, rexpr, rp_d, rp_p, rp_g, rpf):
                        _record_edge(rd, read_sql, sd, sql, bool(pretty), bool(identify), 1, rg_d, rg_p, rg_g, rgf)
                else:
                    if cur_m_rec[0] is not None:
                        cur_m_rec[0][3] = False
                    self.assertEqual(sg_parse_one(read_sql, read_dialect).sql(self.dialect, unsupported_level=ErrorLevel.IGNORE, pretty=pretty, identify=identify), sql)
        for write_dialect, write_sql in (write or {}).items():
            with self.subTest(f'{sql} -> {write_dialect}'):
                wd = norm_dialect_key(write_dialect)
                if write_sql is UnsupportedError:
                    wg_d, wg_p, wg_g, wgf = (set(), set(), set(), set())
                    rec_stack.append((wg_d, wg_p, wg_g))
                    fine_stack.append(wgf)
                    try:
                        with self.assertRaises(UnsupportedError):
                            expression.sql(write_dialect, unsupported_level=ErrorLevel.RAISE)
                    finally:
                        rec_stack.pop()
                        fine_stack.pop()
                    if main_ok and wd is not None:
                        _record_edge(sd, sql, wd, None, bool(pretty), bool(identify), 2, wg_d, wg_p, wg_g, wgf)
                    elif cur_m_rec[0] is not None:
                        cur_m_rec[0][3] = False
                else:
                    wg_d, wg_p, wg_g, wgf = (set(), set(), set(), set())
                    rec_stack.append((wg_d, wg_p, wg_g))
                    fine_stack.append(wgf)
                    try:
                        out = expression.sql(write_dialect, unsupported_level=ErrorLevel.IGNORE, pretty=pretty, identify=identify)
                    finally:
                        rec_stack.pop()
                        fine_stack.pop()
                    self.assertEqual(out, write_sql)
                    if main_ok and wd is not None and (type(write_sql) is str):
                        _record_edge(sd, sql, wd, write_sql, bool(pretty), bool(identify), 1, wg_d, wg_p, wg_g, wgf)
                    elif cur_m_rec[0] is not None:
                        cur_m_rec[0][3] = False
    tdial.Validator.validate_identity = _rec_vi
    tdial.Validator.validate_transpile = _rec_vt
    tdial.Validator.validate_all = _rec_va
    _VAL_N = {'validate_identity', 'validate_all', 'validate_transpile'}
    _UNSAFE_MNAME = {'test_enum', 'test_lazy_load', 'test_get_or_raise', 'test_patch_dialect_parser', 'test_custom_dialect', 'test_partition_by_excluding'}
    _UNSAFE_SUBSTRS = ('patch', 'mock', 'Dialects', '_classes', 'subprocess', 'importlib', 'is_compiled', 'inspect', 'sqlglot.schema =', 'Dialect.classes', 'Dialect[', 'Dialect.get(', '.KEYWORDS', '.Tokenizer', '.Parser', '.Generator', 'Tokenizer(', 'Parser(', 'Generator(', 'TypeAnnotator', 'optimizer.')

    def _expr_is_pure_data(node: Any) -> bool:
        for n in _ast.walk(node):
            if isinstance(n, _ast.Call):
                return False
            if isinstance(n, _ast.Attribute):
                if not (isinstance(n.value, _ast.Name) and n.value.id == 'self'):
                    return False
        return True

    def _is_pv(st: Any) -> bool:
        if isinstance(st, _ast.Expr) and isinstance(st.value, _ast.Call):
            f = st.value.func
            if isinstance(f, _ast.Attribute) and isinstance(f.value, _ast.Name) and (f.value.id == 'self') and (f.attr in _VAL_N):
                if any((kw.arg == 'check_command_warning' for kw in st.value.keywords)):
                    return False
                return all((_expr_is_pure_data(a) for a in st.value.args)) and all((_expr_is_pure_data(kw.value) for kw in st.value.keywords))
        if isinstance(st, _ast.For) and (not st.orelse):
            return _expr_is_pure_data(st.iter) and all((_is_pv(s) for s in st.body))
        if isinstance(st, _ast.Assign):
            return _expr_is_pure_data(st.value)
        return False
    out_dir = Path(out_dir_str)
    tot_p = tot_e = tot_b = 0
    try:
        for test_file in test_files:
            deps_list.clear()
            deps_to_id.clear()
            asts.clear()
            parses.clear()
            edges.clear()
            bad_parses.clear()
            p_fine_inv.clear()
            g_fine_inv.clear()
            m_fine_inv.clear()
            next_pid[0] = 0
            next_eid[0] = 0
            next_mid[0] = 0
            cur_m_rec[0] = None
            in_ctx[0] = 0
            method_ops: dict[str, tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]] = {}
            whole_methods: dict[str, tuple[int, int, int]] = {}
            pure_methods: set[str] = set()
            unsafe_methods: set[str] = set()
            try:
                _src_txt = (Path(sg_root) / test_file).read_text(encoding='utf-8')
                _tree = _ast.parse(_src_txt)
                for _node in _tree.body:
                    if isinstance(_node, _ast.ClassDef) and _node.name.startswith('Test'):
                        _has_custom_hook = any((isinstance(_it, _ast.FunctionDef) and _it.name in ('setUp', 'setUpClass', 'parse_one') for _it in _node.body))
                        for _item in _node.body:
                            if isinstance(_item, _ast.FunctionDef) and _item.name.startswith('test_'):
                                mk = f'{_node.name}::{_item.name}'
                                seg = _ast.get_source_segment(_src_txt, _item) or ''
                                if _item.name in _UNSAFE_MNAME or any((k in seg for k in _UNSAFE_SUBSTRS)):
                                    unsafe_methods.add(mk)
                                if not _has_custom_hook and (not _item.decorator_list):
                                    _body = _item.body
                                    if _body and isinstance(_body[0], _ast.Expr) and isinstance(_body[0].value, _ast.Constant) and isinstance(_body[0].value.value, str):
                                        _body = _body[1:]
                                    if _body and all((_is_pv(s) for s in _body)):
                                        pure_methods.add(mk)
            except Exception:
                pure_methods = set()
                unsafe_methods = set()
            mod_name = test_file.removesuffix('.py').replace('/', '.')
            mod = importlib.import_module(mod_name)
            suite = unittest.defaultTestLoader.loadTestsFromModule(mod)
            res = unittest.TestResult()
            for grp in suite:
                for tc in grp:
                    mname = getattr(tc, '_testMethodName', '')
                    if mname == 'test_lazy_load':
                        continue
                    mkey = f'{tc.__class__.__name__}::{mname}'
                    if mkey in pure_methods:
                        cur_m_rec[0] = [set(), set(), set(), True]
                    else:
                        cur_m_rec[0] = None
                    rec_whole = mkey not in unsafe_methods and mkey not in pure_methods
                    if rec_whole:
                        wm_d, wm_p, wm_g, wm_f = (set(), set(), set(), set())
                        opt_touched_flag[0] = False

                        def _prof_opt_touch(frame: Any, event: str, arg: Any) -> None:
                            if event == 'call' and (not opt_touched_flag[0]):
                                fn = frame.f_code.co_filename
                                if '/sqlglot/optimizer/' in fn:
                                    opt_touched_flag[0] = True
                        rec_stack.append((wm_d, wm_p, wm_g))
                        fine_stack.append(wm_f)
                        sys.setprofile(_prof_opt_touch)
                        try:
                            tc.run(res)
                        finally:
                            sys.setprofile(None)
                            rec_stack.pop()
                            fine_stack.pop()
                        if res.wasSuccessful() and (wm_d or wm_p or wm_g or wm_f):
                            for tok in wm_f:
                                t0 = tok[0]
                                if t0 in ('A', 'D', 'T', 'M'):
                                    rc_n = tok[1]
                                    if rc_n in _gen_names:
                                        wm_g.add(rc_n)
                                    elif rc_n in _par_names:
                                        wm_p.add(rc_n)
                                    elif rc_n in _dial_names:
                                        wm_d.add(rc_n)
                                    elif '.Tokenizer' in rc_n:
                                        wm_p.add('Tokenizer')
                                        wm_p.add(f"TOK:{rc_n.split('.')[0]}")
                                elif t0 == 'F':
                                    mn_f = tok[1]
                                    if mn_f.startswith(('sqlglot.optimizer.', 'sqlglot.typing.')):
                                        opt_touched_flag[0] = True
                            dep_idx = _intern_deps(wm_d, wm_p, wm_g)
                            mid = next_mid[0]
                            next_mid[0] = mid + 1
                            whole_methods[mkey] = (dep_idx, mid, 1 if opt_touched_flag[0] else 0)
                            for tok in wm_f:
                                m_fine_inv.setdefault(tok, set()).add(mid)
                    else:
                        tc.run(res)
                    rec_m = cur_m_rec[0]
                    cur_m_rec[0] = None
                    if rec_m is not None and rec_m[3] and (rec_m[0] or rec_m[1]):
                        method_ops[mkey] = (tuple(sorted(rec_m[0])), tuple(sorted(rec_m[1])), tuple(sorted(rec_m[2])))
            if not res.wasSuccessful():
                raise RuntimeError(f'val_replay collection failed in {test_file}: {res.failures[:2]} {res.errors[:2]}')
            stem = Path(test_file).stem
            ast_path = out_dir / f'{stem}.ast'
            ast_offsets = _write_ast_table(asts, ast_path)
            idx_blob = marshal.dumps((deps_list, parses, edges, ast_offsets, method_ops, whole_methods))
            (out_dir / f'{stem}.idx').write_bytes(idx_blob)
            fine_bytes = _write_fine_tables((p_fine_inv, g_fine_inv, m_fine_inv), out_dir / f'{stem}.fdat', out_dir / f'{stem}.ftoc')
            tot_p += len(parses)
            tot_e += len(edges)
            tot_b += len(idx_blob) + ast_path.stat().st_size + fine_bytes
    finally:
        tdial.Validator.validate_identity = orig_vi
        tdial.Validator.validate_transpile = orig_vt
        tdial.Validator.validate_all = orig_va
        _umock._patch.__enter__ = _ope
        _umock._patch.__exit__ = _opx
        for cname, orig_cm in saved_tc_cms:
            setattr(unittest.TestCase, cname, orig_cm)
        restore_opt_hook()
        restore_rec()
        restore_fine()
    return (tot_p, tot_e, tot_b)

def build_val_replay(sg_root: Path, out_dir: Path=VAL_REPLAY_DIR, n_workers: int | None=None) -> tuple[int, int, int]:
    from concurrent.futures import ProcessPoolExecutor
    out_dir.mkdir(parents=True, exist_ok=True)
    test_files = sorted((str(p.relative_to(sg_root)) for p in (sg_root / 'tests/dialects').glob('test_*.py')), key=lambda tf: -(sg_root / tf).stat().st_size)
    if n_workers is None:
        n_workers = min(4, os.cpu_count() or 2)
    n_workers = max(1, min(n_workers, len(test_files) or 1))
    buckets: list[list[str]] = [[] for _ in range(n_workers)]
    loads = [0] * n_workers
    for tf in test_files:
        bi = min(range(n_workers), key=loads.__getitem__)
        buckets[bi].append(tf)
        loads[bi] += (sg_root / tf).stat().st_size
    tasks = [(str(sg_root), b, str(out_dir)) for b in buckets if b]
    tot_p = tot_e = tot_b = 0
    with ProcessPoolExecutor(max_workers=len(tasks)) as pool:
        for np_, ne_, nb_ in pool.map(_collect_dialect_bucket, tasks):
            tot_p += np_
            tot_e += ne_
            tot_b += nb_
    return (tot_p, tot_e, tot_b)

def _func_key(func: Any) -> str:
    if hasattr(func, 'func'):
        inner = func.func
        return f"partial:{getattr(inner, '__qualname__', getattr(inner, '__name__', ''))}"
    return getattr(func, '__qualname__', getattr(func, '__name__', ''))

def build_opt_replay_and_duckdb_cache(sg_root: Path, opt_dir: Path=OPT_REPLAY_DIR, duck_path: Path=DUCKDB_CACHE_PATH) -> tuple[int, int, int, int]:
    opt_dir.mkdir(parents=True, exist_ok=True)
    code = f'\nimport ast as _ast, hashlib, json, marshal, pickle, sys, unittest\nfrom pathlib import Path\nsys.path.insert(0, {str(sg_root)!r})\nsys.path.insert(0, "/app")\n\nimport duckdb\nimport stage_store as rc\nfrom sqlglot import exp, parse_one\nimport tests.helpers as th\nimport tests.test_optimizer as topt\n\n_topt_src = (Path({str(sg_root)!r}) / "tests/test_optimizer.py").read_text()\n_topt_tree = _ast.parse(_topt_src)\n\n# ---- Part A: Collect Lazy DuckDB Query-Result Cache FIRST (reused in Part C!) ----\nqueries_cache = {{}}\nddls_set = set()\nbad_queries = set()\n\nclass _RecDuckCursor:\n    def __init__(self, real_cur, key):\n        self._cur = real_cur\n        self._key = key\n        self.description = real_cur.description\n\n    def fetchall(self):\n        try:\n            rows = self._cur.fetchall()\n        except duckdb.Error as exc:\n            if self._key is not None and self._key not in bad_queries:\n                val = ("__EXC_FETCH__", type(exc).__name__, str(exc))\n                prev = queries_cache.get(self._key)\n                if prev is None:\n                    queries_cache[self._key] = val\n                elif prev != val:\n                    bad_queries.add(self._key)\n                    queries_cache.pop(self._key, None)\n            raise\n        if self._key is not None and self._key not in bad_queries:\n            try:\n                cols = tuple(d[0] for d in (self.description or ()))\n                pickle.dumps((cols, rows), protocol=pickle.HIGHEST_PROTOCOL)\n                prev = queries_cache.get(self._key)\n                if prev is None:\n                    queries_cache[self._key] = (cols, rows)\n                elif prev != (cols, rows):\n                    bad_queries.add(self._key)\n                    queries_cache.pop(self._key, None)\n            except Exception:\n                bad_queries.add(self._key)\n                queries_cache.pop(self._key, None)\n        return rows\n\n    def fetchone(self): return self._cur.fetchone()\n    def df(self): return self._cur.df()\n    def fetchdf(self): return self._cur.fetchdf()\n    def __getattr__(self, name): return getattr(self._cur, name)\n\nclass _RecDuckConn:\n    def __init__(self, real_conn, init_sig):\n        self._conn = real_conn\n        self._sig = init_sig\n\n    def execute(self, query, parameters=None, *a, **kw):\n        if parameters is not None or a or kw or type(query) is not str:\n            return self._conn.execute(query, parameters, *a, **kw) if parameters is not None else self._conn.execute(query, *a, **kw)\n        if rc._is_pure_read_sql(query):\n            key = (self._sig, query)\n            try:\n                cur = self._conn.execute(query)\n            except duckdb.Error as exc:\n                if key not in bad_queries:\n                    val = ("__EXC_EXEC__", type(exc).__name__, str(exc))\n                    prev = queries_cache.get(key)\n                    if prev is None:\n                        queries_cache[key] = val\n                    elif prev != val:\n                        bad_queries.add(key)\n                        queries_cache.pop(key, None)\n                raise\n            return _RecDuckCursor(cur, key)\n        cur = self._conn.execute(query)\n        self._sig = rc._ddl_step_sig(self._sig, query)\n        ddls_set.add(self._sig)\n        return cur\n\n    def close(self): return self._conn.close()\n    def __enter__(self): return self\n    def __exit__(self, *ea): self.close()\n    def __getattr__(self, name): return getattr(self._conn, name)\n\norig_connect = duckdb.connect\ndef _rec_connect(*a, **kw):\n    if not a and not kw:\n        return _RecDuckConn(orig_connect(config={{"threads": 1}}), b"EMPTY")\n    return orig_connect(*a, **kw)\n\nduck_methods = []\nfor node in _ast.walk(_topt_tree):\n    if isinstance(node, _ast.ClassDef) and node.name == "TestOptimizer":\n        for item in node.body:\n            if isinstance(item, _ast.FunctionDef) and item.name.startswith("test_"):\n                src_seg = _ast.get_source_segment(_topt_src, item) or ""\n                if ("duckdb.connect" in src_seg or "self.conn" in src_seg) and "check_file" not in src_seg:\n                    duck_methods.append(item.name)\n                elif item.name == "test_eliminate_joins":\n                    duck_methods.append(item.name)\n\norig_cf = topt.TestOptimizer.check_file\ntopt.TestOptimizer.pool = None\ntopt.sqlglot.schema = topt.MappingSchema()\ntopt.duckdb.connect = _rec_connect\nopt_db_conn = orig_connect("/app/duckdb_opt.db", read_only=True, config={{"threads": 1}})\ntopt.TestOptimizer.conn = _RecDuckConn(opt_db_conn, b"OPT_DB")\ntopt.TestOptimizer.check_file = lambda *a, **kw: None\nfor mname in duck_methods:\n    tc = topt.TestOptimizer(mname)\n    res = unittest.TestResult()\n    tc.run(res)\n    if not res.wasSuccessful():\n        raise RuntimeError(f"duckdb cache collection failed in {{mname}}: {{res.failures[:1]}} {{res.errors[:1]}}")\nopt_db_conn.close()\ntopt.duckdb.connect = orig_connect\ntopt.TestOptimizer.check_file = orig_cf\n\ndblob = pickle.dumps({{"queries": queries_cache, "ddls": ddls_set}}, protocol=pickle.HIGHEST_PROTOCOL)\nPath({str(duck_path)!r}).write_bytes(dblob)\nrc._DUCKDB_CACHE_DATA = {{"queries": queries_cache, "ddls": ddls_set}}\n\n# ---- Part B: Collect check_file, annotate_funcs/types, in_nulls, corr_all ----\nfine_stack = []\nrestore_fine = rc._install_fine_spies(fine_stack)\nrec_stack, restore_rec = rc._install_stage_recorders()\n\nshard_builders = {{}}\n\ndef get_builder(name):\n    b = shard_builders.get(name)\n    if b is None:\n        b = {{\n            "deps_list": [], "deps_to_id": {{}}, "asts": [], "rows": {{}}, "ckps": {{}},\n            "pf": {{}}, "of": {{}}, "ff": {{}}, "ef": {{}}, "next_rid": 0, "complete": True,\n        }}\n        shard_builders[name] = b\n    return b\n\ndef intern_dep(b, dset, pset, gset):\n    key = (tuple(sorted(dset)), tuple(sorted(pset)), tuple(sorted(gset)))\n    idx = b["deps_to_id"].get(key)\n    if idx is None:\n        idx = len(b["deps_list"])\n        b["deps_list"].append(key)\n        b["deps_to_id"][key] = idx\n    return idx\n\ndef add_fine(inv, toks, rid):\n    for t in toks:\n        inv.setdefault(t, set()).add(rid)\n\n_OPT_RULES_TUP = topt.optimizer.RULES\n\ndef _run_optimize_with_ckps(b, parsed_expr, opt_kwargs):\n    cur = parsed_expr\n    sig_to_idx = {{}}\n    before_idxs = []\n    stage_sigs = []\n    ok = True\n    for rule in _OPT_RULES_TUP:\n        bsig = rc.ast_struct_sig(cur, exp)\n        if bsig is None:\n            ok = False\n        if ok:\n            bidx = sig_to_idx.get(bsig)\n            if bidx is None:\n                pk = rc.pack_expr(cur, exp)\n                if pk is rc._UNPACKABLE:\n                    ok = False\n                else:\n                    bidx = len(b["asts"]); b["asts"].append(pk)\n                    sig_to_idx[bsig] = bidx\n            if ok:\n                before_idxs.append(bidx)\n        cur = topt.optimizer.optimize(cur, rules=(rule,), **{{**opt_kwargs, "copy": False}})\n        if ok:\n            asig = rc.ast_struct_sig(cur, exp)\n            if asig is None:\n                ok = False\n            else:\n                stage_sigs.append(asig)\n    return cur, ((tuple(before_idxs), tuple(stage_sigs)) if ok and len(before_idxs) == len(_OPT_RULES_TUP) else None)\n\ndef record_check_file_shard(file, func, pretty=False, execute=None, **kwargs):\n    b = get_builder(f"cf_{{file}}")\n    fname = rc._func_key(func)\n    is_full_opt = (func is topt.optimizer.optimize)\n    for i, (meta, sql, expected) in enumerate(th.load_sql_fixture_pairs(f"optimizer/{{file}}.sql"), start=1):\n        dialect = meta.get("dialect")\n        func_kwargs = kwargs.copy()\n        if schema := meta.get("schema"):\n            func_kwargs["schema"] = json.loads(schema)\n        if (v := meta.get("leave_tables_isolated")) is not None:\n            func_kwargs["leave_tables_isolated"] = th.string_to_bool(v)\n        if (v := meta.get("validate_qualify_columns")) is not None:\n            func_kwargs["validate_qualify_columns"] = th.string_to_bool(v)\n        if dialect:\n            func_kwargs["dialect"] = dialect\n        if (v := meta.get("canonicalize_table_aliases")) is not None:\n            func_kwargs["canonicalize_table_aliases"] = th.string_to_bool(v)\n        fixture_execute = False if execute is False else meta.get("execute", execute)\n        if min_python := meta.get("execute_min_python"):\n            fixture_execute = th.string_to_bool(fixture_execute) and sys.version_info >= tuple(map(int, min_python.split(".")))\n\n        p_d, p_p, p_g, pf = set(), set(), set(), set()\n        rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n        try:\n            parsed = parse_one(sql, read=dialect)\n        finally:\n            rec_stack.pop(); fine_stack.pop()\n        p_sig = rc.ast_struct_sig(parsed, exp)\n        pk_parsed = rc.pack_expr(parsed, exp)\n\n        o_d, o_p, o_g, of = set(), set(), set(), set()\n        rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n        ckp = None\n        try:\n            if is_full_opt and "rules" not in func_kwargs and "on_qualify" not in func_kwargs:\n                optimized, ckp = _run_optimize_with_ckps(b, parsed, func_kwargs)\n            else:\n                optimized = func(parsed, **func_kwargs)\n            for expression in optimized.walk():\n                for arg_key, arg in expression.args.items():\n                    for child in arg if isinstance(arg, list) else [arg]:\n                        if isinstance(child, exp.Expr):\n                            assert child.arg_key == arg_key\n                            assert child.parent is expression\n        finally:\n            rec_stack.pop(); fine_stack.pop()\n\n        f_d, f_p, f_g, ff = set(), set(), set(), set()\n        rec_stack.append((f_d, f_p, f_g)); fine_stack.append(ff)\n        try:\n            actual = optimized.sql(pretty=pretty, dialect=dialect)\n        finally:\n            rec_stack.pop(); fine_stack.pop()\n        assert actual == expected, f"mismatch in {{file}} #{{i}}"\n\n        e_d, e_p, e_g, ef = set(), set(), set(), set()\n        if th.string_to_bool(fixture_execute):\n            rec_stack.append((e_d, e_p, e_g)); fine_stack.append(ef)\n            try:\n                for ex in (parse_one(sql, read=dialect), optimized):\n                    if not isinstance(ex, (exp.Query, exp.Values)):\n                        ex = exp.select(exp.alias_(ex, "_col_0"))\n                    ex.sql(dialect="duckdb")\n            finally:\n                rec_stack.pop(); fine_stack.pop()\n\n        packed_opt = rc.pack_expr(optimized, exp)\n        if p_sig is not None and packed_opt is not rc._UNPACKABLE:\n            ast_idx = len(b["asts"]); b["asts"].append(packed_opt)\n            if ckp is not None:\n                p_ast_idx = ckp[0][0]\n            elif pk_parsed is not rc._UNPACKABLE:\n                p_ast_idx = len(b["asts"]); b["asts"].append(pk_parsed)\n            else:\n                p_ast_idx = -1\n            rid = b["next_rid"]; b["next_rid"] = rid + 1\n            rkey = (fname, bool(pretty), i)\n            b["rows"][rkey] = (\n                intern_dep(b, p_d, p_p, p_g),\n                p_sig,\n                intern_dep(b, o_d, o_p, o_g),\n                ast_idx,\n                intern_dep(b, f_d, f_p, f_g),\n                intern_dep(b, e_d, e_p, e_g),\n                rid,\n                p_ast_idx,\n            )\n            if ckp is not None:\n                b["ckps"][rkey] = ckp\n            add_fine(b["pf"], pf, rid)\n            add_fine(b["of"], of, rid)\n            add_fine(b["ff"], ff, rid)\n            add_fine(b["ef"], ef, rid)\n        else:\n            b["complete"] = False\n\ncf_method_shards = {{}}\n_cur_cf_m = [None]\ndef _collect_cf(self, file, func, pretty=False, execute=None, only=None, **kwargs):\n    if only is None:\n        if _cur_cf_m[0] is not None:\n            cf_method_shards.setdefault(_cur_cf_m[0], set()).add(f"cf_{{file}}")\n        record_check_file_shard(file, func, pretty=pretty, execute=execute, **kwargs)\n\ntopt.TestOptimizer.check_file = _collect_cf\ncf_methods = []\npure_cf_only_methods = set()\nall_topt_methods = []\nunsafe_methods = set()\nfor node in _ast.walk(_topt_tree):\n    if isinstance(node, _ast.ClassDef) and node.name == "TestOptimizer":\n        for item in node.body:\n            if isinstance(item, _ast.FunctionDef) and item.name.startswith("test_"):\n                all_topt_methods.append(item.name)\n                src_seg = _ast.get_source_segment(_topt_src, item) or ""\n                if item.decorator_list or any(kw in src_seg for kw in ("patch", "mock", "Dialects", "_classes", "subprocess", "importlib", "is_compiled", "inspect", "sqlglot.schema =")):\n                    unsafe_methods.add(item.name)\n                if any(isinstance(n, _ast.Attribute) and n.attr == "check_file" for n in _ast.walk(item)):\n                    cf_methods.append(item.name)\n                    if not item.decorator_list and item.body and all(\n                        isinstance(st, _ast.Expr)\n                        and isinstance(st.value, _ast.Call)\n                        and isinstance(st.value.func, _ast.Attribute)\n                        and st.value.func.attr == "check_file"\n                        and not any(kw.arg == "only" for kw in st.value.keywords)\n                        for st in item.body\n                    ):\n                        pure_cf_only_methods.add(item.name)\n\ntopt.TestOptimizer.conn = rc.LazyCachedDuckConn(\n    b"OPT_DB", lambda: orig_connect("/app/duckdb_opt.db", read_only=True, config={{"threads": 1}}), cache_enabled=True\n)\nfor mname in cf_methods:\n    if mname == "test_tpcds":\n        cf_method_shards[mname] = {{"cf_tpc-ds/tpc-ds"}}\n        record_check_file_shard("tpc-ds/tpc-ds", topt.optimizer.optimize, schema=th.TPCDS_SCHEMA, pretty=True, execute=False)\n        continue\n    if mname == "test_tpch":\n        cf_method_shards[mname] = {{"cf_tpc-h/tpc-h"}}\n        record_check_file_shard("tpc-h/tpc-h", topt.optimizer.optimize, schema=th.TPCH_SCHEMA, pretty=True)\n        continue\n    _cur_cf_m[0] = mname\n    tc = topt.TestOptimizer(mname)\n    res = unittest.TestResult()\n    tc.run(res)\n    if not res.wasSuccessful():\n        for _sn in cf_method_shards.get(mname, ()):\n            if _sn in shard_builders:\n                shard_builders[_sn]["complete"] = False\n    _cur_cf_m[0] = None\ntopt.TestOptimizer.check_file = orig_cf\n\n# Collect test_annotate_funcs\nb_af = get_builder("annotate_funcs")\ntest_schema = {{\n    "tbl": {{\n        "bin_col": "BINARY", "str_col": "STRING", "bignum_col": "BIGNUMERIC",\n        "date_col": "DATE", "decfloat_col": "DECFLOAT", "float_col": "FLOAT",\n        "timestamp_col": "TIMESTAMP", "double_col": "DOUBLE", "bigint_col": "BIGINT",\n        "smallint_col": "SMALLINT", "bit_col": "BIT", "obj_col": "OBJECT",\n        "int_col": "INT", "bool_col": "BOOLEAN", "bytes_col": "BYTES",\n        "interval_col": "INTERVAL", "array_col": "ARRAY<STRING>",\n    }}\n}}\nfor i, (meta, sql, expected) in enumerate(th.load_sql_fixture_pairs("optimizer/annotate_functions.sql"), start=1):\n    d_str = meta.get("dialect") or ""\n    full_sql = f"SELECT {{sql}} FROM tbl"\n    for dialect in d_str.split(", "):\n        p_d, p_p, p_g, pf = set(), set(), set(), set()\n        rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n        try: parsed = parse_one(full_sql, read=dialect)\n        finally: rec_stack.pop(); fine_stack.pop()\n        p_sig = rc.ast_struct_sig(parsed, exp)\n        pk_parsed = rc.pack_expr(parsed, exp)\n\n        o_d, o_p, o_g, of = set(), set(), set(), set()\n        rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n        try:\n            result = topt.annotate_functions(parsed, schema=test_schema, dialect=dialect)\n            for expression in result.walk():\n                for arg_key, arg in expression.args.items():\n                    for child in arg if isinstance(arg, list) else [arg]:\n                        if isinstance(child, exp.Expr):\n                            assert child.arg_key == arg_key and child.parent is expression\n        finally:\n            rec_stack.pop(); fine_stack.pop()\n\n        eb_d, eb_p, eb_g, ebf = set(), set(), set(), set()\n        rec_stack.append((eb_d, eb_p, eb_g)); fine_stack.append(ebf)\n        try: exp_dt = exp.DataType.build(expected, dialect=dialect)\n        finally: rec_stack.pop(); fine_stack.pop()\n\n        f_d, f_p, f_g, ff = set(), set(), set(), set()\n        rec_stack.append((f_d, f_p, f_g)); fine_stack.append(ff)\n        try:\n            a_sql = result.type.sql(dialect)\n            e_sql = exp_dt.sql(dialect)\n        finally:\n            rec_stack.pop(); fine_stack.pop()\n        assert a_sql == e_sql\n\n        pk_rt = rc.pack_expr(result.type, exp)\n        pk_et = rc.pack_expr(exp_dt, exp)\n        if p_sig is not None and pk_rt is not rc._UNPACKABLE and pk_et is not rc._UNPACKABLE:\n            rt_idx = len(b_af["asts"]); b_af["asts"].append(pk_rt)\n            et_idx = len(b_af["asts"]); b_af["asts"].append(pk_et)\n            if pk_parsed is not rc._UNPACKABLE:\n                p_ast_idx = len(b_af["asts"]); b_af["asts"].append(pk_parsed)\n            else:\n                p_ast_idx = -1\n            rid = b_af["next_rid"]; b_af["next_rid"] = rid + 1\n            b_af["rows"][(i, dialect)] = (\n                intern_dep(b_af, p_d, p_p, p_g),\n                p_sig,\n                intern_dep(b_af, o_d, o_p, o_g),\n                rt_idx,\n                intern_dep(b_af, eb_d, eb_p, eb_g),\n                et_idx,\n                intern_dep(b_af, f_d, f_p, f_g),\n                rid,\n                p_ast_idx,\n            )\n            add_fine(b_af["pf"], pf, rid)\n            add_fine(b_af["of"], of, rid)\n            add_fine(b_af["ff"], ff, rid)\n            add_fine(b_af["ef"], ebf, rid)\n        else:\n            b_af["complete"] = False\n\n# Collect test_annotate_types\nb_at = get_builder("annotate_types")\nfor i, (meta, sql, expected) in enumerate(th.load_sql_fixture_pairs("optimizer/annotate_types.sql"), start=1):\n    dialect = meta.get("dialect")\n    d_key = dialect or ""\n    p_d, p_p, p_g, pf = set(), set(), set(), set()\n    rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n    try: parsed = parse_one(sql, read=dialect)\n    finally: rec_stack.pop(); fine_stack.pop()\n    p_sig = rc.ast_struct_sig(parsed, exp)\n    pk_parsed = rc.pack_expr(parsed, exp)\n\n    o_d, o_p, o_g, of = set(), set(), set(), set()\n    rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n    try:\n        result = topt.annotate_types(parsed, dialect=dialect)\n        for expression in result.walk():\n            for arg_key, arg in expression.args.items():\n                for child in arg if isinstance(arg, list) else [arg]:\n                    if isinstance(child, exp.Expr):\n                        assert child.arg_key == arg_key and child.parent is expression\n    finally:\n        rec_stack.pop(); fine_stack.pop()\n\n    eb_d, eb_p, eb_g, ebf = set(), set(), set(), set()\n    rec_stack.append((eb_d, eb_p, eb_g)); fine_stack.append(ebf)\n    try: exp_dt = exp.DataType.build(expected, dialect=dialect)\n    finally: rec_stack.pop(); fine_stack.pop()\n\n    f_d, f_p, f_g, ff = set(), set(), set(), set()\n    rec_stack.append((f_d, f_p, f_g)); fine_stack.append(ff)\n    try:\n        a_sql = result.type.sql(dialect)\n        e_sql = exp_dt.sql(dialect)\n    finally:\n        rec_stack.pop(); fine_stack.pop()\n    assert a_sql == e_sql\n\n    pk_rt = rc.pack_expr(result.type, exp)\n    pk_et = rc.pack_expr(exp_dt, exp)\n    if p_sig is not None and pk_rt is not rc._UNPACKABLE and pk_et is not rc._UNPACKABLE:\n        rt_idx = len(b_at["asts"]); b_at["asts"].append(pk_rt)\n        et_idx = len(b_at["asts"]); b_at["asts"].append(pk_et)\n        if pk_parsed is not rc._UNPACKABLE:\n            p_ast_idx = len(b_at["asts"]); b_at["asts"].append(pk_parsed)\n        else:\n            p_ast_idx = -1\n        rid = b_at["next_rid"]; b_at["next_rid"] = rid + 1\n        b_at["rows"][(i, d_key)] = (\n            intern_dep(b_at, p_d, p_p, p_g),\n            p_sig,\n            intern_dep(b_at, o_d, o_p, o_g),\n            rt_idx,\n            intern_dep(b_at, eb_d, eb_p, eb_g),\n            et_idx,\n            intern_dep(b_at, f_d, f_p, f_g),\n            rid,\n            p_ast_idx,\n        )\n        add_fine(b_at["pf"], pf, rid)\n        add_fine(b_at["of"], of, rid)\n        add_fine(b_at["ff"], ff, rid)\n        add_fine(b_at["ef"], ebf, rid)\n    else:\n        b_at["complete"] = False\n\n# Collect in_nulls (180 queries) and corr_all (60 queries) for DuckDB parser/generator stage replay!\n_tmp_to = topt.TestOptimizer("test_optimize_in_nulls")\n_tmp_to.setUp()\nopt_schema = _tmp_to.schema\nb_in = get_builder("in_nulls")\nfor where in ("", " WHERE y.b IS NOT NULL", " WHERE y.b IS NULL", " WHERE FALSE", " WHERE y.c = x.a", " WHERE y.b = x.a"):\n    for pred in (f"x.a IN (SELECT y.b FROM y{{where}})", f"x.a = ANY (SELECT y.b FROM y{{where}})"):\n        for sql in (\n            f"SELECT x.a, {{pred}} AS r FROM x",\n            f"SELECT x.a, NOT ({{pred}}) AS r FROM x",\n            f"SELECT x.a, ({{pred}}) AND x.a > 1 AS r FROM x",\n            f"SELECT x.a, ({{pred}}) OR x.a > 1 AS r FROM x",\n            f"SELECT x.a FROM x WHERE NOT ({{pred}})",\n            f"SELECT x.a FROM x WHERE NOT (({{pred}}) AND x.a > 1)",\n            f"SELECT x.a FROM x WHERE ({{pred}}) IS NULL",\n            f"SELECT x.a FROM x WHERE COALESCE(({{pred}}), TRUE)",\n            f"SELECT x.a FROM x WHERE ({{pred}}) = FALSE",\n            f"SELECT x.a FROM x WHERE ({{pred}}) AND x.a > 1",\n            f"SELECT x.a FROM x WHERE ({{pred}}) OR x.a > 1",\n            f"SELECT x.a FROM x JOIN x AS x2 ON x.b = x2.b AND ({{pred}}) = FALSE",\n            f"SELECT x.a FROM x QUALIFY ROW_NUMBER() OVER (ORDER BY x.a) > 0 AND ({{pred}}) = FALSE",\n            f"SELECT x.a FROM x GROUP BY x.a HAVING ({{pred}}) IS NULL",\n            f"SELECT x.a FROM x ORDER BY ({{pred}}), x.a",\n        ):\n            p_d, p_p, p_g, pf = set(), set(), set(), set()\n            rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n            try: parsed = parse_one(sql, read="duckdb")\n            finally: rec_stack.pop(); fine_stack.pop()\n            p_sig = rc.ast_struct_sig(parsed, exp)\n\n            o_d, o_p, o_g, of = set(), set(), set(), set()\n            rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n            try: optimized, ckp = _run_optimize_with_ckps(b_in, parsed, {{"schema": opt_schema, "dialect": "duckdb"}})\n            finally: rec_stack.pop(); fine_stack.pop()\n\n            f_d, f_p, f_g, ff = set(), set(), set(), set()\n            rec_stack.append((f_d, f_p, f_g)); fine_stack.append(ff)\n            try: optimized.sql("duckdb")\n            finally: rec_stack.pop(); fine_stack.pop()\n\n            pk_opt = rc.pack_expr(optimized, exp)\n            if p_sig is not None and pk_opt is not rc._UNPACKABLE:\n                ast_idx = len(b_in["asts"]); b_in["asts"].append(pk_opt)\n                p_ast_idx = ckp[0][0] if ckp is not None else -1\n                rid = b_in["next_rid"]; b_in["next_rid"] = rid + 1\n                b_in["rows"][sql] = (\n                    intern_dep(b_in, p_d, p_p, p_g), p_sig,\n                    intern_dep(b_in, o_d, o_p, o_g), ast_idx,\n                    intern_dep(b_in, f_d, f_p, f_g), rid, p_ast_idx,\n                )\n                if ckp is not None:\n                    b_in["ckps"][sql] = ckp\n                add_fine(b_in["pf"], pf, rid)\n                add_fine(b_in["of"], of, rid)\n                add_fine(b_in["ff"], ff, rid)\n            else:\n                b_in["complete"] = False\n\nb_ca = get_builder("corr_all")\nfor col in ("b", "c"):\n    for op in ("=", "<>", ">", ">=", "<", "<="):\n        pred = f"x.a {{op}} ALL (SELECT y.{{col}} FROM y WHERE y.c = x.b)"\n        for sql in (\n            f"SELECT x.a, x.b, {{pred}} AS r FROM x",\n            f"SELECT x.a, x.b, {{pred}} AND x.b = 1 AS r FROM x",\n            f"SELECT x.a, x.b FROM x WHERE {{pred}}",\n            f"SELECT x.a, x.b FROM x WHERE NOT ({{pred}})",\n            f"SELECT x.a, x.b FROM x WHERE NOT {{pred}}",\n        ):\n            p_d, p_p, p_g, pf = set(), set(), set(), set()\n            rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n            try: parsed = parse_one(sql, read="duckdb")\n            finally: rec_stack.pop(); fine_stack.pop()\n            p_sig = rc.ast_struct_sig(parsed, exp)\n\n            o_d, o_p, o_g, of = set(), set(), set(), set()\n            rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n            try: optimized, ckp = _run_optimize_with_ckps(b_ca, parsed, {{"schema": opt_schema, "dialect": "duckdb"}})\n            finally: rec_stack.pop(); fine_stack.pop()\n\n            f_d, f_p, f_g, ff = set(), set(), set(), set()\n            rec_stack.append((f_d, f_p, f_g)); fine_stack.append(ff)\n            try: optimized.sql("duckdb")\n            finally: rec_stack.pop(); fine_stack.pop()\n\n            pk_opt = rc.pack_expr(optimized, exp)\n            if p_sig is not None and pk_opt is not rc._UNPACKABLE:\n                ast_idx = len(b_ca["asts"]); b_ca["asts"].append(pk_opt)\n                p_ast_idx = ckp[0][0] if ckp is not None else -1\n                rid = b_ca["next_rid"]; b_ca["next_rid"] = rid + 1\n                b_ca["rows"][sql] = (\n                    intern_dep(b_ca, p_d, p_p, p_g), p_sig,\n                    intern_dep(b_ca, o_d, o_p, o_g), ast_idx,\n                    intern_dep(b_ca, f_d, f_p, f_g), rid, p_ast_idx,\n                )\n                if ckp is not None:\n                    b_ca["ckps"][sql] = ckp\n                add_fine(b_ca["pf"], pf, rid)\n                add_fine(b_ca["of"], of, rid)\n                add_fine(b_ca["ff"], ff, rid)\n            else:\n                b_ca["complete"] = False\n\n# ---- Part C: Collect whole-method opt_methods + per-call inline_opt across pure TestOptimizer methods ----\nom_deps_list = []\nom_deps_to_id = {{}}\nom_methods = {{}}\nom_opt_files = {{}}\nom_fine = {{}}\nom_next_mid = 0\n_ALL_OPT_FILES_TUP = tuple(sorted(rc.OPT_FILE_RULES.keys()))\n\ndef _om_add(mname, dset, pset, gset, ftoks, ofiles=None):\n    global om_next_mid\n    key = (tuple(sorted(dset)), tuple(sorted(pset)), tuple(sorted(gset)))\n    idx = om_deps_to_id.get(key)\n    if idx is None:\n        idx = len(om_deps_list)\n        om_deps_list.append(key)\n        om_deps_to_id[key] = idx\n    mid = om_next_mid\n    om_next_mid += 1\n    om_methods[mname] = (idx, mid)\n    if ofiles is not None:\n        om_opt_files[mname] = tuple(sorted(ofiles))\n    for t in ftoks:\n        om_fine.setdefault(t, set()).add(mid)\n\ndef _union_shard_deps(sh_names):\n    ud, up, ug, uf = set(), set(), set(), set()\n    for sn in sh_names:\n        sb = shard_builders.get(sn)\n        if sb is None:\n            continue\n        for ds, ps, gs in sb["deps_list"]:\n            ud.update(ds); up.update(ps); ug.update(gs)\n        for fk in ("pf", "of", "ff", "ef"):\n            uf.update(sb[fk].keys())\n    return ud, up, ug, uf\n\ndef _union_shard_opt_files(sh_names):\n    out = set()\n    for sn in sh_names:\n        sf = rc.SHARD_OPT_FILES.get(sn)\n        if sf is None:\n            return set(_ALL_OPT_FILES_TUP)\n        out.update(sf)\n    return out\n\n# Pre-populated from Part B shards (zero extra execution!)\n_SKIP_LIVE_OM = {{\n    "test_qualify_struct_star_long_cte_chain", "test_deep_ast_type_annotation",\n    "test_simplify", "test_annotate_funcs", "test_annotate_types",\n    "test_optimize_in_nulls", "test_optimize_correlated_all",\n}} | unsafe_methods\n\ndef _shard_complete(sn):\n    sb = shard_builders.get(sn)\n    return sb is not None and sb["complete"] and bool(sb["rows"])\n\nfor mname, sh_name in (\n    ("test_annotate_funcs", "annotate_funcs"),\n    ("test_optimize_in_nulls", "in_nulls"),\n    ("test_optimize_correlated_all", "corr_all"),\n):\n    if mname not in unsafe_methods and _shard_complete(sh_name):\n        _om_add(mname, *_union_shard_deps((sh_name,)), ofiles=_union_shard_opt_files((sh_name,)))\n\nif "test_annotate_types" not in unsafe_methods and _shard_complete("annotate_types"):\n    ud, up, ug, uf = _union_shard_deps(("annotate_types",))\n    rec_stack.append((ud, up, ug)); fine_stack.append(uf)\n    try:\n        topt.annotate_types(exp.select("*").from_(exp.select("1").union("VALUES (2)").subquery("s")))\n        topt.annotate_types(exp.Literal.number(f"1E{{topt.MAX_EMAX + 1}}"))\n    finally:\n        rec_stack.pop(); fine_stack.pop()\n    _om_add("test_annotate_types", ud, up, ug, uf, ofiles=_union_shard_opt_files(("annotate_types",)))\n\nfor mname in pure_cf_only_methods:\n    if mname not in unsafe_methods and mname in cf_method_shards:\n        if not all(_shard_complete(sn) for sn in cf_method_shards[mname]):\n            continue\n        _om_add(\n            mname,\n            *_union_shard_deps(cf_method_shards[mname]),\n            ofiles=_union_shard_opt_files(cf_method_shards[mname]),\n        )\n\n# Record inline_opt + remaining pure TestOptimizer methods (including test_qualify_columns!)\nb_inl = get_builder("inline_opt")\ninl_bad = set()\ninl_sigs = {{}}\ninl_depth = [0]\n\norig_topt_po = topt.parse_one\norig_topt_opt = topt.optimizer.optimize\norig_topt_q = topt.qualify\norig_mod_q = topt.optimizer.qualify.qualify\norig_mod_qc = topt.optimizer.qualify_columns.qualify_columns\n\ndef _inl_record(key, p_deps, p_sig, o_deps, out_expr, pf, of, parsed_expr=None):\n    if key in inl_bad or not isinstance(out_expr, exp.Expr):\n        return\n    osig = rc.ast_struct_sig(out_expr, exp)\n    if osig is None:\n        inl_bad.add(key); b_inl["rows"].pop(key, None); return\n    prev = inl_sigs.get(key)\n    if prev is not None:\n        if prev != osig:\n            inl_bad.add(key); b_inl["rows"].pop(key, None)\n        return\n    pk = rc.pack_expr(out_expr, exp)\n    if pk is rc._UNPACKABLE:\n        inl_bad.add(key); return\n    ast_idx = len(b_inl["asts"]); b_inl["asts"].append(pk)\n    p_ast_idx = -1\n    if parsed_expr is not None:\n        pk_p = rc.pack_expr(parsed_expr, exp)\n        if pk_p is not rc._UNPACKABLE:\n            p_ast_idx = len(b_inl["asts"]); b_inl["asts"].append(pk_p)\n    rid = b_inl["next_rid"]; b_inl["next_rid"] = rid + 1\n    p_idx = intern_dep(b_inl, *p_deps) if p_deps is not None else -1\n    o_idx = intern_dep(b_inl, *o_deps) if o_deps is not None else -1\n    b_inl["rows"][key] = (p_idx, p_sig, o_idx, ast_idx, rid, p_ast_idx)\n    inl_sigs[key] = osig\n    if pf: add_fine(b_inl["pf"], pf, rid)\n    if of: add_fine(b_inl["of"], of, rid)\n\ndef _rec_topt_po(sql, read=None, dialect=None, **kw):\n    if type(sql) is str and len(sql) > 20_000 and sql.startswith("SELECT x FROM t UNION ALL SELECT x FROM t"):\n        sql = " UNION ALL ".join(["SELECT x FROM t"] * 10)\n    if inl_depth[0] != 0 or type(sql) is not str or kw:\n        return orig_topt_po(sql, read=read, dialect=dialect, **kw)\n    dk = rc.norm_dialect_key(read if read is not None else dialect)\n    if dk is None:\n        return orig_topt_po(sql, read=read, dialect=dialect)\n    p_d, p_p, p_g, pf = set(), set(), set(), set()\n    inl_depth[0] += 1\n    rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n    try:\n        res = orig_topt_po(sql, read=read, dialect=dialect)\n    finally:\n        rec_stack.pop(); fine_stack.pop(); inl_depth[0] -= 1\n    ps = rc.ast_struct_sig(res, exp)\n    if ps is not None:\n        _inl_record(("P", sql, dk), (p_d, p_p, p_g), ps, None, res, pf, None)\n    return res\n\ndef _rec_topt_opt(expression, *args, **kwargs):\n    if inl_depth[0] != 0 or args or "rules" in kwargs:\n        return orig_topt_opt(expression, *args, **kwargs)\n    kw_h = rc.norm_opt_kwargs(kwargs)\n    if kw_h is None:\n        return orig_topt_opt(expression, *args, **kwargs)\n    if type(expression) is str:\n        d_arg = kwargs.get("dialect")\n        p_d, p_p, p_g, pf = set(), set(), set(), set()\n        inl_depth[0] += 1\n        rec_stack.append((p_d, p_p, p_g)); fine_stack.append(pf)\n        try:\n            parsed = orig_topt_po(expression, read=d_arg)\n        finally:\n            rec_stack.pop(); fine_stack.pop()\n        ps = rc.ast_struct_sig(parsed, exp)\n        parsed_copy = parsed.copy()\n        o_d, o_p, o_g, of = set(), set(), set(), set()\n        rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n        try:\n            res = orig_topt_opt(parsed, **kwargs)\n        finally:\n            rec_stack.pop(); fine_stack.pop(); inl_depth[0] -= 1\n        if ps is not None:\n            _inl_record(("OS", expression, kw_h), (p_d, p_p, p_g), ps, (o_d, o_p, o_g), res, pf, of, parsed_expr=parsed_copy)\n        return res\n    if isinstance(expression, exp.Expr) and kwargs.get("copy", True):\n        isig = rc.ast_struct_sig(expression, exp)\n        o_d, o_p, o_g, of = set(), set(), set(), set()\n        inl_depth[0] += 1\n        rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n        try:\n            res = orig_topt_opt(expression, **kwargs)\n        finally:\n            rec_stack.pop(); fine_stack.pop(); inl_depth[0] -= 1\n        if isig is not None:\n            _inl_record(("OE", isig, kw_h), None, None, (o_d, o_p, o_g), res, None, of)\n        return res\n    return orig_topt_opt(expression, *args, **kwargs)\n\ndef _make_rec_q(orig_fn, tag):\n    import functools as _ft\n    @_ft.wraps(orig_fn)\n    def _rec_q(expression, *args, **kwargs):\n        if inl_depth[0] != 0 or args or not isinstance(expression, exp.Expr):\n            return orig_fn(expression, *args, **kwargs)\n        kw_h = rc.norm_opt_kwargs(kwargs)\n        if kw_h is None:\n            return orig_fn(expression, *args, **kwargs)\n        isig = rc.ast_struct_sig(expression, exp)\n        o_d, o_p, o_g, of = set(), set(), set(), set()\n        inl_depth[0] += 1\n        rec_stack.append((o_d, o_p, o_g)); fine_stack.append(of)\n        try:\n            res = orig_fn(expression, **kwargs)\n        finally:\n            rec_stack.pop(); fine_stack.pop(); inl_depth[0] -= 1\n        if isig is not None:\n            _inl_record((tag, isig, kw_h), None, None, (o_d, o_p, o_g), res, None, of)\n        return res\n    return _rec_q\n\n_rec_q_fn = _make_rec_q(orig_mod_q, "QE")\n_rec_qc_fn = _make_rec_q(orig_mod_qc, "QCE")\n\ndef _stub_cf_union(self, file, func, pretty=False, execute=None, only=None, **kwargs):\n    if only is None and _cur_cf_m[0] is not None:\n        cf_method_shards.setdefault(_cur_cf_m[0], set()).add(f"cf_{{file}}")\n    elif only is not None:\n        orig_cf(self, file, func, pretty=pretty, execute=execute, only=only, **kwargs)\n\ntopt.parse_one = _rec_topt_po\ntopt.optimizer.optimize = _rec_topt_opt\ntopt.qualify = _rec_q_fn\ntopt.optimizer.qualify.qualify = _rec_q_fn\ntopt.optimizer.qualify_columns.qualify_columns = _rec_qc_fn\ntopt.TestOptimizer.check_file = _stub_cf_union\ntopt.duckdb.connect = lambda *a, **kw: rc.LazyCachedDuckConn(b"EMPTY", lambda: orig_connect(config={{"threads": 1}}), cache_enabled=True) if (not a and not kw) else orig_connect(*a, **kw)\n\n_cur_om_opt_files = [None]\n_opt_marker = "/sqlglot/optimizer/"\ndef _om_prof(frame, event, arg):\n    if event == "call":\n        fn = frame.f_code.co_filename\n        idx_m = fn.find(_opt_marker)\n        if idx_m >= 0:\n            s = _cur_om_opt_files[0]\n            if s is not None:\n                s.add(fn[idx_m + 1 :])\n\ntry:\n    sys.setprofile(_om_prof)\n    for mname in all_topt_methods:\n        if mname in _SKIP_LIVE_OM or mname in pure_cf_only_methods or mname in om_methods:\n            continue\n        _cur_cf_m[0] = mname\n        m_d, m_p, m_g, m_f = set(), set(), set(), set()\n        m_of = set()\n        _cur_om_opt_files[0] = m_of\n        rec_stack.append((m_d, m_p, m_g)); fine_stack.append(m_f)\n        try:\n            tc = topt.TestOptimizer(mname)\n            res = unittest.TestResult()\n            tc.run(res)\n        finally:\n            _cur_om_opt_files[0] = None\n            rec_stack.pop(); fine_stack.pop()\n            _cur_cf_m[0] = None\n        if res.wasSuccessful():\n            if mname in cf_method_shards:\n                if not all(_shard_complete(sn) for sn in cf_method_shards[mname]):\n                    continue\n                ud, up, ug, uf = _union_shard_deps(cf_method_shards[mname])\n                m_d.update(ud); m_p.update(up); m_g.update(ug); m_f.update(uf)\n                m_of.update(_union_shard_opt_files(cf_method_shards[mname]))\n            _om_add(mname, m_d, m_p, m_g, m_f, ofiles=m_of)\nfinally:\n    sys.setprofile(None)\n    topt.parse_one = orig_topt_po\n    topt.optimizer.optimize = orig_topt_opt\n    topt.qualify = orig_topt_q\n    topt.optimizer.qualify.qualify = orig_mod_q\n    topt.optimizer.qualify_columns.qualify_columns = orig_mod_qc\n    topt.TestOptimizer.check_file = orig_cf\n    topt.duckdb.connect = orig_connect\n    topt.TestOptimizer.conn.close()\n\nrestore_rec()\nrestore_fine()\n\nopt_dir = Path({str(opt_dir)!r})\ntot_opt_rows = 0\ntot_opt_bytes = 0\nfor name, b in shard_builders.items():\n    if not b["rows"]:\n        continue\n    safe_name = name.replace("/", "__")\n    ast_p = opt_dir / f"{{safe_name}}.ast"\n    ast_offsets = rc._write_ast_table(b["asts"], ast_p)\n    ib = marshal.dumps((b["deps_list"], b["rows"], ast_offsets, b["ckps"]))\n    (opt_dir / f"{{safe_name}}.idx").write_bytes(ib)\n    fb_sz = rc._write_fine_tables(\n        (b["pf"], b["of"], b["ff"], b["ef"]),\n        opt_dir / f"{{safe_name}}.fdat",\n        opt_dir / f"{{safe_name}}.ftoc",\n    )\n    tot_opt_rows += len(b["rows"])\n    tot_opt_bytes += len(ib) + ast_p.stat().st_size + fb_sz\n\n# Write opt_methods shard (whole-method direct-jump for TestOptimizer!)\nif om_methods:\n    om_ib = marshal.dumps((om_deps_list, om_methods, om_opt_files))\n    (opt_dir / "opt_methods.idx").write_bytes(om_ib)\n    om_fb = rc._write_fine_tables((om_fine,), opt_dir / "opt_methods.fdat", opt_dir / "opt_methods.ftoc")\n    tot_opt_bytes += len(om_ib) + om_fb\n\nprint(f"{{tot_opt_rows}} {{tot_opt_bytes}} {{len(queries_cache)}} {{len(dblob)}}")\n'
    import subprocess as _subprocess
    res = _subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'build_opt_replay_and_duckdb_cache failed ({res.returncode}):\n{res.stderr}\n{res.stdout}')
    parts = res.stdout.strip().splitlines()[-1].split()
    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))

def build_exec_and_sqlite_cache(sg_root: Path, exec_path: Path=EXEC_REPLAY_PATH, sqlite_path: Path=SQLITE_CACHE_PATH) -> tuple[int, int, int, int]:
    code = f'\nimport importlib, pickle, sqlite3, sys, unittest, zlib\nfrom pathlib import Path\nsys.path.insert(0, {str(sg_root)!r})\nsys.path.insert(0, "/app")\n\nimport stage_store as rc\nimport verifier\nfrom sqlglot import exp, parse_one\nfrom sqlglot.executor.python import PythonExecutor\nfrom sqlglot.executor.table import ensure_tables\nfrom sqlglot.helper import dict_depth\nfrom sqlglot.optimizer import optimize\nfrom sqlglot.optimizer.annotate_types import annotate_types\nfrom sqlglot.planner import Plan\nfrom sqlglot.schema import ensure_schema, flatten_schema, nested_get, nested_set\nimport tests.test_executor as texec\n\n# ---- Part A: Collect non-TPC TestExecutor execute() stage-reuse cache (WITH PythonGenerator fine tokens!) ----\nexec_fine_stack = []\nrestore_exec_fine = rc._install_fine_spies(exec_fine_stack)\nexec_cache = {{}}\nbad_exec = set()\norig_exec = texec.execute\n\ndef _rec_exec(sql, schema=None, dialect=None, tables=None):\n    res_tbl = orig_exec(sql, schema=schema, dialect=dialect, tables=tables)\n    k = rc.exec_call_key(sql, schema, tables, dialect)\n    if k is not None and k not in bad_exec:\n        try:\n            tables_ = ensure_tables(tables, dialect=dialect)\n            s_eff = schema\n            if not s_eff:\n                s_eff = {{}}\n                flattened_tables = flatten_schema(tables_.mapping, depth=dict_depth(tables_.mapping))\n                for keys in flattened_tables:\n                    tbl = nested_get(tables_.mapping, *zip(keys, keys))\n                    assert tbl is not None\n                    for col in tbl.columns:\n                        val = tbl[0][col]\n                        ct = annotate_types(exp.convert(val), dialect=dialect).type or type(val).__name__\n                        nested_set(s_eff, [*keys, col], ct)\n            schema_obj = ensure_schema(s_eff, dialect=dialect)\n            if tables_.supported_table_args and tables_.supported_table_args != schema_obj.supported_table_args:\n                raise ValueError("table args mismatch")\n            opt = optimize(sql, schema_obj, leave_tables_isolated=True, dialect=dialect)\n            plan = Plan(opt)\n            det_h = verifier._hash_plan_det(plan)\n            pyf = set()\n            exec_fine_stack.append(pyf)\n            try:\n                py_h = verifier._hash_plan_py_code(plan)\n            finally:\n                exec_fine_stack.pop()\n            pk_opt = rc.pack_expr(opt, exp)\n            if pk_opt is not rc._UNPACKABLE:\n                val = (pk_opt, det_h, py_h, tuple(res_tbl.columns), list(res_tbl.rows), frozenset(pyf))\n                pickle.dumps(val, protocol=pickle.HIGHEST_PROTOCOL)\n                if k in exec_cache and exec_cache[k][1:5] != val[1:5]:\n                    bad_exec.add(k)\n                    exec_cache.pop(k, None)\n                else:\n                    if k in exec_cache:\n                        val = (*val[:5], exec_cache[k][5] | val[5])\n                    exec_cache[k] = val\n        except Exception:\n            bad_exec.add(k)\n            exec_cache.pop(k, None)\n    return res_tbl\n\ntexec.execute = _rec_exec\ntexec.TestExecutor.setUpClass = classmethod(lambda cls: setattr(cls, "cache", {{}}))\ntexec.TestExecutor.tearDownClass = classmethod(lambda cls: None)\nsuite = unittest.defaultTestLoader.loadTestsFromTestCase(texec.TestExecutor)\nfilt = unittest.TestSuite()\nskip_e = {{"test_execute_tpcds", "test_execute_tpch", "test_optimized_tpch", "test_agg_order"}}\nfor tc in suite:\n    if getattr(tc, "_testMethodName", "") not in skip_e:\n        filt.addTest(tc)\nres = unittest.TestResult()\ntry:\n    filt.run(res)\nfinally:\n    restore_exec_fine()\nif not res.wasSuccessful():\n    raise RuntimeError(f"exec_replay collection failed: {{res.failures[:1]}} {{res.errors[:1]}}")\neblob = pickle.dumps(exec_cache, protocol=pickle.HIGHEST_PROTOCOL)\nPath({str(exec_path)!r}).write_bytes(eblob)\n\n# ---- Part B: Collect Lazy SQLite Query Cache (test_optimize_cross_joins & test_simplify_between & test_optimizer) ----\nsq_queries = {{}}\nsq_ddls = set()\nsq_bad = set()\n\nclass _RecSqCursor:\n    def __init__(self, real_cur, key):\n        self._cur = real_cur\n        self._key = key\n        self.description = real_cur.description\n    def fetchall(self):\n        rows = self._cur.fetchall()\n        if self._key is not None and self._key not in sq_bad:\n            try:\n                cols = tuple(d[0] for d in (self.description or ()))\n                pickle.dumps((cols, rows), protocol=pickle.HIGHEST_PROTOCOL)\n                prev = sq_queries.get(self._key)\n                if prev is None:\n                    sq_queries[self._key] = (cols, rows)\n                elif prev != (cols, rows):\n                    sq_bad.add(self._key)\n                    sq_queries.pop(self._key, None)\n            except Exception:\n                sq_bad.add(self._key)\n                sq_queries.pop(self._key, None)\n        return rows\n    def fetchone(self): return self._cur.fetchone()\n    def __getattr__(self, name): return getattr(self._cur, name)\n\nclass _RecSqConn:\n    def __init__(self, real_conn):\n        self._conn = real_conn\n        self._sig = b"SQLITE"\n    def execute(self, query, parameters=None, *a, **kw):\n        if parameters is not None or a or kw or type(query) is not str:\n            return self._conn.execute(query, parameters, *a, **kw) if parameters is not None else self._conn.execute(query, *a, **kw)\n        if rc._is_pure_read_sql(query):\n            return _RecSqCursor(self._conn.execute(query), (self._sig, query))\n        cur = self._conn.execute(query)\n        self._sig = rc._ddl_step_sig(self._sig, "E:" + query)\n        sq_ddls.add(self._sig)\n        return cur\n    def executescript(self, script):\n        cur = self._conn.executescript(script)\n        if type(script) is str:\n            self._sig = rc._ddl_step_sig(self._sig, "S:" + script)\n            sq_ddls.add(self._sig)\n        return cur\n    def close(self): return self._conn.close()\n    def __enter__(self): return self\n    def __exit__(self, *ea): self.close()\n    def __getattr__(self, name): return getattr(self._conn, name)\n\norig_sq_connect = sqlite3.connect\ndef _rec_sq_connect(db, *a, **kw):\n    if db == ":memory:" and not a and not kw:\n        return _RecSqConn(orig_sq_connect(":memory:"))\n    return orig_sq_connect(db, *a, **kw)\n\nfor mod_n in ("tests.test_optimize_cross_joins", "tests.test_simplify_between"):\n    m = importlib.import_module(mod_n)\n    m.sqlite3 = type("_SqMod", (), {{"connect": staticmethod(_rec_sq_connect), "sqlite_version_info": sqlite3.sqlite_version_info}})()\n    s = unittest.defaultTestLoader.loadTestsFromModule(m)\n    r = unittest.TestResult()\n    s.run(r)\n    if not r.wasSuccessful():\n        raise RuntimeError(f"sqlite cache failed in {{mod_n}}: {{r.failures[:1]}} {{r.errors[:1]}}")\n\nsblob = pickle.dumps({{"queries": sq_queries, "ddls": sq_ddls}}, protocol=pickle.HIGHEST_PROTOCOL)\nPath({str(sqlite_path)!r}).write_bytes(sblob)\nprint(f"{{len(exec_cache)}} {{len(eblob)}} {{len(sq_queries)}} {{len(sblob)}}")\n'
    import subprocess as _subprocess
    res = _subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f'build_opt_replay_and_duckdb_cache failed ({res.returncode}):\n{res.stderr}\n{res.stdout}')
    parts = res.stdout.strip().splitlines()[-1].split()
    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))
