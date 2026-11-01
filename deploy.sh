#!/bin/bash
# One-shot deploy for a fresh Ubuntu VPS (Hostinger, DigitalOcean, etc.)
# Run as root from inside the unzipped app folder:  bash deploy.sh
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "== 1/6 Installing system packages =="
apt-get update -y
apt-get install -y python3 python3-venv python3-pip nginx ufw curl openssl

echo "== 2/6 Python environment =="
python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r requirements.txt

echo "== 3/6 Config (.env) =="
[ -f .env ] || { cp .env.example .env 2>/dev/null || touch .env; }
grep -q '^HOST='      .env || echo "HOST=127.0.0.1" >> .env
grep -q '^PORT='      .env || echo "PORT=8000"       >> .env
grep -q '^AUTH_USER=' .env || echo "AUTH_USER=admin" >> .env
grep -q '^AUTH_PASS=' .env || echo "AUTH_PASS=$(openssl rand -hex 6)" >> .env
chmod 600 .env

echo "== 4/6 systemd service =="
cat >/etc/systemd/system/agency-dashboard.service <<EOF
[Unit]
Description=Agency Dashboard
After=network.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable agency-dashboard >/dev/null 2>&1
systemctl restart agency-dashboard

echo "== 5/6 nginx reverse proxy =="
cat >/etc/nginx/sites-available/agency <<'EOF'
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 1200s;
    }
}
EOF
ln -sf /etc/nginx/sites-available/agency /etc/nginx/sites-enabled/agency
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "== 6/6 Firewall =="
ufw allow OpenSSH      >/dev/null 2>&1 || true
ufw allow 'Nginx Full' >/dev/null 2>&1 || true
ufw --force enable     >/dev/null 2>&1 || true

IP="$(curl -s ifconfig.me || echo YOUR_VPS_IP)"
U="$(grep '^AUTH_USER=' .env | cut -d= -f2)"
P="$(grep '^AUTH_PASS=' .env | cut -d= -f2)"
cat <<EOF

==================================================================
 ✅ Dashboard is live (password-protected, view-safe by default)

   URL:      http://$IP
   Login:    user: $U    password: $P

 Next (for live sync / control features):
   nano .env      -> paste META_ACCESS_TOKEN=...
   systemctl restart agency-dashboard

 Logs:      journalctl -u agency-dashboard -f
 Update:    unzip -o new.zip && systemctl restart agency-dashboard
==================================================================
EOF
