FROM python:3.13-slim

RUN useradd --create-home --uid 10001 trader
WORKDIR /app

COPY pyproject.toml README.md /app/
COPY --chown=trader:trader src /app/src
RUN pip install --no-cache-dir '.[postgres]'
RUN mkdir /app/data && chown trader:trader /app/data

USER trader
ENV PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRADING_ENABLED=false \
    PAPER_DB_PATH=/app/data/paper.db \
    MARKET_DB_PATH=/app/data/market.db

ENTRYPOINT ["python", "-m", "toss_trader"]
CMD ["--help"]
