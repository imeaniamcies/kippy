"""
Novel Bridge - Kivy/KivyMD client
===================================

The bridge between writer and reader. Talks to the Flask backend in
../server/app.py over plain HTTP/JSON.

Roles
-----
  owner / admin  -> see an Admin tab: approve or reject submitted novels
                    and chapters.
  writer         -> see a Write tab: submit new novels (title, description,
                    thumbnail, tags, status), attach .txt/.md chapter files,
                    see per-novel stats, flip ongoing/completed.
  user / guest   -> browse & read anything approved from the Home tab.
                    Browsing does not require an account; reading fully
                    works while logged out.

Setup
-----
    pip install -r requirements.txt
    # edit SERVER_URL below (or set NOVEL_BRIDGE_SERVER env var) to point
    # at your running server, e.g. https://yourdomain.com or
    # http://127.0.0.1:8000 for local testing.
    python main.py
"""

import os
import re
import threading
from pathlib import Path

import requests

from kivy.app import App
from kivy.factory import Factory
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.graphics import Color, RoundedRectangle
from kivy.properties import (
    BooleanProperty, ListProperty, NumericProperty, ObjectProperty, StringProperty
)
from kivy.uix.image import AsyncImage
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition

from kivymd.app import MDApp
from kivymd.uix.screen import MDScreen
from kivymd.uix.card import MDCard
from kivymd.uix.button import MDRaisedButton, MDFlatButton, MDIconButton
from kivymd.uix.textfield import MDTextField
from kivymd.uix.label import MDLabel
from kivymd.uix.list import MDList, OneLineAvatarIconListItem, IconLeftWidget, IconRightWidget
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.gridlayout import MDGridLayout
from kivymd.uix.dialog import MDDialog
from kivymd.uix.menu import MDDropdownMenu

SERVER_URL = os.environ.get('NOVEL_BRIDGE_SERVER', 'http://127.0.0.1:8000')

BG = (0.06, 0.06, 0.08, 1)
CARD_BG = (0.13, 0.13, 0.17, 1)
ACCENT = (0.42, 0.62, 1, 1)


# --------------------------------------------------------------------------- #
# API client - every call runs on a worker thread, result delivered on the
# main thread via Clock so the UI never blocks.
# --------------------------------------------------------------------------- #

class ApiError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self):
        self.token = None

    def _headers(self):
        h = {}
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        return h

    def _request(self, method, path, **kwargs):
        url = f'{SERVER_URL}{path}'
        try:
            resp = requests.request(method, url, headers=self._headers(), timeout=15, **kwargs)
        except requests.exceptions.RequestException as e:
            raise ApiError(f'Could not reach server: {e}')
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            raise ApiError(body.get('error', f'server error ({resp.status_code})'), resp.status_code)
        return body

    # -- async wrapper -----------------------------------------------------
    def call(self, method, path, on_success=None, on_error=None, **kwargs):
        def worker():
            try:
                result = self._request(method, path, **kwargs)
            except ApiError as e:
                message = str(e)  # capture now - `e` itself is gone once except: ends
                if on_error:
                    Clock.schedule_once(lambda dt: on_error(message))
                return
            if on_success:
                Clock.schedule_once(lambda dt: on_success(result))
        threading.Thread(target=worker, daemon=True).start()

    # -- convenience endpoints ----------------------------------------------
    def login(self, username, password, on_success, on_error):
        self.call('POST', '/api/login', on_success, on_error,
                   json={'username': username, 'password': password})

    def register(self, username, password, on_success, on_error):
        self.call('POST', '/api/register', on_success, on_error,
                   json={'username': username, 'password': password})

    def me(self, on_success, on_error):
        self.call('GET', '/api/me', on_success, on_error)

    def list_novels(self, on_success, on_error, mine=False, pending=False, q=''):
        params = {}
        if mine:
            params['mine'] = '1'
        if pending:
            params['pending'] = '1'
        if q:
            params['q'] = q
        self.call('GET', '/api/novels', on_success, on_error, params=params)

    def get_novel(self, novel_id, on_success, on_error):
        self.call('GET', f'/api/novels/{novel_id}', on_success, on_error)

    def create_novel(self, fields, thumbnail_path, on_success, on_error):
        files = {}
        if thumbnail_path:
            files['thumbnail'] = (Path(thumbnail_path).name, open(thumbnail_path, 'rb'))
        self.call('POST', '/api/novels', on_success, on_error, data=fields, files=files or None)

    def update_novel(self, novel_id, fields, on_success, on_error):
        self.call('PUT', f'/api/novels/{novel_id}', on_success, on_error, json=fields)

    def upload_chapter(self, novel_id, chapter_number, title, file_path, on_success, on_error):
        files = {'file': (Path(file_path).name, open(file_path, 'rb'))}
        data = {'chapter_number': str(chapter_number), 'title': title}
        self.call('POST', f'/api/novels/{novel_id}/chapters', on_success, on_error, data=data, files=files)

    def get_chapter(self, chapter_id, on_success, on_error):
        self.call('GET', f'/api/chapters/{chapter_id}', on_success, on_error)

    def pending_queue(self, on_success, on_error):
        self.call('GET', '/api/admin/pending', on_success, on_error)

    def approve_novel(self, novel_id, on_success, on_error):
        self.call('POST', f'/api/admin/novels/{novel_id}/approve', on_success, on_error)

    def reject_novel(self, novel_id, on_success, on_error):
        self.call('POST', f'/api/admin/novels/{novel_id}/reject', on_success, on_error)

    def approve_chapter(self, chapter_id, on_success, on_error):
        self.call('POST', f'/api/admin/chapters/{chapter_id}/approve', on_success, on_error)

    def reject_chapter(self, chapter_id, on_success, on_error):
        self.call('POST', f'/api/admin/chapters/{chapter_id}/reject', on_success, on_error)

    def writer_stats(self, on_success, on_error):
        self.call('GET', '/api/writer/stats', on_success, on_error)

    def list_users(self, on_success, on_error):
        self.call('GET', '/api/users', on_success, on_error)

    def set_role(self, user_id, role, on_success, on_error):
        self.call('POST', f'/api/users/{user_id}/role', on_success, on_error, json={'role': role})

    def change_password(self, old_password, new_password, on_success, on_error):
        self.call('POST', '/api/me/password', on_success, on_error,
                   json={'old_password': old_password, 'new_password': new_password})

    def thumb_url(self, url):
        """Novel thumbnails are now full Supabase Storage URLs; keep this
        as a thin pass-through (with a fallback for older relative paths)."""
        if not url:
            return ''
        if url.startswith('http://') or url.startswith('https://'):
            return url
        return f'{SERVER_URL}{url}'


