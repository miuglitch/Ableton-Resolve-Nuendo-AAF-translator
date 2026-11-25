"""Windows app — dark grey flat interface with drag and drop.

Drop files anywhere on the window and they go to the right field by type:
  .aaf / .fcpxml / .fcpxmld / .xml → timeline      .als → Live template
  video files                       → picture        folder → save location
Each field also accepts drops of its own.
"""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:          # app still works without drag and drop
    HAS_DND = False

from . import __version__, exporter, importer, video
from .media import SAMPLE_RATES
from .scrolling import ScrollManager
from .timecode import seconds_to_tc

SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "MIUGLITCH", "aaf-translator.json")

# --- palette ---------------------------------------------------------------
BG = "#1b1b1c"          # window
PANEL = "#242426"       # cards
FIELD = "#2e2e31"       # inputs
LINE = "#3a3a3e"        # borders
TEXT = "#e8e8ea"
DIM = "#9a9aa0"
ACCENT = "#d0d0d4"      # light grey accent: buttons, checks
ACCENT_TEXT = "#111112"
HOVER = "#f0f0f2"
DROP = "#36363a"
FONT = "Segoe UI"

NEW_TRACKS_ONLY = "New tracks only (template)"
TIMELINE_EXT = {".aaf", ".fcpxml", ".fcpxmld", ".xml"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv"}


def newest_set(folder: str) -> str | None:
    """Newest .als directly in a folder (Live's Backup folder is ignored)."""
    if not os.path.isdir(folder):
        return None
    sets = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".als")]
    return max(sets, key=os.path.getmtime) if sets else None


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_settings(d):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(SETTINGS, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2)


def f(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


# --- small building blocks -------------------------------------------------
class Card(ctk.CTkFrame):
    def __init__(self, parent, title=None, **kw):
        super().__init__(parent, fg_color=PANEL, corner_radius=10, border_width=1,
                         border_color=LINE, **kw)
        self.columnconfigure(1, weight=1)
        if title:
            ctk.CTkLabel(self, text=title.upper(), font=f(11, "bold"), text_color=DIM).grid(
                row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 4))


def entry(parent, var, width=None):
    kw = dict(textvariable=var, fg_color=FIELD, border_color=LINE, text_color=TEXT,
              font=f(12), corner_radius=6, height=30)
    if width:
        kw["width"] = width
    return ctk.CTkEntry(parent, **kw)


def button(parent, text, cmd, primary=False, width=90):
    if primary:
        return ctk.CTkButton(parent, text=text, command=cmd, fg_color=ACCENT, hover_color=HOVER,
                             text_color=ACCENT_TEXT, font=f(13, "bold"), corner_radius=8,
                             height=38, width=width)
    return ctk.CTkButton(parent, text=text, command=cmd, fg_color=FIELD, hover_color=LINE,
                         text_color=TEXT, font=f(12), corner_radius=6, height=30, width=width,
                         border_width=1, border_color=LINE)


def combo(parent, var, values, width=150):
    return ctk.CTkOptionMenu(parent, variable=var, values=values, width=width, height=30,
                             fg_color=FIELD, button_color=LINE, button_hover_color=DROP,
                             dropdown_fg_color=PANEL, dropdown_hover_color=LINE,
                             text_color=TEXT, font=f(12), dropdown_font=f(12), corner_radius=6)


def check(parent, text, var):
    return ctk.CTkCheckBox(parent, text=text, variable=var, font=f(12), text_color=TEXT,
                           fg_color=ACCENT, hover_color=HOVER, checkmark_color=ACCENT_TEXT,
                           border_color=DIM, corner_radius=4, border_width=2,
                           checkbox_width=18, checkbox_height=18)


def label(parent, text, dim=True, size=12):
    return ctk.CTkLabel(parent, text=text, font=f(size), text_color=DIM if dim else TEXT)


class VideoPanel(Card):
    """Codec / frame rate / resolution / keyframe interval chooser."""

    def __init__(self, parent, title, settings, key):
        super().__init__(parent, title)
        d = settings.get(key, {})
        self.settings, self.key = settings, key
        self.v_codec = tk.StringVar(value=d.get("codec", "H.264 (.mp4)"))
        self.v_rate = tk.StringVar(value=d.get("frame_rate", "Same as source"))
        res = d.get("resolution", "720p")
        if res not in video.RESOLUTIONS:
            h = video._height(res)
            res = f"{h}p" if h and f"{h}p" in video.RESOLUTIONS else "720p"
        self.v_res = tk.StringVar(value=res)
        self.v_gop = tk.StringVar(value=str(d.get("keyframe_interval", 1)))
        self.v_audio = tk.BooleanVar(value=d.get("keep_audio", False))
        self.v_tc = tk.BooleanVar(value=d.get("burn_timecode", False))

        g = ctk.CTkFrame(self, fg_color="transparent")
        g.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 12))
        label(g, "Codec").grid(row=0, column=0, sticky="w")
        combo(g, self.v_codec, list(video.CODECS), 210).grid(row=1, column=0, sticky="w", padx=(0, 12))
        label(g, "Frame rate").grid(row=0, column=1, sticky="w")
        combo(g, self.v_rate, video.FRAME_RATES, 140).grid(row=1, column=1, sticky="w", padx=(0, 12))
        label(g, "Resolution").grid(row=0, column=2, sticky="w")
        combo(g, self.v_res, video.RESOLUTIONS, 140).grid(row=1, column=2, sticky="w", padx=(0, 12))
        label(g, "Keyframe every (frames)").grid(row=0, column=3, sticky="w")
        self.gop = entry(g, self.v_gop, 70)
        self.gop.grid(row=1, column=3, sticky="w")
        row = ctk.CTkFrame(g, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))
        check(row, "Keep audio", self.v_audio).pack(side="left", padx=(0, 18))
        check(row, "Burn in timecode", self.v_tc).pack(side="left")
        self.v_codec.trace_add("write", lambda *a: self._gop_state())
        self._gop_state()

    def _gop_state(self):
        intra = not video.CODECS[self.v_codec.get()][2]
        self.gop.configure(state="disabled" if intra else "normal",
                           text_color=DIM if intra else TEXT)

    def get(self) -> "video.VideoSettings":
        try:
            gop = max(1, int(self.v_gop.get()))
        except ValueError:
            gop = 1
        s = video.VideoSettings(codec=self.v_codec.get(), frame_rate=self.v_rate.get(),
                                resolution=self.v_res.get(), keyframe_interval=gop,
                                keep_audio=self.v_audio.get(), burn_timecode=self.v_tc.get())
        self.settings[self.key] = s.__dict__
        return s


