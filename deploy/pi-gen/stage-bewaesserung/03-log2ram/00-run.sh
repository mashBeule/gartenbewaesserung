#!/bin/bash -e
# log2ram schont die SD-Karte: Logs liegen im RAM und werden periodisch geschrieben.
on_chroot << CHROOT
set -e
curl -fsSL https://azlux.fr/repo.gpg -o /usr/share/keyrings/azlux-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/azlux-archive-keyring.gpg] http://packages.azlux.fr/debian/ ${RELEASE} main" \
	> /etc/apt/sources.list.d/azlux.list
apt-get update
apt-get install -y --no-install-recommends log2ram
CHROOT
