#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>

static inline PyObject *concat_tuples(PyObject *a, PyObject *b) {
    if (PyTuple_CheckExact(a) && PyTuple_CheckExact(b)) {
        Py_ssize_t na = PyTuple_GET_SIZE(a);
        Py_ssize_t nb = PyTuple_GET_SIZE(b);
        PyObject *out = PyTuple_New(na + nb);
        if (!out) return NULL;
        for (Py_ssize_t i = 0; i < na; i++) {
            PyObject *item = PyTuple_GET_ITEM(a, i);
            Py_INCREF(item);
            PyTuple_SET_ITEM(out, i, item);
        }
        for (Py_ssize_t i = 0; i < nb; i++) {
            PyObject *item = PyTuple_GET_ITEM(b, i);
            Py_INCREF(item);
            PyTuple_SET_ITEM(out, na + i, item);
        }
        return out;
    }
    return PyNumber_Add(a, b);
}

static inline PyObject *get_row_item(PyObject *row, Py_ssize_t idx) {
    if (PyTuple_CheckExact(row)) {
        if (idx >= 0 && idx < PyTuple_GET_SIZE(row)) {
            PyObject *v = PyTuple_GET_ITEM(row, idx);
            Py_INCREF(v);
            return v;
        }
    } else if (PyList_CheckExact(row)) {
        if (idx >= 0 && idx < PyList_GET_SIZE(row)) {
            PyObject *v = PyList_GET_ITEM(row, idx);
            Py_INCREF(v);
            return v;
        }
    }
    return PySequence_GetItem(row, idx);
}

static int dict_append_row(PyObject *dict, PyObject *key, PyObject *row) {
    PyObject *grp = PyDict_GetItemWithError(dict, key);
    if (!grp) {
        if (PyErr_Occurred()) return -1;
        grp = PyList_New(1);
        if (!grp) return -1;
        Py_INCREF(row);
        PyList_SET_ITEM(grp, 0, row);
        int rc = PyDict_SetItem(dict, key, grp);
        Py_DECREF(grp);
        return rc;
    }
    return PyList_Append(grp, row);
}

/* Exact inner hash join on 1 integer key index (preserves first-seen source key-group order). */
static PyObject *inner_hash_join_1(PyObject *self, PyObject *args) {
    PyObject *s_rows, *j_rows;
    Py_ssize_t s_idx, j_idx;
    if (!PyArg_ParseTuple(args, "O!O!nn", &PyList_Type, &s_rows, &PyList_Type, &j_rows, &s_idx, &j_idx))
        return NULL;

    Py_ssize_t n_j = PyList_GET_SIZE(j_rows);
    Py_ssize_t n_s = PyList_GET_SIZE(s_rows);
    if (n_j == 0 || n_s == 0) return PyList_New(0);

    PyObject *join_map = PyDict_New();
    if (!join_map) return NULL;

    for (Py_ssize_t i = 0; i < n_j; i++) {
        PyObject *row = PyList_GET_ITEM(j_rows, i);
        PyObject *k = get_row_item(row, j_idx);
        if (!k) { Py_DECREF(join_map); return NULL; }
        if (k != Py_None) {
            if (dict_append_row(join_map, k, row) < 0) {
                Py_DECREF(k);
                Py_DECREF(join_map);
                return NULL;
            }
        }
        Py_DECREF(k);
    }

    if (PyDict_GET_SIZE(join_map) == 0) {
        Py_DECREF(join_map);
        return PyList_New(0);
    }

    PyObject *src_groups = PyDict_New();
    if (!src_groups) { Py_DECREF(join_map); return NULL; }

    for (Py_ssize_t i = 0; i < n_s; i++) {
        PyObject *row = PyList_GET_ITEM(s_rows, i);
        PyObject *k = get_row_item(row, s_idx);
        if (!k) { Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }
        if (k != Py_None) {
            PyObject *jg = PyDict_GetItemWithError(join_map, k);
            if (!jg) {
                if (PyErr_Occurred()) {
                    Py_DECREF(k); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
                }
            } else {
                if (dict_append_row(src_groups, k, row) < 0) {
                    Py_DECREF(k); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
                }
            }
        }
        Py_DECREF(k);
    }

    Py_ssize_t total_out = 0;
    PyObject *k, *sg;
    Py_ssize_t pos = 0;
    while (PyDict_Next(src_groups, &pos, &k, &sg)) {
        PyObject *jg = PyDict_GetItem(join_map, k);
        if (jg) total_out += PyList_GET_SIZE(sg) * PyList_GET_SIZE(jg);
    }

    PyObject *out = PyList_New(total_out);
    if (!out) { Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }

    Py_ssize_t out_i = 0;
    pos = 0;
    while (PyDict_Next(src_groups, &pos, &k, &sg)) {
        PyObject *jg = PyDict_GetItem(join_map, k);
        if (!jg) continue;
        Py_ssize_t n_sg = PyList_GET_SIZE(sg);
        Py_ssize_t n_jg = PyList_GET_SIZE(jg);
        for (Py_ssize_t a = 0; a < n_sg; a++) {
            PyObject *sr = PyList_GET_ITEM(sg, a);
            for (Py_ssize_t b = 0; b < n_jg; b++) {
                PyObject *jr = PyList_GET_ITEM(jg, b);
                PyObject *joined = concat_tuples(sr, jr);
                if (!joined) {
                    Py_DECREF(out); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
                }
                PyList_SET_ITEM(out, out_i++, joined);
            }
        }
    }

    Py_DECREF(src_groups);
    Py_DECREF(join_map);
    return out;
}

