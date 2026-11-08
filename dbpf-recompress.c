/*
 * dbpf-recompress (Linux) -- recompress / decompress The Sims 2 .package files
 *
 * Reimplementation from the public DBPF / QFS (RefPack) format description.
 * Usage: dbpf-recompress [-d] [-n] [-1|-9] a.package b.package ...
 *   -d  decompress all resources instead of recompressing
 *   -n  dry run: do everything but do not touch the file
 *   -1  fast (shallow match search)   -9  best ratio (slow)
 *
 * Build: cc -O2 -o dbpf-recompress dbpf-recompress.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <unistd.h>
#include <sys/stat.h>

#define DIR_TYPE 0xE86B1EEFu
#define HDR_SIZE 96
#define QFS_MAX_USIZE 0xFFFFFFu

/* ------------------------------------------------------------------ */
/* little helpers                                                      */
/* ------------------------------------------------------------------ */
static uint32_t rd32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static void wr32(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }

static const char *g_err;
#define FAIL(msg) do { g_err = (msg); goto fail; } while (0)

/* ------------------------------------------------------------------ */
/* QFS decompression                                                   */
/* ------------------------------------------------------------------ */
/* src/srclen = whole stored resource (with 9 byte header). */
static int qfs_decompress(const uint8_t *src, size_t srclen, uint8_t *dst, size_t usize)
{
    size_t ip = 9, op = 0;
    if (srclen < 9 || src[4] != 0x10 || src[5] != 0xFB) return -1;
    for (;;) {
        unsigned cc, b1, b2, b3;
        size_t plain, copy, off;
        if (ip >= srclen) return -1;
        cc = src[ip++];
        if (cc < 0x80) {
            if (ip + 1 > srclen) return -1;
            b1 = src[ip++];
            plain = cc & 3;
            copy = ((cc & 0x1C) >> 2) + 3;
            off = ((cc & 0x60) << 3) + b1 + 1;
        } else if (cc < 0xC0) {
            if (ip + 2 > srclen) return -1;
            b1 = src[ip++]; b2 = src[ip++];
            plain = (b1 >> 6) & 3;
            copy = (cc & 0x3F) + 4;
            off = ((b1 & 0x3F) << 8) + b2 + 1;
        } else if (cc < 0xE0) {
            if (ip + 3 > srclen) return -1;
            b1 = src[ip++]; b2 = src[ip++]; b3 = src[ip++];
            plain = cc & 3;
            copy = ((cc & 0x0C) << 6) + b3 + 5;
            off = ((cc & 0x10) << 12) + (b1 << 8) + b2 + 1;
        } else if (cc < 0xFC) {
            plain = ((cc & 0x1F) << 2) + 4;
            copy = 0; off = 0;
        } else {
            plain = cc & 3;
            copy = 0; off = 0;
        }
        if (ip + plain > srclen || op + plain > usize) return -1;
        memcpy(dst + op, src + ip, plain);
        ip += plain; op += plain;
        if (cc >= 0xFC) break;
        if (copy) {
            if (off > op || op + copy > usize) return -1;
            for (size_t k = 0; k < copy; k++, op++) dst[op] = dst[op - off];
        }
    }
    return op == usize ? 0 : -1;
}

/* ------------------------------------------------------------------ */
/* QFS compression: hash chains + optimal (cost based) parsing         */
/* ------------------------------------------------------------------ */
#define HASH_BITS 17
static int g_chain = 128;     /* hash-chain depth: -1 fast (32) ... -9 best (512) */
#define NICE_LEN  192   /* stop searching once a match this long is found */
#define LIT_COST  129u   /* 1 byte + ~1/112 byte of E0 command overhead, x128 */
#define C1_COST   256u
#define C2_COST   384u
#define C3_COST   512u

static const uint32_t MAXD[3]  = { 1024, 16384, 131072 };
static const uint32_t MINL[3]  = { 3, 4, 5 };
static const uint32_t MAXL[3]  = { 10, 67, 1028 };
static const uint32_t CCOST[3] = { C1_COST, C2_COST, C3_COST };

