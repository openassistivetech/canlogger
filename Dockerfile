FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    bash \
    git \
    python3 \
    python3-venv \
    python3-pip \
    can-utils \
    zstd \
    rclone \
    iproute2 \
    procps \
    less \
    nano \
    vim \
    curl \
    ca-certificates \
    build-essential \
    debhelper \
    devscripts \
    dh-python \
    systemd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work

CMD ["/bin/bash"]

RUN ln -sf /bin/bash /bin/sh