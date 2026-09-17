"""
Novel Bridge - backend server (Supabase edition, optimized)
=============================================================

Same API as before, but the database and file storage are now a free
Supabase project instead of local SQLite + a local uploads/ folder. That
means the Flask server itself is fully stateless - handy since Hostinger
(or most cheap hosts) don't guarantee a persistent disk across redeploys.

PERFORMANCE / STABILITY CHANGES vs the original version
---------------------------------------------------------
1. Session/user lookups (used on nearly every request) are cached
   in-memory for a few seconds, instead of hitting Supabase with 2
   sequential queries on every single API call.
2. HTTP keep-alive/connection pooling to Supabase is restored (it was
   fully disabled before), except on Windows dev machines where that
   caused a flaky-socket bug - see _build_http_client().
3. Chapter/novel view counters are no longer written synchronously on
   every read. They're buffered in memory and flushed in a batch every
   ~15s via a single atomic DB call (see bump_chapter_views in
   optimizations.sql). This removes a read-then-write race condition
   and turns "N reads = N writes" into "N reads = ~1 write per 15s".
4. writer_stats() no longer does one chapters query per novel (N+1) -
   it does exactly 2 queries total regardless of how many novels a
   writer has.
5. /api/novels supports pagination (page/per_page) and the most common
   query (public, first page, no search) is cached for a few seconds,
   since it's hit on every single Home tab open by every user.
6. Old sessions are now cleaned up automatically on a background timer
   so the `sessions` table doesn't grow forever.
7. Basic rate limiting on login/register (and a light global default)
   to blunt spam/abuse. This is optional - if flask-limiter isn't
   installed, the server runs fine without it.

Run `optimizations.sql` in the Supabase SQL editor once (see that file)
to get the full benefit of #3 and #6. The server also works without it
(it detects missing columns/functions and falls back to the old,
slower-but-correct behavior), so you can deploy this file first and run
the SQL whenever convenient.

One-time setup
---------------
1. Create a free project at https://supabase.com.
2. Project -> SQL Editor -> paste & run schema.sql (creates the tables and
   a public "thumbnails" storage bucket), then also run optimizations.sql.
3. Project -> Settings -> API. Copy:
     - "Project URL"            -> SUPABASE_URL
     - "service_role" secret key -> SUPABASE_SERVICE_KEY
   NEVER put the service_role key in the Kivy client or anywhere public -
   it bypasses Row Level Security. It only ever lives on this server.
4. Set both as environment variables before running:
     export SUPABASE_URL=https://xxxx.supabase.co
     export SUPABASE_SERVICE_KEY=eyJ...
5. pip install -r requirements.txt
   (optionally also: pip install flask-limiter  -> enables rate limiting)
6. python app.py
   This prints a default owner login (owner / change-me-now) the first
   time it finds no owner account - log in and change it immediately via
   POST /api/me/password.

Deploying on Hostinger (or any host)
-------------------------------------
Because state now lives in Supabase, deploying is just: upload server/,
set the two env vars above in your host's environment/panel, install
requirements, and run behind gunicorn with a few workers/threads so
requests don't queue behind each other:
    gunicorn -w 4 --threads 2 --timeout 60 -b 127.0.0.1:8000 app:app
No persistent disk needed - redeploys/restarts are safe.
"""

import os
import re
import time
import secrets
import platform
import datetime
import threading
from collections import defaultdict
from pathlib import Path
from functools import wraps

import httpx
from flask import Flask, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from supabase import create_client, Client, ClientOptions

from dotenv import load_dotenv
load_dotenv()

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    _HAS_LIMITER = True
except ImportError:
    _HAS_LIMITER = False

SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_SERVICE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        'Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables '
        '(see the setup steps at the top of this file).'
    )