static inline uint32_t hash3(const uint8_t *p)
{
    uint32_t v = p[0] | p[1] << 8 | p[2] << 16;
    return (v * 2654435761u) >> (32 - HASH_BITS);
}

typedef struct { uint32_t len, off; } Ev;   /* len==0 -> literal */

static uint8_t *emit_literals(uint8_t *o, const uint8_t *lit, size_t run, unsigned *plain_out)
{
    size_t blk = run & ~(size_t)3;
    while (blk) {
        size_t k = blk > 112 ? 112 : blk;
        *o++ = 0xE0 | ((k - 4) >> 2);
        memcpy(o, lit, k); o += k; lit += k; blk -= k;
    }
    *plain_out = run & 3;
    return o;
}

/* returns compressed size including the 9 byte header, 0 on failure.
   out must hold at least n + n/40 + 64 bytes. */
static size_t qfs_compress(const uint8_t *in, size_t n, uint8_t *out)
{
    size_t i, ret = 0;
    uint32_t *dp = NULL, *head = NULL, *prev = NULL;
    uint16_t *clen = NULL;
    uint32_t *coff = NULL;
    Ev *ev = NULL;
    uint8_t *o;
    size_t nev = 0, skip_until = 0;

    if (n == 0 || n > QFS_MAX_USIZE) return 0;
    dp   = malloc((n + 1) * sizeof *dp);
    clen = calloc(n + 1, sizeof *clen);
    coff = malloc((n + 1) * sizeof *coff);
    head = malloc((size_t)(1u << HASH_BITS) * sizeof *head);
    prev = malloc(n * sizeof *prev);
    if (!dp || !clen || !coff || !head || !prev) goto done;
    memset(head, 0xFF, (size_t)(1u << HASH_BITS) * sizeof *head);
    for (i = 0; i <= n; i++) dp[i] = 0xFFFFFFFFu;
    dp[0] = 0;

    for (i = 0; i < n; i++) {
        uint32_t cur = dp[i];
        /* literal transition */
        if (cur + LIT_COST < dp[i + 1]) { dp[i + 1] = cur + LIT_COST; clen[i + 1] = 0; }

        if (i + 3 <= n) {
            uint32_t h = hash3(in + i);
            if (i >= skip_until) {
                uint32_t bl[3] = {0, 0, 0}, bo[3] = {0, 0, 0};
                uint32_t p = head[h];
                size_t maxlen = n - i;
                int chain = g_chain;
                if (maxlen > 1028) maxlen = 1028;
                while (p != 0xFFFFFFFFu && chain-- > 0) {
                    uint32_t d = (uint32_t)i - p, l = 0;
                    if (d > MAXD[2]) break;
                    /* quick reject: a candidate must beat the best length of at least one class */
                    {
                        uint32_t minimp = 0xFFFFFFFFu;
                        for (int c = 0; c < 3; c++)
                            if (d <= MAXD[c] && bl[c] + 1 < minimp) minimp = bl[c] + 1;
                        if (minimp < 3) minimp = 3;
                        if (minimp > maxlen || in[p + minimp - 1] != in[i + minimp - 1]) { p = prev[p]; continue; }
                    }
                    while (l < maxlen && in[p + l] == in[i + l]) l++;
                    if (l >= 3) {
                        for (int c = 0; c < 3; c++)
                            if (d <= MAXD[c] && l > bl[c]) { bl[c] = l; bo[c] = d; }
                    }
                    if (l == maxlen || l >= NICE_LEN) break;
                    p = prev[p];
                }
                for (int c = 0; c < 3; c++) {
                    uint32_t L = bl[c], lo = MINL[c], hi;
                    if (L < lo) continue;
                    hi = L < MAXL[c] ? L : MAXL[c];
                    if (hi >= 96) {            /* long match: only take the full length */
                        lo = hi;
                        if (hi >= 128 && c == 2) skip_until = i + hi;
                    }
                    for (uint32_t l = lo; l <= hi; l++) {
                        uint32_t nc = cur + CCOST[c];
                        if (nc < dp[i + l]) { dp[i + l] = nc; clen[i + l] = (uint16_t)l; coff[i + l] = bo[c]; }
                    }
                }
            }
            prev[i] = head[h];
            head[h] = (uint32_t)i;
        }
    }

    /* backtrack */
    ev = malloc((n + 1) * sizeof *ev);
    if (!ev) goto done;
    {
        size_t k = n;
        while (k > 0) {
            ev[nev].len = clen[k];
            ev[nev].off = clen[k] ? coff[k] : 0;
            nev++;
            k -= clen[k] ? clen[k] : 1;
        }
    }

    /* emit */
    o = out + 9;
    {
        size_t pos = 0, litstart = 0;
        for (size_t e = nev; e-- > 0;) {
            if (ev[e].len == 0) { pos++; continue; }
            uint32_t len = ev[e].len, off = ev[e].off - 1;
            size_t run = pos - litstart;
            unsigned p;
            o = emit_literals(o, in + litstart, run, &p);
            const uint8_t *tail = in + pos - p;
            if (len <= 10 && off < 1024) {
                *o++ = ((off >> 8) << 5) | ((len - 3) << 2) | p;
                *o++ = off & 0xFF;
            } else if (len <= 67 && len >= 4 && off < 16384) {
                *o++ = 0x80 | (len - 4);
                *o++ = (p << 6) | (off >> 8);
                *o++ = off & 0xFF;
            } else {
                *o++ = 0xC0 | ((off >> 16) << 4) | (((len - 5) >> 8) << 2) | p;
                *o++ = (off >> 8) & 0xFF;
                *o++ = off & 0xFF;
                *o++ = (len - 5) & 0xFF;
            }
            memcpy(o, tail, p); o += p;
            pos += len; litstart = pos;
        }
        {
            unsigned p;
            o = emit_literals(o, in + litstart, n - litstart, &p);
            *o++ = 0xFC | p;
            memcpy(o, in + n - p, p); o += p;
        }
    }
    ret = (size_t)(o - out);
    wr32(out, (uint32_t)ret);
    out[4] = 0x10; out[5] = 0xFB;
    out[6] = n >> 16; out[7] = n >> 8; out[8] = n;
done:
    free(dp); free(clen); free(coff); free(head); free(prev); free(ev);
    return ret;
}

