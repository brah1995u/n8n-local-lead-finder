FROM postgres:17.10-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends age rclone socat python3 ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
COPY backup.py /opt/backup.py
CMD ["python3", "/opt/backup.py"]
