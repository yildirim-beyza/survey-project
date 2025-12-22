# Bulut Tabanlı Anket Yönetim Sistemi

Bu proje, bulut bilişim ve sanallaştırma teknolojileri kullanılarak geliştirilmiş, web tabanlı bir **anket yönetim sistemi**dir.  
Sistem, anketlerin merkezi bir yapı üzerinden oluşturulmasını, katılımcılara bağlantı yoluyla dağıtılmasını ve elde edilen sonuçların analiz edilmesini amaçlamaktadır.

---

## 🚀 Proje Özeti

Bu uygulama, kurumlar veya araştırma yapan organizasyonların farklı alanlarda anketler oluşturup, bu anketleri bağlantı aracılığıyla katılımcılara ulaştırabileceği merkezi bir yapı sunar.

- Anket oluşturma ve düzenleme
- Katılımcılara link ile anket dağıtımı
- Anket cevaplarının güvenli şekilde toplanması
- İstatistiksel analiz ve görselleştirme
- Docker tabanlı sanallaştırma altyapısı
- Bulut ortamında çalışabilir yapı

---

## 🧱 Kullanılan Teknolojiler

- **Backend:** Python (Flask)
- **Uygulama Sunucusu:** Gunicorn
- **Frontend:** HTML, CSS, JavaScript (Jinja2 Template Engine)
- **Veritabanı:** MySQL
- **Sanallaştırma:** Docker & Docker Compose
- **Bulut Ortamı:** Sanal Makine (VM) üzerinde Docker
- **Versiyon Kontrol:** Git & GitHub

---

## 🐳 Mimari Yapı

Uygulama, Docker Compose kullanılarak iki ayrı container şeklinde çalışmaktadır:

- **Web Container**
  - Flask + Gunicorn
  - Uygulama mantığı ve arayüz
- **Database Container**
  - MySQL
  - Anketler, sorular, katılımcılar ve cevaplar

Container’lar aynı Docker ağı (network) içerisinde haberleşir ve servis isimleri üzerinden bağlantı kurar.

---

## 🔐 Yetkilendirme Yapısı

- **Yönetici (Admin) Paneli**
  - Anket oluşturma
  - Soru ve katılımcı alanları tanımlama
  - İstatistik ve analiz ekranları
- **Katılımcı Arayüzü (Public)**
  - Giriş gerektirmez
  - Sadece kendisine gönderilen anket linki üzerinden erişim
  - Anket doldurma ve gönderme

---

## 📊 İstatistik ve Analiz Özellikleri

- Soru bazlı cevap dağılımları
- Katılımcı sayıları
- Zorunlu soruların yanıtlanma durumu
- Ortalama anket tamamlama süresi
- Grafik destekli analizler

---

## ⚙️ Kurulum ve Çalıştırma

### Gereksinimler
- Docker
- Docker Compose

### Çalıştırma Adımları
```bash
docker compose build
docker compose up -d
