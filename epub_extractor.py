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

Chapter detection: by default the book's chapters are found by
book_structure.py (numbered headings in the text, cross-checked against
the table of contents) and written down as chapters.plan.json in the
output folder BEFORE any text is written. Chapters are cut at their
headings regardless of file boundaries and written as chapter_001.txt,
chapter_002.txt, ... (the naming JP-Audiobook-Generator expects); front
and back matter go into a "non-chapters-files" subfolder. An afterword is
never decided automatically - it is always asked (--afterword yes|no). A
plan the two sources disagree on stops for review unless --yes is given;
edit chapters.plan.json and write it with --from-plan. --flat falls back
to one file per spine item.

Usage:
    python epub_extractor.py "path\\to\\book.epub" -o output_folder --afterword no
    python epub_extractor.py "path\\to\\book.epub" -o output_folder --plan-only
    python epub_extractor.py --from-plan output_folder\\chapters.plan.json -o output_folder
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


# Chapter detection lives in book_structure.py. The old rule here matched
# one heading per spine item against three title patterns; it fit one
# book and failed on most others (headings drawn as images, several
# chapters per file, a chapter spread over several files).

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
    plan_file: Optional[Path] = None
    # detection ran and the plan was saved, but nothing was written: an
    # unanswered afterword question or a plan waiting for review
    stopped: str = ""


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
    include_afterword=None,
    accept_review=False,
    ask=None,
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
    include_afterword: True/False answers the afterword question up front;
        None leaves it to `ask`.
    accept_review: write a plan that has warnings without asking.
    ask: optional callable(kind, text) -> bool, used by the chapter
        detection path. kind "afterword": include it as a chapter?
        kind "review": the plan has warnings - write it anyway? With no
        `ask` and no answer, the run stops after saving the plan.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    if detect_chapters and not single_file:
        return _extract_by_plan(epub_path, out_path, keep_furigana, keep_scene_markers,
                                log, on_progress, cancel_event, include_afterword,
                                accept_review, ask)

    book = epub.read_epub(epub_path)

    # Walk the spine in reading order so chapters come out in the order
    # the book is meant to be read, not just file order in the archive.
    spine_ids = [item_id for item_id, _linear in book.spine]
    items_by_id = {item.get_id(): item for item in book.get_items_of_type(ITEM_DOCUMENT)}
    total_items = len(spine_ids)

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

        chapters.append((pos, title, text, None))
        log(f"  [{pos:03d}/{total_items:03d}] item: \"{title}\" ({len(text)} chars)")
        if on_progress:
            on_progress(pos, total_items, title, None)

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

    # --flat: one file per spine item, in reading order
    for idx, title, text, _is_chap in chapters:
        fname = f"{idx:03d}_{sanitize_filename(title)}.txt"
        with open(out_path / fname, "w", encoding="utf-8") as f:
            f.write(text)
    log(f"Wrote {len(chapters)} chapter files to {out_path}")
    return ExtractResult(out_path, total_items, len(chapters), 0)


def _extract_by_plan(epub_path, out_path, keep_furigana, keep_scene_markers, log,
                     on_progress, cancel_event, include_afterword, accept_review, ask):
    """Detect -> save chapters.plan.json -> ask what must be asked -> write."""
    import book_structure as bs

    log(f"Detecting chapters in {Path(epub_path).name} ...")
    plan = bs.build_plan(epub_path, keep_furigana, keep_scene_markers)
    total = len(plan["units"])
    if on_progress:
        on_progress(total, total, "chapter plan", None)
    for line in bs.plan_table(plan).splitlines():
        log("  " + line)
    plan_file = bs.save_plan(plan, out_path)
    log(f"Plan saved: {plan_file}")

    def stop(reason):
        log(f"Stopped before writing: {reason}")
        return ExtractResult(out_path, total, 0, 0, plan_file=plan_file, stopped=reason)

    if plan["questions"]:
        if include_afterword is None and ask is not None:
            names = ", ".join(u["title"] for u in bs.open_questions(plan))
            include_afterword = ask("afterword", f"Include the afterword as a chapter?\n\n{names}")
        if include_afterword is None:
            return stop("the afterword question is unanswered (--afterword yes|no)")
        bs.answer_afterwords(plan, include_afterword)
        log(f"Afterword: {'included' if include_afterword else 'not included'}")
    if plan["needs_review"] and not accept_review:
        ok = ask is not None and ask("review", "\n".join(plan["warnings"]))
        if not ok:
            return stop(f"the plan needs review - edit {plan_file.name}, then write it "
                        f"with --from-plan (or re-run with --yes)")
    if cancel_event is not None and cancel_event.is_set():
        log("Cancelled by user.")
        return ExtractResult(out_path, total, 0, 0, cancelled=True, plan_file=plan_file)
    return _write_from_plan(plan, out_path, log)


