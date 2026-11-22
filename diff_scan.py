"""Patch reading: parse each unified diff, apply it in memory and classify what changed (AST, fixtures, comments)."""
from __future__ import annotations
import ast
import bisect
import marshal
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
try:
    from _blake2 import blake2b as _blake2b
except ImportError:
    from hashlib import blake2b as _blake2b
BASE_AST_INDEX_DIR = Path('/app/base_ast_index')
_EMPTY_SET: set[Any] = set()
HUNK_RE = re.compile('^@@ -(\\d+)(?:,(\\d+))? \\+(\\d+)(?:,(\\d+))? @@')
CODING_RE = re.compile('coding[:=]\\s*([-\\w.]+)')

@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]

@dataclass
class FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ''

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None

    @property
    def is_rename(self) -> bool:
        return self.old_path is not None and self.new_path is not None and (self.old_path != self.new_path)

def _strip_prefix(raw: str) -> str | None:
    raw = raw.strip()
    if raw == '/dev/null':
        return None
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if raw.startswith('a/') or raw.startswith('b/'):
        return raw[2:]
    return raw

def parse_unified_diff(patch_text: str) -> list[FilePatch]:
    lines = patch_text.splitlines(keepends=True)
    patches: list[FilePatch] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith('diff --git '):
            header_old = None
            header_new = None
            parts = line.strip().split(' ')
            if len(parts) >= 4:
                header_old = _strip_prefix(parts[2])
                header_new = _strip_prefix(parts[3])
            i += 1
            old_path = header_old
            new_path = header_new
            is_binary = False
            hunks: list[Hunk] = []
            while i < n and (not lines[i].startswith('diff --git ')):
                cur = lines[i]
                if cur.startswith('--- '):
                    old_path = _strip_prefix(cur[4:])
                    i += 1
                elif cur.startswith('+++ '):
                    new_path = _strip_prefix(cur[4:])
                    i += 1
                elif cur.startswith('new file mode '):
                    old_path = None
                    i += 1
                elif cur.startswith('deleted file mode '):
                    new_path = None
                    i += 1
                elif cur.startswith('rename from '):
                    old_path = _strip_prefix(cur[12:])
                    i += 1
                elif cur.startswith('rename to '):
                    new_path = _strip_prefix(cur[10:])
                    i += 1
                elif cur.startswith('Binary files ') or cur.startswith('GIT binary patch'):
                    is_binary = True
                    i += 1
                elif cur.startswith('@@ '):
                    m = HUNK_RE.match(cur)
                    if not m:
                        i += 1
                        continue
                    old_start = int(m.group(1))
                    old_count = int(m.group(2)) if m.group(2) is not None else 1
                    new_start = int(m.group(3))
                    new_count = int(m.group(4)) if m.group(4) is not None else 1
                    i += 1
                    hunk_lines: list[str] = []
                    while i < n and (not lines[i].startswith(('@@ ', 'diff --git '))):
                        hl = lines[i]
                        if hl.startswith((' ', '+', '-', '\\')):
                            hunk_lines.append(hl)
                            i += 1
                        else:
                            break
                    hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
                else:
                    i += 1
            patches.append(FilePatch(old_path, new_path, hunks, is_binary))
        elif line.startswith('--- '):
            old_path = _strip_prefix(line[4:])
            i += 1
            new_path = None
            if i < n and lines[i].startswith('+++ '):
                new_path = _strip_prefix(lines[i][4:])
                i += 1
            hunks = []
            while i < n and lines[i].startswith('@@ '):
                m = HUNK_RE.match(lines[i])
                if not m:
                    break
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                i += 1
                hunk_lines = []
                while i < n and (not lines[i].startswith(('@@ ', 'diff --git ', '--- '))):
                    hl = lines[i]
                    if hl.startswith((' ', '+', '-', '\\')):
                        hunk_lines.append(hl)
                        i += 1
                    else:
                        break
                hunks.append(Hunk(old_start, old_count, new_start, new_count, hunk_lines))
            patches.append(FilePatch(old_path, new_path, hunks, False))
        else:
            i += 1
    return patches

def apply_file_patch(old_text: str, fp: FilePatch) -> str:
    if fp.is_deleted:
        return ''
    if not fp.hunks:
        return old_text
    old_lines = old_text.splitlines(keepends=True) if old_text else []
    out: list[str] = []
    cursor = 0
    for hunk in fp.hunks:
        if hunk.old_count == 0:
            target = max(0, hunk.old_start)
        else:
            target = max(0, hunk.old_start - 1) if hunk.old_start > 0 else 0
        if target > cursor:
            out.extend(old_lines[cursor:target])
            cursor = target
        for idx, hl in enumerate(hunk.lines):
            if hl.startswith('\\'):
                if idx > 0 and hunk.lines[idx - 1].startswith('+') and out:
                    out[-1] = out[-1].rstrip('\r\n')
                continue
            tag = hl[0]
            body = hl[1:]
            if tag == ' ':
                if cursor < len(old_lines):
                    out.append(old_lines[cursor])
                    cursor += 1
                else:
                    out.append(body)
            elif tag == '-':
                if cursor < len(old_lines):
                    cursor += 1
            elif tag == '+':
                out.append(body)
    if cursor < len(old_lines):
        out.extend(old_lines[cursor:])
    return ''.join(out)
_CF_KEYWORD_RE = re.compile('\\b(?:return|break|continue|raise|yield|if|elif|else|for|while|try|except|finally|with|match|case|def|class|async)\\b')
_CF_BRANCH_KWS = frozenset({'elif', 'else', 'except', 'finally', 'return', 'break', 'continue', 'raise'})

def _leading_indent(s: str) -> str:
    return s[:len(s) - len(s.lstrip(' \t'))]

def _extract_hunk_lines(fp: FilePatch) -> tuple[set[int], set[int], set[int]]:
    changed: set[int] = set()
    anchors: set[int] = set()
    cf_lines: set[int] = set()
    for hunk in fp.hunks:
        old_ln = hunk.old_start
        i = 0
        n = len(hunk.lines)
        while i < n:
            hl = hunk.lines[i]
            if hl.startswith('\\'):
                i += 1
                continue
            tag = hl[0]
            if tag == ' ':
                old_ln += 1
                i += 1
                continue
            start_old_ln = old_ln
            del_bodies: list[str] = []
            add_bodies: list[str] = []
            del_lns: list[int] = []
            while i < n and (not hunk.lines[i].startswith(' ')):
                cur = hunk.lines[i]
                if cur.startswith('\\'):
                    i += 1
                    continue
                if cur[0] == '-':
                    del_bodies.append(cur[1:])
                    del_lns.append(old_ln)
                    changed.add(old_ln)
                    old_ln += 1
                elif cur[0] == '+':
                    add_bodies.append(cur[1:])
                i += 1
            is_strict_inplace = len(del_bodies) > 0 and len(del_bodies) == len(add_bodies) and all((_leading_indent(d) == _leading_indent(a) and (d.strip() == '') == (a.strip() == '') and (_CF_KEYWORD_RE.findall(d) == _CF_KEYWORD_RE.findall(a)) for d, a in zip(del_bodies, add_bodies)))
            if not is_strict_inplace:
                if start_old_ln > 1:
                    anchors.add(start_old_ln - 1)
                if start_old_ln > 0:
                    anchors.add(start_old_ln)
                if old_ln > 1:
                    anchors.add(old_ln - 1)
                if old_ln > 0:
                    anchors.add(old_ln)
                cf_lines.update(del_lns)
                if start_old_ln > 1:
                    cf_lines.add(start_old_ln - 1)
                if start_old_ln > 0:
                    cf_lines.add(start_old_ln)
                if old_ln > 0:
                    cf_lines.add(old_ln)
            else:
                for d, ln in zip(del_bodies, del_lns):
                    if any((kw in _CF_BRANCH_KWS for kw in _CF_KEYWORD_RE.findall(d))):
                        cf_lines.add(ln)
    return (changed, anchors, cf_lines)

def extract_changed_old_lines(fp: FilePatch) -> tuple[set[int], set[int]]:
    changed, anchors, _ = _extract_hunk_lines(fp)
    return (changed, anchors)

@dataclass
class ChangeUnit:
    kind: str
    file: str
    class_name: str | None = None
    symbol_name: str | None = None
    dict_keys: set[str] | None = None
    dict_added_keys: set[str] = field(default_factory=set)
    dict_deleted_keys: set[str] = field(default_factory=set)
    old_lines: set[int] = field(default_factory=set)
    enclosing_lines: set[int] = field(default_factory=set)
    has_nested_func: bool = False
    edit_outside_nested_func: bool = False
    is_cached_func: bool = False
    sig_changed: bool = False

@dataclass
class PyFileAnalysis:
    file: str
    is_noop: bool
    syntax_error: bool = False
    units: list[ChangeUnit] = field(default_factory=list)
    all_changed_old_lines: set[int] = field(default_factory=set)
    changed_funcs: list[str] = field(default_factory=list)

def _strip_docstrings_and_type_checking(tree: ast.AST) -> str:
    return ast.dump(tree, include_attributes=False)

def _strip_leading_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return body[1:]
    return body

def _is_sound_cmp_pair(left: ast.AST, right: ast.AST, allow_none: bool, pure_enums: set[str], strict_enum_attrs: set[tuple[str, str]]) -> bool:
    if allow_none and (isinstance(right, ast.Constant) and right.value is None or (isinstance(left, ast.Constant) and left.value is None)):
        return True
    for lhs, rhs in ((left, right), (right, left)):
        if isinstance(rhs, ast.Attribute) and isinstance(rhs.value, ast.Name):
            enum_name = rhs.value.id
            if enum_name in pure_enums:
                return True
            if isinstance(lhs, ast.Attribute) and (lhs.attr, enum_name) in strict_enum_attrs:
                return True
    return False

