"""Image build step: compile sqlglot's core modules to C with mypyc in three variants."""
from __future__ import annotations
import ast
import concurrent.futures
import heapq
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path
ULTRA_HOT_MODS: frozenset[str] = frozenset({'sqlglot.tokenizer_core', 'sqlglot.trie', 'sqlglot.helper', 'sqlglot.time', 'sqlglot.errors', 'sqlglot.executor.table', 'sqlglot.expressions.datatypes'})
HOT_MODS: frozenset[str] = frozenset({'sqlglot.parser', 'sqlglot.generator', 'sqlglot.tokenizer_core', 'sqlglot.trie', 'sqlglot.helper', 'sqlglot.time', 'sqlglot.errors', 'sqlglot.transforms', 'sqlglot.expressions.core', 'sqlglot.expressions.query', 'sqlglot.expressions.datatypes', 'sqlglot.executor.table', 'sqlglot.executor.env', 'sqlglot.executor.context', 'sqlglot.generators.python', 'sqlglot.optimizer.scope', 'sqlglot.optimizer.resolver', 'sqlglot.optimizer.isolate_table_selects', 'sqlglot.optimizer.normalize_identifiers', 'sqlglot.optimizer.qualify', 'sqlglot.optimizer.qualify_tables', 'sqlglot.optimizer.qualify_columns', 'sqlglot.optimizer.simplify', 'sqlglot.optimizer.annotate_types', 'sqlglot.optimizer.canonicalize_internal_names', 'sqlglot.optimizer.pushdown_predicates', 'sqlglot.optimizer.unnest_subqueries', 'sqlglot.optimizer.eliminate_subqueries', 'sqlglot.optimizer.eliminate_joins', 'sqlglot.optimizer.eliminate_ctes', 'sqlglot.optimizer.optimize_joins', 'sqlglot.optimizer.pushdown_projections', 'sqlglot.optimizer.canonicalize', 'sqlglot.optimizer.normalize'})

def _subpkg_files(sg_dir: Path, sub: str, files: list[str] | None=None) -> list[str]:
    if files is None:
        files = sorted((f for f in os.listdir(sg_dir / sub) if f.endswith('.py') and f != '__init__.py'))
    return [f'sqlglot/{sub}/{f}' for f in files]

def _patch_for_interpreted_subclasses(path: Path, class_name: str, fix_i64: bool=False) -> None:
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    cls_node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name))
    lines = src.splitlines(keepends=True)
    replacements: list[tuple[int, str]] = []
    n_cv = 0
    for stmt in cls_node.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            name = stmt.targets[0].id
            if name == '__slots__':
                continue
            ln = stmt.lineno - 1
            orig = lines[ln]
            idx = orig.find(name)
            eq_idx = orig.find('=', idx + len(name))
            if eq_idx > idx:
                replacements.append((ln, orig[:idx + len(name)] + ': t.ClassVar[t.Any]' + orig[idx + len(name):]))
                n_cv += 1
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if name == '__slots__':
                continue
            if 'ClassVar' in ast.unparse(stmt.annotation):
                continue
            ln = stmt.lineno - 1
            orig = lines[ln]
            idx = orig.find(name)
            col_idx = orig.find(':', idx + len(name))
            eq_idx = orig.find('=', col_idx + 1)
            if col_idx > idx and eq_idx > col_idx:
                ann_raw = orig[col_idx + 1:eq_idx].strip()
                replacements.append((ln, orig[:col_idx + 1] + f' t.ClassVar[{ann_raw}] ' + orig[eq_idx:]))
                n_cv += 1
    for ln, new_line in replacements:
        lines[ln] = new_line
    new_src = ''.join(lines)
    assert 'from __future__ import annotations\n' in new_src
    new_src = new_src.replace('from __future__ import annotations\n', 'from __future__ import annotations\nfrom mypy_extensions import mypyc_attr as _mypyc_attr\n', 1)
    new_src = new_src.replace(f'class {class_name}:', f'@_mypyc_attr(allow_interpreted_subclasses=True)\nclass {class_name}:', 1)
    if fix_i64:
        assert 'def _advance(self, times: i64 = 1) -> None:' in new_src
        new_src = new_src.replace('def _advance(self, times: i64 = 1) -> None:', 'def _advance(self, times: int = 1) -> None:', 1)
        new_src = new_src.replace('def _retreat(self, index: i64) -> None:', 'def _retreat(self, index: int) -> None:', 1)
    path.write_text(new_src, encoding='utf-8')
    print(f'[sqlglotc] patched {path.name}:{class_name} ({n_cv} ClassVars)', flush=True)