def _build_http_client():
    """httpx's HTTP/2 transport (used by default by the supabase client)
    has a known flaky-socket bug on Windows dev machines - it intermittently
    raises `httpx.ReadError: [WinError 10035] A non-blocking socket operation
    could not be completed immediately` when a pooled keep-alive connection
    is reused. Forcing HTTP/1.1 avoids it everywhere.

    The *original* fix for this also disabled connection keep-alive
    entirely (max_keepalive_connections=0), which "fixed" the Windows bug
    but meant every single request to Supabase - on every deployed server,
    Windows or not - opened a brand new TCP+TLS connection from scratch.
    Under real traffic that's a lot of unnecessary handshake overhead and
    a good way to make the server feel like it's melting under load.

    Here we only disable keep-alive on Windows (where the bug actually
    happens) and use a real connection pool everywhere else.
    """
    is_windows = platform.system() == 'Windows'
    limits = httpx.Limits(
        max_keepalive_connections=0 if is_windows else 20,
        max_connections=100,
        keepalive_expiry=30,
    )
    return httpx.Client(http2=False, limits=limits)


_http_client = _build_http_client()
sb: Client = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_KEY,
    options=ClientOptions(httpx_client=_http_client),
)

ALLOWED_THUMB_EXT = {'.png', '.jpg', '.jpeg', '.webp'}
ALLOWED_CHAPTER_EXT = {'.txt', '.md'}
ROLES = ('owner', 'admin', 'writer', 'user')
THUMB_BUCKET = 'thumbnails'
SESSION_LIFETIME_DAYS = 30

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB per request

# --------------------------------------------------------------------------- #
# Optional rate limiting - protects login/register (and everything else, at
# a looser default) from being hammered. Works fine without flask-limiter
# installed; it just means requests aren't rate-limited.
# --------------------------------------------------------------------------- #

if _HAS_LIMITER:
    limiter = Limiter(get_remote_address, app=app, default_limits=['200 per minute'])
else:
    limiter = None
    print('[startup] flask-limiter not installed - running without rate limiting. '
          'pip install flask-limiter to enable it.')


def rate_limit(spec):
    def deco(fn):
        return limiter.limit(spec)(fn) if limiter else fn
    return deco


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def init_owner():
    """Create the default owner account the very first time this runs."""
    existing = sb.table('users').select('id').eq('role', 'owner').limit(1).execute()
    if not existing.data:
        sb.table('users').insert({
            'username': 'owner',
            'password_hash': generate_password_hash('change-me-now'),
            'role': 'owner',
        }).execute()
        print("Created default owner account -> username: owner / password: change-me-now")
        print("Please log in and change this password immediately.")


# --------------------------------------------------------------------------- #
# Session/user cache
# --------------------------------------------------------------------------- #
# current_user() is called on nearly every request. Without this cache it
# means 2 sequential Supabase queries (sessions, then users) per request,
# for every request, all day. A short-lived cache turns that into roughly
# 2 queries per active user every SESSION_CACHE_TTL seconds instead.
#
# Trade-off: a role change (set_role) or account edit can take up to
# SESSION_CACHE_TTL seconds to be reflected for a currently-logged-in user.
# That's an acceptable trade for a reader/writer app; lower the TTL if you
# need tighter guarantees.

SESSION_CACHE_TTL = 20  # seconds
_session_cache = {}
_session_cache_lock = threading.Lock()


def _cache_get_user(token):
    with _session_cache_lock:
        entry = _session_cache.get(token)
        if not entry:
            return None
        user, expires_at = entry
        if expires_at < time.monotonic():
            del _session_cache[token]
            return None
        return user


def _cache_put_user(token, user):
    with _session_cache_lock:
        _session_cache[token] = (user, time.monotonic() + SESSION_CACHE_TTL)


def _cache_drop_token(token):
    with _session_cache_lock:
        _session_cache.pop(token, None)


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #

def current_user():
    token = request.headers.get('Authorization', '')
    if token.startswith('Bearer '):
        token = token[7:]
    if not token:
        return None

    cached = _cache_get_user(token)
    if cached is not None:
        return cached

    session = sb.table('sessions').select('user_id').eq('token', token).limit(1).execute()
    if not session.data:
        return None
    user = sb.table('users').select('*').eq('id', session.data[0]['user_id']).limit(1).execute()
    if not user.data:
        return None

    result = user.data[0]
    _cache_put_user(token, result)
    return result


