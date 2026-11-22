"""Candidate selection: map each change to the tests that can reach it and plan each commit."""
from __future__ import annotations
import ast
import bisect
import marshal
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
_CLASS_DEF_RE = re.compile('^class\\s+(\\w+)\\s*[\\(:]', re.MULTILINE)
_PLAIN_IDENT_KEY_RE = re.compile('^[\'\\"][A-Za-z_][A-Za-z0-9_]*[\'\\"]$')
_COMPILED_C_FILES: frozenset[str] = frozenset()
_UNSET: Any = object()
_EXTRA_DIALECT_META_ATTRS: frozenset[str] = frozenset({'BIT_STRINGS', 'BYTE_STRINGS', 'COMMENTS', 'HEREDOC_STRING', 'HEREDOC_STRINGS', 'HEX_STRINGS', 'HINT_START', 'IDENTIFIERS', 'NATIONAL_RAW_STRING', 'NATIONAL_RAW_STRINGS', 'NATIONAL_STRING', 'QUOTES', 'RAW_STRING', 'RAW_STRINGS', 'SINGLE_TOKENS', 'UNICODE_STRINGS', 'NUMERIC_LITERALS', 'NUMERIC_ESCAPES', 'KEYWORDS', 'VAR_SINGLE_TOKENS', 'COMMANDS', 'COMMAND_PREFIX_TOKENS', 'STRING_ESCAPES', 'BYTE_STRING_ESCAPES', 'IDENTIFIER_ESCAPES', 'SUPPORTED_JSON_PATH_PARTS', 'DATE_PART_MAPPING', 'TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING', 'CREATABLE_KIND_MAPPING', 'UNESCAPED_SEQUENCES', 'ESCAPED_SEQUENCES', 'VALID_INTERVAL_UNITS', 'TIME_TRIE', 'FORMAT_TRIE', 'INVERSE_TIME_TRIE', 'INVERSE_FORMAT_TRIE'})
from diff_scan import BaseAstIndex, ChangeUnit, FilePatch, PyFileAnalysis, _diff_has_identity_or_eq_change, analyze_python_change, apply_file_patch, diff_sql_fixture_lines, diff_sql_fixture_pairs, load_base_ast_index, parse_unified_diff
import attr_deps as _di

@dataclass
class PatchPlan:
    commit: str
    is_noop: bool
    has_syntax_error: bool = False
    syntax_error_files: list[str] = field(default_factory=list)
    patched_files: dict[str, str] = field(default_factory=dict)
    deleted_files: set[str] = field(default_factory=set)
    py_analyses: dict[str, PyFileAnalysis] = field(default_factory=dict)
    candidates: set[int] = field(default_factory=set)
    subtest_filters: dict[int, dict[str, set[Any] | None]] = field(default_factory=dict)
    deferred_subtests: dict[int, list[tuple[str, Any]]] = field(default_factory=dict)
    group: str = 'full_fork'
    exec_engine_touched: bool = False
    tpcds_fixture_touched: bool = False
    tpch_fixture_touched: bool = False
    has_max_nodes_edit: bool = False
    force_report: set[int] = field(default_factory=set)
    dict_factory_files: set[str] = field(default_factory=set)

