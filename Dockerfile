FROM debian:trixie

ENV DEBIAN_FRONTEND=noninteractive
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

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
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv \
    && pip install --upgrade pip setuptools wheel \
    && pip install python-can

WORKDIR /work

CMD ["/bin/bash"]