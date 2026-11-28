FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/crymoder.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Непривилегированный пользователь + каталог данных (named volume наследует владельца)
RUN useradd --create-home --uid 10001 crymoder \
    && mkdir /data && chown crymoder:crymoder /data

COPY crymoder ./crymoder

USER crymoder
VOLUME /data
EXPOSE 8000

CMD ["python", "-m", "crymoder"]
