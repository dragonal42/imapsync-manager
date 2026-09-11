FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Installation des dÃ©pendances Perl, Python, systÃ¨me et rÃ©cupÃ©ration d'imapsync
RUN apt-get update && apt-get install -y \
    perl \
    procps \
    tzdata \
    libauthen-ntlm-perl \
    libclass-load-perl \
    libcrypt-ssleay-perl \
    libdata-uniqid-perl \
    libdigest-hmac-perl \
    libdist-checkconflicts-perl \
    libencode-imaputf7-perl \
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
    libreadonly-perl \
    libregexp-common-perl \
    libsys-meminfo-perl \
    libterm-readkey-perl \
    libunicode-string-perl \
    liburi-perl \
    ca-certificates \
    python3 \
    python3-pip \
    msmtp \
    msmtp-mta \
    gettext-base \
    cron \
    && rm -rf /var/lib/apt/lists/*

#&& wget https://raw.githubusercontent.com/imapsync/imapsync/master/imapsync -O /usr/bin/imapsync \
    
WORKDIR /app
# Installation des modules Python requis
RUN pip3 install --no-cache-dir fastapi uvicorn jinja2 python-multipart requests --break-system-packages

COPY main.py ai_preprocessing.py run_logging.py audit_logging.py /app/
COPY msmtp.conf.template /app/msmtp.conf.template
COPY templates /app/templates
COPY static /app/static
COPY imapsync /usr/bin/imapsync
# Fail the image build if a required Perl module is missing, before deployment.
RUN chmod +x /usr/bin/imapsync \
    && ps -p 1 -o pid= \
    && perl -c /usr/bin/imapsync \
    && /usr/bin/imapsync --version \
        --authmech1 XOAUTH2 --oauthaccesstoken1 build-check \
        --authmech2 XOAUTH2 --oauthaccesstoken2 build-check
COPY entrypoint.sh /app/entrypoint.sh
COPY daily_mail.sh /app/daily_mail.sh
RUN chmod +x /app/entrypoint.sh /app/daily_mail.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=3)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
