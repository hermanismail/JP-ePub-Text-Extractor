"""
epub_extractor_gui.py
----------------------
Simple GUI for epub_extractor.py: pick an EPUB file, pick an output
folder, click Extract. Visual style follows the same design tokens/layout
patterns used in C:\\JP-Audiobook-Generator\\gui_settings.py and
progress_window.py (light card-based theme, customtkinter), reimplemented
locally here rather than importing from that project so this tool has no
dependency on an unrelated app's internals.

Two windows:
  - ExtractorApp: the main window - EPUB file / output folder pickers,
    a couple of extraction options, and an "Extract" button.
  - ProgressWindow: opened on Extract, shows a live process log and
    running/completed/failed state (mirrors JP-Audiobook-Generator's
    progress window, trimmed down - no per-chunk TTS concepts here, and
    "chapters" becomes "items" since that's what's being walked while the
    epub is parsed).

Extraction runs in a background thread (it's in-process, not a
subprocess - epub_extractor.extract_epub() is called directly) and
reports back through a queue.Queue drained on the Tk main thread via
self.after(), the same pattern gui_settings.py uses for its subprocess
log-draining loop.

Requires: customtkinter (pip install customtkinter), plus everything
epub_extractor.py itself requires (see requirements.txt).

Run:
    python epub_extractor_gui.py
"""

import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from epub_extractor import NON_CHAPTER_SUBDIR, ExtractResult, extract_epub

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(SCRIPT_DIR, "epub_extractor_icon.ico")

# ---------------------------------------------------------------------------
# Design tokens - same values as ui_common.py / progress_window.py in
# JP-Audiobook-Generator, copied locally so this stays a standalone tool.
# ---------------------------------------------------------------------------
COLOR_BG = "#F7F7FA"
COLOR_CARD = "#FFFFFF"
COLOR_CARD_BORDER = "#E7E7EC"
COLOR_TITLE = "#17171C"
COLOR_SUBTITLE = "#8B8B94"
COLOR_ENTRY_BORDER = "#E2E2E8"
COLOR_ENTRY_TEXT = "#3A3A42"
COLOR_ACCENT = "#6C5DD3"
COLOR_ACCENT_HOVER = "#5B4FC0"
COLOR_TOGGLE_ON = "#34C773"
COLOR_BTN_NEUTRAL_BORDER = "#D8D8DE"
COLOR_BTN_NEUTRAL_TEXT = "#3A3A42"

COLOR_LOG_BG = "#1C1C22"
COLOR_LOG_BORDER = "#2A2A32"
COLOR_LOG_TIMESTAMP = "#6FD3E6"
COLOR_LOG_PROCESSING = "#B9A3F7"
COLOR_LOG_TEXT = "#C7C7D1"
COLOR_LOG_SUCCESS = "#5FD98A"
COLOR_LOG_ERROR = "#F1706E"

SPINNER_FRAMES = ["\u25D0", "\u25D3", "\u25D1", "\u25D2"]  # ◐ ◓ ◑ ◒

BAR_COLORS = {
    "running": ("#4DA6FF", "#E6F1FB"),
    "completed": ("#2FB668", "#E6F8ED"),
    "failed": ("#D85A5A", "#FCEAEA"),
    "cancelled": ("#7A7A85", "#F0F0F3"),
}

STATE_STYLES = {
    "running": dict(
        icon_bg="#EDEBFC", icon_color=COLOR_ACCENT, spin=True, glyph=None,
        title="Extracting...", subtitle="Reading chapters from the EPUB file.",
        pill_bg="#FFF3DD", pill_text="#C98A2E", pill_label="In Progress"),
    "completed": dict(
        icon_bg="#E6F8ED", icon_color="#2FB668", spin=False, glyph="\u2714",
        title="Completed!", subtitle="All chapters have been extracted successfully.",
        pill_bg="#E6F8ED", pill_text="#2FB668", pill_label="Completed"),
    "failed": dict(
        icon_bg="#FCEAEA", icon_color="#D85A5A", spin=False, glyph="\u2715",
        title="Extraction Failed", subtitle="Something went wrong - check the log below.",
        pill_bg="#FCEAEA", pill_text="#D85A5A", pill_label="Failed"),
    "cancelled": dict(
        icon_bg="#F0F0F3", icon_color="#7A7A85", spin=False, glyph="\u25A0",
        title="Cancelled", subtitle="Extraction was stopped before finishing.",
        pill_bg="#F0F0F3", pill_text="#7A7A85", pill_label="Cancelled"),
}


