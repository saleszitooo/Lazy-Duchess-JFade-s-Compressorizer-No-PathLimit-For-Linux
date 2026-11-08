#!/usr/bin/env python3
"""Headless test of compressorizer.py: Worker + a full run of main() against a mocked tkinter."""
import os, queue, shutil, sys, tempfile, types
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, ".."))
import compressorizer as C
import test_roundtrip as T

tmp = tempfile.mkdtemp(prefix="guitest")
try:
    sub = os.path.join(tmp, "Sub"); os.makedirs(sub)
    paths = []
    for d, name in ((tmp, "a.package"), (sub, "B.PACKAGE"), (sub, "c.Package")):
        p = os.path.join(d, name)
        T.build_pkg(p, [(1, 0, 1, 0, b"hello sims " * 5000), (2, 0, 2, 0, bytes(30000))])
        paths.append(p)
    open(os.path.join(tmp, "notes.txt"), "w").write("x")
    open(os.path.join(sub, "bad.package"), "wb").write(b"not a package" * 10)

    assert [os.path.basename(p) for p in C.collect_packages(tmp, False)] == ["a.package"]
    assert len(C.collect_packages(tmp, True)) == 4          # case-insensitive, recursive
    binary = C.find_binary(); assert binary

    # --- Worker -------------------------------------------------------------
    q = queue.Queue()
    w = C.Worker(binary, C.collect_packages(tmp, True), False, "-1", q)
    sizes0 = {p: os.path.getsize(p) for p in w.files}
    w.run()
    msgs = []
    while not q.empty(): msgs.append(q.get())
    done = msgs[-1]
    assert done[0] == "done" and done[1] == 4 and not done[5], done
    assert len(done[4]) == 1 and done[4][0][0].endswith("bad.package"), done[4]
    assert done[3] > 0
    for p in paths: assert os.path.getsize(p) < sizes0[p]
    print("Worker OK: saved", done[3], "bytes;", "error reported for bad.package:", done[4][0][1])

    # --- main() against mocked tkinter -------------------------------------------
    tk = mock.MagicMock(); ttk = mock.MagicMock(); fd = mock.MagicMock(); mb = mock.MagicMock()
    tk.ttk, tk.filedialog, tk.messagebox = ttk, fd, mb
    fd.askopenfilenames.return_value = tuple(paths)
    mb.askyesno.return_value = True
    class V:
        def __init__(self, value=None): self.v = value
        def get(self): return self.v
        def set(self, v): self.v = v
    tk.StringVar = tk.BooleanVar = V
    root = tk.Tk.return_value
    after_cbs = []
    root.after.side_effect = lambda ms, fn=None, *a: after_cbs.append(fn)
    # Treeview: remember inserted rows
    cmds = []
    ttk.Button.return_value.configure.side_effect = lambda **kw: cmds.append(kw.get("command")) if "command" in kw else None
    with mock.patch.dict(sys.modules, {"tkinter": tk, "tkinter.ttk": ttk, "tkinter.filedialog": fd, "tkinter.messagebox": mb}):
        old = sys.argv; sys.argv = ["x"]
        C.main()
        sys.argv = old
    on_add, on_dir, on_rm, on_clear, on_go, on_stop = [c for c in cmds if c]
    for p in paths: T.build_pkg(p, [(1, 0, 1, 0, b"hello sims " * 5000)])   # reset files
    on_add()
    assert ttk.Treeview.return_value.insert.call_count == 3
    on_go()
    import time
    for _ in range(100):
        for cb in list(after_cbs):
            if cb: cb()
        if mb.showinfo.call_args and "Operation Complete" in str(mb.showinfo.call_args): break
        time.sleep(0.05)
    assert "Operation Complete" in str(mb.showinfo.call_args), mb.showinfo.call_args
    print("main() flow OK:", mb.showinfo.call_args[0][1].replace("\n", " "))
    print("GUI LOGIC TESTS PASSED")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
