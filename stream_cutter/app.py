"""Stream Cutter GUI: project name, drag-and-drop files, live progress."""
import json, os, queue, re, subprocess, threading, time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinterdnd2 import DND_FILES, TkinterDnD
from . import __version__, engine as E

BG, PANEL, FG, MUTED = "#14161b", "#1d2027", "#e8eaf0", "#8b90a0"
ACCENT, OK, BAD, TROUGH = "#7c5cff", "#3ddc97", "#ff5c6c", "#262a34"
SPIN = "◐◓◑◒"
STAGES = ["Listen to audio", "Pick moments", "Render clips", "Export"]


class App:
    def __init__(s):
        s.root = r = TkinterDnD.Tk()
        r.title(f"Stream Cutter {__version__}")
        r.geometry("900x840")
        r.minsize(820, 760)
        r.configure(bg=BG)
        s.q, s.cancel = queue.Queue(), threading.Event()
        s.files, s.segs, s.updaters = [], [], []
        s.running = s.ready = False
        s.frac, s.t0, s.spin = 0.0, 0.0, 0
        s.out_video = s.out_folder = None
        s.stage_state = ["wait"] * len(STAGES)
        s.v = dict(target=tk.DoubleVar(value=20), pre=tk.DoubleVar(value=4), post=tk.DoubleVar(value=3),
                   track=tk.IntVar(value=1), gpu=tk.BooleanVar(value=False))
        s.theme()
        s.layout()
        r.drop_target_register(DND_FILES)
        r.dnd_bind("<<Drop>>", s.on_drop)
        r.protocol("WM_DELETE_WINDOW", s.close)
        s.poll()
        s.tick()
        s.start_tools()
        r.mainloop()

    # ---------- look & layout ----------
    def theme(s):
        st = ttk.Style()
        st.theme_use("clam")
        s.root.option_add("*TCombobox*Listbox.background", PANEL)
        s.root.option_add("*TCombobox*Listbox.foreground", FG)
        st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, font=("Segoe UI", 10))
        st.configure("Card.TFrame", background=PANEL)
        st.configure("Card.TLabel", background=PANEL)
        st.configure("Card.TCheckbutton", background=PANEL, foreground=FG)
        st.configure("TButton", background=PANEL, foreground=FG, borderwidth=0, padding=(12, 6))
        st.map("TButton", background=[("active", "#2a2e38"), ("disabled", BG)], foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="white", font=("Segoe UI", 11, "bold"),
                     padding=(18, 9))
        st.map("Accent.TButton", background=[("active", "#6a4cf0"), ("disabled", "#3a3560")],
               foreground=[("disabled", "#9a95b8")])
        st.configure("Main.Horizontal.TProgressbar", troughcolor=TROUGH, background=ACCENT, borderwidth=0, thickness=16)
        st.configure("Sub.Horizontal.TProgressbar", troughcolor=TROUGH, background=OK, borderwidth=0, thickness=8)
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=TROUGH, foreground=MUTED, borderwidth=0)
        st.map("Treeview", background=[("selected", "#33384a")])
        st.configure("TScale", background=PANEL, troughcolor=TROUGH)
        st.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)
        st.configure("TSpinbox", fieldbackground=PANEL, background=PANEL, foreground=FG, arrowcolor=FG)

    def layout(s):
        r = s.root
        head = ttk.Frame(r)
        head.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(head, text="Stream Cutter", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(head, text="long stream in, rough cut out", foreground=MUTED).pack(side="left", padx=12, pady=(8, 0))

        pr = ttk.Frame(r)
        pr.pack(fill="x", padx=16, pady=4)
        ttk.Label(pr, text="Project name").pack(side="left")
        s.name_var = tk.StringVar(value="My Project")
        s.name_box = ttk.Combobox(pr, textvariable=s.name_var, width=34, values=s.project_names())
        s.name_box.pack(side="left", padx=8)
        s.name_box.bind("<<ComboboxSelected>>", s.load_project)
        ttk.Button(pr, text="Open project folder", command=lambda: s.open_path(s.cur_proj())).pack(side="left")

        s.drop = tk.Label(r, text="⬇   Drop your VODs or clips here\nor click to browse", font=("Segoe UI", 13),
                          fg=MUTED, bg=PANEL, height=3, cursor="hand2")
        s.drop.pack(fill="x", padx=16, pady=8)
        s.drop.bind("<Button-1>", lambda e: s.browse())
        s.drop.drop_target_register(DND_FILES)
        s.drop.dnd_bind("<<Drop>>", s.on_drop)
        s.drop.dnd_bind("<<DropEnter>>", s.enter)
        s.drop.dnd_bind("<<DropLeave>>", s.leave)

        # first-run tool download banner (only packed while needed)
        s.tools = ttk.Frame(r, style="Card.TFrame")
        s.tool_lbl = ttk.Label(s.tools, style="Card.TLabel")
        s.tool_lbl.pack(anchor="w", padx=12, pady=(8, 2))
        s.tool_bar = ttk.Progressbar(s.tools, style="Sub.Horizontal.TProgressbar", maximum=100)
        s.tool_bar.pack(fill="x", padx=12, pady=(0, 8))
        s.tool_retry = ttk.Button(s.tools, text="Retry", command=s.start_tools)

        lf = ttk.Frame(r)
        lf.pack(fill="both", expand=True, padx=16)
        s.tree = ttk.Treeview(lf, columns=("len", "status", "kept"), height=4, selectmode="extended")
        for col, text, w in (("#0", "File", 380), ("len", "Length", 80), ("status", "Status", 150), ("kept", "Kept", 80)):
            s.tree.heading(col, text=text, anchor="w")
            s.tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(lf, command=s.tree.yview)
        s.tree.config(yscrollcommand=sb.set)
        s.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        s.tree.bind("<Delete>", lambda e: s.remove())
        row = ttk.Frame(r)
        row.pack(fill="x", padx=16, pady=(4, 0))
        ttk.Button(row, text="Remove selected", command=s.remove).pack(side="left")
        ttk.Button(row, text="Clear all", command=s.clear_files).pack(side="left", padx=6)

        ttk.Label(r, text="Kept moments across your footage", foreground=MUTED).pack(anchor="w", padx=16, pady=(10, 2))
        s.tl = tk.Canvas(r, height=34, bg=BG, highlightthickness=0)
        s.tl.pack(fill="x", padx=16)
        s.tl.bind("<Configure>", s.draw)

        card = ttk.Frame(r, style="Card.TFrame")
        card.pack(fill="x", padx=16, pady=10)
        card.columnconfigure(1, weight=1)
        s.slider(card, 0, "Max length (% of footage)", "target", 3, 60, "{:.0f}%")
        s.slider(card, 1, "Lead-in before a moment", "pre", 0, 15, "{:.0f} s")
        s.slider(card, 2, "Tail after a moment", "post", 0, 15, "{:.0f} s")
        opt = ttk.Frame(card, style="Card.TFrame")
        opt.grid(row=3, column=0, columnspan=3, sticky="w", padx=12, pady=(2, 8))
        ttk.Label(opt, text="Mic audio track", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(opt, from_=1, to=8, width=4, textvariable=s.v["track"]).pack(side="left", padx=8)
        ttk.Checkbutton(opt, text="NVIDIA GPU encoding (faster)", variable=s.v["gpu"],
                        style="Card.TCheckbutton").pack(side="left", padx=18)

        ctl = ttk.Frame(r)
        ctl.pack(fill="x", padx=16, pady=(2, 4))
        s.go_btn = ttk.Button(ctl, text="Build rough cut", style="Accent.TButton", command=s.go, state="disabled")
        s.go_btn.pack(side="left")
        s.cancel_btn = ttk.Button(ctl, text="Cancel", command=s.cancel.set, state="disabled")
        s.cancel_btn.pack(side="left", padx=8)
        s.clock = ttk.Label(ctl, foreground=MUTED)
        s.clock.pack(side="right")

        sr = ttk.Frame(r)
        sr.pack(fill="x", padx=16, pady=6)
        s.stage_lbls = []
        for i in range(len(STAGES)):
            sr.columnconfigure(i, weight=1)
            lab = ttk.Label(sr)
            lab.grid(row=0, column=i, sticky="w")
            s.stage_lbls.append(lab)
            s.paint_stage(i)

        br = ttk.Frame(r)
        br.pack(fill="x", padx=16)
        s.main = ttk.Progressbar(br, style="Main.Horizontal.TProgressbar", maximum=100)
        s.main.pack(side="left", fill="x", expand=True)
        s.pct = ttk.Label(br, text="0%", width=5, anchor="e")
        s.pct.pack(side="right")
        s.sub = ttk.Progressbar(r, style="Sub.Horizontal.TProgressbar", maximum=100)
        s.sub.pack(fill="x", padx=16, pady=(8, 2))
        s.sublbl = ttk.Label(r, text="Drop in some footage, then hit Build.", foreground=MUTED)
        s.sublbl.pack(anchor="w", padx=16)

        s.result = ttk.Frame(r, style="Card.TFrame")
        s.res_lbl = ttk.Label(s.result, style="Card.TLabel", font=("Segoe UI", 11, "bold"), foreground=OK)
        s.res_lbl.pack(side="left", padx=12, pady=10)
        ttk.Button(s.result, text="Play", command=lambda: s.open_path(s.out_video)).pack(side="right", padx=(0, 10))
        ttk.Button(s.result, text="Open folder", command=lambda: s.open_path(s.out_folder)).pack(side="right", padx=4)

    def slider(s, parent, row, label, key, lo, hi, fmt):
        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=12, pady=5)
        val = ttk.Label(parent, style="Card.TLabel", width=8, anchor="e")
        val.grid(row=row, column=2, padx=12)
        upd = lambda _=None: val.config(text=fmt.format(s.v[key].get()))
        ttk.Scale(parent, from_=lo, to=hi, variable=s.v[key], command=upd).grid(row=row, column=1, sticky="ew", padx=8)
        s.updaters.append(upd)
        upd()

    def draw(s, _=None):
        c = s.tl
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        c.create_rectangle(0, 0, w, h, fill=TROUGH, outline="")
        if not s.segs:
            c.create_text(w / 2, h / 2, text="green blocks = moments that made the cut", fill=MUTED)
            return
        tot, x0 = sum(d for d, _ in s.segs) or 1, 0.0
        for d, segs in s.segs:
            for a, b in segs:
                c.create_rectangle(x0 + a / tot * w, 3, max(x0 + b / tot * w, x0 + a / tot * w + 1), h - 3,
                                   fill=OK, outline="")
            x0 += d / tot * w
            c.create_line(x0, 0, x0, h, fill=BG, width=2)

    # ---------- projects & files ----------
    def project_names(s):
        return sorted(d.name for d in E.ROOT.glob("*") if (d / "project.json").exists())

    def cur_proj(s):
        name = re.sub(r'[<>:"/\\|?*]', "_", s.name_var.get().strip()) or "Untitled"
        p = E.ROOT / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save(s):
        p = s.cur_proj()
        cfg = {k: v.get() for k, v in s.v.items()}
        (p / "project.json").write_text(json.dumps({"files": s.files, "cfg": cfg}, indent=2))
        s.name_box["values"] = s.project_names()
        return p

    def load_project(s, _=None):
        try:
            d = json.loads((E.ROOT / s.name_var.get() / "project.json").read_text())
        except Exception:
            return
        s.clear_files()
        gone = [f for f in d.get("files", []) if not Path(f).exists()]
        s.add_paths([f for f in d.get("files", []) if f not in gone])
        for k, val in d.get("cfg", {}).items():
            if k in s.v:
                s.v[k].set(val)
        for u in s.updaters:
            u()
        s.sublbl.config(text=f"Loaded project. {len(gone)} missing file(s) skipped." if gone else "Project loaded.")

    def add_paths(s, paths):
        if s.running:
            return
        found = []
        for p in map(Path, paths):
            if p.is_dir():
                found += [x for x in sorted(p.rglob("*")) if x.suffix.lower() in E.VIDEO_EXT]
            elif p.suffix.lower() in E.VIDEO_EXT:
                found.append(p)
        if paths and not found:
            s.sublbl.config(text="No video files in that drop (mp4, mkv, mov, flv, ts, webm, avi).", foreground=BAD)
            return
        s.sublbl.config(foreground=MUTED)
        for p in found:
            f = str(p)
            if f not in s.files:
                s.files.append(f)
                s.tree.insert("", "end", iid=f, text=p.name, values=("…", "Queued", ""))
                threading.Thread(target=s.probe_len, args=(f,), daemon=True).start()

    def probe_len(s, f):
        try:
            s.q.put(("file", f, "len", E.mmss(E.probe(f)[0])))
        except Exception:
            pass

    def browse(s):
        s.add_paths(filedialog.askopenfilenames(
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.flv *.ts *.webm *.avi *.m4v"), ("All files", "*.*")]))

    def on_drop(s, ev):
        s.leave(ev)
        s.add_paths(s.root.tk.splitlist(ev.data))
        return ev.action

    def enter(s, ev):
        s.drop.config(bg="#2b2550", fg=FG)
        return ev.action

    def leave(s, ev):
        s.drop.config(bg=PANEL, fg=MUTED)
        return ev.action

    def remove(s):
        if s.running:
            return
        for iid in s.tree.selection():
            s.files.remove(iid)
            s.tree.delete(iid)

    def clear_files(s):
        if s.running:
            return
        s.files.clear()
        s.tree.delete(*s.tree.get_children())

    # ---------- ffmpeg setup ----------
    def start_tools(s):
        s.tool_retry.pack_forget()
        threading.Thread(target=s.setup_tools, daemon=True).start()

    def setup_tools(s):
        try:
            if not E.ffmpeg_ready():
                s.q.put(("tools", "download"))
                E.download_ffmpeg(lambda f: s.q.put(("toolprog", f)))
            s.q.put(("tools", "ready"))
        except Exception as e:
            s.q.put(("tools", f"fail:{e}"))

    # ---------- running a build ----------
    def go(s):
        if not s.files:
            return messagebox.showinfo("Stream Cutter", "Drop at least one video in first.")
        try:
            c = {k: v.get() for k, v in s.v.items()}
        except tk.TclError:
            return messagebox.showerror("Stream Cutter", "Check the settings values.")
        proj = s.save()
        cfg = dict(target=round(c["target"]), pre=round(c["pre"]), post=round(c["post"]),
                   track=int(c["track"]) - 1, gpu=c["gpu"])
        s.running, s.frac, s.t0 = True, 0.0, time.time()
        s.cancel.clear()
        s.segs = []
        s.draw()
        s.result.pack_forget()
        s.stage_state = ["wait"] * len(STAGES)
        for i in range(len(STAGES)):
            s.paint_stage(i)
        s.main.config(value=0)
        s.sub.config(value=0)
        s.pct.config(text="0%")
        s.sublbl.config(text="Starting…", foreground=MUTED)
        for f in s.files:
            s.tree.set(f, "status", "Queued")
            s.tree.set(f, "kept", "")
        s.busy(True)
        threading.Thread(target=s.work, args=(proj, list(s.files), cfg), daemon=True).start()

    def work(s, proj, files, cfg):
        try:
            E.build(proj, files, cfg, s.q.put, s.cancel)
        except E.Cancelled:
            s.q.put(("cancelled",))
        except Exception as e:
            s.q.put(("error", str(e)))

    def busy(s, b):
        s.go_btn.config(state="disabled" if b or not s.ready else "normal")
        s.cancel_btn.config(state="normal" if b else "disabled")

    def paint_stage(s, i):
        icon, col = {"wait": ("○", MUTED), "run": (SPIN[s.spin % 4], ACCENT),
                     "done": ("✓", OK), "err": ("✗", BAD)}[s.stage_state[i]]
        s.stage_lbls[i].config(text=f"{icon}  {STAGES[i]}", foreground=col)

    def tick(s):
        s.spin += 1
        for i, st in enumerate(s.stage_state):
            if st == "run":
                s.paint_stage(i)
        if s.running:
            el = time.time() - s.t0
            eta = f"  ·  about {E.mmss(el / s.frac - el)} left" if s.frac > 0.04 else ""
            s.clock.config(text=f"Elapsed {E.mmss(el)}{eta}")
        s.root.after(150, s.tick)

    def poll(s):
        s.root.after(80, s.poll)
        try:
            while True:
                s.handle(s.q.get_nowait())
        except queue.Empty:
            pass

    def end_run(s, text, color):
        s.running = False
        s.busy(False)
        s.clock.config(text="")
        s.sublbl.config(text=text, foreground=color)

    def handle(s, m):
        k = m[0]
        if k == "stage":
            s.stage_state[m[1]] = m[2]
            s.paint_stage(m[1])
        elif k == "prog":
            s.frac = m[1]
            s.main.config(value=m[1] * 100)
            s.pct.config(text=f"{m[1] * 100:.0f}%")
        elif k == "sub":
            s.sub.config(value=m[1] * 100)
            s.sublbl.config(text=m[2], foreground=MUTED)
        elif k == "file":
            if s.tree.exists(m[1]):
                s.tree.set(m[1], m[2], m[3])
        elif k == "segs":
            s.segs = m[1]
            s.draw()
        elif k == "done":
            d = m[1]
            s.out_video, s.out_folder = d["video"], d["folder"]
            s.res_lbl.config(text=f"✓ Rough cut ready: {d['moments']} moments, {E.mmss(d['kept'])} "
                                  f"from {E.mmss(d['src'])} of footage")
            s.result.pack(fill="x", padx=16, pady=8)
            s.end_run("Finished", OK)
        elif k in ("error", "cancelled"):
            for i, st in enumerate(s.stage_state):
                if st == "run":
                    s.stage_state[i] = "err" if k == "error" else "wait"
                    s.paint_stage(i)
            s.end_run("Failed." if k == "error" else "Cancelled.", BAD if k == "error" else MUTED)
            if k == "error":
                messagebox.showerror("Stream Cutter", m[1])
        elif k == "tools":
            if m[1] == "ready":
                s.tools.pack_forget()
                s.tool_bar.stop()
                s.ready = True
                s.busy(s.running)
                for f in s.files:
                    if s.tree.set(f, "len") == "…":
                        threading.Thread(target=s.probe_len, args=(f,), daemon=True).start()
            elif m[1] == "download":
                s.tools.pack(fill="x", padx=16, pady=(0, 6), before=s.tree.master)
                s.tool_lbl.config(text="First-run setup: downloading video tools (ffmpeg, about 100 MB)…", foreground=FG)
            else:
                s.tool_lbl.config(text=m[1][5:], foreground=BAD)
                s.tool_retry.pack(anchor="w", padx=12, pady=(0, 8))
        elif k == "toolprog":
            if m[1] is None:
                s.tool_bar.config(mode="indeterminate")
                s.tool_bar.start(12)
            else:
                s.tool_bar.config(mode="determinate", value=m[1] * 100)

    @staticmethod
    def open_path(p):
        try:
            os.startfile(str(p)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:
            messagebox.showerror("Stream Cutter", str(e))

    def close(s):
        s.cancel.set()
        try:
            if s.files:
                s.save()
        except Exception:
            pass
        s.root.destroy()


def main():
    App()
