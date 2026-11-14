# Stream Cutter

Turn a long stream VOD into a rough cut shaped like a YouTube gaming video. Drop in footage, name the project,
click **Build rough cut**. There is nothing to configure: it decides the cut itself and tells you why.
You add the memes, captions and sound effects.

## How it decides
It tracks three things across your footage and keeps the moments where they spike:
- **Audio:** yelling, laughing, big reactions
- **Visual:** sudden movement and screen changes (explosions, glitches, chaos); frozen screens count as dead air
- **Speech:** reaction words, laughter and exclamations in what you actually say

Then it tunes everything from the footage itself: how much weight each signal gets (a signal with no real peaks
is ignored), how much run-up and aftermath to keep (more for talk-heavy streams, less for action), how hard to
trim dead stretches, and how picky to be. **Length is automatic**: an eventful stream gives a longer video, a
quiet one a shorter video (never more than half the footage or 40 minutes unless you set "Max length").
The panel on the right shows what it chose. **Fine-tune** is there if you want to override it.

## Engagement structure
- Clips start and end on sentence edges, never mid-sentence (needs speech reading)
- Opens with a 10-second teaser of the best moment (cold open)
- Optional trimming of dead stretches between peaks, soft audio fades on every cut, volume leveled to about
  -14 LUFS (YouTube's usual target)
- **AI director (optional, free):** a language model running **on your own PC** rates the candidate moments from
  their transcripts, drops the weak ones, picks the teaser and writes chapter titles. No account, no API key, no
  cost, and nothing leaves your computer.
  - It uses **Ollama** (ollama.com), or llama.cpp's `llama-server`, or LM Studio, whichever is running. It finds
    them by itself (Ollama on port 11434, llama.cpp on 8080, LM Studio on 1234).
  - **Model choice is automatic**: an installed model of about 3 to 8 billion parameters. Embedding models are
    skipped, and so are Ollama "cloud" models, because those run on Ollama's servers, not your PC. You can pick one
    in Fine-tune.
  - If Ollama is running but has no model, it asks before downloading a small one (about 2.5 GB, once).
  - It needs speech reading, and it is slow on a CPU (minutes, not seconds). If anything goes wrong it carries on
    with the automatic picks and tells you why.
  - Optional: choose "Claude API" instead if you have your own key (paid). The key is stored in plain text in
    `%LOCALAPPDATA%\StreamCutter\config.json`; empty the box and build to forget it.
- **Chapters without any AI:** moments with a clearly reactive phrase get a free chapter title from your own words
  (swearing masked); ordinary chatter is left unnamed.

## What it does not do
- It sees loudness, screen change and keywords, not true meaning. A menu popping open can score like a gag, and
  a quiet joke can be missed. Review `rough_cut_<date>_cuts.txt` and trim in your editor.
- The reaction-word list is English. Other languages still get audio and visual tracking.
- Memes, zooms, captions, music, titles and thumbnails are yours to add.

## Get the app (no Python needed)
1. Open the **Actions** tab on GitHub, pick the latest run of the branch you want.
2. Two artifacts are built:
   - `StreamCutter-speech-run<N>`: everything, including speech reading. Larger download.
   - `StreamCutter-lite-run<N>`: audio and visual tracking only. Use this if the speech build is missing or fails.
3. Unzip, run `StreamCutter.exe`. First launch downloads ffmpeg once (about 100 MB). The first time speech reading
   runs it also downloads a speech model (about 150 MB for the default).

Speech reading runs on the CPU, so the **first** scan of a long VOD is slow (the speech step dominates). Everything
is saved per file, so later runs and slider changes are instant. Turn off "Read speech" for a quick pass.

## Run from source
```
pip install -r requirements.txt
pip install -r requirements-speech.txt   # optional, enables speech reading
python main.py
```

## Branches and versions
Every push to any branch builds a Windows app, so old branches stay downloadable.
```
git checkout -b experiment-name        # try something new
git push -u origin experiment-name     # builds its own .exe
git tag v0.5.0 && git push --tags      # publish a release build
```

## Output (per project, in Videos\StreamCutter\<project>\output)
- `rough_cut_<date>.mp4`            the cut (a new file every run, nothing overwritten)
- `rough_cut_<date>.edl`            the same cuts as an edit list
- `rough_cut_<date>_cuts.txt`       timestamps of every cut ([cold open] marks the teaser)
- `rough_cut_<date>_chapters.txt`   YouTube chapters for the description (needs speech reading)

## Repo layout check
After pushing, the **top level** of the repo on GitHub must show `.github`, `stream_cutter`, `main.py` and
`requirements.txt` directly. If they sit inside another folder, the workflow will not appear in the Actions tab.
