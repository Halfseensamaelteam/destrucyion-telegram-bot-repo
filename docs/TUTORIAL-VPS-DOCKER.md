# Tutorial Deploy di VPS (Docker Compose)

Deploy destrucyion ke VPS Ubuntu dengan Docker Compose — termasuk fitur
restricted-content dari branch `temp/restricted-content-muse`.
Ditulis 2026-10-21. Berbeda dengan tutorial lokal: di VPS pakai Docker
penuh (Postgres + Redis container), migrasi jalan otomatis, dan download
Telegram lewat jaringan datacenter — jauh lebih cepat dari laptop rumah.

## 0. Prasyarat

- VPS Ubuntu 22.04/24.04, minimal 1 vCPU / 2 GB RAM (4 GB lebih nyaman),
  akses SSH dengan sudo.
- Domain `indevs.in` nameserver-nya sudah di Cloudflare (untuk tunnel).
- Nilai-nilai `.env` dari laptop kamu (BOT_TOKEN, TELEGRAM_API_ID/HASH,
  dll.) — file `.env` lokalmu bisa disalin isinya.

## 1. Install Docker + cloudflared (sekali saja)

SSH ke VPS, lalu:

```bash
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo usermod -aG docker $USER
# keluar-masuk SSH agar grup docker berlaku, lalu:
docker compose version
```

cloudflared:

```bash
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb || sudo apt-get install -f -y
cloudflared --version
```

(Untuk VPS ARM seperti Oracle Ampere, ganti `amd64` dengan `arm64`.
Repo ini juga menyediakan `bootstrap-vps.sh` yang melakukan kedua
install di atas sekaligus.)

## 2. Ambil kode (branch fitur, BUKAN main)

```bash
git clone -b temp/restricted-content-muse https://github.com/Halfseensamaelteam/destrucyion-telegram-bot-repo.git
cd destrucyion-telegram-bot-repo
```

## 3. Isi .env

```bash
cp .env.example .env
nano .env
```

Isi minimal:

- `BOT_TOKEN` — dari @BotFather
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` — dari my.telegram.org
- `ADMIN_TELEGRAM_IDS` — ID Telegram kamu
- `SESSION_ENCRYPTION_KEY` — generate: `openssl rand -hex 32`
  (untuk deploy fresh ini tidak harus sama dengan yang di laptop/Neon;
  samakan HANYA kalau mau memakai ulang sesi dari database Neon)
- `APP_SECRET_KEY` — `openssl rand -hex 32`
- `DB_PASSWORD` — password kuat buatanmu sendiri (dipakai Postgres container)
- Kredensial payment sesuai kebutuhan (Midtrans/DOKU/NOWPayments)

## 4. WAJIB: perbaiki docker-compose.yml sebelum jalan

Compose bawaan repo punya dua masalah untuk VPS publik:

1. Port Postgres (5432) dan Redis (6379) diterbitkan ke `0.0.0.0` —
   database bisa dijangkau dari internet, padahal password default-nya
   lemah. Hapus kedua blok `ports:` itu (service tetap saling terhubung
   lewat jaringan internal Compose).
2. Tidak ada `restart:` — VPS reboot berarti semua service mati
   permanen. Tambahkan `restart: unless-stopped` di setiap service.

Edit dengan `nano docker-compose.yml`:

- Di service `postgres`: hapus blok
  ```yaml
      ports:
        - "5432:5432"
  ```
- Di service `redis`: hapus blok
  ```yaml
      ports:
        - "6379:6379"
  ```
- Tambahkan `restart: unless-stopped` tepat di bawah nama tiap service
  (`postgres`, `redis`, `api`, `bot`, `worker`).
- Port `8000` pada service `api` boleh tetap (dipakai tunnel), atau
  batasi ke localhost: `"127.0.0.1:8000:8000"` — lebih aman, tunnel
  tetap jalan karena cloudflared berjalan di host yang sama.

## 5. Jalankan

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f api bot worker
```

Service `api` menjalankan `alembic upgrade head` otomatis saat start
(lihat docker-entrypoint.sh), jadi tabel `link_requests` dari branch ini
terbuat sendiri — tidak perlu langkah migrasi manual.

## 6. Cloudflare Tunnel (URL publik)

Kerjakan pembuatan tunnel di laptop (butuh browser), ikuti
`TUNNEL-SETUP.md`: `cloudflared tunnel login` → `cloudflared tunnel
create visertunnel` → `cloudflared tunnel route dns visertunnel
app.viserlabs.indevs.in` → salin file JSON kredensial ke
`~/.cloudflared/` di VPS dan sesuaikan `~/.cloudflared/config.yml` dari
template `cloudflared-config.yml`.

Lalu di VPS, pasang tunnel sebagai **service** (BUKAN `cloudflared
tunnel run` di foreground — itu mati begitu SSH ditutup):

```bash
sudo cloudflared service install
sudo systemctl status cloudflared
```

Cek: buka https://app.viserlabs.indevs.in di browser.

## 7. Uji fitur restricted-content

Database container ini fresh, jadi sambungkan ulang akunmu:

1. Chat bot → `/connect` → selesaikan login nomor HP sampai akun
   berstatus connected.
2. Beri langganan: `/admin grant <ID Telegram kamu> lifetime`
   (kamu admin sesuai `ADMIN_TELEGRAM_IDS`).
3. Pastikan akunmu **member** channel private yang mau diuji.
4. Kirim: `/save https://t.me/c/<id>/<pesan>`
5. Pantau: `docker compose logs -f worker` — harus muncul
   `link_request_processor_started`, lalu
   `saved_messages_transfer_progress` (persen download, lalu upload),
   dan copy-nya mendarat di Saved Messages akunmu.

Di VPS, unduhan video yang di laptop butuh 4+ menit biasanya selesai
dalam hitungan detik–menit awal, tergantung ukuran file.

## 8. Operasional sehari-hari

```bash
docker compose logs -f worker          # pantau worker
docker compose restart bot worker      # restart sebagian
git pull && docker compose up -d --build   # update kode
docker compose down                    # matikan semua (data tetap)
```

## Opsi lanjutan: pakai database Neon yang sudah ada

Kalau tidak mau connect ulang akun dan ingin memakai data Neon
(ViserBotDeployments) apa adanya:

- Samakan `SESSION_ENCRYPTION_KEY` di `.env` VPS dengan yang dipakai
  mengenkripsi sesi di Neon, kalau tidak sesi akun tidak bisa dibuka.
- Di `docker-compose.yml`, ganti nilai `DATABASE_URL` pada service
  `api`, `bot`, `worker` (bagian `environment:`) dengan URL Neon-mu
  (`postgresql+asyncpg://...neon.tech/...?ssl=require`), lalu jalankan
  tanpa Postgres container: `docker compose up -d --no-deps redis api bot worker`.
- Migrasi Neon-mu sudah di head (sudah dijalankan saat trial laptop),
  jadi langkah ini aman.
