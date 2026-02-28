# CryptoDesk — Deploy lên Railway

## Cấu trúc project
```
cryptodesk/
├── main.py              # Flask app entry point
├── Procfile             # Gunicorn command
├── railway.json         # Railway config
├── requirements.txt     # Python dependencies
├── core/                # Binance API, indicators, utils
├── dashboard/           # FAM engine (phân tích tín hiệu)
├── scanner/             # Market scanner
├── static/              # Frontend (index.html)
└── data/                # Config + history JSON (tự tạo khi chạy)
```

---

## Bước 1 — Tạo GitHub repo

```bash
# Trong thư mục cryptodesk/
git init
git add .
git commit -m "Initial deploy"

# Tạo repo trên github.com rồi push
git remote add origin https://github.com/YOUR_USERNAME/cryptodesk.git
git branch -M main
git push -u origin main
```

---

## Bước 2 — Deploy lên Railway

1. Vào **railway.app** → đăng nhập bằng GitHub
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Chọn repo `cryptodesk` vừa tạo
4. Railway tự detect Python và build — chờ ~2 phút

---

## Bước 3 — Cấu hình domain

1. Vào project → tab **"Settings"**
2. Mục **"Networking"** → click **"Generate Domain"**
3. Sẽ có URL dạng: `cryptodesk-production.up.railway.app`

---

## Bước 4 — Set Environment Variables (nếu dùng Telegram)

Vào tab **"Variables"** trong Railway project, thêm:

| Key | Value |
|-----|-------|
| `TELEGRAM_TOKEN` | Token bot Telegram của anh |
| `TELEGRAM_CHAT_ID` | Chat ID nhận alert |

> Nếu không dùng Telegram thì bỏ qua bước này.

---

## Lưu ý quan trọng về data persistence

Railway **reset filesystem** mỗi khi redeploy. Nghĩa là:
- `data/config.json` — mất khi redeploy → phải config lại trong Settings
- `data/history.json` — mất signal history khi redeploy

**Giải pháp nếu muốn giữ data:**
- Thêm Railway Volume (persistent storage) — $0.25/GB/tháng
- Hoặc dùng Railway PostgreSQL/Redis để lưu history

---

## Pricing Railway

| Plan | Price | RAM | CPU |
|------|-------|-----|-----|
| Hobby | $5/tháng | 512MB | Shared |
| Pro | $20/tháng | 8GB | Dedicated |

CryptoDesk chạy tốt trên **Hobby plan** ($5/tháng).

---

## Update code sau khi deploy

```bash
git add .
git commit -m "Update: mô tả thay đổi"
git push
```
Railway tự động redeploy khi có push mới lên `main`.

---

## Kiểm tra logs

Vào Railway project → tab **"Deployments"** → click deployment → **"View Logs"**

Nếu thấy `🚀 CryptoDesk running` là OK.