class IconBadge(ctk.CTkFrame):
    """Rounded colored square with a centered glyph/emoji or short text."""

    def __init__(self, parent, glyph, bg_color, text_color="white",
                 size=48, font_size=20, corner_radius=12, **kwargs):
        super().__init__(parent, width=size, height=size, corner_radius=corner_radius,
                          fg_color=bg_color, **kwargs)
        self.pack_propagate(False)
        ctk.CTkLabel(self, text=glyph, text_color=text_color,
                     font=ctk.CTkFont(size=font_size, weight="bold")).place(
            relx=0.5, rely=0.5, anchor="center")


def _open_in_explorer(path):
    """Best-effort "open this folder" - os.startfile is Windows-only,
    which is fine since this whole project only targets Windows 11."""
    try:
        os.startfile(path)  # noqa: F821 - Windows-only, intentional
    except Exception as e:
        messagebox.showerror("Could not open folder", f"{path}\n\n{e}")


# ---------------------------------------------------------------------------
# Progress / completion window
# ---------------------------------------------------------------------------
class ProgressWindow(ctk.CTkToplevel):
    """Shown while extract_epub() runs on a background thread. Non-modal,
    same as JP-Audiobook-Generator's ProgressWindow."""

    def __init__(self, master, on_cancel=None, on_open_output=None, **kwargs):
        super().__init__(master, fg_color=COLOR_BG, **kwargs)
        self.title("ePub Text Extractor - Progress")
        self.resizable(True, True)
        self.minsize(560, 560)
        self.geometry("680x700")
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass  # non-fatal cosmetic failure

        self._on_cancel = on_cancel
        self._on_open_output = on_open_output
        self._spinner_label = None
        self._spinner_index = 0
        self._spinner_job = None
        self._timer_job = None
        self._start_time = None
        self._state = None
        self._log_line_count = 0

        self.protocol("WM_DELETE_WINDOW", self._on_close_button)

        self._build_ui()
        self.set_stats(total_items=0, chapters=0, non_chapters=0)
        self.set_state("running")
        self._start_timer()

    # ---------- UI construction ----------
    def _build_ui(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=20)
        outer.grid_columnconfigure(0, weight=1)

        for grid_row, (weight, minsize) in enumerate(
                [(2, 57), (3, 86), (5, 143), (7, 201), (1, 29)]):
            outer.grid_rowconfigure(grid_row, weight=weight, minsize=minsize)

        self._build_header(outer, 0)
        self._build_progress_card(outer, 1)
        self._build_stats_row(outer, 2)
        self._build_log_card(outer, 3)
        self._build_footer(outer, 4)

    def _build_header(self, parent, grid_row):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))

        content = ctk.CTkFrame(cell, fg_color="transparent")
        content.pack(fill="x", expand=True)

        self.state_icon = IconBadge(
            content, "\u25D0", "#EDEBFC", text_color=COLOR_ACCENT,
            size=48, font_size=20, corner_radius=12)
        self.state_icon.pack(side="left", padx=(0, 14))

        text_frame = ctk.CTkFrame(content, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True)
        self.title_label = ctk.CTkLabel(
            text_frame, text="", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"), anchor="w")
        self.title_label.pack(fill="x", anchor="w")
        self.subtitle_label = ctk.CTkLabel(
            text_frame, text="", text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=14), anchor="w", justify="left")
        self.subtitle_label.pack(fill="x", anchor="w")

        self.status_pill = ctk.CTkLabel(
            content, text="", corner_radius=14, fg_color="#FFF3DD", text_color="#C98A2E",
            font=ctk.CTkFont(size=12, weight="bold"), width=104, height=30)
        self.status_pill.pack(side="right", anchor="n")

    def _build_progress_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)

        top_row = ctk.CTkFrame(inner, fg_color="transparent")
        top_row.pack(fill="x")
        self.item_label = ctk.CTkLabel(
            top_row, text="Item 0 of 0", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=17, weight="bold"))
        self.item_label.pack(side="left")
        self.current_item_label = ctk.CTkLabel(
            top_row, text="", text_color=COLOR_ACCENT, font=ctk.CTkFont(size=14))
        self.current_item_label.pack(side="left", padx=(14, 0))
        self.percent_label = ctk.CTkLabel(
            top_row, text="0%", text_color=COLOR_TITLE,
            font=ctk.CTkFont(size=17, weight="bold"))
        self.percent_label.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(
            inner, height=12, corner_radius=6, progress_color=BAR_COLORS["running"][0],
            fg_color=BAR_COLORS["running"][1])
        self.progress_bar.set(0)
        self.progress_bar.pack(fill="x", pady=(10, 0))

    def _build_stats_row(self, parent, grid_row):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))
        row.grid_rowconfigure(0, weight=1)
        for i in range(4):
            row.grid_columnconfigure(i, weight=1, uniform="stat")

        self.stat_chips = {}
        chip_defs = [
            ("total_items", "\U0001F4D6", "#8272F4", "Items Scanned"),
            ("chapters", "\u2728", "#4FCB8F", "Chapters Found"),
            ("non_chapters", "\U0001F5C2", "#4DA6FF", "Non-Chapters"),
        ]
        for i, (key, glyph, bg, caption) in enumerate(chip_defs):
            chip = self._make_chip(row, i)
            inner = ctk.CTkFrame(chip, fg_color="transparent")
            inner.pack(expand=True)

            icon_row = ctk.CTkFrame(inner, fg_color="transparent")
            icon_row.pack()
            icon = IconBadge(icon_row, glyph, bg, size=48, font_size=20, corner_radius=13)
            icon.pack(side="left")
            if key == "total_items":
                icon.winfo_children()[0].place_configure(rely=0.46)
            value_label = ctk.CTkLabel(icon_row, text="0", text_color=COLOR_TITLE,
                                        font=ctk.CTkFont(size=26, weight="bold"))
            value_label.pack(side="left", padx=(9, 0))

            caption_label = ctk.CTkLabel(
                inner, text=caption, text_color=COLOR_SUBTITLE,
                font=ctk.CTkFont(size=14), wraplength=120, justify="center")
            caption_label.pack(pady=(8, 0))

            self.stat_chips[key] = {"value": value_label, "caption": caption_label, "icon": icon}

        time_chip = self._make_chip(row, 3)
        time_inner = ctk.CTkFrame(time_chip, fg_color="transparent")
        time_inner.pack(expand=True)
        time_value = ctk.CTkLabel(time_inner, text="00:00:00", text_color=COLOR_TITLE,
                                   font=ctk.CTkFont(size=28, weight="bold"))
        time_value.pack()
        time_caption = ctk.CTkLabel(
            time_inner, text="Elapsed Time", text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=14), wraplength=120, justify="center")
        time_caption.pack(pady=(8, 0))
        self.stat_chips["time"] = {"value": time_value, "caption": time_caption, "icon": None}

    @staticmethod
    def _make_chip(parent, column):
        chip = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=14,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        chip.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        return chip

    def _build_log_card(self, parent, grid_row):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        card.grid(row=grid_row, column=0, sticky="nsew", pady=(0, 16))
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=14)

        header_row = ctk.CTkFrame(inner, fg_color="transparent")
        header_row.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(header_row, text="\U0001F4C4  Process Log", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(
            header_row, text="\U0001F5D1  Clear Log", width=100, height=28, corner_radius=7,
            fg_color="transparent", hover_color="#F0F0F3", border_width=1,
            border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
            font=ctk.CTkFont(size=11), command=self.clear_log,
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            inner, fg_color=COLOR_LOG_BG, corner_radius=10, border_width=1,
            border_color=COLOR_LOG_BORDER, wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11))
        self.log_box.pack(fill="both", expand=True)

        text = self.log_box._textbox
        text.tag_config("timestamp", foreground=COLOR_LOG_TIMESTAMP)
        text.tag_config("text", foreground=COLOR_LOG_TEXT)
        text.tag_config("processing", foreground=COLOR_LOG_PROCESSING)
        text.tag_config("success", foreground=COLOR_LOG_SUCCESS)
        text.tag_config("error", foreground=COLOR_LOG_ERROR)
        self.log_box.configure(state="disabled")

    def _build_footer(self, parent, grid_row):
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        cell.grid(row=grid_row, column=0, sticky="nsew")
        self.footer = ctk.CTkFrame(cell, fg_color="transparent")
        self.footer.pack(fill="x", expand=True)

    def _render_footer(self, state):
        for w in self.footer.winfo_children():
            w.destroy()

        if state == "running":
            ctk.CTkButton(
                self.footer, text="\u2715  Cancel", width=110, height=38, corner_radius=8,
                fg_color="transparent", hover_color="#FCEAEA", border_width=1,
                border_color="#D85A5A", text_color="#D85A5A",
                command=self._handle_cancel,
            ).pack(side="right")
        elif state == "completed":
            ctk.CTkButton(
                self.footer, text="Close", width=100, height=38, corner_radius=8,
                fg_color="#D85A5A", hover_color="#C24A4A", text_color="white",
                command=self.destroy,
            ).pack(side="right")
            ctk.CTkButton(
                self.footer, text="\U0001F4C1  Open Output Folder", width=190, height=38,
                corner_radius=8, fg_color="white", hover_color="#F5F5F8", border_width=1,
                border_color=COLOR_ENTRY_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
                command=self._handle_open_output,
            ).pack(side="right", padx=(0, 10))
        else:  # failed / cancelled
            ctk.CTkButton(
                self.footer, text="Close", width=100, height=38, corner_radius=8,
                fg_color="transparent", hover_color="#F0F0F3", border_width=1,
                border_color=COLOR_BTN_NEUTRAL_BORDER, text_color=COLOR_BTN_NEUTRAL_TEXT,
                command=self.destroy,
            ).pack(side="right")

    # ---------- Footer / window-close handlers ----------
    def _handle_cancel(self):
        self.append_log("Cancel requested by user...", tag="error")
        if self._on_cancel:
            self._on_cancel()

    def _handle_open_output(self):
        if self._on_open_output:
            self._on_open_output()

    def _on_close_button(self):
        if self._state == "running":
            self._handle_cancel()
        else:
            self.destroy()

    # ---------- Public API ----------
    def set_state(self, state, title=None, subtitle=None):
        style = STATE_STYLES[state]
        self._state = state

        self.title_label.configure(text=title or style["title"])
        self.subtitle_label.configure(text=subtitle or style["subtitle"])
        self.status_pill.configure(
            text=style["pill_label"], fg_color=style["pill_bg"], text_color=style["pill_text"])

        self.state_icon.configure(fg_color=style["icon_bg"])
        glyph_label = self.state_icon.winfo_children()[0]
        glyph_label.configure(text_color=style["icon_color"])

        if style["spin"]:
            self._start_spinner(glyph_label)
        else:
            self._stop_spinner()
            glyph_label.configure(text=style["glyph"])

        self.stat_chips["time"]["caption"].configure(
            text="Elapsed Time" if state == "running" else "Total Time")

        bar_color, bar_track = BAR_COLORS[state]
        self.progress_bar.configure(progress_color=bar_color, fg_color=bar_track)

        if state in ("completed", "failed", "cancelled"):
            self._stop_timer(freeze=True)

        self._render_footer(state)

    def append_log(self, message, tag="text"):
        timestamp = time.strftime("[%H:%M:%S] ")
        text = self.log_box._textbox
        text.configure(state="normal")
        text.insert("end", timestamp, ("timestamp",))
        text.insert("end", message + "\n", (tag,))
        self._log_line_count += 1
        text.see("end")
        text.configure(state="disabled")

    def clear_log(self):
        text = self.log_box._textbox
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.configure(state="disabled")
        self._log_line_count = 0

    def set_item_progress(self, pos, total, current_item, percent):
        self.item_label.configure(text=f"Item {pos} of {total}")
        self.current_item_label.configure(text=current_item)
        self.percent_label.configure(text=f"{int(percent)}%")
        self.progress_bar.set(max(0.0, min(1.0, percent / 100)))

    def set_stats(self, total_items=None, chapters=None, non_chapters=None):
        if total_items is not None:
            self.stat_chips["total_items"]["value"].configure(text=str(total_items))
        if chapters is not None:
            self.stat_chips["chapters"]["value"].configure(text=str(chapters))
        if non_chapters is not None:
            self.stat_chips["non_chapters"]["value"].configure(text=str(non_chapters))

    # ---------- Internal: spinner + elapsed timer ----------
    def _start_spinner(self, glyph_label):
        self._spinner_label = glyph_label
        self._spin_tick()

    def _spin_tick(self):
        self._spinner_label.configure(text=SPINNER_FRAMES[self._spinner_index % len(SPINNER_FRAMES)])
        self._spinner_index += 1
        self._spinner_job = self.after(180, self._spin_tick)

    def _stop_spinner(self):
        if self._spinner_job is not None:
            self.after_cancel(self._spinner_job)
            self._spinner_job = None

    def _start_timer(self):
        self._start_time = time.time()
        self._tick_timer()

    def _tick_timer(self):
        elapsed = time.time() - self._start_time
        self.stat_chips["time"]["value"].configure(text=self._format_duration(elapsed))
        self._timer_job = self.after(1000, self._tick_timer)

    def _stop_timer(self, freeze=True):
        if self._timer_job is not None:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        if freeze and self._start_time is not None:
            self.stat_chips["time"]["value"].configure(
                text=self._format_duration(time.time() - self._start_time))

    @staticmethod
    def _format_duration(seconds):
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class ExtractorApp(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=COLOR_BG)
        ctk.set_appearance_mode("light")
        # Same fix as gui_settings.py/progress_window.py: pin scaling to 1:1
        # so Windows display scaling above 100% doesn't disagree with the
        # literal-pixel geometry() call below.
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)

        self.title("ePub Text Extractor")
        self.resizable(True, True)
        self.minsize(700, 560)
        self.geometry("760x620")
        if os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
            except Exception:
                pass

        self.epub_path_var = ctk.StringVar(value="")
        self.output_dir_var = ctk.StringVar(value="")
        self.keep_furigana_var = ctk.IntVar(value=0)
        self.keep_scene_markers_var = ctk.IntVar(value=0)
        self.detect_chapters_var = ctk.IntVar(value=1)

        self._run_window = None
        self._run_queue = None
        self._cancel_event = None
        self._run_thread = None
        self._run_output_dir = None

        self._build_ui()

    # ---------- UI construction ----------
    def _build_ui(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=20, pady=20)

        self._build_header(outer)
        self._build_paths_card(outer)
        self._build_options_card(outer)
        self._build_footer(outer)

    def _build_header(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", pady=(0, 16))

        IconBadge(header, "\U0001F4D6", "#EDEBFC", text_color=COLOR_ACCENT,
                  size=48, font_size=20, corner_radius=12).pack(side="left", padx=(0, 14))

        text_frame = ctk.CTkFrame(header, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(text_frame, text="ePub Text Extractor", text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=22, weight="bold"), anchor="w").pack(fill="x", anchor="w")
        ctk.CTkLabel(text_frame, text="Extract Japanese chapter text from an EPUB file",
                     text_color=COLOR_SUBTITLE, font=ctk.CTkFont(size=14),
                     anchor="w", justify="left").pack(fill="x", anchor="w")

    def _row_shell(self, parent):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=12)
        return row

    # Shared left-column width for every row's title/subtitle block, across
    # both the paths card and the options card, so the entry boxes and the
    # toggle switches all start at exactly the same x position instead of
    # the switches floating out at the row's far right edge.
    LABEL_COL_WIDTH = 230

    def _title_block(self, row, title, subtitle, fixed_width=LABEL_COL_WIDTH, fixed_height=44):
        text_frame = ctk.CTkFrame(row, fg_color="transparent",
                                   width=fixed_width, height=fixed_height)
        text_frame.pack_propagate(False)
        text_frame.pack(side="left", padx=(0, 14))
        wrap_len = max(fixed_width - 6, 40)
        ctk.CTkLabel(text_frame, text=title, text_color=COLOR_TITLE,
                     font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
                     justify="left", wraplength=wrap_len).pack(anchor="w", fill="x")
        ctk.CTkLabel(text_frame, text=subtitle, text_color=COLOR_SUBTITLE,
                     font=ctk.CTkFont(size=11), anchor="w", justify="left",
                     wraplength=wrap_len).pack(anchor="w", fill="x")

    def _build_paths_card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        card.pack(fill="x", pady=(0, 16))

        # EPUB file row
        row = self._row_shell(card)
        IconBadge(row, "\U0001F4C4", "#8272F4").pack(side="left", padx=(0, 14))
        self._title_block(row, "EPUB File", "The Japanese .epub file to extract from")
        ctk.CTkButton(
            row, text="\U0001F4C1  Browse", width=110, height=36, corner_radius=8,
            fg_color="white", hover_color="#F5F5F8", border_width=1,
            border_color=COLOR_ENTRY_BORDER, text_color=COLOR_ENTRY_TEXT,
            command=self._browse_epub,
        ).pack(side="right")
        ctk.CTkEntry(
            row, textvariable=self.epub_path_var, height=36, corner_radius=8,
            border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white",
            placeholder_text="No file selected",
        ).pack(side="left", fill="x", expand=True, padx=(0, 14))

        # Output folder row
        row = self._row_shell(card)
        IconBadge(row, "\U0001F4C1", "#4FCB8F").pack(side="left", padx=(0, 14))
        self._title_block(row, "Output Folder", "Where extracted .txt files are saved")
        ctk.CTkButton(
            row, text="\U0001F4C1  Browse", width=110, height=36, corner_radius=8,
            fg_color="white", hover_color="#F5F5F8", border_width=1,
            border_color=COLOR_ENTRY_BORDER, text_color=COLOR_ENTRY_TEXT,
            command=self._browse_output,
        ).pack(side="right")
        ctk.CTkEntry(
            row, textvariable=self.output_dir_var, height=36, corner_radius=8,
            border_width=1, border_color=COLOR_ENTRY_BORDER,
            text_color=COLOR_ENTRY_TEXT, fg_color="white",
            placeholder_text="No folder selected",
        ).pack(side="left", fill="x", expand=True, padx=(0, 14))

    def _add_switch_row(self, parent, glyph, bg_color, title, subtitle, int_var,
                         on_text, off_text):
        row = self._row_shell(parent)
        IconBadge(row, glyph, bg_color).pack(side="left", padx=(0, 14))
        # Taller than the path rows' default (56 vs. 44) since these
        # subtitles wrap to two lines - same fixed_height gui_settings.py
        # itself uses for its own two-line-subtitle rows. Width matches
        # LABEL_COL_WIDTH exactly (see _title_block) so the switch below
        # lines up with the entry boxes in the paths card above.
        self._title_block(row, title, subtitle, fixed_width=self.LABEL_COL_WIDTH, fixed_height=56)

        switch = ctk.CTkSwitch(
            row, text=on_text if int_var.get() else off_text,
            variable=int_var, onvalue=1, offvalue=0,
            progress_color=COLOR_TOGGLE_ON, button_color="white",
            switch_width=46, switch_height=24, text_color=COLOR_SUBTITLE,
            font=ctk.CTkFont(size=12))
        switch.configure(command=lambda: switch.configure(
            text=on_text if int_var.get() else off_text))
        # side="left" (not "right"): packs immediately after the title
        # block, so its left edge lines up with the title block's right
        # edge - the same x position the entry boxes above start at -
        # instead of floating out at the row's far right edge.
        switch.pack(side="left")

    def _build_options_card(self, parent):
        card = ctk.CTkFrame(parent, fg_color=COLOR_CARD, corner_radius=16,
                             border_width=1, border_color=COLOR_CARD_BORDER)
        card.pack(fill="x", pady=(0, 16))

        self._add_switch_row(
            card, "\U0001F522", "#F4A24F",
            "Auto-detect chapters",
            f"Numbers chapters chapter_001.txt; rest goes to {NON_CHAPTER_SUBDIR}",
            self.detect_chapters_var, "ON", "OFF (flat)")

        self._add_switch_row(
            card, "\u3042", "#4DA6FF",
            "Keep furigana",
            "Shows readings inline as 漢字(かんじ) instead of dropping them",
            self.keep_furigana_var, "ON (kept inline)", "OFF (discarded)")

        self._add_switch_row(
            card, "\u2726", "#B080E0",
            "Keep scene-divider glyphs",
            "Keeps glyphs like ＊ as text instead of a section break",
            self.keep_scene_markers_var, "ON (kept as text)", "OFF (section break)")

    def _build_footer(self, parent):
        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.pack(fill="x", pady=(4, 0))
        ctk.CTkButton(
            footer, text="\u2728  Extract", width=160, height=42, corner_radius=8,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color="white",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.on_extract,
        ).pack(side="right")

    # ---------- Browse handlers ----------
    def _browse_epub(self):
        current = self.epub_path_var.get()
        initialdir = os.path.dirname(current) if os.path.isfile(current) else SCRIPT_DIR
        chosen = filedialog.askopenfilename(
            title="Select EPUB File", initialdir=initialdir,
            filetypes=[("EPUB files", "*.epub"), ("All files", "*.*")])
        if chosen:
            self.epub_path_var.set(os.path.normpath(chosen))
            # Convenience default: if no output folder chosen yet, suggest
            # an "output" subfolder next to the epub.
            if not self.output_dir_var.get():
                self.output_dir_var.set(
                    os.path.normpath(os.path.join(os.path.dirname(chosen), "output")))

    def _browse_output(self):
        current = self.output_dir_var.get()
        initialdir = current if os.path.isdir(current) else SCRIPT_DIR
        chosen = filedialog.askdirectory(title="Select Output Folder", initialdir=initialdir)
        if chosen:
            self.output_dir_var.set(os.path.normpath(chosen))

    # ---------- Extract flow ----------
    def on_extract(self):
        if self._run_window is not None and self._run_window.winfo_exists():
            messagebox.showinfo(
                "Already Running",
                "An extraction is already in progress. Close its progress "
                "window (or wait for it to finish) before starting another.")
            return

        epub_path = self.epub_path_var.get().strip()
        output_dir = self.output_dir_var.get().strip()

        if not epub_path or not os.path.isfile(epub_path):
            messagebox.showerror("EPUB File Not Found", "Choose a valid .epub file first.")
            return
        if not output_dir:
            messagebox.showerror("No Output Folder", "Choose an output folder first.")
            return
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Cannot Create Output Folder", f"{output_dir}\n\n{e}")
            return

        keep_furigana = bool(self.keep_furigana_var.get())
        keep_scene_markers = bool(self.keep_scene_markers_var.get())
        detect_chapters = bool(self.detect_chapters_var.get())

        self._run_queue = queue.Queue()
        self._cancel_event = threading.Event()
        self._run_output_dir = output_dir

        self._run_window = ProgressWindow(
            self, on_cancel=self._cancel_run,
            on_open_output=lambda: _open_in_explorer(self._run_output_dir),
        )

        self._run_thread = threading.Thread(
            target=_run_extraction,
            args=(epub_path, output_dir, keep_furigana, keep_scene_markers,
                  detect_chapters, self._run_queue, self._cancel_event),
            daemon=True,
        )
        self._run_thread.start()
        self.after(100, self._drain_run_queue)

    def _cancel_run(self):
        if self._cancel_event is not None:
            self._cancel_event.set()

    def _drain_run_queue(self):
        if self._run_window is None or not self._run_window.winfo_exists():
            return
        try:
            while True:
                msg = self._run_queue.get_nowait()
                self._handle_run_message(msg)
        except queue.Empty:
            pass
        if self._run_thread is not None and self._run_thread.is_alive():
            self.after(100, self._drain_run_queue)

    def _handle_run_message(self, msg):
        kind = msg[0]
        if kind == "log":
            _, text, tag = msg
            self._run_window.append_log(text, tag=tag)
        elif kind == "progress":
            _, pos, total, title, is_chapter = msg
            label = f"Reading: {title}"
            self._run_window.set_item_progress(
                pos, total, label, percent=(pos / total * 100) if total else 0)
        elif kind == "result":
            _, result = msg
            self._on_extraction_result(result)
        elif kind == "error":
            _, message = msg
            self._run_window.append_log(message, tag="error")
            self._run_window.set_state("failed")

    def _on_extraction_result(self, result: ExtractResult):
        self._run_window.set_stats(
            total_items=result.total_items,
            chapters=result.chapter_count,
            non_chapters=result.non_chapter_count,
        )
        if result.cancelled:
            self._run_window.set_state("cancelled")
        elif result.chapter_count == 0 and result.non_chapter_count == 0 and not result.combined_file:
            self._run_window.append_log(
                "No chapter text found - the EPUB may use an unsupported structure.",
                tag="error")
            self._run_window.set_state("failed")
        else:
            self._run_window.set_state("completed")