def _select_sources(sg_dir: Path, mode: str) -> tuple[list[str], list[str]]:
    extra_mypy_flags: list[str] = []
    if mode == 'no_dialects':
        _patch_for_interpreted_subclasses(sg_dir / 'parser.py', 'Parser', fix_i64=True)
        _patch_for_interpreted_subclasses(sg_dir / 'generator.py', 'Generator', fix_i64=False)
        extra_mypy_flags = ['--disable-error-code=misc']
        source_files = ['sqlglot/errors.py', 'sqlglot/generator.py', 'sqlglot/helper.py', 'sqlglot/parser.py', 'sqlglot/time.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', *_subpkg_files(sg_dir, 'expressions'), *_subpkg_files(sg_dir, 'optimizer', ['scope.py', 'resolver.py', 'isolate_table_selects.py', 'normalize_identifiers.py', 'qualify.py', 'qualify_tables.py', 'qualify_columns.py', 'simplify.py', 'annotate_types.py', 'canonicalize_internal_names.py']), *_subpkg_files(sg_dir, 'executor', ['table.py'])]
    elif mode == 'no_dialects_no_opt':
        _patch_for_interpreted_subclasses(sg_dir / 'parser.py', 'Parser', fix_i64=True)
        _patch_for_interpreted_subclasses(sg_dir / 'generator.py', 'Generator', fix_i64=False)
        extra_mypy_flags = ['--disable-error-code=misc']
        source_files = ['sqlglot/errors.py', 'sqlglot/generator.py', 'sqlglot/helper.py', 'sqlglot/parser.py', 'sqlglot/time.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', *_subpkg_files(sg_dir, 'expressions'), *_subpkg_files(sg_dir, 'executor', ['table.py'])]
    elif mode == 'base_notable':
        source_files = ['sqlglot/errors.py', 'sqlglot/helper.py', 'sqlglot/time.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', *_subpkg_files(sg_dir, 'expressions')]
    elif mode == 'base':
        source_files = ['sqlglot/errors.py', 'sqlglot/helper.py', 'sqlglot/time.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', *_subpkg_files(sg_dir, 'expressions'), *_subpkg_files(sg_dir, 'executor', ['table.py'])]
    else:
        source_files = ['sqlglot/errors.py', 'sqlglot/generator.py', 'sqlglot/helper.py', 'sqlglot/parser.py', 'sqlglot/time.py', 'sqlglot/tokenizer_core.py', 'sqlglot/trie.py', *_subpkg_files(sg_dir, 'expressions'), *_subpkg_files(sg_dir, 'generators'), *_subpkg_files(sg_dir, 'optimizer', ['scope.py', 'resolver.py', 'isolate_table_selects.py', 'normalize_identifiers.py', 'qualify.py', 'qualify_tables.py', 'qualify_columns.py', 'simplify.py', 'annotate_types.py', 'canonicalize_internal_names.py']), *_subpkg_files(sg_dir, 'parsers'), *_subpkg_files(sg_dir, 'executor', ['table.py'])]
    return (extra_mypy_flags, source_files)