api = Api()


_active_toasts = []


def toast(text, duration=2.2):
    """A small bottom-of-screen message built from plain Kivy widgets only -
    deliberately avoids KivyMD's Snackbar, whose constructor signature has
    changed across versions and isn't worth chasing."""
    label = Label(
        text=str(text),
        color=(1, 1, 1, 1),
        size_hint=(None, None),
        padding=(dp(16), dp(10)),
    )
    label.texture_update()
    label.size = (min(label.texture_size[0] + dp(32), Window.width - dp(24)), dp(44))
    label.text_size = (label.width - dp(32), None)
    label.halign = 'center'
    label.valign = 'middle'

    # stack multiple toasts above one another if they overlap in time
    base_y = dp(24) + sum(t.height + dp(8) for t in _active_toasts)
    label.pos = ((Window.width - label.width) / 2, base_y)

    with label.canvas.before:
        Color(0.13, 0.13, 0.17, 0.96)
        rect = RoundedRectangle(pos=label.pos, size=label.size, radius=[dp(10)])

    def sync_rect(*_a):
        rect.pos = label.pos
        rect.size = label.size
    label.bind(pos=sync_rect, size=sync_rect)

    Window.add_widget(label)
    _active_toasts.append(label)

    def remove(*_a):
        if label in _active_toasts:
            _active_toasts.remove(label)
        Window.remove_widget(label)
    Clock.schedule_once(remove, duration)


def file_pick_popup(title, filters, on_choice):
    """Small filechooser popup; returns the chosen path via on_choice(path)."""
    layout = MDBoxLayout(orientation='vertical', spacing=dp(8), padding=dp(8))
    chooser = FileChooserListView(filters=filters, path=str(Path.home()))
    layout.add_widget(chooser)
    btn_row = MDBoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
    popup = Popup(title=title, content=layout, size_hint=(0.9, 0.9))

    def choose(*_):
        if chooser.selection:
            on_choice(chooser.selection[0])
        popup.dismiss()

    btn_row.add_widget(MDFlatButton(text='Cancel', on_release=lambda *_: popup.dismiss()))
    btn_row.add_widget(MDRaisedButton(text='Select', on_release=choose))
    layout.add_widget(btn_row)
    popup.open()


# --------------------------------------------------------------------------- #
# Reusable widgets
# --------------------------------------------------------------------------- #

Builder.load_string('''
<StatusBadge@MDLabel>:
    adaptive_size: True
    padding: dp(10), dp(4)
    bold: True
    font_size: '11sp'
    canvas.before:
        Color:
            rgba: (0.2, 0.75, 0.4, 1) if self.text == 'ONGOING' else (0.5, 0.5, 0.58, 1)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(12)]

<TagChip@MDLabel>:
    adaptive_size: True
    padding: dp(11), dp(5)
    font_size: '12sp'
    color: 0.85, 0.85, 0.92, 1
    canvas.before:
        Color:
            rgba: 0.18, 0.18, 0.24, 1
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14)]
        Color:
            rgba: 0.3, 0.3, 0.4, 1
        Line:
            rounded_rectangle: (self.x, self.y, self.width, self.height, dp(14))
            width: 1

<NovelCard>:
    orientation: 'vertical'
    size_hint: None, None
    size: dp(120), dp(210)
    md_bg_color: 0.13, 0.13, 0.17, 1
    radius: [dp(10)]
    padding: 0
    AsyncImage:
        source: root.thumb_url
        size_hint_y: None
        height: dp(160)
        allow_stretch: True
        keep_ratio: False
    MDLabel:
        text: root.title
        font_size: '12sp'
        bold: True
        shorten: True
        shorten_from: 'right'
        halign: 'left'
        size_hint_y: None
        height: dp(40)
        padding: dp(6), 0
        color: 0.95, 0.95, 0.97, 1
''')


