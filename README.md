<div align="center">

<img src="assets/icon.svg" width="96" height="96" alt="DeckPort icon">

# DeckPort

**Satu tempat untuk meng-update semua AppImage dan PC port di Steam Deck.**<br>
Mirip Obtainium, tapi untuk Steam Deck: tempel link rilisnya, DeckPort yang cek, download, verifikasi, dan ganti file lamanya.

![Steam Deck](https://img.shields.io/badge/Steam%20Deck-ready-1e40af?style=flat-square&logo=steamdeck&logoColor=white)
![SteamOS](https://img.shields.io/badge/SteamOS-no%20sudo-2563eb?style=flat-square&logo=steam&logoColor=white)
![Sources](https://img.shields.io/badge/GitHub%20·%20GitLab%20·%20Codeberg%20·%20Gitea%20·%20Forgejo-3b82f6?style=flat-square)
![Decky](https://img.shields.io/badge/Decky%20Loader-plugin-60a5fa?style=flat-square)

[Install](#-install) · [Cara pakai](#-cara-pakai) · [Sudah punya AppImage?](#-sudah-punya-appimage-nya) · [CLI](#-cli) · [FAQ](#-faq)

<img src="docs/screenshots/overview.jpg" alt="DeckPort: daftar app dengan status update, up to date, belum terpasang, dan error" width="860">

</div>

## Kenapa DeckPort?

PC port dan emulator di GitHub rilis update terus. Tanpa DeckPort, setiap update berarti buka GitHub, cari file Linux yang benar, download, ganti file lama, lalu berharap shortcut Steam-mu masih jalan.

DeckPort mengerjakan semua itu dengan satu klik **Update all**, atau bahkan otomatis dari Game Mode.

## ✨ Fitur

**Update tanpa ribet**
- Mendukung **GitHub, GitLab, Codeberg, Gitea, dan Forgejo**. Server self-hosted pun dideteksi otomatis.
- **Memilih file Linux yang benar dengan sendirinya**: AppImage dulu, lalu build `linux`/`steamdeck` di zip/tar. Build Windows, macOS, ARM, dan Flatpak dilewati. Kalau perlu, pilihannya bisa diatur dengan regex.
- Rilis *nightly* atau *continuous* tetap terdeteksi, walaupun nama tag-nya sama.
- **Update all** sekali klik, dengan progress per app.

**Ramah Steam**
- **Path file tidak berubah** (`~/Applications/NamaGame.AppImage`), jadi shortcut *Non-Steam Game* tidak rusak setelah update.
- **Memakai file yang sudah kamu punya**: `Game-v1.1-x86_64.AppImage` yang di-download manual akan di-update di tempatnya, dengan nama yang sama.
- **Plugin Decky** untuk Game Mode: update tanpa pindah ke Desktop, ditambah cek otomatis tiap 6 jam dengan notifikasi.

**Aman**
- **Verifikasi SHA-256** memakai checksum dari GitHub atau dari file checksum di rilis (`SHA256SUMS`, `*.sha256`). Kalau tidak cocok, download dihapus dan versi lama tetap dipakai.
- Versi sebelumnya disimpan sebagai `.old` dan bisa di-**Rollback** dengan satu klik.
- App yang sudah versi terbaru **tidak bisa ter-install ulang** tanpa sengaja.
- Port berbentuk folder di-update dengan cara menimpa file, jadi **save dan config tidak terhapus**.

**Ringan**
- Logikanya hanya satu file Python yang memakai Python bawaan SteamOS. Tidak perlu `pip`, tidak perlu `sudo`, dan tetap aman setelah SteamOS update.
- UI *liquid glass* gelap yang tetap ringan: efek blur hanya di header dan dialog, dan animasinya cuma jalan sekali.
- Daftar app bisa di-**export/import** ke JSON untuk backup atau dibagikan ke teman. Token dan path lokal tidak ikut.

## 📦 Install

DeckPort tersedia dalam tiga bentuk. Ketiganya memakai daftar app dan folder `~/Applications` yang sama, jadi boleh dipasang bersamaan.

| Bentuk | Dipakai di | Cocok untuk |
|---|---|---|
| **AppImage**<br>`DeckPort-x.y.z-x86_64.AppImage` | Desktop Mode | Aplikasi dengan jendela sendiri, tanpa browser. **Direkomendasikan.** |
| **Plugin Decky**<br>`DeckPort-decky-x.y.z.zip` | Game Mode (menu ⋯) | Update tanpa keluar dari Game Mode, dengan notifikasi otomatis |
| **Script**<br>`install.sh` | Desktop Mode | Paling ringan (±90 KB), tapi tampil di browser |

File AppImage dan ZIP Decky ada di halaman **Releases**, atau di artifact **appimage** / **decky** pada run terbaru di tab **Actions**.

<details open>
<summary><b>AppImage (Desktop Mode)</b></summary>

1. Masuk **Desktop Mode**, lalu download `DeckPort-…-x86_64.AppImage`.
2. Klik kanan file-nya, pilih **Properties** → **Permissions**, lalu centang **Is executable**.
3. Klik dua kali. DeckPort terbuka di jendelanya sendiri.

<img src="docs/screenshots/app-window.png" alt="Jendela AppImage DeckPort" width="720"><br>
<sub>Screenshot ini diambil otomatis oleh CI dari AppImage yang dijalankan di Linux, jadi tanpa title bar.</sub>

- Ukurannya sekitar 80 MB karena mesin tampilannya (WebKitGTK, lewat Tauri) ikut dibundel. Yang dibutuhkan dari sistem cuma `python3`, dan itu sudah ada di SteamOS.
- Begitu jendelanya ditutup, DeckPort langsung berhenti. Kalau saat itu sedang ada download, download-nya diselesaikan dulu.
- AppImage ini juga bisa dipakai sebagai CLI, misalnya `./DeckPort-…AppImage update`.
</details>

<details>
<summary><b>Plugin Decky (Game Mode)</b></summary>

Butuh [Decky Loader](https://decky.xyz) yang sudah terpasang.

1. Download `DeckPort-decky-….zip` ke Deck. **Jangan diekstrak.**
2. Di Game Mode, buka **⋯** → **Decky** → ⚙ **Settings** → **Developer**, lalu aktifkan **Developer mode**.
3. Pilih **Install Plugin from ZIP File**, lalu pilih file ZIP tadi.
4. DeckPort muncul di daftar plugin. Isinya **Update all**, **Check for updates**, daftar app (tekan sebuah app untuk Check/Rollback/Remove), dan kolom **Add app**.

Asset pattern dan GitHub token diatur dari versi Desktop, lalu otomatis ikut terpakai di plugin.
</details>

<details>
<summary><b>Script (install.sh)</b></summary>

Masuk **Desktop Mode**, copy folder repo ini ke Deck (misalnya ke `~/deckport`), lalu jalankan di **Konsole**:

```bash
bash ~/deckport/install.sh
```

Setelah itu buka **DeckPort** dari menu aplikasi (*Utilities*) atau dari ikon di Desktop. Versi ini terbuka di browser.
</details>

## 🚀 Cara pakai

1. Klik **Add app**, lalu tempel link repo-nya. Contohnya `https://github.com/owner/repo`, `https://gitlab.com/group/project`, atau `https://codeberg.org/owner/repo`. Link `/releases` juga bisa.
2. Klik **Preview assets** untuk melihat file mana yang akan di-download. Kalau pilihannya salah, klik file yang benar dan pattern-nya akan terisi otomatis.
3. Klik **Install**. Untuk seterusnya, cukup **Check all** lalu **Update all**.
4. **Supaya bisa dimainkan dari Game Mode:** di Steam (Desktop Mode), pilih **Games** → **Add a Non-Steam Game**, lalu pilih file di `~/Applications`. Langkah ini cukup sekali saja, karena path-nya tidak pernah berubah.

### Arti status

| Status | Tampilan | Artinya |
|---|---|---|
| **Update → v1.2** | ↑ biru, dengan garis aksen | Ada versi baru. Klik **Update**. |
| **Up to date** | ✓ hijau | Sudah versi terbaru. Tombol Install/Update tidak muncul. |
| **Not installed** | ↓ kuning | Sudah dilacak tapi belum di-download, atau file-nya sudah kamu hapus. |
| **Error** | ⚠ merah | Misalnya tidak ada file Linux di rilis itu. Atur asset pattern lewat **Edit**. |
| **Downloading 45%** | spinner | Sedang proses. Launch dan Rollback dinonaktifkan sampai selesai. |

App yang butuh update selalu tampil paling atas. Chip di atas daftar (**All · Updates · Up to date · Not installed**) bisa dipakai untuk memfilter.

| Sedang download | Sudah terpasang |
|---|---|
| ![Download berjalan](docs/screenshots/downloading.jpg) | ![Terpasang dan terverifikasi](docs/screenshots/installed.jpg) |

## 📁 Sudah punya AppImage-nya?

Banyak rilis memakai nama file yang berisi versi. Misalnya, `~/Applications` sudah berisi `pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage` yang sudah kamu jadikan shortcut Steam. Caranya:

1. Klik **Add app**, tempel link repo-nya, lalu klik **Preview assets**.
2. Di bagian **"Already have this AppImage?"**, DeckPort menunjukkan file yang cocok dan otomatis mencentangnya.
3. Klik **Add**.
   - Kalau file lamanya ternyata sudah versi terbaru (dicek lewat SHA-256), statusnya langsung *Up to date*.
   - Kalau belum, klik **Update**: versi baru ditulis ke **path dan nama file yang sama**, dan yang lama disimpan sebagai `.old`.

<img src="docs/screenshots/adopt.jpg" alt="Dialog Add dengan saran file yang sudah ada" width="720">

Lewat CLI:

```bash
deckport add https://github.com/PCSX2/pcsx2 --adopt ~/Applications/pcsx2-v2.6.0-linux-appimage-x64-Qt.AppImage
```

## ⌨️ CLI

Perintah `deckport` (dari `install.sh`) atau `./DeckPort-…AppImage`:

```bash
deckport add https://github.com/owner/repo --name "My Port"
deckport add <repo> --pattern 'linux.*\.AppImage$'   # pilih asset manual (regex)
deckport add <repo> --pre                            # ikut memantau pre-release
deckport add <repo> --adopt ~/Applications/Game-v1.1.AppImage
deckport list
deckport check
deckport update                 # update semua yang ada versi barunya
deckport update "My Port"       # update satu app
deckport remove "My Port" --delete-files
deckport export daftar.json     # backup/bagikan daftar app
deckport import daftar.json     # gabungkan; yang sudah ada dilewati
```

## ❓ FAQ

<details>
<summary><b>Muncul "GitHub rate limit hit"</b></summary>

Tanpa login, GitHub hanya mengizinkan 60 request per jam. Buat token di <https://github.com/settings/tokens> (tipe *fine-grained*, akses *public repositories* read-only sudah cukup), lalu tempel di **⚙ Settings**. Batasnya naik jadi 5000 request per jam. Token disimpan di `~/.config/deckport/config.json` (hanya bisa dibaca oleh user-mu) dan tidak pernah ikut di export.
</details>

<details>
<summary><b>DeckPort memilih file yang salah</b></summary>

Buka **Edit**, klik **Preview assets**, lalu klik file yang benar. Pattern regex-nya akan terisi otomatis, dengan bagian versinya diganti wildcard supaya tetap cocok untuk rilis berikutnya.
</details>

<details>
<summary><b>AppImage tidak mau terbuka</b></summary>

Pastikan **Is executable** sudah dicentang (lewat Properties → Permissions, atau jalankan `chmod +x DeckPort-*.AppImage`). Kalau muncul pesan *"DeckPort needs Python 3"*, berarti sistemmu tidak punya `python3`. SteamOS sudah menyertakannya, jadi pesan ini biasanya hanya muncul di distro lain.
</details>

<details>
<summary><b>Shortcut Steam-ku masih menjalankan versi lama</b></summary>

Cek baris **File** di kartu app. Shortcut Steam harus mengarah ke file itu. Kalau shortcut-mu mengarah ke file lain (misalnya yang dulu kamu download manual), pakai fitur [Sudah punya AppImage?](#-sudah-punya-appimage-nya) supaya DeckPort meng-update file tersebut.
</details>

<details>
<summary><b>Data DeckPort disimpan di mana?</b></summary>

- Daftar app dan pengaturan: `~/.config/deckport/config.json`
- File game: `~/Applications/` (bisa diganti di **⚙ Settings**)

Ketiga bentuk DeckPort (AppImage, Decky, script) membaca file yang sama. DeckPort tidak mengirim telemetri; yang dihubungi hanya API forge tempat rilisnya berada.
</details>

## 🛠️ Cara kerja

```mermaid
flowchart LR
  A["AppImage<br/>(Tauri window)"] -- "starts on a free localhost port" --> P["deckport.py"]
  S["install.sh<br/>(browser)"] --> P
  D["Decky plugin<br/>(Game Mode)"] -- "imports" --> P
  P -- "release API" --> F["GitHub · GitLab · Codeberg<br/>Gitea · Forgejo"]
  P -- "download + SHA-256" --> L["~/Applications"]
  P <--> C["~/.config/deckport"]
```

Semua logika ada di [`deckport.py`](deckport.py), yang hanya memakai Python standard library. AppImage-nya hanyalah cangkang [Tauri](https://v2.tauri.app) ([`desktop/`](desktop)) yang menjalankan script itu dan menampilkan UI-nya di jendela native. Plugin Decky ([`decky-plugin/`](decky-plugin)) memanggil kode yang sama.

<details>
<summary><b>Build sendiri</b></summary>

- **AppImage** (Linux x86_64): `cd desktop && npm ci && npm run build`. Butuh Rust, Node.js, dan [dependensi Tauri](https://v2.tauri.app/start/prerequisites/#linux). Hasilnya ada di `desktop/src-tauri/target/release/bundle/appimage/`. Untuk mencoba langsung: `npm run dev`.
- **Plugin Decky**: `decky-plugin/package.sh` (butuh Node.js 20+ dan `zip`).
- **Tes**: `python3 -I tests/test_deckport.py deckport.py`.

GitHub Actions mem-build AppImage (di Ubuntu 22.04, supaya kompatibel dengan SteamOS) dan plugin Decky di setiap push ke `main`. Selain itu, CI juga menjalankan AppImage di layar virtual untuk memastikan jendelanya bisa terbuka. Tag `v*` otomatis dipublish sebagai Release.
</details>