def _run_extraction(epub_path, output_dir, keep_furigana, keep_scene_markers,
                     detect_chapters, out_queue, cancel_event):
    """Runs on a background thread - must not touch any Tk widgets
    directly, only push messages onto out_queue for the main thread to
    drain (see ExtractorApp._drain_run_queue)."""

    def log(message):
        stripped = message.strip()
        if not stripped:
            return
        lower = stripped.lower()
        if lower.startswith("wrote") and "chapter" in lower:
            tag = "success"
        elif "cancelled" in lower:
            tag = "error"
        elif stripped.startswith("  ["):
            tag = "processing"
        else:
            tag = "text"
        out_queue.put(("log", stripped, tag))

    def on_progress(pos, total, title, is_chapter):
        out_queue.put(("progress", pos, total, title, is_chapter))

    try:
        result = extract_epub(
            epub_path, output_dir,
            keep_furigana=keep_furigana,
            single_file=False,
            keep_scene_markers=keep_scene_markers,
            detect_chapters=detect_chapters,
            log=log,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
        if (detect_chapters and not result.cancelled
                and result.chapter_count == 0 and result.non_chapter_count > 0):
            log(f"No items matched the chapter-detection rules - all "
                f"{result.non_chapter_count} item(s) went into "
                f"\"{NON_CHAPTER_SUBDIR}\". Check that folder and, if real "
                f"chapters were missed, extend CHAPTER_TITLE_PATTERNS in "
                f"epub_extractor.py (or turn off Auto-detect chapters and "
                f"re-run).")
        out_queue.put(("result", result))
    except Exception:
        out_queue.put(("error", "Extraction failed:\n" + traceback.format_exc()))


def main():
    app = ExtractorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