def markdown_to_kivy_markup(text):
    """Converts a practical subset of Markdown (bold, italic, strikethrough,
    headings, blockquotes, horizontal rules, bullet/numbered lists, inline
    code) into Kivy's markup language, so a chapter written in .md renders
    with real formatting instead of showing raw asterisks/hashes.

    Kivy's Label markup only understands its own [b]/[i]/[color=..] style
    tags (see kivy.core.text.markup), so this is a small Markdown -> Kivy
    markup translator, not a full CommonMark implementation - enough for
    what people actually put in novel chapters."""
    if not text:
        return ''

    # Escape Kivy's own markup special characters first, so any literal
    # [ ] & already in the chapter text can't be misread as a markup tag
    # once we start inserting real [b]/[i]/... tags below.
    text = text.replace('&', '&amp;').replace('[', '&bl;').replace(']', '&br;')

    out_lines = []
    for line in text.split('\n'):
        stripped = line.strip()

        # Horizontal rule: --- / *** / ___ alone on a line
        if re.fullmatch(r'(-{3,}|\*{3,}|_{3,})', stripped):
            out_lines.append('[color=#555566]' + '\u2500' * 30 + '[/color]')
            continue

        # Headings: # / ## / ###
        heading_match = re.match(r'^(#{1,3})\s+(.*)', stripped)
        if heading_match:
            level = len(heading_match.group(1))
            size = {1: 24, 2: 20, 3: 17}[level]
            out_lines.append(f'[size={size}sp][b]{heading_match.group(2)}[/b][/size]')
            continue

        # Blockquote: > text
        quote_match = re.match(r'^>\s?(.*)', stripped)
        if quote_match:
            out_lines.append(f'[color=#9a9aa8][i]\u2503 {quote_match.group(1)}[/i][/color]')
            continue

        # Bullet list: - / * / + followed by a space
        bullet_match = re.match(r'^[-*+]\s+(.*)', stripped)
        if bullet_match:
            out_lines.append(f'   \u2022  {bullet_match.group(1)}')
            continue

        # Numbered list: "1. text"
        num_match = re.match(r'^(\d+)\.\s+(.*)', stripped)
        if num_match:
            out_lines.append(f'   {num_match.group(1)}.  {num_match.group(2)}')
            continue

        out_lines.append(line)

    text = '\n'.join(out_lines)

    # Inline formatting - order matters: bold+italic together, then bold,
    # then italic, then strikethrough, then inline code.
    text = re.sub(r'(\*\*\*|___)(.+?)\1', r'[b][i]\2[/i][/b]', text)
    text = re.sub(r'(\*\*|__)(.+?)\1', r'[b]\2[/b]', text)
    text = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)', r'[i]\1[/i]', text)
    text = re.sub(r'(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?!\w)', r'[i]\1[/i]', text)
    text = re.sub(r'~~(.+?)~~', r'[s]\1[/s]', text)
    text = re.sub(r'`([^`]+?)`', r'[color=#8fd6c9]\1[/color]', text)

    return text


class NovelCard(MDCard):
    title = StringProperty('')
    thumb_url = StringProperty('')
    novel_id = NumericProperty(0)
    press_callback = ObjectProperty(None)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.press_callback:
            self.press_callback(self.novel_id)
            return True
        return super().on_touch_down(touch)


# --------------------------------------------------------------------------- #
# Login / Register
# --------------------------------------------------------------------------- #

class LoginScreen(MDScreen):
    def do_login(self):
        username = self.ids.username.text.strip()
        password = self.ids.password.text
        if not username or not password:
            toast('Enter a username and password')
            return
        self.ids.status_label.text = 'Signing in...'
        api.login(username, password, self._login_ok, self._login_fail)

    def _login_ok(self, result):
        api.token = result['token']
        app = App.get_running_app()
        app.user = result['user']
        self.ids.status_label.text = ''
        toast(f"Welcome back, {result['user']['username']}")
        app.on_authenticated()

    def _login_fail(self, msg):
        self.ids.status_label.text = msg

    def do_register(self):
        username = self.ids.username.text.strip()
        password = self.ids.password.text
        if not username or not password:
            toast('Enter a username and password')
            return
        self.ids.status_label.text = 'Creating account...'
        api.register(username, password, self._register_ok, self._login_fail)

    def _register_ok(self, result):
        self.ids.status_label.text = 'Account created - now log in'
        toast('Registered! Log in below.')

    def continue_as_guest(self):
        api.token = None
        App.get_running_app().user = None
        App.get_running_app().on_authenticated()


Builder.load_string('''
<LoginScreen>:
    name: 'login'
    MDBoxLayout:
        orientation: 'vertical'
        padding: dp(28)
        spacing: dp(14)
        md_bg_color: 0.06, 0.06, 0.08, 1
        Widget:
            size_hint_y: 0.2
        MDLabel:
            text: 'Novel Bridge'
            font_style: 'H4'
            bold: True
            halign: 'center'
            size_hint_y: None
            height: dp(50)
        MDLabel:
            text: 'Where writers and readers meet'
            halign: 'center'
            theme_text_color: 'Secondary'
            size_hint_y: None
            height: dp(30)
        Widget:
            size_hint_y: 0.1
        MDTextField:
            id: username
            hint_text: 'Username'
            size_hint_x: 1
        MDTextField:
            id: password
            hint_text: 'Password'
            password: True
            size_hint_x: 1
        MDLabel:
            id: status_label
            text: ''
            theme_text_color: 'Error'
            halign: 'center'
            size_hint_y: None
            height: dp(24)
        MDRaisedButton:
            text: 'Log In'
            size_hint_x: 1
            on_release: root.do_login()
        MDFlatButton:
            text: 'Create Account'
            size_hint_x: 1
            on_release: root.do_register()
        MDFlatButton:
            text: 'Continue as Guest (read only)'
            size_hint_x: 1
            on_release: root.continue_as_guest()
        Widget:
            size_hint_y: 0.3
''')


# --------------------------------------------------------------------------- #
# Home - browse approved novels
# --------------------------------------------------------------------------- #

class HomeScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.refresh()

    def refresh(self):
        self.ids.grid.clear_widgets()
        api.list_novels(self._loaded, lambda m: toast(m))

    def _loaded(self, novels):
        self.ids.grid.clear_widgets()
        if not novels:
            self.ids.grid.add_widget(MDLabel(text='No novels published yet.', halign='center'))
            return
        for n in novels:
            card = NovelCard(
                title=n['title'],
                thumb_url=api.thumb_url(n['thumbnail_url']) or 'atlas://data/images/defaulttheme/image-missing',
                novel_id=n['id'],
                press_callback=self.open_novel,
            )
            self.ids.grid.add_widget(card)

    def open_novel(self, novel_id):
        app = App.get_running_app()
        app.open_novel_detail(novel_id)

    def do_search(self, text):
        api.list_novels(self._loaded, lambda m: toast(m), q=text)


