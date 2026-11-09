"""Stream Cutter engine: audio + video analysis, moment picking, rendering. No GUI code in here."""
import hashlib, json, os, shutil, subprocess, time, urllib.request, zipfile
from pathlib import Path
import numpy as np

SR, WIN = 16000, 0.5          # audio sample rate, seconds per analysis window (audio and video share it)
VW, VH = 64, 36               # video frames are shrunk to this size for motion tracking
W, H, FPS = 1920, 1080, 60    # format of the rendered rough cut
CLIP_KEEP_ALL = 90            # files shorter than this (sec) are treated as pre-picked clips and kept whole
MIN_SCORE = 1.5               # a moment must stand out by at least this many "sigmas" to be kept
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm", ".avi", ".m4v"}
NOWIN = 0x08000000 if os.name == "nt" else 0
ROOT = Path.home() / "Videos" / "StreamCutter"
FFBIN = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "StreamCutter" / "ffmpeg"
FFURLS = ["https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
          "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"]


class Cancelled(Exception):
    pass


def mmss(t):
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02}:{t % 60:02}" if t >= 3600 else f"{t // 60}:{t % 60:02}"


# ---------- ffmpeg ----------
def ffmpeg_ready():
    if (FFBIN / "ffmpeg.exe").exists():
        os.environ["PATH"] = str(FFBIN) + os.pathsep + os.environ["PATH"]
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def download_ffmpeg(progress):
    """First-run download. progress(fraction) or progress(None) when the size is unknown."""
    FFBIN.mkdir(parents=True, exist_ok=True)
    zp, last = FFBIN / "download.zip", None
    for url in FFURLS:
        try:
            with urllib.request.urlopen(url, timeout=30) as r, open(zp, "wb") as fh:
                total, got = int(r.headers.get("Content-Length") or 0), 0
                while chunk := r.read(1 << 18):
                    fh.write(chunk)
                    got += len(chunk)
                    progress(got / total if total else None)
            with zipfile.ZipFile(zp) as z:
                for n in z.namelist():
                    if n.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                        (FFBIN / Path(n).name).write_bytes(z.read(n))
            zp.unlink()
            if ffmpeg_ready():
                return
        except Exception as e:
            last = e
    raise RuntimeError(f"Could not download ffmpeg ({last}). Check your internet connection and retry.")


def ff(cmd):
    r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, creationflags=NOWIN)
    if r.returncode:
        raise RuntimeError(r.stderr.decode(errors="ignore")[-400:])
    return r.stdout


def probe(f):
    """(duration in seconds, rounded fps) of a video file."""
    d = json.loads(ff(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                       "stream=r_frame_rate:format=duration", "-of", "json", str(f)]))
    n, m = d["streams"][0]["r_frame_rate"].split("/")
    return float(d["format"]["duration"]), round(int(n) / int(m))


# ---------- analysis ----------
def loudness(path, track, dur, on_frac, cancel):
    """Loudness in dB per WIN-second window of one audio track, streamed so memory stays small."""
    n = int(SR * WIN)
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{track}", "-ac", "1",
           "-ar", str(SR), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, creationflags=NOWIN)
    out, buf, done = [], b"", 0
    try:
        while chunk := p.stdout.read(n * 2 * 60):
            if cancel.is_set():
                raise Cancelled()
            buf += chunk
            k = len(buf) // (n * 2)
            if k:
                x = np.frombuffer(buf[: k * n * 2], np.int16).astype(np.float32).reshape(k, n) / 32768
                out.append(20 * np.log10(np.sqrt((x * x).mean(1)) + 1e-6))
                buf = buf[k * n * 2:]
                done += k
                on_frac(min(1.0, done * WIN / dur))
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
    return np.concatenate(out) if out else np.zeros(0)