def _write_from_plan(plan, out_path, log):
    import book_structure as bs

    stale = sorted(p.name for p in out_path.glob("chapter_*.txt"))
    bs.write_plan(plan, out_path, log=lambda s: log("  " + s))
    chapters = plan["written"]["chapters"]
    others = [f for f in plan["written"]["files"] if f.startswith(NON_CHAPTER_SUBDIR)]
    extra = [n for n in stale if n not in {f"chapter_{i:03d}.txt" for i in range(1, chapters + 1)}]
    if extra:
        log(f"Note: {len(extra)} older chapter file(s) from a previous run are still in "
            f"the folder and were not touched: {', '.join(extra[:5])}")
    log(f"Wrote {chapters} chapter file(s) to {out_path}")
    if others:
        log(f"Wrote {len(others)} non-chapter file(s) to {out_path / NON_CHAPTER_SUBDIR}")
    return ExtractResult(
        out_path, len(plan["units"]), chapters, len(others),
        non_chapter_dir=(out_path / NON_CHAPTER_SUBDIR) if others else None,
        plan_file=out_path / bs.PLAN_NAME,
    )


def extract_from_plan(plan_path, out_dir, log=print):
    """Write the text files from a saved (possibly hand-edited) plan."""
    import book_structure as bs

    return _write_from_plan(bs.load_plan(plan_path), Path(out_dir), log)


def main():
    parser = argparse.ArgumentParser(description="Extract chapter text from a Japanese EPUB file.")
    parser.add_argument("epub_path", nargs="?", help="Path to the .epub file")
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
    parser.add_argument(
        "--afterword", choices=["yes", "no"],
        help="Include the afterword (あとがき etc.) as a chapter. Always asked: "
             "without this flag a book that has one stops after saving the plan",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Write a plan that has warnings (sources disagree) without stopping for review",
    )
    parser.add_argument(
        "--plan-only", action="store_true",
        help="Detect chapters and save chapters.plan.json in the output folder; write no text",
    )
    parser.add_argument(
        "--from-plan", metavar="PLAN",
        help="Write the text files from a saved (possibly edited) chapters.plan.json",
    )
    args = parser.parse_args()

    if args.from_plan:
        import book_structure as bs
        plan = bs.load_plan(args.from_plan)
        if args.afterword:
            bs.answer_afterwords(plan, args.afterword == "yes")
        if bs.open_questions(plan):
            print("The plan has an unanswered afterword question - pass --afterword yes|no "
                  "or set its \"include\" in the plan.", file=sys.stderr)
            sys.exit(2)
        _write_from_plan(plan, Path(args.output), print)
        return

    if not args.epub_path or not os.path.isfile(args.epub_path):
        print(f"File not found: {args.epub_path}", file=sys.stderr)
        sys.exit(1)

    if args.plan_only:
        import book_structure as bs
        plan = bs.build_plan(args.epub_path, args.keep_furigana, args.keep_scene_markers)
        print(bs.plan_table(plan))
        print(f"Plan saved: {bs.save_plan(plan, args.output)}")
        return

    result = extract_epub(
        args.epub_path,
        args.output,
        args.keep_furigana,
        args.single_file,
        args.keep_scene_markers,
        detect_chapters=not args.flat,
        include_afterword=None if args.afterword is None else args.afterword == "yes",
        accept_review=args.yes,
    )
    if result.stopped:
        sys.exit(2)


if __name__ == "__main__":
    main()
