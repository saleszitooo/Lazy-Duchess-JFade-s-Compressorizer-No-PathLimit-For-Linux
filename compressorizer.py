#!/usr/bin/env python3
"""The Compressorizer (Linux port) - GUI for dbpf-recompress.

Recompresses The Sims 2 .package files with QFS to save disk space.
Needs: python3 + tkinter (Debian/Ubuntu: sudo apt install python3-tk)
and the dbpf-recompress binary (run `make` once).
"""
import os
import queue
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
TITLE = "The Compressorizer: I Eat Sims For Breakfast! (Linux)"


# --------------------------------------------------------------------------
# Logic (no Tk here so it can be tested headless)
# --------------------------------------------------------------------------
def find_binary():
    for cand in (os.path.join(HERE, "dbpf-recompress"), shutil.which("dbpf-recompress")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def collect_packages(folder, recursive):
    """All *.package under folder (case-insensitive: Linux file systems are case-sensitive)."""
    found = []
    if recursive:
        for root, _dirs, files in os.walk(folder):
            found += [os.path.join(root, f) for f in files if f.lower().endswith(".package")]
    else:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if f.lower().endswith(".package") and os.path.isfile(p):
                found.append(p)
    return sorted(found, key=str.lower)


def kb(nbytes):
    return nbytes // 1024


def mb(nbytes):
    return nbytes / (1024 * 1024)


class Worker(threading.Thread):
    """Runs dbpf-recompress on each file, one at a time, reporting through a queue."""

    def __init__(self, binary, files, decompress, level, out_q):
        super().__init__(daemon=True)
        self.binary, self.files, self.decompress, self.level, self.q = binary, files, decompress, level, out_q
        self.stop_flag = threading.Event()
        self.proc = None

    def stop(self):
        self.stop_flag.set()
        p = self.proc
        if p and p.poll() is None:
            p.terminate()          # safe: the tool writes to "<file>.$new" and renames at the end

    def run(self):
        saved = 0
        errors = []
        done = 0
        for idx, path in enumerate(self.files):
            if self.stop_flag.is_set():
                break
            try:
                before = os.path.getsize(path)
            except OSError as e:
                self.q.put(("file", idx, None, "erro: %s" % e))
                errors.append((path, str(e)))
                continue
            cmd = [self.binary]
            if self.decompress:
                cmd.append("-d")
            elif self.level:
                cmd.append(self.level)
            cmd += ["--", path]
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             errors="replace")
                out, _ = self.proc.communicate()
                rc = self.proc.returncode
            except OSError as e:
                out, rc = str(e), 99
            if self.stop_flag.is_set():
                break
            try:
                after = os.path.getsize(path)
            except OSError:
                after = before
            if rc != 0:
                msg = "\n".join(l.strip() for l in out.splitlines() if "***" in l) or out.strip() or "erro"
                errors.append((path, msg))
                self.q.put(("file", idx, None, "ERRO"))
            else:
                saved += before - after
                self.q.put(("file", idx, after, None))
            done += 1
            self.q.put(("progress", done, len(self.files), saved))
        self.q.put(("done", done, len(self.files), saved, errors, self.stop_flag.is_set()))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("1000x560")
    root.minsize(760, 420)
    for name in ("compressorizer-48.png", "compressorizer.png"):
        icon = os.path.join(HERE, name)
        if os.path.isfile(icon):
            try:
                root.iconphoto(True, tk.PhotoImage(file=icon))
                break
            except tk.TclError:
                pass

    binary = find_binary()
    files = []                      # absolute paths, in list order
    sizes = {}                      # path -> bytes before
    state = {"worker": None}
    q = queue.Queue()

    recursive = tk.BooleanVar(value=False)
    decompress = tk.BooleanVar(value=False)
    level = tk.StringVar(value="Normal")

    # ---- list ------------------------------------------------------------
    frame = ttk.Frame(root, padding=(10, 8, 10, 0))
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="List of Files to Compress").pack(anchor="w")
    tv_frame = ttk.Frame(frame)
    tv_frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(tv_frame, columns=("path", "before", "after"), show="headings", selectmode="extended")
    tree.heading("path", text="File")
    tree.heading("before", text="Before (KB)")
    tree.heading("after", text="After (KB)")
    tree.column("path", width=640, anchor="w")
    tree.column("before", width=100, anchor="e", stretch=False)
    tree.column("after", width=100, anchor="e", stretch=False)
    ys = ttk.Scrollbar(tv_frame, orient="vertical", command=tree.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tree.grid(row=0, column=0, sticky="nsew"); ys.grid(row=0, column=1, sticky="ns")
    tv_frame.rowconfigure(0, weight=1); tv_frame.columnconfigure(0, weight=1)
    xs.pack(fill="x")

    # ---- totals ------------------------------------------------------------
    tot = ttk.Frame(root, padding=(10, 4))
    tot.pack(fill="x")
    lbl_files = tk.StringVar(value="Total Files: 0")
    lbl_size = tk.StringVar(value="Total Size (In MB): 0")
    lbl_after = tk.StringVar(value="")
    ttk.Label(tot, textvariable=lbl_files).pack(side="left")
    ttk.Label(tot, textvariable=lbl_after).pack(side="right")
    ttk.Label(tot, textvariable=lbl_size).pack(side="right", padx=(0, 40))

    # ---- controls ----------------------------------------------------------
    ctl = ttk.Frame(root, padding=(10, 4, 10, 10))
    ctl.pack(fill="x")
    left = ttk.Frame(ctl); left.pack(side="left", fill="y")
    mid = ttk.Frame(ctl); mid.pack(side="left", fill="x", expand=True, padx=12)
    right = ttk.Frame(ctl); right.pack(side="left", fill="y")

    btn_add = ttk.Button(left, text="Add Files to List")
    btn_dir = ttk.Button(left, text="Add Folder to List")
    btn_add.grid(row=0, column=0, sticky="ew", pady=2); btn_dir.grid(row=1, column=0, sticky="ew", pady=2)
    ttk.Checkbutton(left, text="Scan Subfolders", variable=recursive).grid(row=2, column=0, sticky="w")
    ttk.Checkbutton(left, text="Decompress instead", variable=decompress).grid(row=3, column=0, sticky="w")
    lvl = ttk.Frame(left); lvl.grid(row=4, column=0, sticky="w")
    ttk.Label(lvl, text="Level:").pack(side="left")
    ttk.Combobox(lvl, textvariable=level, values=["Fast", "Normal", "Best (slow)"], width=11,
                 state="readonly").pack(side="left", padx=4)

    prog_row = ttk.Frame(mid); prog_row.pack(fill="x")
    lbl_prog = tk.StringVar(value="0  Out Of  0")
    ttk.Label(prog_row, textvariable=lbl_prog).pack()
    bar = ttk.Progressbar(mid, mode="determinate"); bar.pack(fill="x", pady=2)
    btns = ttk.Frame(mid); btns.pack(pady=6)
    btn_go = ttk.Button(btns, text="GO!", width=12)
    btn_stop = ttk.Button(btns, text="STOP!", width=12, state="disabled")
    btn_go.pack(side="left", padx=6); btn_stop.pack(side="left", padx=6)

    btn_rm = ttk.Button(right, text="Remove Selected File From List")
    btn_clear = ttk.Button(right, text="Clear Files List")
    btn_rm.pack(fill="x", pady=2); btn_clear.pack(fill="x", pady=2)
    lbl_saved = tk.StringVar(value="Space Saved (In MB): 0")
    ttk.Label(right, textvariable=lbl_saved).pack(pady=6)

    # ---- helpers -----------------------------------------------------------
    def refresh_totals():
        lbl_files.set("Total Files: %d" % len(files))
        lbl_size.set("Total Size (In MB): %d" % round(mb(sum(sizes.get(p, 0) for p in files))))

    def add_paths(paths):
        known = set(files)
        for p in paths:
            p = os.path.abspath(p)
            if p in known or not os.path.isfile(p):
                continue
            known.add(p)
            files.append(p)
            sizes[p] = os.path.getsize(p)
            tree.insert("", "end", iid=str(len(files) - 1), values=(p, kb(sizes[p]), ""))
        refresh_totals()

    def rebuild():
        tree.delete(*tree.get_children())
        for i, p in enumerate(files):
            tree.insert("", "end", iid=str(i), values=(p, kb(sizes[p]), ""))
        refresh_totals()

    def busy():
        w = state["worker"]
        return w is not None and w.is_alive()

    def on_add():
        if busy():
            return
        sel = filedialog.askopenfilenames(title="Select .package files",
                                          filetypes=[("Sims 2 packages", "*.package *.Package *.PACKAGE"),
                                                     ("All files", "*")])
        add_paths(sel)

    def on_dir():
        if busy():
            return
        d = filedialog.askdirectory(title="Select a folder with .package files", mustexist=True)
        if d:
            found = collect_packages(d, recursive.get())
            if not found:
                messagebox.showinfo(TITLE, "No .package files found in that folder.")
            add_paths(found)

    def on_remove():
        if busy():
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(TITLE, "Please Select An Item First!")
            return
        drop = {int(i) for i in sel}
        keep = [p for i, p in enumerate(files) if i not in drop]
        files[:] = keep
        rebuild()

    def on_clear():
        if busy():
            return
        files.clear(); sizes.clear(); sizes_after.clear()
        rebuild()
        bar["value"] = 0
        lbl_prog.set("0  Out Of  0"); lbl_saved.set("Space Saved (In MB): 0"); lbl_after.set("")

    def set_busy(b):
        for w in (btn_add, btn_dir, btn_rm, btn_clear, btn_go):
            w.configure(state="disabled" if b else "normal")
        btn_stop.configure(state="normal" if b else "disabled")

    def on_go():
        if busy():
            return
        if not files:
            messagebox.showinfo(TITLE, "Please add some files first!")
            return
        if not binary:
            messagebox.showerror(TITLE, "dbpf-recompress was not found.\nRun `make` in the program folder first.")
            return
        if not messagebox.askyesno(TITLE, "This rewrites the .package files in place.\n"
                                          "Make sure you have a backup of your Downloads folder.\n\nContinue?"):
            return
        for i in range(len(files)):
            tree.set(str(i), "after", "")
        bar.configure(maximum=len(files), value=0)
        lbl_prog.set("0  Out Of  %d" % len(files))
        lbl_saved.set("Space Saved (In MB): 0")
        lbl_after.set("")
        lv = {"Fast": "-1", "Normal": "", "Best (slow)": "-9"}[level.get()]
        w = Worker(binary, list(files), decompress.get(), lv, q)
        state["worker"] = w
        set_busy(True)
        w.start()
        root.after(60, pump)

    def on_stop():
        w = state["worker"]
        if w:
            w.stop()
            btn_stop.configure(state="disabled")

    def pump():
        total_after = None
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "file":
                    _, idx, after, err = msg
                    if idx < len(files):
                        tree.set(str(idx), "after", err if err else kb(after))
                        if after is not None:
                            sizes_after[files[idx]] = after
                elif kind == "progress":
                    _, done, total, saved = msg
                    bar["value"] = done
                    lbl_prog.set("%d  Out Of  %d" % (done, total))
                    lbl_saved.set("Space Saved (In MB): %.1f" % mb(saved))
                elif kind == "done":
                    _, done, total, saved, errors, stopped = msg
                    set_busy(False)
                    lbl_after.set("%d" % round(mb(sum(sizes_after.get(p, sizes.get(p, 0)) for p in files))))
                    if errors:
                        detail = "\n".join("%s\n   %s" % (os.path.basename(p), m) for p, m in errors[:10])
                        more = "\n... and %d more" % (len(errors) - 10) if len(errors) > 10 else ""
                        messagebox.showwarning(TITLE, "%d file(s) could not be processed and were left untouched:"
                                                      "\n\n%s%s" % (len(errors), detail, more))
                    if stopped:
                        messagebox.showinfo(TITLE, "Stopped. %d of %d files processed." % (done, total))
                    else:
                        word = "increased" if saved < 0 else "saved"
                        messagebox.showinfo(TITLE, "Operation Complete!\n\n%.1f Megabytes of storage %s!"
                                            % (abs(mb(saved)), word))
                    return
        except queue.Empty:
            pass
        root.after(60, pump)

    sizes_after = {}
    btn_add.configure(command=on_add); btn_dir.configure(command=on_dir)
    btn_rm.configure(command=on_remove); btn_clear.configure(command=on_clear)
    btn_go.configure(command=on_go); btn_stop.configure(command=on_stop)

    # open files / folders passed on the command line
    for arg in sys.argv[1:]:
        if os.path.isdir(arg):
            add_paths(collect_packages(arg, True))
        else:
            add_paths([arg])

    if not binary:
        root.after(300, lambda: messagebox.showwarning(
            TITLE, "dbpf-recompress binary not found.\nRun `make` in this folder (needs gcc)."))
    root.mainloop()


if __name__ == "__main__":
    main()
