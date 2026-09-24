# ePub Text Extractor

Extracts chapter text from Japanese EPUB files, with proper handling of
furigana (ruby text — the small hiragana readings printed above/next to
kanji in Japanese ebooks). 

This tool is optimized to generate text files for audiobook generation tool on my other repo.
Please take a look at [JP-Audiobook-Generator](https://github.com/hermanismail/JP-Audiobook-Generator)

> **⚠️ Disclaimer:** Only use this tool on EPUB files you have the legal
> right to extract text from — for example, books you've purchased for
> personal use, public-domain works, or your own writing. Extracted text
> is still subject to copyright even after it's converted to `.txt`.
> Redistributing or publishing extracted text from a copyrighted book
> without permission from the rights holder can create legal liability
> for you. This project does not include, host, or distribute any book
> content — it is a text-processing tool only, and responsibility for
> how it's used with any given EPUB rests with the person running it.

## Setup (one-time)

Run below scripts on your working folder. Worth checking the requirements.txt beforehand to prevent unecessary install.

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

This opens settings/progress
windows.

### Settings screen

![Settings screen](GUI-setting.png)

The top section picks the input/output paths; the panel below controls
how extraction behaves.

| Field / control | What it does |
| --- | --- |
| **EPUB File** | Path to the Japanese `.epub` file to extract from. Use **Browse** to pick it with a file dialog. |
| **Output Folder** | Where the extracted `.txt` files are saved. Defaults to an `output` folder next to the epub once a file is picked; **Browse** lets you pick or create a different folder. |
| **Auto-detect chapters** | **ON** by default. Splits spine items into real chapters (numbered `chapter_001.txt`, `chapter_002.txt`, ... directly in the output folder) vs. everything else (title pages, colophons — sent to `non-chapters-files\`); an afterword is always asked with a Yes/No dialog. See "Chapter detection" below. Turning this **OFF** falls back to the flat mode — every item gets its own numbered file, no chapter/non-chapter split. |
| **Keep furigana** | **OFF** by default, meaning furigana (ruby readings) are discarded and only the base kanji/text is kept. Turn **ON** to keep the reading inline instead, rendered as 漢字(かんじ). |
| **Keep scene-divider glyphs** | **OFF** by default, meaning a typographic scene-divider glyph (e.g. a centered ＊) is dropped from the text and turned into a section break (blank line) instead — so it isn't read aloud by a downstream TTS engine. Turn **ON** to keep the glyph as literal text rather than converting it to a section break. |
| **Extract** | Starts extraction and opens the progress window below. |

### Progress / completion screen

![Progress and completion screen](GUI-completion.png)

| Element | What it shows |
| --- | --- |
| Status banner (✓ **Completed** / in-progress / failed) | Overall run status, with a short one-line summary underneath. |
| Progress bar | Live position — "Item *N* of *Total*", the current item being read (e.g. `Reading: text00023`), and percent complete. |
| **Items Scanned** | Total spine items read from the epub. |
| **Chapters Found** | How many of those were classified as real chapters (written as `chapter_NNN.txt`). |
| **Non-Chapters** | How many were classified as front/back matter etc. (written to `non-chapters-files\`). |
| **Total Time** | Wall-clock time the extraction run took. |
| **Process Log** | Scrolling, timestamped log of each item as it's processed — shows whether each was classified as a chapter or non-chapter, its detected title, and character count. **Clear Log** clears this panel (doesn't affect output files). |
| **Open Output Folder** | Opens the output folder (from the Settings screen) in File Explorer. |
| **Close** | Closes the progress window. |

## Command line

Activate the virtual environment first (if not already active):

```powershell
cd C:\ePub-Text-Extractor
.\venv\Scripts\Activate.ps1
```

Then run:

```powershell
# Chapter detection on by default: chapter_001.txt, chapter_002.txt, ...
# in the output folder; everything else goes into non-chapters-files\.
# A book with an afterword stops until you answer --afterword yes|no.
python epub_extractor.py "C:\path\to\book.epub" -o output --afterword no

# Only detect: save output\chapters.plan.json and print the table
python epub_extractor.py "C:\path\to\book.epub" -o output --plan-only

# Write from a plan you checked or edited ("include": true/false per row)
python epub_extractor.py --from-plan output\chapters.plan.json -o output

# Write a plan that has warnings without stopping for review
python epub_extractor.py "C:\path\to\book.epub" -o output --afterword no --yes

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

By default (GUI: "Auto-detect chapters" ON; CLI: no `--flat` flag) the
chapters are found by `book_structure.py` and written down as
**`chapters.plan.json`** in the output folder *before* any text is written.
The log shows the plan as a table: every unit of the book, its kind
(front / part / chapter / afterword / back), whether it will be written as
a chapter, its title, length and first words.

Two independent sources are cross-checked:

- **Headings in the text** — a short paragraph that is a number (`１`,
  `12`), `第N章` / `第N話` (kanji incl. 壱弐参, spaces inside allowed), or a
  number plus a title (`第１章　青豆`, `１　火曜日のねじまき鳥…`); image
  `alt` text counts. It must be followed by real text and not be a link,
  which rules out the book's own contents page and part dividers, and the
  numbers must count 1, 2, 3… (restarting after a `第N部` is allowed).
- **The table of contents** (`nav.xhtml`, else `toc.ncx`) — each entry's
  label says what it is (chapter, あとがき, 奥付, 第N部, 目次…) and its link
  says where it starts.

When both find the chapters and agree, the plan says `agree`; when they
disagree, the TOC is used and the run **stops for review** (GUI: a
Yes/No dialog; CLI: `--yes` or `--from-plan`). Chapters are cut at their
headings **regardless of file boundaries**, so a chapter spread over
several files, or several chapters in one file, come out whole.

- **Chapters** are written as `chapter_001.txt`, `chapter_002.txt`, … —
  the naming `JP-Audiobook-Generator` expects, so the output folder can be
  its input folder.
- **Every chapter file opens with a standard header**, whatever the book
  itself printed (`１`, `第壱章`, `第　１　章`, a picture, nothing):

  ```
  第1章

  青豆

  タクシーのラジオは、ＦＭ放送の…
  ```

  `第X章`, a blank line, then the chapter's title on its own line and
  another blank line if it has one. X is the chapter's **own** number as
  the book counts it, so a numbering drift is visible at a glance. The
  book's heading line (and a title line under it) is removed from the
  text, since the header now carries it. A chapter with no number of its
  own gets one: a prologue before chapter 1 becomes `第0章` (kafka's
  カラスと呼ばれる少年), and one after the last numbered chapter carries on
  counting (yojo-senki-2's 外伝　借りてきた猫 → `第8章`). An afterword
  answered "yes" gets the next number too. No two chapters ever share a
  number, and `check_books.py` checks that on every book.
- **An afterword (あとがき, 解説…) is always asked, never decided** — GUI:
  a Yes/No dialog; CLI: `--afterword yes|no` (without it the run stops
  after saving the plan). Yes makes it the next chapter.
- **Everything else** (title pages, contents, part dividers, colophons,
  credits) goes into `non-chapters-files\` as `NNN_Title.txt`, NNN being
  the unit's row in the plan.

To correct a plan by hand, set a row's `"include"` to `true`/`false` in
`chapters.plan.json` and run `--from-plan`. A plan remembers the epub's
SHA-256 and refuses to write if the file has changed.

**Vertical-form text is normalised**: some epubs are typeset with
vertical presentation forms (`﹁﹂` for `「」`, `｜` for the long vowel
`ー`). They are mapped back, `｜` only straight after kana, and the table
reports how many characters changed.

**`check_books.py`** runs detection over the real sample books in
`F:\EPUB` and checks each against a verified chapter count, titles and
text. Run it after touching `book_structure.py`; `test_extractor.py`
needs no books.

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

## Notes / known limitations

- A book with no numbered headings AND no usable table of contents falls
  back to one chapter per long file and always stops for review.
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
- A new book that trips the detection: add it to `CHECKS` in
  `check_books.py` with what it should produce, then fix
  `book_structure.py` until every book passes.

