# ePub Text Extractor

Extracts chapter text from Japanese EPUB files, with proper handling of
furigana (ruby text — the small hiragana readings printed above/next to
kanji in Japanese ebooks).

## Setup (one-time)

See the PowerShell commands your Claude session gave you. In short:

```powershell
cd C:\ePub-Text-Extractor
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

`requirements.txt` includes `customtkinter`, used by the GUI
(`epub_extractor_gui.py`). If you only ever use the command line, it's
harmless to have installed but not required to run `epub_extractor.py`
directly.

## GUI (recommended)

```powershell
cd C:\ePub-Text-Extractor
.\venv\Scripts\Activate.ps1
python epub_extractor_gui.py
```

This opens a window styled after `JP-Audiobook-Generator`'s settings/progress
windows:

1. **EPUB File** — Browse to pick the `.epub` file.
2. **Output Folder** — Browse to pick (or create) where the extracted
   `.txt` files go. Defaults to an `output` folder next to the epub once
   you pick a file.
3. Three switches:
   - **Auto-detect chapters** (on by default) — see "Chapter detection"
     below.
   - **Keep furigana** — off by default (furigana is discarded); turn on
     to render it inline as 漢字(かんじ) instead.
   - **Keep scene-divider glyphs** — off by default (glyphs like "＊"
     become a blank-line section break); turn on to keep them as literal
     text instead.
4. Click **Extract**. A progress window opens showing a live log of each
   item as it's read, then a completed/failed summary with **Open Output
   Folder** and **Close** buttons.

## Command line

Activate the virtual environment first (if not already active):

```powershell
cd C:\ePub-Text-Extractor
.\venv\Scripts\Activate.ps1
```

Then run:

```powershell
# Chapter detection on by default: chapter_001.txt, chapter_002.txt, ...
# in the output folder; everything else goes into non-chapters-files\
python epub_extractor.py "C:\path\to\book.epub" -o output

# Keep furigana inline, e.g. 漢字(かんじ)
python epub_extractor.py "C:\path\to\book.epub" -o output --keep-furigana

# One combined .txt file for the whole book instead of per-chapter files
# (chapter detection doesn't apply in this mode)
python epub_extractor.py "C:\path\to\book.epub" -o output --single-file

# Keep typographic scene-divider glyphs (e.g. a lone ＊) as literal text
# instead of converting them to a section break
python epub_extractor.py "C:\path\to\book.epub" -o output --keep-scene-markers

