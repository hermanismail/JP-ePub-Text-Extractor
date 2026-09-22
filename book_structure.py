"""
book_structure.py
-----------------
Finds a book's chapters and writes the answer down as a PLAN before any
text file is written.

Why a plan: no single rule finds the chapters of every epub. Seven books
were measured (see CHECKS in check_books.py) and each broke a different
assumption - headings drawn as images, a whole book in four files, a
chapter continuing across files, the book's own contents page listing
every heading, numbering restarting per part. So detection is split from
writing: `build_plan()` returns a JSON-able dict that says what was found,
from which source, what it is unsure about, and what it needs asked
(whether to include an afterword - ALWAYS asked, never decided).
`write_plan()` writes the text files from a plan, possibly edited by hand.

Two independent sources, cross-checked:

  A. headings in the text: a short paragraph that is a number, 第N章/話,
     or a number followed by a title (image alt text counts), which is
     followed by real text (MIN_FOLLOW_CHARS) and is not a link - that
     rules out the book's own contents page and part dividers. The
     numbers must count 1, 2, 3... ; they may restart at 1 after a part
     heading (第N部).
  B. the table of contents (nav.xhtml, else toc.ncx): each entry points at
     a file#anchor; its label says what it is (chapter, afterword,
     colophon, part...).

Chapters are cut at the chosen starts regardless of file boundaries, so a
chapter spread over several files, or several chapters in one file, both
come out whole.

Stdlib + bs4/lxml only. No UI code: the GUI, the CLI and (later) the
creation suite all call build_plan() / write_plan().
"""

import hashlib
import json
import posixpath
import re
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# epub content documents are XHTML; lxml's HTML parser reads them fine and
# is what the rest of this tool has always used.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from epub_extractor import (
    _PARAGRAPH_TAGS, _collapse_pretty_print_whitespace, _is_scene_break_marker,
    paragraphs_to_text, resolve_ruby, sanitize_filename, NON_CHAPTER_SUBDIR,
)

PLAN_VERSION = 1
PLAN_NAME = "chapters.plan.json"

# A heading must be followed by at least this much text before the next
# heading. The contents page (1q84, nejimaki) and part dividers (街) have
# none; the shortest real chapter measured is 1,219 chars (dance #33).
MIN_FOLLOW_CHARS = 500
# A candidate heading is short.
MAX_HEADING_CHARS = 64
# With no TOC entry to end the LAST chapter, it stops at the first later
# file shorter than this (dance: colophon and credits of 83-871 chars).
TAIL_FILE_CHARS = 500
# An unlabelled TOC entry after the chapters counts as a chapter only if
# it is at least this long.
UNKNOWN_CHAPTER_CHARS = 2000

# --- text normalisation ----------------------------------------------------
# Some books are typeset with vertical presentation forms (tsukuru: 1,652
# ﹁, 1,208 ｜ in place of ー and not one real ー). Mapped back so the
# generator's bracket rules and the TTS see normal text.
VERTICAL_FORMS = str.maketrans({
    "﹁": "「", "﹂": "」", "﹃": "『", "﹄": "』",
    "︵": "（", "︶": "）", "︷": "｛", "︸": "｝",
    "︹": "〔", "︺": "〕", "︻": "【", "︼": "】",
    "︽": "《", "︾": "》", "︿": "〈", "﹀": "〉",
    "﹇": "［", "﹈": "］", "︐": "，", "︑": "、",
    "︒": "。", "︓": "：", "︔": "；", "︕": "！",
    "︖": "？", "︙": "…", "︰": "‥",
})
# ｜ is a real character elsewhere (ruby markers in plain-text books), so
# it becomes ー only straight after kana or another ー.
_BAR_AFTER_KANA_RE = re.compile(r"(?<=[ぁ-ゖァ-ヺー])[｜︱]")