# --- the window --------------------------------------------------------------
Base = (type("Base", (ctk.CTk, TkinterDnD.DnDWrapper), {}) if HAS_DND else ctk.CTk)


class App(Base):
    def __init__(self):
        global HAS_DND
        super().__init__()
        if HAS_DND:
            try:
                self.TkdndVersion = TkinterDnD._require(self)
            except Exception:
                HAS_DND = False
        ctk.set_appearance_mode("dark")
        self.configure(fg_color=BG)
        self.title(f"MIUGLITCH AAF Translator {__version__}")
        self.geometry("940x980")
        self.minsize(820, 700)
        self.settings = load_settings()
        self.tl = None
        self.track_vars = []

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(16, 4))
        ctk.CTkLabel(head, text="MIUGLITCH", font=f(20, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(head, text="  AAF TRANSLATOR", font=f(20), text_color=DIM).pack(side="left")
        ctk.CTkLabel(head, text=f"v{__version__}", font=f(11), text_color=DIM).pack(side="right")

        tabs = ctk.CTkTabview(self, fg_color=BG, segmented_button_fg_color=PANEL,
                              segmented_button_selected_color=ACCENT,
                              segmented_button_selected_hover_color=HOVER,
                              segmented_button_unselected_color=PANEL,
                              segmented_button_unselected_hover_color=LINE,
                              text_color=TEXT, corner_radius=10, anchor="w",
                              command=self._recolor_tabs)
        tabs.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.tabs = tabs
        for name in ("Resolve / Nuendo → Live", "Live → Resolve / Nuendo", "Picture transcode"):
            tabs.add(name)
        self._recolor_tabs()

        self.scroll = ScrollManager(self)
        self._build_import(tabs.tab("Resolve / Nuendo → Live"))
        self._build_export(tabs.tab("Live → Resolve / Nuendo"))
        self._build_video(tabs.tab("Picture transcode"))

        self.scroll.install()
        if HAS_DND:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._drop_anywhere)

    def _recolor_tabs(self):
        """Dark text on the light selected tab, light text on the others."""
        try:
            sb = self.tabs._segmented_button
            cur = self.tabs.get()
            for name, btn in sb._buttons_dict.items():
                btn.configure(text_color=ACCENT_TEXT if name == cur else TEXT)
        except Exception:
            pass

    # -- drag and drop ---------------------------------------------------
    def _paths(self, event):
        return [p for p in self.tk.splitlist(event.data) if p]

    def _drop_into(self, widget, kind):
        if not HAS_DND:
            return
        target = getattr(widget, "_entry", widget)
        target.drop_target_register(DND_FILES)

        def handler(event):
            paths = self._paths(event)
            if paths:
                self._route(paths[0], force=kind)
            return event.action
        target.dnd_bind("<<Drop>>", handler)

    def _drop_anywhere(self, event):
        for p in self._paths(event):
            self._route(p)
        return event.action

    def _route(self, path, force=None):
        ext = os.path.splitext(path.rstrip("/\\"))[1].lower()
        kind = force or ("folder" if os.path.isdir(path) and ext != ".fcpxmld" else
                         "timeline" if ext in TIMELINE_EXT else
                         "template" if ext == ".als" else
                         "picture" if ext in VIDEO_EXT else None)
        if self.tabs.get() == "Picture transcode" and not force:
            kind = {"picture": "vin", "folder": "vout"}.get(kind, kind)
        if self.tabs.get() == "Live → Resolve / Nuendo" and not force:
            kind = {"template": "xset",
                    "folder": "xset" if newest_set(path) else "xout"}.get(kind, kind)
        if kind == "xset":
            return self._load_set(path)
        if kind == "xout":
            self.vx_next.set(False)
            return self.vx_out.set(path if os.path.isdir(path) else os.path.dirname(path))
        if kind == "timeline":
            self._load_timeline(path)
        elif kind == "template":
            self.v_tpl.set(path)
        elif kind == "picture":
            self.v_pic.set(path)
        elif kind == "folder" and self.v_mode.get() == "Add to existing project":
            self.v_proj.set(path if os.path.isdir(path) else os.path.dirname(path))
        elif kind == "project":
            if path.lower().endswith(".als"):
                self.v_proj.set(os.path.dirname(path))
                self.v_base.set(os.path.basename(path))
            else:
                self.v_proj.set(path if os.path.isdir(path) else os.path.dirname(path))
        elif kind == "folder":
            self.v_next.set(False)
            self._toggle_next()
            self.v_out.set(path if os.path.isdir(path) else os.path.dirname(path))
        elif kind == "vin":
            self.vv_in.set(path)
        elif kind == "vout":
            self.vv_out.set(path if os.path.isdir(path) else os.path.dirname(path))
        else:
            self._log(f"! Not sure what to do with {os.path.basename(path)}")

    # -- import tab --------------------------------------------------------
    def _file_row(self, card, row, text, var, browse, kind):
        label(card, text).grid(row=row, column=0, sticky="w", padx=(14, 10), pady=4)
        e = entry(card, var)
        e.grid(row=row, column=1, sticky="ew", pady=4)
        button(card, "Browse", browse, width=80).grid(row=row, column=2, padx=(8, 14), pady=4)
        self._drop_into(e, kind)
        return e

    def _build_import(self, tab):
        page = ctk.CTkScrollableFrame(tab, fg_color=BG, scrollbar_button_color=LINE,
                                      scrollbar_button_hover_color=DIM)
        page.pack(fill="both", expand=True)
        self.scroll.register(page)
        page.columnconfigure(0, weight=1)

        s = self.settings
        self.v_in = tk.StringVar()
        self.v_tpl = tk.StringVar(value=s.get("template", ""))
        self.v_out = tk.StringVar(value=s.get("out_dir", ""))
        self.custom_out = s.get("out_dir", "")
        self.v_next = tk.BooleanVar(value=s.get("neighbour_folder", True))
        self.v_mode = tk.StringVar(value="New Live project")
        self.v_proj = tk.StringVar()
        self.v_base = tk.StringVar(value=NEW_TRACKS_ONLY)
        self.v_note = tk.StringVar()
        self.v_name = tk.StringVar()
        self.v_tempo = tk.StringVar(value=str(s.get("tempo", 120)))
        self.v_sr = tk.StringVar(value=str(s.get("sample_rate", 48000)))
        self.v_offset = tk.StringVar()
        self.v_pic = tk.StringVar()
        self.v_pic_tc = tk.StringVar()
        self.v_pic_conv = tk.BooleanVar(value=s.get("picture_convert", True))

        if HAS_DND:
            hint = ctk.CTkFrame(page, fg_color=PANEL, corner_radius=10, border_width=1, border_color=LINE)
            hint.grid(row=0, column=0, sticky="ew", pady=(4, 10))
            ctk.CTkLabel(hint, text="Drop files anywhere: timeline (AAF / FCPXML), Live template (.als), "
                                    "picture (video) or a folder", font=f(12), text_color=DIM
                         ).pack(padx=14, pady=10)

        files = Card(page, "Files")
        files.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self._file_row(files, 1, "Timeline (AAF)", self.v_in, self._pick_input, "timeline")
        self._file_row(files, 2, "Live template", self.v_tpl,
                       lambda: self._pick(self.v_tpl, [("Live Set", "*.als")]), "template")
        ctk.CTkFrame(files, height=8, width=1, fg_color="transparent").grid(row=3, column=0)

        dest = Card(page, "Destination")
        dest.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ctk.CTkSegmentedButton(dest, values=["New Live project", "Add to existing project"],
                               variable=self.v_mode, command=lambda v: self._mode_changed(),
                               font=f(12, "bold"), fg_color=FIELD, selected_color="#5a5a60",
                               selected_hover_color="#66666c", unselected_color=FIELD,
                               unselected_hover_color=LINE, text_color=TEXT, corner_radius=6
                               ).grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 8))
        self.seg_dest = dest.winfo_children()[-1]

        self.new_fr = ctk.CTkFrame(dest, fg_color="transparent")
        self.new_fr.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.new_fr.columnconfigure(1, weight=1)
        self.out_entry = self._file_row(self.new_fr, 0, "Save project in", self.v_out, self._pick_out, "folder")
        check(self.new_fr, "Neighbour folder: next to the timeline's folder", self.v_next).grid(
            row=1, column=1, sticky="w", pady=(2, 2))
        self.v_next.trace_add("write", lambda *a: self._toggle_next())

        self.add_fr = ctk.CTkFrame(dest, fg_color="transparent")
        self.add_fr.columnconfigure(1, weight=1)
        self._file_row(self.add_fr, 0, "Live project", self.v_proj, self._pick_project, "project")
        label(self.add_fr, "Build on").grid(row=1, column=0, sticky="w", padx=(14, 10), pady=4)
        self.base_menu = combo(self.add_fr, self.v_base, [NEW_TRACKS_ONLY], 360)
        self.base_menu.grid(row=1, column=1, sticky="w", pady=4)
        label(self.add_fr, "Note").grid(row=2, column=0, sticky="w", padx=(14, 10), pady=4)
        notes = ctk.CTkFrame(self.add_fr, fg_color="transparent")
        notes.grid(row=2, column=1, sticky="w")
        entry(notes, self.v_note, 260).pack(side="left")
        label(notes, "  added to the new set's name, e.g. \"VO pickups\"").pack(side="left")
        self.v_proj.trace_add("write", lambda *a: self._project_changed())

        self.preview = ctk.CTkLabel(dest, text="", font=f(11), text_color=DIM, anchor="w",
                                    justify="left", wraplength=700)
        self.preview.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 12))
        for v in (self.v_out, self.v_name, self.v_in, self.v_note, self.v_base):
            v.trace_add("write", lambda *a: self._update_preview())
        self._toggle_next()

        st = Card(page, "Session")
        st.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        g = ctk.CTkFrame(st, fg_color="transparent")
        g.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 8))
        self.lockable = []
        for i, (t, w) in enumerate([("Set name", entry(g, self.v_name, 260)),
                                    ("Tempo (BPM)", entry(g, self.v_tempo, 80)),
                                    ("Sample rate (Hz)", combo(g, self.v_sr, [str(r) for r in SAMPLE_RATES], 110)),
                                    ("Live 0:00 = timecode", entry(g, self.v_offset, 130))]):
            label(g, t).grid(row=0, column=i, sticky="w")
            w.grid(row=1, column=i, sticky="w", padx=(0, 14))
            if i:
                self.lockable.append(w)
        self.lock_note = label(st, "")
        self.lock_note.grid(row=3, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 10))
        self.info = label(st, "Load a timeline to see its tracks.")
        self.info.grid(row=2, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 10))

        tr = Card(page, "Tracks to import")
        tr.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        bar = ctk.CTkFrame(tr, fg_color="transparent")
        bar.grid(row=1, column=0, columnspan=4, sticky="w", padx=14)
        button(bar, "Select all", lambda: self._all(True)).pack(side="left", padx=(0, 6))
        button(bar, "Select none", lambda: self._all(False)).pack(side="left")
        self.list_frame = ctk.CTkScrollableFrame(tr, fg_color=FIELD, height=40, corner_radius=6,
                                                 scrollbar_button_color=LINE)
        self.list_frame.grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=(8, 0))
        self.scroll.register(self.list_frame)
        label(self.list_frame, "No timeline loaded.").pack(anchor="w", padx=6, pady=6)
        grip = ctk.CTkFrame(tr, height=14, fg_color="transparent", cursor="sb_v_double_arrow")
        grip.grid(row=3, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 8))
        bar_ = ctk.CTkFrame(grip, height=4, width=48, fg_color=LINE, corner_radius=2,
                            cursor="sb_v_double_arrow")
        bar_.place(relx=0.5, rely=0.5, anchor="center")
        for w in (grip, bar_):
            w.bind("<ButtonPress-1>", self._grip_start)
            w.bind("<B1-Motion>", self._grip_drag)
        self._list_h = 40
        self.after(50, self._fit_list)

        pc = Card(page, "Picture reference (optional)")
        pc.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        self._file_row(pc, 1, "Video file", self.v_pic,
                       lambda: self._pick(self.v_pic, [("Video", "*.mov *.mp4 *.m4v *.mxf"),
                                                       ("All", "*.*")]), "picture")
        label(pc, "Start timecode").grid(row=2, column=0, sticky="w", padx=(14, 10), pady=4)
        tcrow = ctk.CTkFrame(pc, fg_color="transparent")
        tcrow.grid(row=2, column=1, sticky="w")
        entry(tcrow, self.v_pic_tc, 130).pack(side="left")
        label(tcrow, "  blank = read from the file").pack(side="left")
        check(pc, "Transcode picture for Live", self.v_pic_conv).grid(
            row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 8))
        self.pic_panel = VideoPanel(pc, "Picture format", s, "picture_video")
        self.pic_panel.grid(row=4, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 14))

        run = ctk.CTkFrame(page, fg_color="transparent")
        run.grid(row=6, column=0, sticky="ew", pady=(4, 6))
        run.columnconfigure(1, weight=1)
        self.go = button(run, "Create Live Set", self._run, primary=True, width=200)
        self.go.grid(row=0, column=0, sticky="w")
        self.bar = ctk.CTkProgressBar(run, fg_color=FIELD, progress_color=ACCENT, height=8)
        self.bar.set(0)
        self.bar.grid(row=0, column=1, sticky="ew", padx=(16, 0))

        self.log = ctk.CTkTextbox(page, height=160, fg_color=PANEL, text_color=TEXT,
                                  border_width=1, border_color=LINE, font=ctk.CTkFont("Consolas", 12),
                                  corner_radius=8)
        self.log.grid(row=7, column=0, sticky="ew", pady=(6, 4))

    def _pick(self, var, types):
        p = filedialog.askopenfilename(filetypes=types)
        if p:
            var.set(p)

    def _pick_out(self):
        p = filedialog.askdirectory()
        if p:
            self.v_out.set(p)

    def _pick_input(self):
        p = filedialog.askopenfilename(filetypes=[("Timelines", "*.aaf *.fcpxml *.xml"), ("All", "*.*")])
        if p:
            self._load_timeline(p)

    def _load_timeline(self, p):
        self.v_in.set(p)
        try:
            self.tl = importer.load(p)
        except Exception as e:
            self._log(f"! Can't read timeline: {e}")
            self.tl = None
            return
        tl = self.tl
        self.v_name.set(tl.name)
        start_tc = seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)
        self.v_offset.set(start_tc)
        self.info.configure(text=f"{tl.name}  ·  {float(tl.frame_rate):.3f} fps"
                                 f"{' DF' if tl.drop_frame else ''}  ·  starts {start_tc}  ·  "
                                 f"{tl.sample_rate} Hz  ·  {len(tl.markers)} marker{'s' if len(tl.markers) != 1 else ''}",
                            text_color=TEXT)
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.track_vars = []
        for t in tl.audio_tracks:
            v = tk.BooleanVar(value=True)
            chans = "mono" if t.channels == 1 else "stereo"
            n = len(t.clips)
            check(self.list_frame, f"{t.name}    {n} clip{'s' if n != 1 else ''} · {chans}", v).pack(
                anchor="w", padx=6, pady=3)
            self.track_vars.append((t.name, v))
        if self.v_next.get():
            self.v_out.set(self._neighbour_dir())
        self.after(30, self._fit_list)
        self._log(f"Loaded {os.path.basename(p)}: {len(tl.audio_tracks)} audio tracks")
        for w in tl.warnings:
            self._log("! " + w)

    # -- save location ----------------------------------------------------------
    def _mode_changed(self):
        adding = self.v_mode.get() == "Add to existing project"
        if adding:
            self.new_fr.grid_remove()
            self.add_fr.grid(row=2, column=0, columnspan=3, sticky="ew")
        else:
            self.add_fr.grid_remove()
            self.new_fr.grid()
        self._project_changed()

    def _toggle_next(self):
        if not hasattr(self, "out_entry"):
            return
        if self.v_next.get():
            if self.v_out.get() and self.v_out.get() != self._neighbour_dir():
                self.custom_out = self.v_out.get()
            self.v_out.set(self._neighbour_dir())
            self.out_entry.configure(state="disabled", text_color=DIM)
        else:
            self.out_entry.configure(state="normal", text_color=TEXT)
            if self.v_out.get() == self._neighbour_dir():
                self.v_out.set(self.custom_out)
        self._update_preview()

    def _neighbour_dir(self):
        """The folder that holds the timeline's folder: the project becomes a
        sibling of the export folder, never inside it."""
        p = self.v_in.get()
        return os.path.dirname(os.path.dirname(os.path.abspath(p))) if p else ""

    def _pick_project(self):
        p = filedialog.askdirectory(title="Choose the Live project folder")
        if p:
            self.v_proj.set(p)

    def _project_changed(self):
        """List the project's sets and lock tempo/rate/offset to its earlier import."""
        proj = self.v_proj.get()
        adding = self.v_mode.get() == "Add to existing project"
        sets = []
        if proj and os.path.isdir(proj):
            sets = sorted((n for n in os.listdir(proj) if n.lower().endswith(".als")),
                          key=lambda n: os.path.getmtime(os.path.join(proj, n)), reverse=True)
        self.base_menu.configure(values=[NEW_TRACKS_ONLY] + sets)
        if proj != getattr(self, "_last_proj", None) or self.v_base.get() not in [NEW_TRACKS_ONLY] + sets:
            self.v_base.set(sets[0] if sets else NEW_TRACKS_ONLY)   # newest set by default
        self._last_proj = proj
        side = None
        if adding and proj and os.path.isdir(proj):
            base = self._base_path()
            try:
                side = importer.project_sidecar(proj, base)
            except Exception:
                side = None
        for w in self.lockable:
            w.configure(state="disabled" if side else "normal",
                        text_color=DIM if side else TEXT)
        if side:
            self.v_tempo.set(f"{side.tempo:g}")
            self.v_sr.set(str(side.media_sample_rate))
            self.v_offset.set(seconds_to_tc(side.offset_s, side.rate, side.drop_frame))
            self.lock_note.configure(text="Tempo, sample rate and offset come from this project's "
                                          "earlier import, so new audio lines up.")
        else:
            self.lock_note.configure(text="")
        self._update_preview()

    def _base_path(self):
        b = self.v_base.get()
        return os.path.join(self.v_proj.get(), b) if b and b != NEW_TRACKS_ONLY else None

    def _update_preview(self):
        if not hasattr(self, "preview"):
            return
        name = self.v_name.get() or "Timeline"
        if self.v_mode.get() == "Add to existing project":
            if not self.v_proj.get():
                return self.preview.configure(text="Choose or drop the Live project folder.")
            opt = importer.ImportOptions(template="", existing_project=self.v_proj.get(),
                                         note=self.v_note.get())
            _, als = importer.target_paths(opt, name)
            base = self.v_base.get()
            what = "new tracks only" if base == NEW_TRACKS_ONLY else f"a copy of {base} + new tracks"
            return self.preview.configure(
                text=f"Creates  {os.path.normpath(als)}   ({what}; audio goes to Samples\\Imported)")
        if not self.v_out.get():
            return self.preview.configure(text="Load a timeline, or untick and choose a folder.")
        opt = importer.ImportOptions(template="", out_dir=self.v_out.get())
        _, als = importer.target_paths(opt, name)
        self.preview.configure(text=f"Creates  {os.path.normpath(als)}")

    # -- track list sizing --------------------------------------------------------
    MAX_ROWS = 10

    def _rows(self):
        return [w for w in self.list_frame.winfo_children() if w.winfo_manager() == "pack"]

    def _fit_list(self):
        """Height = all tracks up to 10, then scroll."""
        self.update_idletasks()
        rows = self._rows()
        if not rows:
            return
        row_h = max(r.winfo_reqheight() for r in rows) + 6
        visible = min(len(rows), self.MAX_ROWS)
        self._set_list_height(visible * row_h + 6)

    def _set_list_height(self, h):
        h = int(max(36, min(h, 2000)))
        self._list_h = h
        self.list_frame.configure(height=h)
        self.update_idletasks()
        content = sum(r.winfo_reqheight() + 6 for r in self._rows())
        sb = self.list_frame._scrollbar
        if content > h + 4:
            sb.grid()
        else:
            sb.grid_remove()
            self.list_frame._parent_canvas.yview_moveto(0)

    def _grip_start(self, event):
        self._grip = (event.y_root, self._list_h)

    def _grip_drag(self, event):
        y0, h0 = self._grip
        self._set_list_height(h0 + (event.y_root - y0))

    def _all(self, on):
        for _, v in self.track_vars:
            v.set(on)

    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _run(self):
        if not self.tl:
            return self._log("! Load or drop an AAF / FCPXML first.")
        if not os.path.isfile(self.v_tpl.get()):
            return self._log("! Pick or drop your Live template set (.als).")
        adding = self.v_mode.get() == "Add to existing project"
        if adding and not os.path.isdir(self.v_proj.get()):
            return self._log("! Choose or drop the Live project folder to add to.")
        if not adding and not self.v_out.get():
            return self._log("! Choose or drop the folder to save the project in.")
        selected = [n for n, v in self.track_vars if v.get()]
        if not selected:
            return self._log("! Tick at least one track.")
        try:
            tempo = float(self.v_tempo.get())
        except ValueError:
            return self._log("! Tempo must be a number.")
        if not self.v_next.get():
            self.settings["out_dir"] = self.v_out.get()
        self.settings.update(template=self.v_tpl.get(), neighbour_folder=self.v_next.get(), tempo=tempo,
                             picture_convert=self.v_pic_conv.get(), sample_rate=int(self.v_sr.get()))
        opt = importer.ImportOptions(
            template=self.v_tpl.get(), out_dir=self.v_out.get(), set_name=self.v_name.get(),
            tempo=tempo, offset_tc=self.v_offset.get() or None, selected=selected,
            sample_rate=int(self.v_sr.get()),
            existing_project=self.v_proj.get() if adding else None,
            base_set=self._base_path() if adding else None, note=self.v_note.get(),
            picture=self.v_pic.get() or None, picture_tc=self.v_pic_tc.get() or None,
            picture_video=self.pic_panel.get() if self.v_pic_conv.get() else None,
            log=lambda m: self.after(0, self._log, m),
            progress=lambda i, n: self.after(0, self.bar.set, (i + 1) / max(n, 1)))
        save_settings(self.settings)
        self.go.configure(state="disabled", text="Working…")
        path = self.v_in.get()

        def work():
            try:
                res = importer.run(path, opt, tl=self.tl)
                self.after(0, self._log, f"✓ Live Set created: {res.als_path}")
                self.after(0, self._project_changed)
                self.after(0, self._log, f"  {len(res.warnings)} warning(s), report saved next to the set.")
            except Exception as e:
                self.after(0, self._log, f"✗ Import failed: {e}")
            finally:
                self.after(0, lambda: self.go.configure(state="normal", text="Create Live Set"))
                try:
                    self.tl = importer.load(path)     # fresh copy for another run
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    # -- export tab -----------------------------------------------------------------
    def _build_export(self, tab):
        page = ctk.CTkScrollableFrame(tab, fg_color=BG, scrollbar_button_color=LINE,
                                      scrollbar_button_hover_color=DIM)
        page.pack(fill="both", expand=True)
        self.scroll.register(page)
        page.columnconfigure(0, weight=1)
        s = self.settings
        self.vx_set = tk.StringVar()
        self.vx_out = tk.StringVar(value=s.get("export_out", ""))
        self.vx_custom = s.get("export_out", "")
        self.vx_next = tk.BooleanVar(value=s.get("export_neighbour", True))
        self.vx_name = tk.StringVar()
        self.vx_fader = tk.BooleanVar(value=s.get("export_fader", True))
        self.vx_muted = tk.BooleanVar(value=s.get("export_muted", False))
        self.vx_split = tk.BooleanVar(value=s.get("export_split", True))
        self.vx_prefix = tk.StringVar(value=s.get("export_prefix", exporter.SD_PREFIX))
        self.vx_tc = tk.StringVar(value="01:00:00:00")
        self.vx_rate = tk.StringVar(value="25")
        self.xpre = None
        self.xtrack_vars = []

        files = Card(page, "Files")
        files.grid(row=0, column=0, sticky="ew", pady=(4, 10))
        self._file_row(files, 1, "Live Set (.als)", self.vx_set,
                       lambda: self._load_set(filedialog.askopenfilename(filetypes=[("Live Set", "*.als")])),
                       "xset")
        self.xout_entry = self._file_row(files, 2, "Save export in", self.vx_out,
                                         lambda: self.vx_out.set(filedialog.askdirectory() or self.vx_out.get()),
                                         "xout")
        check(files, "Neighbour folder: 'Export to Resolve' next to the set's folder", self.vx_next).grid(
            row=3, column=1, sticky="w", pady=(2, 2))
        self.vx_next.trace_add("write", lambda *a: self._x_toggle())
        self.x_preview = ctk.CTkLabel(files, text="", font=f(11), text_color=DIM, anchor="w",
                                      justify="left", wraplength=700)
        self.x_preview.grid(row=4, column=1, columnspan=2, sticky="w", pady=(0, 10))
        for v in (self.vx_out, self.vx_name):
            v.trace_add("write", lambda *a: self._x_preview())

        opts = Card(page, "Export")
        opts.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        g = ctk.CTkFrame(opts, fg_color="transparent")
        g.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 6))
        label(g, "Format").grid(row=0, column=0, sticky="w")
        label(g, "FCPXML for DaVinci Resolve  (AAF for Nuendo is next)", dim=False).grid(
            row=1, column=0, sticky="w", padx=(0, 20))
        label(g, "Timeline name").grid(row=0, column=1, sticky="w")
        entry(g, self.vx_name, 240).grid(row=1, column=1, sticky="w", padx=(0, 14))
        label(g, "New sound design prefix").grid(row=0, column=2, sticky="w")
        entry(g, self.vx_prefix, 90).grid(row=1, column=2, sticky="w")
        row = ctk.CTkFrame(opts, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=4, sticky="w", padx=14, pady=(6, 6))
        check(row, "Include track fader level", self.vx_fader).pack(side="left", padx=(0, 18))
        check(row, "Separate tracks for new sound design", self.vx_split).pack(side="left")
        self.x_fallback = ctk.CTkFrame(opts, fg_color="transparent")
        label(self.x_fallback, "This set has no import info. Timeline start").pack(side="left")
        entry(self.x_fallback, self.vx_tc, 110).pack(side="left", padx=6)
        label(self.x_fallback, "Frame rate").pack(side="left", padx=(8, 0))
        combo(self.x_fallback, self.vx_rate, ["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"],
              100).pack(side="left", padx=6)
        self.x_info = label(opts, "Load or drop a Live Set.")
        self.x_info.grid(row=4, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 12))

        tr = Card(page, "Tracks to export")
        tr.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        bar = ctk.CTkFrame(tr, fg_color="transparent")
        bar.grid(row=1, column=0, columnspan=4, sticky="w", padx=14)
        button(bar, "Select all", lambda: [v.set(True) for _, v in self.xtrack_vars]).pack(side="left", padx=(0, 6))
        button(bar, "Select none", lambda: [v.set(False) for _, v in self.xtrack_vars]).pack(side="left")
        self.xlist = ctk.CTkScrollableFrame(tr, fg_color=FIELD, height=40, corner_radius=6,
                                            scrollbar_button_color=LINE)
        self.xlist.grid(row=2, column=0, columnspan=4, sticky="ew", padx=14, pady=(8, 10))
        self.scroll.register(self.xlist)
        label(self.xlist, "No Live Set loaded.").pack(anchor="w", padx=6, pady=6)

        self.xwarn = Card(page, "Before exporting")
        self.xwarn_text = ctk.CTkLabel(self.xwarn, text="", font=f(12), text_color=TEXT, anchor="w",
                                       justify="left", wraplength=780)
        self.xwarn_text.grid(row=1, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 12))

        run = ctk.CTkFrame(page, fg_color="transparent")
        run.grid(row=4, column=0, sticky="ew", pady=(4, 6))
        self.xgo = button(run, "Export to Resolve", self._run_export, primary=True, width=200)
        self.xgo.grid(row=0, column=0, sticky="w")
        self.xlog = ctk.CTkTextbox(page, height=150, fg_color=PANEL, text_color=TEXT, border_width=1,
                                   border_color=LINE, font=ctk.CTkFont("Consolas", 12), corner_radius=8)
        self.xlog.grid(row=5, column=0, sticky="ew", pady=(6, 4))
        self._x_toggle()

    def _xsay(self, m):
        self.xlog.insert("end", m + "\n")
        self.xlog.see("end")

    def _x_neighbour(self):
        p = self.vx_set.get()
        return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(p))), "Export to Resolve") if p else ""

    def _x_toggle(self):
        if self.vx_next.get():
            if self.vx_out.get() and self.vx_out.get() != self._x_neighbour():
                self.vx_custom = self.vx_out.get()
            self.vx_out.set(self._x_neighbour())
            self.xout_entry.configure(state="disabled", text_color=DIM)
        else:
            self.xout_entry.configure(state="normal", text_color=TEXT)
            if self.vx_out.get() == self._x_neighbour():
                self.vx_out.set(self.vx_custom)
        self._x_preview()

    def _x_preview(self):
        if not self.vx_out.get():
            return self.x_preview.configure(text="Load a Live Set, or untick and choose a folder.")
        name = exporter._safe(self.vx_name.get() or "Timeline")
        self.x_preview.configure(text=f"Creates  {os.path.normpath(os.path.join(self.vx_out.get(), name))}"
                                      f" <date time>.fcpxml  +  '{name} Media' folder for new sound design")

    def _load_set(self, p):
        if not p:
            return
        if os.path.isdir(p):                       # a Live project folder was dropped
            found = newest_set(p)
            if not found:
                return self._xsay(f"! No Live Set (.als) in {p}")
            self._xsay(f"Using the newest set in that folder: {os.path.basename(found)}")
            p = found
        self.vx_set.set(p)
        try:
            pre = exporter.preflight(p)
        except Exception as e:
            return self._xsay(f"! Can't read Live Set: {e}")
        self.xpre = pre
        self.vx_name.set(os.path.splitext(os.path.basename(p))[0])
        if self.vx_next.get():
            self.vx_out.set(self._x_neighbour())
        side = pre.side
        if side:
            self.x_fallback.grid_forget()
            self.x_info.configure(
                text=f"{pre.live.tempo:g} BPM  ·  {float(side.rate):.3f} fps  ·  Live 0:00 = "
                     f"{seconds_to_tc(side.offset_s, side.rate, side.drop_frame)}  ·  from '{side.timeline_name}'",
                text_color=TEXT)
        else:
            self.x_fallback.grid(row=3, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 6))
            self.x_info.configure(text=f"{pre.live.tempo:g} BPM", text_color=TEXT)
        for w in self.xlist.winfo_children():
            w.destroy()
        self.xtrack_vars = []
        for lt in exporter.export_tracks(pre):
            n_orig = sum(1 for c in lt.track.clips if side and side.link_for(c.source_path))
            n_sd = len(lt.track.clips) - n_orig
            parts = [f"{n_orig} original"] if n_orig else []
            parts += [f"{n_sd} new"] if n_sd else []
            flags = ("  ·  plug-ins" if lt.has_devices else "") + ("  ·  muted" if lt.muted else "")
            v = tk.BooleanVar(value=not lt.muted)
            check(self.xlist, f"{lt.track.name}    {', '.join(parts)}{flags}", v).pack(anchor="w", padx=6, pady=3)
            self.xtrack_vars.append((lt.track.name, v))
        self.after(30, lambda: self._fit_any(self.xlist))
        if pre.warnings:
            self.xwarn_text.configure(text="\n".join("•  " + w for w in pre.warnings))
            self.xwarn.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        else:
            self.xwarn.grid_forget()
        self._xsay(f"Loaded {os.path.basename(p)}: {len(self.xtrack_vars)} tracks with audio")

    def _fit_any(self, lst):
        self.update_idletasks()
        rows = [w for w in lst.winfo_children() if w.winfo_manager() == "pack"]
        if not rows:
            return
        row_h = max(r.winfo_reqheight() for r in rows) + 6
        h = min(len(rows), self.MAX_ROWS) * row_h + 6
        lst.configure(height=h)
        self.update_idletasks()
        if len(rows) > self.MAX_ROWS:
            lst._scrollbar.grid()
        else:
            lst._scrollbar.grid_remove()

    def _run_export(self):
        p = self.vx_set.get()
        if not self.xpre or not os.path.isfile(p):
            return self._xsay("! Load or drop a Live Set (.als) first.")
        sel = [n for n, v in self.xtrack_vars if v.get()]
        if not sel:
            return self._xsay("! Tick at least one track.")
        if not self.vx_out.get():
            return self._xsay("! Choose where to save the export.")
        s = self.settings
        if not self.vx_next.get():
            s["export_out"] = self.vx_out.get()
        s.update(export_neighbour=self.vx_next.get(), export_fader=self.vx_fader.get(),
                 export_muted=self.vx_muted.get(), export_split=self.vx_split.get(),
                 export_prefix=self.vx_prefix.get())
        save_settings(s)
        opt = exporter.ExportOptions(
            out_dir=self.vx_out.get(), selected=sel, name=self.vx_name.get(),
            sd_prefix=self.vx_prefix.get(), split_mixed=self.vx_split.get(),
            include_muted=True, include_fader=self.vx_fader.get(),
            offset_tc=self.vx_tc.get(), frame_rate=self.vx_rate.get(),
            log=lambda m: self.after(0, self._xsay, m))
        self.xgo.configure(state="disabled", text="Working…")

        def work():
            try:
                res = exporter.run(p, opt, exporter.preflight(p))
                self.after(0, self._xsay, f"✓ Exported: {res.path}")
                self.after(0, self._xsay, "  In Resolve: File → Import → Timeline… and pick this file.")
            except Exception as e:
                self.after(0, self._xsay, f"✗ Export failed: {e}")
            finally:
                self.after(0, lambda: self.xgo.configure(state="normal", text="Export to Resolve"))

        threading.Thread(target=work, daemon=True).start()

    # -- picture transcode tab --------------------------------------------------
    def _build_video(self, tab):
        page = ctk.CTkFrame(tab, fg_color=BG)
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        self.vv_in = tk.StringVar()
        self.vv_out = tk.StringVar(value=self.settings.get("video_out", ""))
        files = Card(page, "Files")
        files.grid(row=0, column=0, sticky="ew", pady=(4, 10))
        self._file_row(files, 1, "Video file", self.vv_in,
                       lambda: self._pick(self.vv_in, [("Video", "*.mov *.mp4 *.mxf *.m4v *.avi"),
                                                       ("All", "*.*")]), "vin")
        self._file_row(files, 2, "Save to folder", self.vv_out,
                       lambda: self.vv_out.set(filedialog.askdirectory() or self.vv_out.get()), "vout")
        ctk.CTkFrame(files, height=8, width=1, fg_color="transparent").grid(row=3, column=0)
        self.vid_panel = VideoPanel(page, "Output format", self.settings, "standalone_video")
        self.vid_panel.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.vbtn = button(page, "Transcode", self._run_video, primary=True, width=200)
        self.vbtn.grid(row=2, column=0, sticky="w", pady=6)
        self.vlog = ctk.CTkTextbox(page, height=180, fg_color=PANEL, text_color=TEXT, border_width=1,
                                   border_color=LINE, font=ctk.CTkFont("Consolas", 12), corner_radius=8)
        self.vlog.grid(row=3, column=0, sticky="nsew", pady=6)

    def _run_video(self):
        src = self.vv_in.get()
        out = self.vv_out.get() or os.path.dirname(src)

        def say(m):
            self.after(0, lambda: (self.vlog.insert("end", m + "\n"), self.vlog.see("end")))
        if not os.path.isfile(src):
            return say("! Choose or drop a video file.")
        s = self.vid_panel.get()
        self.settings["video_out"] = out
        save_settings(self.settings)
        self.vbtn.configure(state="disabled", text="Working…")

        def work():
            try:
                say(f"✓ Done: {video.transcode(src, out, s, say)}")
            except Exception as e:
                say(f"✗ Failed: {e}")
            finally:
                self.after(0, lambda: self.vbtn.configure(state="normal", text="Transcode"))

        threading.Thread(target=work, daemon=True).start()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
