# Stream Cutter

Drop in your stream VODs or clips, name the project, click **Build rough cut**.
It listens for the loud, reactive moments (yelling, laughing, big plays), skips dead air,
and renders a rough cut plus an edit list you can open in Resolve or Premiere.

## Get the app (no Python needed)
1. Open the **Actions** tab on GitHub, pick the latest run of the branch you want.
2. Download the `StreamCutter-windows-<branch>` artifact, unzip it, run `StreamCutter.exe`.
3. First launch downloads ffmpeg once (about 100 MB), with a progress bar.

Tagged versions (`v0.2.0`, ...) also get a download on the **Releases** page.

## Run from source
```
pip install -r requirements.txt
python main.py
```

## Branches and versions
Every push to any branch builds a Windows app, so old branches stay downloadable.
```
git checkout -b experiment-whisper     # try something new
git push -u origin experiment-whisper  # builds its own .exe
git tag v0.3.0 && git push --tags      # publish a release build
```

## Output (per project, in Videos\StreamCutter\<project>\output)
- `rough_cut_<date>.mp4`   the cut (a new file every run, nothing overwritten)
- `rough_cut_<date>.edl`   same cuts as an edit list
- `rough_cut_<date>_cuts.txt`   timestamps of every cut

## Repo layout check
After pushing, the **top level** of the repo on GitHub must show `.github`, `stream_cutter`, `main.py` and `requirements.txt` directly.
If they sit inside another folder, the workflow will not appear in the Actions tab.
