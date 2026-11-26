"""The 'Resolve → Live' tab: one click builds a Live Set from the timeline
that's open in DaVinci Resolve Studio."""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from . import importer, resolve_api, resolve_live, template, video
from .media import SAMPLE_RATES
from .timecode import seconds_to_tc

TAB = "Resolve → Live"
def res_label(res: str, w: int = 1920, h: int = 1080) -> str:
    """'720p' -> '720p (1280x720)' for the timeline's shape."""
    if res == "Same as source":
        return f"Same as source ({w}x{h})"
    s = video.VideoSettings(resolution=res)
    tw, th = video.target_size(s, w, h)
    return f"{res} ({tw}x{th})"


DEFAULT_OUT = os.path.join(os.path.expanduser("~"), "Documents", "MIUGLITCH Live Sets")


def open_file(path):
    """Open with the default app (Live for .als)."""
    import subprocess
    import sys
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)                         # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass
NEW, ADD = "New Live project", "Add to existing project"
FULL_LBL = "Handles from the edit (fastest)"
HANDLES_LBL = "Real source handles"


class ResolveTab:
    """Mixed into App (gui.py); uses its helpers and palette."""

    def _build_resolve(self, tab):
        from .gui import (ACCENT, BG, DIM, FIELD, LINE, PANEL, TEXT, Card, NEW_TRACKS_ONLY,
                          button, check, combo, entry, f, label)
        self._rv_new_tracks_only = NEW_TRACKS_ONLY
        s = self.settings
        page = ctk.CTkScrollableFrame(tab, fg_color=BG, scrollbar_button_color=LINE,
                                      scrollbar_button_hover_color=DIM)
        page.pack(fill="both", expand=True)
        self.scroll.register(page)
        page.columnconfigure(0, weight=1)
        self.rt = None

        # --- Resolve ---------------------------------------------------------
        rc = Card(page, "DaVinci Resolve Studio")
        rc.grid(row=0, column=0, sticky="ew", pady=(4, 10))
        row = ctk.CTkFrame(rc, fg_color="transparent")
        row.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 6))
        self.rv_connect = button(row, "Read open timeline", self._rv_read, width=170)
        self.rv_connect.pack(side="left")
        self.rv_status = ctk.CTkLabel(row, text="Open your timeline in Resolve, then click.", font=f(12),
                                      text_color=DIM, anchor="w", justify="left", wraplength=620)
        self.rv_status.pack(side="left", padx=12)
        self.rv_detail = label(rc, "")
        self.rv_detail.grid(row=2, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 10))

        # --- template + destination -------------------------------------------
        dest = Card(page, "Live Set")
        dest.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.rv_tpl = tk.StringVar(value=s.get("rv_template", ""))
        self.rv_own = tk.BooleanVar(value=s.get("rv_own_template", False))
        self.rv_open = tk.BooleanVar(value=s.get("rv_open_live", False))
        self.rv_mode = tk.StringVar(value=NEW)
        ctk.CTkSegmentedButton(dest, values=[NEW, ADD], variable=self.rv_mode,
                               command=lambda v: self._rv_mode_changed(),
                               font=f(12, "bold"), fg_color=FIELD, selected_color="#5a5a60",
                               selected_hover_color="#66666c", unselected_color=FIELD,
                               unselected_hover_color=LINE, text_color=TEXT, corner_radius=6
                               ).grid(row=2, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 6))
        self.rv_out = tk.StringVar(value=s.get("resolve_out_dir") or DEFAULT_OUT)
        self.rv_new_fr = ctk.CTkFrame(dest, fg_color="transparent")
        self.rv_new_fr.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.rv_new_fr.columnconfigure(1, weight=1)
        self._file_row(self.rv_new_fr, 0, "Save project in", self.rv_out,
                       lambda: self.rv_out.set(filedialog.askdirectory() or self.rv_out.get()), "rout")
        self.rv_proj = tk.StringVar()
        self.rv_base = tk.StringVar(value=NEW_TRACKS_ONLY)
        self.rv_note = tk.StringVar()
        self.rv_add_fr = ctk.CTkFrame(dest, fg_color="transparent")
        self.rv_add_fr.columnconfigure(1, weight=1)
        self._file_row(self.rv_add_fr, 0, "Live project", self.rv_proj,
                       lambda: self.rv_proj.set(filedialog.askdirectory() or self.rv_proj.get()), "rproj")
        label(self.rv_add_fr, "Build on").grid(row=1, column=0, sticky="w", padx=(14, 10), pady=4)
        self.rv_base_menu = combo(self.rv_add_fr, self.rv_base, [NEW_TRACKS_ONLY], 360)
        self.rv_base_menu.grid(row=1, column=1, sticky="w", pady=4)
        label(self.rv_add_fr, "Note").grid(row=2, column=0, sticky="w", padx=(14, 10), pady=4)
        entry(self.rv_add_fr, self.rv_note, 260).grid(row=2, column=1, sticky="w", pady=4)
        self.rv_proj.trace_add("write", lambda *a: self._rv_project_changed())
        g = ctk.CTkFrame(dest, fg_color="transparent")
        g.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 12))
        self.rv_name = tk.StringVar()
        self.rv_tempo = tk.StringVar(value=str(s.get("tempo", 120)))
        self.rv_sr = tk.StringVar(value=str(s.get("sample_rate", 48000)))
        self.rv_lockable = []
        for i, (t, w) in enumerate([("Set name", entry(g, self.rv_name, 260)),
                                    ("Tempo (BPM)", entry(g, self.rv_tempo, 80)),
                                    ("Sample rate (Hz)", combo(g, self.rv_sr, [str(r) for r in SAMPLE_RATES], 110))]):
            label(g, t).grid(row=0, column=i, sticky="w")
            w.grid(row=1, column=i, sticky="w", padx=(0, 14))
            if i:
                self.rv_lockable.append(w)
        self.rv_lock_note = label(dest, "")
        self.rv_lock_note.grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 8))

        # --- picture -------------------------------------------------------------
        pc = Card(page, "Picture")
        pc.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.rv_video = tk.BooleanVar(value=s.get("rv_video", True))
        check(pc, "Bring the picture: Resolve renders it (grade, Fusion, titles), one Live clip per shot",
              self.rv_video).grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 8))
        self.rv_rmode = tk.StringVar(value=HANDLES_LBL if s.get("rv_render_mode") == "handles" else FULL_LBL)
        ctk.CTkSegmentedButton(pc, values=[FULL_LBL, HANDLES_LBL], variable=self.rv_rmode,
                               font=f(12, "bold"), fg_color=FIELD, selected_color="#5a5a60",
                               selected_hover_color="#66666c", unselected_color=FIELD,
                               unselected_hover_color=LINE, text_color=TEXT, corner_radius=6
                               ).grid(row=2, column=0, columnspan=3, sticky="w", padx=14)
        self.rv_mode_hint = label(pc, "")
        self.rv_mode_hint.configure(wraplength=760, justify="left")
        self.rv_mode_hint.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 6))
        self.rv_rmode.trace_add("write", lambda *a: self._rv_hint())
        self._rv_hint()
        hrow = ctk.CTkFrame(pc, fg_color="transparent")
        hrow.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=(2, 4))
        self.rv_vhead = tk.StringVar(value=str(s.get("rv_video_head", 2.0)))
        self.rv_vtail = tk.StringVar(value=str(s.get("rv_video_tail", 2.0)))
        label(hrow, "Head (s)").pack(side="left")
        entry(hrow, self.rv_vhead, 60).pack(side="left", padx=(8, 18))
        label(hrow, "Tail (s)").pack(side="left")
        entry(hrow, self.rv_vtail, 60).pack(side="left", padx=(8, 18))
        crow = ctk.CTkFrame(pc, fg_color="transparent")
        crow.grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 4))
        self.rv_shotmarks = tk.BooleanVar(value=s.get("rv_shot_markers", True))
        self.rv_rmarks = tk.BooleanVar(value=s.get("rv_resolve_markers", True))
        self.rv_mix = tk.BooleanVar(value=s.get("rv_mix_track", False))
        check(crow, "Locator at each shot", self.rv_shotmarks).pack(side="left", padx=(0, 18))
        check(crow, "Resolve markers → locators", self.rv_rmarks).pack(side="left", padx=(0, 18))
        crow2 = ctk.CTkFrame(pc, fg_color="transparent")
        crow2.grid(row=6, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 8))
        check(crow2, "Also one continuous stereo mix track", self.rv_mix).pack(side="left", padx=(0, 18))
        label(pc, "Each video clip plays the timeline's stereo mix as its sound.").grid(
            row=7, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 6))
        vp = Card(pc, "Video format for Live (Resolve renders it directly)")
        vp.grid(row=8, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 14))
        d = s.get("resolve_video", {})
        codecs = list(resolve_live.RESOLVE_CODECS)
        self.rv_codec = tk.StringVar(value=d.get("codec") if d.get("codec") in codecs else codecs[0])
        self.rv_rate = tk.StringVar(value=d.get("frame_rate", "Same as source"))
        res = d.get("resolution", "720p")
        self.rv_res = tk.StringVar(value=res_label(res if res in video.RESOLUTIONS else "720p"))
        self.rv_preset = tk.StringVar(value=s.get("rv_render_preset", ""))
        vg = ctk.CTkFrame(vp, fg_color="transparent")
        vg.grid(row=1, column=0, columnspan=4, sticky="ew", padx=14, pady=(0, 8))
        label(vg, "Codec").grid(row=0, column=0, sticky="w")
        combo(vg, self.rv_codec, codecs, 210).grid(row=1, column=0, sticky="w", padx=(0, 12))
        label(vg, "Frame rate").grid(row=0, column=1, sticky="w")
        combo(vg, self.rv_rate, video.FRAME_RATES, 140).grid(row=1, column=1, sticky="w", padx=(0, 12))
        label(vg, "Resolution").grid(row=0, column=2, sticky="w")
        self.rv_res_menu = combo(vg, self.rv_res, [res_label(r) for r in video.RESOLUTIONS], 190)
        self.rv_res_menu.grid(row=1, column=2, sticky="w", padx=(0, 12))
        label(vg, "Resolve render preset (optional)").grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        entry(vg, self.rv_preset, 210).grid(row=3, column=0, sticky="w", padx=(0, 12))
        hint = label(vg, "H.264 gets a key frame on every frame (Live needs it) through your 'ABLETON VIDEO' Resolve "
                         "render preset, added to Resolve automatically. Only type a preset name to use your own.")
        hint.configure(wraplength=560, justify="left")
        hint.grid(row=3, column=1, columnspan=3, sticky="w")

        # --- audio ---------------------------------------------------------------
        ac = Card(page, "Audio")
        ac.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.rv_audio = tk.BooleanVar(value=s.get("rv_audio", True))
        check(ac, "Bring the audio: each Resolve track → a Live track with the same name",
              self.rv_audio).grid(row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 6))
        self.rv_list = ctk.CTkScrollableFrame(ac, fg_color=FIELD, height=90, corner_radius=6,
                                              scrollbar_button_color=LINE)
        self.rv_list.grid(row=2, column=0, columnspan=3, sticky="ew", padx=14, pady=(2, 6))
        self.scroll.register(self.rv_list)
        label(self.rv_list, "Read the timeline to see its audio tracks.").pack(anchor="w", padx=6, pady=6)
        self.rv_track_vars = []
        arow = ctk.CTkFrame(ac, fg_color="transparent")
        arow.grid(row=3, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 4))
        self.rv_whole = tk.StringVar(value="Whole source files" if s.get("rv_audio_whole") else "Handles")
        ctk.CTkSegmentedButton(arow, values=["Handles", "Whole source files"], variable=self.rv_whole,
                               font=f(12, "bold"), fg_color=FIELD, selected_color="#5a5a60",
                               selected_hover_color="#66666c", unselected_color=FIELD,
                               unselected_hover_color=LINE, text_color=TEXT, corner_radius=6
                               ).pack(side="left", padx=(0, 18))
        self.rv_ahead = tk.StringVar(value=str(s.get("rv_audio_head", 2.0)))
        self.rv_atail = tk.StringVar(value=str(s.get("rv_audio_tail", 2.0)))
        label(arow, "Head (s)").pack(side="left")
        self.rv_ahead_e = entry(arow, self.rv_ahead, 60)
        self.rv_ahead_e.pack(side="left", padx=(8, 18))
        label(arow, "Tail (s)").pack(side="left")
        self.rv_atail_e = entry(arow, self.rv_atail, 60)
        self.rv_atail_e.pack(side="left", padx=(8, 0))
        self.rv_whole.trace_add("write", lambda *a: self._rv_whole_state())
        self._rv_whole_state()
        nrow = ctk.CTkFrame(ac, fg_color="transparent")
        nrow.grid(row=4, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 4))
        self.rv_norm = tk.BooleanVar(value=s.get("rv_normalize", False))
        self.rv_norm_db = tk.StringVar(value=str(s.get("rv_normalize_db", -1.0)))
        check(nrow, "Normalise clips to", self.rv_norm).pack(side="left")
        entry(nrow, self.rv_norm_db, 50).pack(side="left", padx=(8, 4))
        label(nrow, "dBFS peak (as clip gain)").pack(side="left")
        self.rv_aaf = tk.BooleanVar(value=s.get("rv_read_aaf", True))
        self.rv_stage = tk.BooleanVar(value=s.get("rv_gain_staging", True))
        check(ac, "Clip gain, fades and volume automation from Resolve (automation → Utility Gain)",
              self.rv_aaf).grid(row=5, column=0, columnspan=3, sticky="w", padx=14, pady=(6, 4))
        check(ac, "Tracks at -18 dB, +18 dB Utility on the Main track",
              self.rv_stage).grid(row=6, column=0, columnspan=3, sticky="w", padx=14, pady=(4, 12))

        # --- advanced ---------------------------------------------------------------
        adv = Card(page, "Advanced")
        adv.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        check(adv, "Use my own Live template instead of the built-in one", self.rv_own).grid(
            row=1, column=0, columnspan=3, sticky="w", padx=14, pady=(0, 6))
        self.rv_tpl_fr = ctk.CTkFrame(adv, fg_color="transparent")
        self.rv_tpl_fr.columnconfigure(1, weight=1)
        self._file_row(self.rv_tpl_fr, 0, "Live template", self.rv_tpl,
                       lambda: self._pick(self.rv_tpl, [("Live Set", "*.als")]), "rtpl")
        self.rv_own.trace_add("write", lambda *a: self._rv_own_state())
        self._rv_own_state()
        ctk.CTkFrame(adv, height=6, width=1, fg_color="transparent").grid(row=3, column=0)

        # --- go --------------------------------------------------------------------
        run = ctk.CTkFrame(page, fg_color="transparent")
        run.grid(row=5, column=0, sticky="ew", pady=(4, 6))
        run.columnconfigure(1, weight=1)
        self.rv_go = button(run, "Build Live Set from Resolve", self._rv_run, primary=True, width=260)
        self.rv_go.grid(row=0, column=0, sticky="w")
        self.rv_bar = ctk.CTkProgressBar(run, fg_color=FIELD, progress_color=ACCENT, height=8)
        self.rv_bar.set(0)
        self.rv_bar.grid(row=0, column=1, sticky="ew", padx=(16, 0))
        self.rv_step = label(run, "")
        self.rv_step.grid(row=1, column=1, sticky="w", padx=(16, 0))
        orow = ctk.CTkFrame(run, fg_color="transparent")
        orow.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        check(orow, "Open the new set in Live when it's done", self.rv_open).pack(side="left", padx=(0, 18))
        self.rv_folder_btn = button(orow, "Show saved set", self._rv_show_folder, width=140)
        self.rv_folder_btn.pack(side="left")
        self.rv_folder_btn.configure(state="disabled")
        self._rv_last_set = None
        self.rv_log = ctk.CTkTextbox(page, height=180, fg_color=PANEL, text_color=TEXT,
                                     border_width=1, border_color=LINE, font=ctk.CTkFont("Consolas", 12),
                                     corner_radius=8)
        self.rv_log.grid(row=6, column=0, sticky="ew", pady=(6, 4))

    # -- helpers ---------------------------------------------------------------------
    def _rv_say(self, m):
        self.rv_log.insert("end", m + "\n")
        self.rv_log.see("end")

    def _rv_hint(self):
        if self.rv_rmode.get() == HANDLES_LBL:
            t = ("Handles are the shot's own footage, graded. Resolve renders head, shot and tail as separate "
                 "clips (the head and tail from a temporary timeline that's deleted afterwards; your edit is "
                 "never changed), joined without re-encoding. Titles show inside the cut; handles are clean "
                 "footage. Retimed shots, stills and other frame rates use the edit for their handles.")
        else:
            t = "Fastest: Resolve renders one clip per shot straight from your edit. Handles show what's next to the shot."
        self.rv_mode_hint.configure(text=t)

    def _rv_own_state(self):
        if self.rv_own.get():
            self.rv_tpl_fr.grid(row=2, column=0, columnspan=3, sticky="ew")
        else:
            self.rv_tpl_fr.grid_remove()

    def _rv_whole_state(self):
        from .gui import DIM, TEXT
        whole = self.rv_whole.get() != "Handles"
        for e in (self.rv_ahead_e, self.rv_atail_e):
            e.configure(state="disabled" if whole else "normal", text_color=DIM if whole else TEXT)

    def _rv_mode_changed(self):
        if self.rv_mode.get() == ADD:
            self.rv_new_fr.grid_remove()
            self.rv_add_fr.grid(row=3, column=0, columnspan=3, sticky="ew")
        else:
            self.rv_add_fr.grid_remove()
            self.rv_new_fr.grid()
        self._rv_project_changed()

    def _rv_project_changed(self):
        from .gui import DIM, TEXT
        proj = self.rv_proj.get()
        nto = self._rv_new_tracks_only
        sets = []
        if proj and os.path.isdir(proj):
            sets = sorted((n for n in os.listdir(proj) if n.lower().endswith(".als")),
                          key=lambda n: os.path.getmtime(os.path.join(proj, n)), reverse=True)
        self.rv_base_menu.configure(values=[nto] + sets)
        if proj != getattr(self, "_rv_last_proj", None) or self.rv_base.get() not in [nto] + sets:
            self.rv_base.set(sets[0] if sets else nto)
        self._rv_last_proj = proj
        side = None
        if self.rv_mode.get() == ADD and proj and os.path.isdir(proj):
            try:
                side = importer.project_sidecar(proj, self._rv_base_path())
            except Exception:
                side = None
        for w in self.rv_lockable:
            w.configure(state="disabled" if side else "normal", text_color=DIM if side else TEXT)
        if side:
            self.rv_tempo.set(f"{side.tempo:g}")
            self.rv_sr.set(str(side.media_sample_rate))
            self.rv_lock_note.configure(text="Tempo and sample rate come from this project's earlier import.")
        else:
            self.rv_lock_note.configure(text="")

    def _rv_base_path(self):
        b = self.rv_base.get()
        return os.path.join(self.rv_proj.get(), b) if b and b != self._rv_new_tracks_only else None

    @staticmethod
    def _num(var, default):
        try:
            return float(str(var.get()).replace(",", "."))
        except ValueError:
            return default

    # -- read ----------------------------------------------------------------------
    def _rv_read(self):
        self.rv_connect.configure(state="disabled", text="Reading…")

        def work():
            try:
                rt = resolve_api.read(resolve_api.connect())
                self.after(0, self._rv_show, rt, None)
            except Exception as e:
                self.after(0, self._rv_show, None, e)
            finally:
                self.after(0, lambda: self.rv_connect.configure(state="normal", text="Read open timeline"))
        threading.Thread(target=work, daemon=True).start()

    def _rv_show(self, rt, err):
        from .gui import DIM, TEXT, check, label
        if err is not None:
            self.rt = None
            self.rv_status.configure(text=str(err), text_color="#ff9a8a")
            self.rv_detail.configure(text="")
            return
        self.rt = rt
        self._rv_res_labels(rt.width, rt.height)
        shots = resolve_live.plan_shots(rt)
        self.rv_status.configure(text=f"{rt.project}  /  {rt.name}", text_color=TEXT)
        st = seconds_to_tc(rt.sec(rt.start), rt.fps, rt.drop_frame)
        self.rv_detail.configure(
            text=f"{float(rt.fps):.3f} fps{' DF' if rt.drop_frame else ''} · {rt.width}x{rt.height} · starts {st} · "
                 f"{len(shots)} shot{'s' if len(shots) != 1 else ''} on "
                 f"{len({s.item.track_index for s in shots})} video track(s) · {len(rt.audio_tracks)} audio tracks · "
                 f"{len(rt.markers)} marker{'s' if len(rt.markers) != 1 else ''}", text_color=TEXT)
        if not self.rv_name.get() or self.rv_name.get() == getattr(self, "_rv_auto_name", None):
            self.rv_name.set(rt.name)
            self._rv_auto_name = rt.name
        for w in self.rv_list.winfo_children():
            w.destroy()
        self.rv_track_vars = []
        for idx in sorted(rt.audio_tracks):
            name = rt.audio_tracks[idx]
            n = sum(1 for i in rt.audio_items if i.track_index == idx)
            v = tk.BooleanVar(value=n > 0)
            check(self.rv_list, f"{name}    {n} clip{'s' if n != 1 else ''} · {rt.audio_kinds.get(idx, '')}", v).pack(
                anchor="w", padx=6, pady=3)
            self.rv_track_vars.append((name, v))
        if not rt.audio_tracks:
            label(self.rv_list, "No audio tracks.").pack(anchor="w", padx=6, pady=6)
        self._rv_say(f"Read {rt.project} / {rt.name}: {len(shots)} shots, {len(rt.audio_items)} audio clips")
        for w in rt.warnings:
            self._rv_say("! " + w)

    # -- build ---------------------------------------------------------------------
    def _rv_run(self):
        own = self.rv_own.get()
        if own and not os.path.isfile(self.rv_tpl.get()):
            return self._rv_say("! Pick your own Live template (.als), or untick it to use the built-in one.")
        tpl = template.resolve(self.rv_tpl.get() if own else None)
        adding = self.rv_mode.get() == ADD
        if adding and not os.path.isdir(self.rv_proj.get()):
            return self._rv_say("! Choose or drop the Live project folder to add to.")
        if not adding:
            if not self.rv_out.get():
                self.rv_out.set(DEFAULT_OUT)
            try:
                os.makedirs(self.rv_out.get(), exist_ok=True)
            except OSError as e:
                return self._rv_say(f"! Can't use that save folder: {e}")
        try:
            tempo = float(self.rv_tempo.get())
        except ValueError:
            return self._rv_say("! Tempo must be a number.")
        handles = self.rv_rmode.get() == HANDLES_LBL
        whole = self.rv_whole.get() != "Handles"
        vs = video.VideoSettings(codec=self.rv_codec.get(), frame_rate=self.rv_rate.get(),
                                 resolution=self.rv_res.get().split(" (")[0])
        self.settings["resolve_video"] = {"codec": vs.codec, "frame_rate": vs.frame_rate,
                                          "resolution": vs.resolution}
        chosen = [n for n, v in self.rv_track_vars if v.get()] if self.rv_track_vars else None
        self.settings.update(
            rv_template=self.rv_tpl.get(), rv_own_template=own, rv_open_live=self.rv_open.get(),
            tempo=tempo, sample_rate=int(self.rv_sr.get()),
            rv_video=self.rv_video.get(), rv_render_mode="handles" if handles else "full",
            rv_video_head=self._num(self.rv_vhead, 2.0), rv_video_tail=self._num(self.rv_vtail, 2.0),
            rv_shot_markers=self.rv_shotmarks.get(), rv_resolve_markers=self.rv_rmarks.get(),
            rv_mix_track=self.rv_mix.get(), rv_render_preset=self.rv_preset.get().strip(),
            rv_audio=self.rv_audio.get(),
            rv_audio_whole=whole, rv_audio_head=self._num(self.rv_ahead, 2.0),
            rv_audio_tail=self._num(self.rv_atail, 2.0), rv_normalize=self.rv_norm.get(),
            rv_normalize_db=self._num(self.rv_norm_db, -1.0), rv_read_aaf=self.rv_aaf.get(),
            rv_gain_staging=self.rv_stage.get())
        if not adding:
            self.settings["resolve_out_dir"] = self.rv_out.get()
        from .gui import save_settings
        save_settings(self.settings)
        sv = self.settings
        opt = resolve_live.Options(
            template=tpl, out_dir=self.rv_out.get(), set_name=self.rv_name.get(), tempo=tempo,
            sample_rate=int(self.rv_sr.get()), existing_project=self.rv_proj.get() if adding else None,
            base_set=self._rv_base_path() if adding else None, note=self.rv_note.get(),
            video=sv["rv_video"], render_mode=resolve_live.MODE_HANDLES if handles else resolve_live.MODE_FULL,
            video_head=sv["rv_video_head"], video_tail=sv["rv_video_tail"], video_settings=vs,
            shot_markers=sv["rv_shot_markers"], resolve_markers=sv["rv_resolve_markers"],
            mix_track=sv["rv_mix_track"], render_preset=sv["rv_render_preset"],
            audio=sv["rv_audio"], audio_tracks=chosen, audio_whole_files=whole,
            audio_head=sv["rv_audio_head"], audio_tail=sv["rv_audio_tail"], normalize=sv["rv_normalize"],
            normalize_db=sv["rv_normalize_db"], read_aaf=sv["rv_read_aaf"], gain_staging=sv["rv_gain_staging"],
            log=lambda m: self.after(0, self._rv_say, m),
            progress=lambda fr, t="": self.after(0, self._rv_prog, fr, t))
        opt_open = self.rv_open.get()
        self.rv_go.configure(state="disabled", text="Working…")
        self.rv_bar.set(0)

        def work():
            try:
                resolve = resolve_api.connect()
                rt = resolve_api.read(resolve)               # always the timeline open right now
                self.after(0, self._rv_show, rt, None)
                res = resolve_live.run(resolve, rt, opt)
                self.after(0, self._rv_say, f"✓ Live Set created: {res.als_path}")
                self.after(0, self._rv_say, f"  {len(res.warnings)} warning(s), report saved next to the set.")
                self.after(0, self._rv_project_changed)
                self._rv_last_set = res.als_path
                self.after(0, lambda: self.rv_folder_btn.configure(state="normal"))
                if opt_open:
                    self.after(0, self._rv_say, "Opening it in Live…")
                    open_file(res.als_path)
                else:
                    self.after(0, self._rv_say, f"Saved in: {os.path.dirname(res.als_path)}")
            except resolve_api.ResolveError as e:
                self.after(0, self._rv_say, f"✗ {e}")
            except Exception as e:
                self.after(0, self._rv_say, f"✗ Failed: {e}")
            finally:
                self.after(0, lambda: self.rv_go.configure(state="normal", text="Build Live Set from Resolve"))
        threading.Thread(target=work, daemon=True).start()

    def _rv_show_folder(self):
        p = self._rv_last_set
        if not p:
            return
        import subprocess
        import sys
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            open_file(os.path.dirname(p))

    def _rv_res_labels(self, w, h):
        cur = self.rv_res.get().split(" (")[0]
        self.rv_res_menu.configure(values=[res_label(r, w, h) for r in video.RESOLUTIONS])
        self.rv_res.set(res_label(cur, w, h))

    def _rv_prog(self, fr, text):
        self.rv_bar.set(max(0.0, min(1.0, fr)))
        self.rv_step.configure(text=text or "")
