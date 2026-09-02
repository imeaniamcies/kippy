"""
Comic Reader - a Kivy-based comic book reader.

Supports .cbz/.zip, .cbr/.rar (requires the `rarfile` package + a system
`unrar`/`unar` binary), .pdf (requires the `PyMuPDF` package, `pip install
pymupdf`) and plain folders of images.

Features
--------
* Library / Updates / History / Browse / More tabs behind a bottom
  navigation bar, styled after modern manga-reader apps (dark theme,
  vector icons drawn on-canvas -- no emoji anywhere in the UI).
* Reader screen:
    - Smooth pinch-to-zoom / drag-to-pan (multitouch) on every page.
    - Double-tap to zoom in/out.
    - Tap left/right thirds of the screen to turn pages, tap the middle
      to show/hide the controls. Controls auto-hide after a few seconds.
    - Swipe left/right to turn pages when not zoomed in.
    - Page scrubber slider + page counter.
    - Left-to-right / right-to-left (manga) reading direction toggle.
    - Background preloading of the next couple of pages for instant
      page turns.
    - Reading progress is remembered per comic automatically, and logged
      to a reading History tab.
    - Fullscreen toggle.
    - Full keyboard support (arrow keys / space / F / Esc).

Run
---
    pip install -r requirements.txt
    python main.py
"""

import io
import os
import re
import json
import math
import datetime
import threading
from pathlib import Path

from kivy.config import Config
Config.set('kivy', 'exit_on_escape', '0')

from kivy.app import App
from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.animation import Animation
from kivy.graphics import Color, Rectangle, Line, Ellipse, Mesh
from kivy.properties import (
    BooleanProperty, NumericProperty, ObjectProperty, StringProperty, ListProperty
)
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition, NoTransition
from kivy.uix.widget import Widget
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.behaviors import ButtonBehavior

try:
    import rarfile
    RAR_SUPPORT = True
except ImportError:
    RAR_SUPPORT = False

try:
    import fitz  # PyMuPDF
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False


IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
COMIC_EXTS = {'.cbz', '.zip', '.cbr', '.rar', '.pdf'}
PDF_RENDER_ZOOM = 2.0  # ~144 DPI; raise for sharper (but heavier) pages

DATA_DIR = Path.home() / '.comic_reader'
DATA_FILE = DATA_DIR / 'data.json'

# Palette (kept in one place so every screen matches).
BG = (0.06, 0.06, 0.08, 1)
BAR_BG = (0.10, 0.10, 0.13, 1)
CARD_BG = (0.13, 0.13, 0.17, 1)
CARD_BG_DARK = (0.045, 0.045, 0.06, 1)
ACCENT = (0.42, 0.62, 1, 1)
TEXT_PRIMARY = (0.96, 0.96, 0.98, 1)
TEXT_SECONDARY = (0.6, 0.6, 0.66, 1)
TEXT_MUTED = (0.45, 0.45, 0.52, 1)


def natural_key(s):
    """Sort key so 'page2' comes before 'page10'."""
    s = str(s)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