/* ------------------------------------------------------------------ */
/* DBPF container                                                      */
/* ------------------------------------------------------------------ */
typedef struct {
    uint32_t type, group, inst, inst2;
    uint32_t offset, size;
    uint8_t *logical;       /* uncompressed bytes (malloc'd)            */
    uint32_t lsize;
    int was_compressed;     /* stored compressed in the source          */
    uint8_t *stored;        /* what will be written (points to logical or to cbuf) */
    uint8_t *cbuf;
    uint32_t ssize;
    int store_compressed;
} Res;

typedef struct {
    uint8_t hdr[HDR_SIZE];
    Res *r;
    uint32_t n;
    int esz;                /* index entry size: 20 or 24 */
} Pkg;

static void pkg_free(Pkg *p)
{
    if (!p->r) return;
    for (uint32_t i = 0; i < p->n; i++) { free(p->r[i].logical); free(p->r[i].cbuf); }
    free(p->r);
    p->r = NULL;
}

static int cmp_key(const Res *a, const Res *b)
{
    if (a->type != b->type) return a->type < b->type ? -1 : 1;
    if (a->group != b->group) return a->group < b->group ? -1 : 1;
    if (a->inst2 != b->inst2) return a->inst2 < b->inst2 ? -1 : 1;
    if (a->inst != b->inst) return a->inst < b->inst ? -1 : 1;
    return 0;
}
typedef struct { Res k; uint32_t usize; } DirRec;
static int cmp_dir(const void *a, const void *b) { return cmp_key(&((const DirRec *)a)->k, &((const DirRec *)b)->k); }
typedef struct { Res *r; uint32_t idx; } Ref;
static int cmp_ref(const void *a, const void *b)
{
    int c = cmp_key(((const Ref *)a)->r, ((const Ref *)b)->r);
    if (c) return c;
    return ((const Ref *)a)->idx < ((const Ref *)b)->idx ? -1 : 1;
}

