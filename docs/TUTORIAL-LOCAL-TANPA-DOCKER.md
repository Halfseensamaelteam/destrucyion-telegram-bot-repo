# Tutorial Lokal TANPA Docker (untuk laptop ringan)

Menjalankan destrucyion + fitur restricted content di laptop tanpa Docker,
tanpa PostgreSQL, dan tanpa Redis:

- Database diganti **SQLite** (fallback resmi repo ini, lihat `docs/TERMUX.md`)
- **Redis tidak dipakai** — worker berjalan dalam mode single-worker fallback
  (bawaan `worker/lock.py`). Jangan menjalankan worker lebih dari satu.
- **FastAPI/API tidak dijalankan** — untuk mencoba fitur ini cukup bot + worker.
  Hemat RAM.

Sudah diverifikasi: worker start normal dengan `REDIS_URL` kosong dan
database SQLite, dan tabel `link_requests` terbuat lengkap.

> ⚠️ Jangan pakai `alembic upgrade head` di jalur SQLite ini — sebagian
> migrasi repo ditulis khusus PostgreSQL dan akan error di SQLite.
> Tabel dibuat dengan script `create_all` di langkah 5 (cara yang sama
> dengan yang dipakai test suite repo ini).

---

## 1. Syarat

- Python **3.12** (cek: `python --version`)
- Git
- Koneksi internet

## 2. Ambil kode & buat virtualenv

Windows (PowerShell / CMD):

```bat
git clone https://github.com/Halfseensamaelteam/destrucyion-telegram-bot-repo
cd destrucyion-telegram-bot-repo
git checkout temp/restricted-content-muse
python -m venv .venv
.venv\Scripts\activate
```

Linux/macOS: sama, kecuali aktivasi venv: `source .venv/bin/activate`

## 3. Install dependency (versi ringan)

```bash
pip install -e ".[sqlite]"
```

Ini hanya memasang dependency utama + driver SQLite — tanpa alat dev
(pytest/ruff/dll) supaya ringan.

## 4. Buat dan isi `.env`

```bash
copy .env.example .env        # Windows — Linux/macOS: cp
python scripts/generate_key.py
```

Isi `.env` dengan nilai berikut / sesuaikan:

```ini
APP_ENV=development
APP_SECRET_KEY=isi-bebas-untuk-lokal
DATABASE_URL=sqlite+aiosqlite:///./local.db
REDIS_URL=
BOT_TOKEN=<redacted>
ADMIN_TELEGRAM_IDS=<ID Telegram kamu, cek via @userinfobot>
SESSION_ENCRYPTION_KEY=<hasil generate_key.py di atas>
WEB_BASE_URL=http://localhost:8000
```

## 5. Buat tabel database (pengganti migrasi)

Simpan script ini sebagai `create_tables.py` di folder repo:

```python
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import get_settings
from app.db.models.base import Base
import app.db.models  # noqa: F401 — mendaftarkan semua model

async def main():
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
    print("OK - tabel dibuat:", sorted(Base.metadata.tables))

asyncio.run(main())
```

Lalu jalankan:

```bash
python create_tables.py
```

Harus muncul daftar tabel termasuk `link_requests`. File `local.db`
akan terbuat di folder repo.

## 6. Jalankan bot + worker (2 terminal)

Terminal 1:

```bat
.venv\Scripts\activate
python -m app.bot.main
```

Terminal 2:

```bat
.venv\Scripts\activate
python -m worker.main
```

Log worker yang normal di mode ini:

```
redis_not_configured — REDIS_URL is empty, running without Redis
account_lock_fallback — single-worker fallback mode
link_request_processor_started
```

## 7. Siapkan akun (sekali saja)

1. Chat bot kamu → join channel pengumuman yang diminta bot (gate bawaan),
   lalu `/connect` → hubungkan akun Telegram kamu (siapkan api_id/api_hash
   akun itu dari https://my.telegram.org). Pakai jalur QR/telepon di bot;
   login via browser (phone login web) butuh Redis, jadi tidak dipakai di
   mode ini.
2. Beri langganan ke diri sendiri (worker tidak menjalankan akun tanpa
   langganan aktif):
   `/admin grant <ID Telegram kamu> lifetime`
3. Pastikan akun Telegram yang terhubung **sudah jadi member** channel
   private restrict yang mau dicoba (join manual dari aplikasi Telegram).

## 8. Coba fiturnya

- Via bot: `/save https://t.me/c/1234567/890` → bot balas "Queued" →
  tunggu ±15 detik → copy muncul di **Saved Messages** akun kamu +
  konfirmasi di chat bot.
- Tanpa bot: buka Saved Messages → kirim `.get https://t.me/c/1234567/890`
  (range juga bisa: `.../100-110`).
- Otomatis: postingan baru dari channel restrict tersimpan sendiri selama
  worker jalan.
- Belum join tapi punya invite: `/save https://t.me/+xxxxx` dulu, baru
  kirim link postingannya.

## 9. Cek status / troubleshooting

Lihat antrean link di database:

```bash
python -c "import sqlite3; print(sqlite3.connect('local.db').execute('SELECT id, status, result_count, error FROM link_requests').fetchall())"
```

| Gejala | Penyebab & obat |
|---|---|
| Bot diam / menolak semua perintah | Belum join channel pengumuman bot — join dulu, lalu tap tombol verifikasi |
| `/save` balas "no connected account" | Belum `/connect`, atau akun terputus |
| Log worker `client_skipped_no_subscription` | Langganan belum aktif — ulangi `/admin grant ... lifetime` |
| Error "not a member" | Akun terhubung belum join channel private-nya |
| Error tabel `link_requests` tidak ada | Langkah 5 belum dijalankan |
| Worker jalan tapi link tidak diproses | Pastikan yang jalan branch ini dan terminal worker tidak tertutup |

## Catatan

- Mode SQLite + tanpa Redis ini untuk **mencoba di lokal saja**. Untuk
  produksi tetap pakai PostgreSQL + Redis + migrasi Alembic seperti
  panduan utama.
- File `local.db`, `.env`, dan `create_tables.py` jangan di-commit.
