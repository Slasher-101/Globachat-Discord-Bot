FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY bot.py .

# Run as a non-root user
RUN useradd -m -u 1000 autotranslate && chown -R autotranslate:autotranslate /app
USER autotranslate

ENTRYPOINT ["python", "-u", "bot.py"]
