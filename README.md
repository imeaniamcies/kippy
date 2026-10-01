# Novel Bridge (Kippy) – Backend

Server backend REST API untuk platform baca-tulis novel, dibangun dengan **Flask** dan **Supabase** (database PostgreSQL + file storage). Server bersifat *stateless*, sehingga aman di-deploy di hosting yang tidak menjamin disk persisten (Hostinger, Vercel, dll).

## Fitur

- **Autentikasi & peran**: register, login (token Bearer, masa berlaku 30 hari), ganti password. Peran: `owner`, `admin`, `writer`, `user`.
- **Novel**: buat, edit, unggah thumbnail (`.png`, `.jpg`, `.jpeg`, `.webp`), pencarian judul, dan paginasi.
- **Bab (chapter)**: unggah file `.txt` / `.md`, nomor bab desimal didukung.
- **Moderasi**: novel dan bab baru harus disetujui `admin`/`owner` sebelum tampil publik.
- **Statistik penulis**: jumlah bab, bab pending, dan total views per novel.
- **Health check**: `GET /api/health`.

## Optimasi

- Cache sesi/user di memori (20 detik) untuk mengurangi query ke Supabase.
- Connection pooling HTTP/1.1 (keep-alive dimatikan hanya di Windows karena bug socket).
- View counter di-*buffer* di memori dan di-flush tiap ±15 detik lewat satu panggilan RPC atomik.
- `writer_stats()` hanya memakai 2 query (tanpa N+1).
- Daftar novel publik halaman pertama di-cache 20 detik.
- Sesi kedaluwarsa dibersihkan otomatis tiap jam.
- Rate limiting login/register via `flask-limiter` (opsional).

## Struktur Proyek

```
.
├── app.py             # Server Flask (seluruh endpoint)
├── requirements.txt   # Dependensi Python
├── vercel.json        # Konfigurasi Vercel (maxDuration 60 detik)
├── .python-version    # Python 3.12
└── .vercelignore      # File yang diabaikan saat deploy
```

> `schema.sql` dan `optimizations.sql` dirujuk oleh `app.py` untuk setup database. Pastikan keduanya tersedia di repo Anda.

## Instalasi

### 1. Siapkan Supabase

1. Buat proyek gratis di [supabase.com](https://supabase.com).
2. Buka **SQL Editor**, jalankan `schema.sql` (membuat tabel dan bucket publik `thumbnails`), lalu `optimizations.sql`.
3. Buka **Settings → API**, salin **Project URL** dan **service_role key**.

> ⚠️ Kunci `service_role` melewati Row Level Security. Jangan pernah dipakai di client (Kivy/mobile) atau dipublikasikan.

### 2. Konfigurasi environment

Buat file `.env`:

```env
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
# Opsional
FLASK_DEBUG=0
```

### 3. Jalankan

```bash
pip install -r requirements.txt
python app.py
```

Server berjalan di `http://0.0.0.0:8000`. Saat pertama kali dijalankan tanpa akun owner, akan dibuat akun default:

| Username | Password        |
|----------|-----------------|
| `owner`  | `change-me-now` |

**Segera ganti password** lewat `POST /api/me/password`.

## Deploy

### Gunicorn (Hostinger / VPS)

```bash
gunicorn -w 4 --threads 2 --timeout 60 -b 127.0.0.1:8000 app:app
```

Set `SUPABASE_URL` dan `SUPABASE_SERVICE_KEY` di panel environment hosting.

### Vercel

Konfigurasi sudah tersedia di `vercel.json`. Catatan: di lingkungan serverless, thread latar belakang (flush view counter dan pembersihan sesi) tidak dijamin berjalan terus. Untuk view counter yang akurat, gunakan host dengan proses persisten (gunicorn).

## Ringkasan API

Autentikasi memakai header `Authorization: Bearer <token>`.

### Auth & User

| Method | Endpoint | Akses | Keterangan |
|--------|----------|-------|------------|
| POST | `/api/register` | Publik | Daftar akun (10/menit) |
| POST | `/api/login` | Publik | Login, mengembalikan token (15/menit) |
| POST | `/api/logout` | Login | Hapus sesi |
| GET | `/api/me` | Login | Info akun saat ini |
| POST | `/api/me/password` | Login | Ganti password |
| GET | `/api/users` | Admin/Owner | Daftar user |
| POST | `/api/users/<id>/role` | Owner | Ubah peran user |

### Novel & Bab

| Method | Endpoint | Akses | Keterangan |
|--------|----------|-------|------------|
| GET | `/api/novels` | Publik | Daftar novel. Query: `q`, `page`, `per_page`, `mine=1`, `pending=1` |
| GET | `/api/novels/<id>` | Publik* | Detail novel + daftar bab |
| POST | `/api/novels` | Writer+ | Buat novel (form-data, `thumbnail` opsional) |
| PUT | `/api/novels/<id>` | Pemilik/Admin | Edit judul, deskripsi, tag, status |
| POST | `/api/novels/<id>/thumbnail` | Pemilik/Admin | Ganti thumbnail |
| POST | `/api/novels/<id>/chapters` | Pemilik/Admin | Unggah bab (`file`, `chapter_number`, `title`) |
| GET | `/api/chapters/<id>` | Publik* | Isi bab (menambah view) |

\*Konten yang belum disetujui hanya terlihat oleh pemilik, admin, dan owner.

### Admin

| Method | Endpoint | Keterangan |
|--------|----------|------------|
| GET | `/api/admin/pending` | Antrean novel & bab pending |
| POST | `/api/admin/novels/<id>/approve` | Setujui novel |
| POST | `/api/admin/novels/<id>/reject` | Tolak novel |
| POST | `/api/admin/chapters/<id>/approve` | Setujui bab |
| POST | `/api/admin/chapters/<id>/reject` | Tolak bab |

### Lainnya

| Method | Endpoint | Keterangan |
|--------|----------|------------|
| GET | `/api/writer/stats` | Statistik novel milik penulis |
| GET | `/api/health` | Cek status server |

## Teknologi

Python 3.12 · Flask · Supabase · httpx · Werkzeug · flask-limiter · Gunicorn
