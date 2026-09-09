FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    imapsync python3 python3-pip msmtp msmtp-mta gettext-base cron ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip3 install --no-cache-dir fastapi uvicorn jinja2 python-multipart --break-system-packages
COPY main.py /app/main.py
COPY msmtp.conf.template /app/msmtp.conf.template
COPY templates /app/templates
COPY static /app/static
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
