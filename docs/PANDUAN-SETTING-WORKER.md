# Panduan Setting Worker — Bahasa Paling Sederhana

Dokumen ini menjelaskan cara menghitung setting worker yang paling pas
untuk VPS kamu. Ditulis dengan bahasa sehari-hari dan contoh angka
nyata, supaya siapa pun bisa mengikuti hitung-hitungannya.

Setting yang dibahas ada di file `.env`, di bagian `LINK_REQUEST_*`
dan `DOWNLOAD_*`.

---

## 1. Bayangkan worker itu seperti gudang

Supaya gampang, bayangkan seperti ini:

- **Worker** = gudang tempat memproses file.
- **File yang sedang diunduh** = kardus yang ditaruh di lantai gudang
  selama diproses. Kardus besar makan tempat besar, kardus kecil makan
  tempat kecil. Kardus dibuang begitu selesai diproses.
- **`LINK_REQUEST_MAX_CONCURRENCY`** = jumlah kasir yang boleh melayani
  bersamaan. Kasir banyak = antrean cepat habis, tapi kasir juga butuh
  tempat berdiri.
- **`LINK_REQUEST_MEMORY_BUDGET_MB`** = batas berat keranjang tiap
  gudang. Kalau isi gudang sudah terlalu berat (RAM penuh), kasir
  berhenti menerima kardus baru sampai ada yang selesai.
- **`LINK_REQUEST_DISK_RESERVE_MB`** = garis kuning di lantai gudang.
  Area di dalam garis kuning **tidak boleh** ditaruh kardus, karena
  dipakai jalan orang lewat (sistem operasi, database Postgres,
  Docker). Semua kardus hanya boleh ditaruh di luar garis kuning.
- **Sewa lantai (disk lease)** = sebelum kardus masuk, petugas mengukur
  dulu: "lantai kosong cukup tidak?" Kalau cukup, kardus masuk. Kalau
  tidak cukup, kardus menunggu di luar. Jadi gudang **tidak akan pernah
  kepenuhan sampai meledak** — yang terjadi hanya antrean menunggu.

Dua aturan emas yang membuat setting besar tetap aman:

1. Yang membatasi bukan jumlah kasir, tapi **luas lantai** dan **berat
   keranjang**. Kasir boleh dibuka banyak sekali, kardus tetap hanya
   masuk kalau lantainya cukup dan RAM masih muat.
2. Tiap akun Telegram hanya boleh dilayani **satu kasir pada satu
   waktu**. Jadi 500 kasir bukan berarti 1 akun dibanjiri 500 unduhan —
   tetap satu per satu untuk akun yang sama.

---

## 2. Kenali dulu VPS kamu

Jalankan dua perintah ini di VPS:

```bash
free -h        # melihat RAM
df -h /        # melihat disk
```

Yang perlu dicatat hanya dua angka:

- **RAM total** (contoh: 8 GB)
- **Disk bebas** (kolom `Avail` di `df -h`, contoh: 22 GB)

Dua angka ini yang menentukan semua setting di bawah.

---

## 3. Hitung RAM: `LINK_REQUEST_MEMORY_BUDGET_MB`

RAM dipakai bersama-sama: worker, Postgres, Redis, api, bot, dan
sistem operasi semuanya makan RAM. Worker tidak boleh mengambil
semuanya.

**Aturan praktis: budget worker = kira-kira 1/3 sampai 1/2 dari RAM
total.**

Dari hasil uji beban yang sudah dilakukan: 100 unduhan bersamaan
memakai RAM sekitar 525 MB, jadi kira-kira **1 slot aktif ≈ 5 MB RAM**.
Ini angka kasar untuk memperkirakan, bukan angka pasti.

| RAM total VPS | Budget yang disarankan | Kira-kira cukup untuk |
|---|---|---|
| 2 GB | 512 MB | ±100 slot |
| 4 GB | 1536 MB | ±300 slot |
| 8 GB | 3072 MB | ±600 slot |
| 16 GB | 6144 MB | ±1200 slot |

Budget ini sifatnya **rem**, bukan target: worker bebas bekerja di
bawah angka ini, dan otomatis mengerem kalau mendekatinya.

---

## 4. Hitung disk: `LINK_REQUEST_DISK_RESERVE_MB`

Rumusnya satu baris:

```
Ruang pakai = Disk bebas − Reserve
```

- **Reserve kecil (2048)** = ruang pakai besar, disk selalu penuh
  terpakai kerja, tidak ada yang menganggur. Margin untuk sistem
  operasi dan database jadi tipis.
- **Reserve besar (5120–6144)** = lebih aman untuk sistem, tapi ruang
  kerja menyusut.

