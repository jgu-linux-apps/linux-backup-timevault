# Linux Backup TimeVault

TimeVault — Time Machine dla Ubuntu (24.04 LTS i nowsze): pełny backup systemu na dysk zewnętrzny
(opcjonalnie Google Drive) i odtworzenie 1:1 — aplikacje, ustawienia, tapeta, użytkownik,
snapy — na ten sam albo zupełnie nowy dysk.

> **Status: 0.7.4 — aplikacja (tray) + automatyczne kopie + Recovery + paczka .deb.** (wcześniej: linux-backup-timemachine; dyski i backupy z 0.1.x działają bez zmian) Backup, retencja, weryfikacja i restore działają.
> Przetestowane end-to-end na wirtualnych dyskach oraz pełnym cyklem: minimalne Ubuntu 24.04
> → backup → restore (z initramfs i update-grub w chroocie) → start w QEMU/UEFI do ekranu
> logowania. Recovery przetestowane w QEMU: start z dysku backupu → restore na pusty dysk (z grub-install)
> → start odtworzonego systemu bez wpisu NVRAM.

## Instalacja i codzienne użycie

Najnowszy plik `timevault_<wersja>_all.deb` pobierz ze [strony Releases](https://github.com/jgu-linux-apps/linux-backup-timevault/releases/latest):

```bash
sudo apt install ./timevault_<wersja>_all.deb
```

Po instalacji w menu aplikacji jest **TimeVault**, a po zalogowaniu ikona siedzi w zasobniku
(autostart). Pierwsze uruchomienie:

aplikacja sama rozpoznaje podłączony dysk i pokazuje jedną akcję:

* **Użyj tego dysku…** — dysk jest już dyskiem TimeVault: tylko hasło, nic nie jest usuwane,
* **Przygotuj ten dysk…** — nowy dysk zewnętrzny: `WYMAZ`, hasło — partycje + Recovery +
  pierwsza kopia w jednym kroku (proponowane są wyłącznie dyski USB).

**Google Drive (kopia poza domem):** Ustawienia → Chmura → *Połącz z Google Drive*. Kopie są
kompresowane i szyfrowane na komputerze przed wysłaniem; TimeVault ma dostęp tylko do plików,
które sam utworzy. Przed włączeniem aplikacja pokazuje rozmiar pierwszej wysyłki, szacowany
czas i sprawdza wolne miejsce (darmowy Drive ma 15 GB). W Recovery przy odtwarzaniu można
wybrać źródło: dysk USB albo Google Drive. Po połączeniu z Drive odśwież raz system ratunkowy.

**Zgodność z Ubuntu** jest sprawdzana po każdej kopii. Po aktualizacji Ubuntu (np. 26.10 →
27.04) przychodzi powiadomienie, żeby odświeżyć system ratunkowy, a problemy (np. brak
pakietu, inny bootloader) są zgłaszane, zanim coś przestanie działać. Ręcznie: **Więcej →
Sprawdź zgodność z Ubuntu** albo `timevault doctor`.

**Więcej → Odśwież system ratunkowy na dysku…** buduje Recovery od nowa (np. po dużej
aktualizacji Ubuntu); poprzednia wersja zostaje jako zapas.

Potem wszystko dzieje się samo. Wybierasz **częstotliwość** (co godzinę, co 4 godziny, raz
dziennie, raz w tygodniu albo wyłączone) i czy robić **kopię zaraz po podłączeniu dysku**.
Po najechaniu myszką na przycisk lub opcję pojawia się podpowiedź. Aplikacja i menu Recovery
działają po polsku i po angielsku (jak w systemie; zmiana: **Więcej → Język / Language**).
Po kopii przychodzi powiadomienie, a po kopii dysk jest odmontowany — można go
odłączyć. **Przeglądaj kopie** otwiera historię w Plikach (tylko do odczytu) — skopiuj z niej
dowolny plik z dowolnej godziny. Partycje dysku TimeVault nie pojawiają się w Plikach.

**Hasła nie są nigdzie zapisane jawnie.** Hasło do kopii i token Google Drive leżą w systemowym
sejfie (`systemd-creds`), zaszyfrowane kluczem z układu **TPM 2.0** laptopa i kluczem systemu.
Skopiowany plik jest bezużyteczny na innym komputerze. Hasła zapisane jawnie przez starsze wersje
są szyfrowane automatycznie przy aktualizacji, a oryginały nadpisywane zerami i usuwane. W
systemie ratunkowym dane do Drive są zaszyfrowane Twoim hasłem do kopii. **Zapamiętaj hasło** —
Recovery zawsze o nie pyta, a bez niego kopii nie da się odszyfrować.

## Dysk backupu

`timevault prepare-disk /dev/sdX` wymazuje dysk (po wpisaniu `WYMAZ`) i tworzy:

| Partycja | Rozmiar | Rola |
|---|---|---|
| TM-EFI | 512 MiB | start systemu Recovery (UEFI, Secure Boot przez shim) |
| TM-RECOVERY-A | 3 GiB | system ratunkowy |
| TM-RECOVERY-B | 3 GiB | druga kopia — aktualizacje A/B bez ryzyka |
| TM-DATA | reszta | repozytorium restic, manifesty, cache |

Backup montuje **wyłącznie TM-DATA** — EFI i Recovery są nietykalne (sprawdzane w testach
sumą kontrolną przed i po backupie + retencji).

## Użycie

```bash
sudo apt install restic
sudo python3 -m timevault disks                       # dysk systemowy oznaczony [SYSTEM]
sudo python3 -m timevault prepare-disk /dev/sda       # jednorazowo
sudo python3 -m timevault init
sudo python3 -m timevault backup                      # cały system + manifest + retencja
sudo python3 -m timevault snapshots
sudo python3 -m timevault check --read-data 5%

# Google Drive (rclone skonfigurowany jako "gdrive"):
sudo python3 -m timevault --repo rclone:gdrive:timevault init
sudo python3 -m timevault --repo rclone:gdrive:timevault backup
```

Hasło: pytanie interaktywne, `TIMEVAULT_PASSWORD` albo `--password-file`.
**Zgubione hasło = backup nie do odzyskania.**

### System Recovery (start z dysku backupu)

```bash
sudo apt install debootstrap mtools
sudo python3 -m timevault install-recovery     # ~5 min, pobiera minimalne Ubuntu
```

Recovery trafia do nieaktywnego slotu (A/B); menu startowe przełącza się dopiero po udanej
instalacji, poprzednia wersja zostaje jako zapas. Na dysku backupu zapisywane są wyłącznie
TM-EFI i slot Recovery — EFI laptopa nie jest dotykana.

Użycie: menu startu komputera (F12/F10/Esc) → dysk USB → „TimeVault Recovery” →
„Odtworz system z backupu” → hasło → kopia → cel (poprzednie Ubuntu albo cały dysk) →
plan → `WYMAZ`. Opcje „Uruchom ponownie” i „Wylacz” niczego nie zmieniają.

### Test odtworzenia w maszynie wirtualnej

```bash
sudo apt install qemu-system-x86 ovmf
sudo python3 -m timevault vm-test        # obraz VM na TM-DATA/vm, laptop i jego NVRAM nietknięte
TM-DATA/vm/run-vm.sh                    # bez sudo — okno QEMU z Twoim odtworzonym Ubuntu
```

W obrazie VM (tylko tam): GRUB bez os-probera (bez wpisów dysków laptopa) i dłuższy limit
startu GNOME Shell — obraz na wolnym dysku USB uruchamia pulpit dłużej niż domyślne 90 s.

```
```

### Restore (docelowo z Recovery, nie z działającego systemu)

```bash
# Ten sam laptop z Windowsem — formatuje TYLKO partycję Ubuntu, EFI i NTFS nietknięte:
timevault restore --partition /dev/nvme0n1p5 --plan-only
timevault restore --partition /dev/nvme0n1p5

# Nowy / wymieniony dysk — cały dysk: EFI + Ubuntu:
timevault restore --disk /dev/nvme0n1 [--snapshot <id>]
```

Kolejność: plan → potwierdzenie `WYMAZ` → partycje/mkfs → restic restore → nowe UUID
w fstab → odtworzenie swapu i katalogów systemowych → chroot: `grub-install`,
`update-initramfs`, `update-grub` → jednorazowa usługa pierwszego startu
(dopasowanie sterowników NVIDIA do nowego sprzętu).

## Co jest w backupie

Cały `/` z `--one-file-system`: zamontowane systemy plików (`/proc`, `/sys`, `/run`,
dyski NTFS jak „Dane”, dyski zewnętrzne, `/snap/*`) są pomijane, ale ich puste katalogi
zostają, więc fstab po restore działa. `/var/lib/snapd` jest w backupie, więc snapy
wstają bez pobierania. Wykluczone: cache, kosz, `/tmp`, swap (odtwarzany), paczki `.deb`
w cache apt. Pełna lista: `timevault/repo.py`.

Retencja jak w Time Machine: wszystko z ostatnich 24 h, dzienne z 31 dni, potem tygodniowe;
nigdy mniej niż 3 ostatnie.

## Zabezpieczenia (każde pokryte testem)

- odmowa wymazania dysku, z którego działa system, i dysku z zamontowanymi partycjami,
- odmowa formatowania jako root partycji NTFS/BitLocker/FAT/LUKS/swap,
- ponowna kontrola tuż przed zapisem (nic nie zostało zamontowane w międzyczasie),
- w trybie partycji: Windows bit w bit niezmieniony, EFI bez formatowania, wpisy Windows zostają,
- dyski z fstab nieobecne na nowym sprzęcie dostają `nofail` — system nie utknie przy starcie,
- za mały dysk docelowy wykrywany przed wymazaniem czegokolwiek.

## Ograniczenia 0.1.0

- root musi być na ext4 (bez osobnego `/boot`, bez LUKS/LVM) — tak jak domyślne Ubuntu,
- na ext4 nie ma snapshotów: pliki zmieniane w trakcie backupu (np. otwarta przeglądarka)
  mogą być niespójne; restic raportuje pliki niedostępne,
- `grub-install` na prawdziwej partycji FAT32 sprawdzany u użytkownika przez `vm-test`
  (piaskownica deweloperska nie ma sterownika vfat).

## Testy

Polecenia uruchamiaj w katalogu `src/` (kod źródłowy; paczkę buduje `bash packaging/build-deb.sh`).

```bash
sudo python3 -m pytest tests/      # tworzy dyski loop w /tmp/timevault-test
```

## Planowane

- „Sprawdź, czy dysk się bootuje” i „test restore” w QEMU z poziomu aplikacji (dziś: `timevault vm-test` w terminalu)

## Historia zmian

| Wersja | Co nowego |
|---|---|
| **0.7.4** | Bez zmian w aplikacji: kod w repozytorium w katalogu `src/`, paczka `.deb` na stronie GitHub Releases (budowana automatycznie dla każdego tagu wersji) |
| **0.7.3** | **Powiadomienia:** bez dużego dzwonka w panelu, zwykłe powiadomienia GNOME z ikoną aplikacji; kliknięcie otwiera aplikację i usuwa powiadomienie |
| **0.7.2** | W „O programie”: *Created by: JGu* / *Supported by Claude* |
| **0.7.1** | **Poprawka:** kopia, w której kilku plików nie dało się w pełni odczytać (kod 3 resticu, np. plik Flatpaka aktualizowany w trakcie), była pokazywana jako nieudana — teraz to zapisana kopia z ostrzeżeniem i listą plików |
| **0.7.0** | **Bezpieczeństwo:** hasło do kopii i token Google Drive zaszyfrowane w systemowym sejfie (TPM 2.0 + klucz systemu), automatyczna migracja jawnych plików ze starszych wersji, informacja o ochronie hasła w sprawdzaniu zgodności |
| **0.6.3** | **Poprawka przeglądania kopii** (AppArmor w Ubuntu 25.04+ blokował montowanie poza /mnt); samo połączenie z Google Drive niczego nie wysyła (domyślnie „tylko ręcznie”); łączenie z Drive nie zawiesza się na liczeniu rozmiaru |
| **0.6.2** | Kliknięcie kopii w historii otwiera ją w Plikach (katalog domowy z tamtej chwili, tylko odczyt); **poprawka logowania do Google Drive** (nowe rclone zwraca token w base64); komunikat błędu nie pokazuje fragmentów tokenu |
| **0.6.1** | Nowa nazwa wyświetlana: **Linux Backup TimeVault**; okno zgodności w pełni po polsku; **poprawka logowania do Google Drive** (błąd „illegal base64 data”) |
| **0.6.0** | **Google Drive** jako druga kopia poza domem (logowanie w przeglądarce, szyfrowanie przed wysłaniem, własny harmonogram, tylko przy ładowarce, szacunek miejsca i czasu); **odtwarzanie z Google Drive w Recovery**; okno **Ustawienia** (Kopie / Chmura / Język) |
| **0.5.1** | Ikona aplikacji jasna/ciemna zgodna z motywem (okno, dock, menu aplikacji); ikona traya biała w GNOME (panel ciemny w obu motywach), w innych środowiskach wg motywu; wersja w oknie, w podpowiedzi traya i w **Więcej → O programie** |
| **0.5.0** | Sprawdzanie zgodności z Ubuntu po każdej kopii (system plików, bootloader, pakiety systemu ratunkowego); wykrywanie aktualizacji Ubuntu z powiadomieniem „odśwież system ratunkowy”; twarda blokada wymazania dysku z kopiami; **poprawka krytyczna**: na Ubuntu 26.10 aplikacja nie rozpoznawała dysku TimeVault (płaska odpowiedź `lsblk`) i proponowała jego przygotowanie |
| **0.4.0** | Język angielski (aplikacja, tray, powiadomienia, Recovery, komunikaty); częstotliwość kopii (1 h / 4 h / dziennie / tygodniowo / wyłączone); kopia zaraz po podłączeniu dysku; podpowiedzi przy wszystkich przyciskach; ustawienia bez pytania o hasło |
| **0.3.1** | Ekran startowy rozpoznaje dysk i pokazuje jedną akcję („Użyj tego dysku” / „Przygotuj ten dysk”); kreator proponuje tylko dyski USB (poprawka: mógł pokazać wewnętrzny dysk danych) |
| **0.3.0** | Aplikacja z ikoną w trayu, automatyczne kopie (timer + podłączenie dysku), powiadomienia, przeglądanie kopii w Plikach, „Sprawdź kopie”, konfiguracja „wszystko w jednym”, ukryte partycje, paczka `.deb`, README po angielsku |
| **0.2.0** | System Recovery na dysku backupu (start z USB, Secure Boot), sloty A/B, menu odtwarzania; przetestowane w QEMU i na prawdziwym laptopie |
| **0.1.2** | Zmiana nazwy na linux-backup-timevault; poprawka GRUB-a (konsola `grub>` na Ubuntu 26.10); test VM: dłuższy start GNOME, bez os-probera |
| **0.1.1** | Test odtworzenia w maszynie wirtualnej (`vm-test`); poprawka `root=UUID` w GRUB; potwierdzenie `WYMAZ` bez polskich znaków |
| **0.1.0** | Silnik: kopia całego systemu (restic), retencja jak w Time Machine, odtwarzanie na partycję (dual boot) lub cały dysk, zabezpieczenia i testy |

Pełny opis zmian (EN): sekcja *Changelog* w [README.md](README.md#changelog).
