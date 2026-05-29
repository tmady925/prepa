#!/bin/bash
# Script de déploiement Hostinger KM2
# Lance en tant que root : bash setup.sh

set -e

APP_DIR="/home/prepa/app"
SERVICE_NAME="prepa"

echo "=== Déploiement Prepa sur Hostinger KM2 ==="

# 1. Crée l'utilisateur système si besoin
if ! id "prepa" &>/dev/null; then
    useradd --system --create-home --shell /bin/bash prepa
    echo "  ✓ Utilisateur 'prepa' créé"
fi

# 2. Crée les dossiers de données
mkdir -p /home/prepa/app/exercises
mkdir -p /home/prepa/app/corrections
chown -R prepa:prepa /home/prepa/app
echo "  ✓ Dossiers créés"

# 3. Installe le service systemd
cp deploy/prepa.service /etc/systemd/system/prepa.service
systemctl daemon-reload
systemctl enable prepa
echo "  ✓ Service systemd installé"

# 4. Configure nginx
cp deploy/nginx.conf /etc/nginx/sites-available/prepa
ln -sf /etc/nginx/sites-available/prepa /etc/nginx/sites-enabled/prepa
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
echo "  ✓ Nginx configuré"

# 5. Lance l'app
systemctl restart prepa
sleep 2
systemctl status prepa --no-pager

echo ""
echo "=== Commandes utiles ==="
echo "  Logs en temps réel : journalctl -u prepa -f"
echo "  Redémarrer        : systemctl restart prepa"
echo "  Statut            : systemctl status prepa"