def require_role(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                return jsonify(error='authentication required'), 401
            if roles and user['role'] not in roles:
                return jsonify(error='insufficient privileges'), 403
            g.user = user
            return fn(*args, **kwargs)
        return wrapper
    return deco


def optional_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        g.user = current_user()
        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# Background jobs: expired-session cleanup + buffered view-count flushing
# --------------------------------------------------------------------------- #
# Both are daemon threads started once at import time, so they run whether
# the app is launched directly (python app.py) or imported by gunicorn.

def _cleanup_expired_sessions_loop():
    while True:
        time.sleep(3600)  # hourly is plenty
        try:
            sb.table('sessions').delete().lt('expires_at', now_iso()).execute()
        except Exception as e:
            # optimizations.sql not run yet (no expires_at column) - skip quietly.
            print(f'[session cleanup] skipped ({e})')


_view_buffer = defaultdict(int)
_view_buffer_lock = threading.Lock()
VIEW_FLUSH_INTERVAL = 15  # seconds


def _queue_chapter_view(chapter_id):
    """Record a view in memory instead of writing to the DB immediately."""
    with _view_buffer_lock:
        _view_buffer[chapter_id] += 1


def _flush_views_loop():
    while True:
        time.sleep(VIEW_FLUSH_INTERVAL)
        with _view_buffer_lock:
            pending = dict(_view_buffer)
            _view_buffer.clear()
        for chapter_id, amount in pending.items():
            try:
                # Atomic, single round trip - see bump_chapter_views() in
                # optimizations.sql. Bumps both the chapter's and its
                # parent novel's view count in one DB call.
                sb.rpc('bump_chapter_views', {
                    'p_chapter_id': chapter_id, 'p_amount': amount,
                }).execute()
            except Exception:
                # optimizations.sql not run yet - fall back to the old
                # (less atomic, but still correct-enough) read-then-write.
                try:
                    chapter = sb.table('chapters').select('views, novel_id') \
                        .eq('id', chapter_id).limit(1).execute()
                    if not chapter.data:
                        continue
                    row = chapter.data[0]
                    sb.table('chapters').update(
                        {'views': row['views'] + amount}
                    ).eq('id', chapter_id).execute()
                    novel = sb.table('novels').select('views') \
                        .eq('id', row['novel_id']).limit(1).execute()
                    if novel.data:
                        sb.table('novels').update(
                            {'views': novel.data[0]['views'] + amount}
                        ).eq('id', row['novel_id']).execute()
                except Exception as e2:
                    print(f'[view flush] failed for chapter {chapter_id}: {e2}')


threading.Thread(target=_cleanup_expired_sessions_loop, daemon=True).start()
threading.Thread(target=_flush_views_loop, daemon=True).start()


# --------------------------------------------------------------------------- #
# Public novel-list cache
# --------------------------------------------------------------------------- #
# The single most-hit query in this whole app: every user, every time they
# open the Home tab, with no filters. Cache it briefly instead of asking
# Supabase to re-run the same query over and over within a few seconds of
# itself.

NOVELS_CACHE_TTL = 20  # seconds
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 50
_novels_cache_lock = threading.Lock()
_novels_cache = {'data': None, 'expires_at': 0.0}


def _invalidate_novels_cache():
    with _novels_cache_lock:
        _novels_cache['data'] = None
        _novels_cache['expires_at'] = 0.0


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

def novel_public(row):
    return {
        'id': row['id'],
        'title': row['title'],
        'description': row['description'],
        'thumbnail_url': row.get('thumbnail_url') or '',
        'tags': [t for t in (row['tags'] or '').split(',') if t],
        'status': row['status'],
        'approved': bool(row['approved']),
        'rejected': bool(row['rejected']),
        'views': row['views'],
        'writer_id': row['writer_id'],
        'created_at': row['created_at'],
    }


def chapter_public(row, include_content=False):
    d = {
        'id': row['id'],
        'novel_id': row['novel_id'],
        'chapter_number': row['chapter_number'],
        'title': row['title'],
        'approved': bool(row['approved']),
        'rejected': bool(row['rejected']),
        'views': row['views'],
        'created_at': row['created_at'],
    }
    if include_content:
        d['content'] = row.get('content', '')
    return d


def can_see_unapproved(user, novel_row):
    if user is None:
        return False
    if user['role'] in ('owner', 'admin'):
        return True
    if user['role'] == 'writer' and user['id'] == novel_row['writer_id']:
        return True
    return False


# --------------------------------------------------------------------------- #
# Auth endpoints
# --------------------------------------------------------------------------- #

@app.post('/api/register')
@rate_limit('10 per minute')
def register():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username or not password:
        return jsonify(error='username and password required'), 400
    if not re.match(r'^[A-Za-z0-9_.-]{3,32}$', username):
        return jsonify(error='username must be 3-32 chars: letters, numbers, _ . -'), 400
    if len(password) < 6:
        return jsonify(error='password must be at least 6 characters'), 400

    existing = sb.table('users').select('id').eq('username', username).limit(1).execute()
    if existing.data:
        return jsonify(error='username already taken'), 409

    sb.table('users').insert({
        'username': username,
        'password_hash': generate_password_hash(password),
        'role': 'user',
    }).execute()
    return jsonify(message='registered, please log in'), 201


@app.post('/api/login')
@rate_limit('15 per minute')
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    result = sb.table('users').select('*').eq('username', username).limit(1).execute()
    user = result.data[0] if result.data else None
    if user is None or not check_password_hash(user['password_hash'], password):
        return jsonify(error='invalid username or password'), 401
    token = secrets.token_hex(32)
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=SESSION_LIFETIME_DAYS)
    ).isoformat()
    try:
        sb.table('sessions').insert({
            'token': token, 'user_id': user['id'], 'expires_at': expires_at,
        }).execute()
    except Exception:
        # optimizations.sql not run yet - sessions table has no expires_at
        # column. Fall back so login still works; run the SQL when you can
        # so old sessions get cleaned up automatically.
        sb.table('sessions').insert({'token': token, 'user_id': user['id']}).execute()
    return jsonify(token=token, user={'id': user['id'], 'username': user['username'], 'role': user['role']})


