"""Worker packing: split the chosen tests over at most 8 workers for the lowest busiest-worker cost."""
from __future__ import annotations
import math
from typing import Sequence
try:
    import _joinkit as _FJC
    _FJC_LB_COST = getattr(_FJC, 'lb_cost_ms', None)
    _FJC_LPT_COST = getattr(_FJC, 'lpt_best_cost', None)
except Exception:
    _FJC_LB_COST = None
    _FJC_LPT_COST = None
WORKER_COST_MS = 2000
MAX_WORKERS = 8

def duration_ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)

def _pigeonhole_lb(durs_desc: Sequence[int], total_ms: int, w: int) -> int:
    n = len(durs_desc)
    lb = durs_desc[0]
    avg = (total_ms + w - 1) // w
    if avg > lb:
        lb = avg
    max_q = (n - 1) // w
    if max_q > 4:
        max_q = 4
    for q in range(1, max_q + 1):
        end = q * w + 1
        ph = sum(durs_desc[end - q - 1:end])
        if ph > lb:
            lb = ph
    return lb

def _lpt_pack(items: list[tuple[int, int]], w: int) -> tuple[int, list[list[int]], list[int]]:
    workers: list[list[int]] = [[] for _ in range(w)]
    loads = [0] * w
    for num, dur in items:
        best_i = 0
        best_l = loads[0]
        for i in range(1, w):
            if loads[i] < best_l:
                best_l = loads[i]
                best_i = i
        workers[best_i].append(num)
        loads[best_i] = best_l + dur
    return (max(loads), workers, loads)

def _improve_pack(workers: list[list[int]], loads: list[int], dur_by_num: dict[int, int], lb: int) -> tuple[int, list[list[int]], list[int]]:
    w = len(workers)
    makespan = max(loads)
    if makespan <= lb:
        return (makespan, workers, loads)
    n_items = sum((len(wk) for wk in workers))
    if n_items > 100 and makespan <= lb + 2:
        return (makespan, workers, loads)
    max_iters = 8 if n_items > 80 else 64
    for _ in range(max_iters):
        hi = max(range(w), key=loads.__getitem__)
        makespan = loads[hi]
        if makespan <= lb:
            break
        improved = False
        hi_items = workers[hi]
        for idx_a, a in enumerate(hi_items):
            da = dur_by_num[a]
            if da == 0:
                continue
            for lo in range(w):
                if lo == hi:
                    continue
                if loads[lo] + da < makespan:
                    hi_items.pop(idx_a)
                    workers[lo].append(a)
                    loads[hi] -= da
                    loads[lo] += da
                    improved = True
                    break
            if improved:
                break
        if improved:
            continue
        for idx_a, a in enumerate(hi_items):
            da = dur_by_num[a]
            for lo in range(w):
                if lo == hi:
                    continue
                lo_items = workers[lo]
                slack = makespan - loads[lo]
                for idx_b, b in enumerate(lo_items):
                    db = dur_by_num[b]
                    diff = da - db
                    if 0 < diff < slack:
                        hi_items[idx_a] = b
                        lo_items[idx_b] = a
                        loads[hi] -= diff
                        loads[lo] += diff
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
        if improved:
            continue
        if n_items <= 80 and len(hi_items) <= 24:
            nh = len(hi_items)
            hi_durs = [dur_by_num[a] for a in hi_items]
            for i1 in range(nh):
                d1 = hi_durs[i1]
                if d1 == 0:
                    continue
                for i2 in range(i1 + 1, nh):
                    d12 = d1 + hi_durs[i2]
                    for lo in range(w):
                        if lo == hi:
                            continue
                        lo_items = workers[lo]
                        if len(lo_items) > 24:
                            continue
                        slack = makespan - loads[lo]
                        for idx_b, b in enumerate(lo_items):
                            diff = d12 - dur_by_num[b]
                            if 0 < diff < slack:
                                a2 = hi_items.pop(i2)
                                a1 = hi_items.pop(i1)
                                lo_items[idx_b] = a1
                                lo_items.append(a2)
                                hi_items.append(b)
                                loads[hi] -= diff
                                loads[lo] += diff
                                improved = True
                                break
                        if improved:
                            break
                    if improved:
                        break
                if improved:
                    break
            if improved:
                continue
            for idx_a, da in enumerate(hi_durs):
                if da == 0:
                    continue
                for lo in range(w):
                    if lo == hi:
                        continue
                    lo_items = workers[lo]
                    nl = len(lo_items)
                    if nl < 2 or nl > 24:
                        continue
                    slack = makespan - loads[lo]
                    lo_durs = [dur_by_num[b] for b in lo_items]
                    for j1 in range(nl):
                        db1 = lo_durs[j1]
                        for j2 in range(j1 + 1, nl):
                            diff = da - (db1 + lo_durs[j2])
                            if 0 < diff < slack:
                                a = hi_items[idx_a]
                                b2 = lo_items.pop(j2)
                                b1 = lo_items.pop(j1)
                                hi_items[idx_a] = b1
                                hi_items.append(b2)
                                lo_items.append(a)
                                loads[hi] -= diff
                                loads[lo] += diff
                                improved = True
                                break
                        if improved:
                            break
                    if improved:
                        break
                if improved:
                    break
        if not improved:
            break
    return (max(loads), workers, loads)

