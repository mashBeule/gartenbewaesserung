#!/bin/bash -e
# Anwendung und systemd-Dienst ins Image kopieren.
# files/ füllt deploy/pi-gen/vorbereiten.sh (Backend-Quellen + Unit) vor dem Build.
if [ ! -f files/backend/pyproject.toml ] || [ ! -f files/bewaesserung.service ]; then
	echo "files/ ist leer: zuerst deploy/pi-gen/vorbereiten.sh ausführen" >&2
	exit 1
fi
install -d "${ROOTFS_DIR}/opt/bewaesserung"
rm -rf "${ROOTFS_DIR}/opt/bewaesserung/backend"
cp -r files/backend "${ROOTFS_DIR}/opt/bewaesserung/backend"
install -m 644 files/bewaesserung.service "${ROOTFS_DIR}/etc/systemd/system/bewaesserung.service"
install -d -m 755 "${ROOTFS_DIR}/etc/bewaesserung"
install -m 600 files/zugang.env.beispiel "${ROOTFS_DIR}/etc/bewaesserung/zugang.env.beispiel"

on_chroot << 'CHROOT'
set -e
# Eigener Systembenutzer ohne Login
if ! id bewaesserung >/dev/null 2>&1; then
	adduser --system --group --no-create-home --home /var/lib/bewaesserung bewaesserung
fi
python3 -m venv /opt/bewaesserung/venv
/opt/bewaesserung/venv/bin/pip install --no-cache-dir /opt/bewaesserung/backend
systemctl enable bewaesserung.service
# Dienst startet erst nach erfolgreicher Zeitsynchronisierung (time-sync.target)
systemctl enable systemd-time-wait-sync.service
CHROOT
