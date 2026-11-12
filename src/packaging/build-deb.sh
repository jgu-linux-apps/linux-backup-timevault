#!/bin/sh
# Buduje timevault_<wersja>_all.deb z katalogu projektu.
set -e
cd "$(dirname "$0")/.."
VER=$(python3 -c "import timevault; print(timevault.__version__)")
B=$(mktemp -d)/timevault_${VER}_all
mkdir -p $B/DEBIAN $B/usr/bin $B/usr/share/timevault $B/usr/lib/systemd/system \
         $B/usr/lib/udev/rules.d $B/usr/share/polkit-1/rules.d $B/usr/share/applications \
         $B/etc/xdg/autostart $B/usr/share/icons/hicolor/scalable/apps $B/usr/share/doc/timevault
cp -r timevault $B/usr/share/timevault/
find $B -name __pycache__ -prune -exec rm -rf {} +
printf '#!/bin/sh\nexport PYTHONPATH=/usr/share/timevault\nexec /usr/bin/python3 -m timevault "$@"\n' > $B/usr/bin/timevault
printf '#!/bin/sh\nexport PYTHONPATH=/usr/share/timevault\nexec /usr/bin/python3 -m timevault.gui "$@"\n' > $B/usr/bin/timevault-gui
chmod 755 $B/usr/bin/*
cp packaging/systemd/* $B/usr/lib/systemd/system/
cp packaging/udev/90-timevault.rules $B/usr/lib/udev/rules.d/
cp packaging/polkit/50-timevault.rules $B/usr/share/polkit-1/rules.d/
cp packaging/desktop/timevault.desktop $B/usr/share/applications/
cp packaging/desktop/timevault-autostart.desktop $B/etc/xdg/autostart/timevault.desktop
cp timevault/icons/timevault.svg $B/usr/share/icons/hicolor/scalable/apps/timevault.svg
cp README.md README.pl.md $B/usr/share/doc/timevault/
SIZE=$(du -sk $B | cut -f1)
cat > $B/DEBIAN/control <<CTL
Package: timevault
Version: $VER
Section: admin
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), rclone, openssl, libnotify-bin, python3-pyqt6, python3-pyqt6.qtsvg, qt6-svg-plugins | libqt6svg6, restic, debootstrap, mtools, fuse3, dosfstools, e2fsprogs, fdisk, pkexec, polkitd, util-linux
Recommends: qemu-system-x86, ovmf
Maintainer: Jaro <jaroslawguza83@gmail.com>
Description: Linux Backup TimeVault - full-system backups like Time Machine
 Automatyczne, szyfrowane kopie całego Ubuntu na dysk zewnętrzny (restic),
 bootowalny system Recovery na tym samym dysku i odtworzenie 1:1 na ten sam
 albo nowy dysk (także przy dual boot z Windows).
CTL
echo "/etc/xdg/autostart/timevault.desktop" > $B/DEBIAN/conffiles
cat > $B/DEBIAN/postinst <<'PI'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  mkdir -p /var/lib/timevault /etc/timevault
  chmod 755 /var/lib/timevault; chmod 700 /etc/timevault
  # ustawienia (bez sekretów): administrator zmienia je w aplikacji bez hasła
  C=/var/lib/timevault/config.json
  [ -s $C ] || echo '{"auto": true, "interval": "1h", "on_connect": true, "language": "auto"}' > $C
  chgrp sudo $C 2>/dev/null || true; chmod 664 $C
  # hasła zapisane jawnie przez wersje <= 0.6.3 -> zaszyfrowany sejf (systemd-creds, TPM 2.0)
  /usr/bin/timevault migrate-secrets >/dev/null 2>&1 || true
  # przeglądanie kopii w Plikach (restic mount --allow-other)
  if [ -f /etc/fuse.conf ] && ! grep -q '^user_allow_other' /etc/fuse.conf; then
    echo user_allow_other >> /etc/fuse.conf
  fi
  systemctl daemon-reload || true
  systemctl enable --now timevault-backup.timer || true
  systemctl enable --now timevault-cloud.timer || true
  udevadm control --reload-rules || true
  udevadm trigger --subsystem-match=block --action=change || true
  gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
PI
cat > $B/DEBIAN/prerm <<'PR'
#!/bin/sh
set -e
if [ "$1" = remove ]; then
  systemctl disable --now timevault-backup.timer timevault-cloud.timer 2>/dev/null || true
  systemctl stop timevault-browse.service 2>/dev/null || true
fi
exit 0
PR
cat > $B/DEBIAN/postrm <<'PO'
#!/bin/sh
set -e
if [ "$1" = purge ]; then
  rm -rf /etc/timevault /var/lib/timevault
fi
systemctl daemon-reload 2>/dev/null || true
udevadm control --reload-rules 2>/dev/null || true
exit 0
PO
chmod 755 $B/DEBIAN/postinst $B/DEBIAN/prerm $B/DEBIAN/postrm
dpkg-deb --root-owner-group --build $B dist/ >/dev/null 2>&1 || { mkdir -p dist; dpkg-deb --root-owner-group --build $B dist/; }
ls dist/timevault_${VER}_all.deb
