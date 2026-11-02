#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
IFS= read -r bot_token
[[ -n "$bot_token" ]] || { echo "missing bot token" >&2; exit 1; }
panel_token=$(/usr/local/x-ui/x-ui setting -getApiToken -tokenName rdnet-bot 2>/dev/null | grep -oE '[A-Za-z0-9_-]{32,}' | tail -1)
[[ -n "$panel_token" ]] || { echo "could not create panel token" >&2; exit 1; }
set -a
. /etc/x-ui/install-result.env
. /etc/alex-server/.env
set +a
owner_code=$(openssl rand -hex 12)
base_path=${XUI_WEB_BASE_PATH#/}
sub_path=$(sqlite3 /etc/x-ui/x-ui.db "select value from settings where key='subPath';")
[[ -n "$sub_path" ]] || sub_path=/sub/
umask 077
cat > /etc/rdnet-bot/env <<EOF
BOT_TOKEN=$bot_token
PANEL_URL=https://$DOMAIN:${PANEL_PORT:-52667}/$base_path
PANEL_TOKEN=$panel_token
OWNER_BOOTSTRAP_CODE=$owner_code
DB_PATH=/var/lib/rdnet-bot/bot.db
CHECK_INTERVAL=600
TELEGRAM_PROXY=http://127.0.0.1:10808
SUB_URL_BASE=https://$DOMAIN:${SUB_PORT:-2096}${sub_path}
EOF
printf '%s\n' "$owner_code" > /root/rdnet-owner-bootstrap-code
chmod 600 /etc/rdnet-bot/env /root/rdnet-owner-bootstrap-code
install -d -o root -g rdnet-bot -m 750 /var/lib/rdnet-vpn-stats
install -m 755 "$HERE/rdnet-vpn-helper.py" /usr/local/sbin/rdnet-vpn-helper
install -m 755 "$HERE/ppp-rdnet-up" /etc/ppp/ip-up.d/rdnet-status
install -m 755 "$HERE/ppp-rdnet-down" /etc/ppp/ip-down.d/rdnet-status
install -m 440 "$HERE/rdnet-bot.sudoers" /etc/sudoers.d/rdnet-bot-vpn
visudo -cf /etc/sudoers.d/rdnet-bot-vpn >/dev/null
install -m 644 "$HERE/rdnet-vpn-sample.service" /etc/systemd/system/rdnet-vpn-sample.service
install -m 644 "$HERE/rdnet-vpn-sample.timer" /etc/systemd/system/rdnet-vpn-sample.timer
systemctl daemon-reload
systemctl enable --now rdnet-vpn-sample.timer
echo configured




