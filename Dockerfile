FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Installation des dépendances Perl, Python, système et récupération d'imapsync
RUN apt-get update && apt-get install -y \
    perl \
    libauthen-ntlm-perl \
    libclass-load-perl \
    libcrypt-ssleay-perl \
    libdata-uniqid-perl \
    libdigest-hmac-perl \
    libdist-checkconflicts-perl \
    libfile-copy-recursive-perl \
    libfile-tail-perl \
    libio-compress-perl \
    libio-socket-inet6-perl \
    libio-socket-ssl-perl \
    libio-tee-perl \
    libjson-webtoken-perl \
    libmail-imapclient-perl \
    libmodule-scandeps-perl \
    libnet-ssleay-perl \
    libproc-processtable-perl \
    libsys-meminfo-perl \
    libterm-readkey-perl \
    libunicode-string-perl \
    liburi-perl \
    wget \
    ca-certificates \
    python3 \
    python3-pip \
    msmtp \
    msmtp-mta \
    gettext-base \
    cron \
    && wget https://raw.githubusercontent.com/imapsync/imapsync/master/imapsync -O /usr/bin/imapsync \
    && chmod +x /usr/bin/imapsync \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir fastapi uvicorn jinja2 python-multipart requests --break-system-packages

WORKDIR /app

# Installation des modules Python requis
RUN pip3 install --no-cache-dir fastapi uvicorn jinja2 python-multipart --break-system-packages

COPY main.py /app/main.py
COPY msmtp.conf.template /app/msmtp.conf.template
COPY templates /app/templates
COPY static /app/static
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

EXPOSE 8080

ENTRYPOINT ["/app/entrypoint.sh"]