/* Parse buf; fills p with logical (decompressed) data. Returns 0 or -1 (g_err set). */
static int pkg_parse(const uint8_t *buf, size_t len, Pkg *p)
{
    uint32_t n, ioff, isz, i;
    DirRec *dir = NULL; uint32_t ndir = 0;
    Ref *refs = NULL;
    int dir_idx = -1;

    memset(p, 0, sizeof *p);
    if (len < HDR_SIZE || memcmp(buf, "DBPF", 4)) FAIL("not a DBPF file (missing signature)");
    if (rd32(buf + 4) != 1 || rd32(buf + 8) > 2 || rd32(buf + 32) != 7)
        FAIL("not a supported DBPF file (unrecognized version)");
    memcpy(p->hdr, buf, HDR_SIZE);
    n = rd32(buf + 36); ioff = rd32(buf + 40); isz = rd32(buf + 44);
    if ((uint64_t)ioff + isz > len) FAIL("bad DBPF file (index outside of file)");
    if (n == 0) p->esz = 24;
    else if (isz == (uint64_t)n * 24) p->esz = 24;
    else if (isz == (uint64_t)n * 20) p->esz = 20;
    else FAIL("bad DBPF file (index size mismatch)");
    p->n = n;
    p->r = calloc(n ? n : 1, sizeof(Res));
    if (!p->r) FAIL("allocation failure");

    for (i = 0; i < n; i++) {
        const uint8_t *e = buf + ioff + (size_t)i * p->esz;
        Res *r = &p->r[i];
        r->type = rd32(e); r->group = rd32(e + 4); r->inst = rd32(e + 8);
        if (p->esz == 24) { r->inst2 = rd32(e + 12); r->offset = rd32(e + 16); r->size = rd32(e + 20); }
        else              { r->inst2 = 0;            r->offset = rd32(e + 12); r->size = rd32(e + 16); }
        if ((uint64_t)r->offset + r->size > len) FAIL("bad DBPF file (bad data offset in index)");
        if (r->type == DIR_TYPE) {
            if (dir_idx >= 0) FAIL("bad DBPF file (more than one compressed file directory)");
            dir_idx = (int)i;
        }
    }

    /* duplicate check */
    refs = malloc((n ? n : 1) * sizeof *refs);
    if (!refs) FAIL("allocation failure");
    for (i = 0; i < n; i++) { refs[i].r = &p->r[i]; refs[i].idx = i; }
    qsort(refs, n, sizeof *refs, cmp_ref);
    for (i = 1; i < n; i++)
        if (cmp_key(refs[i - 1].r, refs[i].r) == 0)
            FAIL("duplicate entry in index (same type, group and instance); file left untouched");
    free(refs); refs = NULL;

    if (dir_idx >= 0) {
        const Res *d = &p->r[dir_idx];
        const uint8_t *dd = buf + d->offset;
        size_t rec = p->esz - 4, dsize = d->size;
        uint8_t *dtmp = NULL;
        /* the DIR resource itself is (almost always) stored uncompressed */
        if (dsize >= 9 && dd[4] == 0x10 && dd[5] == 0xFB && rd32(dd) <= dsize) {
            size_t us = (dd[6] << 16) | (dd[7] << 8) | dd[8];
            dtmp = malloc(us ? us : 1);
            if (!dtmp || qfs_decompress(dd, dsize, dtmp, us)) { free(dtmp); FAIL("bad DBPF file (bad compressed directory)"); }
            dd = dtmp; dsize = us;
        }
        if (dsize % rec) { free(dtmp); FAIL("bad DBPF file (bad compressed directory size)"); }
        ndir = (uint32_t)(dsize / rec);
        dir = malloc((ndir ? ndir : 1) * sizeof *dir);
        if (!dir) { free(dtmp); FAIL("allocation failure"); }
        for (i = 0; i < ndir; i++) {
            const uint8_t *e = dd + (size_t)i * rec;
            memset(&dir[i], 0, sizeof dir[i]);
            dir[i].k.type = rd32(e); dir[i].k.group = rd32(e + 4); dir[i].k.inst = rd32(e + 8);
            if (p->esz == 24) { dir[i].k.inst2 = rd32(e + 12); dir[i].usize = rd32(e + 16); }
            else dir[i].usize = rd32(e + 12);
        }
        free(dtmp);
        qsort(dir, ndir, sizeof *dir, cmp_dir);
        for (i = 1; i < ndir; i++)
            if (cmp_key(&dir[i - 1].k, &dir[i].k) == 0) FAIL("bad DBPF file (duplicate entry in compressed directory)");
    }

    for (i = 0; i < n; i++) {
        Res *r = &p->r[i];
        const uint8_t *data = buf + r->offset;
        if ((int)i == dir_idx) continue;               /* DIR is regenerated */
        DirRec key; DirRec *hit = NULL;
        if (ndir) { memset(&key, 0, sizeof key); key.k = *r; hit = bsearch(&key, dir, ndir, sizeof *dir, cmp_dir); }
        if (hit) {
            uint32_t us = hit->usize;
            if (r->size < 9 || data[4] != 0x10 || data[5] != 0xFB ||
                (uint32_t)((data[6] << 16) | (data[7] << 8) | data[8]) != us)
                FAIL("bad DBPF file (invalid compressed data)");
            r->logical = malloc(us ? us : 1);
            if (!r->logical) FAIL("allocation failure");
            if (qfs_decompress(data, r->size, r->logical, us)) FAIL("bad DBPF file (invalid compressed data)");
            r->lsize = us; r->was_compressed = 1;
        } else {
            r->logical = malloc(r->size ? r->size : 1);
            if (!r->logical) FAIL("allocation failure");
            memcpy(r->logical, data, r->size);
            r->lsize = r->size;
        }
    }
    free(dir);
    return 0;
fail:
    free(dir); free(refs);
    pkg_free(p);
    return -1;
}

