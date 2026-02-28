#!/bin/bash
# deploy.sh — Script deploy CryptoDesk lên Railway
# Chạy: bash deploy.sh

set -e
echo "🚀 CryptoDesk Deploy Script"
echo "================================"

# Kiểm tra git đã init chưa
if [ ! -d ".git" ]; then
  echo "📁 Khởi tạo git repo..."
  git init
  git branch -M main
fi

# Kiểm tra có thay đổi chưa
if [ -n "$(git status --porcelain)" ]; then
  echo "📝 Commit changes..."
  git add .
  read -p "Commit message (Enter để dùng mặc định): " MSG
  MSG=${MSG:-"Update CryptoDesk $(date '+%Y-%m-%d %H:%M')"}
  git commit -m "$MSG"
else
  echo "✅ Không có thay đổi mới"
fi

# Kiểm tra remote
if ! git remote get-url origin &>/dev/null; then
  echo ""
  echo "⚠️  Chưa có GitHub remote."
  echo "1. Tạo repo mới tại: https://github.com/new"
  echo "2. Chạy lệnh sau:"
  echo "   git remote add origin https://github.com/USERNAME/cryptodesk.git"
  echo "   git push -u origin main"
  echo ""
  echo "3. Vào railway.app → New Project → Deploy from GitHub"
else
  echo "📤 Pushing to GitHub..."
  git push origin main
  echo ""
  echo "✅ Done! Railway sẽ tự động redeploy."
  echo "   Kiểm tra tại: https://railway.app/dashboard"
fi