@app.post('/api/logout')
@require_role()
def logout():
    token = request.headers.get('Authorization', '').replace('Bearer ', '')
    sb.table('sessions').delete().eq('token', token).execute()
    _cache_drop_token(token)
    return jsonify(message='logged out')


@app.get('/api/me')
@require_role()
def me():
    u = g.user
    return jsonify(id=u['id'], username=u['username'], role=u['role'])


@app.post('/api/me/password')
@require_role()
def change_password():
    data = request.get_json(force=True, silent=True) or {}
    old = data.get('old_password') or ''
    new = data.get('new_password') or ''
    if not check_password_hash(g.user['password_hash'], old):
        return jsonify(error='old password incorrect'), 400
    if len(new) < 6:
        return jsonify(error='new password must be at least 6 characters'), 400
    sb.table('users').update({'password_hash': generate_password_hash(new)}).eq('id', g.user['id']).execute()
    return jsonify(message='password changed')


@app.post('/api/users/<int:user_id>/role')
@require_role('owner')
def set_role(user_id):
    data = request.get_json(force=True, silent=True) or {}
    role = data.get('role')
    if role not in ROLES:
        return jsonify(error=f'role must be one of {ROLES}'), 400
    if role == 'owner':
        return jsonify(error='cannot grant owner role'), 400
    sb.table('users').update({'role': role}).eq('id', user_id).execute()
    return jsonify(message='role updated')


@app.get('/api/users')
@require_role('owner', 'admin')
def list_users():
    result = sb.table('users').select('id, username, role, created_at').order('id').execute()
    return jsonify(result.data)


# --------------------------------------------------------------------------- #
# Novels
# --------------------------------------------------------------------------- #