def _ast_nodes_equiv(a: Any, b: Any, ignore_docstrings: bool=False, ignore_type_checking: bool=False, allow_sound_cmp: bool=False, allow_none: bool=False, pure_enums: set[str] | None=None, strict_enum_attrs: set[tuple[str, str]] | None=None, cmp_site_ok: Any=None) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all((_ast_nodes_equiv(x, y, ignore_docstrings, ignore_type_checking, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok) for x, y in zip(a, b)))
    if (allow_sound_cmp or cmp_site_ok is not None) and isinstance(a, ast.Compare):
        if len(a.ops) == 1 and len(b.ops) == 1 and (len(a.comparators) == 1) and (len(b.comparators) == 1):
            op_a, op_b = (a.ops[0], b.ops[0])
            if type(op_a) is not type(op_b):
                eq_is_pair = isinstance(op_a, ast.Is) and isinstance(op_b, ast.Eq) or (isinstance(op_a, ast.Eq) and isinstance(op_b, ast.Is)) or (isinstance(op_a, ast.IsNot) and isinstance(op_b, ast.NotEq)) or (isinstance(op_a, ast.NotEq) and isinstance(op_b, ast.IsNot))
                site_safe = cmp_site_ok is not None and cmp_site_ok(a.lineno, a.col_offset) or (allow_sound_cmp and _is_sound_cmp_pair(a.left, a.comparators[0], allow_none, pure_enums or _EMPTY_SET, strict_enum_attrs or _EMPTY_SET))
                if not (eq_is_pair and site_safe and _ast_nodes_equiv(a.left, b.left, ignore_docstrings, ignore_type_checking, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok) and _ast_nodes_equiv(a.comparators[0], b.comparators[0], ignore_docstrings, ignore_type_checking, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok)):
                    return False
                return True
    if isinstance(a, ast.AST):
        is_doc_owner = ignore_docstrings and isinstance(a, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        is_tc_owner = ignore_type_checking and isinstance(a, (ast.Module, ast.ClassDef))
        for field_name in a._fields:
            va = getattr(a, field_name)
            vb = getattr(b, field_name)
            if field_name == 'body':
                if is_doc_owner:
                    va = _strip_leading_docstring(va)
                    vb = _strip_leading_docstring(vb)
                if is_tc_owner:
                    va = [s for s in va if not _is_type_checking_if(s)]
                    vb = [s for s in vb if not _is_type_checking_if(s)]
            if not _ast_nodes_equiv(va, vb, ignore_docstrings, ignore_type_checking, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok):
                return False
        return True
    return a == b

def _stmt_start_line(node: ast.stmt) -> int:
    start = node.lineno
    for d in getattr(node, 'decorator_list', None) or ():
        if d.lineno < start:
            start = d.lineno
    return start

def _same_stmt_text(x: ast.stmt, y: ast.stmt, old_lines: list[str], new_lines: list[str]) -> bool:
    if type(x) is not type(y) or x.col_offset != y.col_offset or x.end_col_offset != y.end_col_offset:
        return False
    xs = _stmt_start_line(x)
    ys = _stmt_start_line(y)
    xe = x.end_lineno
    ye = y.end_lineno
    if xe is None or ye is None or xe - xs != ye - ys:
        return False
    return old_lines[xs - 1:xe] == new_lines[ys - 1:ye]

def _fast_tree_equiv(a: Any, b: Any, old_lines: list[str] | None, new_lines: list[str] | None, ignore_docstrings: bool=False, ignore_type_checking: bool=False, allow_sound_cmp: bool=False, allow_none: bool=False, pure_enums: set[str] | None=None, strict_enum_attrs: set[tuple[str, str]] | None=None, cmp_site_ok: Any=None) -> bool:
    rest = (ignore_docstrings, ignore_type_checking, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok)
    if old_lines is None or new_lines is None or (not isinstance(a, (ast.Module, ast.ClassDef))):
        return _ast_nodes_equiv(a, b, *rest)
    if type(a) is not type(b):
        return False
    for field_name in a._fields:
        if field_name != 'body' and (not _ast_nodes_equiv(getattr(a, field_name), getattr(b, field_name), *rest)):
            return False
    va = a.body
    vb = b.body
    if ignore_docstrings:
        va = _strip_leading_docstring(va)
        vb = _strip_leading_docstring(vb)
    if ignore_type_checking:
        va = [st for st in va if not _is_type_checking_if(st)]
        vb = [st for st in vb if not _is_type_checking_if(st)]
    if len(va) != len(vb):
        return False
    for x, y in zip(va, vb):
        if _same_stmt_text(x, y, old_lines, new_lines):
            continue
        if isinstance(x, ast.ClassDef) and isinstance(y, ast.ClassDef):
            if not _fast_tree_equiv(x, y, old_lines, new_lines, *rest):
                return False
        elif not _ast_nodes_equiv(x, y, *rest):
            return False
    return True

def _source_lines(src: str) -> list[str] | None:
    if '\r' in src:
        return None
    return src.split('\n')

def _diff_has_identity_or_eq_change(fp: FilePatch) -> bool:
    for h in fp.hunks:
        for line in h.lines:
            if line.startswith(('+', '-')) and (' is ' in line or '==' in line or '!=' in line or ('None' in line)):
                return True
    return False
_UNSAFE_NONE_EQ_FILES = frozenset({'sqlglot/executor/env.py', 'sqlglot/executor/table.py', 'sqlglot/expressions/builders.py'})

def _is_sound_cmp_equiv(file: str, old_ast: ast.AST, new_ast: ast.AST, fp: FilePatch, pure_enums: set[str], strict_enum_attrs: set[tuple[str, str]], ignore_docstrings: bool=False, old_lines: list[str] | None=None, new_lines: list[str] | None=None) -> bool:
    if not file.startswith('sqlglot/') or not _diff_has_identity_or_eq_change(fp):
        return False
    allow_none = not file.startswith('sqlglot/executor/') and file != 'sqlglot/expressions/builders.py'
    return _fast_tree_equiv(old_ast, new_ast, old_lines, new_lines, ignore_docstrings=ignore_docstrings, ignore_type_checking=False, allow_sound_cmp=True, allow_none=allow_none, pure_enums=pure_enums, strict_enum_attrs=strict_enum_attrs)

def _coding_cookie(src: str) -> str | None:
    for line in src.splitlines()[:2]:
        m = CODING_RE.search(line)
        if m:
            return m.group(1).lower()
    return None

def _const_key_repr(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool)) or node.value is None:
            return repr(node.value)
        return None
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            parts.reverse()
            return '.'.join(parts)
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Tuple):
        elts = [_const_key_repr(e) for e in node.elts]
        if any((e is None for e in elts)):
            return None
        return '(' + ', '.join(elts) + ')'
    return None

def _diff_dict_nodes_detailed(old_dict: ast.Dict, new_dict: ast.Dict) -> tuple[set[str] | None, set[str], set[str], set[int] | None]:
    old_unpacks: list[str] = []
    old_map: dict[str, list[tuple[int, str, ast.AST]]] = {}
    seg = 0
    for k, v in zip(old_dict.keys, old_dict.values):
        if k is None:
            old_unpacks.append(ast.dump(v, include_attributes=False))
            seg += 1
        else:
            kr = _const_key_repr(k)
            if kr is None:
                return (None, set(), set(), None)
            old_map.setdefault(kr, []).append((seg, ast.dump(v, include_attributes=False), v))
    new_unpacks: list[str] = []
    new_map: dict[str, list[tuple[int, str, ast.AST]]] = {}
    seg = 0
    for k, v in zip(new_dict.keys, new_dict.values):
        if k is None:
            new_unpacks.append(ast.dump(v, include_attributes=False))
            seg += 1
        else:
            kr = _const_key_repr(k)
            if kr is None:
                return (None, set(), set(), None)
            new_map.setdefault(kr, []).append((seg, ast.dump(v, include_attributes=False), v))
    if old_unpacks != new_unpacks:
        return (None, set(), set(), None)
    old_keys = set(old_map)
    new_keys = set(new_map)
    added_keys = new_keys - old_keys
    deleted_keys = old_keys - new_keys
    changed_keys = {k for k in old_keys | new_keys if [(s, d) for s, d, _ in old_map.get(k, ())] != [(s, d) for s, d, _ in new_map.get(k, ())]}
    if not changed_keys:
        return (None, set(), set(), None)
    lambda_only_lines: set[int] | None = None
    if not added_keys and (not deleted_keys):
        lam_lines: set[int] = set()
        all_lam = True
        for k in changed_keys:
            ol = old_map[k]
            nl = new_map[k]
            if len(ol) == 1 and len(nl) == 1 and (ol[0][0] == nl[0][0]) and isinstance(ol[0][2], ast.Lambda) and isinstance(nl[0][2], ast.Lambda) and (ast.dump(ol[0][2].args, include_attributes=False) == ast.dump(nl[0][2].args, include_attributes=False)):
                lam_lines |= _stmt_line_span(ol[0][2].body)
            else:
                all_lam = False
                break
        if all_lam and lam_lines:
            lambda_only_lines = lam_lines
    return (changed_keys, added_keys, deleted_keys, lambda_only_lines)

