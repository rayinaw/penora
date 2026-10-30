FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:99 \
    HOME=/home/browser \
    DATA_DIR=/data \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg git procps \
    xvfb x11vnc openbox \
    python3 python3-venv \
    supervisor socat \
    libnss3-tools \
    fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Real Google Chrome — fingerprint parity with genuine user traffic.
RUN wget -q -O /tmp/chrome.deb \
      https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*

# noVNC + websockify for human takeover
RUN git clone --depth 1 https://github.com/novnc/noVNC.git /opt/novnc \
    && git clone --depth 1 https://github.com/novnc/websockify.git \
       /opt/novnc/utils/websockify

# Python env (sidecar + mitmproxy)
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY sidecar/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN mkdir -p /home/browser /data /opt/extensions /var/log \
    && chmod -R a+rwX /home/browser /data

COPY supervisord.conf /etc/supervisor/conf.d/pentera.conf
COPY entrypoint.sh prepare_profile.sh launch_chrome.sh start_mitm.sh start_x11vnc.sh \
     /opt/pentera/
COPY mitm_addon.py load_extensions.py /opt/pentera/
COPY sidecar /opt/pentera/sidecar
RUN chmod +x /opt/pentera/*.sh

WORKDIR /opt/pentera
CMD ["/opt/pentera/entrypoint.sh"]
