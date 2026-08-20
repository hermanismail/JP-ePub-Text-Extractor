"""
epub_extractor.py
------------------
Extracts chapter text from a Japanese EPUB file.

Handles ruby/furigana markup (<ruby>漢字<rt>かんじ</rt></ruby>), which is
common in Japanese ebooks to show reading hints above kanji. By default,
furigana is stripped and only the base text is kept. Use --keep-furigana
to instead render it inline as 漢字(かんじ).

Paragraph/section structure is preserved in the output text: a normal
paragraph break becomes a single line break, while a blank spacer
paragraph in the source (or a typographic scene-divider glyph like "＊")
becomes a blank line (2+ line breaks in a row). This matches the
section/paragraph convention expected by downstream tools such as
JP-Audiobook-Generator (see its README section 7.1: "Section — a run of
sentences that ends with more than one CRLF in a row"). Use
--keep-scene-markers to keep divider glyphs as literal text instead.

Chapter detection (new): by default, each spine item is classified as a
real "chapter" or not, using a small set of rule-based patterns matched
against its detected title (see CHAPTER_TITLE_PATTERNS below). Chapters
are renumbered sequentially and written as chapter_001.txt, chapter_002.txt,
... directly in the output folder (matching the chapter_*.txt naming
JP-Audiobook-Generator's run_audiobook.py expects to find there). Anything
that doesn't look like a chapter (title pages, colophons, translator's
notes, etc.) is written into a "non-chapters-files" subfolder instead, so
it's out of the way but still easy to eyeball. This is intentionally a
rough-and-ready rule set, not a perfect classifier - use --flat to fall
back to the old behavior (every item written flat, no splitting) if it
guesses wrong for a given book, and extend CHAPTER_TITLE_PATTERNS as new
epub formats show up.

Usage:
    python epub_extractor.py "path\\to\\book.epub" -o output_folder
    python epub_extractor.py "path\\to\\book.epub" -o output_folder --keep-furigana
    python epub_extractor.py "path\\to\\book.epub" -o output_folder --single-file
    python epub_extractor.py "path\\to\\book.epub" -o output_folder --keep-scene-markers
    python epub_extractor.py "path\\to\\book.epub" -o output_folder --flat

Requires: ebooklib, beautifulsoup4, lxml (see requirements.txt)
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup, NavigableString


def sanitize_filename(name: str, max_len: int = 60) -> str:
    """Make a string safe to use as a Windows filename."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "untitled"
    return name[:max_len]


def resolve_ruby(soup: BeautifulSoup, keep_furigana: bool) -> None:
    """
    Replace <ruby>base<rt>reading</rt></ruby> constructs in-place.

    - keep_furigana=False (default): keep only the base text, drop <rt>/<rp>.
    - keep_furigana=True: render as base(reading), e.g. 漢字(かんじ).
    """
    for ruby in soup.find_all("ruby"):
        # <rp> tags hold the fallback parentheses for non-ruby-aware
        # readers; always drop them, we build our own formatting.
        for rp in ruby.find_all("rp"):
            rp.decompose()

        rt_tags = ruby.find_all("rt")
        reading = "".join(rt.get_text() for rt in rt_tags)
        for rt in rt_tags:
            rt.decompose()

        base_text = ruby.get_text()

        if keep_furigana and reading:
            new_text = f"{base_text}({reading})"
        else:
            new_text = base_text

        ruby.replace_with(NavigableString(new_text))


# Private-use codepoint used as a placeholder for a *real* line break
# (from <br> or a block-element boundary). Real ebook text should never
# contain this character, so it's safe to use as a marker.
_LINE_BREAK_MARK = ""

# Many Japanese ebook exports are pretty-printed XHTML: tags are indented
# with real newlines/spaces that carry no meaning (e.g. a <ruby> element's
# <rb>/<rt> children are often laid out one per line, and a <span> that
# wraps a couple of digits is often followed by "\n " before the next
# word). Those are source formatting artifacts, not paragraph breaks or
# intentional spaces, so they're stripped entirely. Only our explicit
# _LINE_BREAK_MARK insertions become real newlines in the output.
_PRETTY_PRINT_WS_RE = re.compile(r"[ \t]*[\r\n]+[ \t]*")


