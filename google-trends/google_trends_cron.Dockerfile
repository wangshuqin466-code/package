FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir requests openpyxl pymysql psycopg[binary]

COPY google_trends_cron_entrypoint.sh /usr/local/bin/google_trends_cron_entrypoint.sh
RUN chmod +x /usr/local/bin/google_trends_cron_entrypoint.sh

CMD ["/usr/local/bin/google_trends_cron_entrypoint.sh"]