@app.get('/api/novels')
@optional_auth
def list_novels():
    mine = request.args.get('mine') == '1'
    pending = request.args.get('pending') == '1'
    search = (request.args.get('q') or '').strip()

    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    try:
        per_page = min(MAX_PAGE_SIZE, max(1, int(request.args.get('per_page', DEFAULT_PAGE_SIZE))))
    except ValueError:
        per_page = DEFAULT_PAGE_SIZE
    offset = (page - 1) * per_page

    # Only the plain "public, first page, default size" query is cache-eligible -
    # that's the one every user hits just by opening the app.
    cacheable = not mine and not pending and not search and page == 1 and per_page == DEFAULT_PAGE_SIZE
    if cacheable:
        with _novels_cache_lock:
            if _novels_cache['data'] is not None and _novels_cache['expires_at'] > time.monotonic():
                return jsonify(_novels_cache['data'])

    if mine:
        if g.user is None or g.user['role'] not in ('writer', 'admin', 'owner'):
            return jsonify(error='authentication required'), 401
        result = sb.table('novels').select('*').eq('writer_id', g.user['id']) \
            .order('created_at', desc=True).range(offset, offset + per_page - 1).execute()
    elif pending:
        if g.user is None or g.user['role'] not in ('admin', 'owner'):
            return jsonify(error='insufficient privileges'), 403
        result = sb.table('novels').select('*').eq('approved', False).eq('rejected', False) \
            .order('created_at').range(offset, offset + per_page - 1).execute()
    else:
        q = sb.table('novels').select('*').eq('approved', True)
        if search:
            q = q.ilike('title', f'%{search}%')
        result = q.order('created_at', desc=True).range(offset, offset + per_page - 1).execute()

    data = [novel_public(r) for r in result.data]

    if cacheable:
        with _novels_cache_lock:
            _novels_cache['data'] = data
            _novels_cache['expires_at'] = time.monotonic() + NOVELS_CACHE_TTL

    return jsonify(data)


@app.get('/api/novels/<int:novel_id>')
@optional_auth
def get_novel(novel_id):
    result = sb.table('novels').select('*').eq('id', novel_id).limit(1).execute()
    if not result.data:
        return jsonify(error='not found'), 404
    row = result.data[0]
    if not row['approved'] and not can_see_unapproved(g.user, row):
        return jsonify(error='not found'), 404

    chapters_q = sb.table('chapters').select('*').eq('novel_id', novel_id)
    if not can_see_unapproved(g.user, row):
        chapters_q = chapters_q.eq('approved', True)
    chapters = chapters_q.order('chapter_number').execute().data

    data = novel_public(row)
    data['chapters'] = [chapter_public(c) for c in chapters]

    writer = sb.table('users').select('username').eq('id', row['writer_id']).limit(1).execute()
    data['writer_username'] = writer.data[0]['username'] if writer.data else 'unknown'
    return jsonify(data)


def _upload_thumbnail(file_storage):
    """Uploads to the Supabase 'thumbnails' bucket, returns a public URL."""
    ext = Path(file_storage.filename).suffix.lower()
    if ext not in ALLOWED_THUMB_EXT:
        raise ValueError(f'thumbnail must be one of {ALLOWED_THUMB_EXT}')
    name = f'{secrets.token_hex(8)}{ext}'
    content_type = file_storage.mimetype or 'image/png'
    sb.storage.from_(THUMB_BUCKET).upload(
        name, file_storage.read(), {'content-type': content_type}
    )
    return sb.storage.from_(THUMB_BUCKET).get_public_url(name)


@app.post('/api/novels')
@require_role('writer', 'admin', 'owner')
def create_novel():
    title = (request.form.get('title') or '').strip()
    description = request.form.get('description') or ''
    tags = request.form.get('tags') or ''
    status = request.form.get('status') or 'ongoing'
    if status not in ('ongoing', 'completed'):
        status = 'ongoing'
    if not title:
        return jsonify(error='title required'), 400

    thumbnail_url = ''
    file = request.files.get('thumbnail')
    if file and file.filename:
        try:
            thumbnail_url = _upload_thumbnail(file)
        except ValueError as e:
            return jsonify(error=str(e)), 400

    result = sb.table('novels').insert({
        'writer_id': g.user['id'],
        'title': title,
        'description': description,
        'thumbnail_url': thumbnail_url,
        'tags': tags,
        'status': status,
        'approved': False,
        'rejected': False,
        'views': 0,
    }).execute()
    _invalidate_novels_cache()
    return jsonify(id=result.data[0]['id'], message='submitted for admin approval'), 201


