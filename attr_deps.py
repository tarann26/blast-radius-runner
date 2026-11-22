"""Dependency index built at image build time from the pinned sqlglot: which tests read which
class attributes, transform tables, dict keys, methods and module-level functions."""
from __future__ import annotations
import ast
import marshal
from pathlib import Path
from typing import Any
DEP_INDEX_PATH = Path('/app/attr_deps.marshal')
TRACKED_GEN_DICTS: frozenset[str] = frozenset({'TRANSFORMS', 'TYPE_MAPPING', 'PROPERTIES_LOCATION', 'TOKEN_MAPPING', 'TYPE_PARAM_SETTINGS', 'TIME_PART_SINGULARS', 'AFTER_HAVING_MODIFIER_TRANSFORMS'})
TRACKED_DIALECT_DICTS: frozenset[str] = frozenset({'EXPRESSION_METADATA', 'COLUMN_ALIAS_PRECEDENCE', 'DEFAULT_FUNCTIONS_COLUMN_NAMES', 'SET_OP_DISTINCT_BY_DEFAULT'})
TRACKED_PAR_DICTS: frozenset[str] = frozenset({'FUNCTIONS', 'FUNCTION_PARSERS', 'STATEMENT_PARSERS', 'EXPRESSION_PARSERS', 'PROPERTY_PARSERS', 'CONSTRAINT_PARSERS', 'PRIMARY_PARSERS', 'NO_PAREN_FUNCTIONS', 'NO_PAREN_FUNCTION_PARSERS', 'TYPE_CONVERTERS', 'TYPE_LITERAL_PARSERS', 'UNARY_PARSERS', 'CAST_ACTIONS', 'COLUMN_OPERATORS', 'COMPARISON', 'CONFLICT_ACTIONS', 'CONJUNCTION', 'DISJUNCTION', 'EQUALITY', 'EXPONENT', 'FACTOR', 'TERM', 'BITWISE', 'JSON_OPERATORS', 'SUBQUERY_PREDICATES', 'QUERY_MODIFIER_PARSERS', 'RANGE_PARSERS', 'SHOW_PARSERS', 'SET_PARSERS', 'ALTER_PARSERS', 'ALTER_ALTER_PARSERS', 'ANALYZE_EXPRESSION_PARSERS', 'ARRAY_CONSTRUCTORS', 'ASSIGNMENT', 'NUMERIC_PARSERS', 'PLACEHOLDER_PARSERS', 'STRING_PARSERS', 'KEY_CONSTRAINT_OPTIONS', 'WINDOW_EXCLUDE_OPTIONS', 'TRANSACTION_CHARACTERISTICS', 'TRIGGER_DEFERRABLE', 'TRIGGER_TIMING', 'EXECUTE_AS_OPTIONS', 'CREATE_SEQUENCE', 'ISOLATED_LOADING_OPTIONS', 'PROCEDURE_OPTIONS', 'SCHEMA_BINDING_OPTIONS', 'ODBC_DATETIME_LITERALS', 'PIPE_SYNTAX_TRANSFORM_PARSERS', 'SIGNED_TO_UNSIGNED_TYPE_TOKEN', 'USABLES'})
ALL_TRACKED_DICTS: frozenset[str] = TRACKED_GEN_DICTS | TRACKED_PAR_DICTS | TRACKED_DIALECT_DICTS
META_SKIP_ATTRS: frozenset[str] = frozenset({'BIT_STRINGS', 'BYTE_STRINGS', 'COMMENTS', 'HEREDOC_STRING', 'HEREDOC_STRINGS', 'HEX_STRINGS', 'HINT_START', 'IDENTIFIERS', 'NATIONAL_RAW_STRING', 'NATIONAL_RAW_STRINGS', 'NATIONAL_STRING', 'QUOTES', 'RAW_STRING', 'RAW_STRINGS', 'SINGLE_TOKENS', 'UNICODE_STRINGS', 'NUMERIC_LITERALS', 'NUMERIC_ESCAPES', 'KEYWORDS', 'VAR_SINGLE_TOKENS', 'COMMANDS', 'COMMAND_PREFIX_TOKENS', 'STRING_ESCAPES', 'BYTE_STRING_ESCAPES', 'IDENTIFIER_ESCAPES', 'SUPPORTED_JSON_PATH_PARTS', 'DATE_PART_MAPPING', 'TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING', 'CREATABLE_KIND_MAPPING', 'INVERSE_CREATABLE_KIND_MAPPING', 'UNESCAPED_SEQUENCES', 'ESCAPED_SEQUENCES', 'VALID_INTERVAL_UNITS', 'TIME_TRIE', 'FORMAT_TRIE', 'INVERSE_TIME_TRIE', 'INVERSE_FORMAT_TRIE', 'SET_TRIE', 'SHOW_TRIE', 'COERCES_TO'})
_GEN_MICRO_METHODS: frozenset[str] = frozenset({'__init__', 'generate', 'preprocess', 'sql', 'seg', 'sep', 'indent', 'expressions', 'wrap', 'pad_comment', 'maybe_comment', 'sanitize_comment', '_replace_line_breaks', 'func', 'binary', 'unary', 'format_args', 'normalize_func', 'escape_str', 'format_time', 'unsupported', '_warn_unsupported_arg', '_move_ctes_to_top_level', 'modifiers', 'properties', 'locate_properties', 'comments_sql', 'identifier_sql', 'column_sql', 'literal_sql', 'table_sql', 'alias_sql', 'select_sql', 'from_sql', 'where_sql', 'and_sql', 'or_sql', 'eq_sql', 'paren_sql', 'star_sql', 'var_sql', 'datatype_sql', 'anonymous_sql'})
_PAR_MICRO_METHODS: frozenset[str] = frozenset({'__init__', 'parse', 'parse_into', '_parse', 'reset', '_reset', '_advance', '_retreat', '_match', '_match_set', '_match_pair', '_match_text_seq', '_match_texts', '_match_l_paren', '_match_r_paren', '_expect', '_curr', '_next', '_prev', '_index', 'expression', 'validate_expression', 'raise_error', 'check_errors', '_find_sql', '_parse_statement', '_parse_expression', '_parse_conjunction', '_parse_disjunction', '_parse_equality', '_parse_comparison', '_parse_range', '_parse_bitwise', '_parse_term', '_parse_factor', '_parse_unary', '_parse_type', '_parse_types', '_parse_column', '_parse_column_ops', '_parse_column_def', '_parse_primary', '_parse_field', '_parse_id_var', '_parse_var', '_parse_identifier', '_parse_alias', '_parse_csv', '_parse_wrapped', '_parse_wrapped_csv', '_parse_wrapped_id_vars', '_parse_tokens', '_parse_select', '_parse_projections', '_parse_from', '_parse_where', '_parse_table', '_parse_table_parts', '_parse_function', '_parse_function_args', '_parse_number', '_parse_string', '_parse_placeholder', '_parse_null', '_parse_boolean', '_parse_star', '_parse_parens', '_parse_binary', '_parse_set_operations', '_parse_joins', '_parse_join', '_parse_order', '_parse_ordered', '_parse_group', '_parse_having', '_parse_limit', '_parse_offset', '_parse_cte', '_parse_with', '_parse_subquery', '_parse_derived_table_values', '_parse_dot'})
_OPT_HOT_FUNCS: frozenset[str] = frozenset({'is_true', 'is_false', 'is_null', 'is_zero', 'is_complement', 'always_true', 'always_false', 'flatten', 'uniq_sort', 'walk_in_scope', 'find_in_scope', 'find_all_in_scope', '_simplify', 'simplify', 'traverse_scope', 'build_scope', '_build_scope'})
_CELL_TYPE = type((lambda x: lambda: x)(0).__closure__[0])
ALWAYS_KEEP_TEST_FILES: frozenset[str] = frozenset({'tests/test_dialect_imports.py', 'tests/test_dialect_entry_points.py', 'tests/test_integration_loader.py', 'tests/test_sqlglotc.py'})
ALWAYS_KEEP_TEST_IDS: frozenset[str] = frozenset({'tests/dialects/test_dialect.py::TestDialect::test_enum', 'tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata'})
_SEPARATELY_HANDLED_TEST_IDS: frozenset[str] = frozenset({'tests/dialects/test_dialect.py::TestDialect::test_lazy_load'})
_ENUM_CLS: type | None = None
_IMPORT_CALLED_CACHE: dict[str, tuple[frozenset[str], frozenset[tuple[str, str]]]] = {}