class ReachPlanner:

    def __init__(self, maps: dict[str, Any], tests_list: list[dict], sqlglot_root: Path, subtests_dir: Path | None=None, base_ast_index: Any=_UNSET) -> None:
        self.maps = maps
        self.sqlglot_root = sqlglot_root
        self.tests_list = tests_list
        self.subtests_dir = subtests_dir
        id_to_num: dict[str, int] = {}
        num_to_test: dict[int, dict] = {}
        file_to_test_nums: dict[str, list[int]] = {}
        for t in tests_list:
            num = t['number']
            id_to_num[t['id']] = num
            num_to_test[num] = t
            file_to_test_nums.setdefault(t['file'], []).append(num)
        self.id_to_num = id_to_num
        self.num_to_test = num_to_test
        self.file_to_test_nums = file_to_test_nums
        map_test_ids: list[str] = maps['test_ids']
        map_tid_to_num: dict[int, int] = {}
        num_to_map_tid: dict[int, int] = {}
        identity_map = True
        for mtid, tid_str in enumerate(map_test_ids):
            num = id_to_num.get(tid_str)
            if num is not None:
                map_tid_to_num[mtid] = num
                num_to_map_tid[num] = mtid
                if num != mtid:
                    identity_map = False
            else:
                identity_map = False
        self.map_tid_to_num = map_tid_to_num
        self.num_to_map_tid = num_to_map_tid
        self._identity_map: bool = identity_map
        self._subtest_tags_by_num: dict[int, list[str]] = {}
        self._loaded_subtests: dict[int, list[dict[str, Any]]] = {}
        if 'subtest_index' in maps:
            if identity_map:
                self._subtest_tags_by_num = dict(maps['subtest_index'])
            else:
                for mtid, tags in maps['subtest_index'].items():
                    rnum = map_tid_to_num.get(mtid)
                    if rnum is not None:
                        self._subtest_tags_by_num[rnum] = tags
        elif 'subtests' in maps:
            for sg in maps['subtests'].values():
                rnum = map_tid_to_num.get(sg['tid'])
                if rnum is not None:
                    self._loaded_subtests.setdefault(rnum, []).append(sg)
                    self._subtest_tags_by_num.setdefault(rnum, []).append(sg['tag'])
        self.maps_dir: Path | None = subtests_dir.parent if subtests_dir is not None else None
        self._dict_keys_by_oa: dict[str, dict[str, dict[str, list[int]]]] | None = None
        if 'dict_keys_by_oa' in maps:
            self._dict_keys_by_oa = maps['dict_keys_by_oa']
        elif 'dict_key_to_tests' in maps:
            dk_oa: dict[str, dict[str, dict[str, list[int]]]] = {}
            for full_k, mtids in maps['dict_key_to_tests'].items():
                o, a, k = full_k.split('|', 2)
                dk_oa.setdefault(o, {}).setdefault(a, {})[k] = mtids
            self._dict_keys_by_oa = dk_oa
        self._line_cov_inline: dict[str, dict[int, list[int]]] | None = maps.get('line_cov')
        self._line_cov_index: dict[str, int] = maps.get('line_cov_index', {})
        self._line_cov_cache: dict[str, dict[int, list[int]]] = {}
        self._dep_index_loaded: bool = False
        self._dep_index_val: dict[str, Any] | None = None
        self._dep_tracked_attrs: frozenset[str] = frozenset()
        self._dep_tracked_dicts: frozenset[str] = frozenset()
        self._dep_always_keep_rnums: set[int] = set()
        self.typing_test_num = id_to_num.get('tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata')
        self.lazy_load_test_num = id_to_num.get('tests/dialects/test_dialect.py::TestDialect::test_lazy_load')
        self.dialect_meta_attrs: set[str] = set(maps['dialect_meta_attrs']) | _EXTRA_DIALECT_META_ATTRS
        self.expr_meta_attrs: set[str] = set(maps['expr_meta_attrs'])
        cls_subs = maps['class_subclasses']
        self.all_expr_classes: set[str] = set(cls_subs.get('Expr', []))
        self._expr_key_to_cls_cache: dict[str, list[str]] | None = None
        self.all_dialect_family: set[str] = set(cls_subs.get('Dialect', [])) | set(cls_subs.get('Tokenizer', [])) | set(cls_subs.get('Parser', [])) | set(cls_subs.get('Generator', [])) | {'JSONPathTokenizer'}
        self.pure_enums: set[str] = set(maps.get('pure_enums', []))
        self.strict_enum_attrs: set[tuple[str, str]] = {tuple(x) for x in maps.get('strict_enum_attrs', [])}
        self._cmp_agree_loaded: bool = False
        self._cmp_agree_val: frozenset[str] = frozenset()
        self._file_cache: dict[str, str] = {}
        self._file_lines_cache: dict[str, list[str]] = {}
        self._old_ast_cache: dict[str, Any] = {}
        self._expand_cache: dict[tuple[str, str], list[str]] = {}
        self._subclasses_cache: dict[str, list[str]] = {}
        self._rev_closure_cache: dict[str, set[str]] = {}
        self._guarded_reader_cache: dict[tuple[str, str, int], frozenset[str] | None] = {}
        self._base_ast_index: BaseAstIndex | None = load_base_ast_index() if base_ast_index is _UNSET else base_ast_index
        static_meta = maps.get('static_ast_meta')
        self._static_meta: dict[str, Any] | None = static_meta
        if static_meta is not None:
            self._file_classes_cache: dict[str, list[str]] = dict(static_meta['file_classes'])
            if static_meta.get('_preconverted'):
                self._rev_import_graph: dict[str, Any] | None = static_meta['rev_import_graph']
                self._class_dep_graph: dict[str, Any] | None = static_meta['class_dep_graph']
                self._non_runtime_cache: dict[str, tuple[Any, Any]] = dict(static_meta['non_runtime_info'])
                self._import_time_called: dict[str, Any] | None = static_meta['import_time_called']
                self._import_time_ref_map: dict[str, Any] | None = static_meta['import_time_refs']
            else:
                self._rev_import_graph = None
                self._class_dep_graph = None
                self._non_runtime_cache = {}
                self._import_time_called = None
                self._import_time_ref_map = None
        else:
            self._rev_import_graph = None
            self._file_classes_cache = {}
            self._class_dep_graph = None
            self._non_runtime_cache = {}
            self._import_time_called = None
            self._import_time_ref_map = None
        self.ignore_docstrings: bool = 'tests/test_docs.py' not in self.file_to_test_nums

    @property
    def _dep_index(self) -> dict[str, Any] | None:
        if not self._dep_index_loaded:
            self._dep_index_loaded = True
            di = _di.load_dep_index()
            self._dep_index_val = di
            if di is not None:
                self._dep_tracked_attrs = frozenset(di.get('tracked_attrs', ()))
                self._dep_tracked_dicts = frozenset(di.get('tracked_dicts', ()))
                self._dep_always_keep_rnums = self._to_rnums(di.get('always_keep_mtids', ()))
        return self._dep_index_val

    @property
    def _expr_key_to_cls(self) -> dict[str, list[str]]:
        if self._expr_key_to_cls_cache is None:
            m: dict[str, list[str]] = {'expression': ['Expr']}
            for ec in self.all_expr_classes:
                m.setdefault(ec.lower(), []).append(ec)
            self._expr_key_to_cls_cache = m
        return self._expr_key_to_cls_cache

    def _get_cmp_agree(self) -> frozenset[str]:
        if not self._cmp_agree_loaded:
            self._cmp_agree_loaded = True
            raw_cmp = Path('/app/maps_raw/cmp_agree.marshal')
            if raw_cmp.is_file():
                obj = marshal.loads(raw_cmp.read_bytes())
                self._cmp_agree_val = obj if isinstance(obj, frozenset) else frozenset(obj)
            else:
                cmp_path = self.maps_dir / 'cmp_agree.zlib' if self.maps_dir is not None else None
                if cmp_path is not None and cmp_path.exists():
                    import zlib
                    self._cmp_agree_val = frozenset(marshal.loads(zlib.decompress(cmp_path.read_bytes())))
        return self._cmp_agree_val

    @property
    def cmp_agree(self) -> frozenset[str]:
        return self._get_cmp_agree()

    def _parse_base_ast(self, rel: str) -> ast.Module | None:
        cached = self._old_ast_cache.get(rel)
        if cached is not None:
            return cached
        txt = self._read_base_file(rel)
        if not txt:
            return None
        try:
            tree = ast.parse(txt, filename=rel)
        except SyntaxError:
            return None
        self._old_ast_cache[rel] = tree
        return tree

    @staticmethod
    def _attr_chain(node: ast.AST) -> list[str] | None:
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            parts.reverse()
            return parts
        return None

    @staticmethod
    def _iter_non_runtime_nodes(root: ast.AST):
        stack: list[ast.AST] = [root]
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Lambda):
                stack.extend(node.args.defaults)
                stack.extend((d for d in node.args.kw_defaults if d is not None))
                continue
            stack.extend(ast.iter_child_nodes(node))

    def _get_class_dep_graph(self) -> dict[str, Any]:
        if self._class_dep_graph is not None:
            return self._class_dep_graph
        if self._static_meta is not None:
            cdg = self._static_meta['class_dep_graph']
            self._class_dep_graph = {'class_children': {k: frozenset(v) for k, v in cdg['class_children'].items()}, 'attr_copiers': {tuple(k): frozenset((tuple(x) for x in v)) for k, v in cdg['attr_copiers'].items()}, 'class_aliases': {k: frozenset(v) for k, v in cdg['class_aliases'].items()}, 'class_copiers': {k: frozenset(v) for k, v in cdg['class_copiers'].items()}, 'attr_mutators': {k: frozenset(v) for k, v in cdg['attr_mutators'].items()}, 'all_class_body_refs': frozenset(cdg['all_class_body_refs'])}
            return self._class_dep_graph
        class_children: dict[str, set[str]] = {}
        attr_copiers: dict[tuple[str, str], set[tuple[str, str]]] = {}
        class_aliases: dict[str, set[str]] = {}
        class_copiers: dict[str, set[str]] = {}
        attr_mutators: dict[str, set[str]] = {}
        all_class_body_refs: set[str] = set()
        sg_dir = self.sqlglot_root / 'sqlglot'
        scan_paths: list[Path] = []
        for sub in ('dialects', 'parsers', 'generators', 'typing'):
            d = sg_dir / sub
            if d.exists():
                scan_paths.extend(d.glob('*.py'))
        for single in ('tokens.py', 'jsonpath.py', 'optimizer/annotate_types.py', 'expressions/__init__.py'):
            p = sg_dir / single
            if p.exists():
                scan_paths.append(p)

        def _extract_imports(tree: ast.Module) -> dict[str, str]:
            imp: dict[str, str] = {}
            for s in tree.body:
                if isinstance(s, ast.ImportFrom):
                    for a in s.names:
                        if a.name != '*':
                            imp[a.asname or a.name] = a.name
                elif isinstance(s, ast.Import):
                    for a in s.names:
                        if a.asname:
                            imp[a.asname] = a.name.rsplit('.', 1)[-1]
            return imp

        def _resolve_chain(parts: list[str], imp: dict[str, str]) -> list[str]:
            if not parts:
                return parts
            head = imp.get(parts[0], parts[0])
            return [head, *parts[1:]]

        def _scan_class(cls_node: ast.ClassDef, parent_chain: list[str], imp: dict[str, str], local_classes: set[str]) -> None:
            cur_chain = [*parent_chain, cls_node.name]
            cur_qual = '.'.join(cur_chain)
            for base in cls_node.bases:
                parts = self._attr_chain(base)
                if parts:
                    rparts = _resolve_chain(parts, imp)
                    parent_qual = '.'.join(rparts)
                    class_children.setdefault(parent_qual, set()).add(cur_qual)
                    if len(rparts) == 1 and rparts[0] in ('Tokenizer', 'Parser', 'Generator', 'JSONPathTokenizer'):
                        class_children.setdefault(rparts[0], set()).add(cur_qual)
                    elif len(rparts) >= 2:
                        class_children.setdefault(rparts[-1], set()).add(cur_qual)
            for stmt in cls_node.body:
                if isinstance(stmt, ast.ClassDef):
                    _scan_class(stmt, cur_chain, imp, local_classes)
                    continue
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                dst_attr: str | None = None
                val_node: ast.AST | None = None
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    dst_attr = stmt.targets[0].id
                    val_node = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    dst_attr = stmt.target.id
                    val_node = stmt.value
                if dst_attr is not None and isinstance(val_node, ast.Name):
                    rname = imp.get(val_node.id, val_node.id)
                    if rname in local_classes or rname in self.all_dialect_family or rname.startswith('_'):
                        class_aliases.setdefault(rname, set()).add(cur_qual)
                        class_children.setdefault(rname, set()).add(f'{cur_qual}.{dst_attr}')
                if isinstance(stmt, ast.Delete):
                    for t in stmt.targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                            attr_mutators.setdefault(t.value.id, set()).add(cur_qual)
                elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                    fn = stmt.value.func
                    if isinstance(fn, ast.Attribute) and fn.attr in ('pop', 'update', 'clear', 'setdefault'):
                        if isinstance(fn.value, ast.Name):
                            attr_mutators.setdefault(fn.value.id, set()).add(cur_qual)
                for node in self._iter_non_runtime_nodes(stmt):
                    if isinstance(node, ast.Name):
                        if node.id != dst_attr and isinstance(node.ctx, ast.Load):
                            all_class_body_refs.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        if isinstance(node.ctx, ast.Load):
                            all_class_body_refs.add(node.attr)
                        parts = self._attr_chain(node)
                        if parts and len(parts) >= 2:
                            rparts = _resolve_chain(parts, imp)
                            src_head = rparts[0]
                            if src_head in ('self', 'cls', 'exp', 't', 'typing', 'TokenType', 're', 'math', 'functools', 'itertools', 'collections'):
                                continue
                            if len(rparts) == 2:
                                src_cls, src_attr = (rparts[0], rparts[1])
                                if src_cls != cur_qual and src_cls != cls_node.name:
                                    attr_copiers.setdefault((src_cls, src_attr), set()).add((cur_qual, dst_attr or '*'))
                                    class_copiers.setdefault(src_cls, set()).add(cur_qual)
                            elif len(rparts) >= 3:
                                src_nested = '.'.join(rparts[:-1])
                                src_attr = rparts[-1]
                                if src_nested != cur_qual:
                                    attr_copiers.setdefault((src_nested, src_attr), set()).add((cur_qual, dst_attr or '*'))
                                    attr_copiers.setdefault((rparts[0], src_attr), set()).add((cur_qual, dst_attr or '*'))
                                    class_copiers.setdefault(src_nested, set()).add(cur_qual)
                                    class_copiers.setdefault(rparts[0], set()).add(cur_qual)
        for p in scan_paths:
            rel = p.relative_to(self.sqlglot_root).as_posix()
            tree = self._parse_base_ast(rel)
            if tree is None:
                continue
            imp = _extract_imports(tree)
            local_classes = {s.name for s in tree.body if isinstance(s, ast.ClassDef)}
            for s in tree.body:
                if isinstance(s, ast.ClassDef):
                    _scan_class(s, [], imp, local_classes)
        self._class_dep_graph = {'class_children': class_children, 'attr_copiers': attr_copiers, 'class_aliases': class_aliases, 'class_copiers': class_copiers, 'attr_mutators': attr_mutators, 'all_class_body_refs': all_class_body_refs}
        return self._class_dep_graph

    def _get_non_runtime_info(self, rel: str) -> tuple[Any, Any]:
        cached = self._non_runtime_cache.get(rel)
        if cached is not None:
            return cached
        if self._static_meta is not None:
            raw = self._static_meta['non_runtime_info'].get(rel)
            if raw is not None:
                res_s = (frozenset(raw[0]), frozenset(raw[1]))
                self._non_runtime_cache[rel] = res_s
                return res_s
        tree = self._parse_base_ast(rel)
        if tree is None:
            res: tuple[set[int], set[str]] = (set(), set())
            self._non_runtime_cache[rel] = res
            return res
        non_rt: set[int] = set()
        cb_refs: set[str] = set()
        meta_hooks = frozenset({'__new__', '__init_subclass__', '__set_name__', '__class_getitem__', '_generate_next_value_'})

        def _add_node_lines(node: ast.AST) -> None:
            ln = getattr(node, 'lineno', None)
            if ln is not None:
                non_rt.add(ln)

        def _scan_stmts(stmts: list[ast.stmt], in_class: bool) -> None:
            for s in stmts:
                if isinstance(s, ast.ClassDef):
                    for d in s.decorator_list:
                        for n in ast.walk(d):
                            _add_node_lines(n)
                    for b in s.bases:
                        for n in ast.walk(b):
                            _add_node_lines(n)
                    for kw in s.keywords:
                        for n in ast.walk(kw):
                            _add_node_lines(n)
                    non_rt.add(s.lineno)
                    _scan_stmts(s.body, True)
                elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in s.decorator_list:
                        for n in ast.walk(d):
                            _add_node_lines(n)
                    for df in s.args.defaults:
                        for n in ast.walk(df):
                            _add_node_lines(n)
                    for df in s.args.kw_defaults:
                        if df is not None:
                            for n in ast.walk(df):
                                _add_node_lines(n)
                    non_rt.add(s.lineno)
                    if in_class and s.name in meta_hooks:
                        for n in ast.walk(s):
                            _add_node_lines(n)
                            if isinstance(n, ast.Name):
                                cb_refs.add(n.id)
                            elif isinstance(n, ast.Attribute):
                                cb_refs.add(n.attr)
                else:
                    for n in self._iter_non_runtime_nodes(s):
                        _add_node_lines(n)
                        if in_class:
                            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                                cb_refs.add(n.id)
                            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                                cb_refs.add(n.attr)
        _scan_stmts(tree.body, False)
        res = (non_rt, cb_refs)
        self._non_runtime_cache[rel] = res
        return res

    def _is_func_called_at_import(self, rel: str, meth_name: str) -> tuple[bool, bool, set[str]]:
        rev_cl = self._rev_import_closure(rel)
        if self._import_time_called is not None and self._import_time_ref_map is not None:
            cf = self._import_time_called.get(meth_name)
            rf = self._import_time_ref_map.get(meth_name)
            cf_hit = (rev_cl & cf) - {'sqlglot/__main__.py'} if cf else set()
            rf_hit = (rev_cl & rf) - {'sqlglot/__main__.py'} if rf else set()
            if cf_hit:
                return (True, True, cf_hit)
            if rf_hit:
                return (False, True, rf_hit)
            return (False, False, set())
        if self._static_meta is not None:
            raw_cf = self._static_meta['import_time_called'].get(meth_name)
            raw_rf = self._static_meta['import_time_refs'].get(meth_name)
            cf_hit = rev_cl.intersection(raw_cf) - {'sqlglot/__main__.py'} if raw_cf else set()
            rf_hit = rev_cl.intersection(raw_rf) - {'sqlglot/__main__.py'} if raw_rf else set()
            if cf_hit:
                return (True, True, cf_hit)
            if rf_hit:
                return (False, True, rf_hit)
            return (False, False, set())
        sg_dir = self.sqlglot_root / 'sqlglot'
        py_files = list(sg_dir.rglob('*.py')) if sg_dir.exists() else []
        meta_hooks = frozenset({'__new__', '__init_subclass__', '__set_name__', '__class_getitem__', '_generate_next_value_'})
        called_files: set[str] = set()
        ref_files: set[str] = set()

        def _record_expr(expr: ast.AST, f_rel: str, is_dec: bool=False) -> None:
            for n in self._iter_non_runtime_nodes(expr):
                if isinstance(n, ast.Call):
                    f = n.func
                    if isinstance(f, ast.Name) and f.id == meth_name or (isinstance(f, ast.Attribute) and f.attr == meth_name):
                        called_files.add(f_rel)
                if isinstance(n, ast.Name) and n.id == meth_name or (isinstance(n, ast.Attribute) and n.attr == meth_name):
                    ref_files.add(f_rel)
                    if is_dec and n is expr:
                        called_files.add(f_rel)

        def _scan_stmts(stmts: list[ast.stmt], f_rel: str, in_class: bool) -> None:
            for s in stmts:
                if isinstance(s, ast.ClassDef):
                    for d in s.decorator_list:
                        _record_expr(d, f_rel, is_dec=True)
                    _scan_stmts(s.body, f_rel, True)
                elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in s.decorator_list:
                        _record_expr(d, f_rel, is_dec=True)
                    for df in s.args.defaults:
                        _record_expr(df, f_rel)
                    for df in s.args.kw_defaults:
                        if df is not None:
                            _record_expr(df, f_rel)
                    if in_class and s.name in meta_hooks:
                        for n in ast.walk(s):
                            if isinstance(n, ast.Call):
                                f = n.func
                                if isinstance(f, ast.Name) and f.id == meth_name or (isinstance(f, ast.Attribute) and f.attr == meth_name):
                                    called_files.add(f_rel)
                elif isinstance(s, ast.If):
                    test = s.test
                    is_tc = isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING' or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')
                    if not is_tc:
                        _record_expr(test, f_rel)
                        _scan_stmts(s.body, f_rel, in_class)
                    _scan_stmts(s.orelse, f_rel, in_class)
                else:
                    _record_expr(s, f_rel)
        for p in py_files:
            f_rel = p.relative_to(self.sqlglot_root).as_posix()
            txt = self._read_base_file(f_rel)
            if meth_name not in txt:
                continue
            tree = self._parse_base_ast(f_rel)
            if tree is not None:
                _scan_stmts(tree.body, f_rel, False)
        return (bool(called_files), bool(ref_files), called_files if called_files else ref_files)

    def _find_dict_only_import_sites(self, rel: str, meth_name: str, ref_files: set[str]) -> list[tuple[str, str, str, str]] | None:
        if not meth_name or not ref_files:
            return None
        sites: list[tuple[str, str, str, str]] = []
        meta_hooks = frozenset({'__new__', '__init_subclass__', '__set_name__', '__class_getitem__', '_generate_next_value_', '_build_dispatch', 'sql_names'})

        def _has_name(node: ast.AST) -> bool:
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and n.id == meth_name:
                    return True
                if isinstance(n, ast.Attribute) and n.attr == meth_name:
                    return True
            return False
        for rf in sorted(ref_files):
            tree = self._parse_base_ast(rf)
            if tree is None:
                return None
            for stmt in tree.body:
                if isinstance(stmt, ast.ClassDef):
                    for d in stmt.decorator_list:
                        if _has_name(d):
                            return None
                    cls_name = stmt.name
                    for cstmt in stmt.body:
                        if isinstance(cstmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            for d in cstmt.decorator_list:
                                if _has_name(d):
                                    return None
                            for df in cstmt.args.defaults:
                                if _has_name(df):
                                    return None
                            for df in cstmt.args.kw_defaults:
                                if df is not None and _has_name(df):
                                    return None
                            if cstmt.name in meta_hooks and _has_name(cstmt):
                                return None
                        elif isinstance(cstmt, (ast.Assign, ast.AnnAssign)):
                            val = cstmt.value
                            if val is None or not _has_name(val):
                                continue
                            if not isinstance(val, ast.Dict):
                                return None
                            attr_name: str | None = None
                            if isinstance(cstmt, ast.Assign) and len(cstmt.targets) == 1 and isinstance(cstmt.targets[0], ast.Name):
                                attr_name = cstmt.targets[0].id
                            elif isinstance(cstmt, ast.AnnAssign) and isinstance(cstmt.target, ast.Name):
                                attr_name = cstmt.target.id
                            if not attr_name or cls_name not in self.all_dialect_family:
                                return None
                            for k, v in zip(val.keys, val.values):
                                if k is None:
                                    if _has_name(v):
                                        return None
                                    continue
                                if _has_name(k):
                                    return None
                                if _has_name(v):
                                    sites.append((rf, cls_name, attr_name, ast.unparse(k)))
                        elif _has_name(cstmt):
                            return None
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in stmt.decorator_list:
                        if _has_name(d):
                            return None
                    for df in stmt.args.defaults:
                        if _has_name(df):
                            return None
                    for df in stmt.args.kw_defaults:
                        if df is not None and _has_name(df):
                            return None
                elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    continue
                elif isinstance(stmt, ast.If):
                    test = stmt.test
                    is_tc = isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING' or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')
                    if not is_tc and _has_name(stmt):
                        return None
                    if is_tc and any((_has_name(s2) for s2 in stmt.orelse)):
                        return None
                elif _has_name(stmt):
                    return None
        if not sites:
            return None
        for _, cls_name, attr_name, _ in sites:
            if attr_name == 'TRANSFORMS':
                continue
            owners = self._expand_class_and_copiers(cls_name, attr_name)
            if cls_name not in owners:
                owners.append(cls_name)
            if not any((attr_name in self.dict_keys_by_oa.get(o, {}) for o in owners)):
                return None
        return sites

    def _guarded_isinstance_for_reader(self, attr: str, rfile: str, rline: int) -> frozenset[str] | None:
        ck = (attr, rfile, rline)
        if ck in self._guarded_reader_cache:
            return self._guarded_reader_cache[ck]
        search_root: ast.AST | None = self._old_ast_cache.get(rfile)
        if search_root is None and self._base_ast_index is not None:
            txt = self._read_base_file(rfile)
            if txt:
                idx_entry = self._base_ast_index.get_verified(rfile, txt)
                if idx_entry is not None:
                    ustarts = idx_entry['unit_starts']
                    units_idx = idx_entry['units']
                    ui = bisect.bisect_right(ustarts, rline) - 1
                    if ui >= 0:
                        u = units_idx[ui]
                        if u[0] <= rline <= u[1] and u[3] == 'func':
                            lines = txt.split('\n')
                            u0, u1 = (u[0] - 1, u[1])
                            c_indents = u[6]
                            d = len(c_indents)
                            prefix = ''.join((f'{ind}class _C{i}:\n' for i, ind in enumerate(c_indents)))
                            try:
                                stree = ast.parse(prefix + '\n'.join(lines[u0:u1]))
                                body = stree.body
                                ok = True
                                for _ in range(d):
                                    if len(body) != 1 or not isinstance(body[0], ast.ClassDef):
                                        ok = False
                                        break
                                    body = body[0].body
                                if ok and len(body) == 1:
                                    fn_n = body[0]
                                    if u0 - d != 0:
                                        ast.increment_lineno(fn_n, u0 - d)
                                    search_root = fn_n
                            except SyntaxError:
                                search_root = None
        if search_root is None:
            search_root = self._parse_base_ast(rfile)
        if search_root is None:
            self._guarded_reader_cache[ck] = None
            return None
        matched_if: ast.If | None = None
        for node in ast.walk(search_root):
            if isinstance(node, ast.If):
                t_start = getattr(node.test, 'lineno', 0)
                t_end = getattr(node.test, 'end_lineno', t_start)
                if t_start <= rline <= t_end:
                    matched_if = node
                    break
        if matched_if is None or not isinstance(matched_if.test, ast.BoolOp) or (not isinstance(matched_if.test.op, ast.And)):
            self._guarded_reader_cache[ck] = None
            return None
        has_self_attr = False
        found_cls: set[str] = set()
        for part in matched_if.test.values:
            if isinstance(part, ast.Attribute) and part.attr == attr and isinstance(part.value, ast.Name) and (part.value.id == 'self'):
                has_self_attr = True
            elif isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and (part.func.id == 'isinstance') and (len(part.args) == 2):
                arg2 = part.args[1]
                items = arg2.elts if isinstance(arg2, ast.Tuple) else [arg2]
                local_cls: set[str] = set()
                all_exp = bool(items)
                for it in items:
                    if isinstance(it, ast.Attribute) and isinstance(it.value, ast.Name) and (it.value.id == 'exp') and (it.attr in self.all_expr_classes):
                        local_cls.add(it.attr)
                    else:
                        all_exp = False
                        break
                if all_exp and local_cls:
                    found_cls.update(local_cls)
        res = frozenset(found_cls) if has_self_attr and found_cls else None
        self._guarded_reader_cache[ck] = res
        return res

    def _guarded_isinstance_exprs(self, attr: str, readers: list[tuple[str, int]]) -> list[str] | None:
        if not readers or len(readers) > 4:
            return None
        _gcache = getattr(self, '_guarded_isinstance_cache', None)
        if _gcache is None:
            _gp = Path('/app/guarded_isinstance.marshal')
            if _gp.is_file():
                try:
                    _gcache = marshal.loads(_gp.read_bytes())
                except Exception:
                    _gcache = {}
            else:
                _gcache = {}
            self._guarded_isinstance_cache = _gcache
        if attr in _gcache:
            return _gcache[attr]
        expr_classes: set[str] = set()
        for rfile, rline in readers:
            found_cls = self._guarded_isinstance_for_reader(attr, rfile, rline)
            if found_cls is None:
                return None
            expr_classes.update(found_cls)
        if not expr_classes:
            return None
        subs: set[str] = set()
        for ec in expr_classes:
            subs.update(self._subclasses_of(ec))
        return sorted(subs)

    def _rev_import_closure(self, rel: str) -> set[str]:
        cached = self._rev_closure_cache.get(rel)
        if cached is not None:
            return cached
        rev_graph = self._get_rev_import_graph()
        closure: set[str] = {rel}
        stack = [rel]
        while stack:
            cur = stack.pop()
            for nxt in rev_graph.get(cur, ()):
                if nxt not in closure:
                    closure.add(nxt)
                    stack.append(nxt)
        self._rev_closure_cache[rel] = closure
        return closure

    def _expand_class_and_copiers(self, class_name: str, attr: str='*') -> list[str]:
        ck = (class_name, attr)
        cached = self._expand_cache.get(ck)
        if cached is not None:
            return cached
        dep = self._get_class_dep_graph()
        class_children = dep['class_children']
        attr_copiers = dep['attr_copiers']
        class_aliases = dep['class_aliases']
        class_copiers = dep['class_copiers']
        visited_pairs: set[tuple[str, str]] = set()
        visited_classes: set[str] = set()
        queue: list[tuple[str, str]] = [(class_name, attr)]
        if class_name == 'JSONPathTokenizer':
            for o in self.dict_keys_by_oa:
                if 'JSONPathTokenizer' in o:
                    queue.append((o, attr))
        while queue:
            cur, cur_attr = queue.pop()
            pair = (cur, cur_attr)
            if pair in visited_pairs:
                continue
            visited_pairs.add(pair)
            visited_classes.add(cur)
            for sc in self._subclasses_of(cur):
                if (sc, cur_attr) not in visited_pairs:
                    queue.append((sc, cur_attr))
            for ch in class_children.get(cur, ()):
                if (ch, cur_attr) not in visited_pairs:
                    queue.append((ch, cur_attr))
            for al in class_aliases.get(cur, ()):
                if (al, cur_attr) not in visited_pairs:
                    queue.append((al, cur_attr))
            if '.' in cur:
                parent_d, leaf = cur.split('.', 1)
                for psub in set(self._subclasses_of(parent_d)) | set(class_children.get(parent_d, ())):
                    cand = f'{psub}.{leaf}'
                    if (cand, cur_attr) not in visited_pairs:
                        queue.append((cand, cur_attr))
                    if (psub, cur_attr) not in visited_pairs:
                        queue.append((psub, cur_attr))
            if cur_attr != '*':
                for dst_cls, dst_attr in attr_copiers.get((cur, cur_attr), ()):
                    nxt_attr = dst_attr if dst_attr != '*' else cur_attr
                    if (dst_cls, nxt_attr) not in visited_pairs:
                        queue.append((dst_cls, nxt_attr))
            else:
                for dst_cls in class_copiers.get(cur, ()):
                    if (dst_cls, '*') not in visited_pairs:
                        queue.append((dst_cls, '*'))
        res = sorted(visited_classes)
        self._expand_cache[ck] = res
        return res

    def _get_rev_import_graph(self) -> dict[str, Any]:
        if self._rev_import_graph is not None:
            return self._rev_import_graph
        if self._static_meta is not None:
            self._rev_import_graph = {k: frozenset(v) for k, v in self._static_meta['rev_import_graph'].items()}
            return self._rev_import_graph
        mod_to_file: dict[str, str] = {}
        sg_dir = self.sqlglot_root / 'sqlglot'
        py_files = list(sg_dir.rglob('*.py')) if sg_dir.exists() else []
        for p in py_files:
            rel = p.relative_to(self.sqlglot_root).as_posix()
            if rel.endswith('/__init__.py'):
                mod = rel.removesuffix('/__init__.py').replace('/', '.')
            else:
                mod = rel.removesuffix('.py').replace('/', '.')
            mod_to_file[mod] = rel

        def _scan_stmts(stmts: list[ast.stmt], cur_pkg: str, targets: list[str]) -> None:
            for s in stmts:
                if isinstance(s, ast.Import):
                    for alias in s.names:
                        targets.append(alias.name)
                elif isinstance(s, ast.ImportFrom):
                    if s.level and s.level > 0:
                        parts = cur_pkg.split('.')
                        base = '.'.join(parts[:max(0, len(parts) - (s.level - 1))])
                        mod_name = f'{base}.{s.module}' if s.module else base
                    else:
                        mod_name = s.module or ''
                    if mod_name:
                        targets.append(mod_name)
                        for alias in s.names:
                            if alias.name != '*':
                                targets.append(f'{mod_name}.{alias.name}')
                elif isinstance(s, ast.If):
                    test = s.test
                    is_tc = isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING' or (isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')
                    if not is_tc:
                        _scan_stmts(s.body, cur_pkg, targets)
                    _scan_stmts(s.orelse, cur_pkg, targets)
                elif isinstance(s, ast.Try):
                    _scan_stmts(s.body, cur_pkg, targets)
                    for h in s.handlers:
                        _scan_stmts(h.body, cur_pkg, targets)
                    _scan_stmts(s.orelse, cur_pkg, targets)
                    _scan_stmts(s.finalbody, cur_pkg, targets)
        rev: dict[str, set[str]] = {}
        imp_re = re.compile('^[ \\t]*(?:from[ \\t]+(\\.*[\\w.]*)[ \\t]+import[ \\t]+([^\\n#\\\\]+)|import[ \\t]+([^\\n#\\\\]+))', re.MULTILINE)
        for p in py_files:
            rel = p.relative_to(self.sqlglot_root).as_posix()
            cur_pkg = rel.removesuffix('/__init__.py').replace('/', '.') if rel.endswith('/__init__.py') else rel.rsplit('/', 1)[0].replace('/', '.')
            txt = self._read_base_file(rel)
            if rel in self._old_ast_cache:
                tree = self._old_ast_cache[rel]
                self._file_classes_cache[rel] = [s.name for s in tree.body if isinstance(s, ast.ClassDef)]
                targets: list[str] = []
                _scan_stmts(tree.body, cur_pkg, targets)
            else:
                self._file_classes_cache[rel] = _CLASS_DEF_RE.findall(txt)
                if 'TYPE_CHECKING' in txt:
                    tree = self._parse_base_ast(rel)
                    if tree is None:
                        continue
                    targets = []
                    _scan_stmts(tree.body, cur_pkg, targets)
                else:
                    targets = []
                    for m in imp_re.finditer(txt):
                        if m.group(0)[0] in (' ', '\t'):
                            continue
                        if m.group(1) is not None:
                            raw_mod = m.group(1)
                            level = len(raw_mod) - len(raw_mod.lstrip('.'))
                            sub_mod = raw_mod[level:]
                            if level > 0:
                                parts = cur_pkg.split('.')
                                base = '.'.join(parts[:max(0, len(parts) - (level - 1))])
                                mod_name = f'{base}.{sub_mod}' if sub_mod else base
                            else:
                                mod_name = sub_mod
                            if mod_name:
                                targets.append(mod_name)
                                for part in m.group(2).replace('(', '').replace(')', '').split(','):
                                    nm = part.strip().split(' ')[0]
                                    if nm and nm != '*':
                                        targets.append(f'{mod_name}.{nm}')
                        elif m.group(3) is not None:
                            for part in m.group(3).split(','):
                                nm = part.strip().split(' ')[0]
                                if nm:
                                    targets.append(nm)
            for t in targets:
                tf = mod_to_file.get(t)
                if tf and tf != rel:
                    rev.setdefault(tf, set()).add(rel)
        self._rev_import_graph = rev
        return rev

    @property
    def dict_keys_by_oa(self) -> dict[str, dict[str, dict[str, list[int]]]]:
        if self._dict_keys_by_oa is None:
            raw_p = Path('/app/maps_raw/dict_maps.marshal')
            if raw_p.is_file():
                self._dict_keys_by_oa = marshal.loads(raw_p.read_bytes())
                return self._dict_keys_by_oa
            if self.maps_dir is not None:
                p = self.maps_dir / 'dict_maps.zlib'
                if p.exists():
                    import zlib
                    self._dict_keys_by_oa = marshal.loads(zlib.decompress(p.read_bytes()))
                    return self._dict_keys_by_oa
            self._dict_keys_by_oa = {}
        return self._dict_keys_by_oa

    def _get_line_cov(self, rel: str) -> dict[int, list[int]] | None:
        if self._line_cov_inline is not None:
            return self._line_cov_inline.get(rel)
        cached = self._line_cov_cache.get(rel)
        if cached is not None:
            return cached
        idx = self._line_cov_index.get(rel)
        if idx is None or self.maps_dir is None:
            return None
        raw_p = Path(f'/app/maps_raw/line_cov/{idx}.marshal')
        if raw_p.is_file():
            lm = marshal.loads(raw_p.read_bytes())
            self._line_cov_cache[rel] = lm
            return lm
        p = self.maps_dir / 'line_cov' / f'{idx}.zlib'
        if not p.exists():
            return None
        import zlib
        lm = marshal.loads(zlib.decompress(p.read_bytes()))
        self._line_cov_cache[rel] = lm
        return lm

    def _get_subtests(self, rnum: int) -> list[dict[str, Any]]:
        if rnum not in self._subtest_tags_by_num:
            return []
        loaded = self._loaded_subtests.get(rnum)
        if loaded is not None:
            return loaded
        mtid = self.num_to_map_tid.get(rnum)
        if mtid is None or self.subtests_dir is None:
            return []
        raw_p = Path(f'/app/maps_raw/subtests/{mtid}.marshal')
        if raw_p.is_file():
            groups = marshal.loads(raw_p.read_bytes())
            self._loaded_subtests[rnum] = groups
            return groups
        p = self.subtests_dir / f'{mtid}.zlib'
        if not p.exists():
            self._loaded_subtests[rnum] = []
            return []
        import zlib
        groups = marshal.loads(zlib.decompress(p.read_bytes()))
        self._loaded_subtests[rnum] = groups
        return groups

    def _read_base_file(self, rel_path: str) -> str:
        txt = self._file_cache.get(rel_path)
        if txt is None:
            p = self.sqlglot_root / rel_path
            txt = p.read_text(encoding='utf-8', errors='replace') if p.exists() else ''
            self._file_cache[rel_path] = txt
        return txt

    def _read_base_lines(self, rel_path: str) -> list[str]:
        lines = self._file_lines_cache.get(rel_path)
        if lines is None:
            lines = self._read_base_file(rel_path).splitlines()
            self._file_lines_cache[rel_path] = lines
        return lines

    def _to_rnums(self, mtids: list[int] | set[int]) -> set[int]:
        if self._identity_map:
            return set(mtids)
        m = self.map_tid_to_num
        out: set[int] = set()
        for t in mtids:
            r = m.get(t)
            if r is not None:
                out.add(r)
        return out

    def _match_dict_key_in_bucket(self, bucket: dict[str, list[Any]], ast_key_repr: str) -> list[Any]:
        hits: list[Any] = []
        if '*' in bucket:
            hits.extend(bucket['*'])
        if ast_key_repr in bucket:
            hits.extend(bucket[ast_key_repr])
            return hits
        if not ast_key_repr.startswith(("'", '"')):
            last = ast_key_repr.rsplit('.', 1)[-1]
            t_key = f'TYPE:{last}'
            if t_key in bucket:
                hits.extend(bucket[t_key])
            suffix = f'.{last}'
            angle_mid = f'.{last}:'
            for rk, vals in bucket.items():
                if rk.startswith('ENUM:') and rk.endswith(suffix) or (rk.startswith('<') and angle_mid in rk):
                    hits.extend(vals)
        return hits

    def _match_dict_key(self, owner: str, attr: str, ast_key_repr: str) -> list[int]:
        o_map = self.dict_keys_by_oa.get(owner)
        if not o_map:
            return []
        bucket = o_map.get(attr)
        if not bucket:
            return []
        return self._match_dict_key_in_bucket(bucket, ast_key_repr)

    def _subtest_match_lines(self, sg: dict[str, Any], rel: str, lines: set[int]) -> set[Any]:
        lm = sg['lines'].get(rel)
        if not lm:
            return set()
        out: set[Any] = set()
        for ln in lines:
            cids = lm.get(ln)
            if cids:
                out.update(cids)
        return out

    def _add_subtest_filter(self, filters: dict[int, dict[str, set[Any] | None]], rnum: int, tag: str, cases: set[Any] | None) -> None:
        tf = filters.setdefault(rnum, {})
        if tag not in tf:
            tf[tag] = None if cases is None else set(cases)
        elif tf[tag] is None or cases is None:
            tf[tag] = None
        else:
            tf[tag].update(cases)

    def _subclasses_of(self, class_name: str | None) -> list[str]:
        if not class_name:
            return []
        cached = self._subclasses_cache.get(class_name)
        if cached is not None:
            return cached
        subs = set(self.maps['class_subclasses'].get(class_name, []))
        if not subs and '.' in class_name:
            leaf = class_name.rsplit('.', 1)[-1]
            parent_d = class_name.split('.', 1)[0]
            for dsub in self.maps['class_subclasses'].get(parent_d, [parent_d]):
                subs.add(f'{dsub}.{leaf}')
                subs.add(dsub)
        if not subs:
            subs.add(class_name)
        res = sorted(subs)
        self._subclasses_cache[class_name] = res
        return res

    def _tests_covering_lines(self, rel: str, lines: set[int]) -> set[int]:
        lm = self._get_line_cov(rel)
        if not lm:
            return set()
        out: set[int] = set()
        for ln in lines:
            mtids = lm.get(ln)
            if mtids:
                out.update(self._to_rnums(mtids))
        return out

    def _tests_for_broken_import(self, rel: str) -> set[int]:
        out: set[int] = set()
        if rel.startswith('tests/'):
            if rel in self.file_to_test_nums:
                out.update(self.file_to_test_nums[rel])
            for tf in self.maps['import_file_to_test_files'].get(rel, []):
                out.update(self.file_to_test_nums.get(tf, []))
            out.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
            return out
        closure = self._rev_import_closure(rel)
        touches_dialect_pkg = False
        for f in closure:
            for tf in self.maps['import_file_to_test_files'].get(f, []):
                out.update(self.file_to_test_nums.get(tf, []))
            out.update(self._to_rnums(self.maps['dyn_import_to_tests'].get(f, [])))
            out.update(self._to_rnums(self.maps['file_cov'].get(f, [])))
            if f.startswith(('sqlglot/dialects/', 'sqlglot/parsers/', 'sqlglot/generators/')):
                touches_dialect_pkg = True
            classes = self._file_classes_cache.get(f)
            if classes is None:
                classes = _CLASS_DEF_RE.findall(self._read_base_file(f))
                self._file_classes_cache[f] = classes
            for cls_name in classes:
                for sc in self._subclasses_of(cls_name):
                    out.update(self._to_rnums(self.maps['dialect_to_tests'].get(sc, [])))
                    out.update(self._to_rnums(self.maps['expr_to_tests'].get(sc, [])))
                out.update(self._to_rnums(self.maps['dialect_to_tests'].get(cls_name, [])))
                out.update(self._to_rnums(self.maps['expr_to_tests'].get(cls_name, [])))
        if touches_dialect_pkg:
            out.update(self.file_to_test_nums.get('tests/dialects/test_dialect.py', []))
            gmem = self.id_to_num.get('tests/test_generator.py::TestGenerator::test_generator_memory')
            if gmem is not None:
                out.add(gmem)
        return out

    def resolve_subtest_filter(self, commit_plan: PatchPlan, rnum: int) -> dict[str, set[Any] | None] | None:
        specs = commit_plan.deferred_subtests.pop(rnum, None)
        proven_empty_tags: set[str] = set()
        untracked_empty_tags: set[str] = set()
        if specs:
            sgs = self._get_subtests(rnum)
            if sgs:
                for kind, payload in specs:
                    if kind == 'lines':
                        rel, old_lines = payload
                        for sg in sgs:
                            matched = self._subtest_match_lines(sg, rel, old_lines)
                            if not matched:
                                untracked_empty_tags.add(sg['tag'])
                            self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], matched)
                    elif kind == 'exprs':
                        subs = payload
                        for sg in sgs:
                            matched_e: set[Any] = set()
                            for sc in subs:
                                matched_e.update(sg['exprs'].get(sc, []))
                            if not matched_e:
                                untracked_empty_tags.add(sg['tag'])
                            self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], matched_e if matched_e or len(sgs) > 1 else None)
                    elif kind == 'dict_meta':
                        owners = payload
                        for sg in sgs:
                            matched_m: set[Any] = set()
                            for o in owners:
                                matched_m.update(sg['dialects'].get(o.split('.')[0], []))
                                matched_m.update(sg['dialects'].get(o, []))
                            if not matched_m:
                                untracked_empty_tags.add(sg['tag'])
                            self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], matched_m if matched_m or len(sgs) > 1 else None)
                    elif kind == 'dict_meta_lines':
                        owners, line_specs = payload
                        for sg in sgs:
                            d_cases_ml: set[Any] = set()
                            for o in owners:
                                d_cases_ml.update(sg['dialects'].get(o.split('.')[0], []))
                                d_cases_ml.update(sg['dialects'].get(o, []))
                            l_cases_ml: set[Any] = set()
                            for lf, lset in line_specs:
                                l_cases_ml.update(self._subtest_match_lines(sg, lf, lset))
                            if d_cases_ml and l_cases_ml:
                                inter_ml = d_cases_ml & l_cases_ml
                                if not inter_ml:
                                    proven_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], inter_ml)
                            else:
                                fb_ml = d_cases_ml or l_cases_ml
                                if not fb_ml:
                                    untracked_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], fb_ml if fb_ml or len(sgs) > 1 else None)
                    elif kind == 'dict_keys':
                        owners, attr, dict_keys, rel, old_lines = payload
                        for sg in sgs:
                            matched_d: set[Any] = set()
                            dk_oa = sg.get('dict_keys_by_oa')
                            if dk_oa is not None:
                                for o in owners:
                                    b = dk_oa.get(o, {}).get(attr)
                                    if b:
                                        for kr in dict_keys:
                                            matched_d.update(self._match_dict_key_in_bucket(b, kr))
                            else:
                                for dk_full, cids in sg.get('dict_keys', {}).items():
                                    do, da, dk = dk_full.split('|', 2)
                                    if da == attr and do in owners:
                                        if dk == '*':
                                            matched_d.update(cids)
                                        else:
                                            for kr in dict_keys:
                                                if dk == kr or (not kr.startswith(("'", '"')) and (dk == f"TYPE:{kr.rsplit('.', 1)[-1]}" or (dk.startswith('ENUM:') and dk.endswith(f".{kr.rsplit('.', 1)[-1]}")) or (dk.startswith('<') and f".{kr.rsplit('.', 1)[-1]}:" in dk))):
                                                    matched_d.update(cids)
                            matched_d.update(self._subtest_match_lines(sg, rel, old_lines))
                            if not matched_d:
                                untracked_empty_tags.add(sg['tag'])
                            self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], matched_d)
                    elif kind == 'dialect_readers_exprs':
                        subs, base_cls, readers, g_exprs = payload
                        for sg in sgs:
                            d_cases: set[Any] = set()
                            for sc in subs:
                                d_cases.update(sg['dialects'].get(sc, []))
                                if '.' in sc:
                                    d_cases.update(sg['dialects'].get(sc.split('.')[0], []))
                            d_cases.update(sg['dialects'].get(base_cls, []))
                            r_cases: set[Any] = set()
                            for rfile, rline in readers:
                                r_cases.update(self._subtest_match_lines(sg, rfile, {rline}))
                            e_cases: set[Any] = set()
                            for ec in g_exprs:
                                e_cases.update(sg['exprs'].get(ec, []))
                            if d_cases and r_cases and e_cases:
                                inter = d_cases & r_cases & e_cases
                                if not inter:
                                    proven_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], inter)
                            elif d_cases and e_cases:
                                inter = d_cases & e_cases
                                if not inter:
                                    proven_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], inter)
                            elif d_cases and r_cases:
                                inter = d_cases & r_cases
                                if not inter:
                                    proven_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], inter)
                            else:
                                fb_dre = d_cases or r_cases or e_cases
                                if not fb_dre:
                                    untracked_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], fb_dre if fb_dre or len(sgs) > 1 else None)
                    elif kind == 'dialect_readers':
                        subs, base_cls, readers = payload
                        for sg in sgs:
                            d_cases: set[Any] = set()
                            for sc in subs:
                                d_cases.update(sg['dialects'].get(sc, []))
                                if '.' in sc:
                                    d_cases.update(sg['dialects'].get(sc.split('.')[0], []))
                            d_cases.update(sg['dialects'].get(base_cls, []))
                            r_cases: set[Any] = set()
                            for rfile, rline in readers:
                                r_cases.update(self._subtest_match_lines(sg, rfile, {rline}))
                            if d_cases and r_cases:
                                inter = d_cases & r_cases
                                if not inter:
                                    proven_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], inter)
                            else:
                                fb_dr = d_cases or r_cases
                                if not fb_dr:
                                    untracked_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], fb_dr if fb_dr or len(sgs) > 1 else None)
                    elif kind == 'transforms':
                        owners, expr_subs, rel, old_lines = payload
                        for sg in sgs:
                            d_cases_t: set[Any] = set()
                            for o in owners:
                                d_cases_t.update(sg['dialects'].get(o.split('.')[0], []))
                                d_cases_t.update(sg['dialects'].get(o, []))
                            e_cases_t: set[Any] = set()
                            for sc in expr_subs:
                                e_cases_t.update(sg['exprs'].get(sc, []))
                            l_cases_t = self._subtest_match_lines(sg, rel, old_lines)
                            if d_cases_t and e_cases_t:
                                matched_t = d_cases_t & e_cases_t | l_cases_t
                                if not matched_t:
                                    proven_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], matched_t)
                            else:
                                fb_t = d_cases_t | e_cases_t | l_cases_t
                                if not fb_t:
                                    untracked_empty_tags.add(sg['tag'])
                                self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], fb_t if fb_t or len(sgs) > 1 else None)
                    elif kind == 'dialect':
                        subs, base_cls = payload
                        for sg in sgs:
                            d_cases2: set[Any] = set()
                            for sc in subs:
                                d_cases2.update(sg['dialects'].get(sc, []))
                                if '.' in sc:
                                    d_cases2.update(sg['dialects'].get(sc.split('.')[0], []))
                            d_cases2.update(sg['dialects'].get(base_cls, []))
                            if not d_cases2:
                                untracked_empty_tags.add(sg['tag'])
                            self._add_subtest_filter(commit_plan.subtest_filters, rnum, sg['tag'], d_cases2 if d_cases2 or len(sgs) > 1 else None)
        tf = commit_plan.subtest_filters.get(rnum)
        if tf:
            proven_only = proven_empty_tags - untracked_empty_tags
            if 'val-calls' in tf and tf['val-calls'] is not None and (len(tf['val-calls']) == 0) and ('val-calls' not in proven_only):
                tf['val-calls'] = None
            if 'gen-mem' in tf and tf['gen-mem'] is not None and (len(tf['gen-mem']) == 0) and ('gen-mem' not in proven_only):
                tf['gen-mem'] = None
            for k in list(tf.keys()):
                if tf[k] is not None and len(tf[k]) == 0 and (k in untracked_empty_tags) and (k not in proven_only):
                    if not any((v is not None and len(v) > 0 for v in tf.values())):
                        tf[k] = None
            if all((v is not None and len(v) == 0 for v in tf.values())):
                for k in list(tf.keys()):
                    if k not in proven_only:
                        tf[k] = None
        return tf

    def _is_covered_same_attr_copy(self, rfile: str, rline: int, attr: str, owners_set: set[str], unit: ChangeUnit) -> bool:
        if rfile in ('sqlglot/dialects/dialect.py', 'sqlglot/tokens.py'):
            return True
        if rfile == 'sqlglot/parser.py' and unit.class_name and ('JSONPath' in unit.class_name):
            return True
        lines = self._read_base_lines(rfile)
        line_txt = lines[rline - 1].strip() if 0 < rline <= len(lines) else ''
        if f'IsPredicatePrecedence.{attr}' in line_txt:
            return True
        lam_idx = line_txt.find('lambda')
        if lam_idx >= 0:
            col_idx = line_txt.find(':', lam_idx)
            if col_idx >= 0 and attr not in line_txt[:col_idx] and (f'.{attr}' in line_txt[col_idx:]):
                return True
        if rfile == 'sqlglot/parsers/teradata.py' and attr == 'FUNCTION_PARSERS' and ('TRY_CAST' in line_txt) and (unit.dict_keys is not None) and ("'TRY_CAST'" not in unit.dict_keys) and ('"TRY_CAST"' not in unit.dict_keys):
            return True
        file_classes = self._file_classes_cache.get(rfile, ())
        if any((c in owners_set or any((o.startswith(f'{c}.') for o in owners_set)) for c in file_classes)):
            if '**' in line_txt and f'.{attr}' in line_txt or f'.{attr}.copy()' in line_txt or f'.{attr}.items()' in line_txt or (line_txt.startswith(f'{attr} = ') and f'.{attr}' in line_txt) or (attr == 'PROPERTIES_LOCATION' and f'.{attr}' in line_txt and ('for ' in line_txt)):
                return True
            if f'.{attr}' in line_txt and ('*' in line_txt or '|' in line_txt or '-' in line_txt):
                for back in range(1, min(5, rline)):
                    prev_txt = lines[rline - 1 - back].strip()
                    if prev_txt.startswith((f'{attr} =', f'{attr}:')):
                        return True
                    if prev_txt.startswith(('def ', 'class ')) or (' = ' in prev_txt and (not prev_txt.startswith('*'))):
                        break
        return False

    def _dialect_hits_for_owners(self, owners: list[str]) -> set[int]:
        out: set[int] = set()
        dmap = self.maps['dialect_to_tests']
        for o in owners:
            out.update(self._to_rnums(dmap.get(o.split('.')[0], [])))
            out.update(self._to_rnums(dmap.get(o, [])))
        return out

    def _handle_change_unit(self, unit: ChangeUnit, candidates: set[int], subtest_filters: dict[int, dict[str, set[Any] | None]], deferred_subtests: dict[int, list[tuple[str, Any]]], dict_factory_files: set[str] | None=None) -> None:
        rel = unit.file
        has_sub = self._subtest_tags_by_num
        if unit.kind == 'syntax_error':
            broken = self._tests_for_broken_import(rel)
            candidates.update(broken)
            for rnum in broken:
                for tag in has_sub.get(rnum, ()):
                    self._add_subtest_filter(subtest_filters, rnum, tag, None)
            return
        if rel.startswith('tests/'):
            if unit.kind == 'func_body' and unit.symbol_name and ('::' not in unit.symbol_name):
                parts = unit.symbol_name.split('.')
                if len(parts) == 2 and parts[1].startswith('test_'):
                    exact_id = f'{rel}::{parts[0]}::{parts[1]}'
                    rnum = self.id_to_num.get(exact_id)
                    if rnum is not None:
                        matched_nums = {rnum}
                        for imp_tf in self.maps['import_file_to_test_files'].get(rel, []):
                            for cand_num in self.file_to_test_nums.get(imp_tf, []):
                                if self.num_to_test[cand_num]['id'].endswith(f'::{parts[1]}'):
                                    matched_nums.add(cand_num)
                        candidates.update(matched_nums)
                        for mnum in matched_nums:
                            for tag in has_sub.get(mnum, ()):
                                self._add_subtest_filter(subtest_filters, mnum, tag, None)
                        return
                elif rel == 'tests/helpers.py' and len(parts) == 1 and (not unit.sig_changed):
                    htree = self._parse_base_ast(rel)
                    fn_h = parts[0]
                    if htree is not None and fn_h:
                        top_defs: dict[str, ast.AST] = {}
                        top_level_safe = True
                        for _st in htree.body:
                            if isinstance(_st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                top_defs[_st.name] = _st
                                for _dec in _st.decorator_list:
                                    if any((isinstance(_n, ast.Name) and _n.id == fn_h or (isinstance(_n, ast.Attribute) and _n.attr == fn_h) for _n in ast.walk(_dec))):
                                        top_level_safe = False
                                if isinstance(_st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                    for _df in list(_st.args.defaults) + [d for d in _st.args.kw_defaults if d is not None]:
                                        if any((isinstance(_n, ast.Name) and _n.id == fn_h or (isinstance(_n, ast.Attribute) and _n.attr == fn_h) for _n in ast.walk(_df))):
                                            top_level_safe = False
                            elif not isinstance(_st, (ast.Import, ast.ImportFrom)):
                                if any((isinstance(_n, ast.Name) and _n.id == fn_h or (isinstance(_n, ast.Attribute) and _n.attr == fn_h) for _n in ast.walk(_st))):
                                    top_level_safe = False
                        if top_level_safe and fn_h in top_defs:
                            reach_h: set[str] = {fn_h}
                            changed_r = True
                            while changed_r:
                                changed_r = False
                                for _dn, _dnode in top_defs.items():
                                    if _dn in reach_h:
                                        continue
                                    for _n in ast.walk(_dnode):
                                        if isinstance(_n, ast.Name) and _n.id in reach_h or (isinstance(_n, ast.Attribute) and _n.attr in reach_h):
                                            reach_h.add(_dn)
                                            changed_r = True
                                            break
                            for _st in htree.body:
                                if not isinstance(_st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                                    if any((isinstance(_n, ast.Name) and _n.id in reach_h or (isinstance(_n, ast.Attribute) and _n.attr in reach_h) for _n in ast.walk(_st))):
                                        top_level_safe = False
                                        break
                            if top_level_safe:
                                line_h = self._tests_covering_lines(rel, unit.old_lines or unit.enclosing_lines)
                                seed_tfs: set[str] = {self.num_to_test[r]['file'] for r in line_h}
                                for imp_tf in self.maps['import_file_to_test_files'].get(rel, []):
                                    txt_tf = self._read_base_file(imp_tf)
                                    if any((nm in txt_tf for nm in reach_h)) or 'import *' in txt_tf or ('getattr(' in txt_tf and 'helpers' in txt_tf):
                                        seed_tfs.add(imp_tf)
                                if seed_tfs:
                                    q_tf = list(seed_tfs)
                                    while q_tf:
                                        cur_tf = q_tf.pop()
                                        for nxt_tf in self.maps['import_file_to_test_files'].get(cur_tf, []):
                                            if nxt_tf not in seed_tfs:
                                                seed_tfs.add(nxt_tf)
                                                q_tf.append(nxt_tf)
                                    affected_h = set(line_h)
                                    for tf_h in seed_tfs:
                                        affected_h.update(self.file_to_test_nums.get(tf_h, []))
                                    candidates.update(affected_h)
                                    for rnum in affected_h:
                                        for tag in has_sub.get(rnum, ()):
                                            self._add_subtest_filter(subtest_filters, rnum, tag, None)
                                    return
            affected = set(self.file_to_test_nums.get(rel, []))
            for tf in self.maps['import_file_to_test_files'].get(rel, []):
                affected.update(self.file_to_test_nums.get(tf, []))
            affected.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
            candidates.update(affected)
            for rnum in affected:
                for tag in has_sub.get(rnum, ()):
                    self._add_subtest_filter(subtest_filters, rnum, tag, None)
            return
        if rel.startswith('sqlglotc/'):
            candidates.update(self.file_to_test_nums.get('tests/test_sqlglotc.py', []))
            return
        if rel.startswith('sqlglot/typing/') and self.typing_test_num is not None:
            candidates.add(self.typing_test_num)
        if rel == 'sqlglot/executor/__init__.py':
            for tid in ('tests/test_executor.py::TestExecutor::test_execute_tpch', 'tests/test_executor.py::TestExecutor::test_execute_tpcds'):
                rnum = self.id_to_num.get(tid)
                if rnum is not None:
                    candidates.add(rnum)
                    for tag in has_sub.get(rnum, ()):
                        self._add_subtest_filter(subtest_filters, rnum, tag, None)
        if unit.kind == 'func_body':
            target_lines = unit.old_lines
            hits = self._tests_covering_lines(rel, target_lines)
            if not hits and unit.enclosing_lines:
                target_lines = unit.enclosing_lines
                hits = self._tests_covering_lines(rel, target_lines)
            meth_name = (unit.symbol_name or '').rsplit('.', 1)[-1]
            is_meta_hook = meth_name in ('__new__', '__init_subclass__', '__set_name__', '__class_getitem__', '_generate_next_value_', '_build_dispatch', 'sql_names')
            is_import_called = False
            import_ref_files: set[str] = set()
            if not unit.class_name or is_meta_hook:
                was_called, was_ref, f_set = self._is_func_called_at_import(rel, meth_name)
                if was_called or (not hits and was_ref):
                    is_import_called = True
                    import_ref_files = f_set
            if is_meta_hook or (is_import_called and (unit.edit_outside_nested_func or not hits)):
                if not is_meta_hook and is_import_called and (dict_factory_files is not None):
                    factory_sites = self._find_dict_only_import_sites(rel, meth_name, import_ref_files)
                    if factory_sites:
                        dict_factory_files.add(rel)
                        dict_factory_files.update(import_ref_files)
                        candidates.update(hits)
                        spec_f = ('lines', (rel, target_lines))
                        for rnum in hits:
                            if rnum in has_sub:
                                deferred_subtests.setdefault(rnum, []).append(spec_f)
                        grouped_sites: dict[tuple[str, str, str], set[str]] = {}
                        for rf, cname, aname, k_repr in factory_sites:
                            grouped_sites.setdefault((rf, cname, aname), set()).add(k_repr)
                        for (rf, cname, aname), k_set in grouped_sites.items():
                            synth_unit = ChangeUnit(kind='dict_key', file=rf, class_name=cname, symbol_name=aname, dict_keys=k_set, old_lines=set(), enclosing_lines=set())
                            self._handle_change_unit(synth_unit, candidates, subtest_filters, deferred_subtests, dict_factory_files)
                        return
                extra = self._tests_for_broken_import(rel) if is_meta_hook else set()
                for rf in import_ref_files:
                    extra.update(self._tests_for_broken_import(rf))
                if is_meta_hook or extra:
                    all_hits = hits | extra
                    candidates.update(all_hits)
                    for rnum in all_hits:
                        for tag in has_sub.get(rnum, ()):
                            self._add_subtest_filter(subtest_filters, rnum, tag, None)
                    return
            candidates.update(hits)
            spec = ('lines', (rel, target_lines))
            for rnum in hits:
                if rnum in has_sub:
                    deferred_subtests.setdefault(rnum, []).append(spec)
            return
        if unit.kind == 'dict_lambda_body':
            hits = self._tests_covering_lines(rel, unit.old_lines)
            if hits:
                candidates.update(hits)
                spec = ('lines', (rel, unit.old_lines))
                for rnum in hits:
                    if rnum in has_sub:
                        deferred_subtests.setdefault(rnum, []).append(spec)
                return
        if unit.kind in ('dict_key', 'dict_lambda_body'):
            attr = unit.symbol_name or ''
            if attr == 'arg_types' and unit.class_name:
                subs = self._subclasses_of(unit.class_name)
                hits: set[int] = set()
                for sc in subs:
                    hits.update(self._to_rnums(self.maps['expr_to_tests'].get(sc, [])))
                hits.update(self._tests_covering_lines(rel, unit.old_lines))
                if not hits:
                    hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                candidates.update(hits)
                spec = ('exprs', subs)
                for rnum in hits:
                    if rnum in has_sub:
                        deferred_subtests.setdefault(rnum, []).append(spec)
                return
            owners: list[str] = []
            if unit.class_name:
                if unit.class_name == 'Tokenizer':
                    owners = sorted({o for o in self.dict_keys_by_oa if 'Tokenizer' in o} | set(self._expand_class_and_copiers('Tokenizer', attr)))
                elif unit.class_name == 'Parser':
                    owners = sorted({o for o in self.dict_keys_by_oa if 'Parser' in o} | set(self._expand_class_and_copiers('Parser', attr)))
                elif unit.class_name == 'Generator':
                    owners = sorted({o for o in self.dict_keys_by_oa if 'Generator' in o} | set(self._expand_class_and_copiers('Generator', attr)))
                elif unit.class_name == 'Dialect':
                    owners = sorted(set(self.dict_keys_by_oa.keys()) | set(self._expand_class_and_copiers('Dialect', attr)))
                else:
                    owners = self._expand_class_and_copiers(unit.class_name, attr)
                    if unit.class_name not in owners:
                        owners.append(unit.class_name)
            elif rel.startswith('sqlglot/typing/'):
                mod_owner = rel.removesuffix('.py').replace('/', '.')
                owner_set: set[str] = {mod_owner}
                closure = self._rev_import_closure(rel)
                for cf in closure:
                    if cf.startswith('sqlglot/typing/'):
                        owner_set.add(cf.removesuffix('.py').replace('/', '.'))
                    for cname in self._file_classes_cache.get(cf, ()):
                        owner_set.update(self._expand_class_and_copiers(cname, attr))
                if 'Dialect' in owner_set or rel == 'sqlglot/typing/__init__.py':
                    for o, o_map in self.dict_keys_by_oa.items():
                        if attr in o_map:
                            owner_set.add(o)
                    owner_set.update(self.maps['class_subclasses'].get('Dialect', []))
                owners = sorted(owner_set)
            dep_graph = self._get_class_dep_graph()
            if attr == 'TRANSFORMS':
                dialect_hits = self._dialect_hits_for_owners(owners)
                mtid_hits_t: set[int] = set()
                if unit.dict_keys is None:
                    for o in owners:
                        for mtids in self.dict_keys_by_oa.get(o, {}).get(attr, {}).values():
                            mtid_hits_t.update(mtids)
                else:
                    for o in owners:
                        for kr in unit.dict_keys:
                            mtid_hits_t.update(self._match_dict_key(o, attr, kr))
                direct_hits = self._to_rnums(mtid_hits_t) | self._tests_covering_lines(rel, unit.old_lines)
                if self._dep_index is not None and unit.dict_keys is not None and (not (attr in dep_graph['attr_mutators'] and unit.dict_deleted_keys)):
                    all_expr_d = True
                    expr_subs_d: set[str] = set()
                    for kr in unit.dict_keys:
                        ck = _di.canon_source_key(kr, self.all_expr_classes)
                        if ck is not None and ck.startswith('E:'):
                            for sc in self._subclasses_of(ck[2:]):
                                expr_subs_d.add(sc)
                        else:
                            all_expr_d = False
                            break
                    if all_expr_d and expr_subs_d:
                        gd = self._dep_index['gen_dispatch']
                        db_ra = self._dep_index['dict_by_ra']
                        exact_m: set[int] = set()
                        for o in owners:
                            for sc in expr_subs_d:
                                exact_m.update(gd.get((o, sc), ()))
                            t_map = db_ra.get((o, 'TRANSFORMS'))
                            if t_map:
                                exact_m.update(t_map.get('*', ()))
                                for sc in expr_subs_d:
                                    exact_m.update(t_map.get(f'E:{sc}', ()))
                        hits = self._to_rnums(exact_m) | self._dep_always_keep_rnums & (dialect_hits | self._to_rnums(self.maps['file_cov'].get(rel, []))) | self._tests_covering_lines(rel, unit.old_lines)
                        if unit.class_name and unit.class_name.split('.')[0] not in self.all_dialect_family:
                            hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                        candidates.update(hits)
                        spec = ('transforms', (owners, sorted(expr_subs_d), rel, unit.old_lines))
                        for rnum in hits:
                            if rnum in has_sub:
                                deferred_subtests.setdefault(rnum, []).append(spec)
                        return
                use_expr_inter = False
                expr_subs: set[str] = set()
                expr_hits: set[int] = set()
                if unit.dict_keys is not None and (not unit.dict_deleted_keys) and (not (attr in dep_graph['attr_mutators'] and unit.dict_deleted_keys)):
                    all_expr = True
                    for kr in unit.dict_keys:
                        last = kr.rsplit('.', 1)[-1]
                        if last in self.all_expr_classes:
                            for sc in self._subclasses_of(last):
                                expr_subs.add(sc)
                                expr_hits.update(self._to_rnums(self.maps['expr_to_tests'].get(sc, [])))
                        else:
                            all_expr = False
                            break
                    if all_expr and expr_hits and dialect_hits & expr_hits:
                        use_expr_inter = True
                if use_expr_inter:
                    hits = dialect_hits & expr_hits | direct_hits
                    spec = ('transforms', (owners, sorted(expr_subs), rel, unit.old_lines))
                else:
                    hits = dialect_hits | direct_hits
                    spec = ('dict_meta', owners)
                if unit.class_name and unit.class_name.split('.')[0] not in self.all_dialect_family:
                    hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                candidates.update(hits)
                for rnum in hits:
                    if rnum in has_sub:
                        deferred_subtests.setdefault(rnum, []).append(spec)
                return
            if self._dep_index is not None and attr in self._dep_tracked_dicts and (attr not in _di.META_SKIP_ATTRS) and (unit.dict_keys is not None) and (not (attr in ({'SHOW_PARSERS', 'SET_PARSERS', 'NO_PAREN_FUNCTIONS', 'QUERY_MODIFIER_PARSERS'} if unit.class_name == 'Parser' else {'SHOW_PARSERS', 'SET_PARSERS'}) and bool(unit.dict_added_keys or unit.dict_deleted_keys))) and (not (bool(unit.dict_deleted_keys) and attr in dep_graph['attr_mutators'])):
                canon_ks: list[str] = []
                all_ck_ok = True
                for kr in unit.dict_keys:
                    ck = _di.canon_source_key(kr, self.all_expr_classes)
                    if ck is None:
                        all_ck_ok = False
                        break
                    canon_ks.append(ck)
                if all_ck_ok and canon_ks:
                    db_ra = self._dep_index['dict_by_ra']
                    exact_dm: set[int] = set()
                    for o in owners:
                        dmap = db_ra.get((o, attr))
                        if dmap:
                            exact_dm.update(dmap.get('*', ()))
                            for ck in canon_ks:
                                exact_dm.update(dmap.get(ck, ()))
                                if ck.startswith('E:'):
                                    for sc in self._subclasses_of(ck[2:]):
                                        exact_dm.update(dmap.get(f'E:{sc}', ()))
                    non_rt_extra: set[int] = set()
                    readers_d = self.maps['attr_readers'].get(attr, [])
                    if readers_d:
                        rev_cl = self._rev_import_closure(rel)
                        owners_set = set(owners)
                        for rfile, rline in readers_d:
                            if rfile in rev_cl and rfile != rel and (rline in self._get_non_runtime_info(rfile)[0]) and (not self._is_covered_same_attr_copy(rfile, rline, attr, owners_set, unit)):
                                non_rt_extra.update(self._to_rnums(self.maps['file_cov'].get(rfile, [])))
                                non_rt_extra.update(self._tests_for_broken_import(rfile))
                    dialect_hits_d = self._dialect_hits_for_owners(owners)
                    hits = self._to_rnums(exact_dm) | self._dep_always_keep_rnums & (dialect_hits_d | self._to_rnums(self.maps['file_cov'].get(rel, []))) | self._tests_covering_lines(rel, unit.old_lines) | non_rt_extra
                    if unit.class_name and unit.class_name.split('.')[0] not in self.all_dialect_family:
                        hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                    candidates.update(hits)
                    if unit.dict_added_keys or unit.dict_deleted_keys:
                        spec: tuple[str, Any] = ('dict_meta', owners)
                    else:
                        spec = ('dict_keys', (owners, attr, unit.dict_keys, rel, unit.old_lines))
                    for rnum in hits:
                        if rnum in non_rt_extra:
                            for tag in has_sub.get(rnum, ()):
                                self._add_subtest_filter(subtest_filters, rnum, tag, None)
                        elif rnum in has_sub:
                            deferred_subtests.setdefault(rnum, []).append(spec)
                    return
            tracked = any((attr in self.dict_keys_by_oa.get(o, {}) for o in owners))
            if tracked:
                mtid_hits: set[int] = set()
                any_zero_hit = False
                if unit.dict_keys is None:
                    for o in owners:
                        for mtids in self.dict_keys_by_oa.get(o, {}).get(attr, {}).values():
                            mtid_hits.update(mtids)
                else:
                    for kr in unit.dict_keys:
                        key_found = False
                        for o in owners:
                            mh = self._match_dict_key(o, attr, kr)
                            if mh:
                                key_found = True
                                mtid_hits.update(mh)
                        if not key_found and (kr in unit.dict_added_keys or kr in unit.dict_deleted_keys or (not kr.startswith(("'", '"')))):
                            any_zero_hit = True
                hits = self._to_rnums(mtid_hits)
                hits.update(self._tests_covering_lines(rel, unit.old_lines))
                unconditional_meta_attrs = {'TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING', 'UNESCAPED_SEQUENCES', 'ESCAPED_SEQUENCES', 'DATE_PART_MAPPING', 'CREATABLE_KIND_MAPPING', 'QUOTES', 'IDENTIFIERS', 'STRING_ESCAPES', 'BYTE_STRING_ESCAPES', 'IDENTIFIER_ESCAPES', 'SUPPORTED_JSON_PATH_PARTS', 'SINGLE_TOKENS', 'NUMERIC_LITERALS', 'NUMERIC_ESCAPES', 'COERCES_TO'}
                keyset_sensitive_attrs = {'SHOW_PARSERS', 'SET_PARSERS'}
                if unit.class_name == 'Parser':
                    keyset_sensitive_attrs = {'SHOW_PARSERS', 'SET_PARSERS', 'NO_PAREN_FUNCTIONS', 'QUERY_MODIFIER_PARSERS'}
                has_keyset_change = bool(unit.dict_added_keys or unit.dict_deleted_keys)
                kw_needs_trie = attr == 'KEYWORDS' and (has_keyset_change or unit.dict_keys is None or any((not _PLAIN_IDENT_KEY_RE.match(kr) for kr in unit.dict_keys)))
                need_meta_expand = attr in unconditional_meta_attrs or (attr in keyset_sensitive_attrs and has_keyset_change) or kw_needs_trie or (unit.dict_keys is None) or any_zero_hit or (bool(unit.dict_deleted_keys) and attr in dep_graph['attr_mutators'])
                meta_line_specs: list[tuple[str, set[int]]] | None = None
                if need_meta_expand:
                    d_hits = self._dialect_hits_for_owners(owners)
                    if attr == 'NUMERIC_ESCAPES':
                        if has_keyset_change or unit.dict_keys is None:
                            meta_line_specs = [('sqlglot/tokenizer_core.py', {1219, 1240, 1250, 1362, 1363}), ('sqlglot/generator.py', {1723, 3279, 3283})]
                        else:
                            meta_line_specs = [('sqlglot/tokenizer_core.py', {1219, 1240, 1250, 1363})]
                    elif attr == 'NUMERIC_LITERALS':
                        meta_line_specs = [('sqlglot/tokenizer_core.py', {1047})]
                    elif attr == 'CREATABLE_KIND_MAPPING':
                        meta_line_specs = [('sqlglot/parser.py', {2557, 2848}), ('sqlglot/generator.py', {1403, 1932})]
                    elif attr == 'SHOW_PARSERS':
                        meta_line_specs = [('sqlglot/parser.py', {10083}), ('sqlglot/parsers/singlestore.py', {197})]
                    elif attr == 'SET_PARSERS':
                        meta_line_specs = [('sqlglot/parser.py', {10118}), ('sqlglot/parsers/duckdb.py', {328}), ('sqlglot/parsers/mysql.py', {259}), ('sqlglot/parsers/spark.py', {64}), ('sqlglot/parsers/teradata.py', {191})]
                    elif attr in ('TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING'):
                        ml_by_file: dict[str, set[int]] = {'sqlglot/time.py': set(range(1, 120))}
                        for ta in ('TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING', 'TIME_TRIE', 'FORMAT_TRIE', 'INVERSE_TIME_TRIE', 'INVERSE_FORMAT_TRIE'):
                            for rf, rl in self.maps['attr_readers'].get(ta, []):
                                if rl not in self._get_non_runtime_info(rf)[0]:
                                    ml_by_file.setdefault(rf, set()).add(rl)
                        meta_line_specs = list(ml_by_file.items())
                        hits.update(self.file_to_test_nums.get('tests/test_time.py', []))
                    elif attr == 'DATE_PART_MAPPING':
                        meta_line_specs = [('sqlglot/dialects/dialect.py', {2576}), ('sqlglot/parser.py', {6583})]
                    elif attr == 'ESCAPED_SEQUENCES':
                        meta_line_specs = [('sqlglot/generator.py', {3279}), ('sqlglot/generators/bigquery.py', {739})]
                    elif attr == 'COERCES_TO':
                        meta_line_specs = [('sqlglot/optimizer/annotate_types.py', {208}), ('sqlglot/optimizer/canonicalize.py', {207})]
                    elif attr == 'ALTER_ALTER_PARSERS':
                        meta_line_specs = [('sqlglot/parser.py', {9585, 9586})]
                    elif attr == 'VERSION_PHRASES':
                        meta_line_specs = [('sqlglot/parser.py', {5373})]
                    elif attr == 'LAMBDAS':
                        meta_line_specs = [('sqlglot/parser.py', {7864, 7865, 7868, 7871, 7872})]
                    elif attr == 'ALTER_PARSERS':
                        meta_line_specs = [('sqlglot/parser.py', {9757, 9763, 9764, 9789, 9790})]
                    elif attr == 'TYPE_LITERAL_PARSERS':
                        meta_line_specs = [('sqlglot/parser.py', {6804})]
                    elif attr == 'PROPERTIES_LOCATION':
                        meta_line_specs = [('sqlglot/generator.py', {2222, 2268})]
                    elif attr == 'PROPERTY_PARSERS':
                        ml_pp: dict[str, set[int]] = {}
                        for rf, rl in self.maps['attr_readers'].get(attr, []):
                            if rl not in self._get_non_runtime_info(rf)[0]:
                                ml_pp.setdefault(rf, set()).add(rl)
                        if ml_pp:
                            meta_line_specs = list(ml_pp.items())
                    elif attr == 'CONSTRAINT_PARSERS':
                        ml_cp: dict[str, set[int]] = {}
                        for rf, rl in self.maps['attr_readers'].get(attr, []):
                            if rl not in self._get_non_runtime_info(rf)[0]:
                                ml_cp.setdefault(rf, set()).add(rl)
                        if ml_cp:
                            meta_line_specs = list(ml_cp.items())
                    if meta_line_specs is not None:
                        rt_hits: set[int] = set()
                        for lf, lset in meta_line_specs:
                            rt_hits.update(self._tests_covering_lines(lf, lset))
                        inter_m = d_hits & rt_hits
                        if inter_m:
                            hits.update(inter_m)
                        else:
                            hits.update(d_hits)
                            meta_line_specs = None
                    elif unit.class_name == 'JSONPathTokenizer' or (unit.class_name or '').endswith('.JSONPathTokenizer'):
                        jp_hits = self._to_rnums(self.maps['file_cov'].get('sqlglot/jsonpath.py', [])) | self._to_rnums(self.maps['dialect_to_tests'].get('JSONPathTokenizer', []))
                        inter_jp = d_hits & jp_hits
                        hits.update(inter_jp if inter_jp else jp_hits)
                    else:
                        hits.update(d_hits)
                    if rel.startswith('sqlglot/typing/'):
                        hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                non_rt_extra: set[int] = set()
                readers_d = self.maps['attr_readers'].get(attr, [])
                if readers_d:
                    rev_cl = self._rev_import_closure(rel)
                    owners_set = set(owners)
                    for rfile, rline in readers_d:
                        if rfile in rev_cl and rfile != rel and (rline in self._get_non_runtime_info(rfile)[0]) and (not self._is_covered_same_attr_copy(rfile, rline, attr, owners_set, unit)):
                            non_rt_extra.update(self._to_rnums(self.maps['file_cov'].get(rfile, [])))
                            non_rt_extra.update(self._tests_for_broken_import(rfile))
                if unit.class_name and unit.class_name.split('.')[0] not in self.all_dialect_family and (unit.class_name not in self.all_dialect_family):
                    non_rt_extra.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                hits.update(non_rt_extra)
                candidates.update(hits)
                if need_meta_expand:
                    if meta_line_specs is not None:
                        spec = ('dict_meta_lines', (owners, meta_line_specs))
                    else:
                        spec = ('dict_meta', owners)
                else:
                    spec = ('dict_keys', (owners, attr, unit.dict_keys, rel, unit.old_lines))
                for rnum in hits:
                    if rnum in non_rt_extra:
                        for tag in has_sub.get(rnum, ()):
                            self._add_subtest_filter(subtest_filters, rnum, tag, None)
                    elif rnum in has_sub:
                        deferred_subtests.setdefault(rnum, []).append(spec)
                return
        if unit.kind in ('class_attr', 'dict_key', 'dict_lambda_body') and unit.class_name:
            attr = unit.symbol_name or '*'
            subs = self._expand_class_and_copiers(unit.class_name, attr)
            base_cls = unit.class_name.split('.')[0]
            dep_graph = self._get_class_dep_graph()
            if base_cls in self.all_expr_classes or unit.class_name in self.all_expr_classes:
                hits = set()
                for sc in subs:
                    hits.update(self._to_rnums(self.maps['expr_to_tests'].get(sc, [])))
                hits.update(self._tests_covering_lines(rel, unit.old_lines))
                if attr in ('_sql_names', 'sql_names', '*'):
                    hits.update(self._to_rnums(self.maps['dialect_to_tests'].get('Parser', [])))
                    hits.update(self._to_rnums(self.maps['file_cov'].get('sqlglot/parser.py', [])))
                if not hits:
                    hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
                candidates.update(hits)
                spec = ('exprs', subs)
                for rnum in hits:
                    if rnum in has_sub:
                        if attr in ('_sql_names', 'sql_names', '*'):
                            for tag in has_sub.get(rnum, ()):
                                self._add_subtest_filter(subtest_filters, rnum, tag, None)
                        else:
                            deferred_subtests.setdefault(rnum, []).append(spec)
                return
            if base_cls in self.all_dialect_family or unit.class_name in self.all_dialect_family:
                dialect_hits = self._dialect_hits_for_owners(subs)
                dialect_hits.update(self._to_rnums(self.maps['dialect_to_tests'].get(base_cls, [])))
                if unit.class_name == 'JSONPathTokenizer' or unit.class_name.endswith('.JSONPathTokenizer'):
                    jp_hits_c = self._to_rnums(self.maps['file_cov'].get('sqlglot/jsonpath.py', [])) | self._to_rnums(self.maps['dialect_to_tests'].get('JSONPathTokenizer', []))
                    if dialect_hits & jp_hits_c:
                        dialect_hits = dialect_hits & jp_hits_c
                if attr.endswith('_sql') and (not attr.startswith('_')) and (attr != 'sql') and (base_cls.endswith('Generator') or unit.class_name.endswith('Generator')):
                    expr_cls_list = self._expr_key_to_cls.get(attr[:-4])
                    if expr_cls_list:
                        expr_subs_m: set[str] = set()
                        expr_hits_m: set[int] = set()
                        for ec in expr_cls_list:
                            for sc in self._subclasses_of(ec):
                                expr_subs_m.add(sc)
                                expr_hits_m.update(self._to_rnums(self.maps['expr_to_tests'].get(sc, [])))
                        direct_m = self._tests_covering_lines(rel, unit.old_lines)
                        for rfile, rline in self.maps['attr_readers'].get(attr, []):
                            direct_m.update(self._tests_covering_lines(rfile, {rline}))
                        inter_em = dialect_hits & expr_hits_m
                        if inter_em or direct_m:
                            hits_em = inter_em | direct_m
                            candidates.update(hits_em)
                            spec = ('transforms', (subs, sorted(expr_subs_m), rel, unit.old_lines))
                            for rnum in hits_em:
                                if rnum in has_sub:
                                    deferred_subtests.setdefault(rnum, []).append(spec)
                            return
                readers = self.maps['attr_readers'].get(attr, []) if attr != '*' else []
                is_gen_cls = (unit.class_name or '').endswith('Generator') or base_cls.endswith('Generator')
                is_par_cls = (unit.class_name or '').endswith('Parser') or base_cls.endswith('Parser')
                if readers and is_gen_cls ^ is_par_cls:
                    filtered_readers: list[tuple[str, int]] = []
                    for rfile, rline in readers:
                        if is_gen_cls and (rfile == 'sqlglot/parser.py' or rfile.startswith('sqlglot/parsers/')):
                            r_lines = self._read_base_lines(rfile)
                            r_txt = r_lines[rline - 1] if 0 < rline <= len(r_lines) else ''
                            if f'self.{attr}' in r_txt and 'dialect' not in r_txt.lower() and ('generator' not in r_txt.lower()):
                                continue
                        elif is_par_cls and (rfile == 'sqlglot/generator.py' or rfile.startswith('sqlglot/generators/')):
                            r_lines = self._read_base_lines(rfile)
                            r_txt = r_lines[rline - 1] if 0 < rline <= len(r_lines) else ''
                            if f'self.{attr}' in r_txt and 'dialect' not in r_txt.lower() and ('parser' not in r_txt.lower()):
                                continue
                        filtered_readers.append((rfile, rline))
                    readers = filtered_readers
                non_rt_extra_c: set[int] = set()
                has_non_rt_reader = False
                rev_cl_c = self._rev_import_closure(rel)
                core_meta_files = {'sqlglot/dialects/dialect.py', 'sqlglot/tokens.py', 'sqlglot/parser.py', 'sqlglot/generator.py'}
                if readers:
                    subs_set = set(subs)
                    for rfile, rline in readers:
                        if rline in self._get_non_runtime_info(rfile)[0]:
                            if self._is_covered_same_attr_copy(rfile, rline, attr, subs_set, unit):
                                continue
                            if rfile in rev_cl_c or rfile in core_meta_files:
                                has_non_rt_reader = True
                            if rfile in rev_cl_c and rfile != rel:
                                non_rt_extra_c.update(self._to_rnums(self.maps['file_cov'].get(rfile, [])))
                                non_rt_extra_c.update(self._tests_for_broken_import(rfile))
                cls_meta_specs: list[tuple[str, set[int]]] | None = None
                if attr in ('TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING'):
                    ml_by_file_c: dict[str, set[int]] = {'sqlglot/time.py': set(range(1, 120))}
                    for ta in ('TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING', 'TIME_TRIE', 'FORMAT_TRIE', 'INVERSE_TIME_TRIE', 'INVERSE_FORMAT_TRIE'):
                        for rf, rl in self.maps['attr_readers'].get(ta, []):
                            if rl not in self._get_non_runtime_info(rf)[0]:
                                ml_by_file_c.setdefault(rf, set()).add(rl)
                    cls_meta_specs = list(ml_by_file_c.items())
                elif attr == 'NUMERIC_ESCAPES':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1219, 1240, 1250, 1362, 1363}), ('sqlglot/generator.py', {1723, 3279, 3283})]
                elif attr == 'NUMERIC_LITERALS':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1047})]
                elif attr == 'CREATABLE_KIND_MAPPING':
                    cls_meta_specs = [('sqlglot/parser.py', {2557, 2848}), ('sqlglot/generator.py', {1403, 1932})]
                elif attr == 'SHOW_PARSERS':
                    cls_meta_specs = [('sqlglot/parser.py', {10083}), ('sqlglot/parsers/singlestore.py', {197})]
                elif attr == 'SET_PARSERS':
                    cls_meta_specs = [('sqlglot/parser.py', {10118}), ('sqlglot/parsers/duckdb.py', {328}), ('sqlglot/parsers/mysql.py', {259}), ('sqlglot/parsers/spark.py', {64}), ('sqlglot/parsers/teradata.py', {191})]
                elif attr == 'HEREDOC_TAG_IS_IDENTIFIER':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1126, 1131})]
                elif attr == 'NESTED_COMMENTS':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {927, 939})]
                elif attr == 'DASH_COMMENT_REQUIRES_BOUNDARY':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {908})]
                elif attr == 'COMMENTS_TERMINATE_AT_NEWLINE_ONLY':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {950})]
                elif attr == 'NUMBERS_CAN_HAVE_DECIMALS':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1009})]
                elif attr == 'DROP_UNKNOWN_ESCAPES':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1388}), ('sqlglot/generator.py', {1724})]
                elif attr == 'STRING_ESCAPES_ALLOWED_IN_RAW_STRINGS':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1405})]
                elif attr == 'BYTE_STRING_IS_BYTES_TYPE':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1155, 1354, 1393}), ('sqlglot/parser.py', {1263}), ('sqlglot/generator.py', {1734, 1738})]
                elif attr == 'TOKENS_PRECEDING_HINT':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {963})]
                elif attr == 'HEREDOC_STRING_ALTERNATIVE':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1138})]
                elif attr == 'NUMBERS_CAN_BE_UNDERSCORE_SEPARATED':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1024, 1033})]
                elif attr == 'IDENTIFIERS_CAN_START_WITH_DIGIT':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1051}), ('sqlglot/generator.py', {2182})]
                elif attr == 'LONE_SURROGATE_REPLACEMENT':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1267, 1270})]
                elif attr == 'NUMERIC_ESCAPES_ARE_BYTES':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {1219, 1240, 1250, 1362, 1363})]
                elif attr == 'UNICODE_START':
                    cls_meta_specs = [('sqlglot/generator.py', {1754})]
                elif attr == 'UNICODE_END':
                    cls_meta_specs = [('sqlglot/generator.py', {1758})]
                elif attr == 'COMMAND_PREFIX_TOKENS':
                    cls_meta_specs = [('sqlglot/tokenizer_core.py', {833})]
                elif attr == 'DATE_PART_MAPPING':
                    cls_meta_specs = [('sqlglot/dialects/dialect.py', {2576}), ('sqlglot/parser.py', {6583})]
                elif attr == 'ESCAPED_SEQUENCES':
                    cls_meta_specs = [('sqlglot/generator.py', {3279}), ('sqlglot/generators/bigquery.py', {739})]
                elif attr == 'ALTER_ALTER_PARSERS':
                    cls_meta_specs = [('sqlglot/parser.py', {9585, 9586})]
                elif attr == 'VERSION_PHRASES':
                    cls_meta_specs = [('sqlglot/parser.py', {5373})]
                elif attr == 'LAMBDAS':
                    cls_meta_specs = [('sqlglot/parser.py', {7864, 7865, 7868, 7871, 7872})]
                elif attr == 'COERCES_TO':
                    cls_meta_specs = [('sqlglot/optimizer/annotate_types.py', {208}), ('sqlglot/optimizer/canonicalize.py', {207})]
                if cls_meta_specs is not None:
                    rt_hits_c: set[int] = set()
                    for lf, lset in cls_meta_specs:
                        rt_hits_c.update(self._tests_covering_lines(lf, lset))
                    inter_cm = dialect_hits & rt_hits_c
                    if inter_cm:
                        hits_cm = inter_cm | non_rt_extra_c | self._tests_covering_lines(rel, unit.old_lines)
                        if attr in ('TIME_MAPPING', 'FORMAT_MAPPING', 'INVERSE_TIME_MAPPING', 'INVERSE_FORMAT_MAPPING'):
                            hits_cm.update(self.file_to_test_nums.get('tests/test_time.py', []))
                        candidates.update(hits_cm)
                        spec = ('dict_meta_lines', (subs, cls_meta_specs))
                        for rnum in hits_cm:
                            if rnum in non_rt_extra_c:
                                for tag in has_sub.get(rnum, ()):
                                    self._add_subtest_filter(subtest_filters, rnum, tag, None)
                            elif rnum in has_sub:
                                deferred_subtests.setdefault(rnum, []).append(spec)
                        return
                has_scoped_cb_ref = any((attr in self._get_non_runtime_info(rf)[1] for rf in rev_cl_c | core_meta_files))
                if self._dep_index is not None and attr != '*' and (attr in self._dep_tracked_attrs) and (attr not in self.dialect_meta_attrs) and (attr not in _di.META_SKIP_ATTRS) and (not has_non_rt_reader) and (not has_scoped_cb_ref):
                    ar_map = self._dep_index['attr_reads']
                    owners_a = self._expand_class_and_copiers(unit.class_name, attr)
                    exact_am: set[int] = set()
                    for o in owners_a:
                        exact_am.update(ar_map.get((o, attr), ()))
                    exact_rnums = self._to_rnums(exact_am)
                    spec_u: tuple[str, Any] = ('dialect_readers', (subs, base_cls, readers)) if readers else ('dialect', (subs, base_cls))
                    if readers:
                        g_exprs = self._guarded_isinstance_exprs(attr, readers)
                        if g_exprs:
                            g_hits: set[int] = set()
                            for ec in g_exprs:
                                g_hits.update(self._to_rnums(self.maps['expr_to_tests'].get(ec, [])))
                            if exact_rnums & g_hits:
                                exact_rnums = exact_rnums & g_hits
                                spec_u = ('dialect_readers_exprs', (subs, base_cls, readers, g_exprs))
                    hits = exact_rnums | self._dep_always_keep_rnums & (dialect_hits | self._to_rnums(self.maps['file_cov'].get(rel, []))) | non_rt_extra_c | self._tests_covering_lines(rel, unit.old_lines)
                    candidates.update(hits)
                    for rnum in hits:
                        if rnum in non_rt_extra_c:
                            for tag in has_sub.get(rnum, ()):
                                self._add_subtest_filter(subtest_filters, rnum, tag, None)
                        elif rnum in has_sub:
                            deferred_subtests.setdefault(rnum, []).append(spec_u)
                    return
                can_intersect = attr != '*' and attr not in self.dialect_meta_attrs and bool(readers) and (not has_non_rt_reader) and (not has_scoped_cb_ref)
                if can_intersect:
                    reader_hits: set[int] = set()
                    for rfile, rline in readers:
                        reader_hits.update(self._tests_covering_lines(rfile, {rline}))
                    inter = dialect_hits & reader_hits
                    if inter:
                        g_exprs = self._guarded_isinstance_exprs(attr, readers)
                        use_g_exprs = False
                        if g_exprs:
                            g_hits: set[int] = set()
                            for ec in g_exprs:
                                g_hits.update(self._to_rnums(self.maps['expr_to_tests'].get(ec, [])))
                            if inter & g_hits:
                                inter = inter & g_hits
                                use_g_exprs = True
                        hits = inter | self._tests_covering_lines(rel, unit.old_lines)
                        candidates.update(hits)
                        spec = ('dialect_readers_exprs', (subs, base_cls, readers, g_exprs)) if use_g_exprs else ('dialect_readers', (subs, base_cls, readers))
                        for rnum in hits:
                            if rnum in has_sub:
                                deferred_subtests.setdefault(rnum, []).append(spec)
                        return
                all_hits = dialect_hits | non_rt_extra_c | self._tests_covering_lines(rel, unit.old_lines)
                candidates.update(all_hits)
                spec = ('dialect', (subs, base_cls))
                for rnum in all_hits:
                    if rnum in non_rt_extra_c:
                        for tag in has_sub.get(rnum, ()):
                            self._add_subtest_filter(subtest_filters, rnum, tag, None)
                    elif rnum in has_sub:
                        deferred_subtests.setdefault(rnum, []).append(spec)
                return
        sym = unit.symbol_name or '*'
        hits = set()
        has_non_rt_fb = False
        if sym != '*' and sym in self.maps['name_readers']:
            for rfile, rline in self.maps['name_readers'][sym]:
                hits.update(self._tests_covering_lines(rfile, {rline}))
                if rline in self._get_non_runtime_info(rfile)[0]:
                    has_non_rt_fb = True
                    hits.update(self._tests_for_broken_import(rfile))
        if sym != '*' and sym in self.maps['attr_readers']:
            for rfile, rline in self.maps['attr_readers'][sym]:
                hits.update(self._tests_covering_lines(rfile, {rline}))
                if rline in self._get_non_runtime_info(rfile)[0]:
                    has_non_rt_fb = True
                    hits.update(self._tests_for_broken_import(rfile))
        if unit.class_name:
            expanded = self._expand_class_and_copiers(unit.class_name, sym)
            hits.update(self._dialect_hits_for_owners(expanded))
        if unit.kind == 'module_other' or sym == '*' or (not sym[0].isupper()) or (not hits) or has_non_rt_fb:
            hits.update(self._to_rnums(self.maps['file_cov'].get(rel, [])))
            hits.update(self._tests_for_broken_import(rel))
        else:
            hits.update(self._tests_covering_lines(rel, unit.old_lines))
        candidates.update(hits)
        for rnum in hits:
            for tag in has_sub.get(rnum, ()):
                self._add_subtest_filter(subtest_filters, rnum, tag, None)

    def plan_patch(self, commit_id: str, patch_text: str) -> PatchPlan:
        fps = parse_unified_diff(patch_text)
        patched_files: dict[str, str] = {}
        deleted_files: set[str] = set()
        py_analyses: dict[str, PyFileAnalysis] = {}
        candidates: set[int] = set()
        force_report: set[int] = set()
        dict_factory_files: set[str] = set()
        subtest_filters: dict[int, dict[str, set[Any] | None]] = {}
        deferred_subtests: dict[int, list[tuple[str, Any]]] = {}
        has_non_noop = False
        has_syntax_error = False
        syntax_error_files: list[str] = []
        only_fixtures = True
        all_hot_func = True
        exec_engine_touched = False
        tpcds_fixture_touched = False
        tpch_fixture_touched = False
        has_max_nodes_edit = False
        for fp in fps:
            rel = fp.path
            if not rel:
                continue
            if fp.is_rename and fp.old_path and fp.new_path:
                old_p = fp.old_path
                new_p = fp.new_path
                has_non_noop = True
                only_fixtures = False
                all_hot_func = False
                deleted_files.add(old_p)
                if 'tpc-ds/' in old_p or 'tpc-ds/' in new_p:
                    tpcds_fixture_touched = True
                    exec_engine_touched = True
                if 'tpc-h/' in old_p or 'tpc-h/' in new_p:
                    tpch_fixture_touched = True
                    exec_engine_touched = True
                old_affected: set[int] = set()
                if old_p.endswith('.py'):
                    old_affected.update(self._tests_for_broken_import(old_p))
                else:
                    old_affected.update(self._to_rnums(self.maps['fixture_to_tests'].get(old_p, [])))
                if new_p.endswith('.py'):
                    old_affected.update(self._tests_for_broken_import(new_p))
                else:
                    old_affected.update(self._to_rnums(self.maps['fixture_to_tests'].get(new_p, [])))
                candidates.update(old_affected)
                for rnum in old_affected:
                    for tag in self._subtest_tags_by_num.get(rnum, ()):
                        self._add_subtest_filter(subtest_filters, rnum, tag, None)
                if fp.is_binary:
                    force_report.update(old_affected)
                else:
                    base_src = self._read_base_file(old_p)
                    new_src = apply_file_patch(base_src, fp)
                    patched_files[new_p] = new_src
                    if new_p.endswith('.py'):
                        try:
                            ast.parse(new_src.encode('utf-8'), filename=new_p)
                        except SyntaxError:
                            has_syntax_error = True
                            syntax_error_files.append(new_p)
                continue
            if fp.is_binary:
                has_non_noop = True
                only_fixtures = False
                all_hot_func = False
                bin_hits = set(self._to_rnums(self.maps['fixture_to_tests'].get(rel, [])))
                if 'tpc-ds/' in rel:
                    tpcds_fixture_touched = True
                    exec_engine_touched = True
                    t_ds = self.id_to_num.get('tests/test_executor.py::TestExecutor::test_execute_tpcds')
                    if t_ds is not None:
                        bin_hits.add(t_ds)
                if 'tpc-h/' in rel:
                    tpch_fixture_touched = True
                    exec_engine_touched = True
                    for tid_k in ('tests/test_executor.py::TestExecutor::test_execute_tpch', 'tests/test_executor.py::TestExecutor::test_optimized_tpch'):
                        t_h = self.id_to_num.get(tid_k)
                        if t_h is not None:
                            bin_hits.add(t_h)
                candidates.update(bin_hits)
                force_report.update(bin_hits)
                continue
            old_text = self._read_base_file(rel)
            new_text = apply_file_patch(old_text, fp)
            if rel.endswith('.py'):
                cmp_ag = self._get_cmp_agree() if _diff_has_identity_or_eq_change(fp) else None
                ana = analyze_python_change(rel, old_text, new_text, fp, old_ast_cache=self._old_ast_cache, pure_enums=self.pure_enums, strict_enum_attrs=self.strict_enum_attrs, ignore_docstrings=self.ignore_docstrings, cmp_agree=cmp_ag, base_ast_index=self._base_ast_index)
                if ana.is_noop:
                    continue
                has_non_noop = True
                only_fixtures = False
                if fp.is_deleted:
                    deleted_files.add(rel)
                    all_hot_func = False
                else:
                    patched_files[rel] = new_text
                py_analyses[rel] = ana
                if rel.startswith(('sqlglot/executor/', 'sqlglot/planner.py', 'sqlglot/optimizer/annotate_types.py', 'sqlglot/typing/', 'sqlglot/generators/python.py', 'sqlglot/generator.py', 'sqlglot/optimizer/scope.py', 'sqlglot/optimizer/eliminate_joins.py', 'tests/test_executor.py', 'tests/helpers.py')) or (rel == 'sqlglot/dialects/dialect.py' and any((u.symbol_name in ('*', 'EXPRESSION_METADATA', 'COERCES_TO', 'DEFAULT_NULL_TYPE', 'PRIORITIZE_NON_LITERAL_TYPES', 'SUPPORTS_NULL_TYPE') for u in ana.units))):
                    exec_engine_touched = True
                _tokens_needs_max_nodes = False
                if rel == 'sqlglot/tokens.py':
                    if ana.syntax_error or fp.is_deleted or (not ana.units):
                        _tokens_needs_max_nodes = True
                    else:
                        _MAX_NODES_TOK_KEYS = frozenset({'SELECT', 'FROM', 'X', 'T', '=', ','})
                        for _u in ana.units:
                            if not (_u.kind in ('dict_key', 'dict_lambda_body') and _u.class_name == 'Tokenizer' and (_u.symbol_name == 'KEYWORDS') and (_u.dict_keys is not None)):
                                _tokens_needs_max_nodes = True
                                break
                            for _kr in _u.dict_keys:
                                _s = _kr.strip()
                                if len(_s) < 2 or _s[0] != _s[-1] or _s[0] not in ("'", '"'):
                                    _tokens_needs_max_nodes = True
                                    break
                                _ku = _s[1:-1].upper()
                                if _ku in _MAX_NODES_TOK_KEYS or _ku.startswith('COL') or (not _ku.isidentifier()):
                                    _tokens_needs_max_nodes = True
                                    break
                            if _tokens_needs_max_nodes:
                                break
                if rel == 'tests/test_parser.py' and any(('test_max_nodes' in fn for fn in ana.changed_funcs)) or rel == 'sqlglot/tokenizer_core.py' or _tokens_needs_max_nodes or (rel == 'sqlglot/parser.py' and (any((fn in ('Parser.expression', 'Parser.__init__', 'Parser._reset', 'Parser.parse') for fn in ana.changed_funcs)) or any((u.kind == 'module_other' for u in ana.units)))) or any((hl.startswith(('+', '-')) and (not hl[1:].lstrip().startswith('#')) and ('max_nodes' in hl.lower() or '_node_count' in hl or '1_000_000' in hl or ('1000000' in hl)) for h in fp.hunks for hl in h.lines)):
                    has_max_nodes_edit = True
                if ana.syntax_error:
                    has_syntax_error = True
                    syntax_error_files.append(rel)
                    all_hot_func = False
                if rel.startswith(('tests/', 'sqlglotc/')):
                    all_hot_func = False
                file_has_non_hot = False
                for unit in ana.units:
                    meth_name = (unit.symbol_name or '').rsplit('.', 1)[-1]
                    if rel in _COMPILED_C_FILES or unit.kind != 'func_body' or unit.sig_changed or (meth_name in ('__new__', '__init_subclass__', '__set_name__', '__class_getitem__', '_generate_next_value_', '_build_dispatch', 'sql_names')) or any(self._is_func_called_at_import(rel, meth_name)[:2]):
                        all_hot_func = False
                        file_has_non_hot = True
                    self._handle_change_unit(unit, candidates, subtest_filters, deferred_subtests, dict_factory_files)
                if self.lazy_load_test_num is not None and self.lazy_load_test_num in self._to_rnums(self.maps['dyn_import_to_tests'].get(rel, [])) and (ana.syntax_error or fp.is_deleted or fp.is_new or any((u.kind != 'func_body' for u in ana.units)) or any((self._is_func_called_at_import(rel, (u.symbol_name or '').rsplit('.', 1)[-1])[0] for u in ana.units if u.kind == 'func_body')) or any((hl.startswith(('+', '-')) and any((tok in hl for tok in ('import ', 'from ', 'Dialect', '_classes', '_try_load', 'importlib', '__import__'))) for h in fp.hunks for hl in h.lines))):
                    candidates.add(self.lazy_load_test_num)
                    all_hot_func = False
            else:
                f_hits = self._to_rnums(self.maps['fixture_to_tests'].get(rel, []))
                if not f_hits:
                    continue
                if rel.endswith('.sql'):
                    if 'qualify_columns__invalid' in rel or rel.endswith(('tests/fixtures/identity.sql', 'tests/fixtures/partial.sql')):
                        err_or_shift, changed_idxs = diff_sql_fixture_lines(old_text, new_text)
                    else:
                        err_or_shift, changed_idxs = diff_sql_fixture_pairs(old_text, new_text)
                    if not err_or_shift and (not changed_idxs):
                        continue
                    has_non_noop = True
                    if fp.is_deleted:
                        deleted_files.add(rel)
                        all_hot_func = False
                    else:
                        patched_files[rel] = new_text
                    if 'tpc-ds' in rel:
                        tpcds_fixture_touched = True
                    if 'tpc-h' in rel:
                        tpch_fixture_touched = True
                    if not err_or_shift and (not fp.is_deleted):
                        if rel == 'tests/fixtures/optimizer/tpc-h/tpc-h.sql':
                            f_hits = {r for r in f_hits if self.num_to_test[r]['file'] != 'tests/test_executor.py' or self.num_to_test[r]['id'].endswith(('::test_execute_tpch', '::test_optimized_tpch'))}
                        elif rel == 'tests/fixtures/optimizer/tpc-ds/tpc-ds.sql':
                            f_hits = {r for r in f_hits if self.num_to_test[r]['file'] != 'tests/test_executor.py' or self.num_to_test[r]['id'].endswith('::test_execute_tpcds')}
                    candidates.update(f_hits)
                    for rnum in f_hits:
                        tags = self._subtest_tags_by_num.get(rnum, [])
                        matched_tags = [tag for tag in tags if tag == rel or ('tpc-ds.sql' in rel and tag == 'tpc-ds') or ('tpc-h.sql' in rel and tag in ('tpc-h', 'opt-tpch'))]
                        if matched_tags:
                            for tag in tags:
                                if tag in matched_tags:
                                    self._add_subtest_filter(subtest_filters, rnum, tag, None if err_or_shift else set(changed_idxs))
                                else:
                                    self._add_subtest_filter(subtest_filters, rnum, tag, set())
                        else:
                            for tag in tags:
                                self._add_subtest_filter(subtest_filters, rnum, tag, None)
                else:
                    if old_text == new_text:
                        continue
                    has_non_noop = True
                    all_hot_func = False
                    if fp.is_deleted:
                        deleted_files.add(rel)
                    else:
                        patched_files[rel] = new_text
                    if 'tpc-ds/' in rel:
                        tpcds_fixture_touched = True
                        exec_engine_touched = True
                    if 'tpc-h/' in rel:
                        tpch_fixture_touched = True
                        exec_engine_touched = True
                    candidates.update(f_hits)
                    for rnum in f_hits:
                        for tag in self._subtest_tags_by_num.get(rnum, []):
                            self._add_subtest_filter(subtest_filters, rnum, tag, None)
        if not has_non_noop or not candidates:
            return PatchPlan(commit=commit_id, is_noop=True, group='noop')
        if self.lazy_load_test_num in candidates:
            all_hot_func = False
        if only_fixtures:
            group = 'fixture'
        elif all_hot_func:
            group = 'hot_func'
        else:
            group = 'full_fork'
        return PatchPlan(commit=commit_id, is_noop=False, has_syntax_error=has_syntax_error, syntax_error_files=syntax_error_files, patched_files=patched_files, deleted_files=deleted_files, py_analyses=py_analyses, candidates=candidates, subtest_filters=subtest_filters, deferred_subtests=deferred_subtests, group=group, exec_engine_touched=exec_engine_touched, tpcds_fixture_touched=tpcds_fixture_touched, tpch_fixture_touched=tpch_fixture_touched, has_max_nodes_edit=has_max_nodes_edit, force_report=force_report, dict_factory_files=dict_factory_files)
