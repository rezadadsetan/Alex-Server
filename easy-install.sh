#!/usr/bin/env bash
# Interactive front-end for Alex Server. It only collects required deployment data.
set -Eeuo pipefail
ROOT=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
[[ $(id -u) -eq 0 ]] || { echo "Run as root: sudo ./easy-install.sh"; exit 1; }

ask() {
  local var=$1 prompt=$2 default=${3:-} value
  if [[ -n "$default" ]]; then read -r -p "$prompt [$default]: " value; value=${value:-$default}
  else read -r -p "$prompt: " value; fi
  [[ -n "$value" && "$value" != *[[:space:]]* ]] || { echo "Value is required and must not contain spaces."; exit 1; }
  printf -v "$var" '%s' "$value"
}
secret() {
  local var=$1 prompt=$2 value
  read -r -s -p "$prompt: " value; echo
  [[ -n "$value" && "$value" != *[[:space:]]* ]] || { echo "Value is required and must not contain spaces."; exit 1; }
  printf -v "$var" '%s' "$value"
}

echo "Alex Server easy installer"
echo "Values are stored only in the root-readable .env file."
echo
ask DOMAIN "Panel/VPN domain (A record already points here)"
ask ACME_EMAIL "Let's Encrypt email"
ask SHARE_ADDR "Address used in client configs" "$DOMAIN"
detected_ssh=$(ss -Hlntp 2>/dev/null | awk '/sshd/ {sub(/.*:/,"",$4); print $4; exit}')
ask SSH_PORT "Current SSH port" "${detected_ssh:-22}"
ask PANEL_USER "3x-ui admin username" "admin"
secret PANEL_PASS "3x-ui admin password"
secret L2TP_PSK "L2TP/IPsec pre-shared key"
ask EGRESS_ENDPOINT "Foreign WireGuard endpoint (IP:port)"
read -r -p "Foreign WireGuard public key (leave empty on first run): " EGRESS_PEER_PUBKEY
ask EGRESS_ADDRESS "This server's tunnel address" "10.203.70.2/30"
read -r -s -p "Telegram bot token (leave empty to skip RDnet Bot): " TELEGRAM_BOT_TOKEN; echo

umask 077
write_env() { printf '%s=%q\n' "$1" "$2"; }
{
  write_env DOMAIN "$DOMAIN"
  write_env ACME_EMAIL "$ACME_EMAIL"
  write_env SHARE_ADDR "$SHARE_ADDR"
  write_env INBOUND_REMARK Server
  write_env SSH_PORT "$SSH_PORT"
  write_env PANEL_PORT 52667
  write_env PANEL_USER "$PANEL_USER"
  write_env PANEL_PASS "$PANEL_PASS"
  write_env PANEL_PATH ""
  write_env SUB_PORT 2096
  write_env VLESS_PORT 443
  write_env REALITY_TARGET www.digikala.com
  write_env CLIENT_DNS 1.1.1.1
  write_env L2TP_PSK "$L2TP_PSK"
  write_env L2TP_NET 10.203.62
  write_env WG_PORT 51820
  write_env WG_NET 10.203.61
  write_env EGRESS_ENDPOINT "$EGRESS_ENDPOINT"
  write_env EGRESS_PEER_PUBKEY "$EGRESS_PEER_PUBKEY"
  write_env EGRESS_ADDRESS "$EGRESS_ADDRESS"
  write_env EGRESS_MTU 1420
  write_env EGRESS_LISTEN_PORT 54455
  write_env XUI_VERSION v3.10.2
  write_env XRAY_VERSION v26.3.27
  write_env ALLOW_PING yes
  write_env TELEGRAM_BOT_TOKEN "$TELEGRAM_BOT_TOKEN"
} > "$ROOT/.env"
chmod 600 "$ROOT/.env"
exec "$ROOT/install.sh"
