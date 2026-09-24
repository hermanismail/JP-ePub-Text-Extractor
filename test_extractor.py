"""
Standalone test harness, no real book needed (check_books.py covers the
real ones).

1. The chapter-plan path, end to end, on a small REAL epub built into a
   temp folder: one chapter split over two files, two chapters in one
   file, a contents page listing every heading, vertical-form text, an
   afterword and a colophon. Checks that the afterword is ASKED (the run
   stops without an answer), that the answer is honoured, the plan file,
   the chapter files and their text.
2. --flat (one file per spine item) through ebooklib.
3. parse_heading / classify_label unit checks.

    venv\\Scripts\\python test_extractor.py
"""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import book_structure as bs  # noqa: E402
import epub_extractor as ee  # noqa: E402

BODY = "　" + "これは本文の一文です。" * 60        # ~660 chars: a real chapter's worth


def page(body):
    return ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>本</title></head><body>{body}</body></html>")


FILES = {
    "front.xhtml": page('<p>表紙の注意書きです。</p>'),
    "toc.xhtml": page('<p><a href="c1.xhtml">１</a></p><p><a href="c23.xhtml">２</a></p>'
                      '<p><a href="c23.xhtml#h3">３</a></p>'),
    "c1.xhtml": page(f'<h2>１</h2><p>{BODY}</p><p><br/></p><p>＊</p><p>﹁コ｜ヒ｜だ﹂</p>'),
    "c1b.xhtml": page(f'<p>{BODY}続き。</p>'),      # chapter 1 continues, no heading
    "c23.xhtml": page(f'<p>２</p><p>{BODY}</p><p id="h3">３</p><p>{BODY}</p>'),
    "after.xhtml": page(f'<p>あとがき</p><p>{BODY}</p>'),
    "colophon.xhtml": page('<p>二〇二六年発行</p>'),
}
SPINE = list(FILES)


