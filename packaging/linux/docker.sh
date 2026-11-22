#!/bin/bash
# The Linux app from a Mac (or any machine with Docker), in an Ubuntu 24.04 container of the machine's
# own architecture (aarch64 on Apple silicon):
#
#   packaging/linux/docker.sh build        packaging/linux/build.sh in the container: dist/TetherInvert/
#                                          and dist/TetherInvert-<version>-linux-<arch>.tar.gz
#   packaging/linux/docker.sh gui [--sim]  unpacks that tarball as a fresh user and opens the window;
#                                          watch and use it at http://localhost:6080/vnc.html
#
# No USB in the container: Docker Desktop on macOS cannot pass a camera through. A test with a real
# camera needs a Linux machine or a virtual machine with USB passthrough (see docs/manual/install.md).

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=tetherinvert-linux

docker build --quiet --tag "$IMAGE" - > /dev/null <<'EOF'
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
        python3 python3-venv libpython3.12t64 libglib2.0-0t64 git binutils ca-certificates \
        xvfb xauth x11vnc novnc websockify openbox libgl1 libegl1 libfontconfig1 libdbus-1-3 \
        libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
        libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xkb1 \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*' \
    && useradd --create-home tester
EOF

case "${1:-}" in
build)
    docker run --rm -v "$ROOT:/src" -w /src "$IMAGE" packaging/linux/build.sh
    ;;
gui)
    shift
    TARBALL=$(ls -t "$ROOT"/dist/TetherInvert-*-linux-"$(uname -m | sed 's/arm64/aarch64/')".tar.gz 2> /dev/null | head -1)
    [ -n "$TARBALL" ] || { echo "no Linux tarball in dist/: run $0 build first" >&2; exit 1; }
    echo "$(basename "$TARBALL"): open http://localhost:6080/vnc.html and choose Connect (Ctrl-C here ends it)"
    docker run --rm $([ -t 0 ] && echo -it) --name "$IMAGE" -p 127.0.0.1:6080:6080 -v "$TARBALL:/tmp/app.tar.gz:ro" "$IMAGE" su tester -c "
        cd && tar xzf /tmp/app.tar.gz && TetherInvert/install.sh > /dev/null
        Xvfb :1 -screen 0 1600x1000x24 & sleep 1
        export DISPLAY=:1
        openbox & x11vnc -display :1 -forever -nopw -quiet -localhost > /dev/null 2>&1 &
        websockify --web /usr/share/novnc 6080 localhost:5900 > /dev/null 2>&1 &
        ~/.local/bin/tetherinvert $*"
    ;;
*)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
