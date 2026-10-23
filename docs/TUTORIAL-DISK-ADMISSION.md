# Tutorial Uji Coba: Size-Aware Disk Admission (Load Test Campuran)

Menguji fitur *disk admission* — worker menyewa ruang disk per request
sesuai UKURAN file-nya, dengan dua kelas (file kecil vs file besar) —
memakai skenario campuran persis kasus production: **400 user file
10–80 MB + 100 user file 300–900 MB**, di atas disk virtual 22 GB.
Ditulis 2026-10-23, untuk branch `temp/size-aware-disk-admission-muse`.

## 0. Prasyarat & PERINGATAN KERAS

- ⚠️ Uji coba HANYA dari clone uji coba terpisah
  (`~/muse/destrucyion-telegram-bot-repo`), JANGAN PERNAH dari folder
  production `~/destrucyion-telegram-bot-repo`. Kenapa: load test
  mengklaim SEMUA baris `link_requests` yang pending di database yang
  dituju — kalau itu DB production, request user asli akan "disimpan"
  oleh client palsu dan ditandai DONE.
- ⚠️ Selalu pakai DB scratch bernama `loadtest`. Kenapa: alasan yang
  sama — DB production tidak boleh tersentuh sama sekali.
- Clone uji coba harus sudah bisa jalan seperti di
  `docs/TUTORIAL-VPS-DOCKER.md` (Docker terpasang, `.env` terisi).
  File `.env` di clone uji coba boleh berisi nilai dummy untuk token,
  karena load test tidak menyentuh Telegram asli sama sekali.
- Disk di VPS tidak perlu besar: ukuran file di skenario campuran
  adalah SIMULASI (disk virtual), file temp asli cuma stub 1 byte.

## 1. Masuk clone uji coba & ganti branch

Kalau clone uji coba belum ada, buat dulu:

```bash
mkdir -p ~/muse && cd ~/muse
git clone https://github.com/Halfseensamaelteam/destrucyion-telegram-bot-repo.git
cd destrucyion-telegram-bot-repo
```

Lalu ambil branch fitur terbaru:

```bash
cd ~/muse/destrucyion-telegram-bot-repo
git fetch origin
git checkout temp/size-aware-disk-admission-muse
git pull
ls scripts/load_test_link_queue.py
```

Perintah `ls` terakhir harus menampilkan nama file itu (bukan error).
Kenapa: memastikan kamu benar-benar di branch yang punya skenario
campurannya, bukan di main.

## 2. Build ulang image worker

```bash
docker compose build worker
```

Kenapa: `git pull` cuma memperbarui file di disk — image Docker yang
menjalankan kode worker tidak ikut berubah sampai di-build ulang.

## 3. Buat database scratch

```bash
docker compose up -d postgres redis
docker compose exec postgres createdb -U postgres loadtest
```

Kalau perintah terakhir menjawab `database "loadtest" already
exists`, itu aman — DB-nya sudah ada dari uji coba sebelumnya. Mau
mulai dari nol bersih? Buang dulu, baru buat lagi:

```bash
docker compose exec postgres dropdb -U postgres --if-exists loadtest
docker compose exec postgres createdb -U postgres loadtest
```

## 4. Jalankan load test skenario campuran

Format perintah ini WAJIB persis seperti di bawah — terutama
`--entrypoint python`. Kenapa: entrypoint bawaan service worker
(`docker-entrypoint.sh`) mengabaikan command apa pun dan selalu
menjalankan worker beneran berdasarkan `SERVICE_ROLE`; tanpa override
entrypoint, script-nya tidak akan pernah jalan (yang jalan malah
worker biasa).

Smoke test dulu (±1 menit, membuktikan pipa jalan):

```bash
docker compose run --rm -e LINK_REQUEST_MAX_CONCURRENCY=50 \
  --entrypoint python worker scripts/load_test_link_queue.py \
  --confirm-scratch-db --no-live \
  --small-accounts 10 --small-file-min-mb 10 --small-file-max-mb 80 \
  --big-accounts 6 --big-file-min-mb 300 --big-file-max-mb 900 \
  --virtual-free-mb 22528 --disk-reserve-mb 3072 \
  --requests-per-account 1 \
  --download-mbps 50 --upload-mbps 100 --stagger-seconds 0 \
  --database-url postgresql+asyncpg://postgres:postgres@postgres:5432/loadtest
```

Lalu skenario penuh persis kasus production (400 user kecil + 100
user besar). `-e LINK_REQUEST_MAX_CONCURRENCY=200` menaikkan batas
slot khusus run ini saja (file `.env` tidak berubah), supaya yang
mengatur benar-benar sewa disk-nya, bukan batas slot:

```bash
docker compose run --rm -e LINK_REQUEST_MAX_CONCURRENCY=200 \
  --entrypoint python worker scripts/load_test_link_queue.py \
  --confirm-scratch-db --live \
  --small-accounts 400 --small-file-min-mb 10 --small-file-max-mb 80 \
  --big-accounts 100 --big-file-min-mb 300 --big-file-max-mb 900 \
  --virtual-free-mb 22528 --disk-reserve-mb 3072 \
  --requests-per-account 1 \
  --download-mbps 20 --upload-mbps 40 \
  --database-url postgresql+asyncpg://postgres:postgres@postgres:5432/loadtest
```