def _cls_key(cls: type) -> str:
    qn = cls.__qualname__
    return qn if '.' in qn and '<locals>' not in qn else cls.__name__

def _scan_import_time_funcs(sg_root: Path) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    key = str(sg_root)
    cached = _IMPORT_CALLED_CACHE.get(key)
    if cached is not None:
        return cached
    called_at_import: set[str] = set()
    factories: set[tuple[str, str]] = set()
    sg_pkg = sg_root / 'sqlglot'

    def _collect_import_calls(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for dec in getattr(node, 'decorator_list', ()):
                _collect_import_calls(dec)
            for d in getattr(node.args, 'defaults', ()):
                _collect_import_calls(d)
            for d in getattr(node.args, 'kw_defaults', ()):
                if d is not None:
                    _collect_import_calls(d)
            return
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called_at_import.add(f.id)
            elif isinstance(f, ast.Attribute):
                called_at_import.add(f.attr)
        for ch in ast.iter_child_nodes(node):
            _collect_import_calls(ch)

    def _is_factory_func(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        inner_def_names: set[str] = set()
        for sub in ast.walk(fn_node):
            if sub is not fn_node and isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner_def_names.add(sub.name)
        for st in ast.walk(fn_node):
            if isinstance(st, ast.Return) and st.value is not None:
                v = st.value
                if isinstance(v, ast.Lambda):
                    return True
                if isinstance(v, ast.Name) and v.id in inner_def_names:
                    return True
                if isinstance(v, ast.Call):
                    cf = v.func
                    cname = cf.id if isinstance(cf, ast.Name) else cf.attr if isinstance(cf, ast.Attribute) else ''
                    if cname in ('partial', 'preprocess'):
                        return True
        return False
    if sg_pkg.is_dir():
        for pyf in sg_pkg.rglob('*.py'):
            try:
                tree = ast.parse(pyf.read_text(encoding='utf-8', errors='replace'))
            except Exception:
                continue
            _collect_import_calls(tree)
            rel = str(pyf.relative_to(sg_root))
            mod_name = rel.removesuffix('/__init__.py').removesuffix('.py').replace('/', '.')
            for stmt in tree.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_factory_func(stmt):
                        factories.add((mod_name, stmt.name))
    res = (frozenset(called_at_import), frozenset(factories))
    _IMPORT_CALLED_CACHE[key] = res
    return res

def canon_runtime_key(k: Any, exp_expr_cls: type) -> str | None:
    global _ENUM_CLS
    tk = type(k)
    if tk is str:
        return f'S:{k}'
    if tk is int:
        return f'I:{k}'
    if tk is bool:
        return f'B:{int(k)}'
    if isinstance(k, type) and issubclass(k, exp_expr_cls):
        return f'E:{k.__name__}'
    if _ENUM_CLS is None:
        from enum import Enum
        _ENUM_CLS = Enum
    if isinstance(k, _ENUM_CLS):
        en = 'DType' if tk.__name__ in ('Type', 'DType') else tk.__name__
        return f'M:{en}.{k.name}'
    return None

def canon_source_key(kr: str, all_expr_classes: set[str]) -> str | None:
    s = kr.strip()
    if not s:
        return None
    if s[0] == "'" and s[-1] == "'" or (s[0] == '"' and s[-1] == '"'):
        try:
            val = ast.literal_eval(s)
            if type(val) is str:
                return f'S:{val}'
        except Exception:
            return None
    if s in ('True', 'False'):
        return f"B:{(1 if s == 'True' else 0)}"
    if s.isdigit() or (s[0] == '-' and s[1:].isdigit()):
        return f'I:{int(s)}'
    parts = s.split('.')
    last = parts[-1]
    if len(parts) == 2 and parts[0] == 'exp' and (last in all_expr_classes):
        return f'E:{last}'
    if len(parts) == 1 and last in all_expr_classes:
        return f'E:{last}'
    if len(parts) >= 2 and last.isupper():
        en = parts[-2]
        if en in ('Type', 'DType') and ('DataType' in parts or 'DType' in parts):
            en = 'DType'
        return f'M:{en}.{last}'
    return None

class _BoundSpyDict(dict):
    __slots__ = ('_raw', '_recv', '_attr', '_on_key', '_exp_cls')

    def __init__(self, raw: dict, recv: str, attr: str, on_key: Any, exp_cls: type) -> None:
        super().__init__(raw)
        self._raw = raw
        self._recv = recv
        self._attr = attr
        self._on_key = on_key
        self._exp_cls = exp_cls

    def _rec(self, k: Any) -> None:
        ck = canon_runtime_key(k, self._exp_cls)
        self._on_key(self._recv, self._attr, ck if ck is not None else '*')

    def _rec_all(self) -> None:
        self._on_key(self._recv, self._attr, '*')

    def __getitem__(self, k: Any) -> Any:
        self._rec(k)
        return self._raw[k]

    def __setitem__(self, k: Any, v: Any) -> None:
        self._rec(k)
        self._raw[k] = v
        super().__setitem__(k, v)

    def __delitem__(self, k: Any) -> None:
        self._rec(k)
        del self._raw[k]
        super().pop(k, None)

    def pop(self, k: Any, *args: Any) -> Any:
        self._rec(k)
        super().pop(k, None)
        return self._raw.pop(k, *args)

    def popitem(self) -> tuple[Any, Any]:
        self._rec_all()
        k, v = self._raw.popitem()
        super().pop(k, None)
        return (k, v)

    def setdefault(self, k: Any, default: Any=None) -> Any:
        self._rec(k)
        if k not in self._raw:
            self._raw[k] = default
            super().__setitem__(k, default)
        return self._raw[k]

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._rec_all()
        for a in args:
            if isinstance(a, _BoundSpyDict):
                a._rec_all()
        self._raw.update(*args, **kwargs)
        super().update(*args, **kwargs)

    def clear(self) -> None:
        self._rec_all()
        self._raw.clear()
        super().clear()

    def get(self, k: Any, default: Any=None) -> Any:
        self._rec(k)
        return self._raw.get(k, default)

    def __contains__(self, k: Any) -> bool:
        self._rec(k)
        return k in self._raw

    def __iter__(self):
        self._rec_all()
        return iter(self._raw)

    def keys(self):
        self._rec_all()
        return self._raw.keys()

    def values(self):
        self._rec_all()
        return self._raw.values()

    def items(self):
        self._rec_all()
        return self._raw.items()

    def copy(self) -> dict:
        self._rec_all()
        return self._raw.copy()

    def __len__(self) -> int:
        self._rec_all()
        return len(self._raw)

    def __bool__(self) -> bool:
        self._rec_all()
        return bool(self._raw)

    def __eq__(self, other: Any) -> bool:
        self._rec_all()
        if isinstance(other, _BoundSpyDict):
            other._rec_all()
            return self._raw == other._raw
        return self._raw == other

    def __ne__(self, other: Any) -> bool:
        self._rec_all()
        if isinstance(other, _BoundSpyDict):
            other._rec_all()
            return self._raw != other._raw
        return self._raw != other

    def __repr__(self) -> str:
        self._rec_all()
        return repr(self._raw)

    def __or__(self, other: Any) -> dict:
        self._rec_all()
        if isinstance(other, _BoundSpyDict):
            other._rec_all()
            return self._raw | other._raw
        return self._raw | other

    def __ror__(self, other: Any) -> dict:
        self._rec_all()
        if isinstance(other, _BoundSpyDict):
            other._rec_all()
            return other._raw | self._raw
        return other | self._raw

class _AttrSpy:
    __slots__ = ('_attr', '_val', '_is_dict', '_on_attr', '_on_key', '_exp_cls', '_bound_cache')

    def __init__(self, attr: str, val: Any, is_dict: bool, on_attr: Any, on_key: Any, exp_cls: type) -> None:
        self._attr = attr
        self._val = val
        self._is_dict = is_dict
        self._on_attr = on_attr
        self._on_key = on_key
        self._exp_cls = exp_cls
        self._bound_cache: dict[str, _BoundSpyDict] | None = {} if is_dict else None

    def __get__(self, obj: Any, owner: type | None=None) -> Any:
        cls_ref = obj.__class__ if obj is not None else owner
        recv = _cls_key(cls_ref) if cls_ref is not None else ''
        if self._is_dict:
            bc = self._bound_cache
            bd = bc.get(recv)
            if bd is None:
                bd = _BoundSpyDict(self._val, recv, self._attr, self._on_key, self._exp_cls)
                bc[recv] = bd
            return bd
        self._on_attr(recv, self._attr)
        return self._val

def _is_preprocess_list(lst: list) -> bool:
    import gc
    import inspect
    import types
    lrefs = []
    for r in gc.get_referrers(lst):
        if not isinstance(r, types.FrameType):
            lrefs.append(r)
    if not lrefs:
        return False
    for c in lrefs:
        if type(c) is not _CELL_TYPE:
            return False
        crefs = []
        for x in gc.get_referrers(c):
            if not isinstance(x, types.FrameType):
                crefs.append(x)
        if not crefs:
            return False
        for tup in crefs:
            if type(tup) is not tuple:
                return False
            trefs = []
            for f in gc.get_referrers(tup):
                if not isinstance(f, types.FrameType):
                    trefs.append(f)
            if not trefs:
                return False
            for fn in trefs:
                if not inspect.isfunction(fn) or getattr(fn, '__module__', '') != 'sqlglot.transforms' or getattr(fn, '__qualname__', '') != 'preprocess.<locals>._to_sql':
                    return False
    return True

def _audit_func_refs(orig_fn: Any, allowed_plain_dicts: dict[int, dict]) -> tuple[bool, list[tuple[dict, Any]], list[tuple[list, int]], list[tuple[type, str]], list[tuple[Any, list[tuple[dict, Any]]]]]:
    import functools
    import gc
    import types
    raw_refs = gc.get_referrers(orig_fn)
    plan_dicts: list[tuple[dict, Any]] = []
    plan_lists: list[tuple[list, int]] = []
    plan_cls: list[tuple[type, str]] = []
    plan_partials: list[tuple[Any, list[tuple[dict, Any]]]] = []
    safe = True
    for r in raw_refs:
        if isinstance(r, types.FrameType):
            continue
        tr = type(r)
        if tr is dict:
            has_key_ref = False
            for k in r.keys():
                if k is orig_fn:
                    has_key_ref = True
                    break
            if has_key_ref:
                safe = False
                break
            if id(r) in allowed_plain_dicts:
                matched_k: list[Any] = []
                for k, v in r.items():
                    if v is orig_fn:
                        matched_k.append(k)
                if not matched_k:
                    safe = False
                    break
                for k in matched_k:
                    plan_dicts.append((r, k))
            else:
                owners: list[type] = []
                for x in gc.get_referrers(r):
                    if isinstance(x, type) and getattr(x, '__module__', '').startswith('sqlglot.'):
                        owners.append(x)
                matched_k2: list[Any] = []
                for k, v in r.items():
                    if v is orig_fn:
                        matched_k2.append(k)
                all_match = len(owners) == 1 and bool(matched_k2)
                if all_match:
                    owner_dict = owners[0].__dict__
                    for k in matched_k2:
                        if owner_dict.get(k) is not orig_fn:
                            all_match = False
                            break
                if all_match:
                    for k in matched_k2:
                        plan_cls.append((owners[0], k))
                else:
                    safe = False
                    break
        elif tr is list:
            if _is_preprocess_list(r):
                idxs: list[int] = []
                for i, v in enumerate(r):
                    if v is orig_fn:
                        idxs.append(i)
                if not idxs:
                    safe = False
                    break
                for i in idxs:
                    plan_lists.append((r, i))
            else:
                safe = False
                break
        elif isinstance(r, functools.partial) and r.func is orig_fn:
            bad_p = False
            for a in r.args:
                if a is orig_fn:
                    bad_p = True
                    break
            if not bad_p:
                for v in r.keywords.values():
                    if v is orig_fn:
                        bad_p = True
                        break
            if bad_p:
                safe = False
                break
            prefs: list[Any] = []
            for x in gc.get_referrers(r):
                if not isinstance(x, types.FrameType):
                    prefs.append(x)
            if not prefs:
                safe = False
                break
            p_binds: list[tuple[dict, Any]] = []
            for pr in prefs:
                if type(pr) is not dict or id(pr) not in allowed_plain_dicts:
                    safe = False
                    break
                pr_key_ref = False
                for k in pr.keys():
                    if k is r:
                        pr_key_ref = True
                        break
                if pr_key_ref:
                    safe = False
                    break
                pk_list: list[Any] = []
                for k, v in pr.items():
                    if v is r:
                        pk_list.append(k)
                if not pk_list:
                    safe = False
                    break
                for pk in pk_list:
                    p_binds.append((pr, pk))
            if not safe:
                break
            plan_partials.append((r, p_binds))
        else:
            safe = False
            break
    del raw_refs
    return (safe and bool(plan_dicts), plan_dicts, plan_lists, plan_cls, plan_partials)

def install_spies(on_attr: Any, on_key: Any, on_gen_dispatch: Any, on_method: Any | None=None, on_func: Any | None=None, sg_root: Path | None=None) -> tuple[set[str], set[str], set[tuple[str, str]], set[tuple[str, str]], Any]:
    import functools
    import gc
    import importlib
    import inspect
    import sys
    import sqlglot
    from sqlglot import Dialect, Dialects, exp
    from sqlglot.generator import Generator, _DISPATCH_CACHE, _build_dispatch
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer
    if sg_root is None:
        sg_file = getattr(sqlglot, '__file__', None)
        if sg_file:
            sg_root = Path(sg_file).resolve().parent.parent
    all_classes: set[type] = set()
    for d in Dialects:
        d_inst = Dialect.get_or_raise(d)
        dc = type(d_inst)
        for base_cls in (dc, dc.parser_class, dc.generator_class, dc.tokenizer_class):
            for c in base_cls.__mro__:
                if c.__module__.startswith('sqlglot.'):
                    all_classes.add(c)
        gcls = dc.generator_class
        if gcls not in _DISPATCH_CACHE:
            _DISPATCH_CACHE[gcls] = _build_dispatch(gcls)
    if Generator not in _DISPATCH_CACHE:
        _DISPATCH_CACHE[Generator] = _build_dispatch(Generator)
    try:
        from sqlglot.generators.python import PythonGenerator
        for c in PythonGenerator.__mro__:
            if c.__module__.startswith('sqlglot.'):
                all_classes.add(c)
        if PythonGenerator not in _DISPATCH_CACHE:
            _DISPATCH_CACHE[PythonGenerator] = _build_dispatch(PythonGenerator)
    except Exception:
        pass
    _sub_stack: list[type] = [Dialect, Parser, Generator, Tokenizer]
    while _sub_stack:
        _cur = _sub_stack.pop()
        if _cur.__module__.startswith('sqlglot.'):
            all_classes.add(_cur)
        for _sc in _cur.__subclasses__():
            if _sc not in all_classes:
                _sub_stack.append(_sc)
    for _cls_g in list(all_classes):
        if issubclass(_cls_g, Generator) and _cls_g not in _DISPATCH_CACHE:
            _DISPATCH_CACHE[_cls_g] = _build_dispatch(_cls_g)
    if sg_root is not None:
        for subpkg in ('typing', 'optimizer'):
            pdir = sg_root / 'sqlglot' / subpkg
            if pdir.is_dir():
                for pyf in sorted(pdir.glob('*.py')):
                    if pyf.name.startswith('__'):
                        continue
                    try:
                        importlib.import_module(f'sqlglot.{subpkg}.{pyf.stem}')
                    except Exception:
                        pass
    wrapped_funcs: set[tuple[str, str]] = set()
    saved_dict_entries: list[tuple[dict, Any, Any]] = []
    saved_list_entries: list[tuple[list, int, Any]] = []
    saved_cls_entries: list[tuple[type, str, Any]] = []
    called_at_import, factory_funcs = _scan_import_time_funcs(sg_root) if sg_root is not None else (frozenset(), frozenset())
    allowed_plain_dicts: dict[int, dict] = {}
    for mname, mobj in list(sys.modules.items()):
        if mobj is not None and mname.startswith('sqlglot.'):
            md = getattr(mobj, '__dict__', None)
            if type(md) is dict:
                allowed_plain_dicts[id(md)] = md
                em = md.get('EXPRESSION_METADATA')
                if type(em) is dict:
                    allowed_plain_dicts[id(em)] = em
                    for inner in em.values():
                        if type(inner) is dict:
                            allowed_plain_dicts[id(inner)] = inner
    for cls in all_classes:
        for attr_v in cls.__dict__.values():
            if type(attr_v) is dict:
                allowed_plain_dicts[id(attr_v)] = attr_v
                for inner in attr_v.values():
                    if type(inner) is dict:
                        allowed_plain_dicts[id(inner)] = inner
    for dmap in _DISPATCH_CACHE.values():
        if type(dmap) is dict:
            allowed_plain_dicts[id(dmap)] = dmap

    def _is_target_func_mod(mn: str) -> bool:
        if mn.startswith('sqlglot.parsers.') and mn != 'sqlglot.parsers.base':
            return True
        if mn.startswith('sqlglot.generators.'):
            return True
        if mn.startswith('sqlglot.dialects.') and mn not in ('sqlglot.dialects.dialect', 'sqlglot.dialects.__init__'):
            return True
        if mn.startswith('sqlglot.typing.'):
            return True
        if mn.startswith('sqlglot.optimizer.') and mn not in ('sqlglot.optimizer.optimizer', 'sqlglot.optimizer.__init__'):
            return True
        return False

    def _make_fwrap(_orig, _mn, _fn):

        @functools.wraps(_orig)
        def _fwrap(*args, **kwargs):
            on_func(_mn, _fn)
            return _orig(*args, **kwargs)
        _fwrap._blast_fwrapped = True
        return _fwrap
    gc.collect()
    target_mod_names = sorted((mn for mn in list(sys.modules.keys()) if _is_target_func_mod(mn)))
    for mn in target_mod_names:
        mod_obj = sys.modules.get(mn)
        if mod_obj is None:
            continue
        is_opt_mod = mn.startswith('sqlglot.optimizer.')
        md_cur = mod_obj.__dict__
        fn_names: list[str] = []
        for k in list(md_cur.keys()):
            v = md_cur.get(k)
            if inspect.isfunction(v) and getattr(v, '__module__', None) == mn and (getattr(v, '__name__', None) == k) and (not getattr(v, '_blast_fwrapped', False)) and (not (k.startswith('__') and k.endswith('__'))):
                fn_names.append(k)
        del v
        for fn_name in fn_names:
            if fn_name in called_at_import or (mn, fn_name) in factory_funcs:
                continue
            if is_opt_mod and fn_name in _OPT_HOT_FUNCS:
                continue
            orig_fn = md_cur.get(fn_name)
            if not inspect.isfunction(orig_fn):
                continue
            ok_fn, plan_dicts, plan_lists, plan_cls, plan_partials = _audit_func_refs(orig_fn, allowed_plain_dicts)
            if not ok_fn:
                continue
            wrapped_funcs.add((mn, fn_name))
            if on_func is not None:
                f_wrapped = _make_fwrap(orig_fn, mn, fn_name)
                for d_obj, k in plan_dicts:
                    saved_dict_entries.append((d_obj, k, orig_fn))
                    d_obj[k] = f_wrapped
                for l_obj, idx in plan_lists:
                    saved_list_entries.append((l_obj, idx, orig_fn))
                    l_obj[idx] = f_wrapped
                for c_obj, cname_k in plan_cls:
                    saved_cls_entries.append((c_obj, cname_k, orig_fn))
                    setattr(c_obj, cname_k, f_wrapped)
                for p_obj, p_binds in plan_partials:
                    new_p = functools.partial(f_wrapped, *p_obj.args, **p_obj.keywords)
                    for pr, pk in p_binds:
                        saved_dict_entries.append((pr, pk, p_obj))
                        pr[pk] = new_p
    saved_attrs: list[tuple[type, str, Any]] = []
    saved_method_dict_entries: list[tuple[dict[Any, Any], Any, Any]] = []
    tracked_attrs: set[str] = set()
    tracked_dicts: set[str] = set()
    raw_tracked_dicts: list[dict[Any, Any]] = []
    for cls in all_classes:
        is_gen = issubclass(cls, Generator)
        is_par = issubclass(cls, Parser)
        is_dial = issubclass(cls, Dialect)
        for name, val in list(cls.__dict__.items()):
            if not name.isupper() or name.startswith('_') or name in META_SKIP_ATTRS:
                continue
            if callable(val) or isinstance(val, (classmethod, staticmethod, property, type)):
                continue
            if isinstance(val, dict):
                if is_gen and name in TRACKED_GEN_DICTS or (is_par and name in TRACKED_PAR_DICTS) or (is_dial and name in TRACKED_DIALECT_DICTS):
                    saved_attrs.append((cls, name, val))
                    raw_tracked_dicts.append(val)
                    setattr(cls, name, _AttrSpy(name, val, True, on_attr, on_key, exp.Expr))
                    tracked_dicts.add(name)
            else:
                saved_attrs.append((cls, name, val))
                setattr(cls, name, _AttrSpy(name, val, False, on_attr, on_key, exp.Expr))
                tracked_attrs.add(name)
    wrapped_methods: set[tuple[str, str]] = set()
    for cls in all_classes:
        mod = cls.__module__
        is_core_gen = cls is Generator
        is_core_par = cls is Parser
        is_sub_pg = mod.startswith('sqlglot.parsers.') and mod != 'sqlglot.parsers.base' or mod.startswith('sqlglot.generators.')
        is_sub_dial = mod.startswith('sqlglot.dialects.') and mod not in ('sqlglot.dialects.dialect', 'sqlglot.dialects.__init__')
        if not (is_sub_pg or is_sub_dial or is_core_gen or is_core_par):
            continue
        cname = _cls_key(cls)
        skip_m = _GEN_MICRO_METHODS if is_core_gen else _PAR_MICRO_METHODS if is_core_par else frozenset()
        for mname, mval in list(cls.__dict__.items()):
            if mname != '__init__' and mname.startswith('__') and mname.endswith('__'):
                continue
            if mname in skip_m:
                continue
            if inspect.isfunction(mval):
                wrapped_methods.add((cname, mname))
                if on_method is not None:
                    saved_attrs.append((cls, mname, mval))

                    def _make_mwrap(orig_fn, _cn, _mn):

                        def _mwrap(*args, **kwargs):
                            on_method(_cn, _mn)
                            return orig_fn(*args, **kwargs)
                        _mwrap.__name__ = orig_fn.__name__
                        _mwrap.__qualname__ = orig_fn.__qualname__
                        return _mwrap
                    wrapped = _make_mwrap(mval, cname, mname)
                    setattr(cls, mname, wrapped)
                    for dmap in _DISPATCH_CACHE.values():
                        for ek, fn in list(dmap.items()):
                            if fn is mval:
                                dmap[ek] = wrapped
                    for rdict in raw_tracked_dicts:
                        for dk, dv in list(rdict.items()):
                            if dv is mval:
                                saved_method_dict_entries.append((rdict, dk, mval))
                                rdict[dk] = wrapped
    orig_gen_sql = Generator.sql

    def _spy_gen_sql(self, expression, key=None, comment=True):
        if not expression:
            return ''
        if isinstance(expression, str):
            return expression
        if key:
            value = expression.args.get(key)
            if value:
                return self.sql(value)
            return ''
        on_gen_dispatch(_cls_key(self.__class__), expression.__class__.__name__)
        return orig_gen_sql(self, expression, None, comment)
    Generator.sql = _spy_gen_sql

    def _restore() -> None:
        Generator.sql = orig_gen_sql
        for rdict, dk, orig_v in reversed(saved_method_dict_entries):
            rdict[dk] = orig_v
        for cls, name, val in reversed(saved_attrs):
            setattr(cls, name, val)
        for c_obj, cname_k, orig_v in reversed(saved_cls_entries):
            setattr(c_obj, cname_k, orig_v)
        for l_obj, idx, orig_v in reversed(saved_list_entries):
            l_obj[idx] = orig_v
        for d_obj, k, orig_v in reversed(saved_dict_entries):
            d_obj[k] = orig_v
        _DISPATCH_CACHE.clear()
    return (tracked_attrs, tracked_dicts, wrapped_methods, wrapped_funcs, _restore)

def _collect_dep_worker(args: tuple[str, list[str], dict[str, int]]) -> dict[str, Any]:
    import importlib
    import sys
    import unittest
    sg_root, test_files, tid_to_mtid = args
    sys.path.insert(0, sg_root)
    sys.path.insert(0, '/app')
    cur_mtid = [-1]
    setup_attr: set[tuple[str, str]] = set()
    setup_key: set[tuple[str, str, str]] = set()
    setup_disp: set[tuple[str, str]] = set()
    attr_reads: dict[tuple[str, str], set[int]] = {}
    dict_reads: dict[tuple[str, str, str], set[int]] = {}
    gen_dispatch: dict[tuple[str, str], set[int]] = {}
    seen_mtids: set[int] = set()
    failed_mtids: set[int] = set()

    def _on_attr(recv: str, attr: str) -> None:
        t = cur_mtid[0]
        if t >= 0:
            s = attr_reads.get((recv, attr))
            if s is None:
                attr_reads[recv, attr] = {t}
            else:
                s.add(t)
        elif t == -2:
            setup_attr.add((recv, attr))

    def _on_key(recv: str, attr: str, ck: str) -> None:
        t = cur_mtid[0]
        if t >= 0:
            k = (recv, attr, ck)
            s = dict_reads.get(k)
            if s is None:
                dict_reads[k] = {t}
            else:
                s.add(t)
        elif t == -2:
            setup_key.add((recv, attr, ck))

    def _on_disp(recv: str, ecls: str) -> None:
        t = cur_mtid[0]
        if t >= 0:
            k = (recv, ecls)
            s = gen_dispatch.get(k)
            if s is None:
                gen_dispatch[k] = {t}
            else:
                s.add(t)
        elif t == -2:
            setup_disp.add((recv, ecls))
    tracked_attrs, tracked_dicts, wrapped_methods, wrapped_funcs, restore = install_spies(_on_attr, _on_key, _on_disp, sg_root=Path(sg_root))
    import duckdb
    import tests.helpers as th
    from sqlglot import Dialect, Dialects, exp, parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from sqlglot.schema import MappingSchema, ensure_schema
    try:
        for tf in test_files:
            if tf in ALWAYS_KEEP_TEST_FILES:
                continue
            mod_name = tf.removesuffix('.py').replace('/', '.')
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            if tf == 'tests/test_optimizer.py':
                import tests.test_optimizer as topt
                opt_db = Path('/app/duckdb_opt.db')

                @classmethod
                def _fast_opt_su(cls):
                    import sqlglot
                    cls.pool = None
                    sqlglot.schema = MappingSchema()
                    cls.conn = duckdb.connect(str(opt_db), read_only=True, config={'threads': 1})
                topt.TestOptimizer.setUpClass = _fast_opt_su

                def _fast_cf(self, file, func, pretty=False, execute=None, only=None, **kwargs):
                    import json
                    for i, (meta, sql, expected) in enumerate(th.load_sql_fixture_pairs(f'optimizer/{file}.sql'), start=1):
                        title = meta.get('title') or f'{i}, {sql}'
                        if only and title != only:
                            continue
                        dialect = meta.get('dialect')
                        fkw = kwargs.copy()
                        if (schema := meta.get('schema')):
                            fkw['schema'] = json.loads(schema)
                        if (v := meta.get('leave_tables_isolated')) is not None:
                            fkw['leave_tables_isolated'] = th.string_to_bool(v)
                        if (v := meta.get('validate_qualify_columns')) is not None:
                            fkw['validate_qualify_columns'] = th.string_to_bool(v)
                        if dialect:
                            fkw['dialect'] = dialect
                        if (v := meta.get('canonicalize_table_aliases')) is not None:
                            fkw['canonicalize_table_aliases'] = th.string_to_bool(v)
                        optimized = func(parse_one(sql, read=dialect), **fkw)
                        optimized.sql(pretty=pretty, dialect=dialect)
                        fixture_execute = False if execute is False else meta.get('execute', execute)
                        if th.string_to_bool(fixture_execute):
                            for ex in (parse_one(sql, read=dialect), optimized):
                                if not isinstance(ex, (exp.Query, exp.Values)):
                                    ex = exp.select(exp.alias_(ex, '_col_0'))
                                ex.sql(dialect='duckdb')
                topt.TestOptimizer.check_file = _fast_cf

                def _fast_in_nulls_dep(self):
                    for where in ('', ' WHERE y.b IS NOT NULL', ' WHERE y.b IS NULL', ' WHERE FALSE', ' WHERE y.c = x.a', ' WHERE y.b = x.a'):
                        for pred in (f'x.a IN (SELECT y.b FROM y{where})', f'x.a = ANY (SELECT y.b FROM y{where})'):
                            for sql in (f'SELECT x.a, {pred} AS r FROM x', f'SELECT x.a, NOT ({pred}) AS r FROM x', f'SELECT x.a, ({pred}) AND x.a > 1 AS r FROM x', f'SELECT x.a, ({pred}) OR x.a > 1 AS r FROM x', f'SELECT x.a FROM x WHERE NOT ({pred})', f'SELECT x.a FROM x WHERE ({pred}) IS NULL', f'SELECT x.a FROM x WHERE COALESCE(({pred}), TRUE)', f'SELECT x.a FROM x WHERE ({pred}) = FALSE', f'SELECT x.a FROM x JOIN x AS x2 ON x.b = x2.b AND ({pred}) = FALSE', f'SELECT x.a FROM x QUALIFY ROW_NUMBER() OVER (ORDER BY x.a) > 0 AND ({pred}) = FALSE', f'SELECT x.a FROM x GROUP BY x.a HAVING ({pred}) IS NULL', f'SELECT x.a FROM x ORDER BY ({pred}), x.a'):
                                topt.optimizer.optimize(sql, schema=self.schema, dialect='duckdb').sql('duckdb')
                topt.TestOptimizer.test_optimize_in_nulls = _fast_in_nulls_dep

                def _fast_corr_dep(self):
                    for col in ('b', 'c'):
                        for op in ('=', '<>', '>', '>=', '<', '<='):
                            pred = f'x.a {op} ALL (SELECT y.{col} FROM y WHERE y.c = x.b)'
                            for sql in (f'SELECT x.a, x.b, {pred} AS r FROM x', f'SELECT x.a, x.b FROM x WHERE {pred}', f'SELECT x.a, x.b FROM x WHERE NOT ({pred})'):
                                topt.optimizer.optimize(sql, schema=self.schema, dialect='duckdb').sql('duckdb')
                topt.TestOptimizer.test_optimize_correlated_all = _fast_corr_dep
                orig_scope = topt.TestOptimizer.test_scope

                def _slim_scope(self):
                    op = topt.parse_one
                    topt.parse_one = lambda s, *a, **kw: op(' UNION ALL '.join(['SELECT x FROM t'] * 20), *a, **kw) if isinstance(s, str) and len(s) > 50000 else op(s, *a, **kw)
                    try:
                        orig_scope(self)
                    finally:
                        topt.parse_one = op
                topt.TestOptimizer.test_scope = _slim_scope
            elif tf == 'tests/test_executor.py':
                import tests.test_executor as texec
                from sqlglot.generators.python import PythonGenerator

                @classmethod
                def _fast_exec_su(cls):
                    cls.conn = duckdb.connect(':memory:', config={'threads': 1})
                    cls.tpch_tables = {}
                    cls.tpcds_tables = {}
                    cls.cache = {}
                texec.TestExecutor.setUpClass = _fast_exec_su
                texec.TestExecutor.tearDownClass = classmethod(lambda cls: cls.conn.close())

                def _fast_tpcds_dep(self):
                    s_ds = ensure_schema(th.TPCDS_SCHEMA)
                    pg = PythonGenerator()
                    for args, sql, _ in th.load_sql_fixture_pairs('optimizer/tpc-ds/tpc-ds.sql'):
                        expr = parse_one(sql)
                        if args.get('execute'):
                            opt = optimize(expr, s_ds, leave_tables_isolated=True)
                            plan = Plan(opt)
                            for step in plan.dag:
                                for attr_v in step.__dict__.values():
                                    if isinstance(attr_v, exp.Expr):
                                        pg.generate(attr_v)
                                    elif isinstance(attr_v, list):
                                        for item in attr_v:
                                            if isinstance(item, exp.Expr):
                                                pg.generate(item)
                                    elif isinstance(attr_v, dict):
                                        for item in attr_v.values():
                                            if isinstance(item, exp.Expr):
                                                pg.generate(item)
                            transpile(sql, write='duckdb')

                def _fast_tpch_dep(self):
                    s_h = ensure_schema(th.TPCH_SCHEMA)
                    for args, sql, _ in th.load_sql_fixture_pairs('optimizer/tpc-h/tpc-h.sql'):
                        expr = parse_one(sql)
                        optimize(expr, s_h)
                        transpile(sql, write='duckdb')
                texec.TestExecutor.test_execute_tpcds = _fast_tpcds_dep
                texec.TestExecutor.test_execute_tpch = _fast_tpch_dep
                texec.TestExecutor.test_optimized_tpch = _fast_tpch_dep
            elif tf == 'tests/test_parser.py':
                import tests.test_parser as tpar

                def _fast_max_nodes_dep(self):
                    parse_one('SELECT 1', max_nodes=1000000)
                    try:
                        parse_one('SELECT ' + ','.join(('x=x' for _ in range(100))), max_nodes=5)
                    except Exception:
                        pass
                tpar.TestParser.test_max_nodes = _fast_max_nodes_dep
            elif tf == 'tests/test_generator.py':
                import tests.test_generator as tgen

                def _fast_gen_mem_dep(self):
                    for name in Dialects:
                        Dialect.get_or_raise(name).generator()
                tgen.TestGenerator.test_generator_memory = _fast_gen_mem_dep
            loader = unittest.defaultTestLoader
            suite = loader.loadTestsFromModule(mod)
            by_cls: dict[type, list[unittest.TestCase]] = {}
            for grp in suite:
                for tc in grp:
                    by_cls.setdefault(type(tc), []).append(tc)
            for cls, tcs in by_cls.items():
                setup_attr.clear()
                setup_key.clear()
                setup_disp.clear()
                cur_mtid[0] = -2
                su_ok = True
                try:
                    cls.setUpClass()
                except Exception:
                    su_ok = False
                cur_mtid[0] = -1
                cls_mtids: list[int] = []
                for tc in tcs:
                    mname = getattr(tc, '_testMethodName', '')
                    tid = f'{tf}::{cls.__name__}::{mname}'
                    mtid = tid_to_mtid.get(tid)
                    if mtid is None:
                        continue
                    if tid in _SEPARATELY_HANDLED_TEST_IDS:
                        seen_mtids.add(mtid)
                        continue
                    if tid in ALWAYS_KEEP_TEST_IDS:
                        failed_mtids.add(mtid)
                        continue
                    if not su_ok:
                        failed_mtids.add(mtid)
                        continue
                    cls_mtids.append(mtid)
                    cur_mtid[0] = mtid
                    res = unittest.TestResult()
                    try:
                        tc.run(res)
                        if res.wasSuccessful():
                            seen_mtids.add(mtid)
                        else:
                            failed_mtids.add(mtid)
                    except Exception:
                        failed_mtids.add(mtid)
                    finally:
                        cur_mtid[0] = -1
                for mtid in cls_mtids:
                    for k in setup_attr:
                        attr_reads.setdefault(k, set()).add(mtid)
                    for k in setup_key:
                        dict_reads.setdefault(k, set()).add(mtid)
                    for k in setup_disp:
                        gen_dispatch.setdefault(k, set()).add(mtid)
                try:
                    cls.tearDownClass()
                except Exception:
                    pass
    finally:
        restore()
    return {'attr_reads': {k: sorted(v) for k, v in attr_reads.items()}, 'dict_reads': {k: sorted(v) for k, v in dict_reads.items()}, 'gen_dispatch': {k: sorted(v) for k, v in gen_dispatch.items()}, 'seen_mtids': sorted(seen_mtids), 'failed_mtids': sorted(failed_mtids), 'tracked_attrs': sorted(tracked_attrs), 'tracked_dicts': sorted(tracked_dicts), 'wrapped_methods': sorted(wrapped_methods), 'wrapped_funcs': sorted(wrapped_funcs)}

def build_dep_index(sg_root: Path, core_maps_path: Path=Path('/app/maps/core_maps.zlib'), out_path: Path=DEP_INDEX_PATH, n_workers: int | None=None) -> tuple[int, int, int, int, int]:
    import os
    import time
    import zlib
    from concurrent.futures import ProcessPoolExecutor
    t0 = time.perf_counter()
    raw_core = Path('/app/maps_raw/core_maps.marshal')
    if raw_core.is_file():
        maps = marshal.loads(raw_core.read_bytes())
    else:
        maps = marshal.loads(zlib.decompress(core_maps_path.read_bytes()))
    test_ids: list[str] = maps['test_ids']
    tid_to_mtid = {tid: i for i, tid in enumerate(test_ids)}
    all_tfiles = sorted({tid.split('::')[0] for tid in test_ids})
    if n_workers is None:
        n_workers = min(4, os.cpu_count() or 2)
    n_workers = max(1, n_workers)
    buckets: list[list[str]] = [[] for _ in range(n_workers)]
    if n_workers >= 2 and 'tests/test_optimizer.py' in all_tfiles:
        buckets[0].append('tests/test_optimizer.py')
        rem_files = [f for f in all_tfiles if f != 'tests/test_optimizer.py']
        loads = [0] * (n_workers - 1)

        def _weight(tf: str) -> int:
            p = sg_root / tf
            sz = p.stat().st_size if p.is_file() else 1000
            return sz * 4 if tf == 'tests/test_executor.py' else sz
        for tf in sorted(rem_files, key=lambda f: -_weight(f)):
            bi = min(range(n_workers - 1), key=loads.__getitem__)
            buckets[bi + 1].append(tf)
            loads[bi] += _weight(tf)
    else:
        loads = [0] * n_workers
        for tf in sorted(all_tfiles, key=lambda f: -((sg_root / f).stat().st_size if (sg_root / f).is_file() else 1000)):
            bi = min(range(n_workers), key=loads.__getitem__)
            buckets[bi].append(tf)
            loads[bi] += (sg_root / tf).stat().st_size if (sg_root / tf).is_file() else 1000
    merged_attr: dict[tuple[str, str], set[int]] = {}
    merged_dict: dict[tuple[str, str, str], set[int]] = {}
    merged_disp: dict[tuple[str, str], set[int]] = {}
    seen_all: set[int] = set()
    failed_all: set[int] = set()
    tracked_attrs: set[str] = set()
    tracked_dicts: set[str] = set()
    wrapped_methods: set[tuple[str, str]] = set()
    wrapped_funcs: set[tuple[str, str]] = set()
    tasks = [(str(sg_root), b, tid_to_mtid) for b in buckets if b]
    with ProcessPoolExecutor(max_workers=len(tasks)) as pool:
        for res in pool.map(_collect_dep_worker, tasks):
            for k, v in res['attr_reads'].items():
                merged_attr.setdefault(k, set()).update(v)
            for k, v in res['dict_reads'].items():
                merged_dict.setdefault(k, set()).update(v)
            for k, v in res['gen_dispatch'].items():
                merged_disp.setdefault(k, set()).update(v)
            seen_all.update(res['seen_mtids'])
            failed_all.update(res['failed_mtids'])
            tracked_attrs.update(res['tracked_attrs'])
            tracked_dicts.update(res['tracked_dicts'])
            wrapped_methods.update(res['wrapped_methods'])
            wrapped_funcs.update(res['wrapped_funcs'])
    always_keep: set[int] = set(failed_all)
    for mtid, tid in enumerate(test_ids):
        if tid in _SEPARATELY_HANDLED_TEST_IDS:
            continue
        tf = tid.split('::')[0]
        if tf in ALWAYS_KEEP_TEST_FILES or tid in ALWAYS_KEEP_TEST_IDS or mtid not in seen_all:
            always_keep.add(mtid)
    dict_by_ra: dict[tuple[str, str], dict[str, list[int]]] = {}
    for (r, a, ck), mtids in merged_dict.items():
        dict_by_ra.setdefault((r, a), {})[ck] = sorted(mtids)
    payload = {'attr_reads': {k: sorted(v) for k, v in merged_attr.items()}, 'dict_by_ra': dict_by_ra, 'gen_dispatch': {k: sorted(v) for k, v in merged_disp.items()}, 'tracked_attrs': sorted(tracked_attrs), 'tracked_dicts': sorted(tracked_dicts), 'wrapped_methods': sorted(wrapped_methods), 'wrapped_funcs': sorted(wrapped_funcs), 'always_keep_mtids': sorted(always_keep)}
    blob = marshal.dumps(payload)
    out_path.write_bytes(blob)
    meta_payload = {'tracked_attrs': payload['tracked_attrs'], 'tracked_dicts': payload['tracked_dicts'], 'wrapped_methods': payload['wrapped_methods'], 'wrapped_funcs': payload['wrapped_funcs']}
    (out_path.parent / 'dep_meta.marshal').write_bytes(marshal.dumps(meta_payload))
    dt = time.perf_counter() - t0
    print(f'[attr_deps] seen={len(seen_all)}/{len(test_ids)} always_keep={len(always_keep)} attrs={len(merged_attr)} dicts={len(merged_dict)} disp={len(merged_disp)} wm={len(wrapped_methods)} wf={len(wrapped_funcs)} ({len(blob) / 1024:.0f} KB) in {dt:.1f}s', flush=True)
    return (len(seen_all), len(always_keep), len(merged_attr), len(merged_dict), len(blob))
DEP_META_PATH = Path('/app/dep_meta.marshal')
_DEP_INDEX_CACHE: dict[str, Any] | None = None
_DEP_META_CACHE: dict[str, Any] | None = None

def load_dep_meta(path: Path=DEP_META_PATH) -> dict[str, Any] | None:
    global _DEP_META_CACHE
    if _DEP_META_CACHE is None:
        if path.is_file():
            try:
                _DEP_META_CACHE = marshal.loads(path.read_bytes())
            except Exception:
                _DEP_META_CACHE = None
    return _DEP_META_CACHE

def load_dep_index(path: Path=DEP_INDEX_PATH) -> dict[str, Any] | None:
    global _DEP_INDEX_CACHE
    if _DEP_INDEX_CACHE is None:
        if path.is_file():
            try:
                _DEP_INDEX_CACHE = marshal.loads(path.read_bytes())
            except Exception:
                _DEP_INDEX_CACHE = None
    return _DEP_INDEX_CACHE