Builder.load_string('''
<HomeScreen>:
    name: 'home'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: 'Novel Bridge'
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            right_action_items: [['account', lambda x: app.open_profile()]]
        MDTextField:
            id: search
            hint_text: 'Search novels'
            size_hint_y: None
            height: dp(48)
            padding: dp(10), dp(10)
            on_text_validate: root.do_search(self.text)
        MDScrollView:
            MDGridLayout:
                id: grid
                cols: 3
                spacing: dp(10)
                padding: dp(10)
                size_hint_y: None
                height: self.minimum_height
                adaptive_height: True
''')


# --------------------------------------------------------------------------- #
# Novel Detail - styled after the reference screenshot:
#   thumbnail + title/author/status header, action row, description,
#   tag chips, "N chapters" heading, chapter list with dates, resume button.
# --------------------------------------------------------------------------- #

class ChapterRow(MDBoxLayout):
    chapter_id = NumericProperty(0)
    number_text = StringProperty('')
    date_text = StringProperty('')
    pending = BooleanProperty(False)
    press_callback = ObjectProperty(None)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.press_callback:
            self.press_callback(self.chapter_id)
            return True
        return super().on_touch_down(touch)


Builder.load_string('''
<ChapterRow>:
    size_hint_y: None
    height: dp(56)
    padding: dp(16), 0
    canvas.before:
        Color:
            rgba: 0.42, 0.62, 1, 1
        Ellipse:
            pos: self.x, self.center_y - dp(3)
            size: dp(6), dp(6)
    MDBoxLayout:
        orientation: 'vertical'
        padding: dp(14), 0, 0, 0
        MDLabel:
            text: root.number_text + ('  [PENDING]' if root.pending else '')
            bold: True
            font_size: '15sp'
            color: (0.95,0.7,0.3,1) if root.pending else (0.95, 0.95, 0.97, 1)
        MDLabel:
            text: root.date_text
            font_size: '12sp'
            theme_text_color: 'Secondary'
    MDIconButton:
        icon: 'book-open-page-variant-outline'
        on_release: root.press_callback(root.chapter_id) if root.press_callback else None
''')


class NovelDetailScreen(MDScreen):
    novel_id = NumericProperty(0)
    novel_data = ObjectProperty(None)

    def load(self, novel_id):
        self.novel_id = novel_id
        self.ids.chapter_list.clear_widgets()
        self.ids.title_label.text = 'Loading...'
        api.get_novel(novel_id, self._loaded, lambda m: toast(m))

    def _loaded(self, data):
        self.novel_data = data
        self.ids.thumb.source = api.thumb_url(data['thumbnail_url']) or 'atlas://data/images/defaulttheme/image-missing'
        self.ids.title_label.text = data['title']
        self.ids.author_label.text = f"By {data['writer_username']}"
        self.ids.status_badge.text = data['status'].upper()
        self.ids.desc_label.text = data['description'] or '(no description yet)'
        self.ids.chapter_count_label.text = f"{len(data['chapters'])} chapters"

        self.ids.tag_row.clear_widgets()
        for tag in data['tags']:
            self.ids.tag_row.add_widget(Factory.TagChip(text=tag))

        self.ids.chapter_list.clear_widgets()
        chapters = sorted(data['chapters'], key=lambda c: c['chapter_number'], reverse=True)
        for c in chapters:
            row = ChapterRow(
                chapter_id=c['id'],
                number_text=f"Chapter {c['chapter_number']:g}" + (f" - {c['title']}" if c['title'] else ''),
                date_text=c['created_at'][:10],
                pending=(not c['approved']),
                press_callback=self.open_chapter,
            )
            self.ids.chapter_list.add_widget(row)

        app = App.get_running_app()
        is_owner_writer = app.user and (
            app.user['role'] in ('owner', 'admin') or
            (app.user['role'] == 'writer' and app.user['id'] == data['writer_id'])
        )
        self.ids.writer_controls.clear_widgets()
        if is_owner_writer:
            toggle_to = 'completed' if data['status'] == 'ongoing' else 'ongoing'
            self.ids.writer_controls.add_widget(MDFlatButton(
                text=f'Mark as {toggle_to}',
                on_release=lambda *_: self.set_status(toggle_to),
            ))
            self.ids.writer_controls.add_widget(MDFlatButton(
                text='Add chapter',
                on_release=lambda *_: App.get_running_app().open_upload_chapter(self.novel_id),
            ))
        self.ids.admin_controls.clear_widgets()
        if app.user and app.user['role'] in ('owner', 'admin') and not data['approved']:
            self.ids.admin_controls.add_widget(MDRaisedButton(
                text='Approve novel', on_release=lambda *_: self.approve_novel()))
            self.ids.admin_controls.add_widget(MDFlatButton(
                text='Reject', on_release=lambda *_: self.reject_novel()))

    def set_status(self, status):
        api.update_novel(self.novel_id, {'status': status},
                          lambda r: (toast('Status updated'), self.load(self.novel_id)),
                          lambda m: toast(m))

    def approve_novel(self):
        api.approve_novel(self.novel_id, lambda r: (toast('Approved'), self.load(self.novel_id)),
                           lambda m: toast(m))

    def reject_novel(self):
        api.reject_novel(self.novel_id, lambda r: (toast('Rejected'), self.load(self.novel_id)),
                          lambda m: toast(m))

    def open_chapter(self, chapter_id):
        App.get_running_app().open_reader(chapter_id, self.novel_data)

    def resume(self):
        if self.novel_data and self.novel_data['chapters']:
            approved = [c for c in self.novel_data['chapters'] if c['approved']]
            if approved:
                latest = sorted(approved, key=lambda c: c['chapter_number'])[0]
                self.open_chapter(latest['id'])
                return
        toast('No readable chapters yet')

    def go_back(self):
        App.get_running_app().go_home()