def _exact_w2_dp(items: list[tuple[int, int]], total_ms: int, ub: int, lb: int) -> tuple[int, list[list[int]]] | None:
    pos_items = [(num, dur) for num, dur in items if dur > 0]
    n = len(pos_items)
    half = total_ms // 2
    if n <= 2 or n > 250 or half > 200000:
        return None
    target_s = total_ms - lb
    if target_s < 0:
        target_s = 0
    mask = (1 << half + 1) - 1
    bits = 1
    dp: list[int] = [1]
    for _, d in pos_items:
        bits = (bits | bits << d) & mask
        dp.append(bits)
        if target_s <= half and bits >> target_s & 1:
            break
    best_s = target_s if target_s <= half and bits >> target_s & 1 else bits.bit_length() - 1
    opt_ms = total_ms - best_s
    if opt_ms >= ub:
        return None
    w0: list[int] = []
    w1: list[int] = []
    processed = len(dp) - 1
    for idx in range(processed, n):
        w1.append(pos_items[idx][0])
    rem = best_s
    for idx in range(processed - 1, -1, -1):
        num, d = pos_items[idx]
        if rem >= d and dp[idx] >> rem - d & 1:
            w0.append(num)
            rem -= d
        else:
            w1.append(num)
    for num, dur in items:
        if dur == 0:
            w0.append(num)
    return (opt_ms, [w0, w1])

def _exact_bnb(items: list[tuple[int, int]], w: int, ub: int, lb: int, node_limit: int=15000) -> tuple[int, list[list[int]]] | None:
    pos_items = [(num, dur) for num, dur in items if dur > 0]
    n = len(pos_items)
    if n <= w or n > 40:
        return None
    durs = [d for _, d in pos_items]
    best_ms = ub
    best_assign: list[int] | None = None
    loads = [0] * w
    assign = [0] * n
    nodes = 0
    last_idx = n - 1

    def dfs(idx: int, cur_max: int, prev_bin: int) -> None:
        nonlocal best_ms, best_assign, nodes
        nodes += 1
        if nodes > node_limit:
            return
        if cur_max >= best_ms:
            return
        dur = durs[idx]
        if idx == last_idx:
            min_b = 0
            min_l = loads[0]
            for b in range(1, w):
                if loads[b] < min_l:
                    min_l = loads[b]
                    min_b = b
            nl = min_l + dur
            cand_max = nl if nl > cur_max else cur_max
            if cand_max < best_ms:
                best_ms = cand_max
                assign[idx] = min_b
                best_assign = assign.copy()
            return
        start_b = prev_bin if idx > 0 and dur == durs[idx - 1] else 0
        seen_loads: set[int] = set()
        order = sorted(range(start_b, w), key=loads.__getitem__)
        for b in order:
            lb_bin = loads[b]
            if lb_bin in seen_loads:
                continue
            new_load = lb_bin + dur
            if new_load >= best_ms:
                continue
            seen_loads.add(lb_bin)
            loads[b] = new_load
            assign[idx] = b
            dfs(idx + 1, new_load if new_load > cur_max else cur_max, b)
            loads[b] = lb_bin
            if best_ms <= lb or nodes > node_limit:
                return
    dfs(0, 0, 0)
    if best_assign is None:
        return None
    workers: list[list[int]] = [[] for _ in range(w)]
    for (num, _), b in zip(pos_items, best_assign):
        workers[b].append(num)
    for num, dur in items:
        if dur == 0:
            workers[0].append(num)
    return (best_ms, workers)