class Store:
    """Small JSON-backed store for the library folder, reading progress,
    reading history and favorites."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = {
            'library_folder': '',
            'progress': {},
            'history': [],
            'favorites': [],
            'settings': {'compact_grid': False, 'autohide_controls': True},
        }
        if DATA_FILE.exists():
            try:
                loaded = json.loads(DATA_FILE.read_text())
                self.data.update(loaded)
                self.data.setdefault('settings', {})
                self.data['settings'].setdefault('compact_grid', False)
                self.data['settings'].setdefault('autohide_controls', True)
            except Exception:
                pass

    def save(self):
        try:
            DATA_FILE.write_text(json.dumps(self.data, indent=2))
        except Exception as e:
            print(f"Could not save data: {e}")

    def get_progress(self, comic_path):
        entry = self.data['progress'].get(str(comic_path))
        if isinstance(entry, dict):
            return entry.get('page', 0), entry.get('total', 0)
        return 0, 0

    def set_progress(self, comic_path, page, total):
        self.data['progress'][str(comic_path)] = {'page': page, 'total': total}
        self.add_history(comic_path, page, total)
        self.save()

    def add_history(self, comic_path, page, total):
        now = datetime.datetime.now()
        date_str = now.strftime('%-m/%-d/%y') if os.name != 'nt' else now.strftime('%#m/%#d/%y')
        time_str = now.strftime('%H:%M')
        entries = self.data['history']
        path_str = str(comic_path)
        for e in entries:
            if e.get('path') == path_str and e.get('date') == date_str:
                e['time'] = time_str
                e['page'] = page
                e['total'] = total
                e['ts'] = now.timestamp()
                return
        entries.insert(0, {
            'path': path_str, 'date': date_str, 'time': time_str,
            'page': page, 'total': total, 'ts': now.timestamp(),
        })
        # keep it from growing forever
        del entries[500:]

    def remove_history_entry(self, entry):
        try:
            self.data['history'].remove(entry)
            self.save()
        except ValueError:
            pass

    def clear_history(self):
        self.data['history'] = []
        self.save()

    def toggle_favorite(self, comic_path):
        favs = self.data['favorites']
        p = str(comic_path)
        if p in favs:
            favs.remove(p)
        else:
            favs.append(p)
        self.save()

    def is_favorite(self, comic_path):
        return str(comic_path) in self.data['favorites']

    def get_setting(self, key, default=None):
        return self.data.get('settings', {}).get(key, default)

    def set_setting(self, key, value):
        self.data.setdefault('settings', {})[key] = value
        self.save()

    def stats(self):
        total = len(self.data['progress'])
        finished = 0
        pages_read = 0
        for entry in self.data['progress'].values():
            if isinstance(entry, dict):
                page, tot = entry.get('page', 0), entry.get('total', 0)
                pages_read += page + 1
                if tot and page >= tot - 1:
                    finished += 1
        return total, finished, pages_read


# --------------------------------------------------------------------------- #
# Comic loading
# --------------------------------------------------------------------------- #

class ComicSource:
    """Abstracts a comic (zip/cbz, rar/cbr, or a folder) as ordered pages."""

    def __init__(self, path):
        self.path = Path(path)
        self.pages = []
        self._archive = None
        self._kind = None
        self._load_index()

    def _load_index(self):
        import zipfile
        p = self.path
        if p.is_dir():
            self._kind = 'dir'
            files = [f for f in p.iterdir() if f.suffix.lower() in IMG_EXTS]
            files.sort(key=natural_key)
            self.pages = files
        elif p.suffix.lower() in ('.cbz', '.zip'):
            self._kind = 'zip'
            self._archive = zipfile.ZipFile(p, 'r')
            names = [n for n in self._archive.namelist()
                     if Path(n).suffix.lower() in IMG_EXTS and not n.endswith('/')]
            names.sort(key=natural_key)
            self.pages = names
        elif p.suffix.lower() in ('.cbr', '.rar'):
            if not RAR_SUPPORT:
                raise ValueError("CBR/RAR support requires the 'rarfile' package "
                                  "and a system unrar/unar tool.")
            self._kind = 'rar'
            self._archive = rarfile.RarFile(p, 'r')
            names = [n for n in self._archive.namelist()
                     if Path(n).suffix.lower() in IMG_EXTS]
            names.sort(key=natural_key)
            self.pages = names
        elif p.suffix.lower() == '.pdf':
            if not PDF_SUPPORT:
                raise ValueError("PDF support requires the 'PyMuPDF' package "
                                  "(pip install pymupdf).")
            self._kind = 'pdf'
            self._archive = fitz.open(p)
            self.pages = list(range(self._archive.page_count))
        else:
            raise ValueError(f"Unsupported comic format: {p.suffix}")

    def page_count(self):
        return len(self.pages)

    def get_page_bytes(self, index):
        if index < 0 or index >= len(self.pages):
            return None
        item = self.pages[index]
        try:
            if self._kind == 'dir':
                return item.read_bytes()
            elif self._kind == 'pdf':
                page = self._archive.load_page(item)
                mat = fitz.Matrix(PDF_RENDER_ZOOM, PDF_RENDER_ZOOM)
                pix = page.get_pixmap(matrix=mat)
                return pix.tobytes('png')
            else:
                return self._archive.read(item)
        except Exception as e:
            print(f"Failed to read page {index}: {e}")
            return None

    def close(self):
        if self._archive is not None:
            try:
                self._archive.close()
            except Exception:
                pass


class PageCache:
    """Loads page bytes off the UI thread, builds textures on the UI thread."""

    def __init__(self, source, max_cache=8):
        self.source = source
        self.max_cache = max_cache
        self.textures = {}
        self._pending = set()
        self._lock = threading.Lock()

    def request(self, index, callback=None):
        tex = self.textures.get(index)
        if tex is not None:
            if callback:
                callback(index, tex)
            return
        with self._lock:
            if index in self._pending:
                return
            self._pending.add(index)
        threading.Thread(target=self._load, args=(index, callback), daemon=True).start()

    def _load(self, index, callback):
        data = self.source.get_page_bytes(index)
        Clock.schedule_once(lambda dt: self._on_loaded(index, data, callback))

    def _on_loaded(self, index, data, callback):
        with self._lock:
            self._pending.discard(index)
        if not data:
            return
        try:
            name = self.source.pages[index]
            ext = (name.suffix if isinstance(name, Path) else Path(name).suffix)
            ext = ext.lstrip('.').lower() or 'jpg'
            texture = CoreImage(io.BytesIO(data), ext=ext).texture
        except Exception as e:
            print(f"Failed to decode page {index}: {e}")
            return
        self.textures[index] = texture
        self._trim(index)
        if callback:
            callback(index, texture)

    def _trim(self, keep_near):
        if len(self.textures) <= self.max_cache:
            return
        order = sorted(self.textures.keys(), key=lambda k: abs(k - keep_near), reverse=True)
        while len(self.textures) > self.max_cache and order:
            del self.textures[order.pop(0)]


def load_cover_async(comic_path, callback):
    """Decode the first page of a comic in the background for a thumbnail."""

    def work():
        data, ext = None, 'jpg'
        try:
            src = ComicSource(comic_path)
            if src.page_count():
                data = src.get_page_bytes(0)
                name = src.pages[0]
                ext = (name.suffix if isinstance(name, Path) else Path(name).suffix)
                ext = ext.lstrip('.').lower() or 'jpg'
            src.close()
        except Exception as e:
            print(f"Cover load failed for {comic_path}: {e}")
            return
        if not data:
            return

        def make_texture(dt):
            try:
                callback(CoreImage(io.BytesIO(data), ext=ext).texture)
            except Exception as e:
                print(f"Cover decode failed: {e}")

        Clock.schedule_once(make_texture)

    threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------- #
# Vector icons -- drawn on a 24x24 grid with Kivy's canvas instructions.
# Used everywhere a symbol is needed instead of an emoji/text glyph.
# --------------------------------------------------------------------------- #

class Icon(Widget):
    """A small square widget that draws one named vector icon."""

    icon_name = StringProperty('circle')
    color = ListProperty([0.85, 0.85, 0.92, 1])
    line_width = NumericProperty(1.6)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bind(pos=self._redraw, size=self._redraw,
                  icon_name=self._redraw, color=self._redraw)
        Clock.schedule_once(self._redraw, 0)

    # -- coordinate helper: (x, y) on a 24x24 svg-like grid (y down,
    #    origin at the widget's center-ish) -> actual pixel position.
    def _p(self, cx, cy, u, x, y):
        return (cx + (x - 12) * u, cy - (y - 12) * u)

    def _redraw(self, *a):
        self.canvas.clear()
        if self.width <= 0 or self.height <= 0:
            return
        cx, cy = self.center
        u = min(self.width, self.height) / 24.0
        fn = getattr(self, f'_draw_{self.icon_name}', self._draw_circle)
        with self.canvas:
            Color(*self.color)
            fn(cx, cy, u)

    def _filled_polygon(self, pts_px):
        xs = pts_px[0::2]
        ys = pts_px[1::2]
        n = len(xs)
        if n < 3:
            return
        ccx = sum(xs) / n
        ccy = sum(ys) / n
        vertices = [ccx, ccy, 0, 0]
        for i in range(n):
            vertices += [xs[i], ys[i], 0, 0]
        indices = []
        for i in range(1, n):
            nxt = i + 1 if i + 1 <= n else 1
            indices += [0, i, nxt]
        Mesh(vertices=vertices, indices=indices, mode='triangles')

    # -- individual icons ------------------------------------------------ #

    def _draw_circle(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 8 * u), width=self.line_width)

    def _draw_search(self, cx, cy, u):
        p = self._p(cx, cy, u, 10, 10)
        Line(circle=(p[0], p[1], 6 * u), width=self.line_width)
        p1 = self._p(cx, cy, u, 14.6, 14.6)
        p2 = self._p(cx, cy, u, 20.5, 20.5)
        Line(points=[p1[0], p1[1], p2[0], p2[1]], width=self.line_width, cap='round')

    def _draw_filter(self, cx, cy, u):
        rows = [(6, 9), (12, 15), (18, 8)]
        for yy, kx in rows:
            p1 = self._p(cx, cy, u, 3, yy)
            p2 = self._p(cx, cy, u, 21, yy)
            Line(points=[p1[0], p1[1], p2[0], p2[1]], width=self.line_width, cap='round')
            kp = self._p(cx, cy, u, kx, yy)
            Line(circle=(kp[0], kp[1], 1.8 * u), width=self.line_width)

    def _draw_more_vert(self, cx, cy, u):
        for yy in (6, 12, 18):
            p = self._p(cx, cy, u, 12, yy)
            Ellipse(pos=(p[0] - 1.2 * u, p[1] - 1.2 * u), size=(2.4 * u, 2.4 * u))

    def _draw_more_horiz(self, cx, cy, u):
        for xx in (6, 12, 18):
            p = self._p(cx, cy, u, xx, 12)
            Ellipse(pos=(p[0] - 1.2 * u, p[1] - 1.2 * u), size=(2.4 * u, 2.4 * u))

    def _draw_library(self, cx, cy, u):
        for y1, y2 in [(4, 7.2), (10.4, 13.6), (16.8, 20)]:
            bl = self._p(cx, cy, u, 3, y2)
            Line(rounded_rectangle=(bl[0], bl[1], 18 * u, (y2 - y1) * u, 1.1 * u),
                 width=self.line_width)

    def _draw_updates(self, cx, cy, u):
        pts_svg = [(12, 3), (9, 5.5), (7.5, 9), (7.5, 15.5), (5.5, 17.5), (5.5, 18.5),
                   (18.5, 18.5), (18.5, 17.5), (16.5, 15.5), (16.5, 9), (14.5, 5.5), (12, 3)]
        pts = []
        for x, y in pts_svg:
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        Line(points=pts, width=self.line_width, joint='round', cap='round')
        p = self._p(cx, cy, u, 12, 20.6)
        Ellipse(pos=(p[0] - 1.1 * u, p[1] - 1.1 * u), size=(2.2 * u, 2.2 * u))

    def _draw_history(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 13)
        Line(circle=(p[0], p[1], 8 * u), width=self.line_width)
        tip1 = self._p(cx, cy, u, 12, 8)
        tip2 = self._p(cx, cy, u, 16.5, 13)
        Line(points=[p[0], p[1], tip1[0], tip1[1]], width=self.line_width, cap='round')
        Line(points=[p[0], p[1], tip2[0], tip2[1]], width=self.line_width * 0.85, cap='round')
        a1 = self._p(cx, cy, u, 4.6, 5.6)
        a2 = self._p(cx, cy, u, 7.4, 4.6)
        a3 = self._p(cx, cy, u, 6.8, 7.4)
        Line(points=[a2[0], a2[1], a1[0], a1[1], a3[0], a3[1]],
             width=self.line_width * 0.85, cap='round', joint='round')

    def _draw_browse(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 9 * u), width=self.line_width)
        n = self._p(cx, cy, u, 15.2, 8.8)
        s = self._p(cx, cy, u, 8.8, 15.2)
        Line(points=[n[0], n[1], p[0], p[1], s[0], s[1]],
             width=self.line_width, cap='round', joint='round')

    def _draw_settings(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 3.2 * u), width=self.line_width)
        for i in range(8):
            ang = math.radians(i * 45)
            x1, y1 = 12 + 5.6 * math.sin(ang), 12 - 5.6 * math.cos(ang)
            x2, y2 = 12 + 8.4 * math.sin(ang), 12 - 8.4 * math.cos(ang)
            pp1 = self._p(cx, cy, u, x1, y1)
            pp2 = self._p(cx, cy, u, x2, y2)
            Line(points=[pp1[0], pp1[1], pp2[0], pp2[1]], width=self.line_width * 1.4, cap='round')

    def _draw_info(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 9 * u), width=self.line_width)
        dot = self._p(cx, cy, u, 12, 7.4)
        Ellipse(pos=(dot[0] - 1 * u, dot[1] - 1 * u), size=(2 * u, 2 * u))
        b = self._p(cx, cy, u, 12, 10.8)
        t = self._p(cx, cy, u, 12, 17)
        Line(points=[b[0], b[1], t[0], t[1]], width=self.line_width * 1.3, cap='round')

    def _draw_help(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 9 * u), width=self.line_width)
        pts_svg = [(9.4, 9), (9.6, 7.1), (11, 5.9), (13, 5.9), (14.4, 7.1),
                   (14.4, 9), (13, 10.4), (12, 11.7), (12, 13.5)]
        pts = []
        for x, y in pts_svg:
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        Line(points=pts, width=self.line_width, cap='round', joint='round')
        dot = self._p(cx, cy, u, 12, 16.7)
        Ellipse(pos=(dot[0] - 1 * u, dot[1] - 1 * u), size=(2 * u, 2 * u))

    def _draw_support(self, cx, cy, u):
        self._draw_heart(cx, cy, u)
        p = self._p(cx, cy, u, 12, 20.5)
        Ellipse(pos=(p[0] - 0.9 * u, p[1] - 0.9 * u), size=(1.8 * u, 1.8 * u))

    def _draw_download(self, cx, cy, u):
        top = self._p(cx, cy, u, 12, 4)
        mid = self._p(cx, cy, u, 12, 14)
        Line(points=[top[0], top[1], mid[0], mid[1]], width=self.line_width, cap='round')
        l = self._p(cx, cy, u, 8, 10.5)
        r = self._p(cx, cy, u, 16, 10.5)
        Line(points=[l[0], l[1], mid[0], mid[1], r[0], r[1]],
             width=self.line_width, cap='round', joint='round')
        bl = self._p(cx, cy, u, 5, 19)
        br = self._p(cx, cy, u, 19, 19)
        Line(points=[bl[0], bl[1], br[0], br[1]], width=self.line_width, cap='round')

    def _draw_category(self, cx, cy, u):
        pts_svg = [(4, 6), (13, 6), (20, 13), (13, 20), (4, 11), (4, 6)]
        pts = []
        for x, y in pts_svg:
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        Line(points=pts, width=self.line_width, joint='round')
        dot = self._p(cx, cy, u, 8, 9.5)
        Ellipse(pos=(dot[0] - 1 * u, dot[1] - 1 * u), size=(2 * u, 2 * u))

    def _draw_stats(self, cx, cy, u):
        base = self._p(cx, cy, u, 4, 20)
        rt = self._p(cx, cy, u, 20, 20)
        Line(points=[base[0], base[1], rt[0], rt[1]], width=self.line_width, cap='round')
        for x, h in [(7, 6), (12, 10), (17, 4)]:
            b = self._p(cx, cy, u, x, 20)
            t = self._p(cx, cy, u, x, 20 - h)
            Line(points=[b[0], b[1], t[0], t[1]], width=self.line_width * 1.6, cap='round')

    def _draw_storage(self, cx, cy, u):
        for y1, y2 in [(4, 9), (10, 15), (16, 21)]:
            bl = self._p(cx, cy, u, 3, y2)
            Line(rounded_rectangle=(bl[0], bl[1], 18 * u, (y2 - y1) * u, 1 * u),
                 width=self.line_width)
            dot = self._p(cx, cy, u, 18, (y1 + y2) / 2)
            Ellipse(pos=(dot[0] - 0.7 * u, dot[1] - 0.7 * u), size=(1.4 * u, 1.4 * u))

    def _heart_points_svg(self):
        return [(12, 19), (4, 12.5), (4, 7.5), (7.2, 4.5), (10.2, 5.6),
                (12, 8), (13.8, 5.6), (16.8, 4.5), (20, 7.5), (20, 12.5), (12, 19)]

    def _draw_heart(self, cx, cy, u):
        pts = []
        for x, y in self._heart_points_svg():
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        Line(points=pts, width=self.line_width, joint='round', cap='round')

    def _draw_heart_filled(self, cx, cy, u):
        pts = []
        for x, y in self._heart_points_svg():
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        self._filled_polygon(pts)

    def _draw_trash(self, cx, cy, u):
        tl = self._p(cx, cy, u, 5, 7)
        tr = self._p(cx, cy, u, 19, 7)
        Line(points=[tl[0], tl[1], tr[0], tr[1]], width=self.line_width, cap='round')
        l1 = self._p(cx, cy, u, 9, 7)
        l2 = self._p(cx, cy, u, 9, 4.5)
        r1 = self._p(cx, cy, u, 15, 7)
        r2 = self._p(cx, cy, u, 15, 4.5)
        Line(points=[l1[0], l1[1], l2[0], l2[1]], width=self.line_width, cap='round')
        Line(points=[l2[0], l2[1], r2[0], r2[1]], width=self.line_width, cap='round')
        Line(points=[r2[0], r2[1], r1[0], r1[1]], width=self.line_width, cap='round')
        bl = self._p(cx, cy, u, 6.5, 7)
        bb = self._p(cx, cy, u, 7.3, 20)
        bc = self._p(cx, cy, u, 16.7, 20)
        br = self._p(cx, cy, u, 17.5, 7)
        Line(points=[bl[0], bl[1], bb[0], bb[1], bc[0], bc[1], br[0], br[1]],
             width=self.line_width, joint='round', cap='round')
        for x in (9.5, 12, 14.5):
            a = self._p(cx, cy, u, x, 10)
            b = self._p(cx, cy, u, x, 17)
            Line(points=[a[0], a[1], b[0], b[1]], width=self.line_width * 0.75, cap='round')

    def _draw_back(self, cx, cy, u):
        a = self._p(cx, cy, u, 15, 5)
        b = self._p(cx, cy, u, 8, 12)
        c = self._p(cx, cy, u, 15, 19)
        Line(points=[a[0], a[1], b[0], b[1], c[0], c[1]],
             width=self.line_width, joint='round', cap='round')

    def _draw_fullscreen(self, cx, cy, u):
        corners = [
            ((3, 3), (3, 8)), ((3, 3), (8, 3)),
            ((21, 3), (21, 8)), ((21, 3), (16, 3)),
            ((3, 21), (3, 16)), ((3, 21), (8, 21)),
            ((21, 21), (21, 16)), ((21, 21), (16, 21)),
        ]
        for (x1, y1), (x2, y2) in corners:
            p1 = self._p(cx, cy, u, x1, y1)
            p2 = self._p(cx, cy, u, x2, y2)
            Line(points=[p1[0], p1[1], p2[0], p2[1]], width=self.line_width, cap='round')

    def _draw_close(self, cx, cy, u):
        a = self._p(cx, cy, u, 5, 5)
        b = self._p(cx, cy, u, 19, 19)
        c = self._p(cx, cy, u, 19, 5)
        d = self._p(cx, cy, u, 5, 19)
        Line(points=[a[0], a[1], b[0], b[1]], width=self.line_width, cap='round')
        Line(points=[c[0], c[1], d[0], d[1]], width=self.line_width, cap='round')

    def _draw_folder(self, cx, cy, u):
        pts_svg = [(3, 7), (3, 19), (21, 19), (21, 9), (11, 9), (9, 6.5), (3, 6.5), (3, 7)]
        pts = []
        for x, y in pts_svg:
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        Line(points=pts, width=self.line_width, joint='round')

    def _draw_refresh(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 7 * u, 15, 300), width=self.line_width)
        tip = self._p(cx, cy, u, 17.6, 6.2)
        a1 = self._p(cx, cy, u, 19.6, 9.2)
        a2 = self._p(cx, cy, u, 15.6, 9.2)
        Line(points=[a1[0], a1[1], tip[0], tip[1], a2[0], a2[1]],
             width=self.line_width, cap='round', joint='round')

    def _draw_direction(self, cx, cy, u):
        top1 = self._p(cx, cy, u, 4, 8)
        top2 = self._p(cx, cy, u, 20, 8)
        Line(points=[top1[0], top1[1], top2[0], top2[1]], width=self.line_width, cap='round')
        tip1a = self._p(cx, cy, u, 16.5, 4.8)
        tip1b = self._p(cx, cy, u, 20, 8)
        tip1c = self._p(cx, cy, u, 16.5, 11.2)
        Line(points=[tip1a[0], tip1a[1], tip1b[0], tip1b[1], tip1c[0], tip1c[1]],
             width=self.line_width, cap='round', joint='round')
        bot1 = self._p(cx, cy, u, 20, 16)
        bot2 = self._p(cx, cy, u, 4, 16)
        Line(points=[bot1[0], bot1[1], bot2[0], bot2[1]], width=self.line_width, cap='round')
        tip2a = self._p(cx, cy, u, 7.5, 12.8)
        tip2b = self._p(cx, cy, u, 4, 16)
        tip2c = self._p(cx, cy, u, 7.5, 19.2)
        Line(points=[tip2a[0], tip2a[1], tip2b[0], tip2b[1], tip2c[0], tip2c[1]],
             width=self.line_width, cap='round', joint='round')

    def _draw_empty(self, cx, cy, u):
        p = self._p(cx, cy, u, 12, 12)
        Line(circle=(p[0], p[1], 10 * u), width=self.line_width)
        for ex in (8.3, 15.7):
            e = self._p(cx, cy, u, ex, 10)
            Ellipse(pos=(e[0] - 0.9 * u, e[1] - 0.9 * u), size=(1.8 * u, 1.8 * u))
        pts_svg = [(8, 16.4), (10, 14.6), (14, 14.6), (16, 16.4)]
        pts = []
        for x, y in pts_svg:
            pp = self._p(cx, cy, u, x, y)
            pts += [pp[0], pp[1]]
        Line(points=pts, width=self.line_width, joint='round', cap='round')

    def _draw_app_logo(self, cx, cy, u):
        l = [(12, 5), (3, 7), (3, 19), (12, 17), (12, 5)]
        r = [(12, 5), (21, 7), (21, 19), (12, 17), (12, 5)]
        for pts_svg in (l, r):
            pts = []
            for x, y in pts_svg:
                pp = self._p(cx, cy, u, x, y)
                pts += [pp[0], pp[1]]
            Line(points=pts, width=self.line_width, joint='round')
        top = self._p(cx, cy, u, 12, 5)
        bot = self._p(cx, cy, u, 12, 17)
        Line(points=[top[0], top[1], bot[0], bot[1]], width=self.line_width)


class IconButton(ButtonBehavior, Widget):
    """A tappable icon-only button used in app bars."""
    icon_name = StringProperty('circle')
    color = ListProperty([0.85, 0.85, 0.92, 1])
    icon_size = NumericProperty(22)


Builder.load_string('''
<IconButton>:
    size_hint: None, None
    size: dp(44), dp(44)
    Icon:
        icon_name: root.icon_name
        color: root.color
        size_hint: None, None
        size: dp(root.icon_size), dp(root.icon_size)
        pos: root.center_x - self.width / 2, root.center_y - self.height / 2
''')


# --------------------------------------------------------------------------- #
# Zoomable / pannable page viewer with gesture-based page turning
# --------------------------------------------------------------------------- #

class PageViewer(Widget):
    texture = ObjectProperty(None, allownone=True)
    scale = NumericProperty(1.0)
    offset_x = NumericProperty(0.0)
    offset_y = NumericProperty(0.0)
    min_scale = NumericProperty(1.0)
    max_scale = NumericProperty(4.0)

    __events__ = ('on_prev', 'on_next', 'on_toggle_overlay')

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._touches = {}
        self._pinch_start_scale = 1.0
        self._pinch_start_dist = 1.0
        with self.canvas:
            Color(0.03, 0.03, 0.04, 1)
            self._bg_rect = Rectangle(pos=self.pos, size=self.size)
            Color(1, 1, 1, 1)
            self._img_rect = Rectangle(pos=(0, 0), size=(0, 0))
        self.bind(pos=self._redraw, size=self._redraw, texture=self._on_texture,
                  scale=self._redraw, offset_x=self._redraw, offset_y=self._redraw)

    def set_texture(self, texture):
        self.texture = texture

    def reset_zoom(self, *a):
        self.scale = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0

    def _on_texture(self, *a):
        self._img_rect.texture = self.texture
        self._redraw()

    def _fit_size(self):
        if not self.texture or self.width <= 0 or self.height <= 0:
            return (0, 0)
        tw, th = self.texture.size
        if not tw or not th:
            return (0, 0)
        ratio = min(self.width / tw, self.height / th)
        return (tw * ratio, th * ratio)

    def _clamp_offsets(self):
        fw, fh = self._fit_size()
        dw, dh = fw * self.scale, fh * self.scale
        max_ox = max(0.0, (dw - self.width) / 2)
        max_oy = max(0.0, (dh - self.height) / 2)
        self.offset_x = max(-max_ox, min(max_ox, self.offset_x))
        self.offset_y = max(-max_oy, min(max_oy, self.offset_y))

    def _redraw(self, *a):
        self._bg_rect.pos = self.pos
        self._bg_rect.size = self.size
        if not self.texture:
            self._img_rect.size = (0, 0)
            return
        self._clamp_offsets()
        fw, fh = self._fit_size()
        dw, dh = fw * self.scale, fh * self.scale
        cx = self.center_x + self.offset_x
        cy = self.center_y + self.offset_y
        self._img_rect.size = (dw, dh)
        self._img_rect.pos = (cx - dw / 2, cy - dh / 2)

    # -- touch handling -------------------------------------------------- #

    def on_touch_down(self, touch):
        if not self.collide_point(touch.x, touch.y):
            return super().on_touch_down(touch)
        touch.grab(self)
        self._touches[touch.uid] = {
            'start': (touch.x, touch.y), 'last': (touch.x, touch.y),
            'time': Clock.get_time(), 'moved': False,
        }
        if len(self._touches) == 2:
            uids = list(self._touches.keys())
            x1, y1 = self._touches[uids[0]]['last']
            x2, y2 = self._touches[uids[1]]['last']
            self._pinch_start_dist = max(10.0, ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5)
            self._pinch_start_scale = self.scale
        return True

    def on_touch_move(self, touch):
        if touch.uid not in self._touches:
            return super().on_touch_move(touch)
        info = self._touches[touch.uid]
        dx = touch.x - info['last'][0]
        dy = touch.y - info['last'][1]
        info['last'] = (touch.x, touch.y)
        if abs(touch.x - info['start'][0]) > dp(6) or abs(touch.y - info['start'][1]) > dp(6):
            info['moved'] = True

        if len(self._touches) >= 2:
            uids = list(self._touches.keys())[:2]
            x1, y1 = self._touches[uids[0]]['last']
            x2, y2 = self._touches[uids[1]]['last']
            dist = max(10.0, ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5)
            new_scale = self._pinch_start_scale * (dist / self._pinch_start_dist)
            self.scale = max(self.min_scale, min(self.max_scale, new_scale))
        elif len(self._touches) == 1 and self.scale > 1.0:
            self.offset_x += dx
            self.offset_y += dy
        return True

    def on_touch_up(self, touch):
        if touch.uid not in self._touches:
            return super().on_touch_up(touch)
        info = self._touches.pop(touch.uid)
        touch.ungrab(self)
        if len(self._touches) != 0:
            return True

        duration = Clock.get_time() - info['time']
        dx = touch.x - info['start'][0]
        dy = touch.y - info['start'][1]

        if not info['moved'] or (abs(dx) < dp(10) and abs(dy) < dp(10)):
            if touch.is_double_tap:
                if self.scale > 1.01:
                    self.reset_zoom()
                else:
                    self.scale = 2.2
            else:
                frac = (touch.x - self.x) / max(1.0, self.width)
                if frac < 0.32:
                    self.dispatch('on_prev')
                elif frac > 0.68:
                    self.dispatch('on_next')
                else:
                    self.dispatch('on_toggle_overlay')
        elif self.scale <= 1.01 and abs(dx) > dp(60) and abs(dx) > abs(dy) * 1.5 and duration < 0.6:
            if dx < 0:
                self.dispatch('on_next')
            else:
                self.dispatch('on_prev')
        return True

    def on_prev(self, *a):
        pass

    def on_next(self, *a):
        pass

    def on_toggle_overlay(self, *a):
        pass


# --------------------------------------------------------------------------- #
# Shared popup helpers
# --------------------------------------------------------------------------- #

def show_message(title, message):
    content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(14))
    from kivy.uix.label import Label
    lbl = Label(text=message, color=TEXT_PRIMARY, halign='left', valign='top')
    lbl.bind(size=lambda w, s: setattr(w, 'text_size', s))
    content.add_widget(lbl)
    btn = Button(text='OK', size_hint_y=None, height=dp(44))
    content.add_widget(btn)
    popup = Popup(title=title, content=content, size_hint=(0.85, 0.6))
    btn.bind(on_release=lambda *a: popup.dismiss())
    popup.open()


def show_confirm(title, message, on_confirm):
    content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(14))
    from kivy.uix.label import Label
    lbl = Label(text=message, color=TEXT_PRIMARY, halign='left', valign='top')
    lbl.bind(size=lambda w, s: setattr(w, 'text_size', s))
    content.add_widget(lbl)
    row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
    cancel = Button(text='Cancel')
    confirm = Button(text='Confirm')
    row.add_widget(cancel)
    row.add_widget(confirm)
    content.add_widget(row)
    popup = Popup(title=title, content=content, size_hint=(0.85, 0.6))
    cancel.bind(on_release=lambda *a: popup.dismiss())

    def do_confirm(*a):
        popup.dismiss()
        on_confirm()

    confirm.bind(on_release=do_confirm)
    popup.open()


def folder_picker_popup(title, start_path, on_choose):
    content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
    if not Path(start_path).exists():
        start_path = str(Path.home())
    chooser = FileChooserListView(path=start_path, dirselect=True)
    content.add_widget(chooser)
    btns = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
    popup = Popup(title=title, content=content, size_hint=(0.85, 0.85))

    def choose(*a):
        folder = chooser.selection[0] if chooser.selection else chooser.path
        popup.dismiss()
        on_choose(folder)

    btns.add_widget(Button(text='Cancel', on_release=lambda *a: popup.dismiss()))
    btns.add_widget(Button(text='Select', on_release=choose))
    content.add_widget(btns)
    popup.open()


# --------------------------------------------------------------------------- #
# Library grid + tile
# --------------------------------------------------------------------------- #

class ComicTile(ButtonBehavior, BoxLayout):
    title_text = StringProperty('')
    progress_text = StringProperty('New')
    cover_texture = ObjectProperty(None, allownone=True)

    def __init__(self, comic_path, store, **kwargs):
        super().__init__(**kwargs)
        self.comic_path = comic_path
        self.store = store
        self.title_text = Path(comic_path).stem.replace('_', ' ').replace('.', ' ')
        self.refresh_progress()
        load_cover_async(comic_path, self._set_cover)

    def _set_cover(self, texture):
        self.cover_texture = texture

    def refresh_progress(self):
        page, total = self.store.get_progress(self.comic_path)
        if not total:
            self.progress_text = 'New'
        elif page >= total - 1:
            self.progress_text = f'Finished \u2022 {total}p'
        else:
            self.progress_text = f'Page {page + 1} / {total}'


class ComicGrid(BoxLayout):
    """Reusable scrollable grid of ComicTile widgets, plus an empty state."""

    empty_icon = StringProperty('empty')
    empty_title = StringProperty('Nothing here yet')
    empty_action_text = StringProperty('')

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tiles = {}
        self.on_empty_action = lambda: None

    def clear(self):
        self.ids.grid.clear_widgets()
        self._tiles.clear()

    def set_status(self, text):
        self.ids.status_label.text = text
        self.ids.status_label.opacity = 1 if text else 0

    def show_empty(self, show):
        self.ids.empty_box.opacity = 1 if show else 0
        self.ids.empty_box.disabled = not show
        self.ids.scroller.opacity = 0 if show else 1
        self.ids.scroller.disabled = show

    def populate(self, comic_paths, store, on_open):
        self.clear()
        for path in comic_paths:
            tile = ComicTile(path, store)
            tile.bind(on_release=lambda inst: on_open(inst.comic_path))
            self.ids.grid.add_widget(tile)
            self._tiles[path] = tile
        self.show_empty(len(comic_paths) == 0)

    def refresh_tile_progress(self):
        for tile in self._tiles.values():
            tile.refresh_progress()


Builder.load_string('''
<ComicTile>:
    orientation: 'vertical'
    size_hint: None, None
    size: dp(170), dp(266)
    padding: dp(6)
    spacing: dp(4)
    canvas.before:
        Color:
            rgba: 0.13, 0.13, 0.17, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(10)]
    FloatLayout:
        canvas.before:
            Color:
                rgba: 0.045, 0.045, 0.06, 1
            RoundedRectangle:
                pos: self.pos
                size: self.size
                radius: [dp(8)]
        Image:
            texture: root.cover_texture
            allow_stretch: True
            keep_ratio: True
            pos_hint: {'center_x': 0.5, 'center_y': 0.5}
            size_hint: 0.96, 0.96
    Label:
        text: root.title_text
        size_hint_y: None
        height: dp(36)
        font_size: '13sp'
        color: 0.92, 0.92, 0.95, 1
        shorten: True
        shorten_from: 'right'
        halign: 'center'
        valign: 'middle'
        text_size: self.size
    Label:
        text: root.progress_text
        size_hint_y: None
        height: dp(18)
        font_size: '11sp'
        color: 0.45, 0.72, 1, 1

<ComicGrid>:
    orientation: 'vertical'
    Label:
        id: status_label
        text: ''
        opacity: 0
        size_hint_y: None
        height: dp(40) if self.opacity else 0
        color: 0.6, 0.6, 0.66, 1
    AnchorLayout:
        id: empty_box
        opacity: 0
        disabled: True
        size_hint_y: None if self.opacity else 0
        height: self.parent.height if self.opacity else 0
        BoxLayout:
            orientation: 'vertical'
            size_hint: None, None
            size: dp(280), dp(160)
            spacing: dp(10)
            Icon:
                icon_name: root.empty_icon
                color: 0.5, 0.5, 0.58, 1
                size_hint: None, None
                size: dp(56), dp(56)
                pos_hint: {'center_x': 0.5}
            Label:
                text: root.empty_title
                color: 0.75, 0.75, 0.8, 1
                font_size: '14sp'
            Button:
                text: root.empty_action_text
                opacity: 1 if root.empty_action_text else 0
                disabled: not root.empty_action_text
                size_hint: None, None
                size: dp(200), dp(36)
                pos_hint: {'center_x': 0.5}
                background_color: 0, 0, 0, 0
                color: 0.42, 0.62, 1, 1
                on_release: root.on_empty_action()
    ScrollView:
        id: scroller
        do_scroll_x: False
        bar_width: dp(6)
        GridLayout:
            id: grid
            cols: max(1, int(self.width // dp(190)))
            spacing: dp(14)
            padding: dp(16)
            size_hint_y: None
            height: self.minimum_height
''')


# --------------------------------------------------------------------------- #
# Top app bar (title + up to 3 icon actions), shared by every tab
# --------------------------------------------------------------------------- #

class AppBar(BoxLayout):
    title_text = StringProperty('')


Builder.load_string('''
<AppBar>:
    size_hint_y: None
    height: dp(58)
    padding: dp(14), dp(8)
    spacing: dp(6)
    canvas.before:
        Color:
            rgba: 0.10, 0.10, 0.13, 1
        Rectangle:
            pos: self.pos
            size: self.size
    Label:
        text: root.title_text
        font_size: '20sp'
        bold: True
        color: 0.96, 0.96, 0.98, 1
        halign: 'left'
        valign: 'middle'
        text_size: self.size
''')


# --------------------------------------------------------------------------- #
# Bottom navigation bar
# --------------------------------------------------------------------------- #

class NavItem(ButtonBehavior, BoxLayout):
    icon_name = StringProperty('circle')
    label_text = StringProperty('')
    active = BooleanProperty(False)


Builder.load_string('''
<NavItem>:
    orientation: 'vertical'
    padding: 0, dp(6)
    Icon:
        icon_name: root.icon_name
        color: (0.42, 0.62, 1, 1) if root.active else (0.55, 0.55, 0.62, 1)
        size_hint: None, None
        size: dp(24), dp(24)
        pos_hint: {'center_x': 0.5}
    Label:
        text: root.label_text
        font_size: '11sp'
        color: (0.42, 0.62, 1, 1) if root.active else (0.55, 0.55, 0.62, 1)
        size_hint_y: None
        height: dp(16)
''')


class BottomNav(BoxLayout):
    active_tab = StringProperty('library')

    TABS = [
        ('library', 'library', 'Library'),
        ('updates', 'updates', 'Updates'),
        ('history', 'history', 'History'),
        ('browse', 'browse', 'Browse'),
        ('more', 'more_horiz', 'More'),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.on_tab = lambda name: None
        for key, icon_name, label in self.TABS:
            item = NavItem(icon_name=icon_name, label_text=label, active=(key == self.active_tab))
            item.bind(on_release=lambda inst, k=key: self._pick(k))
            self.add_widget(item)

    def _pick(self, key):
        self.active_tab = key
        self.on_tab(key)

    def on_active_tab(self, *a):
        for item in self.children:
            item.active = (item.label_text.lower() == self.active_tab
                            or (item.label_text == 'More' and self.active_tab == 'more'))


Builder.load_string('''
<BottomNav>:
    size_hint_y: None
    height: dp(64)
    canvas.before:
        Color:
            rgba: 0.10, 0.10, 0.13, 1
        Rectangle:
            pos: self.pos
            size: self.size
        Color:
            rgba: 0.2, 0.2, 0.24, 1
        Line:
            points: [self.x, self.top, self.right, self.top]
            width: 1
''')


# --------------------------------------------------------------------------- #
# Library tab
# --------------------------------------------------------------------------- #

class LibraryTab(BoxLayout):

    def on_kv_post(self, base_widget):
        self.ids.grid_holder.empty_action_text = 'Open Folder'
        self.ids.grid_holder.empty_title = 'Your library is empty'
        self.ids.grid_holder.on_empty_action = self.choose_folder
        Clock.schedule_once(lambda dt: self.rescan(), 0)

    def choose_folder(self):
        store = App.get_running_app().store
        start = store.data.get('library_folder') or str(Path.home())

        def chosen(folder):
            store.data['library_folder'] = folder
            store.save()
            self.rescan()

        folder_picker_popup('Choose your comics folder', start, chosen)

    def show_menu(self, anchor):
        content = BoxLayout(orientation='vertical', spacing=dp(4), padding=dp(6))
        popup = Popup(title='Library', content=content, size_hint=(0.6, 0.36))

        def action(fn):
            def run(*a):
                popup.dismiss()
                fn()
            return run

        for label, fn in [
            ('Open Folder\u2026', self.choose_folder),
            ('Refresh', self.rescan),
            ('About', lambda: show_message(
                'About', 'Comic Reader\n\nA local .cbz / .cbr / .pdf / folder comic reader.')),
        ]:
            b = Button(text=label, size_hint_y=None, height=dp(44))
            b.bind(on_release=action(fn))
            content.add_widget(b)
        popup.open()

    def rescan(self):
        folder = App.get_running_app().store.data.get('library_folder')
        grid = self.ids.grid_holder
        grid.clear()
        if not folder or not Path(folder).exists():
            grid.show_empty(True)
            grid.empty_title = 'Your library is empty'
            grid.empty_action_text = 'Open Folder'
            return
        grid.set_status('Scanning for comics\u2026')
        threading.Thread(target=self._scan_thread, args=(folder,), daemon=True).start()

    def _scan_thread(self, folder):
        found = []
        try:
            for root, dirs, files in os.walk(folder):
                comic_files = [f for f in files if Path(f).suffix.lower() in COMIC_EXTS]
                for f in comic_files:
                    found.append(str(Path(root) / f))
                image_files = [f for f in files if Path(f).suffix.lower() in IMG_EXTS]
                if image_files and not comic_files and str(Path(root)) != str(Path(folder)):
                    found.append(root)
                    dirs[:] = []
        except Exception as e:
            print(f"Scan error: {e}")
        found.sort(key=natural_key)
        Clock.schedule_once(lambda dt: self._populate(found))

    def _populate(self, found):
        grid = self.ids.grid_holder
        grid.set_status('')
        store = App.get_running_app().store
        grid.empty_title = 'No comics found in this folder'
        grid.empty_action_text = 'Open Folder'
        grid.populate(found, store, lambda path: App.get_running_app().open_comic(path))

    def refresh_tile_progress(self):
        self.ids.grid_holder.refresh_tile_progress()


Builder.load_string('''
<LibraryTab>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.06, 0.06, 0.08, 1
        Rectangle:
            pos: self.pos
            size: self.size
    AppBar:
        title_text: 'My Comics'
        IconButton:
            icon_name: 'search'
            on_release: app.root.get_screen('main').show_message_soon()
        IconButton:
            icon_name: 'filter'
        IconButton:
            icon_name: 'more_vert'
            on_release: root.show_menu(self)
    ComicGrid:
        id: grid_holder
''')


# --------------------------------------------------------------------------- #
# Updates tab -- comics sorted by most-recently-modified on disk
# --------------------------------------------------------------------------- #

class UpdatesTab(BoxLayout):

    def on_kv_post(self, base_widget):
        Clock.schedule_once(lambda dt: self.refresh(), 0)
        self.ids.grid_holder.empty_title = 'No recent updates'

    def refresh(self):
        folder = App.get_running_app().store.data.get('library_folder')
        grid = self.ids.grid_holder
        grid.clear()
        if not folder or not Path(folder).exists():
            grid.show_empty(True)
            return
        grid.set_status('Checking for updates\u2026')
        threading.Thread(target=self._scan_thread, args=(folder,), daemon=True).start()

    def _scan_thread(self, folder):
        found = []
        try:
            for root, dirs, files in os.walk(folder):
                comic_files = [f for f in files if Path(f).suffix.lower() in COMIC_EXTS]
                for f in comic_files:
                    p = Path(root) / f
                    try:
                        found.append((p.stat().st_mtime, str(p)))
                    except OSError:
                        pass
                image_files = [f for f in files if Path(f).suffix.lower() in IMG_EXTS]
                if image_files and not comic_files and str(Path(root)) != str(Path(folder)):
                    try:
                        found.append((Path(root).stat().st_mtime, root))
                    except OSError:
                        pass
                    dirs[:] = []
        except Exception as e:
            print(f"Scan error: {e}")
        found.sort(key=lambda t: t[0], reverse=True)
        paths = [p for _, p in found[:40]]
        Clock.schedule_once(lambda dt: self._populate(paths))

    def _populate(self, paths):
        grid = self.ids.grid_holder
        grid.set_status('')
        store = App.get_running_app().store
        grid.populate(paths, store, lambda path: App.get_running_app().open_comic(path))


Builder.load_string('''
<UpdatesTab>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.06, 0.06, 0.08, 1
        Rectangle:
            pos: self.pos
            size: self.size
    AppBar:
        title_text: 'Updates'
        IconButton:
            icon_name: 'refresh'
            on_release: root.refresh()
    ComicGrid:
        id: grid_holder
        empty_icon: 'empty'
''')


# --------------------------------------------------------------------------- #
# History tab
# --------------------------------------------------------------------------- #

class HistoryRow(BoxLayout):
    title_text = StringProperty('')
    subtitle_text = StringProperty('')
    cover_texture = ObjectProperty(None, allownone=True)
    is_favorite = BooleanProperty(False)


Builder.load_string('''
<HistoryRow>:
    size_hint_y: None
    height: dp(72)
    padding: dp(10), dp(8)
    spacing: dp(12)
    canvas.before:
        Color:
            rgba: 0.045, 0.045, 0.06, 1
        RoundedRectangle:
            pos: self.x, self.y + dp(2)
            size: self.width - dp(52), dp(56)
            radius: [dp(6)]
    FloatLayout:
        size_hint_x: None
        width: dp(50)
        Image:
            texture: root.cover_texture
            allow_stretch: True
            keep_ratio: True
            size_hint: 0.94, 0.94
            pos_hint: {'center_x': 0.5, 'center_y': 0.5}
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: root.title_text
            font_size: '14sp'
            color: 0.92, 0.92, 0.95, 1
            halign: 'left'
            valign: 'middle'
            text_size: self.size
            shorten: True
        Label:
            text: root.subtitle_text
            font_size: '12sp'
            color: 0.6, 0.6, 0.66, 1
            halign: 'left'
            valign: 'middle'
            text_size: self.size
    IconButton:
        id: heart_btn
        icon_name: 'heart_filled' if root.is_favorite else 'heart'
        color: (0.95, 0.35, 0.45, 1) if root.is_favorite else (0.55, 0.55, 0.62, 1)
        icon_size: 20
    IconButton:
        icon_name: 'trash'
        color: 0.55, 0.55, 0.62, 1
        icon_size: 20
''')


class HistoryTab(BoxLayout):

    def on_kv_post(self, base_widget):
        Clock.schedule_once(lambda dt: self.refresh(), 0)

    def refresh(self):
        store = App.get_running_app().store
        entries = store.data.get('history', [])
        holder = self.ids.list_holder
        holder.clear_widgets()
        if not entries:
            self.ids.empty_box.opacity = 1
            self.ids.empty_box.disabled = False
            self.ids.scroller.opacity = 0
            return
        self.ids.empty_box.opacity = 0
        self.ids.empty_box.disabled = True
        self.ids.scroller.opacity = 1

        from kivy.uix.label import Label
        last_date = None
        for entry in entries:
            if entry.get('date') != last_date:
                last_date = entry.get('date')
                dl = Label(text=last_date, size_hint_y=None, height=dp(30),
                           color=TEXT_SECONDARY, halign='left', valign='middle',
                           padding=(dp(10), 0))
                dl.bind(size=lambda w, s: setattr(w, 'text_size', s))
                holder.add_widget(dl)
            title = Path(entry['path']).stem.replace('_', ' ').replace('.', ' ')
            page, total = entry.get('page', 0), entry.get('total', 0)
            subtitle = f"Page {page + 1} / {total} \u2022 {entry.get('time', '')}" if total \
                else entry.get('time', '')
            row = HistoryRow(title_text=title, subtitle_text=subtitle,
                              is_favorite=store.is_favorite(entry['path']))
            row.comic_entry = entry
            load_cover_async(entry['path'], lambda tex, r=row: setattr(r, 'cover_texture', tex))
            row.ids.heart_btn.bind(
                on_release=lambda inst, r=row: self._toggle_fav(r))
            for child in row.children:
                if getattr(child, 'icon_name', None) == 'trash':
                    child.bind(on_release=lambda inst, r=row: self._remove(r))
            row.bind(on_touch_down=lambda inst, touch, r=row: self._maybe_open(r, touch))
            holder.add_widget(row)

    def _maybe_open(self, row, touch):
        if row.collide_point(*touch.pos):
            for child in row.children:
                if child.collide_point(*touch.pos) and isinstance(child, ButtonBehavior):
                    return False
            App.get_running_app().open_comic(row.comic_entry['path'])
        return False

    def _toggle_fav(self, row):
        store = App.get_running_app().store
        store.toggle_favorite(row.comic_entry['path'])
        row.is_favorite = store.is_favorite(row.comic_entry['path'])

    def _remove(self, row):
        store = App.get_running_app().store
        store.remove_history_entry(row.comic_entry)
        self.refresh()

    def clear_all(self):
        store = App.get_running_app().store
        show_confirm('Clear history', 'Remove all reading history entries?',
                     lambda: (store.clear_history(), self.refresh()))


Builder.load_string('''
<HistoryTab>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.06, 0.06, 0.08, 1
        Rectangle:
            pos: self.pos
            size: self.size
    AppBar:
        title_text: 'History'
        IconButton:
            icon_name: 'trash'
            on_release: root.clear_all()
    AnchorLayout:
        id: empty_box
        opacity: 0
        disabled: True
        size_hint_y: None if self.opacity else 0
        height: self.parent.height if self.opacity else 0
        BoxLayout:
            orientation: 'vertical'
            size_hint: None, None
            size: dp(260), dp(120)
            spacing: dp(10)
            Icon:
                icon_name: 'history'
                color: 0.5, 0.5, 0.58, 1
                size_hint: None, None
                size: dp(56), dp(56)
                pos_hint: {'center_x': 0.5}
            Label:
                text: 'No reading history yet'
                color: 0.75, 0.75, 0.8, 1
                font_size: '14sp'
    ScrollView:
        id: scroller
        do_scroll_x: False
        bar_width: dp(6)
        BoxLayout:
            id: list_holder
            orientation: 'vertical'
            spacing: dp(6)
            padding: dp(8)
            size_hint_y: None
            height: self.minimum_height
''')


# --------------------------------------------------------------------------- #
# Browse tab -- ad-hoc filesystem browsing, independent of the library folder
# --------------------------------------------------------------------------- #

class BrowseTab(BoxLayout):

    def on_kv_post(self, base_widget):
        home = str(Path.home())
        if Path(home).exists():
            self.ids.chooser.path = home

    def open_selected(self):
        chooser = self.ids.chooser
        target = chooser.selection[0] if chooser.selection else None
        if not target:
            show_message('Browse', 'Select a comic file or a folder of images first.')
            return
        App.get_running_app().open_comic(target)

    def use_as_library(self):
        chooser = self.ids.chooser
        target = chooser.selection[0] if chooser.selection else chooser.path
        store = App.get_running_app().store
        store.data['library_folder'] = target
        store.save()
        show_message('Library folder set', f'{target}\n\nSwitch to the Library tab to see it.')


Builder.load_string('''
<BrowseTab>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.06, 0.06, 0.08, 1
        Rectangle:
            pos: self.pos
            size: self.size
    AppBar:
        title_text: 'Browse'
        IconButton:
            icon_name: 'folder'
            on_release: root.use_as_library()
        IconButton:
            icon_name: 'search'
            on_release: root.open_selected()
    FileChooserListView:
        id: chooser
        dirselect: True
        on_submit: root.open_selected()
''')


# --------------------------------------------------------------------------- #
# More tab -- settings + info
# --------------------------------------------------------------------------- #

class SettingRow(BoxLayout):
    icon_name = StringProperty('settings')
    title_text = StringProperty('')
    subtitle_text = StringProperty('')


Builder.load_string('''
<SettingRow>:
    size_hint_y: None
    height: dp(58)
    padding: dp(14), dp(4)
    spacing: dp(14)
    Icon:
        icon_name: root.icon_name
        color: 0.55, 0.68, 1, 1
        size_hint: None, None
        size: dp(22), dp(22)
        pos_hint: {'center_y': 0.5}
    BoxLayout:
        orientation: 'vertical'
        Label:
            text: root.title_text
            font_size: '15sp'
            color: 0.92, 0.92, 0.95, 1
            halign: 'left'
            valign: 'middle'
            text_size: self.size
        Label:
            text: root.subtitle_text
            opacity: 1 if root.subtitle_text else 0
            font_size: '11sp'
            color: 0.55, 0.55, 0.6, 1
            size_hint_y: None
            height: dp(14) if root.subtitle_text else 0
            halign: 'left'
            valign: 'middle'
            text_size: self.size
''')


class MenuRow(ButtonBehavior, SettingRow):
    pass


class MoreTab(BoxLayout):

    def on_kv_post(self, base_widget):
        store = App.get_running_app().store
        self.ids.compact_switch.active = bool(store.get_setting('compact_grid', False))
        self.ids.autohide_switch.active = bool(store.get_setting('autohide_controls', True))

    def set_compact(self, value):
        App.get_running_app().store.set_setting('compact_grid', bool(value))

    def set_autohide(self, value):
        App.get_running_app().store.set_setting('autohide_controls', bool(value))

    def show_stats(self):
        store = App.get_running_app().store
        total, finished, pages = store.stats()
        show_message('Statistics',
                     f'Comics tracked: {total}\nFinished: {finished}\nPages read: {pages}')

    def show_storage(self):
        folder = App.get_running_app().store.data.get('library_folder') or '(none set)'
        size = 0
        try:
            size = DATA_FILE.stat().st_size
        except OSError:
            pass
        show_message('Data and storage',
                     f'Library folder:\n{folder}\n\nApp data file:\n{DATA_FILE}\n({size} bytes)')

    def clear_history(self):
        store = App.get_running_app().store
        show_confirm('Clear history', 'Remove all reading history entries?',
                     lambda: store.clear_history())

    def show_about(self):
        show_message('About', 'Comic Reader\nVersion 1.0\n\n'
                     'A local .cbz / .cbr / .pdf / folder comic reader built with Kivy.')

    def show_help(self):
        show_message('Help',
                     'Getting started:\n\n'
                     '1. Open the Browse tab and pick the folder that holds your '
                     'comics, or use the folder icon in the Library tab.\n'
                     '2. Tap a comic to start reading.\n'
                     '3. In the reader: tap the sides to turn pages, tap the '
                     'middle to show or hide the controls, pinch to zoom.')


Builder.load_string('''
<MoreTab>:
    orientation: 'vertical'
    canvas.before:
        Color:
            rgba: 0.06, 0.06, 0.08, 1
        Rectangle:
            pos: self.pos
            size: self.size
    AppBar:
        title_text: 'More'
    ScrollView:
        do_scroll_x: False
        bar_width: dp(6)
        BoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: 0, dp(16)
            spacing: dp(4)
            AnchorLayout:
                size_hint_y: None
                height: dp(90)
                Icon:
                    icon_name: 'app_logo'
                    color: 0.55, 0.68, 1, 1
                    size_hint: None, None
                    size: dp(64), dp(64)
            Widget:
                size_hint_y: None
                height: dp(8)
            SettingRow:
                icon_name: 'category'
                title_text: 'Compact grid'
                subtitle_text: 'Smaller cover thumbnails in Library'
                Switch:
                    id: compact_switch
                    size_hint_x: None
                    width: dp(60)
                    on_active: root.set_compact(self.active)
            SettingRow:
                icon_name: 'fullscreen'
                title_text: 'Auto-hide reader controls'
                subtitle_text: 'Hide the reader bars while idle'
                Switch:
                    id: autohide_switch
                    size_hint_x: None
                    width: dp(60)
                    on_active: root.set_autohide(self.active)
            Widget:
                size_hint_y: None
                height: dp(10)
                canvas:
                    Color:
                        rgba: 0.16, 0.16, 0.2, 1
                    Rectangle:
                        pos: self.x, self.center_y
                        size: self.width, dp(1)
            MenuRow:
                icon_name: 'stats'
                title_text: 'Statistics'
                on_release: root.show_stats()
            MenuRow:
                icon_name: 'storage'
                title_text: 'Data and storage'
                on_release: root.show_storage()
            MenuRow:
                icon_name: 'trash'
                title_text: 'Clear reading history'
                on_release: root.clear_history()
            Widget:
                size_hint_y: None
                height: dp(10)
                canvas:
                    Color:
                        rgba: 0.16, 0.16, 0.2, 1
                    Rectangle:
                        pos: self.x, self.center_y
                        size: self.width, dp(1)
            MenuRow:
                icon_name: 'support'
                title_text: 'Support Us'
                on_release: root.show_about()
            MenuRow:
                icon_name: 'info'
                title_text: 'About'
                on_release: root.show_about()
            MenuRow:
                icon_name: 'help'
                title_text: 'Help'
                on_release: root.show_help()
''')


# --------------------------------------------------------------------------- #
# Main screen: hosts the 5 tabs + the bottom navigation bar
# --------------------------------------------------------------------------- #

class MainScreen(Screen):

    def on_kv_post(self, base_widget):
        self.ids.tabs.transition = NoTransition()
        self.ids.bottom_nav.on_tab = self._switch_tab

    def _switch_tab(self, key):
        self.ids.tabs.current = f'tab_{key}'
        if key == 'library':
            self.ids.tabs.get_screen('tab_library').children[0].refresh_tile_progress()
        elif key == 'history':
            self.ids.tabs.get_screen('tab_history').children[0].refresh()
        elif key == 'updates':
            self.ids.tabs.get_screen('tab_updates').children[0].refresh()

    def refresh_after_reading(self):
        try:
            self.ids.tabs.get_screen('tab_library').children[0].refresh_tile_progress()
        except Exception:
            pass

    def show_message_soon(self):
        show_message('Search', 'Type a title to filter your library (coming soon).')


Builder.load_string('''
<MainScreen>:
    BoxLayout:
        orientation: 'vertical'
        ScreenManager:
            id: tabs
            Screen:
                name: 'tab_library'
                LibraryTab
            Screen:
                name: 'tab_updates'
                UpdatesTab
            Screen:
                name: 'tab_history'
                HistoryTab
            Screen:
                name: 'tab_browse'
                BrowseTab
            Screen:
                name: 'tab_more'
                MoreTab
        BottomNav:
            id: bottom_nav
''')


# --------------------------------------------------------------------------- #
# Reader
# --------------------------------------------------------------------------- #

class ReaderScreen(Screen):
    comic_title = StringProperty('')
    page_label_text = StringProperty('')
    rtl = BooleanProperty(False)
    total_pages = NumericProperty(0)
    current_index = NumericProperty(-1)
    overlay_visible = BooleanProperty(True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.source = None
        self.cache = None
        self.comic_path = None
        self._save_ev = None
        self._hide_ev = None

    def load_comic(self, path):
        if self.source is not None:
            self.source.close()
        self.comic_path = path
        self.comic_title = Path(path).stem.replace('_', ' ')
        try:
            self.source = ComicSource(path)
        except Exception as e:
            self.comic_title = f'Could not open comic: {e}'
            self.source = None
            self.total_pages = 0
            return
        self.cache = PageCache(self.source)
        self.total_pages = self.source.page_count()
        self.current_index = -1
        page, _total = App.get_running_app().store.get_progress(path)
        start = page if 0 <= page < self.total_pages else 0
        self.show_page(start)
        self.overlay_visible = True
        self._apply_overlay(instant=True)
        self._schedule_autohide()

    def show_page(self, index):
        if self.source is None or self.total_pages == 0:
            return
        index = max(0, min(self.total_pages - 1, index))
        if index == self.current_index:
            return
        self.current_index = index
        self.page_label_text = f'{index + 1} / {self.total_pages}'
        self.cache.request(index, self._display_page)
        self.cache.request(min(index + 1, self.total_pages - 1))
        self.cache.request(min(index + 2, self.total_pages - 1))
        if index > 0:
            self.cache.request(index - 1)
        self._save_progress()

    def _display_page(self, idx, texture):
        if idx == self.current_index:
            self.ids.page_viewer.set_texture(texture)
            self.ids.page_viewer.reset_zoom()

    def _save_progress(self):
        if self._save_ev:
            self._save_ev.cancel()
        self._save_ev = Clock.schedule_once(lambda dt: self._do_save(), 0.6)

    def _do_save(self):
        if self.comic_path is not None:
            App.get_running_app().store.set_progress(self.comic_path, self.current_index, self.total_pages)

    def on_slider_value(self, value):
        self.show_page(int(value))

    def next_page(self):
        self._nav(-1 if self.rtl else 1)

    def prev_page(self):
        self._nav(1 if self.rtl else -1)

    def _nav(self, delta):
        if self.source is None:
            return
        self.show_page(self.current_index + delta)

    def toggle_rtl(self):
        self.rtl = not self.rtl

    def toggle_fullscreen(self):
        Window.fullscreen = False if Window.fullscreen else 'auto'

    def toggle_overlay(self):
        self.overlay_visible = not self.overlay_visible
        self._apply_overlay()
        if self.overlay_visible:
            self._schedule_autohide()
        else:
            self._cancel_autohide()

    def _apply_overlay(self, instant=False):
        target = 1 if self.overlay_visible else 0
        d = 0 if instant else 0.18
        Animation.cancel_all(self.ids.top_bar)
        Animation.cancel_all(self.ids.bottom_bar)
        Animation(opacity=target, d=d, t='out_quad').start(self.ids.top_bar)
        Animation(opacity=target, d=d, t='out_quad').start(self.ids.bottom_bar)

    def _schedule_autohide(self):
        self._cancel_autohide()
        if App.get_running_app().store.get_setting('autohide_controls', True):
            self._hide_ev = Clock.schedule_once(lambda dt: self._auto_hide(), 3.2)

    def _cancel_autohide(self):
        if self._hide_ev:
            self._hide_ev.cancel()
            self._hide_ev = None

    def _auto_hide(self):
        self.overlay_visible = False
        self._apply_overlay()

    def go_back(self):
        self._cancel_autohide()
        self._do_save()
        if self._save_ev:
            self._save_ev.cancel()
        App.get_running_app().back_to_library()


Builder.load_string('''
<ReaderScreen>:
    FloatLayout:
        canvas.before:
            Color:
                rgba: 0.02, 0.02, 0.03, 1
            Rectangle:
                pos: self.pos
                size: self.size
        PageViewer:
            id: page_viewer
            size_hint: 1, 1
            on_prev: root.prev_page()
            on_next: root.next_page()
            on_toggle_overlay: root.toggle_overlay()
        BoxLayout:
            id: top_bar
            size_hint: 1, None
            height: dp(58)
            pos_hint: {'top': 1}
            padding: dp(6), dp(8)
            spacing: dp(4)
            opacity: 1
            canvas.before:
                Color:
                    rgba: 0.05, 0.05, 0.07, 0.90
                Rectangle:
                    pos: self.pos
                    size: self.size
            IconButton:
                icon_name: 'back'
                on_release: root.go_back()
            Label:
                text: root.comic_title
                font_size: '16sp'
                bold: True
                color: 0.95, 0.95, 0.97, 1
                halign: 'left'
                valign: 'middle'
                text_size: self.size
                shorten: True
            Label:
                text: root.page_label_text
                size_hint_x: None
                width: dp(84)
                color: 0.8, 0.8, 0.86, 1
            IconButton:
                icon_name: 'direction'
                color: (0.42, 0.62, 1, 1) if root.rtl else (0.85, 0.85, 0.92, 1)
                on_release: root.toggle_rtl()
            IconButton:
                icon_name: 'fullscreen'
                on_release: root.toggle_fullscreen()
        BoxLayout:
            id: bottom_bar
            size_hint: 1, None
            height: dp(60)
            pos_hint: {'x': 0, 'y': 0}
            padding: dp(18), dp(10)
            spacing: dp(10)
            opacity: 1
            canvas.before:
                Color:
                    rgba: 0.05, 0.05, 0.07, 0.90
                Rectangle:
                    pos: self.pos
                    size: self.size
            Slider:
                id: page_slider
                min: 0
                max: max(0, root.total_pages - 1)
                value: root.current_index if root.current_index >= 0 else 0
                step: 1
                on_value: root.on_slider_value(self.value)
''')


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class ComicReaderApp(App):
    title = 'Comic Reader'

    def build(self):
        self.store = Store()
        Window.minimum_width, Window.minimum_height = (480, 320)
        if Window.size[0] < 800:
            Window.size = (1080, 760)
        Window.bind(on_key_down=self._on_key_down)

        sm = ScreenManager(transition=SlideTransition(duration=0.18))
        sm.add_widget(MainScreen(name='main'))
        sm.add_widget(ReaderScreen(name='reader'))
        return sm

    def open_comic(self, comic_path):
        reader = self.root.get_screen('reader')
        self.root.transition.direction = 'left'
        self.root.current = 'reader'
        reader.load_comic(comic_path)

    def back_to_library(self):
        self.root.transition.direction = 'right'
        self.root.current = 'main'
        self.root.get_screen('main').refresh_after_reading()

    def _on_key_down(self, window, key, scancode, codepoint, modifier):
        if self.root.current != 'reader':
            return False
        reader = self.root.get_screen('reader')
        if key in (275, 32):  # right arrow, space
            reader.next_page()
            return True
        if key == 276:  # left arrow
            reader.prev_page()
            return True
        if key == 102:  # 'f'
            reader.toggle_fullscreen()
            return True
        if key in (27, 8):  # escape, backspace
            reader.go_back()
            return True
        return False

    def on_stop(self):
        self.store.save()


if __name__ == '__main__':
    ComicReaderApp().run()