/* Serialise. mode_decompress: store everything raw. Returns malloc'd buffer. */
static uint8_t *pkg_build(Pkg *p, int decompress, size_t *outlen)
{
    uint32_t i, ncomp = 0, nout = 0;
    size_t total = HDR_SIZE, pos;
    int esz = p->esz;
    size_t rec = esz - 4;
    uint8_t *out = NULL;

    for (i = 0; i < p->n; i++) {
        Res *r = &p->r[i];
        if (r->type == DIR_TYPE) continue;
        free(r->cbuf); r->cbuf = NULL;
        r->stored = r->logical; r->ssize = r->lsize; r->store_compressed = 0;
        if (!decompress && r->lsize >= 16 && r->lsize <= QFS_MAX_USIZE) {
            uint8_t *cb = malloc((size_t)r->lsize + r->lsize / 40 + 64);
            if (cb) {
                size_t cl = qfs_compress(r->logical, r->lsize, cb);
                if (cl && cl < r->lsize) { r->cbuf = cb; r->stored = cb; r->ssize = (uint32_t)cl; r->store_compressed = 1; }
                else free(cb);
            }
        }
        if (r->store_compressed) ncomp++;
        total += r->ssize; nout++;
    }
    if (ncomp) { total += (size_t)ncomp * rec; nout++; }
    total += (size_t)nout * esz;
    if (total > 0xFFFFFFF0u) { g_err = "DBPF file too large"; return NULL; }
    out = malloc(total);
    if (!out) { g_err = "allocation failure"; return NULL; }
    memcpy(out, p->hdr, HDR_SIZE);
    pos = HDR_SIZE;
    uint32_t *offs = malloc((p->n + 1) * sizeof *offs);
    if (!offs) { free(out); g_err = "allocation failure"; return NULL; }
    for (i = 0; i < p->n; i++) {
        Res *r = &p->r[i];
        if (r->type == DIR_TYPE) continue;
        offs[i] = (uint32_t)pos;
        memcpy(out + pos, r->stored, r->ssize); pos += r->ssize;
    }
    uint32_t dir_off = (uint32_t)pos, dir_size = (uint32_t)(ncomp * rec);
    if (ncomp) {
        uint8_t *d = out + pos;
        for (i = 0; i < p->n; i++) {
            Res *r = &p->r[i];
            if (r->type == DIR_TYPE || !r->store_compressed) continue;
            wr32(d, r->type); wr32(d + 4, r->group); wr32(d + 8, r->inst);
            if (esz == 24) { wr32(d + 12, r->inst2); wr32(d + 16, r->lsize); }
            else wr32(d + 12, r->lsize);
            d += rec;
        }
        pos += dir_size;
    }
    uint32_t ioff = (uint32_t)pos;
    for (i = 0; i < p->n; i++) {
        Res *r = &p->r[i];
        uint8_t *e = out + pos;
        if (r->type == DIR_TYPE) continue;
        wr32(e, r->type); wr32(e + 4, r->group); wr32(e + 8, r->inst);
        if (esz == 24) { wr32(e + 12, r->inst2); wr32(e + 16, offs[i]); wr32(e + 20, r->ssize); }
        else { wr32(e + 12, offs[i]); wr32(e + 16, r->ssize); }
        pos += esz;
    }
    if (ncomp) {
        uint8_t *e = out + pos;
        wr32(e, DIR_TYPE);
        wr32(e + 4, 0xE86B1EEFu);
        wr32(e + 8, 0x286B1F03u);
        if (esz == 24) { wr32(e + 12, 0); wr32(e + 16, dir_off); wr32(e + 20, dir_size); }
        else { wr32(e + 12, dir_off); wr32(e + 16, dir_size); }
        pos += esz;
    }
    free(offs);
    wr32(out + 36, nout);
    wr32(out + 40, ioff);
    wr32(out + 44, (uint32_t)(pos - ioff));
    wr32(out + 48, 0); wr32(out + 52, 0); wr32(out + 56, 0);   /* no hole index */
    if (esz == 24 && rd32(out + 60) == 0) wr32(out + 60, 1);
    if (esz == 20) wr32(out + 60, 0);
    *outlen = pos;
    return out;
}

