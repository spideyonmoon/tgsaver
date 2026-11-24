FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py gen_session.py ./

# Same uid/gid as the rest of the stack so FileBrowser and rclone can manage the files.
USER 1000:1000
CMD ["python", "bot.py"]
