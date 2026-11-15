#!/bin/bash -e
# Kopiert Backend-Quellen und systemd-Unit in die pi-gen-Stufe. Vor jedem Image-Build ausführen
# (macht der Workflow .github/workflows/build-image.yml). Ohne .venv, Caches und Tests.
cd "$(dirname "$0")/../.."
ziel=deploy/pi-gen/stage-bewaesserung/01-anwendung/files
rm -rf "$ziel"
mkdir -p "$ziel/backend"
cp backend/pyproject.toml "$ziel/backend/"
cp -r backend/src "$ziel/backend/src"
find "$ziel/backend" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$ziel/backend" -name '*.egg-info' -type d -prune -exec rm -rf {} +
cp deploy/systemd/bewaesserung.service deploy/systemd/zugang.env.beispiel "$ziel/"
echo "Stufe vorbereitet: $ziel"
# pi-gen führt nur ausführbare Skripte aus; unter Windows geht das x-Bit leicht verloren.
chmod +x deploy/pi-gen/stage-bewaesserung/prerun.sh deploy/pi-gen/stage-bewaesserung/*/*.sh