def _stmt_line_span(node: ast.AST) -> set[int]:
    start = getattr(node, 'lineno', None)
    end = getattr(node, 'end_lineno', start)
    if start is None:
        return set()
    return set(range(start, (end or start) + 1))
_TRY_TYPES: tuple[type[ast.stmt], ...] = (ast.Try, ast.TryStar) if hasattr(ast, 'TryStar') else (ast.Try,)
_MATCH_TYPES: tuple[type[ast.stmt], ...] = (ast.Match,) if hasattr(ast, 'Match') else ()
_COMPOUND_TYPES: tuple[type[ast.stmt], ...] = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, *_TRY_TYPES, *_MATCH_TYPES)

def _stmt_entry_lines(stmt: ast.stmt) -> set[int]:
    if isinstance(stmt, ast.If):
        return _stmt_line_span(stmt.test) | {stmt.lineno}
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return _stmt_line_span(stmt.target) | _stmt_line_span(stmt.iter) | {stmt.lineno}
    if isinstance(stmt, ast.While):
        return _stmt_line_span(stmt.test) | {stmt.lineno}
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        hdr = {stmt.lineno}
        for item in stmt.items:
            hdr |= _stmt_line_span(item)
        return hdr
    if isinstance(stmt, _TRY_TYPES):
        hdr = {stmt.lineno}
        if stmt.body:
            hdr |= _stmt_entry_lines(stmt.body[0])
        return hdr
    if _MATCH_TYPES and isinstance(stmt, _MATCH_TYPES):
        return _stmt_line_span(stmt.subject) | {stmt.lineno}
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        first_body_ln = getattr(stmt.body[0], 'lineno', stmt.lineno + 1) if stmt.body else stmt.lineno + 1
        dec_min = min((getattr(d, 'lineno', stmt.lineno) for d in stmt.decorator_list), default=stmt.lineno)
        return set(range(dec_min, max(stmt.lineno + 1, first_body_ln)))
    return _stmt_line_span(stmt)