Builder.load_string('''
<NovelDetailScreen>:
    name: 'novel_detail'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: ''
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            left_action_items: [['arrow-left', lambda x: root.go_back()]]
        MDScrollView:
            MDBoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(16)
                spacing: dp(12)
                MDBoxLayout:
                    size_hint_y: None
                    height: dp(190)
                    spacing: dp(14)
                    AsyncImage:
                        id: thumb
                        size_hint_x: None
                        width: dp(130)
                        allow_stretch: True
                        keep_ratio: False
                    MDBoxLayout:
                        orientation: 'vertical'
                        spacing: dp(6)
                        MDLabel:
                            id: title_label
                            text: ''
                            font_style: 'H6'
                            bold: True
                            shorten: False
                        MDLabel:
                            id: author_label
                            text: ''
                            theme_text_color: 'Secondary'
                            size_hint_y: None
                            height: dp(22)
                        StatusBadge:
                            id: status_badge
                            text: 'ONGOING'
                        MDBoxLayout:
                            id: writer_controls
                            orientation: 'vertical'
                            size_hint_y: None
                            height: self.minimum_height
                        MDBoxLayout:
                            id: admin_controls
                            spacing: dp(6)
                            size_hint_y: None
                            height: self.minimum_height
                MDRaisedButton:
                    text: 'Resume Reading'
                    size_hint_x: 1
                    on_release: root.resume()
                MDLabel:
                    id: desc_label
                    text: ''
                    theme_text_color: 'Secondary'
                    size_hint_y: None
                    height: self.texture_size[1]
                    text_size: self.width, None
                MDBoxLayout:
                    id: tag_row
                    spacing: dp(8)
                    size_hint_y: None
                    height: dp(34)
                MDLabel:
                    id: chapter_count_label
                    text: '0 chapters'
                    bold: True
                    font_style: 'Subtitle1'
                    size_hint_y: None
                    height: dp(30)
                MDList:
                    id: chapter_list
                    size_hint_y: None
                    height: self.minimum_height
''')


# --------------------------------------------------------------------------- #
# Reader - renders the raw .txt / .md chapter content
# --------------------------------------------------------------------------- #

class ReaderScreen(MDScreen):
    novel_data = ObjectProperty(None)

    def load(self, chapter_id, novel_data=None):
        self.novel_data = novel_data
        self.ids.body_label.text = 'Loading...'
        api.get_chapter(chapter_id, self._loaded, lambda m: toast(m))

    def _loaded(self, data):
        chapter_title = data['title'] or f"Chapter {data['chapter_number']:g}"
        self.ids.topbar.title = chapter_title
        self.ids.body_label.text = markdown_to_kivy_markup(data['content'])
        self._chapter = data

    def go_back(self):
        App.get_running_app().go_back_from_reader()


Builder.load_string('''
<ReaderScreen>:
    name: 'reader'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            id: topbar
            title: ''
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            left_action_items: [['arrow-left', lambda x: root.go_back()]]
        MDScrollView:
            MDLabel:
                id: body_label
                text: ''
                markup: True
                padding: dp(20), dp(20)
                size_hint_y: None
                height: self.texture_size[1] + dp(40)
                text_size: self.width - dp(40), None
                font_size: '16sp'
                line_height: 1.4
''')


# --------------------------------------------------------------------------- #
# Writer dashboard - create novel, upload chapters, view stats, toggle status
# --------------------------------------------------------------------------- #

class WriterScreen(MDScreen):
    thumbnail_path = StringProperty('')

    def on_pre_enter(self, *args):
        self.refresh_stats()

    def pick_thumbnail(self):
        file_pick_popup('Pick a thumbnail image', ['*.png', '*.jpg', '*.jpeg', '*.webp'],
                         self._thumb_chosen)

    def _thumb_chosen(self, path):
        self.thumbnail_path = path
        self.ids.thumb_label.text = Path(path).name

    def submit_novel(self):
        title = self.ids.title_field.text.strip()
        desc = self.ids.desc_field.text.strip()
        tags = self.ids.tags_field.text.strip()
        status = 'completed' if self.ids.status_switch.active else 'ongoing'
        if not title:
            toast('Title is required')
            return
        fields = {'title': title, 'description': desc, 'tags': tags, 'status': status}
        api.create_novel(fields, self.thumbnail_path or None, self._novel_created, lambda m: toast(m))

    def _novel_created(self, result):
        toast('Novel submitted for admin approval')
        self.ids.title_field.text = ''
        self.ids.desc_field.text = ''
        self.ids.tags_field.text = ''
        self.thumbnail_path = ''
        self.ids.thumb_label.text = 'No thumbnail chosen'
        self.refresh_stats()

    def refresh_stats(self):
        self.ids.stats_list.clear_widgets()
        api.writer_stats(self._stats_loaded, lambda m: toast(m))

    def _stats_loaded(self, novels):
        self.ids.stats_list.clear_widgets()
        if not novels:
            self.ids.stats_list.add_widget(MDLabel(text='You have not published any novels yet.'))
            return
        for n in novels:
            approval = 'approved' if n['approved'] else ('rejected' if n['rejected'] else 'pending review')
            box = MDBoxLayout(orientation='vertical', size_hint_y=None, height=dp(96),
                               padding=(dp(10), dp(6)))
            box.md_bg_color = CARD_BG
            box.add_widget(MDLabel(text=f"{n['title']}  ({approval})", bold=True))
            box.add_widget(MDLabel(
                text=(f"{n['chapter_count']} chapters "
                      f"({n['approved_chapter_count']} approved, {n['pending_chapter_count']} pending)  |  "
                      f"{n['views']} novel views  |  {n['total_chapter_views']} chapter views  |  "
                      f"status: {n['status']}"),
                theme_text_color='Secondary', font_size='12sp'))
            row = MDBoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
            row.add_widget(MDFlatButton(text='Open', on_release=lambda *_, nid=n['id']: App.get_running_app().open_novel_detail(nid)))
            row.add_widget(MDFlatButton(text='Add chapter', on_release=lambda *_, nid=n['id']: App.get_running_app().open_upload_chapter(nid)))
            box.add_widget(row)
            self.ids.stats_list.add_widget(box)


