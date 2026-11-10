#!/usr/bin/env bash
# Optional, deliberate deployment only. Never invoked by the main VPN installer.
set -euo pipefail
if [[ ${1:-} != --apply || $# != 6 || $EUID != 0 ]]; then
  echo 'Usage: sudo bash bot/install-proxy.sh --apply HOST PORT CERT KEY PUBLIC_IP' >&2
  exit 2
fi
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=$2 PORT=$3 CERT=$4 KEY=$5 PUBLIC_IP=$6
[[ $HOST =~ ^[A-Za-z0-9.-]+$ && $PORT =~ ^[0-9]+$ && $PORT -ge 1024 && $PORT -le 65535 ]] || exit 2
[[ -f $CERT && -f $KEY && -f /etc/nftables.conf ]] || exit 2
python3 -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$PUBLIC_IP"
openssl x509 -in "$CERT" -noout -checkhost "$HOST" >/dev/null
openssl x509 -in "$CERT" -noout -checkend 86400 >/dev/null
[[ $(openssl x509 -in "$CERT" -pubkey -noout | openssl sha256) == $(openssl pkey -in "$KEY" -pubout | openssl sha256) ]] || { echo 'Certificate/key mismatch'; exit 1; }
[[ -z $(ss -H -lnt "sport = :$PORT") ]] || { echo 'Port already in use'; exit 1; }
# Refuse to replace a bot that has changed since the read-only audit.
EXPECTED=c640499ff4f2304bc8a49c57d472ce2074cdcb5064fcfbeb6c03578fd9deddf3
CURRENT=$(sha256sum /opt/rdnet-bot/rdnet_bot.py | cut -d' ' -f1)
SHIPPED=$(sha256sum "$HERE/rdnet_bot.py" | cut -d' ' -f1)
[[ $CURRENT == "$EXPECTED" || $CURRENT == "$SHIPPED" ]] || { echo 'Bot changed: review/rebase before deployment'; exit 1; }
[[ -d /opt/rdnet-bot/venv && -f /etc/systemd/system/rdnet-bot.service ]] || exit 1
for target in /etc/rdnet-proxy /opt/rdnet-proxy /etc/sudoers.d/rdnet-proxy /etc/systemd/system/rdnet-bot.service.d/proxy.conf; do
  [[ ! -e $target ]] || { echo "Existing proxy installation: review before replacing $target"; exit 1; }
done
/opt/rdnet-bot/venv/bin/python -B - "$HERE" <<'PY'
import sys
sys.path.insert(0,sys.argv[1])
import rdnet_proxy_ui
PY
nft list chain inet alex_server input >/dev/null
BACKUP=/var/backups/rdnet-proxy-$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 700 "$BACKUP"
cp -a /opt/rdnet-bot/rdnet_bot.py /etc/nftables.conf "$BACKUP/"
cp -a /etc/systemd/system/rdnet-bot.service "$BACKUP/"
python3 - "$BACKUP" <<'PY'
import sqlite3,sys
from pathlib import Path
source=Path('/var/lib/rdnet-bot/bot.db')
if source.exists():
    with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as a, sqlite3.connect(sys.argv[1]+'/bot.db') as b:
        a.backup(b)
PY
echo "Backup: $BACKUP"
SUCCESS=no
rollback() {
  local code=$?
  if [[ $SUCCESS != yes ]]; then
    echo 'Install failed; restoring bot and closing only the new proxy port.' >&2
    systemctl disable --now rdnet-proxy 2>/dev/null || true
    while read -r handle; do
      [[ $handle =~ ^[0-9]+$ ]] && nft delete rule inet alex_server input handle "$handle" || true
    done < <(nft -a list chain inet alex_server input | sed -n '/comment "rdnet-proxy"/s/.*# handle \([0-9]*\).*/\1/p')
    cp -a "$BACKUP/nftables.conf" /etc/nftables.conf
    cp -a "$BACKUP/rdnet_bot.py" /opt/rdnet-bot/rdnet_bot.py
    rm -f /etc/sudoers.d/rdnet-proxy /etc/systemd/system/rdnet-bot.service.d/proxy.conf
    systemctl daemon-reload
    systemctl restart rdnet-bot || true
    echo "Rollback finished. Backup: $BACKUP" >&2
  fi
  exit "$code"
}
trap rollback EXIT
getent passwd rdnet-proxy >/dev/null || useradd --system --home-dir /var/lib/rdnet-proxy --shell /usr/sbin/nologin rdnet-proxy
install -d -o root -g root -m 755 /opt/rdnet-proxy
install -d -o root -g rdnet-proxy -m 750 /etc/rdnet-proxy
install -d -o rdnet-proxy -g rdnet-proxy -m 700 /var/lib/rdnet-proxy
install -o root -g rdnet-proxy -m 640 "$CERT" /etc/rdnet-proxy/tls.crt
install -o root -g rdnet-proxy -m 640 "$KEY" /etc/rdnet-proxy/tls.key
python3 - "$HOST" "$PORT" "$PUBLIC_IP" <<'PY'
import json,os,sys
host,port,public_ip=sys.argv[1:]
with open('/etc/rdnet-proxy/config.json','w') as f:
    json.dump(dict(public_host=host,port=int(port),cert='/etc/rdnet-proxy/tls.crt',key='/etc/rdnet-proxy/tls.key',blocked_ips=[public_ip]),f)
os.chmod('/etc/rdnet-proxy/config.json',0o644)
PY
for module in rdnet_proxy_store.py rdnet_proxy_server.py rdnet_proxy_admin.py; do
  install -o root -g root -m 644 "$HERE/$module" /opt/rdnet-proxy/
done
install -o root -g root -m 644 "$HERE/rdnet_proxy_store.py" "$HERE/rdnet_proxy_ui.py" /opt/rdnet-bot/
install -o root -g root -m 644 "$HERE/rdnet_bot.py" /opt/rdnet-bot/rdnet_bot.py
# Isolated interpreter ignores caller-controlled Python environment variables.
printf '%s\n' '#!/bin/sh' 'exec /usr/bin/python3 -I /opt/rdnet-proxy/rdnet_proxy_admin.py' > /usr/local/sbin/rdnet-proxy-admin
chmod 755 /usr/local/sbin/rdnet-proxy-admin
printf '%s\n' 'rdnet-bot ALL=(root) NOPASSWD: /usr/local/sbin/rdnet-proxy-admin ""' > /etc/sudoers.d/rdnet-proxy
chmod 440 /etc/sudoers.d/rdnet-proxy
visudo -cf /etc/sudoers.d/rdnet-proxy >/dev/null
install -d -m 755 /etc/systemd/system/rdnet-bot.service.d
printf '%s\n' '[Service]' 'ReadWritePaths=/var/lib/rdnet-proxy' > /etc/systemd/system/rdnet-bot.service.d/proxy.conf
install -m 644 "$HERE/rdnet-proxy.service" /etc/systemd/system/rdnet-proxy.service
printf 'add rule inet alex_server input tcp dport %s accept comment "rdnet-proxy"\n' "$PORT" > /etc/rdnet-proxy/firewall.nft
chmod 644 /etc/rdnet-proxy/firewall.nft
if ! grep -Fqx 'include "/etc/rdnet-proxy/firewall.nft"' /etc/nftables.conf; then
  printf '\ninclude "/etc/rdnet-proxy/firewall.nft"\n' >> /etc/nftables.conf
fi
nft -c -f /etc/nftables.conf
systemctl daemon-reload
systemctl enable --now rdnet-proxy
for attempt in {1..10}; do
  [[ -f /var/lib/rdnet-proxy/accounts.db ]] && break
  sleep 1
done
[[ -f /var/lib/rdnet-proxy/accounts.db ]] || { echo 'Proxy did not initialize; firewall remains closed'; exit 1; }
nft -f /etc/rdnet-proxy/firewall.nft
systemctl restart rdnet-bot
systemctl is-active --quiet rdnet-proxy rdnet-bot
SUCCESS=yes
echo 'Ready: /vpnadmin -> پروکسی اختصاصی, or /proxyadmin. No accounts pre-created.'