# Old flat behavior: every item as its own numbered file, no chapter
# detection / no non-chapters-files split
python epub_extractor.py "C:\path\to\book.epub" -o output --flat
```

## Chapter detection

By default (GUI: "Auto-detect chapters" ON; CLI: no `--flat` flag), each
EPUB spine item is classified as a real chapter or not, based on its
detected title (the first `<h1>`/`<h2>`/`<h3>`/`<title>` found in that
item's HTML):

- **Chapters** are renumbered in reading order and written directly in
  the output folder as `chapter_001.txt`, `chapter_002.txt`, etc. — this
  matches the `chapter_*.txt` naming `JP-Audiobook-Generator`'s
  `run_audiobook.py` expects to find in its input folder, so the output
  folder here can be pointed at directly as that pipeline's input.
- **Everything else** (title pages, publisher's notes, colophons,
  translator's afterwords, etc.) is written into a `non-chapters-files`
  subfolder, keeping the original `NNN_Title.txt` numbering so it's easy
  to trace back to its position in the book.

The classification rule (`CHAPTER_TITLE_PATTERNS` /
`is_chapter_title()` in `epub_extractor.py`) currently matches titles
that are:

- a bare number, half- or full-width (`1`, `16`, `１`) — this is the
  convention the sample book (村上春樹『スプートニクの恋人』, 講談社文庫)
  uses for every chapter
- `第...章` / `第...話` / `第...部` / `第...編` (e.g. 第一章, 第3話)
- `Chapter N` (romaji headers, seen in some epubs)

This is a **rough, rule-based first pass, not a perfect classifier** — by
design, since every book's epub is a little different and it's meant to
be verified by eye each run rather than trusted blindly. If it
misclassifies a book:

1. Check the `non-chapters-files` subfolder — a real chapter that got
   missed will be sitting in there under its original `NNN_Title.txt`
   name.
2. Add a pattern to `CHAPTER_TITLE_PATTERNS` (or loosen an existing one)
   to match that book's chapter-heading convention, and re-run.
3. Or turn off "Auto-detect chapters" (GUI) / pass `--flat` (CLI) to skip
   classification entirely and get the old flat, numbered-per-item output.

## Paragraph / section structure

Output is compatible with `JP-Audiobook-Generator`'s convention (see its
README §7.1): a normal paragraph break is written as a single line break,
and a blank line (2+ line breaks in a row) marks a **section** break.

Two things in the source EPUB become a section break:

- A blank "spacer" paragraph — Japanese ebook typesetting commonly inserts
  an empty `<p><br/></p>` between paragraphs for extra visual breathing
  room. Where the source has one, the output gets a blank line instead of
  just a normal paragraph break.
- A paragraph that's just a typographic scene-divider glyph (e.g. a
  centered "＊", "※", "○", or a run of dashes) — dropped from the text
  (so it isn't read aloud by a TTS engine) and turned into a blank line.
  Pass `--keep-scene-markers` / toggle "Keep scene-divider glyphs" in the
  GUI to keep the glyph as literal text instead.

Files are written with Python's normal text-mode `open(..., "w")`, so on
Windows a `\n` in the code becomes a real CRLF (`\r\n`) on disk — no extra
handling needed for that part.

## Verified against a real book

Tested end-to-end against 村上春樹『スプートニクの恋人』(講談社文庫 epub, 25
spine items, vertical-text typesetting with `<ruby><rb>/<rt></ruby>`
furigana). All 16 numbered chapters plus front/back matter extracted
cleanly: correct chapter order, correct furigana handling (including
multi-character ruby like `完膚(かんぷ)` split across two `<rb>/<rt>`
pairs), no stray line breaks from the source file's pretty-printed
indentation. Empty pages (e.g. an image-only cover) are skipped
automatically. Paragraph/section structure (see above) was checked
against this book's actual blank-spacer and "＊" divider paragraphs and
matches `text_pipeline.py`'s `split_sections()`/`split_paragraphs()`
logic (2+ CRLF vs. single CRLF). The chapter-detection rule (bare-number
titles) matches this book's actual heading convention exactly.

The chapter-detection + GUI code itself was verified with a synthetic
fake-`ebooklib` harness (`ebooklib` can't be installed in the cloud
sandbox that built this, so the harness fakes just the bit of its API
`extract_epub()` touches) shaped like this book's real structure — see
`test_extractor.py` for that harness if you want to re-run it. The GUI
window (`epub_extractor_gui.py`) itself could only be checked for valid
Python syntax in that sandbox (no display/tkinter available there) — it
hasn't been visually run yet. Please run it for real once and let your
Claude session know if anything looks off.

## Notes / known limitations

- Chapter titles are pulled from the first `<h1>`/`<h2>`/`<h3>`/`<title>`
  found in each spine item. If a book doesn't mark titles this way, files
  fall back to a numbered name based on the internal file name (and won't
  match the chapter-detection patterns, so they land in
  `non-chapters-files`).
- Ruby spanning multiple kanji with irregular groupings (common in some
  Japanese typesetting) is handled on a best-effort basis — the base text
  inside each `<ruby>` tag is kept in full; only the `<rt>` reading is
  stripped or parenthesized.
- Vertical-text-only or heavily illustrated EPUBs (some manga/light-novel
  editions) may have little or no extractable text if the "text" is
  actually embedded as images.
- If a chapter comes out empty or garbled, send the `.epub` back and we
  can inspect its internal HTML structure and adjust the parser.
- Scene-divider detection (`_SCENE_BREAK_RE` in the script) only matches
  short runs of common divider characters (＊, *, ・, ○, ●, ◎, □, ■, ▽,
  △, ◇, ☆, ★, †, ‡, ~, 〜, -, －, ー). A book using a different divider
  convention would need that pattern extended, or run with
  `--keep-scene-markers` and handle it downstream instead.
- Chapter detection (`CHAPTER_TITLE_PATTERNS`) only matches a few common
  numbering conventions for now — see "Chapter detection" above for how
  to extend it as new book formats come up.
=======
# JP-ePub-Text-Extractor
This tool will extract chapters from Japanese epub file and output to txt files.