def _codegen_jobs(root: Path, mode: str) -> list[dict]:
    from mypyc.build import mypycify
    os.chdir(root)
    os.environ['MYPYPATH'] = str(root)
    sg_dir = root / 'sqlglot'
    extra_mypy_flags, source_files = _select_sources(sg_dir, mode)
    t0 = time.time()
    exts = mypycify([*extra_mypy_flags, *source_files], opt_level='1', debug_level='0', separate=True, verbose=False)
    t_gen = time.time() - t0
    print(f'[sqlglotc:{mode}] mypyc codegen ({len(source_files)} files -> {len(exts)} exts): {t_gen:.1f}s', flush=True)
    py_inc = sysconfig.get_path('include')
    ext_suffix = sysconfig.get_config_var('EXT_SUFFIX') or '.so'
    obj_root = root / 'build' / 'obj'
    obj_root.mkdir(parents=True, exist_ok=True)
    jobs: list[dict] = []
    for ext in exts:
        base = ext.name.removesuffix('__mypyc')
        if base in ULTRA_HOT_MODS:
            opt_tier = 2
        elif base in HOT_MODS or '.' not in ext.name:
            opt_tier = 1
        else:
            opt_tier = 0
        parts = ext.name.split('.')
        if parts[0] == 'sqlglot':
            dst_so = str(sg_dir.joinpath(*parts[1:-1], parts[-1] + ext_suffix))
        else:
            dst_so = str(sg_dir / (ext.name + ext_suffix))
        sources = [str((root / s).resolve()) for s in ext.sources]
        incs = [py_inc, *(str((root / d).resolve()) for d in ext.include_dirs)]
        total_sz = sum((os.path.getsize(s) for s in sources if os.path.exists(s)))
        jobs.append({'size': total_sz, 'root': str(root), 'mode': mode, 'name': ext.name, 'sources': sources, 'incs': incs, 'obj_root': str(obj_root), 'dst_so': dst_so, 'opt_tier': opt_tier})
    return jobs

def _compiler_config() -> tuple[str, list[str], list[str]]:
    cc = shutil.which('clang') or 'gcc'
    is_clang = 'clang' in os.path.basename(cc)
    base_flags = ['-fPIC', '-g0', '-fno-strict-overflow', '-fno-semantic-interposition', '-fno-asynchronous-unwind-tables', '-DNDEBUG']
    if is_clang:
        base_flags.append('-Wno-everything')
    else:
        base_flags.extend(['-Wno-unused-function', '-Wno-unused-label', '-Wno-unreachable-code', '-Wno-unused-variable', '-Wno-unused-but-set-variable', '-Wno-array-bounds', '-Wno-cpp'])
    link_flags = ['-Wl,-s'] if sys.platform.startswith('linux') else []
    return (cc, base_flags, link_flags)

def _compile_one(job: dict, cc: str, base_flags: list[str], link_flags: list[str]) -> str:
    name = job['name']
    opt_tier = job['opt_tier']
    opt_flag = '-O2' if opt_tier == 2 else '-O1' if opt_tier == 1 else '-O0'
    inc_flags = [f'-I{d}' for d in job['incs']]
    obj_root = Path(job['obj_root'])
    dst_so = job['dst_so']
    objs: list[str] = []
    for idx, src in enumerate(job['sources']):
        obj = str(obj_root / f'{name}_{idx}.o')
        cmd = [cc, *base_flags, opt_flag, *inc_flags, '-c', src, '-o', obj]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Compile failed for {job['mode']}:{name} ({src}):\n{res.stderr}")
        objs.append(obj)
    os.makedirs(os.path.dirname(dst_so), exist_ok=True)
    link_cmd = [cc, '-shared', *link_flags, *objs, '-o', dst_so]
    res = subprocess.run(link_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"Link failed for {job['mode']}:{name}:\n{res.stderr}")
    return name

def _cleanup_root(root: Path) -> None:
    shutil.rmtree(root / 'build', ignore_errors=True)
    shutil.rmtree(root / '.mypy_cache', ignore_errors=True)
    for egg in root.glob('*.egg-info'):
        shutil.rmtree(egg, ignore_errors=True)

def _build_single(root: Path, mode: str) -> None:
    t0 = time.time()
    jobs = _codegen_jobs(root, mode)
    jobs.sort(key=lambda j: -j['size'])
    cc, base_flags, link_flags = _compiler_config()
    n_workers = max(1, os.cpu_count() or 4)
    n_o2 = sum((1 for j in jobs if j['opt_tier'] == 2))
    n_o1 = sum((1 for j in jobs if j['opt_tier'] == 1))
    n_o0 = len(jobs) - n_o2 - n_o1
    print(f'[sqlglotc:{mode}] compiling {len(jobs)} exts ({n_o2} -O2, {n_o1} -O1, {n_o0} -O0) with {cc} -j{n_workers}', flush=True)
    t_cc0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(_compile_one, j, cc, base_flags, link_flags) for j in jobs]
        for fut in concurrent.futures.as_completed(futs):
            fut.result()
    t_cc = time.time() - t_cc0
    _cleanup_root(root)
    print(f'[sqlglotc:{mode}] DONE in {time.time() - t0:.1f}s (parallel_cc={t_cc:.1f}s, workers={n_workers})', flush=True)

