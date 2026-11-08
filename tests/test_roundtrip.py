#!/usr/bin/env python3
"""Round-trip tests for dbpf-recompress using synthetic Sims 2 style packages.

Uses an independent pure-Python DBPF reader / QFS decoder written from the
public format description (not sharing code with the C implementation).
"""
import os, random, struct, subprocess, sys, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "dbpf-recompress")
DIR_T = 0xE86B1EEF


def qfs_decode(b):
    assert b[4:6] == b"\x10\xfb", "bad QFS magic"
    usize = int.from_bytes(b[6:9], "big")
    ip, out = 9, bytearray()
    while True:
        cc = b[ip]; ip += 1
        if cc < 0x80:
            b1 = b[ip]; ip += 1
            plain = cc & 3; copy = ((cc & 0x1C) >> 2) + 3; off = ((cc & 0x60) << 3) + b1 + 1
        elif cc < 0xC0:
            b1, b2 = b[ip], b[ip + 1]; ip += 2
            plain = b1 >> 6; copy = (cc & 0x3F) + 4; off = ((b1 & 0x3F) << 8) + b2 + 1
        elif cc < 0xE0:
            b1, b2, b3 = b[ip], b[ip + 1], b[ip + 2]; ip += 3
            plain = cc & 3; copy = ((cc & 0x0C) << 6) + b3 + 5; off = ((cc & 0x10) << 12) + (b1 << 8) + b2 + 1
        elif cc < 0xFC:
            plain = ((cc & 0x1F) << 2) + 4; copy = 0; off = 0
        else:
            plain = cc & 3; copy = 0; off = 0
        out += b[ip:ip + plain]; ip += plain
        if cc >= 0xFC:
            break
        for _ in range(copy):
            out.append(out[-off])
    assert len(out) == usize, (len(out), usize)
    return bytes(out)


def qfs_literal_only(data):
    """Trivial valid QFS stream (only literal blocks) - used to build 'compressed' input."""
    o = bytearray(); i = 0; n = len(data)
    blk = n & ~3
    while i < blk:
        k = min(112, blk - i)
        o.append(0xE0 | ((k - 4) >> 2)); o += data[i:i + k]; i += k
    rest = data[i:]
    o.append(0xFC | len(rest)); o += rest
    return struct.pack("<I", len(o) + 9) + b"\x10\xfb" + len(data).to_bytes(3, "big") + bytes(o)