def normalise_vertical(text, counts):
    for ch in set(text) & set(VERTICAL_FORMS_KEYS):
        counts[ch] = counts.get(ch, 0) + text.count(ch)
    text = text.translate(VERTICAL_FORMS)
    while True:
        new, n = _BAR_AFTER_KANA_RE.subn("ー", text)
        if not n:
            return text
        counts["｜→ー"] = counts.get("｜→ー", 0) + n
        text = new


VERTICAL_FORMS_KEYS = "".join(chr(k) for k in VERTICAL_FORMS)

# --- numbers and labels ----------------------------------------------------
_KANJI_DIGITS = {c: i for i, c in enumerate("〇一二三四五六七八九")}
_KANJI_DIGITS.update({"零": 0, "壱": 1, "弐": 2, "参": 3, "肆": 4, "伍": 5,
                      "陸": 6, "漆": 7, "捌": 8, "玖": 9})
_KANJI_UNITS = {"十": 10, "拾": 10, "百": 100}
_NUM_CHARS = "0-9０-９〇一二三四五六七八九零壱弐参肆伍陸漆捌玖十拾百"
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")


def kanji_number(s):
    if all(c in _KANJI_DIGITS for c in s):          # positional: 二〇
        return int("".join(str(_KANJI_DIGITS[c]) for c in s))
    total = cur = 0
    for c in s:
        if c in _KANJI_DIGITS:
            cur = _KANJI_DIGITS[c]
        elif c in _KANJI_UNITS:
            total += (cur or 1) * _KANJI_UNITS[c]
            cur = 0
        else:
            return None
    return total + cur


def to_number(s):
    s = s.translate(_FULLWIDTH)
    return int(s) if s.isdigit() else kanji_number(s)


def squash(text):
    """Whitespace runs -> one ideographic space, trimmed."""
    return re.sub(r"[\s　]+", "　", text).strip("　")


# 第１章 / 第　１　章 / 第壱章 / １ / 12 / 第3話, optionally + a title.
_HEADING_RE = re.compile(
    rf"^(?:第　?([{_NUM_CHARS}]+)　?([章話回節部])|([{_NUM_CHARS}]+))(?:　?(.*))?$")
_PART_RE = re.compile(rf"^(?:第　?[{_NUM_CHARS}]+　?部|BOOK\s*\d+|[上中下]巻)", re.I)
_SPECIAL_CHAPTER_RE = re.compile(
    r"^(プロローグ|エピローグ|序章|終章|序|幕間|間章|断章|外伝|番外編?|"
    r"インターミッション|Prologue|Epilogue|Interlude|Chapter)", re.I)
_AFTERWORD_RE = re.compile(r"(あとがき|後書き|後記|解説|謝辞|Afterword)", re.I)
_BACK_RE = re.compile(
    r"^(奥付|参考文献|主要参考文献|付録|初出|著者紹介|著者略歴|出典|引用|"
    r"クレジット|Copyright|Credits|装丁|装幀|カバー|広告)", re.I)
_FRONT_RE = re.compile(
    r"^(目次|もくじ|CONTENTS|表紙|Cover|本編|扉|タイトル|口絵|登場人物|Title)", re.I)


def parse_heading(text):
    """(number, kind, title) for a heading-shaped text, else None.
    kind is 'chapter' or 'part'."""
    t = squash(text)
    if not t or len(t) > MAX_HEADING_CHARS:
        return None
    m = _HEADING_RE.match(t)
    if not m:
        return None
    digits = m.group(1) or m.group(3)
    unit, rest = m.group(2), (m.group(4) or "").strip("　")
    if m.group(3) and rest and not t[len(digits):].startswith("　"):
        return None          # "12月の…" - a number glued to a word, not a heading
    n = to_number(digits)
    if n is None:
        return None
    return n, ("part" if unit == "部" else "chapter"), rest


