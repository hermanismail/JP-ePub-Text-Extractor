"""
check_books.py - runs chapter detection over the real sample books and
checks each against what a person verified by reading it.

Run after touching book_structure.py:

    venv\\Scripts\\python check_books.py            (all books)
    venv\\Scripts\\python check_books.py dance      (one)
    venv\\Scripts\\python check_books.py --table    (also print each plan)

The books are not in the repo (F:\\EPUB, the user's own copies); a missing
one is reported as SKIP, not as a pass. Writes nothing.

Each entry records the trap that book set, because each one broke a
different assumption of the old one-rule detector.
"""
import os
import sys

import book_structure as bs

EPUB_DIR = os.environ.get("EPUB_DIR", r"F:\EPUB")

CHECKS = {
    # 70 chapters whose number is an IMAGE (alt="1"); TOC lists only parts.
    "街": dict(chapters=70, afterwords=1, method="headings",
              first="1", last="70", parts=3),
    # h2 numbers; ch.33 continues in a later file with no heading after a
    # picture page; ch.42's number is a <p>; credits + colophon after ch.44.
    "dance": dict(chapters=44, afterwords=0, method="headings", first="１", last="44",
                  min_chars={33: 14000}, not_in={44: "TRAVELIN"}),
    # 4 files, chapters are bare-number <p> inside; vertical forms ﹁﹂｜.
    "tsukuru": dict(chapters=19, afterwords=0, method="headings", first="１", last="19",
                    not_in_any=["﹁", "﹂", "コ｜ヒ｜", "天火"], contains={1: "「"}),
    # 第１章　青豆 headings + full TOC; the book's own 目次 lists them all.
    "1q84_1": dict(chapters=24, afterwords=0, method="headings+toc", agreement="agree",
                   first="第１章", last="第24章"),
    # 第　１　章 (spaces inside) + full TOC; unnumbered prologue first.
    "kafka_1": dict(chapters=24, afterwords=0, method="headings+toc", agreement="agree",
                    first="カラスと呼ばれる少年", last="第"),
    # headings are pictures with EMPTY alt - only the TOC knows.
    "nejimaki_1": dict(chapters=13, afterwords=0, method="toc", first="１", last="13"),
    # named chapters, title pages are pictures, a chapter spans many files,
    # 外伝 side story, 付録 + あとがき at the end.
    "yojo-senki-2": dict(chapters=8, afterwords=1, method="toc",
                         first="第一章", last="外伝"),
}


def check(name, spec, show_table):
    path = os.path.join(EPUB_DIR, name + ".epub")
    if not os.path.isfile(path):
        return "SKIP", [f"{path} not found"]
    plan = bs.build_plan(path)
    if show_table:
        print(bs.plan_table(plan))
    book = bs.Book(path)
    chapters = [u for u in plan["units"] if u["kind"] == "chapter"]
    afterwords = [u for u in plan["units"] if u["kind"] == "afterword"]
    fails = []

    def want(cond, msg):
        if not cond:
            fails.append(msg)

    want(len(chapters) == spec["chapters"], f"chapters {len(chapters)} != {spec['chapters']}")
    want(len(afterwords) == spec["afterwords"], f"afterwords {len(afterwords)} != {spec['afterwords']}")
    want(plan["method"] == spec["method"], f"method {plan['method']} != {spec['method']}")
    if "agreement" in spec:
        want(plan["agreement"] == spec["agreement"], f"agreement {plan['agreement']}")
    if chapters:
        want(chapters[0]["title"].startswith(spec["first"]), f"first title {chapters[0]['title']!r}")
        want(chapters[-1]["title"].startswith(spec["last"]), f"last title {chapters[-1]['title']!r}")
    want(len(plan["questions"]) == spec["afterwords"], "afterword must be asked, every time")
    if "parts" in spec:
        want(len({u["part"] for u in chapters if u["part"]}) == spec["parts"], "part names")
    for u in chapters:
        want(u["chars"] >= bs.MIN_FOLLOW_CHARS, f"chapter {u['chapter']} only {u['chars']} chars")
    texts = {u["chapter"]: bs.unit_text(book, u) for u in chapters}
    for n, least in spec.get("min_chars", {}).items():
        want(len(texts[n]) >= least, f"chapter {n} is {len(texts[n])} chars, expected >= {least}")
    for n, s in spec.get("not_in", {}).items():
        want(s not in texts[n], f"chapter {n} contains {s!r}")
    for n, s in spec.get("contains", {}).items():
        want(s in texts[n], f"chapter {n} lacks {s!r}")
    for s in spec.get("not_in_any", []):
        bad = [n for n, t in texts.items() if s in t]
        want(not bad, f"{s!r} in chapters {bad[:5]}")
    # every character of the book lands in exactly one unit
    total = sum(u["chars"] for u in plan["units"])
    want(total == book.text_chars(0, len(book.paras)), "units do not cover the book exactly")
    return ("PASS" if not fails else "FAIL"), fails


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = "--table" in sys.argv
    names = args or list(CHECKS)
    bad = 0
    for name in names:
        status, notes = check(name, CHECKS[name], show)
        bad += status == "FAIL"
        print(f"{status}  {name}")
        for n in notes:
            print(f"      {n}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
