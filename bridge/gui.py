"""Windows app: Resolve/Nuendo → Live import with a track checklist."""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, importer, video
from .timecode import seconds_to_tc

SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "MIUGLITCH", "aaf-translator.json")


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


class VideoPanel(ttk.LabelFrame):
    """Codec / frame rate / resolution / keyframe interval chooser."""

    def __init__(self, parent, title, settings, key):
        super().__init__(parent, text=title, padding=6)
        d = settings.get(key, {})
        self.settings, self.key = settings, key
        self.v_codec = tk.StringVar(value=d.get("codec", "H.264 (.mp4)"))
        self.v_rate = tk.StringVar(value=d.get("frame_rate", "Same as source"))
        self.v_res = tk.StringVar(value=d.get("resolution", "1280x720"))
        self.v_gop = tk.StringVar(value=str(d.get("keyframe_interval", 1)))
        self.v_audio = tk.BooleanVar(value=d.get("keep_audio", False))
        self.v_tc = tk.BooleanVar(value=d.get("burn_timecode", False))
        rows = [("Codec", ttk.Combobox(self, textvariable=self.v_codec, state="readonly",
                                      values=list(video.CODECS), width=26)),
                ("Frame rate", ttk.Combobox(self, textvariable=self.v_rate, state="readonly",
                                           values=video.FRAME_RATES, width=16)),
                ("Resolution", ttk.Combobox(self, textvariable=self.v_res,
                                           values=video.RESOLUTIONS, width=16)),
                ("Keyframe every (frames)", ttk.Spinbox(self, textvariable=self.v_gop,
                                                        from_=1, to=600, width=8))]
        for i, (label, w) in enumerate(rows):
            ttk.Label(self, text=label).grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 4), pady=2)
            w.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 16))
        self.gop_widget = rows[3][1]
        ttk.Checkbutton(self, text="Keep audio", variable=self.v_audio).grid(row=2, column=0, sticky="w")
        ttk.Checkbutton(self, text="Burn in timecode", variable=self.v_tc).grid(row=2, column=1, sticky="w")
        self.v_codec.trace_add("write", lambda *a: self._gop_state())
        self._gop_state()

    def _gop_state(self):
        intra_only = not video.CODECS[self.v_codec.get()][2]
        self.gop_widget.config(state="disabled" if intra_only else "normal")

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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"MIUGLITCH AAF Translator {__version__}")
        self.geometry("860x900")
        self.settings = load_settings()
        self.tl = None
        self.track_vars = []

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        imp = ttk.Frame(nb, padding=10)
        exp = ttk.Frame(nb, padding=10)
        vid = ttk.Frame(nb, padding=10)
        nb.add(imp, text="Resolve / Nuendo → Live")
        nb.add(exp, text="Live → Resolve / Nuendo")
        nb.add(vid, text="Picture transcode")
        ttk.Label(exp, text="Export is the next build step.").pack(anchor="w")
        self._build_import(imp)
        self._build_video(vid)

    # -- UI --------------------------------------------------------------
    def _row(self, parent, label, var, browse=None, r=0):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", padx=4)
        if browse:
            ttk.Button(parent, text="Browse…", command=browse).grid(row=r, column=2)

    def _build_import(self, f):
        f.columnconfigure(1, weight=1)
        self.v_in = tk.StringVar()
        self.v_tpl = tk.StringVar(value=self.settings.get("template", ""))
        self.v_out = tk.StringVar(value=self.settings.get("out_dir", ""))
        self.v_name = tk.StringVar()
        self.v_tempo = tk.StringVar(value=str(self.settings.get("tempo", 120)))
        self.v_offset = tk.StringVar()
        self.v_pic = tk.StringVar()
        self.v_pic_tc = tk.StringVar()

        self._row(f, "Timeline (AAF / FCPXML)", self.v_in, self._pick_input, 0)
        self._row(f, "Live template set", self.v_tpl,
                  lambda: self._pick(self.v_tpl, [("Live Set", "*.als")]), 1)
        self._row(f, "Save project in", self.v_out, self._pick_out, 2)
        self._row(f, "Set name", self.v_name, None, 3)
        self._row(f, "Tempo (BPM)", self.v_tempo, None, 4)
        self._row(f, "Live 0:00 = timecode", self.v_offset, None, 5)
        self._row(f, "Picture reference (optional)", self.v_pic,
                  lambda: self._pick(self.v_pic, [("Video", "*.mov *.mp4 *.m4v"), ("All", "*.*")]), 6)
        self._row(f, "Picture start TC (blank = from file)", self.v_pic_tc, None, 7)
        self.v_pic_conv = tk.BooleanVar(value=self.settings.get("picture_convert", True))
        ttk.Checkbutton(f, text="Transcode picture for Live", variable=self.v_pic_conv).grid(
            row=8, column=0, columnspan=3, sticky="w")
        self.pic_panel = VideoPanel(f, "Picture format", self.settings, "picture_video")
        self.pic_panel.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        self.info = ttk.Label(f, text="Load a timeline to see its tracks.")
        self.info.grid(row=10, column=0, columnspan=3, sticky="w", pady=(10, 2))

        box = ttk.LabelFrame(f, text="Tracks to import", padding=6)
        box.grid(row=11, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(11, weight=1)
        btns = ttk.Frame(box)
        btns.pack(fill="x")
        ttk.Button(btns, text="Select all", command=lambda: self._all(True)).pack(side="left")
        ttk.Button(btns, text="Select none", command=lambda: self._all(False)).pack(side="left", padx=4)
        canvas = tk.Canvas(box, height=150, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        self.list_frame = ttk.Frame(canvas)
        self.list_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.go = ttk.Button(f, text="Create Live Set", command=self._run)
        self.go.grid(row=12, column=0, columnspan=3, pady=8)
        self.bar = ttk.Progressbar(f, mode="determinate")
        self.bar.grid(row=13, column=0, columnspan=3, sticky="ew")
        self.log = tk.Text(f, height=7, wrap="word")
        self.log.grid(row=14, column=0, columnspan=3, sticky="nsew", pady=(6, 0))

    def _build_video(self, f):
        f.columnconfigure(1, weight=1)
        self.vv_in = tk.StringVar()
        self.vv_out = tk.StringVar(value=self.settings.get("video_out", ""))
        self._row(f, "Video file", self.vv_in,
                  lambda: self._pick(self.vv_in, [("Video", "*.mov *.mp4 *.mxf *.m4v *.avi"), ("All", "*.*")]), 0)
        self._row(f, "Save to folder", self.vv_out,
                  lambda: self.vv_out.set(filedialog.askdirectory() or self.vv_out.get()), 1)
        self.vid_panel = VideoPanel(f, "Output format", self.settings, "standalone_video")
        self.vid_panel.grid(row=2, column=0, columnspan=3, sticky="ew", pady=8)
        self.vbtn = ttk.Button(f, text="Transcode", command=self._run_video)
        self.vbtn.grid(row=3, column=0, columnspan=3)
        self.vlog = tk.Text(f, height=8, wrap="word")
        self.vlog.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=6)

    def _run_video(self):
        src, out = self.vv_in.get(), self.vv_out.get() or os.path.dirname(self.vv_in.get())
        if not os.path.isfile(src):
            messagebox.showinfo("No file", "Choose a video file.")
            return
        s = self.vid_panel.get()
        self.settings["video_out"] = out
        save_settings(self.settings)
        self.vbtn.config(state="disabled")
        say = lambda m: self.after(0, lambda: (self.vlog.insert("end", m + "\n"), self.vlog.see("end")))

        def work():
            try:
                dst = video.transcode(src, out, s, say)
                say(f"Done: {dst}")
            except Exception as e:
                say(f"Failed: {e}")
            finally:
                self.after(0, lambda: self.vbtn.config(state="normal"))

        threading.Thread(target=work, daemon=True).start()

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
        if not p:
            return
        self.v_in.set(p)
        try:
            self.tl = importer.load(p)
        except Exception as e:
            messagebox.showerror("Can't read timeline", str(e))
            return
        tl = self.tl
        self.v_name.set(tl.name)
        start_tc = seconds_to_tc(tl.start, tl.frame_rate, tl.drop_frame)
        self.v_offset.set(start_tc)
        self.info.config(text=f"{tl.name} — {float(tl.frame_rate):.3f} fps"
                              f"{' DF' if tl.drop_frame else ''}, starts {start_tc}, "
                              f"{len(tl.markers)} markers")
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.track_vars = []
        for t in tl.audio_tracks:
            v = tk.BooleanVar(value=True)
            chans = "mono" if t.channels == 1 else "stereo"
            ttk.Checkbutton(self.list_frame, variable=v,
                            text=f"{t.name}   ({len(t.clips)} clips, {chans})").pack(anchor="w")
            self.track_vars.append((t.name, v))
        for w in tl.warnings:
            self._log("! " + w)

    def _all(self, on):
        for _, v in self.track_vars:
            v.set(on)

    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")

    def _run(self):
        if not self.tl:
            messagebox.showinfo("No timeline", "Load an AAF or FCPXML first.")
            return
        if not os.path.isfile(self.v_tpl.get()):
            messagebox.showinfo("Template needed",
                                "Pick your Live template set (see README: 'Make the Live template').")
            return
        if not self.v_out.get():
            messagebox.showinfo("Folder needed", "Choose where to save the project.")
            return
        selected = [n for n, v in self.track_vars if v.get()]
        if not selected:
            messagebox.showinfo("No tracks", "Tick at least one track.")
            return
        try:
            tempo = float(self.v_tempo.get())
        except ValueError:
            messagebox.showerror("Tempo", "Tempo must be a number.")
            return
        self.settings.update(template=self.v_tpl.get(), out_dir=self.v_out.get(), tempo=tempo,
                             picture_convert=self.v_pic_conv.get())
        save_settings(self.settings)

        opt = importer.ImportOptions(
            template=self.v_tpl.get(), out_dir=self.v_out.get(), set_name=self.v_name.get(),
            tempo=tempo, offset_tc=self.v_offset.get() or None, selected=selected,
            picture=self.v_pic.get() or None, picture_tc=self.v_pic_tc.get() or None,
            picture_video=self.pic_panel.get() if self.v_pic_conv.get() else None,
            log=lambda m: self.after(0, self._log, m),
            progress=lambda i, n: self.after(0, self._progress, i, n))
        self.go.config(state="disabled")

        def work():
            try:
                res = importer.run(self.v_in.get(), opt, tl=self.tl)
                self.after(0, lambda: messagebox.showinfo(
                    "Done", f"Live Set created:\n{res.als_path}\n\n"
                            f"{len(res.warnings)} warning(s) — see the report next to the set."))
            except Exception as e:
                self.after(0, lambda e=e: messagebox.showerror("Import failed", str(e)))
            finally:
                self.after(0, lambda: self.go.config(state="normal"))
                self.tl = importer.load(self.v_in.get())   # fresh copy for another run

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, i, n):
        self.bar.config(maximum=max(n, 1), value=i + 1)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
