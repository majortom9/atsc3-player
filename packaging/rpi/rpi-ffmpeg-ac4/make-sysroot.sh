#!/bin/bash
# Build the small armv7h sysroot that cross-compiling rpi-ffmpeg-ac4 needs.
#
# The ARM toolchain only ships glibc; rpi-ffmpeg's V4L2 request decoder also
# links libdrm and libudev. Copy those (headers, libraries, pkg-config files)
# from a Raspberry Pi running Arch Linux ARM (armv7h) with libdrm and
# systemd-libs installed, so they match what the package will run against.
#
#   ./make-sysroot.sh USER@PI-HOST          -> ./sysroot-armv7h
#   ./make-sysroot.sh USER@PI-HOST /path    -> /path

set -euo pipefail

host=${1:?usage: $0 user@pi [sysroot-dir]}
dest=${2:-"$(cd "$(dirname "$0")" && pwd)/sysroot-armv7h"}

mkdir -p "$dest"
# resolve the real files on the Pi so the version-numbered names come along
ssh "$host" 'set -e
  cd /
  files="usr/include/libdrm usr/include/xf86drm.h usr/include/xf86drmMode.h
         usr/include/libsync.h usr/include/libudev.h
         usr/lib/pkgconfig/libdrm.pc usr/lib/pkgconfig/libudev.pc"
  for l in libdrm libudev; do
    files="$files $(ls -d usr/lib/$l.so*)"
  done
  tar -cf - $files' | tar -xf - -C "$dest"

ssh "$host" 'pacman -Q libdrm systemd-libs' > "$dest/SOURCE"
echo "sysroot ready in $dest:"
cat "$dest/SOURCE"