def watch(path, dur, on_frac, cancel):
    """Motion per WIN-second window: how much the picture changed since the previous sample.
    Frames are shrunk to 64x36 grey, so big or sudden changes stand out and a frozen screen reads ~0."""
    fsz = VW * VH
    for hw in (["-hwaccel", "auto"], []):          # try GPU decoding first, fall back to CPU
        cmd = ["ffmpeg", "-v", "error", *hw, "-i", str(path), "-an", "-sn", "-vf",
               f"fps={1 / WIN:g},scale={VW}:{VH}:flags=fast_bilinear,format=gray", "-f", "rawvideo", "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, creationflags=NOWIN)
        out, buf, prev, done = [], b"", None, 0
        try:
            while chunk := p.stdout.read(fsz * 120):
                if cancel.is_set():
                    raise Cancelled()
                buf += chunk
                k = len(buf) // fsz
                if k:
                    x = np.frombuffer(buf[: k * fsz], np.uint8).reshape(k, fsz).astype(np.float32)
                    buf = buf[k * fsz:]
                    d = np.abs(np.diff(x if prev is None else np.vstack([prev, x]), axis=0)).mean(1)
                    out.append(np.concatenate([[0.0], d]) if prev is None else d)
                    prev = x[-1:]
                    done += k
                    on_frac(min(1.0, done * WIN / dur))
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()
        if out:
            return np.concatenate(out)
    return np.zeros(0)


def _avg(x, k):
    """Moving average that stays honest at the start and end of the file (no zero-padding bias)."""
    ones = np.ones(k)
    return np.convolve(x, ones, "same") / np.convolve(np.ones(len(x)), ones, "same")


def _excess(x, live, floor_sigma):
    """How far each window sits above its local norm (the surrounding minute), in 'sigmas'.
    Dead windows (silence / frozen picture) score -99 and never drag the norm around."""
    if not live.any():
        return np.full(len(x), -99.0)
    flat = np.where(live, x, np.median(x[live]))
    e = _avg(flat - _avg(flat, 120), 3)
    sig = max(1.4826 * np.median(np.abs(e[live] - np.median(e[live]))), floor_sigma)
    return np.where(live, e / sig, -99.0)


def scores(db, vis=None):
    """(audio, visual) excitement per window. visual is None when the video was not scanned."""
    n = len(db) if vis is None or not len(vis) else min(len(db), len(vis))
    db = db[:n]
    sa = _excess(db, db > np.percentile(db, 10) + 6, 2.0)
    sv = None
    if vis is not None and len(vis):
        v = vis[:n]
        sv = _excess(np.log1p(v), v > 0.5, 0.4)
    return sa, sv


def pick(sc, balance, target, pre, post, gap=5.0, min_len=6.0):
    """Choose moments as (start, end, kind) with kind 'a' (audio), 'v' (visual) or 'av' (both).
    balance 0 = audio only ... 1 = visual only; 0.5 takes either kind. The threshold is tuned so
    the result is at most `target`% of the footage."""
    sa, sv = sc
    n = len(sa)
    dur = n * WIN
    if dur < CLIP_KEEP_ALL:
        return [(0.0, dur, "a")] if dur else []
    wa, wv = (1.0, 0.0) if sv is None else (min(1.0, 2 * (1 - balance)), min(1.0, 2 * balance))
    A = np.where(sa > -98, wa * sa, -99.0) if wa else np.full(n, -99.0)
    V = np.where(sv > -98, wv * sv, -99.0) if wv else np.full(n, -99.0)
    score = np.maximum(A, V)
    live = score > -98
    if not live.any():
        return []

    def cut(pct):
        thr = max(np.percentile(score[live], 100 - pct), MIN_SCORE)
        segs = []
        for i in np.flatnonzero(score >= thr):
            s, e, ah, vh = i * WIN - pre, (i + 1) * WIN + post, A[i] >= thr, V[i] >= thr
            if segs and s - segs[-1][1] <= gap:
                seg = segs[-1]
                seg[1], seg[2], seg[3] = max(seg[1], e), seg[2] or ah, seg[3] or vh
            else:
                segs.append([s, e, ah, vh])
        return [(max(0.0, s), min(dur, e), "av" if ah and vh else "a" if ah else "v")
                for s, e, ah, vh in segs if e - s >= min_len]

    lo, hi, best = 0.5, 100.0, []
    for _ in range(8):
        mid = (lo + hi) / 2
        segs = cut(mid)
        if sum(e - s for s, e, _ in segs) <= target / 100 * dur:
            lo, best = mid, segs
        else:
            hi = mid
    return best


# ---------- cache ----------
def cache_paths(proj, f, track):
    k = hashlib.md5(f"{f}{os.path.getmtime(f)}".encode()).hexdigest()[:10]
    return proj / "cache" / f"{k}_a{track}.npy", proj / "cache" / f"{k}_v.npy"


def cached(proj, f, track, visual):
    """(audio, visual-or-None) from an earlier analysis, or None if it was never analyzed."""
    try:
        a, v = cache_paths(proj, f, track)
        if not a.exists() or (visual and not v.exists()):
            return None
        return np.load(a), (np.load(v) if visual else None)
    except OSError:
        return None


# ---------- full pipeline ----------
def build(proj, files, cfg, emit, cancel):
    """Runs everything and reports through emit(tuple). Events:
    stage(i, run|done), prog(0-1), sub(0-1, text), file(path, col, text), dur(path, sec),
    analysis(path, audio, visual), done(dict)."""
    out, tmp = proj / "output", proj / "output" / "_tmp"
    (proj / "cache").mkdir(parents=True, exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    vis_on = cfg["visual"]
    L, V = (0.15, 0.30) if vis_on else (0.30, 0.0)      # share of the progress bar for each stage
    P = L + V + 0.02

    def chk():
        if cancel.is_set():
            raise Cancelled()

    try:
        info = {}
        for f in files:
            try:
                info[f] = probe(f)
            except Exception:
                raise RuntimeError(f"Couldn't read {Path(f).name}. Is it a valid video file?")
            emit(("file", f, "len", mmss(info[f][0])))
            emit(("dur", f, info[f][0]))
        total = sum(d for d, _ in info.values()) or 1

        # 1. listen
        emit(("stage", 0, "run"))
        dbs, base = {}, 0.0
        for f in files:
            chk()
            dur, name = info[f][0], Path(f).name
            apath = cache_paths(proj, f, cfg["track"])[0]

            def frac(x):
                emit(("prog", L * (base + x * dur) / total))
                emit(("sub", x, f"Listening to {name}  ·  {x * 100:.0f}%"))
                emit(("file", f, "status", f"Listening {x * 100:.0f}%"))

            if apath.exists():
                db = np.load(apath)
                frac(1)
            else:
                db = loudness(f, cfg["track"], dur, frac, cancel)
                if not len(db) and cfg["track"]:
                    db = loudness(f, 0, dur, frac, cancel)
                if len(db):
                    np.save(apath, db)
            dbs[f] = db
            base += dur
        emit(("stage", 0, "done"))

        # 2. watch
        emit(("stage", 1, "run"))
        viss, base = {}, 0.0
        if vis_on:
            for f in files:
                chk()
                dur, name = info[f][0], Path(f).name
                vpath = cache_paths(proj, f, cfg["track"])[1]

                def frac(x):
                    emit(("prog", L + V * (base + x * dur) / total))
                    emit(("sub", x, f"Watching {name}  ·  {x * 100:.0f}%"))
                    emit(("file", f, "status", f"Watching {x * 100:.0f}%"))

                if vpath.exists():
                    v = np.load(vpath)
                    frac(1)
                else:
                    v = watch(f, dur, frac, cancel)
                    if len(v):
                        np.save(vpath, v)
                viss[f] = v if len(v) else None
                base += dur
        emit(("stage", 1, "done"))

        # 3. pick
        emit(("stage", 2, "run"))
        cuts = []
        for f in files:
            db = dbs[f]
            if not len(db):
                emit(("file", f, "status", "No audio"))
                continue
            emit(("analysis", f, db, viss.get(f)))
            segs = pick(scores(db, viss.get(f)), cfg["balance"], cfg["target"], cfg["pre"], cfg["post"])
            emit(("file", f, "status", f"{len(segs)} moments"))
            emit(("file", f, "kept", mmss(sum(e - s for s, e, _ in segs))))
            cuts += [(f, s, e) for s, e, _ in segs]
        emit(("prog", P))
        emit(("stage", 2, "done"))
        if not cuts:
            raise RuntimeError("No good moments found. Try moving 'How much to keep' up, turning on "
                               "'Watch the video too', or choosing a different audio track.")

        # 4. render
        emit(("stage", 3, "run"))
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p")
        enc = (["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "21"] if cfg["gpu"]
               else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        tot, done, parts = sum(e - s for _, s, e in cuts), 0.0, []
        for i, (f, s, e) in enumerate(cuts):
            chk()
            emit(("sub", done / tot, f"Rendering clip {i + 1} of {len(cuts)}"))
            emit(("prog", P + (0.95 - P) * done / tot))
            p = tmp / f"{i:04d}.mp4"
            ff(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.2f}", "-i", f, "-t", f"{e - s:.2f}",
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *enc,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(p)])
            done += e - s
            parts.append(p)
        emit(("stage", 3, "done"))

        # 5. export
        emit(("stage", 4, "run"))
        emit(("sub", 0.5, "Joining clips and writing edit files"))
        stem = "rough_cut_" + time.strftime("%Y%m%d_%H%M")
        (out / f"{stem}_cuts.txt").write_text(
            "\n".join(f"{Path(f).name}   {mmss(s)} - {mmss(e)}" for f, s, e in cuts), encoding="utf-8")
        fps = info[files[0]][1]
        tc = lambda n: f"{n // (fps * 3600):02}:{n // (fps * 60) % 60:02}:{n // fps % 60:02}:{n % fps:02}"
        lines, rec = [f"TITLE: {proj.name}", "FCM: NON-DROP FRAME", ""], fps * 3600
        for i, (f, s, e) in enumerate(cuts, 1):
            a, b = round(s * fps), round(e * fps)
            lines += [f"{i:03}  AX       AA/V  C        {tc(a)} {tc(b)} {tc(rec)} {tc(rec + b - a)}",
                      f"* FROM CLIP NAME: {Path(f).name}", ""]
            rec += b - a
        (out / f"{stem}.edl").write_text("\n".join(lines), encoding="utf-8")
        lst = tmp / "list.txt"
        lst.write_text("".join("file '" + q.as_posix().replace("'", "'\\''") + "'\n" for q in parts), encoding="utf-8")
        final = out / f"{stem}.mp4"
        ff(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(final)])
        emit(("stage", 4, "done"))
        emit(("prog", 1.0))
        emit(("sub", 1.0, "Finished"))
        emit(("done", dict(video=final, folder=out, moments=len(cuts), kept=tot, src=total)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