@app.put('/api/novels/<int:novel_id>')
@require_role('writer', 'admin', 'owner')
def update_novel(novel_id):
    existing = sb.table('novels').select('*').eq('id', novel_id).limit(1).execute()
    if not existing.data:
        return jsonify(error='not found'), 404
    row = existing.data[0]
    if g.user['role'] == 'writer' and row['writer_id'] != g.user['id']:
        return jsonify(error='insufficient privileges'), 403

    data = request.get_json(force=True, silent=True) or {}
    updates = {}
    for key in ('title', 'description', 'tags'):
        if key in data:
            updates[key] = data[key]
    if 'status' in data:
        if data['status'] not in ('ongoing', 'completed'):
            return jsonify(error='status must be ongoing or completed'), 400
        updates['status'] = data['status']
    if not updates:
        return jsonify(error='nothing to update'), 400
    sb.table('novels').update(updates).eq('id', novel_id).execute()
    _invalidate_novels_cache()
    return jsonify(message='updated')


@app.post('/api/novels/<int:novel_id>/thumbnail')
@require_role('writer', 'admin', 'owner')
def update_thumbnail(novel_id):
    existing = sb.table('novels').select('*').eq('id', novel_id).limit(1).execute()
    if not existing.data:
        return jsonify(error='not found'), 404
    row = existing.data[0]
    if g.user['role'] == 'writer' and row['writer_id'] != g.user['id']:
        return jsonify(error='insufficient privileges'), 403
    file = request.files.get('thumbnail')
    if not file or not file.filename:
        return jsonify(error='thumbnail file required'), 400
    try:
        thumbnail_url = _upload_thumbnail(file)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    sb.table('novels').update({'thumbnail_url': thumbnail_url}).eq('id', novel_id).execute()
    _invalidate_novels_cache()
    return jsonify(message='thumbnail updated')


# --------------------------------------------------------------------------- #
# Chapters
# --------------------------------------------------------------------------- #

@app.post('/api/novels/<int:novel_id>/chapters')
@require_role('writer', 'admin', 'owner')
def create_chapter(novel_id):
    novel = sb.table('novels').select('*').eq('id', novel_id).limit(1).execute()
    if not novel.data:
        return jsonify(error='novel not found'), 404
    novel_row = novel.data[0]
    if g.user['role'] == 'writer' and novel_row['writer_id'] != g.user['id']:
        return jsonify(error='insufficient privileges'), 403

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify(error='.txt or .md file required'), 400
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_CHAPTER_EXT:
        return jsonify(error=f'chapter file must be one of {ALLOWED_CHAPTER_EXT}'), 400

    try:
        chapter_number = float(request.form.get('chapter_number', '0'))
    except ValueError:
        return jsonify(error='chapter_number must be a number'), 400
    title = request.form.get('title') or f'Chapter {chapter_number:g}'
    content = file.read().decode('utf-8', errors='replace')

    result = sb.table('chapters').insert({
        'novel_id': novel_id,
        'chapter_number': chapter_number,
        'title': title,
        'content': content,
        'approved': False,
        'rejected': False,
        'views': 0,
    }).execute()
    return jsonify(id=result.data[0]['id'], message='chapter submitted for admin approval'), 201


@app.get('/api/chapters/<int:chapter_id>')
@optional_auth
def get_chapter(chapter_id):
    result = sb.table('chapters').select('*').eq('id', chapter_id).limit(1).execute()
    if not result.data:
        return jsonify(error='not found'), 404
    row = result.data[0]
    novel = sb.table('novels').select('*').eq('id', row['novel_id']).limit(1).execute().data[0]
    allowed = row['approved'] and novel['approved']
    if not allowed and not can_see_unapproved(g.user, novel):
        return jsonify(error='not found'), 404
    if allowed:
        # Buffered instead of written straight away - see _flush_views_loop().
        # The response below is optimistic (shows the view that was just
        # made) even though the DB write itself happens a little later.
        _queue_chapter_view(chapter_id)
        row['views'] += 1
    return jsonify(chapter_public(row, include_content=True))


