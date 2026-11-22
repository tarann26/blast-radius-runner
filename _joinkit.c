#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

static inline PyObject *tup_cat(PyObject *a, PyObject *b) {
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

static inline PyObject *row_at(PyObject *row, Py_ssize_t idx) {
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

static int dict_push_row(PyObject *dict, PyObject *key, PyObject *row) {
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

static PyObject *hjoin_one(PyObject *self, PyObject *args) {
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
        PyObject *k = row_at(row, j_idx);
        if (!k) { Py_DECREF(join_map); return NULL; }
        if (k != Py_None) {
            if (dict_push_row(join_map, k, row) < 0) {
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
        PyObject *k = row_at(row, s_idx);
        if (!k) { Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }
        if (k != Py_None) {
            PyObject *jg = PyDict_GetItemWithError(join_map, k);
            if (!jg) {
                if (PyErr_Occurred()) {
                    Py_DECREF(k); Py_DECREF(src_groups); Py_DECREF(join_map); return NULL;
                }
            } else {
                if (dict_push_row(src_groups, k, row) < 0) {
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
                PyObject *joined = tup_cat(sr, jr);
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

static PyObject *hjoin_two(PyObject *self, PyObject *args) {
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
        PyObject *v0 = row_at(row, j0);
        if (!v0) { Py_DECREF(join_map); return NULL; }
        if (v0 == Py_None) { Py_DECREF(v0); continue; }
        PyObject *v1 = row_at(row, j1);
        if (!v1) { Py_DECREF(v0); Py_DECREF(join_map); return NULL; }
        if (v1 == Py_None) { Py_DECREF(v0); Py_DECREF(v1); continue; }
        PyObject *k = PyTuple_New(2);
        if (!k) { Py_DECREF(v0); Py_DECREF(v1); Py_DECREF(join_map); return NULL; }
        PyTuple_SET_ITEM(k, 0, v0);
        PyTuple_SET_ITEM(k, 1, v1);
        if (dict_push_row(join_map, k, row) < 0) {
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
        PyObject *v0 = row_at(row, s0);
        if (!v0) { Py_DECREF(src_groups); Py_DECREF(join_map); return NULL; }
        if (v0 == Py_None) { Py_DECREF(v0); continue; }
        PyObject *v1 = row_at(row, s1);
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
            if (dict_push_row(src_groups, k, row) < 0) {
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
                PyObject *joined = tup_cat(sr, jr);
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

static PyObject *proj_cols(PyObject *self, PyObject *args) {
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
            PyObject *v = row_at(row, idxs[c]);
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
static PyObject *s_pass = NULL;
static PyObject *s_fail = NULL;
static PyObject *s_fallback = NULL;
static PyObject *s_py = NULL;
static PyObject *g_empty_tuple = NULL;

static inline int is_expr_obj(PyObject *obj, PyObject *expr_cls) {
    if (PyType_Check(expr_cls)) {
        if (PyType_IsSubtype(Py_TYPE(obj), (PyTypeObject *)expr_cls)) {
            return 1;
        }
        return 0;
    }
    return PyObject_IsInstance(obj, expr_cls);
}

typedef struct {
    uint64_t h1;
    uint64_t h2;
} Sig128;

static inline uint64_t mix_u64(uint64_t x) {
    x ^= x >> 30;
    x *= 0xbf58476d1ce4e5b9ULL;
    x ^= x >> 27;
    x *= 0x94d049bb133111ebULL;
    x ^= x >> 31;
    return x;
}

static inline void sig_u64(Sig128 *h, uint64_t x) {
    h->h1 = mix_u64(h->h1 ^ x);
    h->h2 = mix_u64(h->h2 ^ (x + 0x9e3779b97f4a7c15ULL));
}

static inline void sig_bytes(Sig128 *h, const char *p, Py_ssize_t n) {
    sig_u64(h, (uint64_t)n);
    Py_ssize_t i = 0;
    for (; i + 8 <= n; i += 8) {
        uint64_t w;
        memcpy(&w, p + i, 8);
        sig_u64(h, w);
    }
    if (i < n) {
        uint64_t tail = 0;
        memcpy(&tail, p + i, (size_t)(n - i));
        sig_u64(h, tail ^ ((uint64_t)(n - i) << 56));
    }
}

static int sig_str(Sig128 *h, PyObject *u) {
    Py_ssize_t len = 0;
    const char *buf = PyUnicode_AsUTF8AndSize(u, &len);
    if (!buf) {
        PyErr_Clear();
        PyObject *b = PyUnicode_AsEncodedString(u, "utf-8", "surrogatepass");
        if (!b) { PyErr_Clear(); return 0; }
        sig_bytes(h, PyBytes_AS_STRING(b), PyBytes_GET_SIZE(b));
        Py_DECREF(b);
        return 1;
    }
    sig_bytes(h, buf, len);
    return 1;
}

static int sig_scalar(Sig128 *h, PyObject *v, PyObject *enum_cls, int depth) {
    if (depth > 256) return 0;
    if (v == Py_None) {
        sig_u64(h, 0x01ULL);
        return 1;
    }
    if (PyUnicode_CheckExact(v)) {
        sig_u64(h, 0x02ULL);
        return sig_str(h, v);
    }
    if (PyBool_Check(v)) {
        sig_u64(h, v == Py_True ? 0x03ULL : 0x04ULL);
        return 1;
    }
    if (PyLong_CheckExact(v)) {
        sig_u64(h, 0x05ULL);
        int ov = 0;
        long long lv = PyLong_AsLongLongAndOverflow(v, &ov);
        if (!ov && !PyErr_Occurred()) {
            sig_u64(h, (uint64_t)lv);
            return 1;
        }
        PyErr_Clear();
        PyObject *sv = PyObject_Str(v);
        if (!sv) { PyErr_Clear(); return 0; }
        int ok = sig_str(h, sv);
        Py_DECREF(sv);
        return ok;
    }
    if (PyFloat_CheckExact(v)) {
        sig_u64(h, 0x06ULL);
        double d = PyFloat_AS_DOUBLE(v);
        uint64_t bits = 0;
        memcpy(&bits, &d, 8);
        sig_u64(h, bits);
        return 1;
    }
    if (PyTuple_CheckExact(v)) {
        Py_ssize_t n = PyTuple_GET_SIZE(v);
        sig_u64(h, 0x07ULL);
        sig_u64(h, (uint64_t)n);
        for (Py_ssize_t i = 0; i < n; i++) {
            if (!sig_scalar(h, PyTuple_GET_ITEM(v, i), enum_cls, depth + 1)) return 0;
        }
        return 1;
    }
    if (PyList_CheckExact(v)) {
        Py_ssize_t n = PyList_GET_SIZE(v);
        sig_u64(h, 0x08ULL);
        sig_u64(h, (uint64_t)n);
        for (Py_ssize_t i = 0; i < n; i++) {
            if (!sig_scalar(h, PyList_GET_ITEM(v, i), enum_cls, depth + 1)) return 0;
        }
        return 1;
    }
    if (PyDict_CheckExact(v)) {
        Py_ssize_t n = PyDict_GET_SIZE(v);
        sig_u64(h, 0x09ULL);
        sig_u64(h, (uint64_t)n);
        uint64_t acc1 = 0, acc2 = 0;
        PyObject *dk, *dv;
        Py_ssize_t pos = 0;
        while (PyDict_Next(v, &pos, &dk, &dv)) {
            Sig128 eh = {0x243f6a8885a308d3ULL, 0x13198a2e03707344ULL};
            if (!sig_scalar(&eh, dk, enum_cls, depth + 1)) return 0;
            sig_u64(&eh, 0xD1C7ULL);
            if (!sig_scalar(&eh, dv, enum_cls, depth + 1)) return 0;
            acc1 += mix_u64(eh.h1 ^ eh.h2);
            acc2 ^= mix_u64(eh.h2 + 0x517cc1b727220a95ULL);
        }
        sig_u64(h, acc1);
        sig_u64(h, acc2);
        return 1;
    }
    int is_enum = PyObject_IsInstance(v, enum_cls);
    if (is_enum < 0) { PyErr_Clear(); return 0; }
    if (is_enum == 1) {
        sig_u64(h, 0x0AULL);
        PyObject *qn = PyObject_GetAttr((PyObject *)Py_TYPE(v), s___qualname__);
        if (!qn) { PyErr_Clear(); return 0; }
        int ok = PyUnicode_CheckExact(qn) && sig_str(h, qn);
        Py_DECREF(qn);
        if (!ok) return 0;
        PyObject *nm = PyObject_GetAttr(v, s_name);
        if (!nm) { PyErr_Clear(); return 0; }
        ok = PyUnicode_CheckExact(nm) && sig_str(h, nm);
        Py_DECREF(nm);
        return ok;
    }
    return 0;
}

static int sig_expr(Sig128 *h, PyObject *node, PyObject *expr_cls, PyObject *enum_cls, int depth) {
    if (depth > 512) return 0;
    sig_u64(h, 0xE1ULL);
    const char *tp = Py_TYPE(node)->tp_name;
    const char *dot = strrchr(tp, '.');
    const char *cname = dot ? dot + 1 : tp;
    sig_bytes(h, cname, (Py_ssize_t)strlen(cname));

    PyObject *comments = PyObject_GetAttr(node, s_comments);
    if (!comments) { PyErr_Clear(); return 0; }
    if (comments == Py_None) {
        sig_u64(h, 0xC0ULL);
    } else if (PyList_CheckExact(comments)) {
        Py_ssize_t nc = PyList_GET_SIZE(comments);
        sig_u64(h, 0xC1ULL);
        sig_u64(h, (uint64_t)nc);
        for (Py_ssize_t i = 0; i < nc; i++) {
            PyObject *c = PyList_GET_ITEM(comments, i);
            if (!PyUnicode_CheckExact(c) || !sig_str(h, c)) {
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
        sig_u64(h, 0xD0ULL);
    } else {
        sig_u64(h, 0xD1ULL);
        int ok = sig_scalar(h, meta, enum_cls, 0);
        Py_DECREF(meta);
        if (!ok) return 0;
        meta = NULL;
    }
    Py_XDECREF(meta);

    PyObject *nt = PyObject_GetAttr(node, s__type);
    if (!nt) { PyErr_Clear(); return 0; }
    if (nt == Py_None) {
        sig_u64(h, 0xF0ULL);
    } else {
        int is_e = is_expr_obj(nt, expr_cls);
        if (is_e < 0) { PyErr_Clear(); Py_DECREF(nt); return 0; }
        if (is_e == 1) {
            sig_u64(h, 0xF1ULL);
            int ok = sig_expr(h, nt, expr_cls, enum_cls, depth + 1);
            Py_DECREF(nt);
            if (!ok) return 0;
            nt = NULL;
        } else {
            sig_u64(h, 0xF2ULL);
            int ok = sig_scalar(h, nt, enum_cls, 0);
            Py_DECREF(nt);
            if (!ok) return 0;
            nt = NULL;
        }
    }
    Py_XDECREF(nt);

    PyObject *args = PyObject_GetAttr(node, s_args);
    if (!args) { PyErr_Clear(); return 0; }
    if (!PyDict_CheckExact(args)) { Py_DECREF(args); return 0; }
    sig_u64(h, 0xA0ULL);
    sig_u64(h, (uint64_t)PyDict_GET_SIZE(args));

    PyObject *k, *v;
    Py_ssize_t pos = 0;
    while (PyDict_Next(args, &pos, &k, &v)) {
        if (!PyUnicode_CheckExact(k) || !sig_str(h, k)) {
            Py_DECREF(args);
            return 0;
        }
        if (v == Py_None || PyUnicode_CheckExact(v) || PyBool_Check(v) || PyLong_CheckExact(v) || PyFloat_CheckExact(v)) {
            if (!sig_scalar(h, v, enum_cls, 0)) {
                Py_DECREF(args);
                return 0;
            }
            continue;
        }
        if (PyList_CheckExact(v)) {
            Py_ssize_t nl = PyList_GET_SIZE(v);
            sig_u64(h, 0xA1ULL);
            sig_u64(h, (uint64_t)nl);
            for (Py_ssize_t idx = 0; idx < nl; idx++) {
                PyObject *item = PyList_GET_ITEM(v, idx);
                if (item == Py_None || PyUnicode_CheckExact(item) || PyBool_Check(item) || PyLong_CheckExact(item) || PyFloat_CheckExact(item)) {
                    if (!sig_scalar(h, item, enum_cls, 0)) {
                        Py_DECREF(args);
                        return 0;
                    }
                    continue;
                }
                int is_ie = is_expr_obj(item, expr_cls);
                if (is_ie < 0) { PyErr_Clear(); Py_DECREF(args); return 0; }
                if (is_ie == 1) {
                    PyObject *ip = PyObject_GetAttr(item, s_parent);
                    PyObject *ik = PyObject_GetAttr(item, s_arg_key);
                    PyObject *ii = PyObject_GetAttr(item, s_index);
                    int ok = (
                        ip == node
                        && ik != NULL && (ik == k || PyObject_RichCompareBool(ik, k, Py_EQ) == 1)
                        && ii != NULL && PyLong_CheckExact(ii) && PyLong_AsSsize_t(ii) == idx
                    );
                    Py_XDECREF(ip); Py_XDECREF(ik); Py_XDECREF(ii);
                    if (PyErr_Occurred()) { PyErr_Clear(); ok = 0; }
                    if (!ok || !sig_expr(h, item, expr_cls, enum_cls, depth + 1)) {
                        Py_DECREF(args);
                        return 0;
                    }
                } else {
                    if (!sig_scalar(h, item, enum_cls, 0)) {
                        Py_DECREF(args);
                        return 0;
                    }
                }
            }
            sig_u64(h, 0xA2ULL);
            continue;
        }
        int is_ve = is_expr_obj(v, expr_cls);
        if (is_ve < 0) { PyErr_Clear(); Py_DECREF(args); return 0; }
        if (is_ve == 1) {
            PyObject *vp = PyObject_GetAttr(v, s_parent);
            PyObject *vk = PyObject_GetAttr(v, s_arg_key);
            PyObject *vi = PyObject_GetAttr(v, s_index);
            int ok = (vp == node && vi == Py_None && vk != NULL && (vk == k || PyObject_RichCompareBool(vk, k, Py_EQ) == 1));
            Py_XDECREF(vp); Py_XDECREF(vk); Py_XDECREF(vi);
            if (PyErr_Occurred()) { PyErr_Clear(); ok = 0; }
            if (!ok || !sig_expr(h, v, expr_cls, enum_cls, depth + 1)) {
                Py_DECREF(args);
                return 0;
            }
        } else {
            if (!sig_scalar(h, v, enum_cls, 0)) {
                Py_DECREF(args);
                return 0;
            }
        }
    }
    Py_DECREF(args);
    sig_u64(h, 0xE2ULL);
    return 1;
}

static PyObject *c_ast_sig(PyObject *self, PyObject *args) {
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

    Sig128 h = {0x9e3779b97f4a7c15ULL, 0xd1b54a32d192ed03ULL};
    if (!sig_expr(&h, root, expr_cls, enum_cls, 0)) {
        if (PyErr_Occurred()) PyErr_Clear();
        Py_RETURN_NONE;
    }
    uint64_t digest[2] = {h.h1, h.h2};
    return PyBytes_FromStringAndSize((const char *)digest, 16);
}

static PyObject *c_unpack_node(PyObject *x, PyObject *exp_map, PyObject *expr_cls, PyObject *exp_mod, int depth) {
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
            const char *ts = NULL;
            if (tag == s_tag_E) {
                tlen = 1;
                ts = "E";
            } else {
                ts = PyUnicode_AsUTF8AndSize(tag, &tlen);
                if (!ts) return NULL;
            }
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
                    PyObject *uv = c_unpack_node(pv, exp_map, expr_cls, exp_mod, depth + 1);
                    if (!uv) { Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                    if (PyDict_SetItem(args_dict, k, uv) < 0) {
                        Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL;
                    }
                    if (uv == Py_None || PyUnicode_CheckExact(uv) || PyBool_Check(uv) || PyLong_CheckExact(uv) || PyFloat_CheckExact(uv)) {
                        Py_DECREF(uv);
                        continue;
                    }
                    if (PyList_CheckExact(uv)) {
                        Py_ssize_t nl = PyList_GET_SIZE(uv);
                        for (Py_ssize_t idx = 0; idx < nl; idx++) {
                            PyObject *item = PyList_GET_ITEM(uv, idx);
                            if (item == Py_None || PyUnicode_CheckExact(item) || PyBool_Check(item) || PyLong_CheckExact(item) || PyFloat_CheckExact(item)) {
                                continue;
                            }
                            int is_ie = is_expr_obj(item, expr_cls);
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
                        Py_DECREF(uv);
                        continue;
                    }
                    int is_e = is_expr_obj(uv, expr_cls);
                    if (is_e < 0) { Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }
                    if (is_e == 1) {
                        if (PyObject_SetAttr(uv, s_parent, obj) < 0 ||
                            PyObject_SetAttr(uv, s_arg_key, k) < 0 ||
                            PyObject_SetAttr(uv, s_index, Py_None) < 0) {
                            Py_DECREF(uv); Py_DECREF(args_dict); Py_DECREF(obj); return NULL;
                        }
                    }
                    Py_DECREF(uv);
                }

                PyObject *p_type = PyTuple_GET_ITEM(x, 3);
                PyObject *u_type = (p_type == Py_None) ? (Py_INCREF(Py_None), Py_None) : c_unpack_node(p_type, exp_map, expr_cls, exp_mod, depth + 1);
                if (!u_type) { Py_DECREF(args_dict); Py_DECREF(obj); return NULL; }

                PyObject *p_meta = PyTuple_GET_ITEM(x, 4);
                PyObject *u_meta = (p_meta == Py_None) ? (Py_INCREF(Py_None), Py_None) : c_unpack_node(p_meta, exp_map, expr_cls, exp_mod, depth + 1);
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
                    PyObject *uv = c_unpack_node(PyTuple_GET_ITEM(seq, i), exp_map, expr_cls, exp_mod, depth + 1);
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
                    PyObject *uk = c_unpack_node(PyTuple_GET_ITEM(kv, 0), exp_map, expr_cls, exp_mod, depth + 1);
                    if (!uk) { Py_DECREF(out); return NULL; }
                    PyObject *uv = c_unpack_node(PyTuple_GET_ITEM(kv, 1), exp_map, expr_cls, exp_mod, depth + 1);
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
            PyObject *uv = c_unpack_node(PyList_GET_ITEM(x, i), exp_map, expr_cls, exp_mod, depth + 1);
            if (!uv) { Py_DECREF(out); return NULL; }
            PyList_SET_ITEM(out, i, uv);
        }
        return out;
    }
    Py_INCREF(x);
    return x;
}

static PyObject *c_unpack_expr(PyObject *self, PyObject *args) {
    PyObject *packed, *exp_map, *expr_cls, *exp_mod;
    if (!PyArg_ParseTuple(args, "OO!OO", &packed, &PyDict_Type, &exp_map, &expr_cls, &exp_mod))
        return NULL;
    return c_unpack_node(packed, exp_map, expr_cls, exp_mod, 0);
}

static PyObject *c_open_lookup(PyObject *self, PyObject *args) {
    PyObject *file, *abs_map, *del_set, *basenames;
    if (!PyArg_ParseTuple(
            args, "OO!O!O!",
            &file, &PyDict_Type, &abs_map, &PySet_Type, &del_set, &PyTuple_Type, &basenames))
        return NULL;

    if (PyLong_Check(file)) {
        Py_RETURN_NONE;
    }

    PyObject *s_obj = NULL;
    int owned = 0;
    if (PyUnicode_CheckExact(file)) {
        s_obj = file;
    } else {
        s_obj = PyOS_FSPath(file);
        if (!s_obj) {
            if (PyErr_ExceptionMatches(PyExc_TypeError)) {
                PyErr_Clear();
                Py_RETURN_NONE;
            }
            return NULL;
        }
        owned = 1;
        if (!PyUnicode_Check(s_obj)) {
            Py_DECREF(s_obj);
            Py_RETURN_NONE;
        }
    }

    if (PySet_GET_SIZE(del_set) > 0) {
        int in_del = PySet_Contains(del_set, s_obj);
        if (in_del < 0) {
            if (owned) Py_DECREF(s_obj);
            return NULL;
        }
        if (in_del == 1) {
            if (owned) Py_DECREF(s_obj);
            Py_RETURN_TRUE;
        }
    }

    if (PyDict_GET_SIZE(abs_map) > 0) {
        PyObject *hit = PyDict_GetItemWithError(abs_map, s_obj);
        if (hit) {
            Py_INCREF(hit);
            if (owned) Py_DECREF(s_obj);
            return hit;
        }
        if (PyErr_Occurred()) {
            if (owned) Py_DECREF(s_obj);
            return NULL;
        }
    }

    Py_ssize_t nb = PyTuple_GET_SIZE(basenames);
    if (nb == 0) {
        if (owned) Py_DECREF(s_obj);
        Py_RETURN_NONE;
    }

    Py_ssize_t len = 0;
    const char *buf = PyUnicode_AsUTF8AndSize(s_obj, &len);
    if (!buf) {
        PyErr_Clear();
        if (owned) Py_DECREF(s_obj);
        Py_RETURN_FALSE;
    }
    if (len == 0 || buf[len - 1] == '/') {
        if (owned) Py_DECREF(s_obj);
        Py_RETURN_FALSE;
    }
    if (len >= 5 && memcmp(buf, "/tmp/", 5) == 0) {
        if (owned) Py_DECREF(s_obj);
        Py_RETURN_FALSE;
    }

    Py_ssize_t slash = len - 1;
    while (slash >= 0 && buf[slash] != '/') {
        slash--;
    }
    const char *bname = buf + slash + 1;
    Py_ssize_t blen = len - (slash + 1);
    if ((blen == 1 && bname[0] == '.') || (blen == 2 && bname[0] == '.' && bname[1] == '.')) {
        if (owned) Py_DECREF(s_obj);
        Py_RETURN_FALSE;
    }

    for (Py_ssize_t i = 0; i < nb; i++) {
        PyObject *be = PyTuple_GET_ITEM(basenames, i);
        Py_ssize_t elen = 0;
        const char *ebuf = PyUnicode_AsUTF8AndSize(be, &elen);
        if (!ebuf) {
            PyErr_Clear();
            if (owned) Py_DECREF(s_obj);
            Py_RETURN_FALSE;
        }
        if (blen == elen && memcmp(bname, ebuf, (size_t)blen) == 0) {
            if (owned) Py_DECREF(s_obj);
            Py_RETURN_FALSE;
        }
    }

    if (owned) Py_DECREF(s_obj);
    Py_RETURN_NONE;
}

static PyObject *c_query_fdat(PyObject *self, PyObject *args) {
    PyObject *ftoc, *fdat_obj, *fine_tokens;
    Py_ssize_t n_maps;
    if (!PyArg_ParseTuple(args, "O!OOn", &PyDict_Type, &ftoc, &fdat_obj, &fine_tokens, &n_maps))
        return NULL;
    if (n_maps < 0 || n_maps > 16) {
        PyErr_SetString(PyExc_ValueError, "n_maps out of range");
        return NULL;
    }

    PyObject *out = PyTuple_New(n_maps);
    if (!out) return NULL;
    for (Py_ssize_t i = 0; i < n_maps; i++) {
        PyObject *s = PySet_New(NULL);
        if (!s) {
            Py_DECREF(out);
            return NULL;
        }
        PyTuple_SET_ITEM(out, i, s);
    }

    Py_buffer view;
    if (PyObject_GetBuffer(fdat_obj, &view, PyBUF_SIMPLE) < 0) {
        Py_DECREF(out);
        return NULL;
    }

    PyObject *it = PyObject_GetIter(fine_tokens);
    if (!it) {
        PyBuffer_Release(&view);
        Py_DECREF(out);
        return NULL;
    }

    PyObject *tok = NULL;
    while ((tok = PyIter_Next(it)) != NULL) {
        PyObject *entry = PyDict_GetItemWithError(ftoc, tok);
        Py_DECREF(tok);
        if (!entry) {
            if (PyErr_Occurred()) goto fail;
            continue;
        }
        if (!PyTuple_CheckExact(entry) || PyTuple_GET_SIZE(entry) < 2 * n_maps) {
            continue;
        }
        for (Py_ssize_t i = 0; i < n_maps; i++) {
            Py_ssize_t ln = PyLong_AsSsize_t(PyTuple_GET_ITEM(entry, 2 * i + 1));
            if (ln == -1 && PyErr_Occurred()) goto fail;
            if (ln <= 0) continue;
            Py_ssize_t off = PyLong_AsSsize_t(PyTuple_GET_ITEM(entry, 2 * i));
            if (off == -1 && PyErr_Occurred()) goto fail;
            if (off < 0 || off + ln > view.len || (ln & 3) != 0) continue;

            PyObject *target_set = PyTuple_GET_ITEM(out, i);
            const char *base = (const char *)view.buf + off;
            Py_ssize_t cnt = ln >> 2;
            for (Py_ssize_t j = 0; j < cnt; j++) {
                uint32_t v;
                memcpy(&v, base + (j << 2), 4);
                PyObject *lo = PyLong_FromUnsignedLong((unsigned long)v);
                if (!lo) goto fail;
                int rc = PySet_Add(target_set, lo);
                Py_DECREF(lo);
                if (rc < 0) goto fail;
            }
        }
    }
    if (PyErr_Occurred()) goto fail;

    Py_DECREF(it);
    PyBuffer_Release(&view);
    return out;

fail:
    Py_DECREF(it);
    PyBuffer_Release(&view);
    Py_DECREF(out);
    return NULL;
}

static inline int seq_hits_set(PyObject *s, PyObject *seq) {
    if (s == Py_None || PySet_GET_SIZE(s) == 0) return 0;
    if (PyTuple_CheckExact(seq)) {
        Py_ssize_t m = PyTuple_GET_SIZE(seq);
        for (Py_ssize_t j = 0; j < m; j++) {
            int r = PySet_Contains(s, PyTuple_GET_ITEM(seq, j));
            if (r != 0) return r;
        }
        return 0;
    }
    if (PyList_CheckExact(seq)) {
        Py_ssize_t m = PyList_GET_SIZE(seq);
        for (Py_ssize_t j = 0; j < m; j++) {
            int r = PySet_Contains(s, PyList_GET_ITEM(seq, j));
            if (r != 0) return r;
        }
        return 0;
    }
    PyObject *it = PyObject_GetIter(seq);
    if (!it) return -1;
    PyObject *item;
    while ((item = PyIter_Next(it)) != NULL) {
        int r = PySet_Contains(s, item);
        Py_DECREF(item);
        if (r != 0) {
            Py_DECREF(it);
            return r;
        }
    }
    Py_DECREF(it);
    return PyErr_Occurred() ? -1 : 0;
}

static PyObject *c_dirty_flags(PyObject *self, PyObject *args) {
    PyObject *deps_table, *dd, *dp, *dg, *dtd, *dod;
    if (!PyArg_ParseTuple(args, "O!OOOOO", &PyList_Type, &deps_table, &dd, &dp, &dg, &dtd, &dod))
        return NULL;

    PyObject *sets[5] = {dd, dp, dg, dtd, dod};
    for (int i = 0; i < 5; i++) {
        if (sets[i] != Py_None && !PyAnySet_Check(sets[i])) {
            PyErr_SetString(PyExc_TypeError, "dirty sets must be set, frozenset, or None");
            return NULL;
        }
    }

    Py_ssize_t n = PyList_GET_SIZE(deps_table);
    int has_dod = (dod != Py_None && PySet_GET_SIZE(dod) > 0);
    PyObject *dflags = PyList_New(n);
    if (!dflags) return NULL;
    PyObject *doflags = has_dod ? PyList_New(n) : NULL;
    if (has_dod && !doflags) {
        Py_DECREF(dflags);
        return NULL;
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *row = PyList_GET_ITEM(deps_table, i);
        if (!PyTuple_CheckExact(row) || PyTuple_GET_SIZE(row) < 3) {
            Py_DECREF(dflags);
            Py_XDECREF(doflags);
            PyErr_SetString(PyExc_ValueError, "deps_table row must be a 3-tuple");
            return NULL;
        }
        PyObject *ds = PyTuple_GET_ITEM(row, 0);
        PyObject *ps = PyTuple_GET_ITEM(row, 1);
        PyObject *gs = PyTuple_GET_ITEM(row, 2);

        int dirty = seq_hits_set(dd, ds);
        if (dirty < 0) goto err;
        if (!dirty) {
            dirty = seq_hits_set(dp, ps);
            if (dirty < 0) goto err;
        }
        if (!dirty) {
            dirty = seq_hits_set(dg, gs);
            if (dirty < 0) goto err;
        }
        if (!dirty) {
            dirty = seq_hits_set(dtd, ds);
            if (dirty < 0) goto err;
        }
        PyObject *bv = dirty ? Py_True : Py_False;
        Py_INCREF(bv);
        PyList_SET_ITEM(dflags, i, bv);

        if (has_dod) {
            int od = dirty;
            if (!od) {
                od = seq_hits_set(dod, ds);
                if (od < 0) goto err;
            }
            PyObject *obv = od ? Py_True : Py_False;
            Py_INCREF(obv);
            PyList_SET_ITEM(doflags, i, obv);
        }
    }

    PyObject *res = PyTuple_New(2);
    if (!res) goto err;
    PyTuple_SET_ITEM(res, 0, dflags);
    if (doflags) {
        PyTuple_SET_ITEM(res, 1, doflags);
    } else {
        Py_INCREF(dflags);
        PyTuple_SET_ITEM(res, 1, dflags);
    }
    return res;

err:
    Py_DECREF(dflags);
    Py_XDECREF(doflags);
    return NULL;
}

static PyObject *c_lb_cost(PyObject *self, PyObject *args) {
    double total_ms, max_d;
    if (!PyArg_ParseTuple(args, "dd", &total_ms, &max_d))
        return NULL;
    double best = (max_d > total_ms ? max_d : total_ms) + 2000.0;
    for (int w = 2; w <= 8; w++) {
        double m = total_ms / (double)w;
        if (m < max_d) m = max_d;
        double c = m + 2000.0 * (double)w;
        if (c < best) {
            best = c;
        } else if (m <= max_d) {
            break;
        }
    }
    return PyFloat_FromDouble(best);
}

static PyObject *c_lpt_cost(PyObject *self, PyObject *args) {
    PyObject *durs_obj;
    long long total_ms;
    if (!PyArg_ParseTuple(args, "O!L", &PyList_Type, &durs_obj, &total_ms))
        return NULL;
    Py_ssize_t n = PyList_GET_SIZE(durs_obj);
    if (n <= 0) return PyLong_FromLongLong(0);

    long long stack_d[2048];
    long long *durs = (n <= 2048) ? stack_d : (long long *)PyMem_Malloc(sizeof(long long) * (size_t)n);
    if (!durs) return PyErr_NoMemory();
    for (Py_ssize_t i = 0; i < n; i++) {
        durs[i] = PyLong_AsLongLong(PyList_GET_ITEM(durs_obj, i));
        if (durs[i] == -1 && PyErr_Occurred()) {
            if (durs != stack_d) PyMem_Free(durs);
            return NULL;
        }
    }

    long long max_d = durs[0];
    long long best_cost = total_ms + 2000LL;
    int max_w = (n < 8) ? (int)n : 8;
    for (int w = 2; w <= max_w; w++) {
        long long lb = max_d;
        long long avg = (total_ms + w - 1) / w;
        if (avg > lb) lb = avg;
        int max_q = (int)((n - 1) / w);
        if (max_q > 4) max_q = 4;
        for (int q = 1; q <= max_q; q++) {
            Py_ssize_t end = (Py_ssize_t)q * w + 1;
            long long ph = 0;
            for (Py_ssize_t k = end - q - 1; k < end; k++) {
                ph += durs[k];
            }
            if (ph > lb) lb = ph;
        }
        if (lb + 2000LL * w >= best_cost) {
            if (avg <= max_d) break;
            continue;
        }
        long long loads[8] = {0, 0, 0, 0, 0, 0, 0, 0};
        for (Py_ssize_t i = 0; i < n; i++) {
            int bi = 0;
            long long bl = loads[0];
            for (int j = 1; j < w; j++) {
                if (loads[j] < bl) {
                    bl = loads[j];
                    bi = j;
                }
            }
            loads[bi] = bl + durs[i];
        }
        long long ms = loads[0];
        for (int j = 1; j < w; j++) {
            if (loads[j] > ms) ms = loads[j];
        }
        long long cost = ms + 2000LL * w;
        if (cost < best_cost) {
            best_cost = cost;
            if (ms <= max_d) break;
        }
    }

    if (durs != stack_d) PyMem_Free(durs);
    return PyLong_FromLongLong(best_cost);
}

static PyObject *c_compare_rows(PyObject *self, PyObject *args) {
    PyObject *a_cols, *a_rows, *b_cols, *b_rows;
    if (!PyArg_ParseTuple(args, "OOOO", &a_cols, &a_rows, &b_cols, &b_rows))
        return NULL;

    int cols_eq = PyObject_RichCompareBool(a_cols, b_cols, Py_EQ);
    if (cols_eq < 0) { PyErr_Clear(); Py_INCREF(s_py); return s_py; }
    if (cols_eq == 0) { Py_INCREF(s_fail); return s_fail; }

    if (!PyList_CheckExact(a_cols) || !PyList_CheckExact(a_rows) || !PyList_CheckExact(b_rows)) {
        Py_INCREF(s_py);
        return s_py;
    }

    Py_ssize_t n_rows = PyList_GET_SIZE(a_rows);
    if (n_rows != PyList_GET_SIZE(b_rows)) {
        Py_INCREF(s_fail);
        return s_fail;
    }

    Py_ssize_t n_cols = PyList_GET_SIZE(a_cols);
    if (n_cols > 32) {
        Py_INCREF(s_py);
        return s_py;
    }
    for (Py_ssize_t i = 0; i < n_cols; i++) {
        PyObject *ci = PyList_GET_ITEM(a_cols, i);
        for (Py_ssize_t j = i + 1; j < n_cols; j++) {
            int dup = PyObject_RichCompareBool(ci, PyList_GET_ITEM(a_cols, j), Py_EQ);
            if (dup < 0) { PyErr_Clear(); Py_INCREF(s_py); return s_py; }
            if (dup == 1) { Py_INCREF(s_fallback); return s_fallback; }
        }
    }

    int need_fallback = 0;
    for (Py_ssize_t r = 0; r < n_rows; r++) {
        PyObject *ra = PyList_GET_ITEM(a_rows, r);
        PyObject *rb = PyList_GET_ITEM(b_rows, r);
        if (!PyTuple_CheckExact(ra) || !PyTuple_CheckExact(rb)) {
            Py_INCREF(s_py);
            return s_py;
        }
        Py_ssize_t la = PyTuple_GET_SIZE(ra);
        if (la != PyTuple_GET_SIZE(rb)) {
            Py_INCREF(s_fail);
            return s_fail;
        }
        for (Py_ssize_t c = 0; c < la; c++) {
            PyObject *xa = PyTuple_GET_ITEM(ra, c);
            PyObject *xb = PyTuple_GET_ITEM(rb, c);
            PyTypeObject *ta = Py_TYPE(xa);
            PyTypeObject *tb = Py_TYPE(xb);
            if (ta == tb) {
                if (xa == xb && (xa == Py_None || PyUnicode_CheckExact(xa) || PyBool_Check(xa) || PyLong_CheckExact(xa) || PyFloat_CheckExact(xa))) {
                    continue;
                }
                int eq = PyObject_RichCompareBool(xa, xb, Py_EQ);
                if (eq < 0) { PyErr_Clear(); Py_INCREF(s_py); return s_py; }
                if (eq == 1) continue;
                if (PyFloat_CheckExact(xa)) {
                    double fa = PyFloat_AS_DOUBLE(xa);
                    double fb = PyFloat_AS_DOUBLE(xb);
                    if (isnan(fa) && isnan(fb)) continue;
                    if (isfinite(fa) && isfinite(fb)) {
                        double scale = 1.0;
                        double afa = fabs(fa), afb = fabs(fb);
                        if (afa > scale) scale = afa;
                        if (afb > scale) scale = afb;
                        if (fabs(fa - fb) > 1e-2 * scale) {
                            Py_INCREF(s_fail);
                            return s_fail;
                        }
                    }
                    need_fallback = 1;
                    continue;
                }
                if (PyUnicode_CheckExact(xa) || PyBool_Check(xa) || PyLong_CheckExact(xa)) {
                    Py_INCREF(s_fail);
                    return s_fail;
                }
                Py_INCREF(s_py);
                return s_py;
            } else {
                int a_null = (xa == Py_None) || (PyFloat_CheckExact(xa) && isnan(PyFloat_AS_DOUBLE(xa)));
                int b_null = (xb == Py_None) || (PyFloat_CheckExact(xb) && isnan(PyFloat_AS_DOUBLE(xb)));
                if (a_null && b_null) continue;
                if (a_null != b_null) {
                    Py_INCREF(s_fail);
                    return s_fail;
                }
                if (PyBool_Check(xa) || PyBool_Check(xb)) {
                    need_fallback = 1;
                    continue;
                }
                if ((PyLong_CheckExact(xa) && PyFloat_CheckExact(xb)) || (PyFloat_CheckExact(xa) && PyLong_CheckExact(xb))) {
                    int eq = PyObject_RichCompareBool(xa, xb, Py_EQ);
                    if (eq < 0) { PyErr_Clear(); Py_INCREF(s_py); return s_py; }
                    if (eq == 1) continue;
                    double fa = PyFloat_AsDouble(xa);
                    double fb = PyFloat_AsDouble(xb);
                    if (PyErr_Occurred()) {
                        PyErr_Clear();
                        need_fallback = 1;
                        continue;
                    }
                    if (isfinite(fa) && isfinite(fb)) {
                        double scale = 1.0;
                        double afa = fabs(fa), afb = fabs(fb);
                        if (afa > scale) scale = afa;
                        if (afb > scale) scale = afb;
                        if (fabs(fa - fb) > 1e-2 * scale) {
                            Py_INCREF(s_fail);
                            return s_fail;
                        }
                    }
                    need_fallback = 1;
                    continue;
                }
                Py_INCREF(s_py);
                return s_py;
            }
        }
    }
    PyObject *out = need_fallback ? s_fallback : s_pass;
    Py_INCREF(out);
    return out;
}

static PyMethodDef joinkit_methods[] = {
    {"inner_hash_join_1", hjoin_one, METH_VARARGS, NULL},
    {"inner_hash_join_2", hjoin_two, METH_VARARGS, NULL},
    {"project_idxs", proj_cols, METH_VARARGS, NULL},
    {"ast_struct_sig", c_ast_sig, METH_VARARGS, NULL},
    {"unpack_expr", c_unpack_expr, METH_VARARGS, NULL},
    {"fast_open_lookup", c_open_lookup, METH_VARARGS, NULL},
    {"query_fine_fdat", c_query_fdat, METH_VARARGS, NULL},
    {"compute_dirty_flags", c_dirty_flags, METH_VARARGS, NULL},
    {"lb_cost_ms", c_lb_cost, METH_VARARGS, NULL},
    {"lpt_best_cost", c_lpt_cost, METH_VARARGS, NULL},
    {"fast_compare_rows", c_compare_rows, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef joinkit_module = {
    PyModuleDef_HEAD_INIT,
    "_joinkit",
    NULL,
    -1,
    joinkit_methods
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
    s_pass = PyUnicode_InternFromString("pass");
    s_fail = PyUnicode_InternFromString("fail");
    s_fallback = PyUnicode_InternFromString("fallback");
    s_py = PyUnicode_InternFromString("py");
    g_empty_tuple = PyTuple_New(0);
    if (!s_parent || !s_arg_key || !s_index || !s_comments || !s__meta || !s__type || !s_args ||
        !s_name || !s___qualname__ || !s__hash || !s_tag_E || !s_tag_M1 || !s_tag_M2 || !s_tag_T || !s_tag_D ||
        !s_pass || !s_fail || !s_fallback || !s_py || !g_empty_tuple)
        return NULL;
    return PyModule_Create(&joinkit_module);
}

