FROM python:3.10-slim

# Çalışma dizini
WORKDIR /app

# Gereksinimleri kopyala ve kur
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama dosyalarını kopyala
COPY . .

# Flask app'i Gunicorn ile çalıştır
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