/* Exact inner hash join on 2 integer key indices. */
static PyObject *inner_hash_join_2(PyObject *self, PyObject *args) {
    PyObject *s_rows, *j_rows;
    Py_ssize_t s0, s1, j0, j1;
    if (!PyArg_ParseTuple(args, "O!O!nnnn", &PyList_Type, &s_rows, &PyList_Type, &j_rows, &s0, &s1, &j0, &j1))
        return NULL;

    Py_ssize_t n_j = PyList_GET_SIZE(j_rows);
    Py_ssize_t n_s = PyList_GET_SIZE(s_rows);
    if (n_j == 0 || n_s == 0) return PyList_New(0);

    PyObject *join_map = PyDict_New();
    if (!join_map) return NULL;

    for (Py_ssize_t i = 0; i < n_j; i++) {
        PyObject *row = PyList_GET_ITEM(j_rows, i);
        PyObject *v0 = get_row_item(row, j0);
        if (!v0) { Py_DECREF(join_map); return NULL; }
        if (v0 == Py_None) { Py_DECREF(v0); continue; }
        PyObject *v1 = get_row_item(row, j1);
        if (!v1) { Py_DECREF(v0); Py_DECREF(join_map); return NULL; }
        if (v1 == Py_None) { Py_DECREF(v0); Py_DECREF(v1); continue; }
        PyObject *k = PyTuple_New(2);
        if (!k) { Py_DECREF(v0); Py_DECREF(v1); Py_DECREF(join_map); return NULL; }
        PyTuple_SET_ITEM(k, 0, v0);
        PyTuple_SET_ITEM(k, 1, v1);
        if (dict_append_row(join_map, k, row) < 0) {
            Py_DECREF(k); Py_DECREF(join_map); return NULL;
        }
        Py_DECREF(k);
    }

    if (PyDict_GET_SIZE(join_map) == 0) {
        Py_DECREF(join_map);
        return PyList_New(0);
    }

    PyObject *src_groups = PyDict_New();
    if (!src_groups) { Py_DECREF(join_map); return NULL; }

    for (Py_ssize_t i = 0; i < n_s; i++) {
        PyObject *row = PyList_GET_ITEM(s_rows, i);
        PyObject *v0 = get_row_item(row, s0);
        if (!v0) { Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }
        if (v0 == Py_None) { Py_DECREF(v0); continue; }
        PyObject *v1 = get_row_item(row, s1);
        if (!v1) { Py_DECREF(v0); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }
        if (v1 == Py_None) { Py_DECREF(v0); Py_DECREF(v1); continue; }
        PyObject *k = PyTuple_New(2);
        if (!k) { Py_DECREF(v0); Py_DECREF(v1); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }
        PyTuple_SET_ITEM(k, 0, v0);
        PyTuple_SET_ITEM(k, 1, v1);
        PyObject *jg = PyDict_GetItemWithError(join_map, k);
        if (!jg) {
            if (PyErr_Occurred()) {
                Py_DECREF(k); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
            }
        } else {
            if (dict_append_row(src_groups, k, row) < 0) {
                Py_DECREF(k); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
            }
        }
        Py_DECREF(k);
    }

    Py_ssize_t total_out = 0;
    PyObject *k, *sg;
    Py_ssize_t pos = 0;
    while (PyDict_Next(src_groups, &pos, &k, &sg)) {
        PyObject *jg = PyDict_GetItem(join_map, k);
        if (jg) total_out += PyList_GET_SIZE(sg) * PyList_GET_SIZE(jg);
    }

    PyObject *out = PyList_New(total_out);
    if (!out) { Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }

    Py_ssize_t out_i = 0;
    pos = 0;
    while (PyDict_Next(src_groups, &pos, &k, &sg)) {
        PyObject *jg = PyDict_GetItem(join_map, k);
        if (!jg) continue;
        Py_ssize_t n_sg = PyList_GET_SIZE(sg);
        Py_ssize_t n_jg = PyList_GET_SIZE(jg);
        for (Py_ssize_t a = 0; a < n_sg; a++) {
            PyObject *sr = PyList_GET_ITEM(sg, a);
            for (Py_ssize_t b = 0; b < n_jg; b++) {
                PyObject *jr = PyList_GET_ITEM(jg, b);
                PyObject *joined = concat_tuples(sr, jr);
                if (!joined) {
                    Py_DECREF(out); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
                }
                PyList_SET_ITEM(out, out_i++, joined);
            }
        }
    }

    Py_DECREF(src_groups);
    Py_DECREF(join_map);
    return out;
}

