#!/bin/bash -e
# Baut auf dem Ergebnis von stage2 (Raspberry Pi OS Lite) auf.
if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
