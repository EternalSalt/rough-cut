"""Stream Cutter engine: audio analysis, moment picking, rendering. No GUI code in here."""
import hashlib, json, os, shutil, subprocess, time, urllib.request, zipfile
from pathlib import Path
import numpy as np

SR, WIN = 16000, 0.5          # audio sample rate, seconds per analysis window
W, H, FPS = 1920, 1080, 60    # format of the rendered rough cut
CLIP_KEEP_ALL = 90            # files shorter than this (sec) are treated as pre-picked clips and kept whole
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


def pick(db, target, pre, post, gap=5.0, min_len=6.0):
    """Moments that are loud relative to the surrounding minute (yelling, laughing, reactions),
    never dead air. The threshold is tuned so the result is at most `target`% of the footage."""
    dur = len(db) * WIN
    if dur < CLIP_KEEP_ALL:
        return [(0.0, dur)] if dur else []
    live = db > np.percentile(db, 10) + 6
    if not live.any():
        return []
    flat = np.where(live, db, np.median(db[live]))      # dead air must not drag the baseline down
    score = np.convolve(flat - np.convolve(flat, np.ones(120) / 120, "same"), np.ones(3) / 3, "same")
    score = np.where(live, score, -99.0)

    def cut(pct):
        thr = max(np.percentile(score[live], 100 - pct), 3.0)   # at least 3 dB above the local norm
        segs = []
        for i in np.flatnonzero(score >= thr):
            s, e = i * WIN - pre, (i + 1) * WIN + post
            if segs and s - segs[-1][1] <= gap:
                segs[-1][1] = max(segs[-1][1], e)
            else:
                segs.append([s, e])
        return [(max(0.0, s), min(dur, e)) for s, e in segs if e - s >= min_len]

    lo, hi, best = 0.5, 100.0, []
    for _ in range(8):
        mid = (lo + hi) / 2
        segs = cut(mid)
        if sum(e - s for s, e in segs) <= target / 100 * dur:
            lo, best = mid, segs
        else:
            hi = mid
    return best


# ---------- full pipeline ----------
def build(proj, files, cfg, emit, cancel):
    """Runs everything and reports through emit(tuple). Events:
    stage(i, run|done), prog(0-1), sub(0-1, text), file(path, col, text), segs(list), done(dict)."""
    out, cache = proj / "output", proj / "cache"
    tmp = out / "_tmp"
    cache.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

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
        total = sum(d for d, _ in info.values()) or 1

        # 1. listen
        emit(("stage", 0, "run"))
        dbs, base = {}, 0.0
        for f in files:
            chk()
            dur, name = info[f][0], Path(f).name
            npy = cache / (hashlib.md5(f"{f}{os.path.getmtime(f)}{cfg['track']}".encode()).hexdigest()[:10] + ".npy")

            def frac(x):
                emit(("prog", 0.35 * (base + x * dur) / total))
                emit(("sub", x, f"Listening to {name}  ·  {x * 100:.0f}%"))
                emit(("file", f, "status", f"Listening {x * 100:.0f}%"))

            if npy.exists():
                db = np.load(npy)
                frac(1)
            else:
                db = loudness(f, cfg["track"], dur, frac, cancel)
                if not len(db) and cfg["track"]:
                    db = loudness(f, 0, dur, frac, cancel)
                np.save(npy, db)
            dbs[f] = db
            base += dur
        emit(("stage", 0, "done"))

        # 2. pick
        emit(("stage", 1, "run"))
        cuts, view = [], []
        for f in files:
            db = dbs[f]
            if not len(db):
                emit(("file", f, "status", "No audio"))
                continue
            segs = pick(db, cfg["target"], cfg["pre"], cfg["post"])
            emit(("file", f, "status", f"{len(segs)} moments"))
            emit(("file", f, "kept", mmss(sum(e - s for s, e in segs))))
            view.append((len(db) * WIN, segs))
            cuts += [(f, s, e) for s, e in segs]
        emit(("segs", view))
        emit(("prog", 0.37))
        emit(("stage", 1, "done"))
        if not cuts:
            raise RuntimeError("No good moments found. Try raising Max length, or pick a different audio track.")

        # 3. render
        emit(("stage", 2, "run"))
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={FPS},format=yuv420p")
        enc = (["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "21"] if cfg["gpu"]
               else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"])
        tot, done, parts = sum(e - s for _, s, e in cuts), 0.0, []
        for i, (f, s, e) in enumerate(cuts):
            chk()
            emit(("sub", done / tot, f"Rendering clip {i + 1} of {len(cuts)}"))
            emit(("prog", 0.37 + 0.58 * done / tot))
            p = tmp / f"{i:04d}.mp4"
            ff(["ffmpeg", "-y", "-v", "error", "-ss", f"{s:.2f}", "-i", f, "-t", f"{e - s:.2f}",
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, *enc,
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(p)])
            done += e - s
            parts.append(p)
        emit(("stage", 2, "done"))

        # 4. export
        emit(("stage", 3, "run"))
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
        emit(("stage", 3, "done"))
        emit(("prog", 1.0))
        emit(("sub", 1.0, "Finished"))
        emit(("done", dict(video=final, folder=out, moments=len(cuts), kept=tot, src=total)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