def _collapse_pretty_print_whitespace(text: str) -> str:
    return _PRETTY_PRINT_WS_RE.sub("", text)


_PARAGRAPH_TAGS = ["p", "h1", "h2", "h3", "h4", "h5", "h6", "li"]

# Characters commonly used in Japanese typesetting purely as a visual
# scene/section divider (e.g. a centered "＊" between scenes), not meant
# to be read aloud. A paragraph whose content, once stripped, consists
# ONLY of a short run of these is treated the same as a blank spacer
# paragraph: it becomes a section break instead of literal narration text.
_SCENE_BREAK_RE = re.compile(r"^[＊*・○●◎□■▽△◇☆★†‡~〜\-－ー]{1,10}$")


def _is_scene_break_marker(text: str) -> bool:
    return bool(_SCENE_BREAK_RE.match(text))


def extract_paragraphs(html: bytes, keep_furigana: bool, keep_scene_markers: bool) -> list:
    """
    Return a list of paragraph strings for one chapter's HTML, in
    document order.

    An empty string in the list marks a section break: either a
    genuinely blank/spacer paragraph in the source (e.g. `<p><br/></p>`,
    commonly used in Japanese ebook typesetting for extra breathing room
    between paragraphs), or — unless keep_scene_markers is set — a
    paragraph that's just a typographic scene-divider glyph like "＊",
    which would otherwise be read aloud verbatim by a TTS engine.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()

    resolve_ruby(soup, keep_furigana)

    paragraph_tags = soup.find_all(_PARAGRAPH_TAGS)
    # Drop tags nested inside another matched tag, so we don't extract
    # the same text twice (e.g. a <p> inside an <li>).
    paragraph_tags = [
        tag for tag in paragraph_tags if tag.find_parent(_PARAGRAPH_TAGS) is None
    ]

    if not paragraph_tags:
        # Fallback for bodies with no <p>/heading wrapper at all: treat
        # the whole thing as one paragraph.
        text = _collapse_pretty_print_whitespace(soup.get_text(separator=""))
        return [text.strip()] if text.strip() else []

    paragraphs = []
    for tag in paragraph_tags:
        text = _collapse_pretty_print_whitespace(tag.get_text(separator="")).strip()
        if text and not keep_scene_markers and _is_scene_break_marker(text):
            text = ""
        paragraphs.append(text)
    return paragraphs


def paragraphs_to_text(paragraphs: list) -> str:
    """
    Join paragraph strings into the final chapter text.

    A single "\\n" separates two ordinary paragraphs (a single CRLF once
    written on Windows); a run of one or more blank entries between two
    paragraphs collapses into exactly one blank output line ("\\n\\n",
    i.e. 2+ CRLF), which downstream tools can treat as a section break.
    Leading/trailing blank paragraphs are dropped.
    """
    parts = []
    pending_section_break = False
    for para in paragraphs:
        if not para:
            if parts:
                pending_section_break = True
            continue
        if parts:
            parts.append("\n\n" if pending_section_break else "\n")
        parts.append(para)
        pending_section_break = False
    return "".join(parts)


def html_to_text(html: bytes, keep_furigana: bool, keep_scene_markers: bool = False) -> str:
    return paragraphs_to_text(extract_paragraphs(html, keep_furigana, keep_scene_markers))


def get_chapter_title(html: bytes, fallback: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag_name in ["h1", "h2", "h3", "title"]:
        tag = soup.find(tag_name)
        if tag:
            title = _collapse_pretty_print_whitespace(tag.get_text()).strip()
            if title:
                return title
    return fallback


# ---------------------------------------------------------------------------
# Chapter detection (rule-based)
# ---------------------------------------------------------------------------
# A spine item is treated as a real "chapter" if its detected title (from
# get_chapter_title(), i.e. the first h1/h2/h3/<title> found in that item's
# HTML) matches one of these patterns. This was fit to a real 講談社文庫
# novel epub, where every chapter is headed by nothing but a bare number
# (e.g. "１" full-width, or "16"), while front/back matter (title page,
# publisher's notes, colophon, etc.) either has a descriptive heading or no
# heading at all (falling back to the item's internal filename, which
# won't match these patterns either). A couple of other very common
# Japanese novel/light-novel chapter-heading conventions are included too,
# since they're likely to show up in other books even though the one
# sample this was validated against doesn't use them.
#
# This is deliberately not meant to be a perfect/complete classifier - it's
# a starting rule set that will misclassify some books. When that happens:
#   1. Check the "non-chapters-files" subfolder - real chapters that were
#      missed land there, alongside genuine front/back matter.
#   2. Add a new pattern to this list (or loosen an existing one) to match
#      that book's chapter-heading convention, then re-run.
#   3. Or pass --flat to skip classification entirely and get every spine
#      item as its own file, numbered in reading order (the old behavior).
CHAPTER_TITLE_PATTERNS = [
    re.compile(r"^[0-9０-９]+$"),                                    # bare number: "1", "16", "１"
    re.compile(r"^第[0-9０-９一二三四五六七八九十百千]+[章話部編]$"),   # 第一章 / 第1話 / 第３部
    re.compile(r"^[Cc]hapter\s*[0-9０-９]+$"),                        # "Chapter 1" (romaji headers)
]


def is_chapter_title(title: str) -> bool:
    """Return True if `title` matches one of CHAPTER_TITLE_PATTERNS."""
    title = title.strip()
    if not title:
        return False
    return any(pattern.match(title) for pattern in CHAPTER_TITLE_PATTERNS)


NON_CHAPTER_SUBDIR = "non-chapters-files"


@dataclass
class ExtractResult:
    out_dir: Path
    total_items: int
    chapter_count: int
    non_chapter_count: int
    non_chapter_dir: Optional[Path] = None
    combined_file: Optional[Path] = None
    cancelled: bool = False


def extract_epub(
    epub_path: str,
    out_dir: str,
    keep_furigana: bool,
    single_file: bool,
    keep_scene_markers: bool = False,
    detect_chapters: bool = True,
    log=print,
    on_progress=None,
    cancel_event=None,
) -> ExtractResult:
    """
    Parse `epub_path` and write its chapter text into `out_dir`.

    log: called with each human-readable status line (defaults to print;
        the GUI passes a queue.put wrapper instead).
    on_progress: optional callable(pos, total, title, is_chapter) invoked
        once per spine item as it's processed - `is_chapter` is None for
        items skipped as empty, and for single_file/--flat runs where
        chapter/non-chapter classification doesn't apply.
    cancel_event: optional threading.Event; checked between items so a GUI
        can request an early, cooperative stop.
    """
    book = epub.read_epub(epub_path)
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Walk the spine in reading order so chapters come out in the order
    # the book is meant to be read, not just file order in the archive.
    spine_ids = [item_id for item_id, _linear in book.spine]
    items_by_id = {item.get_id(): item for item in book.get_items_of_type(ITEM_DOCUMENT)}
    total_items = len(spine_ids)

    classify = detect_chapters and not single_file

    chapters = []  # (idx, title, text, is_chapter or None)
    for pos, item_id in enumerate(spine_ids, start=1):
        if cancel_event is not None and cancel_event.is_set():
            log("Cancelled by user.")
            return ExtractResult(out_path, pos - 1, 0, 0, cancelled=True)

        item = items_by_id.get(item_id)
        if item is None:
            continue
        html = item.get_content()
        text = html_to_text(html, keep_furigana, keep_scene_markers)
        fallback_name = Path(item.get_name()).stem
        title = get_chapter_title(html, fallback_name)

        if not text.strip():
            log(f"  [{pos:03d}/{total_items:03d}] (empty, skipped) \"{title}\"")
            if on_progress:
                on_progress(pos, total_items, title, None)
            continue

        is_chap = is_chapter_title(title) if classify else None
        chapters.append((pos, title, text, is_chap))

        if classify:
            tag = "chapter" if is_chap else "non-chapter"
        else:
            tag = "item"
        log(f"  [{pos:03d}/{total_items:03d}] {tag}: \"{title}\" ({len(text)} chars)")
        if on_progress:
            on_progress(pos, total_items, title, is_chap)

    if not chapters:
        log("No chapter text found. The EPUB may use an unsupported structure.")
        return ExtractResult(out_path, total_items, 0, 0)

    if single_file:
        combined_path = out_path / (Path(epub_path).stem + "_full_text.txt")
        with open(combined_path, "w", encoding="utf-8") as f:
            for idx, title, text, _is_chap in chapters:
                f.write(f"\n\n===== {idx:03d} {title} =====\n\n")
                f.write(text)
                f.write("\n")
        log(f"Wrote {len(chapters)} chapters to {combined_path}")
        return ExtractResult(out_path, total_items, len(chapters), 0, combined_file=combined_path)

    if not detect_chapters:
        for idx, title, text, _is_chap in chapters:
            fname = f"{idx:03d}_{sanitize_filename(title)}.txt"
            with open(out_path / fname, "w", encoding="utf-8") as f:
                f.write(text)
        log(f"Wrote {len(chapters)} chapter files to {out_path}")
        return ExtractResult(out_path, total_items, len(chapters), 0)

    # --- default: rule-based chapter detection + separation ---
    non_chapter_dir = out_path / NON_CHAPTER_SUBDIR
    chapter_num = 0
    non_chapter_count = 0
    for idx, title, text, is_chap in chapters:
        if is_chap:
            chapter_num += 1
            fpath = out_path / f"chapter_{chapter_num:03d}.txt"
        else:
            non_chapter_dir.mkdir(parents=True, exist_ok=True)
            non_chapter_count += 1
            fpath = non_chapter_dir / f"{idx:03d}_{sanitize_filename(title)}.txt"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(text)

    log(f"Wrote {chapter_num} chapter file(s) to {out_path}")
    if non_chapter_count:
        log(f"Wrote {non_chapter_count} non-chapter file(s) to {non_chapter_dir}")
    return ExtractResult(
        out_path, total_items, chapter_num, non_chapter_count,
        non_chapter_dir=non_chapter_dir if non_chapter_count else None,
    )


def main():
    parser = argparse.ArgumentParser(description="Extract chapter text from a Japanese EPUB file.")
    parser.add_argument("epub_path", help="Path to the .epub file")
    parser.add_argument("-o", "--output", default="output", help="Output folder (default: output)")
    parser.add_argument(
        "--keep-furigana",
        action="store_true",
        help="Render furigana inline as base(reading) instead of discarding it",
    )
    parser.add_argument(
        "--single-file",
        action="store_true",
        help="Write all chapters into one combined text file instead of one file per chapter",
    )
    parser.add_argument(
        "--keep-scene-markers",
        action="store_true",
        help=(
            "Keep typographic scene-divider paragraphs (e.g. a lone '＊') as literal "
            "text instead of converting them into a section break (blank line)"
        ),
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help=(
            "Disable chapter detection: write every spine item as its own numbered "
            "file directly in the output folder, without splitting off "
            f"non-chapter items into a '{NON_CHAPTER_SUBDIR}' subfolder"
        ),
    )
    args = parser.parse_args()

    if not os.path.isfile(args.epub_path):
        print(f"File not found: {args.epub_path}", file=sys.stderr)
        sys.exit(1)

    extract_epub(
        args.epub_path,
        args.output,
        args.keep_furigana,
        args.single_file,
        args.keep_scene_markers,
        detect_chapters=not args.flat,
    )


if __name__ == "__main__":
    main()
