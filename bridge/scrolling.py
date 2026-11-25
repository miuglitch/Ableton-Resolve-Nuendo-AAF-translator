"""Mouse wheel and middle-button scrolling for the whole window.

customtkinter's own wheel handling divides each wheel step by 6 and rounds
down, so high-resolution / free-spinning wheels (which send many tiny steps)
don't scroll at all. This replaces it with one handler that:
  - adds up every step, however small, and scrolls by whole pixels
  - scrolls the innermost list under the pointer, and hands the scroll on to
    the page when that list is already at its top or bottom
  - supports middle-button autoscroll: hold the middle button and move the
    mouse up or down; the further you move, the faster it scrolls
"""
from __future__ import annotations

import sys

PIXELS_PER_DELTA = 0.5      # Windows: one notch = 120 → 60 px
DEAD_ZONE = 6               # px around the middle-click point that doesn't scroll
AUTO_SPEED = 0.18           # autoscroll px per tick per px of mouse distance
TICK_MS = 16


class ScrollManager:
    def __init__(self, root):
        self.root = root
        self.canvases = {}          # widget -> canvas (frames and canvases themselves)
        self.acc = 0.0
        self.auto = None            # (canvas, origin_y) while middle button held
        self.saved_cursor = ""

    def register(self, scrollable):
        canvas = scrollable._parent_canvas
        canvas.configure(yscrollincrement=1)
        self.canvases[scrollable] = canvas
        self.canvases[canvas] = canvas
        return scrollable

    def install(self):
        r = self.root
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            r.unbind_all(seq)                   # drop customtkinter's handlers
        r.bind_all("<MouseWheel>", self._wheel)
        if sys.platform.startswith("linux"):
            r.bind_all("<Button-4>", lambda e: self._wheel(e, 120))
            r.bind_all("<Button-5>", lambda e: self._wheel(e, -120))
        r.bind_all("<ButtonPress-2>", self._auto_start, add="+")
        r.bind_all("<ButtonRelease-2>", self._auto_stop, add="+")

    # -- helpers ------------------------------------------------------------
    def _chain(self, x_root, y_root):
        """Scroll canvases under the pointer, innermost first."""
        w = self.root.winfo_containing(x_root, y_root)
        out = []
        while w is not None:
            c = self.canvases.get(w)
            if c is not None and c not in out:
                out.append(c)
            w = getattr(w, "master", None)
        return out

    @staticmethod
    def _can_scroll(canvas, pixels):
        top, bottom = canvas.yview()
        if top <= 0.0 and bottom >= 1.0:
            return False
        return top > 0.0 if pixels < 0 else bottom < 1.0

    def _scroll(self, pixels, chain):
        for c in chain:
            if self._can_scroll(c, pixels):
                c.yview_scroll(int(pixels), "units")
                return True
        return False

    # -- wheel ------------------------------------------------------------------
    def _wheel(self, event, delta=None):
        if isinstance(event.widget, str):       # events from non-Tk windows
            return
        d = event.delta if delta is None else delta
        if sys.platform == "darwin":
            d *= 40                              # macOS sends small deltas
        self.acc += -d * PIXELS_PER_DELTA
        whole = int(self.acc)
        if whole:
            self.acc -= whole
            self._scroll(whole, self._chain(event.x_root, event.y_root))
        return "break"

    # -- middle-button autoscroll ------------------------------------------
    def _auto_start(self, event):
        chain = [c for c in self._chain(event.x_root, event.y_root)
                 if c.yview() != (0.0, 1.0)]
        if not chain:
            return
        self.auto = (chain, event.y_root)
        self.saved_cursor = self.root.cget("cursor")
        self.root.configure(cursor="sb_v_double_arrow")
        self._auto_tick()

    def _auto_tick(self):
        if not self.auto:
            return
        chain, y0 = self.auto
        dy = self.root.winfo_pointery() - y0
        if abs(dy) > DEAD_ZONE:
            step = (abs(dy) - DEAD_ZONE) * AUTO_SPEED
            self.acc += step if dy > 0 else -step
            whole = int(self.acc)
            if whole:
                self.acc -= whole
                self._scroll(whole, chain)
        self.root.after(TICK_MS, self._auto_tick)

    def _auto_stop(self, event=None):
        if self.auto:
            self.auto = None
            self.root.configure(cursor=self.saved_cursor)