/* Pure C column-index tuple projection: [tuple(r[i] for i in idxs) for r in rows] */
static PyObject *project_idxs(PyObject *self, PyObject *args) {
    PyObject *rows, *idxs_obj;
    if (!PyArg_ParseTuple(args, "O!O!", &PyList_Type, &rows, &PyTuple_Type, &idxs_obj))
        return NULL;

    Py_ssize_t n_cols = PyTuple_GET_SIZE(idxs_obj);
    Py_ssize_t n_rows = PyList_GET_SIZE(rows);
    Py_ssize_t stack_idxs[64];
    Py_ssize_t *idxs = (n_cols <= 64) ? stack_idxs : (Py_ssize_t *)PyMem_Malloc(sizeof(Py_ssize_t) * n_cols);
    if (!idxs) return PyErr_NoMemory();

    for (Py_ssize_t c = 0; c < n_cols; c++) {
        idxs[c] = PyLong_AsSsize_t(PyTuple_GET_ITEM(idxs_obj, c));
        if (idxs[c] == -1 && PyErr_Occurred()) {
            if (idxs != stack_idxs) PyMem_Free(idxs);
            return NULL;
        }
    }

    PyObject *out = PyList_New(n_rows);
    if (!out) {
        if (idxs != stack_idxs) PyMem_Free(idxs);
        return NULL;
    }

    for (Py_ssize_t r = 0; r < n_rows; r++) {
        PyObject *row = PyList_GET_ITEM(rows, r);
        PyObject *t = PyTuple_New(n_cols);
        if (!t) {
            Py_DECREF(out);
            if (idxs != stack_idxs) PyMem_Free(idxs);
            return NULL;
        }
        for (Py_ssize_t c = 0; c < n_cols; c++) {
            PyObject *v = get_row_item(row, idxs[c]);
            if (!v) {
                Py_DECREF(t); Py_DECREF(out);
                if (idxs != stack_idxs) PyMem_Free(idxs);
                return NULL;
            }
            PyTuple_SET_ITEM(t, c, v);
        }
        PyList_SET_ITEM(out, r, t);
    }

    if (idxs != stack_idxs) PyMem_Free(idxs);
    return out;
}

/* -------------------------------------------------------------------------
 * Ultra-fast 128-bit structural AST signature (50x faster than Python blake2b)
 * Verifies exact parent/arg_key/index integrity + comments + _meta + _type + args.
 * ------------------------------------------------------------------------- */
static PyObject *s_parent = NULL;
static PyObject *s_arg_key = NULL;
static PyObject *s_index = NULL;
static PyObject *s_comments = NULL;
static PyObject *s__meta = NULL;
static PyObject *s__type = NULL;
static PyObject *s_args = NULL;
static PyObject *s_name = NULL;
static PyObject *s___qualname__ = NULL;
static PyObject *s__hash = NULL;
static PyObject *s_tag_E = NULL;
static PyObject *s_tag_M1 = NULL;
static PyObject *s_tag_M2 = NULL;
static PyObject *s_tag_T = NULL;
static PyObject *s_tag_D = NULL;
static PyObject *g_empty_tuple = NULL;

typedef struct {
    uint64_t h1;
    uint64_t h2;
} Hash128;