Builder.load_string('''
<WriterScreen>:
    name: 'writer'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: 'Write'
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
        MDScrollView:
            MDBoxLayout:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: dp(16)
                spacing: dp(10)
                MDLabel:
                    text: 'Submit a new novel'
                    bold: True
                    font_style: 'Subtitle1'
                    size_hint_y: None
                    height: dp(28)
                MDTextField:
                    id: title_field
                    hint_text: 'Title'
                MDTextField:
                    id: desc_field
                    hint_text: 'Description'
                    multiline: True
                MDTextField:
                    id: tags_field
                    hint_text: 'Tags, comma separated (Action, Fantasy, ...)'
                MDBoxLayout:
                    size_hint_y: None
                    height: dp(40)
                    spacing: dp(10)
                    MDFlatButton:
                        text: 'Choose Thumbnail'
                        on_release: root.pick_thumbnail()
                    MDLabel:
                        id: thumb_label
                        text: 'No thumbnail chosen'
                        theme_text_color: 'Secondary'
                MDBoxLayout:
                    size_hint_y: None
                    height: dp(40)
                    MDLabel:
                        text: 'Mark completed'
                    MDSwitch:
                        id: status_switch
                MDRaisedButton:
                    text: 'Submit for Approval'
                    size_hint_x: 1
                    on_release: root.submit_novel()
                MDLabel:
                    text: 'Your novels & stats'
                    bold: True
                    font_style: 'Subtitle1'
                    size_hint_y: None
                    height: dp(36)
                MDBoxLayout:
                    id: stats_list
                    orientation: 'vertical'
                    spacing: dp(8)
                    size_hint_y: None
                    height: self.minimum_height
''')


class UploadChapterScreen(MDScreen):
    novel_id = NumericProperty(0)
    chapter_path = StringProperty('')

    def open_for(self, novel_id):
        self.novel_id = novel_id
        self.chapter_path = ''
        self.ids.file_label.text = 'No file chosen'
        self.ids.number_field.text = ''
        self.ids.title_field.text = ''

    def pick_file(self):
        file_pick_popup('Pick a .txt or .md chapter file', ['*.txt', '*.md'], self._file_chosen)

    def _file_chosen(self, path):
        self.chapter_path = path
        self.ids.file_label.text = Path(path).name

    def submit(self):
        if not self.chapter_path:
            toast('Choose a .txt or .md file first')
            return
        try:
            number = float(self.ids.number_field.text or '0')
        except ValueError:
            toast('Chapter number must be a number')
            return
        title = self.ids.title_field.text.strip()
        api.upload_chapter(self.novel_id, number, title, self.chapter_path,
                            self._uploaded, lambda m: toast(m))

    def _uploaded(self, result):
        toast('Chapter submitted for admin approval')
        App.get_running_app().go_back_generic()


Builder.load_string('''
<UploadChapterScreen>:
    name: 'upload_chapter'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: 'Add Chapter'
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            left_action_items: [['arrow-left', lambda x: app.go_back_generic()]]
        MDBoxLayout:
            orientation: 'vertical'
            padding: dp(16)
            spacing: dp(12)
            MDTextField:
                id: number_field
                hint_text: 'Chapter number (e.g. 1 or 1.5)'
                input_filter: 'float'
            MDTextField:
                id: title_field
                hint_text: 'Chapter title (optional)'
            MDBoxLayout:
                size_hint_y: None
                height: dp(40)
                spacing: dp(10)
                MDFlatButton:
                    text: 'Choose .txt / .md File'
                    on_release: root.pick_file()
                MDLabel:
                    id: file_label
                    text: 'No file chosen'
                    theme_text_color: 'Secondary'
            MDRaisedButton:
                text: 'Upload'
                size_hint_x: 1
                on_release: root.submit()
''')


# --------------------------------------------------------------------------- #
# Admin dashboard - approve/reject the pending queue
# --------------------------------------------------------------------------- #

class AdminScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.refresh()

    def refresh(self):
        self.ids.queue.clear_widgets()
        api.pending_queue(self._loaded, lambda m: toast(m))

    def open_user_management(self):
        App.get_running_app().open_user_management()

    def _loaded(self, data):
        self.ids.queue.clear_widgets()
        if not data['novels'] and not data['chapters']:
            self.ids.queue.add_widget(MDLabel(text='Nothing pending review.'))
            return
        if data['novels']:
            self.ids.queue.add_widget(MDLabel(text='Novels awaiting approval', bold=True,
                                               size_hint_y=None, height=dp(30)))
            for n in data['novels']:
                self.ids.queue.add_widget(self._novel_row(n))
        if data['chapters']:
            self.ids.queue.add_widget(MDLabel(text='Chapters awaiting approval', bold=True,
                                               size_hint_y=None, height=dp(30)))
            for c in data['chapters']:
                self.ids.queue.add_widget(self._chapter_row(c))

    def _novel_row(self, n):
        box = MDBoxLayout(orientation='vertical', size_hint_y=None, height=dp(90),
                           padding=(dp(10), dp(6)))
        box.md_bg_color = CARD_BG
        box.add_widget(MDLabel(text=n['title'], bold=True))
        box.add_widget(MDLabel(text=n['description'][:80], theme_text_color='Secondary', font_size='12sp'))
        row = MDBoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
        row.add_widget(MDRaisedButton(text='Approve', on_release=lambda *_: self._approve_novel(n['id'])))
        row.add_widget(MDFlatButton(text='Reject', on_release=lambda *_: self._reject_novel(n['id'])))
        row.add_widget(MDFlatButton(text='View', on_release=lambda *_: App.get_running_app().open_novel_detail(n['id'])))
        box.add_widget(row)
        return box

    def _chapter_row(self, c):
        box = MDBoxLayout(orientation='vertical', size_hint_y=None, height=dp(70),
                           padding=(dp(10), dp(6)))
        box.md_bg_color = CARD_BG
        box.add_widget(MDLabel(text=f"{c['novel_title']} - Chapter {c['chapter_number']:g}", bold=True))
        row = MDBoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
        row.add_widget(MDRaisedButton(text='Approve', on_release=lambda *_: self._approve_chapter(c['id'])))
        row.add_widget(MDFlatButton(text='Reject', on_release=lambda *_: self._reject_chapter(c['id'])))
        box.add_widget(row)
        return box

    def _approve_novel(self, nid):
        api.approve_novel(nid, lambda r: (toast('Approved'), self.refresh()), lambda m: toast(m))

    def _reject_novel(self, nid):
        api.reject_novel(nid, lambda r: (toast('Rejected'), self.refresh()), lambda m: toast(m))

    def _approve_chapter(self, cid):
        api.approve_chapter(cid, lambda r: (toast('Approved'), self.refresh()), lambda m: toast(m))

    def _reject_chapter(self, cid):
        api.reject_chapter(cid, lambda r: (toast('Rejected'), self.refresh()), lambda m: toast(m))


Builder.load_string('''
<AdminScreen>:
    name: 'admin'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: 'Admin Review Queue'
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            right_action_items: [['account-supervisor', lambda x: root.open_user_management()]]
        MDScrollView:
            MDBoxLayout:
                id: queue
                orientation: 'vertical'
                spacing: dp(8)
                padding: dp(10)
                size_hint_y: None
                height: self.minimum_height
''')


# --------------------------------------------------------------------------- #
# User management (role changes) - owner promotes/demotes here, no more
# needing Postman/curl for POST /api/users/<id>/role.
# --------------------------------------------------------------------------- #

class UserManagementScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.refresh()

    def refresh(self):
        self.ids.user_list.clear_widgets()
        api.list_users(self._loaded, lambda m: toast(m))

    def _loaded(self, users):
        self.ids.user_list.clear_widgets()
        app = App.get_running_app()
        is_owner = app.user and app.user['role'] == 'owner'
        for u in users:
            box = MDBoxLayout(orientation='vertical', size_hint_y=None, height=dp(92),
                               padding=(dp(10), dp(6)))
            box.md_bg_color = CARD_BG
            box.add_widget(MDLabel(text=f"{u['username']}", bold=True))
            box.add_widget(MDLabel(text=f"role: {u['role']}", theme_text_color='Secondary', font_size='12sp'))
            if is_owner and u['role'] != 'owner':
                row = MDBoxLayout(size_hint_y=None, height=dp(36), spacing=dp(6))
                for role in ('user', 'writer', 'admin'):
                    if role == u['role']:
                        continue
                    row.add_widget(MDFlatButton(
                        text=f'Make {role}',
                        on_release=lambda *_, uid=u['id'], r=role: self._change_role(uid, r),
                    ))
                box.add_widget(row)
            self.ids.user_list.add_widget(box)

    def _change_role(self, user_id, role):
        api.set_role(user_id, role, lambda r: (toast(f'Role updated to {role}'), self.refresh()),
                     lambda m: toast(m))


Builder.load_string('''
<UserManagementScreen>:
    name: 'user_management'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: 'Manage Users'
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            left_action_items: [['arrow-left', lambda x: app.open_admin()]]
        MDScrollView:
            MDBoxLayout:
                id: user_list
                orientation: 'vertical'
                spacing: dp(8)
                padding: dp(10)
                size_hint_y: None
                height: self.minimum_height
''')


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #

class ProfileScreen(MDScreen):
    def on_pre_enter(self, *args):
        app = App.get_running_app()
        if app.user:
            self.ids.info_label.text = f"{app.user['username']}  ({app.user['role']})"
        else:
            self.ids.info_label.text = 'Browsing as guest'

    def change_password(self):
        old = self.ids.old_password.text
        new = self.ids.new_password.text
        if not old or not new:
            toast('Fill in both password fields')
            return
        api.change_password(old, new, self._changed, lambda m: toast(m))

    def _changed(self, result):
        toast('Password changed')
        self.ids.old_password.text = ''
        self.ids.new_password.text = ''

    def logout(self):
        api.token = None
        App.get_running_app().user = None
        App.get_running_app().show_login()


