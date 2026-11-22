# syntax=docker/dockerfile:1
# Two-stage linux/amd64 image on Python 3.13.
# Build stage: fetch sqlglot at the pinned commit and compile three C variants of its core with mypyc
#   /sqlglot-c        core, expressions, table and the hot optimizer passes
#   /sqlglot-c-noopt  core, expressions and table (optimizer left in Python)
#   /sqlglot-c-base   core and expressions only
#   /sqlglot          the plain Python tree, for commits that touch compiled modules
# Run stage: the selector, its maps and the caches precomputed below.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends clang libc6-dev > /dev/null && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "mypy>=2.4.0" \
    mypy_extensions==1.2.0 \
    setuptools==84.2.1 \
    typing_extensions==4.16.0 \
    types-python-dateutil \
    types-pytz

# sqlglot at the pinned commit, from its own repository
RUN python -c "import tarfile, urllib.request, io; \
    url = 'https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz'; \
    data = urllib.request.urlopen(url, timeout=120).read(); \
    tf = tarfile.open(fileobj=io.BytesIO(data), mode='r:gz'); \
    tf.extractall('/opt')" && \
    mv /opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa /sqlglot && \
    rm -rf /sqlglot/sqlglotrs /sqlglot/docs /sqlglot/posts /sqlglot/benchmarks /sqlglot/.github && \
    cp -a /sqlglot /sqlglot-c && \
    cp -a /sqlglot /sqlglot-c-noopt && \
    cp -a /sqlglot /sqlglot-c-base

COPY compile_core.py /tmp/compile_core.py
RUN python /tmp/compile_core.py --multi \
        /sqlglot-c:no_dialects \
        /sqlglot-c-noopt:no_dialects_no_opt \
        /sqlglot-c-base:base_notable && \
    rm -f /tmp/compile_core.py

COPY _joinkit.c /tmp/_joinkit.c
RUN PY_INC=$(python -c "import sysconfig; print(sysconfig.get_path('include'))") && \
    EXT=$(python -c "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX') or '.so')") && \
    clang -O3 -g0 -fno-semantic-interposition -fno-asynchronous-unwind-tables -fPIC -shared -Wl,-s -I"$PY_INC" /tmp/_joinkit.c -o "/opt/_joinkit${EXT}" && \
    rm -f /tmp/_joinkit.c

# Run stage: no compiler, no mypy
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    SKIP_INTEGRATION=0 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir --no-deps \
    python-dateutil==4.0.0 \
    mypy_extensions==1.2.0 \
    pytz==2026.6 \
    tzdata==2026.6 \
    setuptools==84.2.1 \
    typing_extensions==4.16.0 \
    numpy==2.5.5 \
    pandas==3.1.1 \
    duckdb==2.0.0

# Bytecode for the standard library, so no run pays to compile it
RUN python -m compileall -q -j 0 /usr/local/lib/python3.13 > /dev/null || true

COPY --from=builder /sqlglot /sqlglot
COPY --from=builder /sqlglot-c /sqlglot-c
COPY --from=builder /sqlglot-c-noopt /sqlglot-c-noopt
COPY --from=builder /sqlglot-c-base /sqlglot-c-base

WORKDIR /app
COPY --from=builder /opt/_joinkit*.so /app/
COPY worker_pack.py diff_scan.py attr_deps.py reach_map.py stage_store.py verifier.py main.py shared_state.py prebuild.py /app/
COPY maps /app/maps

# Caches, dependency index and bytecode, all built from the pinned commit
RUN python /app/prebuild.py && \
    python -m compileall -q -j 0 /sqlglot/sqlglot /sqlglot/tests /sqlglot-c/sqlglot /sqlglot-c/tests /sqlglot-c-noopt/sqlglot /sqlglot-c-noopt/tests /sqlglot-c-base/sqlglot /sqlglot-c-base/tests /app && \
    chmod -R a+rX /app /sqlglot /sqlglot-c /sqlglot-c-noopt /sqlglot-c-base

CMD ["python", "/app/main.py"]