def _collect_struct_enclosing_headers(old_body: list[ast.stmt], new_body: list[ast.stmt], stmt_lines: set[int], enclosing_hdr: set[int], ignore_docstrings: bool=False) -> None:
    if ignore_docstrings:
        old_body = _strip_leading_docstring(old_body)
        new_body = _strip_leading_docstring(new_body)
    if len(old_body) != len(new_body):
        stmt_lines |= enclosing_hdr
    for o_s, n_s in zip(old_body, new_body):
        if type(o_s) is not type(n_s):
            stmt_lines |= enclosing_hdr | _stmt_entry_lines(o_s)
            continue
        if isinstance(o_s, _COMPOUND_TYPES):
            sub_hdr = _stmt_entry_lines(o_s)
            if hasattr(o_s, 'body') and hasattr(n_s, 'body'):
                _collect_struct_enclosing_headers(o_s.body, n_s.body, stmt_lines, sub_hdr, False)
            if hasattr(o_s, 'orelse') and hasattr(n_s, 'orelse'):
                if bool(o_s.orelse) != bool(n_s.orelse):
                    stmt_lines |= sub_hdr
                elif o_s.orelse:
                    _collect_struct_enclosing_headers(o_s.orelse, n_s.orelse, stmt_lines, sub_hdr, False)
            if hasattr(o_s, 'finalbody') and hasattr(n_s, 'finalbody'):
                if bool(o_s.finalbody) != bool(n_s.finalbody):
                    stmt_lines |= sub_hdr
                elif o_s.finalbody:
                    _collect_struct_enclosing_headers(o_s.finalbody, n_s.finalbody, stmt_lines, sub_hdr, False)
            if isinstance(o_s, _TRY_TYPES) and isinstance(n_s, _TRY_TYPES):
                if len(o_s.handlers) != len(n_s.handlers):
                    stmt_lines |= sub_hdr
                    for oh in o_s.handlers:
                        stmt_lines.add(oh.lineno)
                else:
                    for oh, nh in zip(o_s.handlers, n_s.handlers):
                        h_hdr = sub_hdr | {oh.lineno} | (_stmt_line_span(oh.type) if oh.type is not None else set())
                        _collect_struct_enclosing_headers(oh.body, nh.body, stmt_lines, h_hdr, False)
            if _MATCH_TYPES and isinstance(o_s, _MATCH_TYPES) and isinstance(n_s, _MATCH_TYPES):
                o_cases = o_s.cases
                n_cases = n_s.cases
                if len(o_cases) != len(n_cases):
                    stmt_lines |= sub_hdr
                else:
                    for oc, nc in zip(o_cases, n_cases):
                        _collect_struct_enclosing_headers(oc.body, nc.body, stmt_lines, sub_hdr, False)
        elif isinstance(o_s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sub_hdr = _stmt_entry_lines(o_s)
            _collect_struct_enclosing_headers(o_s.body, n_s.body, stmt_lines, sub_hdr, ignore_docstrings)

def _func_signature_dump(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    clone = type(fn)(name=fn.name, args=fn.args, body=[], decorator_list=fn.decorator_list, returns=fn.returns, type_params=getattr(fn, 'type_params', []))
    return ast.dump(clone, include_attributes=False)

def _is_pure_const_default(node: ast.AST | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return _is_pure_const_default(node.operand)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all((_is_pure_const_default(e) for e in node.elts))
    return False

def _is_safe_func_sig_change(os_node: ast.FunctionDef | ast.AsyncFunctionDef, ns_node: ast.FunctionDef | ast.AsyncFunctionDef, file: str, has_future_ann: bool) -> bool:
    if type(os_node) is not type(ns_node) or os_node.name != ns_node.name:
        return False
    if [ast.dump(d, include_attributes=False) for d in os_node.decorator_list] != [ast.dump(d, include_attributes=False) for d in ns_node.decorator_list]:
        return False
    if [ast.dump(tp, include_attributes=False) for tp in getattr(os_node, 'type_params', ())] != [ast.dump(tp, include_attributes=False) for tp in getattr(ns_node, 'type_params', ())]:
        return False
    oa, na = (os_node.args, ns_node.args)
    if [a.arg for a in oa.posonlyargs] != [a.arg for a in na.posonlyargs] or [a.arg for a in oa.args] != [a.arg for a in na.args] or [a.arg for a in oa.kwonlyargs] != [a.arg for a in na.kwonlyargs] or ((oa.vararg.arg if oa.vararg else None) != (na.vararg.arg if na.vararg else None)) or ((oa.kwarg.arg if oa.kwarg else None) != (na.kwarg.arg if na.kwarg else None)) or (len(oa.defaults) != len(na.defaults)) or (len(oa.kw_defaults) != len(na.kw_defaults)) or ([d is None for d in oa.kw_defaults] != [d is None for d in na.kw_defaults]):
        return False
    for od, nd in zip(oa.defaults, na.defaults):
        if ast.dump(od, include_attributes=False) != ast.dump(nd, include_attributes=False):
            if not (_is_pure_const_default(od) and _is_pure_const_default(nd)):
                return False
    for od, nd in zip(oa.kw_defaults, na.kw_defaults):
        if od is not None and nd is not None:
            if ast.dump(od, include_attributes=False) != ast.dump(nd, include_attributes=False):
                if not (_is_pure_const_default(od) and _is_pure_const_default(nd)):
                    return False
    ann_changed = (ast.dump(os_node.returns, include_attributes=False) if os_node.returns else None) != (ast.dump(ns_node.returns, include_attributes=False) if ns_node.returns else None)
    if not ann_changed:
        all_oa = [*oa.posonlyargs, *oa.args, *oa.kwonlyargs, *([oa.vararg] if oa.vararg else []), *([oa.kwarg] if oa.kwarg else [])]
        all_na = [*na.posonlyargs, *na.args, *na.kwonlyargs, *([na.vararg] if na.vararg else []), *([na.kwarg] if na.kwarg else [])]
        for x, y in zip(all_oa, all_na):
            if (ast.dump(x.annotation, include_attributes=False) if x.annotation else None) != (ast.dump(y.annotation, include_attributes=False) if y.annotation else None):
                ann_changed = True
                break
    if ann_changed and (not has_future_ann or file.startswith(('sqlglot/typing/', 'tests/', 'sqlglotc/'))):
        return False
    return True

def _has_future_annotations(tree: ast.Module) -> bool:
    for s in tree.body:
        if isinstance(s, ast.ImportFrom) and s.module == '__future__':
            if any((a.name == 'annotations' for a in s.names)):
                return True
        elif isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            continue
        else:
            break
    return False
_CACHE_DECORATORS = frozenset({'cache', 'lru_cache', 'cached_property'})

def _is_cached_func(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id in _CACHE_DECORATORS:
            return True
        if isinstance(target, ast.Attribute) and target.attr in _CACHE_DECORATORS:
            return True
    return False

def _extract_assign_target_and_value(stmt: ast.stmt) -> tuple[str | None, ast.AST | None]:
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
        return (stmt.targets[0].id, stmt.value)
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return (stmt.target.id, stmt.value)
    return (None, None)

def _is_type_checking_if(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.If) or stmt.orelse:
        return False
    test = stmt.test
    if isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING':
        return True
    if isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING':
        return True
    return False

def _is_overload_fn(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for dec in stmt.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == 'overload':
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == 'overload':
            return True
    return False

def _stmt_src_equal(o_node: ast.AST, n_node: ast.AST, old_lines_arr: list[str], new_lines_arr: list[str]) -> bool:
    o1 = getattr(o_node, 'lineno', None)
    o2 = getattr(o_node, 'end_lineno', None)
    n1 = getattr(n_node, 'lineno', None)
    n2 = getattr(n_node, 'end_lineno', None)
    if o1 is None or o2 is None or n1 is None or (n2 is None):
        return False
    if o2 - o1 != n2 - n1:
        return False
    return old_lines_arr[o1 - 1:o2] == new_lines_arr[n1 - 1:n2]

def _emit_single_unit(file: str, kind_tag: str, name: str, class_qual: str | None, os_node: ast.stmt, ns_node: ast.stmt, changed_old_lines: set[int], anchor_old_lines: set[int], cf_old_lines: set[int], units: list[ChangeUnit], changed_funcs: list[str], ignore_docstrings: bool, has_future_ann: bool) -> None:
    if kind_tag == 'func':
        assert isinstance(os_node, (ast.FunctionDef, ast.AsyncFunctionDef))
        assert isinstance(ns_node, (ast.FunctionDef, ast.AsyncFunctionDef))
        qname = f'{class_qual}.{name}' if class_qual else name
        changed_funcs.append(qname)
        fn_span = _stmt_line_span(os_node)
        sig_changed = _func_signature_dump(os_node) != _func_signature_dump(ns_node)
        inner_defs = [n for n in ast.walk(os_node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not os_node]
        new_inner_defs = [n for n in ast.walk(ns_node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not ns_node]
        has_nested_func = bool(inner_defs or new_inner_defs or any((isinstance(n, ast.Lambda) for n in ast.walk(os_node))) or any((isinstance(n, ast.Lambda) for n in ast.walk(ns_node))))
        is_cached = _is_cached_func(os_node) or _is_cached_func(ns_node)
        hit_lines = fn_span & (changed_old_lines | anchor_old_lines)
        cf_hit = fn_span & cf_old_lines
        inner_body_lines: set[int] = set()
        for idn in inner_defs:
            for is_stmt in idn.body:
                inner_body_lines |= _stmt_line_span(is_stmt)
        edit_outside_nested = True if not inner_defs else bool((hit_lines | cf_hit) - inner_body_lines)
        stmt_lines: set[int] = set()
        _collect_struct_enclosing_headers(os_node.body, ns_node.body, stmt_lines, _stmt_entry_lines(os_node.body[0]) if os_node.body else {os_node.lineno}, ignore_docstrings)
        for sub in ast.walk(os_node):
            if isinstance(sub, ast.stmt) and sub is not os_node:
                sp = _stmt_line_span(sub)
                if sp & hit_lines:
                    if isinstance(sub, _COMPOUND_TYPES):
                        hdr = _stmt_entry_lines(sub)
                        if hdr & hit_lines or sp & cf_hit:
                            stmt_lines |= hdr
                        if isinstance(sub, _TRY_TYPES):
                            for h in sub.handlers:
                                h_hdr = {h.lineno} | (_stmt_line_span(h.type) if h.type is not None else set())
                                if h_hdr & hit_lines or sp & cf_hit:
                                    stmt_lines |= h_hdr
                    elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        hdr = _stmt_entry_lines(sub)
                        if hdr & hit_lines or sp & cf_hit:
                            stmt_lines |= hdr
                    else:
                        stmt_lines |= sp
            for attr in ('body', 'orelse', 'finalbody'):
                raw_blk = getattr(sub, attr, None)
                if isinstance(raw_blk, list) and raw_blk:
                    blk = _strip_leading_docstring(raw_blk) if attr == 'body' else raw_blk
                    if not blk:
                        continue
                    for idx_s, s_item in enumerate(blk):
                        s_start = getattr(s_item, 'lineno', 0)
                        s_end = getattr(s_item, 'end_lineno', s_start)
                        prev_end = getattr(blk[idx_s - 1], 'end_lineno', 0) if idx_s > 0 else getattr(sub, 'lineno', 0)
                        if any((prev_end <= al <= s_start for al in anchor_old_lines)):
                            stmt_lines |= _stmt_entry_lines(s_item)
                            if idx_s > 0:
                                stmt_lines |= _stmt_entry_lines(blk[idx_s - 1])
                        if idx_s == len(blk) - 1:
                            block_end = getattr(sub, 'end_lineno', s_end)
                            if any((s_end <= al <= block_end + 1 for al in anchor_old_lines)):
                                stmt_lines |= _stmt_entry_lines(s_item)
        safe_sig = sig_changed and _is_safe_func_sig_change(os_node, ns_node, file, has_future_ann)
        if safe_sig:
            exec_body = _strip_leading_docstring(os_node.body)
            entry_lines = _stmt_entry_lines(exec_body[0]) if exec_body else set(fn_span)
            units.append(ChangeUnit(kind='func_body', file=file, class_name=class_qual, symbol_name=qname, old_lines=entry_lines | stmt_lines, enclosing_lines=fn_span, has_nested_func=has_nested_func, edit_outside_nested_func=True, is_cached_func=is_cached, sig_changed=True))
        else:
            if not stmt_lines or sig_changed:
                stmt_lines = set(fn_span)
            units.append(ChangeUnit(kind='class_attr' if sig_changed and class_qual else 'module_other' if sig_changed else 'func_body', file=file, class_name=class_qual, symbol_name='*' if sig_changed else qname, old_lines=hit_lines | stmt_lines, enclosing_lines=fn_span, has_nested_func=has_nested_func, edit_outside_nested_func=edit_outside_nested, is_cached_func=is_cached, sig_changed=sig_changed))
    elif kind_tag == 'assign':
        _, o_val = _extract_assign_target_and_value(os_node)
        _, n_val = _extract_assign_target_and_value(ns_node)
        span = _stmt_line_span(os_node)
        if isinstance(o_val, ast.Dict) and isinstance(n_val, ast.Dict):
            diff_keys, added_keys, deleted_keys, lam_lines = _diff_dict_nodes_detailed(o_val, n_val)
            units.append(ChangeUnit(kind='dict_lambda_body' if lam_lines is not None else 'dict_key', file=file, class_name=class_qual, symbol_name=name, dict_keys=diff_keys, dict_added_keys=added_keys, dict_deleted_keys=deleted_keys, old_lines=lam_lines if lam_lines is not None else span & (changed_old_lines | anchor_old_lines), enclosing_lines=span))
        else:
            units.append(ChangeUnit(kind='class_attr' if class_qual else 'module_attr', file=file, class_name=class_qual, symbol_name=name, old_lines=span & (changed_old_lines | anchor_old_lines), enclosing_lines=span))

def _compare_block(file: str, old_body: list[ast.stmt], new_body: list[ast.stmt], old_lines_arr: list[str], new_lines_arr: list[str], class_chain: list[str], changed_old_lines: set[int], anchor_old_lines: set[int], cf_old_lines: set[int], units: list[ChangeUnit], changed_funcs: list[str], ignore_docstrings: bool=False, has_future_ann: bool=False) -> None:
    if ignore_docstrings:
        old_body = _strip_leading_docstring(old_body)
        new_body = _strip_leading_docstring(new_body)

    def stmt_key(s: ast.stmt) -> tuple[str, str] | None:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ('func', s.name)
        if isinstance(s, ast.ClassDef):
            return ('class', s.name)
        name, _ = _extract_assign_target_and_value(s)
        if name is not None:
            return ('assign', name)
        return None
    old_by_key: dict[tuple[str, str], list[ast.stmt]] = {}
    new_by_key: dict[tuple[str, str], list[ast.stmt]] = {}
    old_unkeyed: list[ast.stmt] = []
    new_unkeyed: list[ast.stmt] = []
    old_order: list[tuple[str, str]] = []
    new_order: list[tuple[str, str]] = []
    for s in old_body:
        if _is_type_checking_if(s):
            continue
        k = stmt_key(s)
        if k is None:
            old_unkeyed.append(s)
            old_order.append(('unkeyed', ''))
        else:
            old_by_key.setdefault(k, []).append(s)
            old_order.append(k)
    for s in new_body:
        if _is_type_checking_if(s):
            continue
        k = stmt_key(s)
        if k is None:
            new_unkeyed.append(s)
            new_order.append(('unkeyed', ''))
        else:
            new_by_key.setdefault(k, []).append(s)
            new_order.append(k)
    class_qual = '.'.join(class_chain) if class_chain else None
    common_keys = set(old_order) & set(new_order)
    old_common = [k for k in old_order if k in common_keys]
    new_common = [k for k in new_order if k in common_keys]
    if not _ast_nodes_equiv(old_unkeyed, new_unkeyed, ignore_docstrings) or old_common != new_common:
        units.append(ChangeUnit(kind='class_attr' if class_qual else 'module_other', file=file, class_name=class_qual, symbol_name='*', old_lines=set(changed_old_lines | anchor_old_lines)))
    for key in set(old_by_key) | set(new_by_key):
        o_list = old_by_key.get(key, [])
        n_list = new_by_key.get(key, [])
        if len(o_list) == 1 and len(n_list) == 1 and _stmt_src_equal(o_list[0], n_list[0], old_lines_arr, new_lines_arr):
            continue
        if _ast_nodes_equiv(o_list, n_list, ignore_docstrings):
            continue
        kind_tag, name = key
        if kind_tag == 'func' and len(o_list) > 1 and (len(o_list) == len(n_list)) and all((_is_overload_fn(s) for s in o_list[:-1])) and all((_is_overload_fn(s) for s in n_list[:-1])) and (not _is_overload_fn(o_list[-1])) and (not _is_overload_fn(n_list[-1])) and _ast_nodes_equiv(o_list[:-1], n_list[:-1], ignore_docstrings):
            o_list = [o_list[-1]]
            n_list = [n_list[-1]]
        if len(o_list) != 1 or len(n_list) != 1:
            span = set()
            for s in o_list:
                span |= _stmt_line_span(s)
            if kind_tag == 'func':
                qname = f'{class_qual}.{name}' if class_qual else name
                changed_funcs.append(qname)
                is_dyn_sql_meth = bool(class_qual) and name.endswith('_sql') and (not name.startswith('_')) and (name != 'sql')
                units.append(ChangeUnit(kind='class_attr' if class_qual else 'module_other', file=file, class_name=class_qual, symbol_name=name if is_dyn_sql_meth else '*', old_lines=span & (changed_old_lines | anchor_old_lines), enclosing_lines=span))
            elif kind_tag == 'class':
                sub_qual = f'{class_qual}.{name}' if class_qual else name
                units.append(ChangeUnit(kind='class_attr', file=file, class_name=sub_qual, symbol_name='*', old_lines=span, enclosing_lines=span))
            else:
                units.append(ChangeUnit(kind='class_attr' if class_qual else 'module_attr', file=file, class_name=class_qual, symbol_name=name, old_lines=span, enclosing_lines=span))
            continue
        os_node = o_list[0]
        ns_node = n_list[0]
        if kind_tag == 'class':
            assert isinstance(os_node, ast.ClassDef) and isinstance(ns_node, ast.ClassDef)
            sub_chain = [*class_chain, name]
            sub_qual = '.'.join(sub_chain)
            if not _ast_nodes_equiv(os_node.bases, ns_node.bases) or not _ast_nodes_equiv(os_node.keywords, ns_node.keywords) or (not _ast_nodes_equiv(os_node.decorator_list, ns_node.decorator_list)):
                units.append(ChangeUnit(kind='class_attr', file=file, class_name=sub_qual, symbol_name='*', old_lines=_stmt_line_span(os_node)))
            _compare_block(file, os_node.body, ns_node.body, old_lines_arr, new_lines_arr, sub_chain, changed_old_lines, anchor_old_lines, cf_old_lines, units, changed_funcs, ignore_docstrings, has_future_ann)
        elif kind_tag in ('func', 'assign'):
            _emit_single_unit(file, kind_tag, name, class_qual, os_node, ns_node, changed_old_lines, anchor_old_lines, cf_old_lines, units, changed_funcs, ignore_docstrings, has_future_ann)

def _src_digest(src: str) -> bytes:
    return _blake2b(src.encode('utf-8', 'surrogatepass'), digest_size=8).digest()

def _build_dict_meta(dict_node: ast.Dict, lines: list[str]) -> tuple[Any, ...] | None:
    keys = dict_node.keys
    values = dict_node.values
    m = len(keys)
    if m == 0:
        return None
    open_ln = dict_node.lineno
    close_ln = dict_node.end_lineno
    if close_ln is None or close_ln <= open_ln + 1:
        return None
    if lines[open_ln - 1].endswith('\\'):
        return None
    raw_entries: list[tuple[int, int, int, str | None]] = []
    seg = 0
    key_counts: dict[str, int] = {}
    prev_end = open_ln
    for k, v in zip(keys, values):
        es = k.lineno if k is not None else v.lineno
        ee = v.end_lineno
        if es is None or ee is None or es <= prev_end or (ee >= close_ln):
            return None
        if k is None:
            raw_entries.append((es, ee, seg, None))
            seg += 1
        else:
            kr = _const_key_repr(k)
            if kr is None or kr == '__BLAST_SENTINEL__':
                return None
            key_counts[kr] = key_counts.get(kr, 0) + 1
            raw_entries.append((es, ee, seg, kr))
        prev_end = ee
    slots: list[tuple[int, int, int, str | None]] = []
    slot_starts: list[int] = []
    for i, (es, ee, sg, kr) in enumerate(raw_entries):
        sl_start = open_ln + 1 if i == 0 else es
        sl_end = raw_entries[i + 1][0] - 1 if i + 1 < m else close_ln - 1
        if sl_end < sl_start:
            return None
        if sl_start > 1 and lines[sl_start - 2].endswith('\\'):
            return None
        if lines[sl_end - 1].endswith('\\'):
            return None
        slot_txt = '\n'.join(lines[sl_start - 1:sl_end])
        if i + 1 < m:
            probe = '{\n' + slot_txt + '\n__BLAST_SENTINEL__: 0\n}'
            expect_n = 2
        else:
            probe = '{\n' + slot_txt + '\n}'
            expect_n = 1
        try:
            ptree = ast.parse(probe)
        except SyntaxError:
            return None
        if len(ptree.body) != 1 or not isinstance(ptree.body[0], ast.Expr) or (not isinstance(ptree.body[0].value, ast.Dict)):
            return None
        pdict = ptree.body[0].value
        if len(pdict.keys) != expect_n:
            return None
        pk = pdict.keys[0]
        if kr is None:
            if pk is not None:
                return None
        elif _const_key_repr(pk) != kr:
            return None
        slots.append((sl_start, sl_end, sg, kr))
        slot_starts.append(sl_start)
    return (open_ln, close_ln, slots, slot_starts, key_counts)

def index_python_source(src: str, filename: str='<baseline>') -> dict[str, Any] | None:
    if not src or '\r' in src or src.startswith('\ufeff') or (not src.endswith('\n')):
        return None
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError:
        return None
    lines = src.split('\n')
    n_real = len(lines) - 1
    coding = _coding_cookie(src)
    has_future_ann = _has_future_annotations(tree)
    units: list[tuple[Any, ...]] = []
    gaps: list[tuple[int, int]] = []

    def _stmt_key(s: ast.stmt) -> tuple[str, str] | None:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ('func', s.name)
        if isinstance(s, ast.ClassDef):
            return ('class', s.name)
        nm, _ = _extract_assign_target_and_value(s)
        if nm is not None:
            return ('assign', nm)
        return None

    def _index_block(stmts: list[ast.stmt], class_chain: tuple[str, ...], class_indents: tuple[str, ...], enclosing_header_end: int) -> None:
        by_key: dict[tuple[str, str], list[ast.stmt]] = {}
        for s in stmts:
            if _is_type_checking_if(s):
                continue
            k = _stmt_key(s)
            if k is not None:
                by_key.setdefault(k, []).append(s)
        n_s = len(stmts)
        for i in range(n_s - 1):
            pe = stmts[i].end_lineno
            ns = _stmt_start_line(stmts[i + 1])
            if pe is not None and ns > pe + 1 and (not lines[pe - 1].endswith('\\')):
                gs = pe + 1 if pe + 1 >= 3 else 3
                ge = ns - 1
                if gs <= ge:
                    gaps.append((gs, ge))
        if not class_chain and n_s > 0:
            le = stmts[-1].end_lineno
            if le is not None and le < n_real and (not lines[le - 1].endswith('\\')):
                gs = le + 1 if le + 1 >= 3 else 3
                if gs <= n_real:
                    gaps.append((gs, n_real))
        for i, s in enumerate(stmts):
            s_start = _stmt_start_line(s)
            s_end = s.end_lineno
            if s_end is None or s_start <= 2 or s_start <= enclosing_header_end:
                continue
            if i > 0 and (stmts[i - 1].end_lineno or s_start) >= s_start:
                continue
            if i + 1 < n_s and s_end >= _stmt_start_line(stmts[i + 1]):
                continue
            first_line = lines[s_start - 1]
            decs = getattr(s, 'decorator_list', None) or ()
            s_col = min((d.col_offset for d in decs if d.lineno == s_start), default=s.col_offset)
            if first_line[:s_col].strip() != '':
                continue
            if s_start > 1 and lines[s_start - 2].endswith('\\'):
                continue
            if isinstance(s, ast.ClassDef):
                if len(by_key.get(('class', s.name), ())) != 1:
                    continue
                cls_line = lines[s.lineno - 1]
                cls_indent = cls_line[:s.col_offset]
                if cls_indent.strip() != '':
                    continue
                hdr_end = s.lineno
                for node_h in (*s.bases, *s.keywords):
                    he = getattr(node_h, 'end_lineno', None)
                    if he is not None and he > hdr_end:
                        hdr_end = he
                _index_block(s.body, (*class_chain, s.name), (*class_indents, cls_indent), hdr_end)
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                siblings = by_key.get(('func', s.name), ())
                is_ov_impl = False
                if len(siblings) == 1:
                    pass
                elif len(siblings) > 1 and siblings[-1] is s and (not _is_overload_fn(s)) and all((_is_overload_fn(x) for x in siblings[:-1])):
                    is_ov_impl = True
                else:
                    continue
                units.append((s_start, s_end, s.lineno, 'func', s.name, class_chain, class_indents, is_ov_impl, None))
            elif isinstance(s, (ast.Assign, ast.AnnAssign)):
                nm, val = _extract_assign_target_and_value(s)
                if nm is None or len(by_key.get(('assign', nm), ())) != 1:
                    continue
                if isinstance(val, ast.Dict):
                    dmeta = _build_dict_meta(val, lines)
                    units.append((s_start, s_end, s.lineno, 'dict', nm, class_chain, class_indents, False, dmeta))
                else:
                    units.append((s_start, s_end, s.lineno, 'assign', nm, class_chain, class_indents, False, None))
            elif _is_type_checking_if(s):
                units.append((s_start, s_end, s.lineno, 'type_checking', '', class_chain, class_indents, False, None))
    _index_block(tree.body, (), (), 0)
    gaps.sort()
    unit_starts = [u[0] for u in units]
    gap_starts = [g[0] for g in gaps]
    return {'sig': (len(src), _src_digest(src)), 'n_real': n_real, 'coding': coding, 'has_future_ann': has_future_ann, 'units': units, 'unit_starts': unit_starts, 'gaps': gaps, 'gap_starts': gap_starts}

def build_base_ast_index(sg_root: Path, out_dir: Path=BASE_AST_INDEX_DIR) -> tuple[int, int, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    toc: dict[str, int] = {}
    total_units = 0
    total_bytes = 0
    idx = 0
    for sub in ('sqlglot', 'tests'):
        sub_dir = sg_root / sub
        if not sub_dir.is_dir():
            continue
        for p in sorted(sub_dir.rglob('*.py')):
            rel = p.relative_to(sg_root).as_posix()
            src = p.read_text(encoding='utf-8', errors='replace')
            entry = index_python_source(src, filename=rel)
            if entry is None:
                continue
            blob = marshal.dumps(entry)
            (out_dir / f'{idx}.marshal').write_bytes(blob)
            toc[rel] = idx
            total_units += len(entry['units'])
            total_bytes += len(blob)
            idx += 1
    toc_blob = marshal.dumps(toc)
    (out_dir / 'toc.marshal').write_bytes(toc_blob)
    total_bytes += len(toc_blob)
    return (len(toc), total_units, total_bytes)

class BaseAstIndex:
    __slots__ = ('_dir', '_toc', '_cache', '_mem', '_verified')

    def __init__(self, index_dir: Path | None=None, mem_entries: dict[str, dict[str, Any]] | None=None) -> None:
        self._dir = index_dir
        self._toc: dict[str, int] | None = None
        self._cache: dict[str, dict[str, Any] | None] = {}
        self._mem = mem_entries
        self._verified: dict[str, tuple[str, dict[str, Any]]] = {}
        if index_dir is not None and mem_entries is None:
            toc_p = index_dir / 'toc.marshal'
            if toc_p.is_file():
                try:
                    self._toc = marshal.loads(toc_p.read_bytes())
                except Exception:
                    self._toc = None

    @property
    def available(self) -> bool:
        return self._mem is not None or self._toc is not None

    def get(self, rel: str) -> dict[str, Any] | None:
        if self._mem is not None:
            return self._mem.get(rel)
        if self._toc is None or self._dir is None:
            return None
        if rel in self._cache:
            return self._cache[rel]
        idx = self._toc.get(rel)
        if idx is None:
            self._cache[rel] = None
            return None
        p = self._dir / f'{idx}.marshal'
        if not p.is_file():
            self._cache[rel] = None
            return None
        try:
            data = marshal.loads(p.read_bytes())
        except Exception:
            data = None
        self._cache[rel] = data
        return data

    def get_verified(self, rel: str, old_src: str) -> dict[str, Any] | None:
        prev = self._verified.get(rel)
        if prev is not None and prev[0] is old_src:
            return prev[1]
        entry = self.get(rel)
        if entry is None:
            return None
        sig = entry.get('sig')
        if sig is None or len(old_src) != sig[0]:
            return None
        if _src_digest(old_src) != sig[1]:
            return None
        self._verified[rel] = (old_src, entry)
        return entry
_DEFAULT_BASE_AST_INDEX: BaseAstIndex | None = None
_DEFAULT_BASE_AST_LOADED: bool = False

def load_base_ast_index(index_dir: Path=BASE_AST_INDEX_DIR) -> BaseAstIndex | None:
    global _DEFAULT_BASE_AST_INDEX, _DEFAULT_BASE_AST_LOADED
    if index_dir == BASE_AST_INDEX_DIR:
        if not _DEFAULT_BASE_AST_LOADED:
            _DEFAULT_BASE_AST_LOADED = True
            idx = BaseAstIndex(index_dir=index_dir)
            _DEFAULT_BASE_AST_INDEX = idx if idx.available else None
        return _DEFAULT_BASE_AST_INDEX
    idx = BaseAstIndex(index_dir=index_dir)
    return idx if idx.available else None

def _diff_dict_windows_detailed(dict_windows: list[tuple[int, ast.Dict, ast.Dict]]) -> tuple[set[str] | None, set[str], set[str], set[int] | None]:
    old_map: dict[str, list[tuple[int, str, ast.AST]]] = {}
    new_map: dict[str, list[tuple[int, str, ast.AST]]] = {}
    for win_seg, od, nd in dict_windows:
        for k, v in zip(od.keys, od.values):
            kr = _const_key_repr(k)
            if kr is None:
                return (None, set(), set(), None)
            old_map.setdefault(kr, []).append((win_seg, ast.dump(v, include_attributes=False), v))
        for k, v in zip(nd.keys, nd.values):
            kr = _const_key_repr(k)
            if kr is None:
                return (None, set(), set(), None)
            new_map.setdefault(kr, []).append((win_seg, ast.dump(v, include_attributes=False), v))
    old_keys = set(old_map)
    new_keys = set(new_map)
    added_keys = new_keys - old_keys
    deleted_keys = old_keys - new_keys
    changed_keys = {k for k in old_keys | new_keys if [(s, d) for s, d, _ in old_map.get(k, ())] != [(s, d) for s, d, _ in new_map.get(k, ())]}
    if not changed_keys:
        return (None, set(), set(), None)
    lambda_only_lines: set[int] | None = None
    if not added_keys and (not deleted_keys):
        lam_lines: set[int] = set()
        all_lam = True
        for k in changed_keys:
            ol = old_map[k]
            nl = new_map[k]
            if len(ol) == 1 and len(nl) == 1 and (ol[0][0] == nl[0][0]) and isinstance(ol[0][2], ast.Lambda) and isinstance(nl[0][2], ast.Lambda) and (ast.dump(ol[0][2].args, include_attributes=False) == ast.dump(nl[0][2].args, include_attributes=False)):
                lam_lines |= _stmt_line_span(ol[0][2].body)
            else:
                all_lam = False
                break
        if all_lam and lam_lines:
            lambda_only_lines = lam_lines
    return (changed_keys, added_keys, deleted_keys, lambda_only_lines)

def _try_parse_dict_windows(dict_meta: tuple[Any, ...], u_blocks: list[tuple[int, int, int, int]], old_lines: list[str], new_lines: list[str], n_new_real: int) -> list[tuple[int, ast.Dict, ast.Dict]] | None:
    open_ln, close_ln, slots, slot_starts, key_counts = dict_meta
    m = len(slots)
    if m == 0:
        return None
    slot_ranges: list[tuple[int, int, tuple[int, int, int, int]]] = []
    for blk in u_blocks:
        bo0, bo1, bn0, bn1 = blk
        if bo1 > bo0:
            if bo0 < open_ln or bo1 > close_ln - 1:
                return None
            slo = bisect.bisect_right(slot_starts, bo0 + 1) - 1
            shi = bisect.bisect_right(slot_starts, bo1) - 1
            if slo < 0 or shi >= m or slo > shi:
                return None
            if slots[slo][0] - 1 > bo0 or bo1 > slots[shi][1]:
                return None
        else:
            if bo0 < open_ln or bo0 > close_ln - 1:
                return None
            if bo0 == open_ln:
                slo = shi = 0
            else:
                idx = bisect.bisect_right(slot_starts, bo0) - 1
                if idx < 0 or idx >= m or (not slots[idx][0] <= bo0 <= slots[idx][1]):
                    return None
                slo = shi = idx
        slot_ranges.append((slo, shi, blk))
    windows: list[tuple[int, int, list[tuple[int, int, int, int]]]] = []
    for slo, shi, blk in slot_ranges:
        if windows and slo <= windows[-1][1] + 1:
            prev_lo, prev_hi, prev_blks = windows[-1]
            windows[-1] = (prev_lo, shi if shi > prev_hi else prev_hi, prev_blks)
            prev_blks.append(blk)
        else:
            windows.append((slo, shi, [blk]))
    tot_win_lines = 0
    for lo_w, hi_w, _ in windows:
        for si in range(lo_w, hi_w + 1):
            if slots[si][3] is None:
                return None
        tot_win_lines += slots[hi_w][1] - slots[lo_w][0] + 1
    if tot_win_lines * 2 > close_ln - open_ln:
        return None
    parsed_windows: list[tuple[int, ast.Dict, ast.Dict]] = []
    touched_old_key_counts: dict[str, int] = {}
    all_touched_keys: set[str] = set()
    for lo_w, hi_w, w_blocks in windows:
        w_old_0 = slots[lo_w][0] - 1
        w_old_1 = slots[hi_w][1]
        bf = w_blocks[0]
        bl = w_blocks[-1]
        w_new_0 = w_old_0 + (bf[2] - bf[0])
        w_new_1 = w_old_1 + (bl[3] - bl[1])
        if w_new_0 < 0 or w_new_1 < w_new_0 or w_new_1 > n_new_real:
            return None
        if w_new_1 > w_new_0 and new_lines[w_new_1 - 1].endswith('\\'):
            return None
        has_following = hi_w + 1 < m
        old_txt = '\n'.join(old_lines[w_old_0:w_old_1])
        new_txt = '\n'.join(new_lines[w_new_0:w_new_1])
        if has_following:
            old_probe = '{\n' + old_txt + '\n__BLAST_SENTINEL__: 0\n}'
            new_probe = '{\n' + new_txt + '\n__BLAST_SENTINEL__: 0\n}'
        else:
            old_probe = '{\n' + old_txt + '\n}'
            new_probe = '{\n' + new_txt + '\n}'
        try:
            old_wt = ast.parse(old_probe)
            new_wt = ast.parse(new_probe)
        except SyntaxError:
            return None
        if len(old_wt.body) != 1 or len(new_wt.body) != 1 or (not isinstance(old_wt.body[0], ast.Expr)) or (not isinstance(new_wt.body[0], ast.Expr)) or (not isinstance(old_wt.body[0].value, ast.Dict)) or (not isinstance(new_wt.body[0].value, ast.Dict)):
            return None
        od = old_wt.body[0].value
        nd = new_wt.body[0].value
        if has_following:
            if not od.keys or not nd.keys or (not isinstance(od.keys[-1], ast.Name)) or (od.keys[-1].id != '__BLAST_SENTINEL__') or (not isinstance(nd.keys[-1], ast.Name)) or (nd.keys[-1].id != '__BLAST_SENTINEL__'):
                return None
            od.keys.pop()
            od.values.pop()
            nd.keys.pop()
            nd.values.pop()
        if len(od.keys) != hi_w - lo_w + 1:
            return None
        for k in od.keys:
            if k is None:
                return None
            kr = _const_key_repr(k)
            if kr is None:
                return None
            touched_old_key_counts[kr] = touched_old_key_counts.get(kr, 0) + 1
            all_touched_keys.add(kr)
        for k in nd.keys:
            if k is None:
                return None
            kr = _const_key_repr(k)
            if kr is None:
                return None
            all_touched_keys.add(kr)
        if w_old_0 - 1 != 0:
            ast.increment_lineno(od, w_old_0 - 1)
        if w_new_0 - 1 != 0:
            ast.increment_lineno(nd, w_new_0 - 1)
        parsed_windows.append((slots[lo_w][2], od, nd))
    for kr in all_touched_keys:
        if key_counts.get(kr, 0) > touched_old_key_counts.get(kr, 0):
            return None
    return parsed_windows

def _try_incremental_analyze(file: str, old_src: str, new_src: str, fp: FilePatch, changed_old: set[int], anchor_old: set[int], cf_old: set[int], base_ast_index: BaseAstIndex, pure_enums: set[str] | None, strict_enum_attrs: set[tuple[str, str]] | None, ignore_docstrings: bool, cmp_agree: frozenset[str] | None) -> PyFileAnalysis | None:
    if not old_src or not new_src or '\r' in old_src or ('\r' in new_src) or ('\x00' in old_src) or ('\x00' in new_src):
        return None
    if not old_src.endswith('\n') or not new_src.endswith('\n'):
        return None
    idx_entry = base_ast_index.get_verified(file, old_src)
    if idx_entry is None or idx_entry['coding'] is not None:
        return None
    if not fp.hunks:
        return PyFileAnalysis(file=file, is_noop=True) if old_src == new_src else None
    old_lines = old_src.split('\n')
    new_lines = new_src.split('\n')
    n_old_real = len(old_lines) - 1
    n_new_real = len(new_lines) - 1
    if n_old_real != idx_entry['n_real']:
        return None
    blocks: list[tuple[int, int, int, int]] = []
    old_pos = 0
    new_pos = 0
    for hunk in fp.hunks:
        if hunk.old_count == 0:
            target = max(0, hunk.old_start)
        else:
            target = max(0, hunk.old_start - 1) if hunk.old_start > 0 else 0
        if target < old_pos or target > n_old_real:
            return None
        gap = target - old_pos
        old_pos += gap
        new_pos += gap
        hlines = hunk.lines
        hi = 0
        hn = len(hlines)
        while hi < hn:
            hl = hlines[hi]
            if hl.startswith('\\'):
                return None
            tag = hl[0]
            if tag == ' ':
                if old_pos >= n_old_real:
                    return None
                old_pos += 1
                new_pos += 1
                hi += 1
            elif tag in ('-', '+'):
                bo0 = old_pos
                bn0 = new_pos
                while hi < hn and hlines[hi][0] in ('-', '+'):
                    if hlines[hi][0] == '-':
                        if old_pos >= n_old_real:
                            return None
                        old_pos += 1
                    else:
                        new_pos += 1
                    hi += 1
                if bo0 < 2 or bn0 < 2:
                    return None
                blocks.append((bo0, old_pos, bn0, new_pos))
            else:
                return None
    if new_pos - old_pos != n_new_real - n_old_real:
        return None
    if not blocks:
        return PyFileAnalysis(file=file, is_noop=True)
    units_idx: list[tuple[Any, ...]] = idx_entry['units']
    unit_starts: list[int] = idx_entry['unit_starts']
    gaps: list[tuple[int, int]] = idx_entry['gaps']
    gap_starts: list[int] = idx_entry['gap_starts']
    touched_blocks_by_uidx: dict[int, list[tuple[int, int, int, int]]] = {}
    for blk in blocks:
        bo0, bo1, bn0, bn1 = blk
        matched_u = -1
        if bo1 > bo0:
            ui = bisect.bisect_right(unit_starts, bo0 + 1) - 1
            if ui >= 0:
                u = units_idx[ui]
                if u[0] - 1 <= bo0 and bo1 <= u[1]:
                    matched_u = ui
        else:
            ui = bisect.bisect_right(unit_starts, bo0) - 1
            if ui >= 0:
                u = units_idx[ui]
                ukind = u[3]
                if ukind in ('func', 'type_checking'):
                    if u[0] <= bo0 <= u[1]:
                        matched_u = ui
                elif u[0] <= bo0 < u[1]:
                    matched_u = ui
        if matched_u >= 0:
            touched_blocks_by_uidx.setdefault(matched_u, []).append(blk)
            continue
        is_comment_blank = True
        for ln_s in old_lines[bo0:bo1]:
            st = ln_s.lstrip(' \t')
            if st and (not st.startswith('#')):
                is_comment_blank = False
                break
        if is_comment_blank:
            for ln_s in new_lines[bn0:bn1]:
                st = ln_s.lstrip(' \t')
                if st and (not st.startswith('#')):
                    is_comment_blank = False
                    break
        if not is_comment_blank:
            return None
        gi = bisect.bisect_right(gap_starts, bo0 + 1) - 1
        if gi < 0:
            return None
        gs, ge = gaps[gi]
        if bo1 > bo0:
            if not (gs - 1 <= bo0 and bo1 <= ge):
                return None
        elif not gs - 1 <= bo0 <= ge:
            return None
    if not touched_blocks_by_uidx:
        return PyFileAnalysis(file=file, is_noop=True)
    if len(touched_blocks_by_uidx) > 32:
        return None
    parsed_units: list[tuple[Any, ...]] = []
    tot_stmt_lines = 0
    for ui in sorted(touched_blocks_by_uidx):
        u = units_idx[ui]
        s_start, s_end, stmt_lineno, ukind, uname, class_chain, class_indents, is_ov_impl, dict_meta = u
        u_blocks = touched_blocks_by_uidx[ui]
        if ukind == 'dict' and dict_meta is not None:
            dw = _try_parse_dict_windows(dict_meta, u_blocks, old_lines, new_lines, n_new_real)
            if dw is not None:
                parsed_units.append(('dict_win', u, dw))
                continue
        u0 = s_start - 1
        u1 = s_end
        bf = u_blocks[0]
        bl = u_blocks[-1]
        ns0 = u0 + (bf[2] - bf[0])
        ns1 = u1 + (bl[3] - bl[1])
        if ns0 < 0 or ns1 <= ns0 or ns1 > n_new_real:
            return None
        if new_lines[ns1 - 1].endswith('\\'):
            return None
        tot_stmt_lines += u1 - u0
        if tot_stmt_lines * 2 > n_old_real:
            return None
        d = len(class_indents)
        prefix = ''.join((f'{ind}class _C{i}:\n' for i, ind in enumerate(class_indents)))
        if d > 0:
            old_line0 = old_lines[u0]
            old_lead = old_line0[:len(old_line0) - len(old_line0.lstrip(' \t'))]
            prefix += f'{old_lead}pass\n'
            prefix_lines = d + 1
            exp_body_len = 2
            body_idx = 1
        else:
            prefix_lines = 0
            exp_body_len = 1
            body_idx = 0
        try:
            otree = ast.parse(prefix + '\n'.join(old_lines[u0:u1]))
            ntree = ast.parse(prefix + '\n'.join(new_lines[ns0:ns1]))
        except SyntaxError:
            return None
        obody = otree.body
        nbody = ntree.body
        for _ in range(d):
            if len(obody) != 1 or len(nbody) != 1 or (not isinstance(obody[0], ast.ClassDef)) or (not isinstance(nbody[0], ast.ClassDef)):
                return None
            obody = obody[0].body
            nbody = nbody[0].body
        if len(obody) != exp_body_len or len(nbody) != exp_body_len:
            return None
        os_node = obody[body_idx]
        ns_node = nbody[body_idx]
        if ukind == 'func':
            if not isinstance(os_node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not isinstance(ns_node, (ast.FunctionDef, ast.AsyncFunctionDef)) or ns_node.name != uname:
                return None
            if is_ov_impl and _is_overload_fn(ns_node):
                return None
        elif ukind in ('dict', 'assign'):
            n_nm, _ = _extract_assign_target_and_value(ns_node)
            if n_nm != uname:
                return None
        elif ukind == 'type_checking':
            if not (_is_type_checking_if(os_node) and _is_type_checking_if(ns_node)):
                return None
        else:
            return None
        if u0 - prefix_lines != 0:
            ast.increment_lineno(os_node, u0 - prefix_lines)
        if ns0 - prefix_lines != 0:
            ast.increment_lineno(ns_node, ns0 - prefix_lines)
        if os_node.lineno != stmt_lineno or os_node.end_lineno != s_end:
            return None
        parsed_units.append(('stmt', u, os_node, ns_node))

    def _pu_equiv(pu: tuple[Any, ...], allow_sound_cmp: bool=False, allow_none: bool=False, cmp_site_ok: Any=None) -> bool:
        if pu[0] == 'dict_win':
            return all((_ast_nodes_equiv(od, nd, ignore_docstrings, False, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok) for _, od, nd in pu[2]))
        return _ast_nodes_equiv(pu[2], pu[3], ignore_docstrings, False, allow_sound_cmp, allow_none, pure_enums, strict_enum_attrs, cmp_site_ok)
    base_equivs = [_pu_equiv(pu) for pu in parsed_units]
    if all(base_equivs):
        return PyFileAnalysis(file=file, is_noop=True)
    has_cmp_edit = _diff_has_identity_or_eq_change(fp)
    if has_cmp_edit:
        if file.startswith('sqlglot/'):
            allow_none = not file.startswith('sqlglot/executor/') and file != 'sqlglot/expressions/builders.py'
            if all((eq or _pu_equiv(pu, allow_sound_cmp=True, allow_none=allow_none) for eq, pu in zip(base_equivs, parsed_units))):
                return PyFileAnalysis(file=file, is_noop=True)
        if cmp_agree:
            cmp_ok = lambda line, col: f'{file}:{line}:{col}' in cmp_agree
            if all((eq or _pu_equiv(pu, cmp_site_ok=cmp_ok) for eq, pu in zip(base_equivs, parsed_units))):
                return PyFileAnalysis(file=file, is_noop=True)
    units_out: list[ChangeUnit] = []
    changed_funcs: list[str] = []
    has_future_ann = idx_entry['has_future_ann']
    any_non_tc_changed = False
    for eq, pu in zip(base_equivs, parsed_units):
        if eq:
            continue
        u = pu[1]
        s_start, s_end, stmt_lineno, ukind, uname, class_chain, _, _, _ = u
        if ukind == 'type_checking':
            continue
        any_non_tc_changed = True
        class_qual = '.'.join(class_chain) if class_chain else None
        if pu[0] == 'dict_win':
            span = set(range(stmt_lineno, s_end + 1))
            diff_keys, added_keys, deleted_keys, lam_lines = _diff_dict_windows_detailed(pu[2])
            units_out.append(ChangeUnit(kind='dict_lambda_body' if lam_lines is not None else 'dict_key', file=file, class_name=class_qual, symbol_name=uname, dict_keys=diff_keys, dict_added_keys=added_keys, dict_deleted_keys=deleted_keys, old_lines=lam_lines if lam_lines is not None else span & (changed_old | anchor_old), enclosing_lines=span))
        else:
            kind_tag = 'func' if ukind == 'func' else 'assign'
            _emit_single_unit(file, kind_tag, uname, class_qual, pu[2], pu[3], changed_old, anchor_old, cf_old, units_out, changed_funcs, ignore_docstrings, has_future_ann)
    if not units_out:
        if not file.startswith('sqlglot/typing/') and (not any_non_tc_changed):
            return PyFileAnalysis(file=file, is_noop=True)
        units_out.append(ChangeUnit(kind='module_other', file=file, symbol_name='*', old_lines=changed_old))
    return PyFileAnalysis(file=file, is_noop=False, units=units_out, all_changed_old_lines=changed_old | anchor_old, changed_funcs=changed_funcs)

def analyze_python_change(file: str, old_src: str, new_src: str, fp: FilePatch, *, old_ast_cache: dict[str, ast.Module] | None=None, pure_enums: set[str] | None=None, strict_enum_attrs: set[tuple[str, str]] | None=None, ignore_docstrings: bool=False, cmp_agree: frozenset[str] | None=None, base_ast_index: BaseAstIndex | None=None) -> PyFileAnalysis:
    changed_old, anchor_old, cf_old = _extract_hunk_lines(fp)
    if fp.is_new or fp.is_deleted:
        try:
            if not fp.is_deleted:
                ast.parse(new_src.encode('utf-8'), filename=file)
        except SyntaxError:
            return PyFileAnalysis(file=file, is_noop=False, syntax_error=True, units=[ChangeUnit(kind='syntax_error', file=file)])
        return PyFileAnalysis(file=file, is_noop=False, units=[ChangeUnit(kind='module_other', file=file, symbol_name='*')])
    if base_ast_index is not None:
        inc = _try_incremental_analyze(file, old_src, new_src, fp, changed_old, anchor_old, cf_old, base_ast_index, pure_enums, strict_enum_attrs, ignore_docstrings, cmp_agree)
        if inc is not None:
            return inc
    try:
        new_ast = ast.parse(new_src.encode('utf-8'), filename=file)
    except SyntaxError:
        return PyFileAnalysis(file=file, is_noop=False, syntax_error=True, units=[ChangeUnit(kind='syntax_error', file=file)], all_changed_old_lines=changed_old)
    if old_ast_cache is not None and file in old_ast_cache:
        old_ast = old_ast_cache[file]
    else:
        old_ast = ast.parse(old_src, filename=file)
        if old_ast_cache is not None:
            old_ast_cache[file] = old_ast
    old_lines = _source_lines(old_src)
    new_lines = _source_lines(new_src)
    if _coding_cookie(old_src) == _coding_cookie(new_src):
        if _fast_tree_equiv(old_ast, new_ast, old_lines, new_lines, ignore_docstrings):
            return PyFileAnalysis(file=file, is_noop=True)
        if _is_sound_cmp_equiv(file, old_ast, new_ast, fp, pure_enums or _EMPTY_SET, strict_enum_attrs or _EMPTY_SET, ignore_docstrings=ignore_docstrings, old_lines=old_lines, new_lines=new_lines):
            return PyFileAnalysis(file=file, is_noop=True)
        if cmp_agree and _diff_has_identity_or_eq_change(fp) and _fast_tree_equiv(old_ast, new_ast, old_lines, new_lines, ignore_docstrings, cmp_site_ok=lambda line, col: f'{file}:{line}:{col}' in cmp_agree):
            return PyFileAnalysis(file=file, is_noop=True)
    units: list[ChangeUnit] = []
    changed_funcs: list[str] = []
    has_future_ann = _has_future_annotations(old_ast) and _has_future_annotations(new_ast)
    _compare_block(file, old_ast.body, new_ast.body, old_lines if old_lines is not None else old_src.splitlines(), new_lines if new_lines is not None else new_src.splitlines(), [], changed_old, anchor_old, cf_old, units, changed_funcs, ignore_docstrings, has_future_ann)
    if not units:
        if not file.startswith('sqlglot/typing/') and _coding_cookie(old_src) == _coding_cookie(new_src) and _fast_tree_equiv(old_ast, new_ast, old_lines, new_lines, ignore_docstrings, ignore_type_checking=True):
            return PyFileAnalysis(file=file, is_noop=True)
        units.append(ChangeUnit(kind='module_other', file=file, symbol_name='*', old_lines=changed_old))
    return PyFileAnalysis(file=file, is_noop=False, units=units, all_changed_old_lines=changed_old | anchor_old, changed_funcs=changed_funcs)

def diff_sql_fixture_pairs(old_text: str, new_text: str) -> tuple[bool, list[int]]:

    def _parse(text: str) -> list[tuple[dict[str, str], str, str]]:
        filtered = '\n'.join((line for line in text.splitlines() if line and (not line.startswith('--'))))
        stmts = filtered.split(';')
        out = []
        for i in range(0, len(stmts), 2):
            if i + 1 < len(stmts):
                sql = stmts[i].strip()
                meta: dict[str, str] = {}
                sql_lines = sql.split('\n')
                j = 0
                while j < len(sql_lines) and sql_lines[j].startswith('#'):
                    key, val = sql_lines[j].split(':', maxsplit=1)
                    meta[key.lstrip('#').strip()] = val.strip()
                    j += 1
                sql = '\n'.join(sql_lines[j:])
                expected = stmts[i + 1].strip()
                out.append((meta, sql, expected))
        return out
    old_cases = _parse(old_text)
    try:
        new_cases = _parse(new_text)
    except Exception:
        return (True, [])
    if len(old_cases) != len(new_cases):
        return (True, [])
    changed = [idx for idx, (o, n) in enumerate(zip(old_cases, new_cases), start=1) if o != n]
    return (False, changed)

def diff_sql_fixture_lines(old_text: str, new_text: str) -> tuple[bool, list[int]]:

    def _parse(text: str) -> list[str]:
        filtered = '\n'.join((line for line in text.splitlines() if line and (not line.startswith('--'))))
        out = []
        buf: list[str] = []
        for line in filtered.splitlines():
            buf.append(line)
            if not line.startswith('#'):
                out.append('\n'.join(buf))
                buf.clear()
        return out
    old_cases = _parse(old_text)
    new_cases = _parse(new_text)
    if len(old_cases) != len(new_cases):
        return (True, [])
    changed = [idx for idx, (o, n) in enumerate(zip(old_cases, new_cases), start=1) if o != n]
    return (False, changed)