Catatan:

- `--virtual-free-mb 22528` = disk virtual yang disimulasikan:
  22 GB bebas seperti VPS production sekarang. `--disk-reserve-mb
  3072` = cadangan 3 GB yang tidak boleh ditembus.
- `--download-mbps` / `--upload-mbps` adalah kecepatan simulasi PER
  koneksi. Default-nya (1,5 / 3,0) meniru koneksi asli, tapi skenario
  penuh akan berjalan berjam-jam; nilai 20/40 di atas membuatnya
  selesai dalam hitungan menit TANPA mengubah perilaku antrean yang
  diuji. Mau rasa waktu yang lebih realistis, turunkan lagi.
- Dashboard live akan tampil (tombol `q` / Ctrl+C berhenti dengan aman
  dan laporan akhir tetap dicetak — hasil parsialnya ditandai
  `PARTIAL`, bukan PASS). Di skenario campuran dashboard ini tampil
  secara default — lengkap dengan panel DISK VIRTUAL (sisa disk,
  lease per kelas, garis reserve) dan kolom CLASS di tabel user;
  `--live` di atas hanya penegasan. Smoke test di langkah sebelumnya
  sengaja memakai `--no-live` karena cukup ringkasan teks.

## 5. Cara membaca hasil

Di akhir run tercetak blok `LOAD TEST RESULT` dan, khusus skenario
campuran, blok `---- mixed scenario (virtual disk) ----`. Yang harus
dicek:

- **`verdict: PASS`** — kesimpulan akhir. PASS hanya keluar kalau
  SEMUA invariant di bawah terpenuhi.
- **Reserve tidak pernah ditembus:** baris
  `min free observed ... -> reserve kept: YES` — sisa disk virtual
  terendah selama run harus tetap ≥ 3072 MB.
- **Kelas kecil selesai lebih dulu:**
  `small class finished before big class: YES`, dan waktu
  `all done at` kelas kecil jauh lebih kecil dari kelas besar.
- **Kelas besar tidak kelaparan:** `big progress during small flood
  (no starvation): YES` — selama banjir file kecil masih mengalir,
  file besar sudah ada yang bergerak/selesai (jaminan 15% bekerja).
- **Exactly-once:** `requests: ... done=<total> failed=0` dan
  `bytes balanced ... : YES` (byte terdownload == terupload ==
  rencana; tidak ada yang ganda, tidak ada yang hilang).

Contoh potongan output ASLI (smoke test skala kecil, 10 user kecil +
6 user besar — formatnya sama untuk run penuh):

```
================ LOAD TEST RESULT ================
requests:        16  done=16 (empty=0)  failed=0  unfinished=0
wall time:       30.0s  (0.53 req/s)
bytes downloaded: 4061.9 MB   uploaded: 4061.9 MB   uploads: 16

---- mixed scenario (virtual disk) ----
virtual disk: base free 22528 MB, reserve 3072 MB, min free observed 18466 MB -> reserve kept: YES
small class: 10/10 done, all done at 3.0s
big class:   6/6 done, all done at 28.1s
big progress during small flood (no starvation): YES
small class finished before big class: YES
bytes balanced (down == up == planned 4062 MB): YES
verdict: PASS — every request saved exactly once
```

Kalau ada invariant yang `NO` atau verdict `FAIL`, simpan output
lengkapnya — baris `first failure:` di laporan menunjukkan error
pertama yang ditemui.

## 6. Empat setting .env baru (untuk production, nanti)

Fitur ini diatur dari `.env` worker production — TIDAK perlu diubah
untuk load test di atas (di sana nilainya disuntik dari flag):

| Setting | Default | Arti |
|---|---|---|
| `LINK_REQUEST_DISK_RESERVE_MB` | `3072` | Ruang disk yang selalu disisakan bebas. Isi `0` = fitur mati total, perilaku persis seperti sebelumnya. |
| `LINK_REQUEST_BIG_FILE_MB` | `100` | Request dengan TOTAL ukuran di atas ini masuk kelas besar. |
| `LINK_REQUEST_BIG_POOL_PERCENT` | `30` | Kelas besar boleh menyewa maksimal sekian % dari disk yang bisa dipakai. |
| `LINK_REQUEST_BIG_GUARANTEE_PERCENT` | `15` | Sekian % disk yang bisa dipakai dipagari dari sewa file kecil, supaya kelas besar selalu bisa mulai. |

## 7. Cara kembali

Di clone uji coba, kembali ke main kapan saja:

```bash
cd ~/muse/destrucyion-telegram-bot-repo
git checkout main
```

Folder production `~/destrucyion-telegram-bot-repo` tidak pernah
disentuh selama tutorial ini — tidak ada yang perlu di-rollback di
sana.
