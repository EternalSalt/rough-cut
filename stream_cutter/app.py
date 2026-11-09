"""Stream Cutter GUI: project name, drag-and-drop files, live progress and a live preview of what gets cut."""
import json, os, queue, re, subprocess, threading, time
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import numpy as np
from tkinterdnd2 import DND_FILES, TkinterDnD
from . import __version__, engine as E

BG, PANEL, FG, MUTED = "#14161b", "#1d2027", "#e8eaf0", "#8b90a0"
ACCENT, OK, BAD, TROUGH = "#7c5cff", "#3ddc97", "#ff5c6c", "#262a34"
CYAN, AMBER = "#4cc9f0", "#ffd166"
KIND = {"a": OK, "v": CYAN, "av": AMBER}
SPIN = "◐◓◑◒"
STAGES = ["Listen to audio", "Watch the video", "Pick moments", "Render clips", "Export"]


class App:
    def __init__(s):
        s.root = r = TkinterDnD.Tk()
        r.title(f"Stream Cutter {__version__}")
        r.geometry("1200x800")
        r.minsize(1080, 720)
        r.configure(bg=BG)
        s.q, s.cancel = queue.Queue(), threading.Event()
        s.files, s.sig, s.durs, s.view = [], {}, {}, []
        s.sl, s.scales = {}, []
        s.pending, s.kept = None, None
        s.running = s.ready = False
        s.frac, s.t0, s.spin = 0.0, 0.0, 0
        s.out_video = s.out_folder = None
        s.stage_state = ["wait"] * len(STAGES)
        s.v = dict(target=tk.DoubleVar(value=20), pre=tk.DoubleVar(value=4), post=tk.DoubleVar(value=3),
                   balance=tk.DoubleVar(value=0.5), track=tk.IntVar(value=1),
                   visual=tk.BooleanVar(value=True), gpu=tk.BooleanVar(value=False))
        s.theme()
        s.layout()
        s.v["track"].trace_add("write", s.load_cached)
        s.v["visual"].trace_add("write", s.load_cached)
        s.update_help()
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
        st.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("Segoe UI", 8))
        st.configure("Help.TLabel", background=PANEL, foreground="#b6bac8", font=("Segoe UI", 9))
        st.configure("Val.TLabel", background=PANEL, foreground="#b9a8ff", font=("Segoe UI", 10, "bold"))
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

        body = ttk.Frame(r)
        body.pack(fill="both", expand=True)
        right = ttk.Frame(body, style="Card.TFrame", width=400)
        right.pack(side="right", fill="y", padx=(8, 16), pady=(4, 12))
        right.pack_propagate(False)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(16, 0))

        # ----- left: files, tracking timeline, progress -----
        s.drop = tk.Label(left, text="⬇   Drop your VODs or clips here\nor click to browse", font=("Segoe UI", 13),
                          fg=MUTED, bg=PANEL, height=3, cursor="hand2")
        s.drop.pack(fill="x", pady=8)
        s.drop.bind("<Button-1>", lambda e: s.browse())
        s.drop.drop_target_register(DND_FILES)
        s.drop.dnd_bind("<<Drop>>", s.on_drop)
        s.drop.dnd_bind("<<DropEnter>>", s.enter)
        s.drop.dnd_bind("<<DropLeave>>", s.leave)

        s.tools = ttk.Frame(left, style="Card.TFrame")       # first-run download banner, packed only while needed
        s.tool_lbl = ttk.Label(s.tools, style="Card.TLabel")
        s.tool_lbl.pack(anchor="w", padx=12, pady=(8, 2))
        s.tool_bar = ttk.Progressbar(s.tools, style="Sub.Horizontal.TProgressbar", maximum=100)
        s.tool_bar.pack(fill="x", padx=12, pady=(0, 8))
        s.tool_retry = ttk.Button(s.tools, text="Retry", command=s.start_tools)

        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True)
        s.tree = ttk.Treeview(lf, columns=("len", "status", "kept"), height=4, selectmode="extended")
        for col, text, w in (("#0", "File", 300), ("len", "Length", 70), ("status", "Status", 130), ("kept", "Kept", 70)):
            s.tree.heading(col, text=text, anchor="w")
            s.tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(lf, command=s.tree.yview)
        s.tree.config(yscrollcommand=sb.set)
        s.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        s.tree.bind("<Delete>", lambda e: s.remove())
        row = ttk.Frame(left)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Remove selected", command=s.remove).pack(side="left")
        ttk.Button(row, text="Clear all", command=s.clear_files).pack(side="left", padx=6)

        hd = ttk.Frame(left)
        hd.pack(fill="x", pady=(10, 2))
        ttk.Label(hd, text="What it tracks, and what it keeps", foreground=MUTED).pack(side="left")
        for txt, col in (("both", AMBER), ("visual", CYAN), ("audio", OK)):
            tk.Label(hd, text=f"■ {txt}", fg=col, bg=BG, font=("Segoe UI", 9)).pack(side="right", padx=6)
        s.tl = tk.Canvas(left, height=104, bg=BG, highlightthickness=0)
        s.tl.pack(fill="x")
        s.tl.bind("<Configure>", s.draw)

        ctl = ttk.Frame(left)
        ctl.pack(fill="x", pady=(8, 4))
        s.go_btn = ttk.Button(ctl, text="Build rough cut", style="Accent.TButton", command=s.go, state="disabled")
        s.go_btn.pack(side="left")
        s.cancel_btn = ttk.Button(ctl, text="Cancel", command=s.cancel.set, state="disabled")
        s.cancel_btn.pack(side="left", padx=8)
        s.clock = ttk.Label(ctl, foreground=MUTED)
        s.clock.pack(side="right")

        sr = ttk.Frame(left)
        sr.pack(fill="x", pady=6)
        s.stage_lbls = []
        for i in range(len(STAGES)):
            sr.columnconfigure(i, weight=1)
            lab = ttk.Label(sr)
            lab.grid(row=0, column=i, sticky="w")
            s.stage_lbls.append(lab)
            s.paint_stage(i)

        br = ttk.Frame(left)
        br.pack(fill="x")
        s.main = ttk.Progressbar(br, style="Main.Horizontal.TProgressbar", maximum=100)
        s.main.pack(side="left", fill="x", expand=True)
        s.pct = ttk.Label(br, text="0%", width=5, anchor="e")
        s.pct.pack(side="right")
        s.sub = ttk.Progressbar(left, style="Sub.Horizontal.TProgressbar", maximum=100)
        s.sub.pack(fill="x", pady=(8, 2))
        s.sublbl = ttk.Label(left, text="Drop in some footage, then hit Build.", foreground=MUTED)
        s.sublbl.pack(anchor="w")

        s.result = ttk.Frame(left, style="Card.TFrame")
        s.res_lbl = ttk.Label(s.result, style="Card.TLabel", font=("Segoe UI", 11, "bold"), foreground=OK)
        s.res_lbl.pack(side="left", padx=12, pady=10)
        ttk.Button(s.result, text="Play", command=lambda: s.open_path(s.out_video)).pack(side="right", padx=(0, 10))
        ttk.Button(s.result, text="Open folder", command=lambda: s.open_path(s.out_folder)).pack(side="right", padx=4)

        # ----- right: settings, each one explained -----
        ttk.Label(right, text="What counts as a good moment", style="Card.TLabel",
                  font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=14, pady=(12, 0))
        s.slider(right, "balance", "What to look for", 0, 1, "Audio: voice, laughs", "Visual: motion, changes")
        s.slider(right, "target", "How much to keep", 3, 60, "Short & punchy", "Keep more")
        s.slider(right, "pre", "Run-up before each moment", 0, 15, "None", "15 s")
        s.slider(right, "post", "Wind-down after each moment", 0, 15, "None", "15 s")
        opt = ttk.Frame(right, style="Card.TFrame")
        opt.pack(fill="x", padx=14, pady=(16, 8))
        ttk.Checkbutton(opt, text="Watch the video too (finds visual gags)", variable=s.v["visual"],
                        style="Card.TCheckbutton").pack(anchor="w")
        ttk.Label(opt, text="Slower first scan of a long VOD. The result is saved, so later runs skip it.",
                  style="Help.TLabel", wraplength=350, justify="left").pack(anchor="w", pady=(0, 8))
        trk = ttk.Frame(opt, style="Card.TFrame")
        trk.pack(anchor="w")
        ttk.Label(trk, text="Mic audio track", style="Card.TLabel").pack(side="left")
        ttk.Spinbox(trk, from_=1, to=8, width=4, textvariable=s.v["track"]).pack(side="left", padx=8)
        ttk.Checkbutton(opt, text="NVIDIA GPU encoding (faster render)", variable=s.v["gpu"],
                        style="Card.TCheckbutton").pack(anchor="w", pady=(8, 0))

    def slider(s, parent, key, title, lo, hi, lt, rt):
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="x", padx=14, pady=(12, 0))
        top = ttk.Frame(box, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text=title, style="Card.TLabel", font=("Segoe UI", 10, "bold")).pack(side="left")
        val = ttk.Label(top, style="Val.TLabel")
        val.pack(side="right")
        sc = ttk.Scale(box, from_=lo, to=hi, variable=s.v[key], command=s.on_slide)
        sc.pack(fill="x", pady=(4, 0))
        ends = ttk.Frame(box, style="Card.TFrame")
        ends.pack(fill="x")
        ttk.Label(ends, text=lt, style="Muted.TLabel").pack(side="left")
        ttk.Label(ends, text=rt, style="Muted.TLabel").pack(side="right")
        hp = ttk.Label(box, style="Help.TLabel", wraplength=350, justify="left")
        hp.pack(anchor="w", pady=(3, 0))
        s.sl[key] = (val, hp)
        s.scales.append(sc)

    def update_help(s):
        v, vis = s.v, s.v["visual"].get()
        pct, bal = round(v["target"].get()), v["balance"].get()
        tot = sum(s.durs.get(f, 0) for f in s.files)
        word = ("Audio only" if not vis or bal < .15 else "Mostly audio" if bal < .4 else "Both equally"
                if bal <= .6 else "Mostly visual" if bal <= .85 else "Visual only")
        cap = (f"At most {pct}% of your {E.mmss(tot)} of footage, about {E.mmss(tot * pct / 100)}."
               if tot else f"At most {pct}% of your footage.")
        now = f" Right now it would keep {E.mmss(s.kept)}." if s.kept is not None else ""
        texts = {
            "balance": (word, "Left cuts on your voice (yelling, laughing, reactions). Right cuts on what happens "
                              "on screen (explosions, glitches, sudden big movement). Middle takes either kind."
                        if vis else "Video scan is off, so only audio is used. Tick 'Watch the video too' below."),
            "target": (f"{pct}%", cap + now + " It can come out shorter if there are not enough good moments."),
            "pre": (f"{round(v['pre'].get())} s", "Seconds kept before each moment, so the build-up to the joke is in the clip."),
            "post": (f"{round(v['post'].get())} s", "Seconds kept after each moment, so the reaction and aftermath are in the clip."),
        }
        for k, (val, hp) in s.sl.items():
            val.config(text=texts[k][0])
            hp.config(text=texts[k][1])

    def on_slide(s, _=None):
        s.update_help()
        if s.pending:
            s.root.after_cancel(s.pending)
        s.pending = None if s.running else s.root.after(150, s.preview)

    def draw(s, _=None):
        c = s.tl
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        x0, x1 = 58, max(w - 4, 60)
        lanes = [("Audio", 2, 34, OK), ("Visual", 38, 70, CYAN), ("Kept", 74, 102, None)]
        for name, y0, y1, _c in lanes:
            c.create_rectangle(x0, y0, x1, y1, fill=TROUGH, outline="")
            c.create_text(4, (y0 + y1) / 2, text=name, anchor="w", fill=MUTED, font=("Segoe UI", 9))
        if not s.view:
            c.create_text((x0 + x1) / 2, 52, fill=MUTED,
                          text="Build once (or reopen a built project) to see what it tracks here")
            return
        tot = sum(len(sc[0]) for sc, _ in s.view) * E.WIN
        k, ox = (x1 - x0) / tot, float(x0)
        if s.view[0][0][1] is None:
            c.create_text(x0 + 8, 54, text="video scan off", anchor="w", fill=MUTED)
        for sc, segs in s.view:
            fw = len(sc[0]) * E.WIN * k
            for (_n, y0, y1, col), arr in zip(lanes, sc):
                if arr is None:
                    continue
                m = max(1, min(int(fw), len(arr)))
                idx = np.linspace(0, len(arr), m + 1).astype(int)[:-1]
                vals = np.clip(np.maximum.reduceat(arr, idx), 0, 6) / 6
                for j, val in enumerate(vals):
                    if val > 0.02:
                        x = ox + j * fw / m
                        c.create_line(x, y1, x, y1 - val * (y1 - y0), fill=col)
            for b, e, kind in segs:
                c.create_rectangle(ox + b * k, 76, max(ox + e * k, ox + b * k + 1), 100, fill=KIND[kind], outline="")
            ox += fw
            c.create_line(ox, 0, ox, h, fill=BG, width=2)

    # ---------- live preview ----------
    def cfg_now(s):
        v = s.v
        return dict(target=round(v["target"].get()), pre=round(v["pre"].get()), post=round(v["post"].get()),
                    balance=round(v["balance"].get(), 2), track=int(v["track"].get()) - 1,
                    gpu=v["gpu"].get(), visual=v["visual"].get())

    def preview(s):
        s.pending = None
        try:
            c = s.cfg_now()
        except tk.TclError:
            return
        s.view, s.kept = [], 0.0
        for f in s.files:
            if f in s.sig:
                sc = s.sig[f]
                segs = E.pick(sc, c["balance"], c["target"], c["pre"], c["post"])
                s.view.append((sc, segs))
                s.kept += sum(e - b for b, e, _ in segs)
        if not s.view:
            s.kept = None
        s.update_help()
        s.draw()

    def load_cached(s, *_):
        """Pick up earlier analysis from disk so the timeline and sliders work before building again."""
        s.sig.clear()
        try:
            c = s.cfg_now()
        except tk.TclError:
            return
        for f in s.files:
            got = E.cached(s.proj_path(), f, c["track"], c["visual"])
            if got and len(got[0]):
                s.sig[f] = E.scores(*got)
        s.preview()

    # ---------- projects & files ----------
    def project_names(s):
        return sorted(d.name for d in E.ROOT.glob("*") if (d / "project.json").exists())

    def proj_path(s):
        return E.ROOT / (re.sub(r'[<>:"/\\|?*]', "_", s.name_var.get().strip()) or "Untitled")

    def cur_proj(s):
        p = s.proj_path()
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
        s.load_cached()
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
        s.load_cached()

    def probe_len(s, f):
        try:
            d = E.probe(f)[0]
            s.q.put(("dur", f, d))
            s.q.put(("file", f, "len", E.mmss(d)))
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
            s.sig.pop(iid, None)
            s.durs.pop(iid, None)
        s.preview()

    def clear_files(s):
        if s.running:
            return
        s.files.clear()
        s.sig.clear()
        s.durs.clear()
        s.tree.delete(*s.tree.get_children())
        s.preview()

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
            cfg = s.cfg_now()
        except tk.TclError:
            return messagebox.showerror("Stream Cutter", "Check the settings values.")
        proj = s.save()
        s.running, s.frac, s.t0 = True, 0.0, time.time()
        s.cancel.clear()
        s.view, s.kept = [], None
        s.draw()
        s.update_help()
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
        for sc in s.scales:
            sc.state(["disabled"] if b else ["!disabled"])

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
        elif k == "dur":
            s.durs[m[1]] = m[2]
            s.update_help()
        elif k == "analysis":
            s.sig[m[1]] = E.scores(m[2], m[3])
            s.preview()
        elif k == "done":
            d = m[1]
            s.out_video, s.out_folder = d["video"], d["folder"]
            s.res_lbl.config(text=f"✓ Rough cut ready: {d['moments']} moments, {E.mmss(d['kept'])} "
                                  f"from {E.mmss(d['src'])} of footage")
            s.result.pack(fill="x", pady=8)
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
                    if f not in s.durs:
                        threading.Thread(target=s.probe_len, args=(f,), daemon=True).start()
            elif m[1] == "download":
                s.tools.pack(fill="x", pady=(0, 6), before=s.tree.master)
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
