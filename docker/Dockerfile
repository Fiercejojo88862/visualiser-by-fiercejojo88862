# Visualiser by Fiercejojo88862 - container image.
#
# Arch-based on purpose: the host is Arch (Omarchy), so mesa/Qt/ffmpeg ABI in the
# image matches the host GPU stack exactly. That matters because this container
# drives the host's render node, Wayland socket, PipeWire socket and session bus.
#
# Build:  docker build -f docker/Dockerfile -t visualiser-by-fiercejojo88862:local .
# (or just run bin/visualiser rebuild)

FROM archlinux:latest

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QT_QPA_PLATFORM=wayland

RUN pacman-key --init && pacman-key --populate archlinux 2>/dev/null || true

RUN pacman -Sy --noconfirm archlinux-keyring && \
    pacman -Syu --noconfirm --needed \
        python \
        python-pyqt6 \
        python-pyqt6-webengine \
        python-dbus \
        python-numpy \
        qt6-wayland \
        libpulse \
        ffmpeg \
        mesa \
        vulkan-radeon \
        vulkan-icd-loader \
        libxkbcommon \
        libxcomposite \
        libxdamage \
        libxrandr \
        libxtst \
        libxfixes \
        alsa-lib \
        nss \
        fontconfig \
        ttf-dejavu \
        hicolor-icon-theme \
        xdg-utils

# Offline beat-timeline analysis from recorded streams needs only numpy: it is
# implemented in app/core/beats.py. librosa is deliberately NOT installed - it
# drags in numba, whose jit cache cannot build on python 3.14 in this image
# (`RuntimeError: cannot cache function ... no locator available`), which made
# every offline analysis fail while looking like it had merely found no beats.

RUN pacman -Scc --noconfirm || true

# The app is bind-mounted at /app at runtime so code edits are live; nothing is
# baked in except the toolchain.
WORKDIR /app

# Sanity check: fail the build early if the Qt/Python stack is broken.
RUN python -c "import PyQt6.QtWebEngineWidgets, PyQt6.QtWebChannel, dbus, numpy; print('stack ok')"

CMD ["python", "-m", "app"]