/* ------------------------------------------------------------------ */
/* file handling                                                       */
/* ------------------------------------------------------------------ */
static uint8_t *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    uint8_t *b; long sz;
    if (!f) { g_err = "open failed"; return NULL; }
    fseek(f, 0, SEEK_END); sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (sz < 0 || (unsigned long)sz > 0xFFFFFFF0ul) { fclose(f); g_err = "DBPF file too large"; return NULL; }
    b = malloc(sz ? sz : 1);
    if (!b) { fclose(f); g_err = "allocation failure"; return NULL; }
    if (fread(b, 1, sz, f) != (size_t)sz) { fclose(f); free(b); g_err = "read error"; return NULL; }
    fclose(f);
    *len = (size_t)sz;
    return b;
}

static int process(const char *path, int decompress, int dry)
{
    size_t len = 0, olen = 0;
    uint8_t *buf = NULL, *out = NULL;
    Pkg p, v;
    int rc = 1, have_p = 0;
    char tmp[4096];
    struct stat st;

    memset(&p, 0, sizeof p); memset(&v, 0, sizeof v);
    printf("%s\n", path);
    buf = read_file(path, &len);
    if (!buf) { printf("  *** %s\n", g_err); return 1; }
    if (pkg_parse(buf, len, &p)) { printf("  *** %s\n", g_err); free(buf); return 1; }
    have_p = 1;
    out = pkg_build(&p, decompress, &olen);
    if (!out) { printf("  *** %s\n", g_err); goto end; }

    /* verify: re-parse the new image and compare every resource */
    if (pkg_parse(out, olen, &v)) { printf("  *** verify failed: %s\n", g_err); goto end; }
    {
        uint32_t i, j = 0, cnt_old = 0;
        for (i = 0; i < p.n; i++) if (p.r[i].type != DIR_TYPE) cnt_old++;
        uint32_t cnt_new = 0;
        for (i = 0; i < v.n; i++) if (v.r[i].type != DIR_TYPE) cnt_new++;
        if (cnt_old != cnt_new) { printf("  *** verify failed: entry count mismatch\n"); goto end; }
        for (i = 0; i < p.n; i++) {
            if (p.r[i].type == DIR_TYPE) continue;
            while (v.r[j].type == DIR_TYPE) j++;
            Res *a = &p.r[i], *b = &v.r[j++];
            if (a->type != b->type || a->group != b->group || a->inst != b->inst || a->inst2 != b->inst2)
                { printf("  *** verify failed: file metadata mismatch\n"); goto end; }
            if (a->lsize != b->lsize || memcmp(a->logical, b->logical, a->lsize))
                { printf("  *** verify failed: old and new files are different\n"); goto end; }
        }
    }

    if (!decompress && olen >= len) {
        printf("  no gain (%zu -> %zu bytes), file left untouched\n", len, olen);
        rc = 0; goto end;
    }
    if (dry) { printf("  %zu -> %zu bytes (dry run)\n", len, olen); rc = 0; goto end; }

    snprintf(tmp, sizeof tmp, "%s.$new", path);
    {
        FILE *f = fopen(tmp, "wb");
        if (!f) { printf("  *** output open failed: %s\n", strerror(errno)); goto end; }
        if (fwrite(out, 1, olen, f) != olen || fflush(f) || fclose(f)) {
            printf("  *** rewrite failed: %s\n", strerror(errno)); remove(tmp); goto end;
        }
    }
    if (stat(path, &st) == 0) chmod(tmp, st.st_mode & 07777);
    if (rename(tmp, path)) {
        printf("  *** renaming \"%s\" to \"%s\" failed: %s\n", tmp, path, strerror(errno));
        remove(tmp); goto end;
    }
    printf("  %zu -> %zu bytes (%.1f%%)\n", len, olen, len ? 100.0 * (double)olen / (double)len : 100.0);
    rc = 0;
end:
    free(buf); free(out);
    if (have_p) pkg_free(&p);
    pkg_free(&v);
    return rc;
}

int main(int argc, char **argv)
{
    int decompress = 0, dry = 0, fails = 0, i = 1;
    for (; i < argc && argv[i][0] == '-' && argv[i][1]; i++) {
        if (!strcmp(argv[i], "-d")) decompress = 1;
        else if (!strcmp(argv[i], "-n")) dry = 1;
        else if (!strcmp(argv[i], "-1")) g_chain = 32;
        else if (!strcmp(argv[i], "-9")) g_chain = 512;
        else if (!strcmp(argv[i], "--")) { i++; break; }
        else { i = argc; break; }
    }
    if (i >= argc) {
        fprintf(stderr, "usage: dbpf-recompress [-d] [-n] [-1|-9] a.package b.package ...\n"
                        "  -d  decompress all files instead of recompressing\n"
                        "  -n  dry run (do not modify any file)\n"
                        "  -1  fast compression   -9  best compression (slower)\n");
        return 2;
    }
    setvbuf(stdout, NULL, _IOLBF, 0);
    for (; i < argc; i++) fails += process(argv[i], decompress, dry);
    return fails ? 1 : 0;
}