Builder.load_string('''
<ProfileScreen>:
    name: 'profile'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        MDTopAppBar:
            title: 'Profile'
            elevation: 0
            md_bg_color: 0.10, 0.10, 0.13, 1
            left_action_items: [['arrow-left', lambda x: app.go_home()]]
        MDBoxLayout:
            orientation: 'vertical'
            padding: dp(20)
            spacing: dp(14)
            MDLabel:
                id: info_label
                text: ''
                font_style: 'H6'
                halign: 'center'
                size_hint_y: None
                height: dp(40)
            MDRaisedButton:
                text: 'Log Out / Switch Account'
                pos_hint: {'center_x': 0.5}
                on_release: root.logout()
            Widget:
                size_hint_y: None
                height: dp(10)
            MDLabel:
                text: 'Change password'
                bold: True
                size_hint_y: None
                height: dp(28)
            MDTextField:
                id: old_password
                hint_text: 'Current password'
                password: True
            MDTextField:
                id: new_password
                hint_text: 'New password'
                password: True
            MDRaisedButton:
                text: 'Update Password'
                pos_hint: {'center_x': 0.5}
                on_release: root.change_password()
            Widget:
''')


# --------------------------------------------------------------------------- #
# Bottom navigation bar (kept simple/manual for cross-version robustness)
# --------------------------------------------------------------------------- #

class RootScreen(MDScreen):
    """Holds the ScreenManager plus a bottom nav bar that adapts to role."""
    pass


Builder.load_string('''
<RootScreen>:
    name: 'root'
    MDBoxLayout:
        orientation: 'vertical'
        md_bg_color: 0.06, 0.06, 0.08, 1
        ScreenManager:
            id: sm
        MDBoxLayout:
            id: bottom_nav
            size_hint_y: None
            height: dp(56)
            md_bg_color: 0.10, 0.10, 0.13, 1
''')


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class NovelBridgeApp(MDApp):
    user = ObjectProperty(None, allownone=True)

    def build(self):
        self.title = 'Novel Bridge'
        self.theme_cls.theme_style = 'Dark'
        self.theme_cls.primary_palette = 'Blue'
        Window.minimum_width, Window.minimum_height = (400, 640)
        Clock.max_iteration = 40  # headroom in case any layout needs a few extra passes

        self.root_screen = RootScreen()
        self.sm = self.root_screen.ids.sm
        self.sm.transition = SlideTransition(duration=0.16)

        self.login_screen = LoginScreen()
        self.home_screen = HomeScreen()
        self.novel_detail_screen = NovelDetailScreen()
        self.reader_screen = ReaderScreen()
        self.writer_screen = WriterScreen()
        self.upload_chapter_screen = UploadChapterScreen()
        self.admin_screen = AdminScreen()
        self.user_management_screen = UserManagementScreen()
        self.profile_screen = ProfileScreen()

        for s in (self.login_screen, self.home_screen, self.novel_detail_screen,
                  self.reader_screen, self.writer_screen, self.upload_chapter_screen,
                  self.admin_screen, self.user_management_screen, self.profile_screen):
            self.sm.add_widget(s)

        self._history = []  # simple back-stack for generic screens
        self.sm.current = 'login'
        return self.root_screen

    # -- navigation helpers --------------------------------------------------
    def on_authenticated(self):
        self._build_bottom_nav()
        self.go_home()

    def _build_bottom_nav(self):
        bar = self.root_screen.ids.bottom_nav
        bar.clear_widgets()

        items = [('home', self.go_home)]
        if self.user and self.user['role'] in ('writer', 'admin', 'owner'):
            items.append(('pencil', self.open_writer))
        if self.user and self.user['role'] in ('admin', 'owner'):
            items.append(('shield-check', self.open_admin))
        items.append(('account', self.open_profile))

        # size_hint=(1, 1) on every button makes the MDBoxLayout (horizontal)
        # split its full width evenly between them, instead of each button
        # taking only its natural (small) size and leaving the rest of the
        # bar empty on the right.
        for icon, callback in items:
            bar.add_widget(MDIconButton(
                icon=icon,
                size_hint=(1, 1),
                pos_hint={'center_y': 0.5},
                on_release=lambda *_, cb=callback: cb(),
            ))

    def show_login(self):
        self.root_screen.ids.bottom_nav.clear_widgets()
        self.sm.current = 'login'

    def go_home(self):
        self.sm.transition.direction = 'right'
        # Just switch screens - HomeScreen.on_pre_enter() already calls
        # refresh() whenever it becomes the current screen, so calling it
        # here too fired GET /api/novels twice on every navigation to home.
        self.sm.current = 'home'

    def open_novel_detail(self, novel_id):
        self.sm.transition.direction = 'left'
        self.novel_detail_screen.load(novel_id)
        self.sm.current = 'novel_detail'

    def open_reader(self, chapter_id, novel_data=None):
        self.sm.transition.direction = 'left'
        self.reader_screen.load(chapter_id, novel_data)
        self.sm.current = 'reader'

    def go_back_from_reader(self):
        self.sm.transition.direction = 'right'
        if self.reader_screen.novel_data:
            self.novel_detail_screen.load(self.reader_screen.novel_data['id'])
            self.sm.current = 'novel_detail'
        else:
            self.go_home()

    def open_writer(self):
        self.sm.transition.direction = 'left'
        self.sm.current = 'writer'

    def open_upload_chapter(self, novel_id):
        self.sm.transition.direction = 'left'
        self.upload_chapter_screen.open_for(novel_id)
        self.sm.current = 'upload_chapter'

    def go_back_generic(self):
        self.sm.transition.direction = 'right'
        self.sm.current = 'writer' if self.user and self.user['role'] in ('writer', 'admin', 'owner') else 'home'
        self.writer_screen.refresh_stats()

    def open_admin(self):
        self.sm.transition.direction = 'left'
        self.sm.current = 'admin'

    def open_user_management(self):
        self.sm.transition.direction = 'left'
        self.sm.current = 'user_management'

    def open_profile(self):
        self.sm.transition.direction = 'left'
        self.sm.current = 'profile'


if __name__ == '__main__':
    NovelBridgeApp().run()
