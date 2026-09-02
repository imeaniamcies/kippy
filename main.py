"""
Comic Reader - a Kivy-based comic book reader.

Supports .cbz/.zip, .cbr/.rar (requires the `rarfile` package + a system
`unrar`/`unar` binary) and plain folders of images.

Features
--------
* Library screen: scans a folder for comics, shows cover thumbnails and
  per-comic reading progress.
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
    - Reading progress is remembered per comic automatically.
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
from kivy.graphics import Color, Rectangle
from kivy.properties import (
    BooleanProperty, NumericProperty, ObjectProperty, StringProperty
)
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
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


IMG_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp'}
COMIC_EXTS = {'.cbz', '.zip', '.cbr', '.rar'}

DATA_DIR = Path.home() / '.comic_reader'
DATA_FILE = DATA_DIR / 'data.json'


def natural_key(s):
    """Sort key so 'page2' comes before 'page10'."""
    s = str(s)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

class Store:
    """Small JSON-backed store for the library folder and reading progress."""

    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.data = {'library_folder': '', 'progress': {}}
        if DATA_FILE.exists():
            try:
                self.data.update(json.loads(DATA_FILE.read_text()))
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
        self.save()


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
# Library
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


class LibraryScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._tiles = {}

    def on_pre_enter(self):
        if not self.ids.grid.children:
            self.rescan()

    def choose_folder(self):
        store = App.get_running_app().store
        start_path = store.data.get('library_folder') or str(Path.home())
        if not Path(start_path).exists():
            start_path = str(Path.home())

        content = BoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
        chooser = FileChooserListView(path=start_path, dirselect=True)
        content.add_widget(chooser)
        btns = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        popup = Popup(title='Choose your comics folder', content=content, size_hint=(0.85, 0.85))

        def choose(*a):
            folder = chooser.selection[0] if chooser.selection else chooser.path
            store.data['library_folder'] = folder
            store.save()
            popup.dismiss()
            self.rescan()

        btns.add_widget(Button(text='Cancel', on_release=lambda *a: popup.dismiss()))
        btns.add_widget(Button(text='Select', on_release=choose))
        content.add_widget(btns)
        popup.open()

    def rescan(self):
        folder = App.get_running_app().store.data.get('library_folder')
        self.ids.grid.clear_widgets()
        self._tiles.clear()
        if not folder or not Path(folder).exists():
            self.ids.status_label.text = "No folder selected yet \u2014 tap 'Open Folder' to pick where your comics live."
            self.ids.status_label.opacity = 1
            return
        self.ids.status_label.text = 'Scanning for comics\u2026'
        self.ids.status_label.opacity = 1
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
                    dirs[:] = []  # treat this folder as one comic, don't recurse further
        except Exception as e:
            print(f"Scan error: {e}")
        found.sort(key=natural_key)
        Clock.schedule_once(lambda dt: self._populate(found))

    def _populate(self, found):
        self.ids.status_label.opacity = 0
        if not found:
            self.ids.status_label.text = 'No comics found in this folder.'
            self.ids.status_label.opacity = 1
            return
        store = App.get_running_app().store
        for path in found:
            tile = ComicTile(path, store)
            tile.bind(on_release=lambda inst: App.get_running_app().open_comic(inst.comic_path))
            self.ids.grid.add_widget(tile)
            self._tiles[path] = tile

    def refresh_tile_progress(self):
        for tile in self._tiles.values():
            tile.refresh_progress()


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


# --------------------------------------------------------------------------- #
# UI layout
# --------------------------------------------------------------------------- #

KV = '''
#:import dp kivy.metrics.dp

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

<LibraryScreen>:
    BoxLayout:
        orientation: 'vertical'
        canvas.before:
            Color:
                rgba: 0.06, 0.06, 0.08, 1
            Rectangle:
                pos: self.pos
                size: self.size
        BoxLayout:
            size_hint_y: None
            height: dp(58)
            padding: dp(14), dp(8)
            spacing: dp(10)
            canvas.before:
                Color:
                    rgba: 0.10, 0.10, 0.13, 1
                Rectangle:
                    pos: self.pos
                    size: self.size
            Label:
                text: 'My Comics'
                font_size: '20sp'
                bold: True
                color: 0.96, 0.96, 0.98, 1
                halign: 'left'
                valign: 'middle'
                text_size: self.size
            Button:
                text: 'Refresh'
                size_hint_x: None
                width: dp(94)
                on_release: root.rescan()
            Button:
                text: 'Open Folder'
                size_hint_x: None
                width: dp(126)
                on_release: root.choose_folder()
        Label:
            id: status_label
            text: ''
            opacity: 0
            size_hint_y: None
            height: dp(40) if self.opacity else 0
            color: 0.6, 0.6, 0.66, 1
        ScrollView:
            do_scroll_x: False
            bar_width: dp(6)
            GridLayout:
                id: grid
                cols: max(1, int(self.width // dp(190)))
                spacing: dp(14)
                padding: dp(16)
                size_hint_y: None
                height: self.minimum_height

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
            padding: dp(10), dp(8)
            spacing: dp(10)
            opacity: 1
            canvas.before:
                Color:
                    rgba: 0.05, 0.05, 0.07, 0.90
                Rectangle:
                    pos: self.pos
                    size: self.size
            Button:
                text: '< Library'
                size_hint_x: None
                width: dp(108)
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
            Button:
                text: 'RTL' if root.rtl else 'LTR'
                size_hint_x: None
                width: dp(64)
                on_release: root.toggle_rtl()
            Button:
                text: 'Fullscreen'
                size_hint_x: None
                width: dp(100)
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
'''


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class ComicReaderApp(App):
    title = 'Comic Reader'

    def build(self):
        Builder.load_string(KV)
        self.store = Store()
        Window.minimum_width, Window.minimum_height = (480, 320)
        if Window.size[0] < 800:
            Window.size = (1080, 760)
        Window.bind(on_key_down=self._on_key_down)

        sm = ScreenManager(transition=SlideTransition(duration=0.18))
        sm.add_widget(LibraryScreen(name='library'))
        sm.add_widget(ReaderScreen(name='reader'))
        return sm

    def open_comic(self, comic_path):
        reader = self.root.get_screen('reader')
        self.root.transition.direction = 'left'
        self.root.current = 'reader'
        reader.load_comic(comic_path)

    def back_to_library(self):
        self.root.transition.direction = 'right'
        self.root.current = 'library'
        self.root.get_screen('library').refresh_tile_progress()

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
