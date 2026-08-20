"""
Standalone test harness: exercises epub_extractor.extract_epub() end-to-end
using a minimal fake ebooklib.epub API (same technique the previous
verification round used, per claude/setup-status.md), since ebooklib itself
can't be installed in this sandbox (no PyPI access). This fakes just the
surface extract_epub() actually touches: epub.read_epub(), book.spine,
book.get_items_of_type(ITEM_DOCUMENT), item.get_id()/get_name()/get_content().

Synthesizes a mini "book" shaped like the real 村上春樹『スプートニクの恋人』
epub already validated in a previous session: a couple of front-matter
items with real headings or no heading, 3 numbered chapters, and a couple
of back-matter items - to check that chapter detection + the
non-chapters-files split behave as expected.
"""
import shutil
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# --- build a fake `ebooklib` module before importing epub_extractor -------
ITEM_DOCUMENT = "document"


class FakeItem:
    def __init__(self, item_id, name, content):
        self._id = item_id
        self._name = name
        self._content = content.encode("utf-8")

    def get_id(self):
        return self._id

    def get_name(self):
        return self._name

    def get_content(self):
        return self._content


class FakeBook:
    def __init__(self, items, spine):
        self._items = items
        self.spine = spine

    def get_items_of_type(self, _type):
        return self._items


def make_fake_book():
    items = [
        FakeItem("cover", "text00000.xhtml", "<html><body><p>表紙です。</p></body></html>"),
        FakeItem("note", "text00001.xhtml",
                 "<html><body><h1>スプートニクの恋人</h1><p>村上春樹</p></body></html>"),
        FakeItem("ch1", "text00002.xhtml",
                 "<html><body><h1>１</h1><p>22歳の春にすみれは恋に落ちた。</p>"
                 "<p><br/></p><p>それは激しい恋だった。</p></body></html>"),
        FakeItem("ch2", "text00003.xhtml",
                 "<html><body><h1>２</h1><p>ミュウは年上の女性だった。</p></body></html>"),
        FakeItem("ch3", "text00004.xhtml",
                 "<html><body><h1>３</h1><p>＊</p><p>物語は続く。</p></body></html>"),
        FakeItem("colophon", "text00005.xhtml",
                 "<html><body><p>本書は二〇〇一年に刊行されました。</p></body></html>"),
        FakeItem("empty_cover_img", "cover.xhtml", "<html><body></body></html>"),
    ]
    spine = [(item.get_id(), "yes") for item in items]
    return FakeBook(items, spine)


fake_epub_module = types.ModuleType("ebooklib.epub")
fake_epub_module.read_epub = lambda path: make_fake_book()

fake_ebooklib_module = types.ModuleType("ebooklib")
fake_ebooklib_module.epub = fake_epub_module
fake_ebooklib_module.ITEM_DOCUMENT = ITEM_DOCUMENT

sys.modules["ebooklib"] = fake_ebooklib_module
sys.modules["ebooklib.epub"] = fake_epub_module

import epub_extractor as ee  # noqa: E402


def run():
    out_dir = Path(tempfile.mkdtemp(prefix="epub_extract_test_"))
    try:
        log_lines = []
        progress_calls = []
        result = ee.extract_epub(
            "fake_book.epub", str(out_dir),
            keep_furigana=False, single_file=False, keep_scene_markers=False,
            detect_chapters=True,
            log=log_lines.append,
            on_progress=lambda *a: progress_calls.append(a),
        )

        print("--- log ---")
        for line in log_lines:
            print(line)
        print("--- result ---")
        print(result)

        chapter_files = sorted(p.name for p in out_dir.glob("chapter_*.txt"))
        non_chapter_dir = out_dir / ee.NON_CHAPTER_SUBDIR
        non_chapter_files = sorted(p.name for p in non_chapter_dir.glob("*.txt")) if non_chapter_dir.exists() else []

        print("chapter files:", chapter_files)
        print("non-chapter files:", non_chapter_files)

        assert chapter_files == ["chapter_001.txt", "chapter_002.txt", "chapter_003.txt"], chapter_files
        assert result.chapter_count == 3, result
        assert result.non_chapter_count == 3, result
        assert len(non_chapter_files) == 3, non_chapter_files
        assert len(progress_calls) == 7, progress_calls  # on_progress fires for every item, incl. empty ones

        # chapter_001.txt should contain the section-break from <p><br/></p>
        ch1_text = (out_dir / "chapter_001.txt").read_text(encoding="utf-8")
        assert "\n\n" in ch1_text, repr(ch1_text)
        print("chapter_001.txt content:", repr(ch1_text))

        # chapter_003.txt: the lone "＊" paragraph should have become a
        # section break (dropped from text), not literal narration.
        ch3_text = (out_dir / "chapter_003.txt").read_text(encoding="utf-8")
        assert "＊" not in ch3_text, repr(ch3_text)
        print("chapter_003.txt content:", repr(ch3_text))

        # --- also check --flat behavior (old flat naming, no split) ---
        out_dir_flat = Path(tempfile.mkdtemp(prefix="epub_extract_test_flat_"))
        result_flat = ee.extract_epub(
            "fake_book.epub", str(out_dir_flat),
            keep_furigana=False, single_file=False, keep_scene_markers=False,
            detect_chapters=False,
            log=lambda *_: None,
        )
        flat_files = sorted(p.name for p in out_dir_flat.glob("*.txt"))
        print("flat files:", flat_files)
        assert not (out_dir_flat / ee.NON_CHAPTER_SUBDIR).exists()
        assert len(flat_files) == 6, flat_files
        shutil.rmtree(out_dir_flat)

        # --- is_chapter_title rule sanity checks ---
        assert ee.is_chapter_title("１")
        assert ee.is_chapter_title("16")
        assert ee.is_chapter_title("第一章")
        assert ee.is_chapter_title("第3話")
        assert ee.is_chapter_title("Chapter 5")
        assert not ee.is_chapter_title("スプートニクの恋人")
        assert not ee.is_chapter_title("text00000")
        assert not ee.is_chapter_title("")

        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    run()