def build_epub(path):
    manifest = "".join(f'<item id="i{i}" href="{n}" media-type="application/xhtml+xml"/>'
                       for i, n in enumerate(SPINE))
    spine = "".join(f'<itemref idref="i{i}"/>' for i in range(len(SPINE)))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>テスト本</dc:title>'
           '</metadata><manifest>' + manifest +
           '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
           '</manifest><spine>' + spine + '</spine></package>')
    nav = page('<nav epub:type="toc"><ol><li><a href="after.xhtml">あとがき</a></li>'
               '<li><a href="colophon.xhtml">奥付</a></li></ol></nav>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" '
                   'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        for name, html in FILES.items():
            z.writestr(f"OEBPS/{name}", html)


def test_plan_path(tmp):
    epub_path = tmp / "book.epub"
    build_epub(epub_path)
    plan = bs.build_plan(epub_path)
    print(bs.plan_table(plan))
    chapters = [u for u in plan["units"] if u["kind"] == "chapter"]
    assert plan["method"] == "headings", plan["method"]
    assert [u["title"] for u in chapters] == ["１", "２", "３"], [u["title"] for u in chapters]
    assert len(plan["questions"]) == 1, plan["questions"]
    assert plan["normalised"].get("﹁") == 1 and plan["normalised"].get("｜→ー") == 2, plan["normalised"]

    # no answer -> stops after saving the plan, writes no chapter
    out = tmp / "out"
    r = ee.extract_epub(str(epub_path), str(out), False, False, log=lambda *_: None)
    assert r.stopped and not list(out.glob("chapter_*.txt")), r
    assert (out / bs.PLAN_NAME).is_file()

    # answered no: three chapters, afterword with the rest
    r = ee.extract_epub(str(epub_path), str(out), False, False, log=lambda *_: None,
                        include_afterword=False)
    names = sorted(p.name for p in out.glob("chapter_*.txt"))
    assert names == ["chapter_001.txt", "chapter_002.txt", "chapter_003.txt"], names
    ch1 = (out / "chapter_001.txt").read_text(encoding="utf-8")
    assert ch1.startswith("第1章\n\nこれは本文"), repr(ch1[:20])   # header replaces the "１" line
    assert "続き。" in ch1, "chapter 1 must continue into the next file"
    assert "「コーヒーだ」" in ch1 and "＊" not in ch1, ch1[-40:]
    assert "\n\n" in ch1
    ch2 = (out / "chapter_002.txt").read_text(encoding="utf-8")
    assert ch2.startswith("第2章\n\n") and "３" not in ch2, "chapter 3 must be cut out of chapter 2's file"
    assert bs.chapter_header({"number": 7, "name": ""}) == ["第7章"]
    assert bs.chapter_header({"number": 0, "name": "カラスと呼ばれる少年"}) == ["第0章", "カラスと呼ばれる少年"]
    others = sorted(p.name for p in (out / ee.NON_CHAPTER_SUBDIR).glob("*.txt"))
    assert any("あとがき" in n for n in others) and any("奥付" in n for n in others), others
    saved = json.loads((out / bs.PLAN_NAME).read_text(encoding="utf-8"))
    assert saved["written"]["chapters"] == 3

    # the ask callback, answered yes: the afterword becomes chapter 4
    out2 = tmp / "out2"
    asked = []
    r = ee.extract_epub(str(epub_path), str(out2), False, False, log=lambda *_: None,
                        ask=lambda kind, text: asked.append(kind) or True)
    assert asked == ["afterword"], asked
    after = (out2 / "chapter_004.txt").read_text(encoding="utf-8")
    assert after.startswith("第4章\n\nあとがき\n\nこれは本文"), repr(after[:24])

    # a hand-edited plan: drop chapter 3, write from the plan
    out3 = tmp / "out3"
    edited = bs.load_plan(out / bs.PLAN_NAME)
    for u in edited["units"]:
        if u.get("chapter") == 3:
            u["include"] = False
    (tmp / "edited.json").write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
    ee.extract_from_plan(tmp / "edited.json", out3, log=lambda *_: None)
    assert sorted(p.name for p in out3.glob("chapter_*.txt")) == ["chapter_001.txt", "chapter_002.txt"]

    # flat still works (ebooklib)
    flat = tmp / "flat"
    ee.extract_epub(str(epub_path), str(flat), False, False, detect_chapters=False,
                    log=lambda *_: None)
    assert len(list(flat.glob("*.txt"))) == len(SPINE), list(flat.glob("*.txt"))
    assert not (flat / ee.NON_CHAPTER_SUBDIR).exists()


def test_rules():
    assert bs.parse_heading("１") == (1, "chapter", "")
    assert bs.parse_heading("第　１　章")[:2] == (1, "chapter")
    assert bs.parse_heading("第壱章　ダキア戦役") == (1, "chapter", "ダキア戦役")
    assert bs.parse_heading("第24章　天吾　ここではない世界")[0] == 24
    assert bs.parse_heading("13　間宮中尉の長い話・２")[0] == 13
    assert bs.parse_heading("第二部")[1] == "part"
    assert bs.parse_heading("12月の雨") is None, "a number glued to a word is not a heading"
    assert bs.parse_heading("これは普通の文です。") is None
    assert bs.classify_label("あとがき") == "afterword"
    assert bs.classify_label("文庫版あとがき") == "afterword"
    assert bs.classify_label("奥付") == "back"
    assert bs.classify_label("目次") == "front"
    assert bs.classify_label("外伝　借りてきた猫") == "chapter"
    assert bs.classify_label("第一部") == "part"
    assert bs.kanji_number("二十三") == 23 and bs.kanji_number("二〇") == 20
    counts = {}
    assert bs.normalise_vertical("﹁コ｜ヒ｜﹂｜", counts) == "「コーヒー」｜", "｜ after a bracket stays"


def run():
    tmp = Path(tempfile.mkdtemp(prefix="epub_extract_test_"))
    try:
        test_rules()
        test_plan_path(tmp)
        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    run()