def pack_workers(test_nums: Sequence[int], dur_by_num: dict[int, int]) -> tuple[int, list[list[int]]]:
    if not test_nums:
        return (0, [])
    items = sorted(((n, dur_by_num[n]) for n in test_nums), key=lambda x: (-x[1], x[0]))
    durs_desc = [d for _, d in items]
    total_ms = sum(durs_desc)
    max_d = durs_desc[0]
    n = len(items)
    best_cost = total_ms + WORKER_COST_MS
    best_workers: list[list[int]] = [[num for num, _ in items]]
    max_w = min(MAX_WORKERS, n)
    for w in range(2, max_w + 1):
        lb = _pigeonhole_lb(durs_desc, total_ms, w)
        if lb + WORKER_COST_MS * w >= best_cost:
            if (total_ms + w - 1) // w <= max_d:
                break
            continue
        ms, workers, loads = _lpt_pack(items, w)
        if ms > lb:
            ms, workers, loads = _improve_pack(workers, loads, dur_by_num, lb)
        if ms > lb:
            ub_need = min(ms, best_cost - WORKER_COST_MS * w)
            if ub_need > lb:
                if w == 2 and n <= 250 and (total_ms <= 400000):
                    res = _exact_w2_dp(items, total_ms, ub_need, lb)
                else:
                    res = _exact_bnb(items, w, ub_need, lb)
                if res is not None and res[0] < ms:
                    ms, workers = res
        cost = ms + WORKER_COST_MS * w
        if cost < best_cost:
            best_cost = cost
            best_workers = [wk for wk in workers if wk]
            if ms <= max_d:
                break
    return (best_cost, best_workers)

def optimal_cost_ms(test_nums: Sequence[int] | set[int], dur_by_num: dict[int, int]) -> int:
    if not test_nums:
        return 0
    if len(test_nums) == 1:
        return dur_by_num[next(iter(test_nums))] + WORKER_COST_MS
    cost, _ = pack_workers(list(test_nums), dur_by_num)
    return cost

def max_total_for_cost_ms(cost_ms: int, min_makespan_ms: int) -> int:
    best = 0
    for w in range(1, MAX_WORKERS + 1):
        rem_ms = cost_ms - WORKER_COST_MS * w
        if rem_ms >= min_makespan_ms:
            cap = w * rem_ms
            if cap > best:
                best = cap
    return best

def fast_cost_ms(test_nums: Sequence[int] | set[int], dur_by_num: dict[int, int]) -> int:
    n = len(test_nums)
    if n == 0:
        return 0
    if n == 1:
        return dur_by_num[next(iter(test_nums))] + WORKER_COST_MS
    if n <= 40:
        cost, _ = pack_workers(list(test_nums), dur_by_num)
        return cost
    durs = sorted((dur_by_num[t] for t in test_nums), reverse=True)
    total_ms = sum(durs)
    if _FJC_LPT_COST is not None:
        return _FJC_LPT_COST(durs, total_ms)
    max_d = durs[0]
    best_cost = total_ms + WORKER_COST_MS
    max_w = min(MAX_WORKERS, n)
    for w in range(2, max_w + 1):
        lb = _pigeonhole_lb(durs, total_ms, w)
        if lb + WORKER_COST_MS * w >= best_cost:
            if (total_ms + w - 1) // w <= max_d:
                break
            continue
        loads = [0] * w
        for d in durs:
            bi = 0
            bl = loads[0]
            for i in range(1, w):
                if loads[i] < bl:
                    bl = loads[i]
                    bi = i
            loads[bi] = bl + d
        ms = max(loads)
        cost = ms + WORKER_COST_MS * w
        if cost < best_cost:
            best_cost = cost
            if ms <= max_d:
                break
    return best_cost

def _lb_cost_ms_py(total_ms: float, max_d: float) -> float:
    best = total_ms + WORKER_COST_MS
    if max_d > total_ms:
        best = max_d + WORKER_COST_MS
    for w in range(2, MAX_WORKERS + 1):
        m = total_ms / w
        if m < max_d:
            m = max_d
        c = m + WORKER_COST_MS * w
        if c < best:
            best = c
        elif m <= max_d:
            break
    return best
lb_cost_ms = _FJC_LB_COST if _FJC_LB_COST is not None else _lb_cost_ms_py