def _build_multi(targets: list[tuple[Path, str]]) -> None:
    t0 = time.time()
    cc, base_flags, link_flags = _compiler_config()
    n_workers = max(1, os.cpu_count() or 4)
    script_path = str(Path(__file__).resolve())
    cond = threading.Condition()
    pq: list[tuple[int, int, dict]] = []
    seq = 0
    active_codegens = len(targets)
    active_cc = 0
    remaining_by_root: dict[str, int] = {}
    errors: list[BaseException] = []
    cg_sem = threading.Semaphore(max(1, n_workers))
    total_jobs = 0

    def _run_codegen(root: Path, mode: str) -> None:
        nonlocal active_codegens, seq, total_jobs
        manifest = root / 'build' / f'cc_jobs_{mode}.json'
        try:
            with cg_sem:
                manifest.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run([sys.executable, script_path, '--codegen-only', str(root), mode, str(manifest)], check=True)
            jobs: list[dict] = json.loads(manifest.read_text(encoding='utf-8'))
            with cond:
                if not jobs:
                    _cleanup_root(root)
                else:
                    remaining_by_root[str(root)] = len(jobs)
                    total_jobs += len(jobs)
                    for j in jobs:
                        seq += 1
                        heapq.heappush(pq, (-j['size'], seq, j))
                active_codegens -= 1
                cond.notify_all()
        except BaseException as exc:
            with cond:
                errors.append(exc)
                active_codegens -= 1
                cond.notify_all()

    def _cc_worker() -> None:
        nonlocal active_cc
        while True:
            with cond:
                while True:
                    if errors:
                        return
                    allowed_cc = max(1, n_workers - active_codegens)
                    if pq and active_cc < allowed_cc:
                        _, _, job = heapq.heappop(pq)
                        active_cc += 1
                        break
                    if not pq and active_codegens == 0:
                        return
                    cond.wait()
            try:
                _compile_one(job, cc, base_flags, link_flags)
            except BaseException as exc:
                with cond:
                    errors.append(exc)
                    active_cc -= 1
                    cond.notify_all()
                return
            with cond:
                active_cc -= 1
                rkey = job['root']
                remaining_by_root[rkey] -= 1
                done_root = remaining_by_root[rkey] == 0
                cond.notify_all()
            if done_root:
                _cleanup_root(Path(rkey))
    cg_threads = [threading.Thread(target=_run_codegen, args=(r, m)) for r, m in targets]
    cc_threads = [threading.Thread(target=_cc_worker) for _ in range(n_workers)]
    for th in cg_threads:
        th.start()
    for th in cc_threads:
        th.start()
    for th in cg_threads:
        th.join()
    for th in cc_threads:
        th.join()
    if errors:
        raise errors[0]
    print(f'[sqlglotc:multi] DONE {len(targets)} roots ({total_jobs} exts) in {time.time() - t0:.1f}s with {cc} -j{n_workers}', flush=True)

def main() -> None:
    if len(sys.argv) >= 5 and sys.argv[1] == '--codegen-only':
        root = Path(sys.argv[2])
        mode = sys.argv[3]
        manifest = Path(sys.argv[4])
        jobs = _codegen_jobs(root, mode)
        manifest.write_text(json.dumps(jobs), encoding='utf-8')
        return
    if len(sys.argv) >= 3 and sys.argv[1] == '--multi':
        targets: list[tuple[Path, str]] = []
        for spec in sys.argv[2:]:
            r_str, mode = spec.rsplit(':', 1)
            targets.append((Path(r_str), mode))
        _build_multi(targets)
        return
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/sqlglot-c')
    mode = sys.argv[2] if len(sys.argv) > 2 else 'no_dialects'
    _build_single(root, mode)
if __name__ == '__main__':
    main()