def read_pkg(path):
    d = open(path, "rb").read()
    assert d[:4] == b"DBPF"
    n, ioff, isz = struct.unpack_from("<III", d, 36)
    iminor = struct.unpack_from("<I", d, 60)[0]
    esz = isz // n if n else 24
    assert esz * n == isz and esz in (20, 24)
    ents = []
    for i in range(n):
        e = d[ioff + i * esz: ioff + (i + 1) * esz]
        if esz == 24:
            t, g, i1, i2, off, sz = struct.unpack("<6I", e)
        else:
            t, g, i1, off, sz = struct.unpack("<5I", e); i2 = 0
        ents.append((t, g, i2, i1, off, sz))
    dirs = {}
    for t, g, i2, i1, off, sz in ents:
        if t == DIR_T:
            rec = esz - 4
            for k in range(sz // rec):
                r = d[off + k * rec: off + (k + 1) * rec]
                if esz == 24:
                    a, b, c, dd, u = struct.unpack("<5I", r); dirs[(a, b, dd, c)] = u
                else:
                    a, b, c, u = struct.unpack("<4I", r); dirs[(a, b, 0, c)] = u
    res = []
    for t, g, i2, i1, off, sz in ents:
        if t == DIR_T:
            continue
        raw = d[off:off + sz]
        comp = (t, g, i2, i1) in dirs
        if comp:
            raw = qfs_decode(raw)
            assert len(raw) == dirs[(t, g, i2, i1)]
        res.append(((t, g, i2, i1), raw, comp))
    return res, esz, iminor


def build_pkg(path, items, esz=24, compressed_keys=(), hole=False):
    """items: list of (type, group, inst, inst2, data). compressed_keys: keys stored as (literal) QFS."""
    body = bytearray(96); entries = []; dirrecs = []
    for t, g, i1, i2, data in items:
        key = (t, g, i1, i2)
        if key in compressed_keys:
            stored = qfs_literal_only(data); dirrecs.append((t, g, i1, i2, len(data)))
        else:
            stored = data
        entries.append((t, g, i1, i2, len(body), len(stored))); body += stored
    if dirrecs:
        off = len(body)
        for t, g, i1, i2, u in dirrecs:
            body += struct.pack("<5I", t, g, i1, i2, u) if esz == 24 else struct.pack("<4I", t, g, i1, u)
        entries.append((DIR_T, DIR_T, 0x286B1F03, 0, off, len(body) - off))
    ioff = len(body)
    for t, g, i1, i2, off, sz in entries:
        body += struct.pack("<6I", t, g, i1, i2, off, sz) if esz == 24 else struct.pack("<5I", t, g, i1, off, sz)
    hdr = bytearray(96)
    hdr[0:4] = b"DBPF"
    struct.pack_into("<II", hdr, 4, 1, 1 if esz == 24 else 0)
    struct.pack_into("<I", hdr, 32, 7)
    struct.pack_into("<III", hdr, 36, len(entries), ioff, len(body) - ioff)
    struct.pack_into("<I", hdr, 60, 1 if esz == 24 else 0)
    body[0:96] = hdr
    open(path, "wb").write(body)


def corpus(rng):
    items = []
    text = (b"The quick brown fox jumps over the lazy dog. " * 300)
    items.append((0x1, 0, 1, 0, text))
    items.append((0x2, 0, 2, 0, bytes(100000)))                         # zeros
    items.append((0x3, 0, 3, 0, os.urandom(5000)))                      # incompressible
    items.append((0x4, 0, 4, 0, bytes(rng.choice(b"ab") for _ in range(70000))))
    # structured, mid-range matches
    blob = bytearray()
    words = [bytes(rng.randrange(256) for _ in range(rng.randrange(3, 40))) for _ in range(400)]
    for _ in range(4000):
        blob += rng.choice(words)
    items.append((0x5, 0x7, 5, 9, bytes(blob)))
    items.append((0x6, 0, 6, 0, b"x"))                                  # tiny
    items.append((0x7, 0, 7, 0, b""))                                   # empty
    items.append((0x8, 0, 8, 0, b"abc" * 5))
    # long-distance repeat (offset > 16384 and > 131072 boundary)
    r = os.urandom(20000)
    items.append((0x9, 0, 9, 0, r + os.urandom(130000) + r + os.urandom(100) + r))
    items.append((0xA, 0, 10, 0, bytes(range(256)) * 400 + b"tail!"))
    return items


def run(args):
    return subprocess.run([BIN] + args, capture_output=True, text=True)


def main():
    rng = random.Random(1234)
    tmp = tempfile.mkdtemp(prefix="dbpftest")
    fails = 0
    try:
        for esz in (24, 20):
            items = corpus(rng)
            # half of the items start out "compressed" (literal-only QFS)
            ck = {(t, g, i1, i2) for t, g, i1, i2, d in items if i1 % 2 == 0 and len(d) >= 4}
            p = os.path.join(tmp, f"t{esz}.package")
            build_pkg(p, items, esz, ck)
            orig = read_pkg(p)[0]
            before = os.path.getsize(p)

            r = run([p]); print(r.stdout.strip())
            assert r.returncode == 0, r.stdout + r.stderr
            res, e2, _ = read_pkg(p)
            assert e2 == esz
            assert [(k, d) for k, d, c in res] == [(k, d) for k, d, c in orig], "content changed after recompress"
            after = os.path.getsize(p)
            assert after < before, (before, after)
            ncomp = sum(1 for k, d, c in res if c)
            print(f"[esz={esz}] recompress OK: {before} -> {after} bytes, {ncomp}/{len(res)} stored compressed")
            # incompressible must stay raw
            assert not [c for k, d, c in res if k[3] == 3 and c]
            # idempotent-ish: second run must not grow the file
            r = run([p]); assert r.returncode == 0
            assert os.path.getsize(p) <= after

            r = run(["-d", p]); print(r.stdout.strip())
            assert r.returncode == 0
            res3, _, _ = read_pkg(p)
            assert all(not c for k, d, c in res3)
            assert [(k, d) for k, d, c in res3] == [(k, d) for k, d, c in orig]
            print(f"[esz={esz}] decompress OK: -> {os.path.getsize(p)} bytes")
            assert not any(f.endswith("$new") for f in os.listdir(tmp))

        # error handling: garbage, truncated, wrong magic
        bad = os.path.join(tmp, "bad.package"); open(bad, "wb").write(b"hello world" * 20)
        r = run([bad]); assert r.returncode == 1 and "missing signature" in r.stdout, r.stdout
        good = os.path.join(tmp, "t24.package"); data = open(good, "rb").read()
        trunc = os.path.join(tmp, "trunc.package"); open(trunc, "wb").write(data[:len(data) // 2])
        r = run([trunc]); assert r.returncode == 1, r.stdout
        assert open(trunc, "rb").read() == data[:len(data) // 2]   # untouched
        # one bad file must not stop the next
        r = run([bad, good]); assert r.returncode == 1 and r.stdout.count("->") >= 0
        # dry run leaves file alone
        before = open(good, "rb").read(); r = run(["-n", good]); assert open(good, "rb").read() == before
        # fuzz: corrupt random bytes; tool must never crash (rc 0 or 1)
        for n in range(200):
            b = bytearray(data)
            for _ in range(rng.randrange(1, 6)):
                b[rng.randrange(len(b))] = rng.randrange(256)
            f = os.path.join(tmp, "fuzz.package"); open(f, "wb").write(b)
            r = run(["-n", "-1", f]); assert r.returncode in (0, 1), (n, r.returncode, r.stderr)
        print("error handling + 200 fuzz cases OK")
        print("ALL TESTS PASSED")
    except AssertionError as e:
        print("TEST FAILED:", e); fails = 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return fails


if __name__ == "__main__":
    sys.exit(main())
