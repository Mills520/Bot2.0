FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Database and logs live in /app/data — mount it as a volume to persist.
CMD ["python", "bot.py"]
