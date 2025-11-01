FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TZ=Europe/Amsterdam
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libjpeg62-turbo-dev zlib1g-dev libpng-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD bash -lc "alembic upgrade head && python -u main.py"