Contoh nyata: disk bebas 22 GB, reserve 2 GB → ruang pakai **20 GB**.

**Berapa file bisa jalan bersamaan?** Bagi ruang pakai dengan ukuran
file rata-rata:

| Ruang pakai | File rata-rata 50 MB | File rata-rata 500 MB |
|---|---|---|
| 10 GB | ±200 file | ±20 file |
| 20 GB | ±400 file | ±40 file |
| 40 GB | ±800 file | ±80 file |

Itulah kenapa jumlah slot (kasir) boleh dibuat besar sekali: pada
akhirnya lantai gudang yang menentukan berapa kardus bisa masuk.

---

## 5. File besar vs file kecil: pool, guarantee, grace

File dibagi dua kelas menurut **total ukuran satu request**:

- Di atas `LINK_REQUEST_BIG_FILE_MB` (100 MB) = kelas **besar**.
- Di bawahnya = kelas **kecil**.

Selama dua kelas sama-sama ramai, lantai dibagi begini:

- `LINK_REQUEST_BIG_POOL_PERCENT=30` — kelas besar boleh memakai
  **paling banyak 30%** ruang pakai. Tujuannya supaya file kecil yang
  banyak itu selesai duluan dan tidak tertahan raksasa.
- `LINK_REQUEST_BIG_GUARANTEE_PERCENT=15` — **15% ruang pakai dipagari
  khusus** untuk kelas besar, tidak boleh dipakai kelas kecil.
  Tujuannya supaya file besar tidak pernah berhenti total menunggu
  file kecil habis.
- `LINK_REQUEST_CLASS_IDLE_GRACE_SECONDS=30` — bagian pintarnya:
  pembagian di atas **hanya berlaku selama kelas lawan masih ada**.
  Kalau antrean file kecil sudah sepi selama 30 detik, pagar dibuka
  semua dan file besar boleh memakai **seluruh** ruang pakai. Disk
  tidak pernah menganggur di sebelah file besar yang merangkak.

Contoh dengan ruang pakai 20 GB, saat dua kelas ramai: kelas besar
maksimal memakai 6 GB (30%), dan minimal selalu dapat tempat karena
3 GB (15%) dipagari untuk dia. Saat file kecil habis: kelas besar
boleh memakai seluruh 20 GB.

Nilai 100 / 30 / 15 / 30 ini sudah teruji dan seimbang. Ubah hanya
kalau pola file kamu sangat berbeda (misalnya hampir semua file di
atas 500 MB — naikkan pool ke 40).

---

## 6. Setting lain yang perlu diketahui

- `LINK_REQUEST_MAX_CONCURRENCY` — jumlah kasir maksimum. Karena yang
  membatasi sebenarnya lantai dan RAM, buat saja **tinggi** (500 untuk
  VPS 8 GB). Membuatnya kecil justru membuang tenaga: disk masih
  kosong, RAM masih longgar, tapi kasir sudah ditutup.
- `LINK_REQUEST_POLL_SECONDS=3` — worker melihat antrean tiap 3 detik.
  Sudah pas; lebih kecil dari ini hanya menambah beban database
  tanpa terasa bedanya.
- `LINK_REQUEST_STALE_SECONDS=600` — kalau ada unduhan macet di tengah
  (misalnya worker mati), setelah 10 menit tugasnya dikembalikan ke
  antrean supaya dikerjakan ulang. Jangan diubah.
- `DOWNLOAD_WORKERS=8` — ini beda lagi: jumlah koneksi paralel untuk
  **satu file**. Angka 8 sudah teruji memberi kecepatan ±5–6× dibanding
  1 koneksi. Jangan dinaikkan sembarangan — Telegram membatasi tiap
  akun, koneksi terlalu agresif bisa kena pembatasan (flood) dari
  Telegram.
- `DOWNLOAD_PARALLEL_MIN_BYTES=2097152` — file di bawah 2 MB tidak
  usah diparalelkan, lebih cepat jalan biasa.

---

## 7. Contoh siap pakai untuk macam-macam VPS

Tinggal cari spek yang paling dekat dengan VPS kamu, lalu salin
angkanya ke `.env`.

### VPS kecil — 2 vCPU, RAM 4 GB, disk bebas ±25 GB

```env
LINK_REQUEST_POLL_SECONDS=3
LINK_REQUEST_MAX_CONCURRENCY=150
LINK_REQUEST_MEMORY_BUDGET_MB=1536
LINK_REQUEST_STALE_SECONDS=600
LINK_REQUEST_DISK_RESERVE_MB=3072
# LINK_REQUEST_BIG_FI
...[truncated 2037 chars]