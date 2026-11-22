# Blast Radius selector

Test selection for sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`: for each patch in
`/data/commits`, print one JSON line with the tests to run, packed onto at most 8 workers.

## How it works

1. **Read the patch.** Each diff is applied in memory and classified: comment-only and
   AST-equal edits, function bodies, class and module-level code, fixtures, test files.
2. **Find candidates.** Maps built from the pinned commit (line coverage, attribute and
   dict-key reads, transform tables, fixture reads, subtests) give the tests each change can reach.
   A change no test can reach runs nothing.
3. **Run them.** Candidates run in forked children on the patched tree. When a patch leaves
   sqlglot's compiled core untouched, they run on a C build of that core (mypyc), which is several
   times faster. Recorded optimizer stages, executor results and DuckDB/SQLite answers are reused
   wherever the patch cannot change them. A commit stops early once its result is known.
4. **Report.** Every test that failed, plus every test that could not be checked in time, is
   reported. A test left unchecked costs time but can never be a miss.
5. **Guard shared state.** A test run on its own cannot see a later test broken by state the
   patch leaves behind. When a patch makes sqlglot write shared state (a class setting, a module
   table, an object that outlives a test) or drops a copy, every candidate is reported, and for
   dialect code every dialect test too (`state_guard.py`).
6. **Pack.** The chosen tests are split over up to 8 workers for the lowest busiest-worker cost.

## Build

`docker build --platform linux/amd64 .` compiles the three C variants and precomputes the caches
(`build_sqlglotc.py`, `build_caches.py`). Nothing generated is stored in git.

`make sample` runs the image on the 30 sample commits under the scorer's limits and scores it.