def classify_label(label):
    """TOC label -> 'chapter' | 'part' | 'afterword' | 'back' | 'front' | 'unknown'."""
    t = squash(label).replace("　", "")
    if not t:
        return "unknown"
    if _AFTERWORD_RE.search(t) and len(t) <= 20:
        return "afterword"
    if _BACK_RE.match(t):
        return "back"
    if _FRONT_RE.match(t):
        return "front"
    if _PART_RE.match(t):
        return "part"
    h = parse_heading(squash(label))
    if h:
        return h[1]
    if _SPECIAL_CHAPTER_RE.match(t):
        return "chapter"
    return "unknown"


# --- reading the epub ------------------------------------------------------

class Book:
    """The epub flattened into one list of paragraphs in reading order.

    paras[i] = {"file": spine index, "text": str ("" = spacer/section break),
                "heading": parse_heading() of it or None}
    anchors[(file_path, id)] = paragraph index the anchor points at.
    """

    def __init__(self, epub_path, keep_furigana=False, keep_scene_markers=False,
                 normalise=True):
        self.path = str(epub_path)
        self.zip = zipfile.ZipFile(epub_path)
        self.norm_counts = {}
        self._read_opf()
        self.paras, self.file_start, self.anchors = [], [], {}
        for fi, href in enumerate(self.spine):
            self.file_start.append(len(self.paras))
            self._read_file(fi, href, keep_furigana, keep_scene_markers, normalise)
        self.file_start.append(len(self.paras))
        self.toc = self._read_toc()

    # OPF: manifest, spine, metadata
    def _read_opf(self):
        container = self.zip.read("META-INF/container.xml").decode("utf-8")
        self.opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
        opf = BeautifulSoup(self.zip.read(self.opf_path), "lxml-xml")
        base = posixpath.dirname(self.opf_path)
        self.manifest = {}
        self.nav_path = self.ncx_path = None
        for item in opf.find_all("item"):
            path = posixpath.normpath(posixpath.join(base, unquote(item.get("href", ""))))
            self.manifest[item.get("id")] = path
            if "nav" in (item.get("properties") or "").split():
                self.nav_path = path
            if item.get("media-type") == "application/x-dtbncx+xml":
                self.ncx_path = path
        spine_tag = opf.find("spine")
        self.spine = [self.manifest[r["idref"]] for r in spine_tag.find_all("itemref")
                      if r.get("idref") in self.manifest]
        if not self.ncx_path and spine_tag.get("toc") in self.manifest:
            self.ncx_path = self.manifest[spine_tag["toc"]]
        title = opf.find("title")
        self.title = title.get_text(strip=True) if title else ""
        makers = [c.get_text(strip=True) for c in opf.find_all("contributor")]
        gen = opf.find("meta", attrs={"name": "generator"})
        self.made_by = (makers[0] if makers else "") or (gen.get("content") if gen else "")

    def _read_file(self, fi, href, keep_furigana, keep_scene_markers, normalise):
        soup = BeautifulSoup(self.zip.read(href), "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        resolve_ruby(soup, keep_furigana)
        tags = [t for t in soup.find_all(_PARAGRAPH_TAGS)
                if t.find_parent(_PARAGRAPH_TAGS) is None]
        index = {id(t): i for i, t in enumerate(tags)}
        base = len(self.paras)
        for t in tags:
            text = _collapse_pretty_print_whitespace(t.get_text(separator="")).strip()
            if normalise and text:
                text = normalise_vertical(text, self.norm_counts)
            heading = None
            is_link = t.find("a", href=True) is not None or t.find_parent("a", href=True) is not None
            if not is_link:
                probe = text
                if not probe:
                    img = t.find("img", alt=True)
                    probe = img["alt"].strip() if img else ""
                heading = parse_heading(probe)
            heading_text = squash(probe) if heading else ""
            if text and not keep_scene_markers and _is_scene_break_marker(text):
                text = ""
            self.paras.append({"file": fi, "text": text, "heading": heading,
                               "heading_text": heading_text})
        if not tags:
            # a picture-only file still gets a position of its own, or two
            # TOC entries on consecutive picture pages would collide
            # (yojo-senki-2: 付録 on part0036, あとがき on part0041)
            self.paras.append({"file": fi, "text": "", "heading": None, "heading_text": ""})
        # anchors: an id on or inside a paragraph -> that paragraph; any other
        # id -> the next paragraph after it (end of file if none).
        for el in soup.find_all(id=True):
            host = el if id(el) in index else next(
                (p for p in el.parents if id(p) in index), None)
            if host is None:
                nxt = el.find_next(_PARAGRAPH_TAGS)
                while nxt is not None and id(nxt) not in index:
                    nxt = nxt.find_parent(_PARAGRAPH_TAGS) or nxt.find_next(_PARAGRAPH_TAGS)
                pos = base + index[id(nxt)] if nxt is not None else base + len(tags)
            else:
                pos = base + index[id(host)]
            self.anchors.setdefault((href, el["id"]), pos)
        # a <div title="第壱章　ダキア戦役"> on a picture page (yojo-senki-2)
        self._titles = getattr(self, "_titles", {})
        div = soup.find(attrs={"title": True}, name=["div", "section", "body"])
        if div:
            self._titles[fi] = squash(div["title"])

    def _resolve(self, doc_path, href):
        href = unquote(href)
        path, _, frag = href.partition("#")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(doc_path), path)) if path else doc_path
        if target not in self.spine:
            return None
        fi = self.spine.index(target)
        if frag and (target, frag) in self.anchors:
            return self.anchors[(target, frag)]
        return self.file_start[fi]

    def _read_toc(self):
        """[(label, para_pos)] in document order of the TOC; unresolvable
        targets (files not in this epub) are dropped."""
        entries = []
        if self.nav_path and self.nav_path in self.zip.namelist():
            soup = BeautifulSoup(self.zip.read(self.nav_path), "lxml")
            nav = None
            for n in soup.find_all("nav"):
                if "toc" in (n.get("epub:type") or n.get("type") or ""):
                    nav = n
                    break
            for a in (nav or soup).find_all("a", href=True):
                entries.append((a.get_text(" ", strip=True), self._resolve(self.nav_path, a["href"])))
        if not entries and self.ncx_path and self.ncx_path in self.zip.namelist():
            soup = BeautifulSoup(self.zip.read(self.ncx_path), "lxml-xml")
            for np in soup.find_all("navPoint"):
                label = np.find("navLabel")
                content = np.find("content")
                if content and content.get("src"):
                    entries.append((label.get_text(" ", strip=True) if label else "",
                                    self._resolve(self.ncx_path, content["src"])))
        return [(squash(l), p) for l, p in entries if p is not None]

    def text_chars(self, start, end):
        return sum(len(p["text"]) for p in self.paras[start:end])

    def file_title(self, fi):
        return self._titles.get(fi, "")

    def sha256(self):
        h = hashlib.sha256()
        with open(self.path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()


# --- the two sources -------------------------------------------------------

def heading_chain(book):
    """Source A: the longest run of numbered headings counting from 1.
    Returns (chain, part_positions, skipped) with chain = [(pos, number)]."""
    cands = [i for i, p in enumerate(book.paras) if p["heading"]]
    parts, chapters = [], []
    for k, pos in enumerate(cands):
        nxt = cands[k + 1] if k + 1 < len(cands) else len(book.paras)
        follow = book.text_chars(pos + 1, nxt)
        n, kind, _ = book.paras[pos]["heading"]
        if kind == "part":
            parts.append(pos)
        elif follow >= MIN_FOLLOW_CHARS:
            chapters.append((pos, n))
    best = []
    for s, (pos0, n0) in enumerate(chapters):
        if n0 != 1:
            continue
        chain = [(pos0, 1)]
        for pos, n in chapters[s + 1:]:
            last_pos, last_n = chain[-1]
            restarted = n == 1 and any(last_pos < p < pos for p in parts)
            if n == last_n + 1 or restarted:
                chain.append((pos, n))
        if len(chain) > len(best):
            best = chain
    in_chain = {p for p, _ in best}
    skipped = [(p, n) for p, n in chapters if p not in in_chain]
    return best, parts, skipped


def toc_entries(book):
    """Source B: TOC entries classified, deduplicated by position, sorted."""
    rank = {"chapter": 0, "afterword": 1, "part": 2, "back": 3, "front": 4, "unknown": 5}
    seen = {}
    for label, pos in book.toc:
        kind = classify_label(label)
        # two entries on one spot (1q84: 装幀 and 第１章): the chapter wins
        if pos not in seen or rank[kind] < rank[seen[pos]["kind"]]:
            seen[pos] = {"pos": pos, "label": label, "kind": kind}
    return sorted(seen.values(), key=lambda e: e["pos"])


# --- the plan --------------------------------------------------------------

def _loc(book, pos):
    if pos >= len(book.paras):
        return {"file": book.spine[-1], "para": book.file_start[-1] - book.file_start[-2]}
    fi = book.paras[pos]["file"]
    return {"file": book.spine[fi], "para": pos - book.file_start[fi]}


def _pos(book, loc):
    fi = book.spine.index(loc["file"])
    return book.file_start[fi] + loc["para"]


def build_plan(epub_path, keep_furigana=False, keep_scene_markers=False, normalise=True):
    book = Book(epub_path, keep_furigana, keep_scene_markers, normalise)
    chain, part_heads, skipped = heading_chain(book)
    toc = toc_entries(book)
    toc_chapters = [e for e in toc if e["kind"] == "chapter"]
    warnings, questions = [], []

    # --- choose the chapter starts
    a_ok, b_ok = len(chain) >= 2, len(toc_chapters) >= 2
    if a_ok and b_ok:
        a_files = [book.paras[p]["file"] for p, _ in chain]
        b_files = [book.paras[e["pos"]]["file"] if e["pos"] < len(book.paras) else -1
                   for e in toc_chapters]
        if a_files == b_files:
            method, agreement = "headings+toc", "agree"
            starts = [p for p, _ in chain]
        else:
            method, agreement = "toc", "disagree"
            starts = [e["pos"] for e in toc_chapters]
            warnings.append(f"headings found {len(chain)} chapters, the TOC lists "
                            f"{len(toc_chapters)} - using the TOC; check the table")
    elif b_ok:
        method, agreement = "toc", "single"
        starts = [e["pos"] for e in toc_chapters]
    elif a_ok:
        method, agreement = "headings", "single"
        starts = [p for p, _ in chain]
    else:
        method, agreement = "files", "none"
        starts = [book.file_start[fi] for fi in range(len(book.spine))
                  if book.text_chars(book.file_start[fi], book.file_start[fi + 1]) >= MIN_FOLLOW_CHARS]
        warnings.append("no chapter headings and no usable TOC - one chapter per "
                        "long file; check every row")
    for pos, n in skipped:
        warnings.append(f"heading {book.paras[pos]['heading_text']!r} is out of "
                        f"sequence and was not used")

    # --- every boundary: chapters, plus all other TOC entries and part headings
    bounds = {p: {"kind": "chapter", "label": ""} for p in starts}
    for e in toc:
        if e["pos"] in bounds:
            if not bounds[e["pos"]]["label"]:
                bounds[e["pos"]]["label"] = e["label"]
        elif e["kind"] != "chapter" or method == "headings":
            bounds[e["pos"]] = {"kind": e["kind"], "label": e["label"]}
    for p in part_heads:
        bounds.setdefault(p, {"kind": "part", "label": book.paras[p]["heading_text"]})
    # a TOC entry that sits inside a chapter's heading file, just before its
    # heading, is the same start - fold it in (1q84: file anchor vs <p>).
    for p in list(starts):
        fi = book.paras[p]["file"]
        for q in list(bounds):
            if q < p and q >= book.file_start[fi] and bounds[q]["kind"] in ("chapter", "unknown") \
                    and q not in starts:
                if not bounds[p]["label"] and bounds[q]["kind"] == "chapter":
                    bounds[p]["label"] = bounds[q]["label"]
                del bounds[q]
    order = sorted(bounds)
    if not order or order[0] > 0:
        order.insert(0, 0)
        bounds.setdefault(0, {"kind": "front", "label": ""})

    # --- units
    first_chapter = min(starts) if starts else len(book.paras)
    last_chapter = max(starts) if starts else -1
    units = []
    for k, pos in enumerate(order):
        end = order[k + 1] if k + 1 < len(order) else len(book.paras)
        b = bounds[pos]
        kind = b["kind"]
        if kind == "unknown":
            chars = book.text_chars(pos, end)
            if chars >= UNKNOWN_CHAPTER_CHARS:
                # kafka: an unnumbered prologue before 第１章
                kind = "chapter"
                warnings.append(f"TOC entry {b['label']!r} is not a recognised chapter "
                                f"label but holds {chars} characters - counted as a chapter")
            else:
                kind = "front" if pos < first_chapter else "back"
        elif kind == "part" and pos > last_chapter:
            kind = "back"
        elif kind in ("front", "back") and pos < first_chapter:
            kind = "front"
        units.append({"kind": kind, "label": b["label"], "start": pos, "end": end})

    # the last chapter with nothing after it in the TOC: stop at the first
    # short file after its heading file (colophons, credits)
    chap_idx = [i for i, u in enumerate(units) if u["kind"] == "chapter"]
    if chap_idx:
        ci = chap_idx[-1]
        u = units[ci]
        fi0 = book.paras[u["start"]]["file"]
        for fi in range(fi0 + 1, len(book.spine)):
            s, e = book.file_start[fi], book.file_start[fi + 1]
            if s >= u["end"]:
                break
            if book.text_chars(s, e) < TAIL_FILE_CHARS:
                units.insert(ci + 1, {"kind": "back", "label": "", "start": s, "end": u["end"]})
                u["end"] = s
                break

    # --- titles, parts, include, questions
    part = ""
    out, chapter_no = [], 0
    for n, u in enumerate(units, 1):
        start, end = u["start"], u["end"]
        chars = book.text_chars(start, end)
        if u["kind"] == "part":
            part = u["label"] or next((p["text"] for p in book.paras[start:end] if p["text"]), "")
        head = book.paras[start]["heading_text"] if start < len(book.paras) else ""
        fi = book.paras[start]["file"] if start < len(book.paras) else len(book.spine) - 1
        title = u["label"] or head or book.file_title(fi)
        preview = next((p["text"] for p in book.paras[start:end]
                        if p["text"] and not p["heading"]), "")[:40]
        row = {"n": n, "kind": u["kind"], "include": u["kind"] == "chapter",
               "title": title, "part": part if u["kind"] == "chapter" else "",
               "start": _loc(book, start), "end": _loc(book, end),
               "files": sorted({book.spine[p["file"]] for p in book.paras[start:end]}),
               "chars": chars, "preview": preview}
        if u["kind"] == "chapter":
            chapter_no += 1
            row["chapter"] = chapter_no
        if u["kind"] == "afterword":
            row["include"] = None
            questions.append({"unit": n, "ask": f"Include the afterword {title!r} as a chapter?"})
        if chars == 0 and u["kind"] != "chapter":
            row["include"] = False
        out.append(row)

    empty = [r for r in out if r["kind"] == "chapter" and r["chars"] < MIN_FOLLOW_CHARS]
    for r in empty:
        warnings.append(f"chapter {r['title']!r} holds only {r['chars']} characters")

    return {
        "version": PLAN_VERSION,
        "epub": str(Path(epub_path).resolve()),
        "epub_sha256": book.sha256(),
        "book_title": book.title,
        "made_by": book.made_by,
        "created": datetime.now().isoformat(timespec="seconds"),
        "options": {"keep_furigana": keep_furigana, "keep_scene_markers": keep_scene_markers,
                    "normalise_vertical": normalise},
        "method": method,
        "agreement": agreement,
        "sources": {"headings": len(chain), "toc_chapters": len(toc_chapters),
                    "toc_entries": len(book.toc)},
        "chapters": sum(1 for r in out if r["kind"] == "chapter"),
        "normalised": book.norm_counts,
        "needs_review": agreement in ("disagree", "none") or bool(empty),
        "warnings": warnings,
        "questions": questions,
        "units": out,
    }


def answer_afterwords(plan, include):
    """Answer every open afterword question with the same yes/no."""
    for u in plan["units"]:
        if u["kind"] == "afterword" and u["include"] is None:
            u["include"] = bool(include)
    plan["questions"] = []
    return plan


def open_questions(plan):
    return [u for u in plan["units"] if u["include"] is None]


def save_plan(plan, out_dir):
    path = Path(out_dir) / PLAN_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def load_plan(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def unit_text(book, unit):
    """A unit's text; a file boundary inside a unit becomes a section break."""
    start, end = _pos(book, unit["start"]), _pos(book, unit["end"])
    paras, last_file = [], None
    for p in book.paras[start:end]:
        if last_file is not None and p["file"] != last_file:
            paras.append("")
        paras.append(p["text"])
        last_file = p["file"]
    return paras_to_text(paras)


def paras_to_text(paras):
    return paragraphs_to_text(paras)


def write_plan(plan, out_dir, log=print):
    """Write chapter_NNN.txt for every included unit, in order, and the
    rest to non-chapters-files/. Refuses a plan with an open question or
    one made from a different epub file."""
    if open_questions(plan):
        raise ValueError("the plan still has open questions: " +
                         "; ".join(q["ask"] for q in plan["questions"]))
    opts = plan["options"]
    book = Book(plan["epub"], opts["keep_furigana"], opts["keep_scene_markers"],
                opts["normalise_vertical"])
    if book.sha256() != plan["epub_sha256"]:
        raise ValueError("the epub has changed since this plan was made - detect again")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written, chapter_no = [], 0
    for u in plan["units"]:
        text = unit_text(book, u)
        if u["include"]:
            chapter_no += 1
            path = out / f"chapter_{chapter_no:03d}.txt"
        elif text.strip():
            path = out / NON_CHAPTER_SUBDIR / f"{u['n']:03d}_{sanitize_filename(u['title'] or u['kind'])}.txt"
        else:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(str(path.relative_to(out)))
        log(f"  {path.relative_to(out)}  ({len(text)} chars)  {u['title']}")
    plan["written"] = {"at": datetime.now().isoformat(timespec="seconds"),
                       "chapters": chapter_no, "files": written}
    save_plan(plan, out)
    return plan


def plan_table(plan):
    """Plain-text table of a plan, for the CLI and the GUI log."""
    lines = [f"{plan['book_title']}  |  method: {plan['method']} ({plan['agreement']})  |  "
             f"chapters: {plan['chapters']}  |  headings {plan['sources']['headings']}, "
             f"TOC chapters {plan['sources']['toc_chapters']}"]
    if plan["normalised"]:
        lines.append("normalised: " + ", ".join(f"{k} x{v}" for k, v in plan["normalised"].items()))
    for u in plan["units"]:
        inc = {True: "yes", False: " - ", None: " ? "}[u["include"]]
        ch = f"ch{u['chapter']:>3}" if "chapter" in u else "     "
        lines.append(f"{u['n']:>3} {inc} {ch} {u['kind']:<9} {u['chars']:>7}  "
                     f"{(u['title'] or '')[:30]:<30} {u['preview'][:20]}")
    for w in plan["warnings"]:
        lines.append("WARNING: " + w)
    for q in plan["questions"]:
        lines.append("QUESTION: " + q["ask"])
    return "\n".join(lines)
