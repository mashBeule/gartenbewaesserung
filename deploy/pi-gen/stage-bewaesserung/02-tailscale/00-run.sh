#!/bin/bash -e
# Tailscale installieren, aber nicht anmelden: Anmeldung kommt über die Einrichtungsseite.
on_chroot << CHROOT
set -e
curl -fsSL "https://pkgs.tailscale.com/stable/debian/${RELEASE}.noarmor.gpg" \
	-o /usr/share/keyrings/tailscale-archive-keyring.gpg
curl -fsSL "https://pkgs.tailscale.com/stable/debian/${RELEASE}.tailscale-keyring.list" \
	-o /etc/apt/sources.list.d/tailscale.list
apt-get update
apt-get install -y --no-install-recommends tailscale
systemctl enable tailscaled
CHROOT