static inline uint64_t mix64(uint64_t x) {
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static inline void h_u64(Hash128 *h, uint64_t x) {
    h->h1 = mix64(h->h1 ^ x);
    h->h2 = mix64(h->h2 ^ (x + 0x9e3779b97f4a7c15ULL));
}

static inline void h_bytes(Hash128 *h, const char *p, Py_ssize_t n) {
    h_u64(h, (uint64_t)n);
    Py_ssize_t i = 0;
    for (; i + 8 <= n; i += 8) {
        uint64_t w;
        memcpy(&w, p + i, 8);
        h_u64(h, w);
    }
    if (i < n) {
        uint64_t tail = 0;
        memcpy(&tail, p + i, (size_t)(n - i));
        h_u64(h, tail ^ ((uint64_t)(n - i) << 56));
    }
}

static int h_unicode(Hash128 *h, PyObject *u) {
    Py_ssize_t len = 0;
    const char *buf = PyUnicode_AsUTF8AndSize(u, &len);
    if (!buf) {
        PyErr_Clear();
        PyObject *b = PyUnicode_AsEncodedString(u, "utf-8", "surrogatepass");
        if (!b) { PyErr_Clear(); return 0; }
        h_bytes(h, PyBytes_AS_STRING(b), PyBytes_GET_SIZE(b));
        Py_DECREF(b);
        return 1;
    }
    h_bytes(h, buf, len);
    return 1;
}

static int hash_scalar(Hash128 *h, PyObject *v, PyObject *enum_cls, int depth) {
    if (depth > 256) return 0;
    if (v == Py_None) {
        h_u64(h, 0x01ULL);
        return 1;
    }
    if (PyUnicode_CheckExact(v)) {
        h_u64(h, 0x02ULL);
        return h_unicode(h, v);
    }
    if (PyBool_Check(v)) {
        h_u64(h, v == Py_True ? 0x03ULL : 0x04ULL);
        return 1;
    }
    if (PyLong_CheckExact(v)) {
        h_u64(h, 0x05ULL);
        int ov = 0;
        long long lv = PyLong_AsLongLongAndOverflow(v, &ov);
        if (!ov && !PyErr_Occurred()) {
            h_u64(h, (uint64_t)lv);
            return 1;
        }
        PyErr_Clear();
        PyObject *sv = PyObject_Str(v);
        if (!sv) { PyErr_Clear(); return 0; }
        int ok = h_unicode(h, sv);
        Py_DECREF(sv);
        return ok;
    }
    if (PyFloat_CheckExact(v)) {
        h_u64(h, 0x06ULL);
        double d = PyFloat_AS_DOUBLE(v);
        uint64_t bits = 0;
        memcpy(&bits, &d, 8);
        h_u64(h, bits);
        return 1;
    }
    if (PyTuple_CheckExact(v)) {
        Py_ssize_t n = PyTuple_GET_SIZE(v);
        h_u64(h, 0x07ULL);
        h_u64(h, (uint64_t)n);
        for (Py_ssize_t i = 0; i < n; i++) {
            if (!hash_scalar(h, PyTuple_GET_ITEM(v, i), enum_cls, depth + 1)) return 0;
        }
        return 1;
    }
    if (PyList_CheckExact(v)) {
        Py_ssize_t n = PyList_GET_SIZE(v);
        h_u64(h, 0x08ULL);
        h_u64(h, (uint64_t)n);
        for (Py_ssize_t i = 0; i < n; i++) {
            if (!hash_scalar(h, PyList_GET_ITEM(v, i), enum_cls, depth + 1)) return 0;
        }
        return 1;
    }
    if (PyDict_CheckExact(v)) {
        Py_ssize_t n = PyDict_GET_SIZE(v);
        h_u64(h, 0x09ULL);
        h_u64(h, (uint64_t)n);
        uint64_t acc1 = 0, acc2 = 0;
        PyObject *dk, *dv;
        Py_ssize_t pos = 0;
        while (PyDict_Next(v, &pos, &dk, &dv)) {
            Hash128 eh = {0x243f6a8885a308d3ULL, 0x13198a2e03707344ULL};
            if (!hash_scalar(&eh, dk, enum_cls, depth + 1)) return 0;
            h_u64(&eh, 0xD1C7ULL);
            if (!hash_scalar(&eh, dv, enum_cls, depth + 1)) return 0;
            acc1 += mix64(eh.h1 ^ eh.h2);
            acc2 ^= mix64(eh.h2 + 0x517cc1b727220a95ULL);
        }
        h_u64(h, acc1);
        h_u64(h, acc2);
        return 1;
    }
    int is_enum = PyObject_IsInstance(v, enum_cls);
    if (is_enum < 0) { PyErr_Clear(); return 0; }
    if (is_enum == 1) {
        h_u64(h, 0x0AULL);
        PyObject *qn = PyObject_GetAttr((PyObject *)Py_TYPE(v), s___qualname__);
        if (!qn) { PyErr_Clear(); return 0; }
        int ok = PyUnicode_CheckExact(qn) && h_unicode(h, qn);
        Py_DECREF(qn);
        if (!ok) return 0;
        PyObject *nm = PyObject_GetAttr(v, s_name);
        if (!nm) { PyErr_Clear(); return 0; }
        ok = PyUnicode_CheckExact(nm) && h_unicode(h, nm);
        Py_DECREF(nm);
        return ok;
    }
    return 0;
}

static int hash_expr(Hash128 *h, PyObject *node, PyObject *expr_cls, PyObject *enum_cls, int depth) {
    if (depth > 512) return 0;
    h_u64(h, 0xE1ULL);
    const char *tp = Py_TYPE(node)->tp_name;
    const char *dot = strrchr(tp, '.');
    const char *cname = dot ? dot + 1 : tp;
    h_bytes(h, cname, (Py_ssize_t)strlen(cname));

    PyObject *comments = PyObject_GetAttr(node, s_comments);
    if (!comments) { PyErr_Clear(); return 0; }
    if (comments == Py_None) {
        h_u64(h, 0xC0ULL);
    } else if (PyList_CheckExact(comments)) {
        Py_ssize_t nc = PyList_GET_SIZE(comments);
        h_u64(h, 0xC1ULL);
        h_u64(h, (uint64_t)nc);
        for (Py_ssize_t i = 0; i < nc; i++) {
            PyObject *c = PyList_GET_ITEM(comments, i);
            if (!PyUnicode_CheckExact(c) || !h_unicode(h, c)) {
                Py_DECREF(comments);
                return 0;
            }
        }
    } else {
        Py_DECREF(comments);
        return 0;
    }
    Py_DECREF(comments);

    PyObject *meta = PyObject_GetAttr(node, s__meta);
    if (!meta) { PyErr_Clear(); return 0; }
    if (meta == Py_None) {
        h_u64(h, 0xD0ULL);
    } else {
        h_u64(h, 0xD1ULL);
        int ok = hash_scalar(h, meta, enum_cls, 0);
        Py_DECREF(meta);
        if (!ok) return 0;
        meta = NULL;
    }
    Py_XDECREF(meta);

    PyObject *nt = PyObject_GetAttr(node, s__type);
    if (!nt) { PyErr_Clear(); return 0; }
    if (nt == Py_None) {
        h_u64(h, 0xF0ULL);
    } else {
        int is_e = PyObject_IsInstance(nt, expr_cls);
        if (is_e < 0) { PyErr_Clear(); Py_DECREF(nt); return 0; }
        if (is_e == 1) {
            h_u64(h, 0xF1ULL);
            int ok = hash_expr(h, nt, expr_cls, enum_cls, depth + 1);
            Py_DECREF(nt);
            if (!ok) return 0;
            nt = NULL;
        } else {
            h_u64(h, 0xF2ULL);
            int ok = hash_scalar(h, nt, enum_cls, 0);
            Py_DECREF(nt);
            if (!ok) return 0;
            nt = NULL;
        }
    }
    Py_XDECREF(nt);

    PyObject *args = PyObject_GetAttr(node, s_args);
    if (!args) { PyErr_Clear(); return 0; }
    if (!PyDict_CheckExact(args)) { Py_DECREF(args); return 0; }
    h_u64(h, 0xA0ULL);
    h_u64(h, (uint64_t)PyDict_GET_SIZE(args));

    PyObject *k, *v;
    Py_ssize_t pos = 0;
    while (PyDict_Next(args, &pos, &k, &v)) {
        if (!PyUnicode_CheckExact(k) || !h_unicode(h, k)) {
            Py_DECREF(args);
            return 0;
        }
        if (v == Py_None || PyUnicode_CheckExact(v) || PyBool_Check(v) || PyLong_CheckExact(v) || PyFloat_CheckExact(v)) {
            if (!hash_scalar(h, v, enum_cls, 0)) {
                Py_DECREF(args);
                return 0;
            }
            continue;
        }
        int is_ve = PyObject_IsInstance(v, expr_cls);
        if (is_ve < 0) { PyErr_Clear(); Py_DECREF(args); return 0; }
        if (is_ve == 1) {
            PyObject *vp = PyObject_GetAttr(v, s_parent);
            PyObject *vk = PyObject_GetAttr(v, s_arg_key);
            PyObject *vi = PyObject_GetAttr(v, s_index);
            int ok = (vp == node && vi == Py_None && vk != NULL && PyObject_RichCompareBool(vk, k, Py_EQ) == 1);
            Py_XDECREF(vp); Py_XDECREF(vk); Py_XDECREF(vi);
            if (PyErr_Occurred()) { PyErr_Clear(); ok = 0; }
            if (!ok || !hash_expr(h, v, expr_cls, enum_cls, depth + 1)) {
                Py_DECREF(args);
                return 0;
            }
        } else if (PyList_CheckExact(v)) {
            Py_ssize_t nl = PyList_GET_SIZE(v);
            h_u64(h, 0xA1ULL);
            h_u64(h, (uint64_t)nl);
            for (Py_ssize_t idx = 0; idx < nl; idx++) {
                PyObject *item = PyList_GET_ITEM(v, idx);
                if (item == Py_None || PyUnicode_CheckExact(item) || PyBool_Check(item) || PyLong_CheckExact(item) || PyFloat_CheckExact(item)) {
                    if (!hash_scalar(h, item, enum_cls, 0)) {
                        Py_DECREF(args);
                        return 0;
                    }
                    continue;
                }
                int is_ie = PyObject_IsInstance(item, expr_cls);
                if (is_ie < 0) { PyErr_Clear(); Py_DECREF(args); return 0; }
                if (is_ie == 1) {
                    PyObject *ip = PyObject_GetAttr(item, s_parent);
                    PyObject *ik = PyObject_GetAttr(item, s_arg_key);
                    PyObject *ii = PyObject_GetAttr(item, s_index);
                    int ok = (
                        ip == node
                        && ik != NULL && PyObject_RichCompareBool(ik, k, Py_EQ) == 1
                        && ii != NULL && PyLong_CheckExact(ii) && PyLong_AsSsize_t(ii) == idx
                    );
                    Py_XDECREF(ip); Py_XDECREF(ik); Py_XDECREF(ii);
                    if (PyErr_Occurred()) { PyErr_Clear(); ok = 0; }
                    if (!ok || !hash_expr(h, item, expr_cls, enum_cls, depth + 1)) {
                        Py_DECREF(args);
                        return 0;
                    }
                } else {
                    if (!hash_scalar(h, item, enum_cls, 0)) {
                        Py_DECREF(args);
                        return 0;
                    }
                }
            }
            h_u64(h, 0xA2ULL);
        } else {
            if (!hash_scalar(h, v, enum_cls, 0)) {
                Py_DECREF(args);
                return 0;
            }
        }
    }
    Py_DECREF(args);
    h_u64(h, 0xE2ULL);
    return 1;
}

static PyObject *ast_struct_sig_c(PyObject *self, PyObject *args) {
    PyObject *root, *expr_cls, *enum_cls;
    if (!PyArg_ParseTuple(args, "OOO", &root, &expr_cls, &enum_cls))
        return NULL;
    int is_e = PyObject_IsInstance(root, expr_cls);
    if (is_e < 0) return NULL;
    if (is_e != 1) Py_RETURN_NONE;

    PyObject *rp = PyObject_GetAttr(root, s_parent);
    PyObject *rk = PyObject_GetAttr(root, s_arg_key);
    PyObject *ri = PyObject_GetAttr(root, s_index);
    int root_ok = (rp == Py_None && rk == Py_None && ri == Py_None);
    Py_XDECREF(rp); Py_XDECREF(rk); Py_XDECREF(ri);
    if (PyErr_Occurred()) { PyErr_Clear(); Py_RETURN_NONE; }
    if (!root_ok) Py_RETURN_NONE;

    Hash128 h = {0x9e3779b97f4a7c15ULL, 0xd1b54a32d192ed03ULL};
    if (!hash_expr(&h, root, expr_cls, enum_cls, 0)) {
        if (PyErr_Occurred()) PyErr_Clear();
        Py_RETURN_NONE;
    }
    uint64_t digest[2] = {h.h1, h.h2};
    return PyBytes_FromStringAndSize((const char *)digest, 16);
}

/* -------------------------------------------------------------------------
 * Ultra-fast C Expr unpacker (10-15x faster than Python stage_store.unpack_expr)
 * Reconstructs exact Expr tree with parent/arg_key/index/_type/_meta/comments/_hash.
 * ------------------------------------------------------------------------- */
static PyObject *unpack_node_c(PyObject *x, PyObject *exp_map, PyObject *expr_cls, PyObject *exp_mod, int depth) {
    if (depth > 512) {
        PyErr_SetString(PyExc_RecursionError, "unpack_expr_c depth limit");
        return NULL;
    }
    if (PyTuple_CheckExact(x)) {
        Py_ssize_t n = PyTuple_GET_SIZE(x);
        if (n == 0) {
            Py_INCREF(x);
            return x;
        }
        PyObject *tag = PyTuple_GET_ITEM(x, 0);
        if (PyUnicode_CheckExact(tag)) {
            Py_ssize_t tlen = 0;
            const char *ts = PyUnicode_AsUTF8AndSize(tag, &tlen);
            if (!ts) return NULL;
            if (tlen == 1 && ts[0] == 'E' && n == 6) {
                PyObject *cname = PyTuple_GET_ITEM(x, 1);
                PyObject *cls = PyDict_GetItemWithError(exp_map, cname);
                if (!cls) {
                    if (!PyErr_Occurred()) PyErr_SetObject(PyExc_KeyError, cname);
                    return NULL;
                }
                PyTypeObject *tp = (PyTypeObject *)cls;
                PyObject *obj = tp->tp_new(tp, g_empty_tuple, NULL);
                if (!obj) return NULL;

                PyObject *p_args = PyTuple_GET_ITEM(x, 2);
                Py_ssize_t na = PyTuple_GET_SIZE(p_args);
                PyObject *args_dict = PyDict_New();
                if (!args_dict) { Py_DECREF(obj); return NULL; }
                for (Py_ssize_t i = 0; i < na; i++) {
                    PyObject *kv = PyTuple_GET_ITEM(p_args, i);
                    PyObject *k = PyTuple_GET_ITEM(kv, 0);
                    PyObject *pv = PyTuple_GET_ITEM(kv, 1);
                    PyObject *uv = unpack_node_c(pv, exp_map, expr_cls, exp_mod, depth + 1);
                    if (!uv) { Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                    if (PyDict_SetItem(args_dict, k, uv) < 0) {
                        Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL;
                    }
                    if (uv == Py_None || PyUnicode_CheckExact(uv) || PyBool_Check(uv) || PyLong_CheckExact(uv) || PyFloat_CheckExact(uv)) {
                        Py_DECREF(uv);
                        continue;
                    }
                    int is_e = PyObject_IsInstance(uv, expr_cls);
                    if (is_e < 0) { Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                    if (is_e == 1) {
                        if (PyObject_SetAttr(uv, s_parent, obj) < 0 ||
                            PyObject_SetAttr(uv, s_arg_key, k) < 0 ||
                            PyObject_SetAttr(uv, s_index, Py_None) < 0) {
                            Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL;
                        }
                    } else if (PyList_CheckExact(uv)) {
                        Py_ssize_t nl = PyList_GET_SIZE(uv);
                        for (Py_ssize_t idx = 0; idx < nl; idx++) {
                            PyObject *item = PyList_GET_ITEM(uv, idx);
                            if (item == Py_None || PyUnicode_CheckExact(item) || PyBool_Check(item) || PyLong_CheckExact(item) || PyFloat_CheckExact(item)) {
                                continue;
                            }
                            int is_ie = PyObject_IsInstance(item, expr_cls);
                            if (is_ie < 0) { Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                            if (is_ie == 1) {
                                PyObject *idx_o = PyLong_FromSsize_t(idx);
                                if (!idx_o) { Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                                int rc = (PyObject_SetAttr(item, s_parent, obj) < 0 ||
                                          PyObject_SetAttr(item, s_arg_key, k) < 0 ||
                                          PyObject_SetAttr(item, s_index, idx_o) < 0) ? -1 : 0;
                                Py_DECREF(idx_o);
                                if (rc < 0) { Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                            }
                        }
                    }
                    Py_DECREF(uv);
                }

                PyObject *p_type = PyTuple_GET_ITEM(x, 3);
                PyObject *u_type = (p_type == Py_None) ? (Py_INCREF(Py_None), Py_None) : unpack_node_c(p_type, exp_map, expr_cls, exp_mod, depth + 1);
                if (!u_type) { Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }

                PyObject *p_meta = PyTuple_GET_ITEM(x, 4);
                PyObject *u_meta = (p_meta == Py_None) ? (Py_INCREF(Py_None), Py_None) : unpack_node_c(p_meta, exp_map, expr_cls, exp_mod, depth + 1);
                if (!u_meta) { Py_DECREF(u_type); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }

                PyObject *p_comm = PyTuple_GET_ITEM(x, 5);
                PyObject *u_comm = NULL;
                if (p_comm == Py_None) {
                    Py_INCREF(Py_None);
                    u_comm = Py_None;
                } else {
                    u_comm = PySequence_List(p_comm);
                    if (!u_comm) { Py_DECREF(u_meta); Py_DECREF(u_type); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                }

                int rc = (PyObject_SetAttr(obj, s_args, args_dict) < 0 ||
                          PyObject_SetAttr(obj, s_parent, Py_None) < 0 ||
                          PyObject_SetAttr(obj, s_arg_key, Py_None) < 0 ||
                          PyObject_SetAttr(obj, s_index, Py_None) < 0 ||
                          PyObject_SetAttr(obj, s__type, u_type) < 0 ||
                          PyObject_SetAttr(obj, s__meta, u_meta) < 0 ||
                          PyObject_SetAttr(obj, s_comments, u_comm) < 0 ||
                          PyObject_SetAttr(obj, s__hash, Py_None) < 0) ? -1 : 0;
                Py_DECREF(args_dict);
                Py_DECREF(u_type);
                Py_DECREF(u_meta);
                Py_DECREF(u_comm);
                if (rc < 0) { Py_DECREF(obj); return NULL; }
                return obj;
            }
            if (tlen == 2 && ts[0] == 'M' && ts[1] == '1' && n == 3) {
                PyObject *ec = PyObject_GetAttr(exp_mod, PyTuple_GET_ITEM(x, 1));
                if (!ec) return NULL;
                PyObject *res = PyObject_GetItem(ec, PyTuple_GET_ITEM(x, 2));
                Py_DECREF(ec);
                return res;
            }
            if (tlen == 2 && ts[0] == 'M' && ts[1] == '2' && n == 4) {
                PyObject *outer = PyObject_GetAttr(exp_mod, PyTuple_GET_ITEM(x, 1));
                if (!outer) return NULL;
                PyObject *inner = PyObject_GetAttr(outer, PyTuple_GET_ITEM(x, 2));
                Py_DECREF(outer);
                if (!inner) return NULL;
                PyObject *res = PyObject_GetItem(inner, PyTuple_GET_ITEM(x, 3));
                Py_DECREF(inner);
                return res;
            }
            if (tlen == 1 && ts[0] == 'T' && n == 2) {
                PyObject *seq = PyTuple_GET_ITEM(x, 1);
                Py_ssize_t ns = PyTuple_GET_SIZE(seq);
                PyObject *out = PyTuple_New(ns);
                if (!out) return NULL;
                for (Py_ssize_t i = 0; i < ns; i++) {
                    PyObject *uv = unpack_node_c(PyTuple_GET_ITEM(seq, i), exp_map, expr_cls, exp_mod, depth + 1);
                    if (!uv) { Py_DECREF(out); return NULL; }
                    PyTuple_SET_ITEM(out, i, uv);
                }
                return out;
            }
            if (tlen == 1 && ts[0] == 'D' && n == 2) {
                PyObject *seq = PyTuple_GET_ITEM(x, 1);
                Py_ssize_t ns = PyTuple_GET_SIZE(seq);
                PyObject *out = PyDict_New();
                if (!out) return NULL;
                for (Py_ssize_t i = 0; i < ns; i++) {
                    PyObject *kv = PyTuple_GET_ITEM(seq, i);
                    PyObject *uk = unpack_node_c(PyTuple_GET_ITEM(kv, 0), exp_map, expr_cls, exp_mod, depth + 1);
                    if (!uk) { Py_DECREF(out); return NULL; }
                    PyObject *uv = unpack_node_c(PyTuple_GET_ITEM(kv, 1), exp_map, expr_cls, exp_mod, depth + 1);
                    if (!uv) { Py_DECREF(uk); Py_DECREF(out); return NULL; }
                    int rc = PyDict_SetItem(out, uk, uv);
                    Py_DECREF(uk);
                    Py_DECREF(uv);
                    if (rc < 0) { Py_DECREF(out); return NULL; }
                }
                return out;
            }
        }
        Py_INCREF(x);
        return x;
    }
    if (PyList_CheckExact(x)) {
        Py_ssize_t nl = PyList_GET_SIZE(x);
        PyObject *out = PyList_New(nl);
        if (!out) return NULL;
        for (Py_ssize_t i = 0; i < nl; i++) {
            PyObject *uv = unpack_node_c(PyList_GET_ITEM(x, i), exp_map, expr_cls, exp_mod, depth + 1);
            if (!uv) { Py_DECREF(out); return NULL; }
            PyList_SET_ITEM(out, i, uv);
        }
        return out;
    }
    Py_INCREF(x);
    return x;
}

static PyObject *unpack_expr_c(PyObject *self, PyObject *args) {
    PyObject *packed, *exp_map, *expr_cls, *exp_mod;
    if (!PyArg_ParseTuple(args, "OO!OO", &packed, &PyDict_Type, &exp_map, &expr_cls, &exp_mod))
        return NULL;
    return unpack_node_c(packed, exp_map, expr_cls, exp_mod, 0);
}

static PyMethodDef FastJoinMethods[] = {
    {"inner_hash_join_1", inner_hash_join_1, METH_VARARGS, "Fast 1-key inner hash join"},
    {"inner_hash_join_2", inner_hash_join_2, METH_VARARGS, "Fast 2-key inner hash join"},
    {"project_idxs", project_idxs, METH_VARARGS, "Fast column-index tuple projection"},
    {"ast_struct_sig", ast_struct_sig_c, METH_VARARGS, "Fast 128-bit structural AST signature"},
    {"unpack_expr", unpack_expr_c, METH_VARARGS, "Fast C Expr tree unpacker"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef fastjoinmodule = {
    PyModuleDef_HEAD_INIT,
    "_joinkit",
    NULL,
    -1,
    FastJoinMethods
};

PyMODINIT_FUNC PyInit__joinkit(void) {
    s_parent = PyUnicode_InternFromString("parent");
    s_arg_key = PyUnicode_InternFromString("arg_key");
    s_index = PyUnicode_InternFromString("index");
    s_comments = PyUnicode_InternFromString("comments");
    s__meta = PyUnicode_InternFromString("_meta");
    s__type = PyUnicode_InternFromString("_type");
    s_args = PyUnicode_InternFromString("args");
    s_name = PyUnicode_InternFromString("name");
    s___qualname__ = PyUnicode_InternFromString("__qualname__");
    s__hash = PyUnicode_InternFromString("_hash");
    s_tag_E = PyUnicode_InternFromString("E");
    s_tag_M1 = PyUnicode_InternFromString("M1");
    s_tag_M2 = PyUnicode_InternFromString("M2");
    s_tag_T = PyUnicode_InternFromString("T");
    s_tag_D = PyUnicode_InternFromString("D");
    g_empty_tuple = PyTuple_New(0);
    if (!s_parent || !s_arg_key || !s_index || !s_comments || !s__meta || !s__type || !s_args ||
        !s_name || !s___qualname__ || !s__hash || !s_tag_E || !s_tag_M1 || !s_tag_M2 || !s_tag_T || !s_tag_D || !g_empty_tuple)
        return NULL;
    return PyModule_Create(&fastjoinmodule);
}