# --------------------------------------------------------------------------- #
# Admin approval queue
# --------------------------------------------------------------------------- #

@app.get('/api/admin/pending')
@require_role('admin', 'owner')
def pending_queue():
    novels = sb.table('novels').select('*').eq('approved', False).eq('rejected', False) \
        .order('created_at').execute().data
    chapters = sb.table('chapters').select('*').eq('approved', False).eq('rejected', False) \
        .order('created_at').execute().data

    novel_titles = {}
    if chapters:
        novel_ids = list({c['novel_id'] for c in chapters})
        rows = sb.table('novels').select('id, title').in_('id', novel_ids).execute().data
        novel_titles = {r['id']: r['title'] for r in rows}

    return jsonify(
        novels=[novel_public(r) for r in novels],
        chapters=[dict(chapter_public(c), novel_title=novel_titles.get(c['novel_id'], '?'))
                  for c in chapters],
    )


@app.post('/api/admin/novels/<int:novel_id>/approve')
@require_role('admin', 'owner')
def approve_novel(novel_id):
    sb.table('novels').update({'approved': True, 'rejected': False}).eq('id', novel_id).execute()
    _invalidate_novels_cache()
    return jsonify(message='novel approved')


@app.post('/api/admin/novels/<int:novel_id>/reject')
@require_role('admin', 'owner')
def reject_novel(novel_id):
    sb.table('novels').update({'approved': False, 'rejected': True}).eq('id', novel_id).execute()
    _invalidate_novels_cache()
    return jsonify(message='novel rejected')


@app.post('/api/admin/chapters/<int:chapter_id>/approve')
@require_role('admin', 'owner')
def approve_chapter(chapter_id):
    sb.table('chapters').update({'approved': True, 'rejected': False}).eq('id', chapter_id).execute()
    return jsonify(message='chapter approved')


@app.post('/api/admin/chapters/<int:chapter_id>/reject')
@require_role('admin', 'owner')
def reject_chapter(chapter_id):
    sb.table('chapters').update({'approved': False, 'rejected': True}).eq('id', chapter_id).execute()
    return jsonify(message='chapter rejected')


# --------------------------------------------------------------------------- #
# Writer stats
# --------------------------------------------------------------------------- #

@app.get('/api/writer/stats')
@require_role('writer', 'admin', 'owner')
def writer_stats():
    novels = sb.table('novels').select('*').eq('writer_id', g.user['id']) \
        .order('created_at', desc=True).execute().data
    if not novels:
        return jsonify([])

    # One query for ALL chapters across every novel this writer has,
    # instead of one query per novel (that was the N+1 here before).
    novel_ids = [n['id'] for n in novels]
    all_chapters = sb.table('chapters').select('novel_id, approved, rejected, views') \
        .in_('novel_id', novel_ids).execute().data

    chapters_by_novel = defaultdict(list)
    for c in all_chapters:
        chapters_by_novel[c['novel_id']].append(c)

    out = []
    for n in novels:
        chapters = chapters_by_novel.get(n['id'], [])
        out.append({
            **novel_public(n),
            'chapter_count': len(chapters),
            'approved_chapter_count': sum(1 for c in chapters if c['approved']),
            'pending_chapter_count': sum(1 for c in chapters if not c['approved'] and not c['rejected']),
            'total_chapter_views': sum(c['views'] for c in chapters),
        })
    return jsonify(out)


@app.get('/api/health')
def health():
    return jsonify(status='ok', time=now_iso())


if __name__ == '__main__':
    init_owner()
    debug_mode = os.environ.get('FLASK_DEBUG', '0') == '1'
    # threaded=True so two near-simultaneous requests (e.g. the client
    # firing two GET /api/novels back to back) don't queue behind each
    # other on a single worker thread - which was making the stale-socket
    # issue above much easier to trigger.
    app.run(host='0.0.0.0', port=8000, debug=debug_mode, threaded=True)
