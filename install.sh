#!/usr/bin/env bash
# ============================================================
#  Alex Server installer — Ubuntu 24.04 (amd64 / arm64)
#  VLESS+Reality (3x-ui, per-user accounting) + L2TP/IPsec + WireGuard
#  All user traffic exits ONLY through the egress WireGuard tunnel.
#  Usage:  cp .env.example .env && nano .env && sudo ./install.sh
#  Safe to re-run: every step checks what already exists.
# ============================================================
set -Eeuo pipefail

REPO_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
ENV_FILE=${ENV_FILE:-$REPO_DIR/.env}
STATE=/etc/alex-server
XUI_DIR=/usr/local/x-ui

c_ok=$'\e[1;32m'; c_warn=$'\e[1;33m'; c_err=$'\e[1;31m'; c_off=$'\e[0m'
log()  { echo "${c_ok}[+]${c_off} $*"; }
warn() { echo "${c_warn}[!]${c_off} $*"; }
die()  { echo "${c_err}[x]${c_off} $*" >&2; exit 1; }
trap 'die "failed at line $LINENO: $BASH_COMMAND"' ERR

# ---------- 0. preflight -------------------------------------
[[ $(id -u) -eq 0 ]] || die "run as root: sudo ./install.sh"
. /etc/os-release
[[ "${VERSION_ID:-}" == "24.04" ]] || warn "tested on Ubuntu 24.04, you have ${PRETTY_NAME:-unknown}"
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE — run: cp .env.example .env && nano .env"

set -a; . "$ENV_FILE"; set +a
for v in DOMAIN ACME_EMAIL SSH_PORT PANEL_PORT PANEL_USER PANEL_PASS L2TP_PSK EGRESS_ENDPOINT EGRESS_ADDRESS; do
  [[ -n "${!v:-}" ]] || die "$v is empty in .env"
done
[[ "$PANEL_PASS" != change-me* && "$L2TP_PSK" != change-me* ]] || die "set real PANEL_PASS and L2TP_PSK in .env"
SHARE_ADDR=${SHARE_ADDR:-$DOMAIN}
INBOUND_REMARK=${INBOUND_REMARK:-Server}
SUB_PORT=${SUB_PORT:-2096}
VLESS_PORT=${VLESS_PORT:-443}
REALITY_TARGET=${REALITY_TARGET:-www.digikala.com}
CLIENT_DNS=${CLIENT_DNS:-1.1.1.1}
WG_PORT=${WG_PORT:-51820}
WG_NET=${WG_NET:-10.203.61}
L2TP_NET=${L2TP_NET:-10.203.62}
EGRESS_MTU=${EGRESS_MTU:-1420}
XUI_VERSION=${XUI_VERSION:-v3.10.2}
XRAY_VERSION=${XRAY_VERSION:-v26.3.27}
ALLOW_PING=${ALLOW_PING:-yes}
EGRESS_SRC=${EGRESS_ADDRESS%/*}

ss -Hlnt "sport = :$SSH_PORT" | grep -q . || die "sshd is not listening on SSH_PORT=$SSH_PORT — fix .env first (the firewall would lock you out)"

case "$(uname -m)" in
  x86_64)  XRAY_ZIP=Xray-linux-64.zip;        XRAY_BIN=xray-linux-amd64 ;;
  aarch64) XRAY_ZIP=Xray-linux-arm64-v8a.zip; XRAY_BIN=xray-linux-arm64 ;;
  *) die "unsupported CPU $(uname -m)" ;;
esac

install -d -m 700 "$STATE" "$STATE/wg-clients"
install -m 600 "$ENV_FILE" "$STATE/.env"
[[ -n "${PANEL_PATH:-}" ]] || PANEL_PATH=$(grep -s '^PANEL_PATH_GENERATED=' "$STATE/generated" | cut -d= -f2 || true)
[[ -n "${PANEL_PATH:-}" ]] || { PANEL_PATH=$(openssl rand -hex 9); echo "PANEL_PATH_GENERATED=$PANEL_PATH" >> "$STATE/generated"; }
PANEL_PATH=${PANEL_PATH#/}; PANEL_PATH=${PANEL_PATH%/}

# ---------- 1. packages --------------------------------------
log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq strongswan strongswan-starter xl2tpd ppp wireguard-tools nftables \
  curl jq unzip sqlite3 fail2ban qrencode openssl ca-certificates python3 python3-venv python3-pip sudo >/dev/null

# ---------- 2. kernel settings -------------------------------
log "sysctl: forwarding, policy routing, conntrack"
echo nf_conntrack > /etc/modules-load.d/alex-server.conf
modprobe nf_conntrack
cat > /etc/sysctl.d/90-alex-server.conf <<EOF
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2
net.ipv4.conf.all.src_valid_mark = 1
net.netfilter.nf_conntrack_max = 65536
net.netfilter.nf_conntrack_tcp_timeout_established = 7200
EOF
sysctl -q --system

# ---------- 3. egress tunnel + policy routing ----------------
log "egress WireGuard tunnel (wg-out) and routing table 200"
[[ -s $STATE/egress.key ]] || (umask 077; wg genkey > "$STATE/egress.key")
EGRESS_PUB=$(wg pubkey < "$STATE/egress.key")

install -m 755 "$REPO_DIR/tools/alex-routing" /usr/local/sbin/alex-routing
cat > /etc/systemd/system/alex-routing.service <<'EOF'
[Unit]
Description=Alex Server policy routing (table 200 + blackhole)
After=network-online.target
Wants=network-online.target
Before=wg-quick@wg-out.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/alex-routing

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now alex-routing.service >/dev/null

if [[ -n "${EGRESS_PEER_PUBKEY:-}" ]]; then
  umask 077
  cat > /etc/wireguard/wg-out.conf <<EOF
[Interface]
PrivateKey = $(cat "$STATE/egress.key")
Address = $EGRESS_ADDRESS
MTU = $EGRESS_MTU
Table = off
PostUp = /usr/local/sbin/alex-routing

[Peer]
PublicKey = $EGRESS_PEER_PUBKEY
Endpoint = $EGRESS_ENDPOINT
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
EOF
  umask 022
  systemctl enable wg-quick@wg-out >/dev/null
  systemctl restart wg-quick@wg-out
  EGRESS_READY=1
else
  EGRESS_READY=0
  warn "EGRESS_PEER_PUBKEY is empty: users will have NO internet (fail-closed) until you set it and re-run"
fi

# ---------- 4. WireGuard for users ---------------------------
log "WireGuard server for users (wg-clients, UDP $WG_PORT)"
[[ -s $STATE/wg-clients.key ]] || (umask 077; wg genkey > "$STATE/wg-clients.key")
if [[ ! -f /etc/wireguard/wg-clients.conf ]]; then
  umask 077
  cat > /etc/wireguard/wg-clients.conf <<EOF
[Interface]
PrivateKey = $(cat "$STATE/wg-clients.key")
Address = $WG_NET.1/24
ListenPort = $WG_PORT

EOF
  umask 022
fi
systemctl enable wg-quick@wg-clients >/dev/null
systemctl restart wg-quick@wg-clients

# ---------- 5. L2TP/IPsec ------------------------------------
log "L2TP/IPsec (strongSwan + xl2tpd)"
cat > /etc/ipsec.conf <<'EOF'
config setup
    uniqueids=no

conn l2tp-ipsec
    keyexchange=ikev1
    authby=secret
    type=transport
    left=%defaultroute
    leftprotoport=17/1701
    right=%any
    rightprotoport=17/%any
    auto=add
    dpdaction=clear
EOF
umask 077
printf '%%any %%any : PSK "%s"\n' "$L2TP_PSK" > /etc/ipsec.secrets
umask 022
cat > /etc/xl2tpd/xl2tpd.conf <<EOF
[global]
port = 1701

[lns default]
ip range = $L2TP_NET.10-$L2TP_NET.99
local ip = $L2TP_NET.1
require authentication = yes
name = l2tpd
pppoptfile = /etc/ppp/options.xl2tpd
length bit = yes
EOF
cat > /etc/ppp/options.xl2tpd <<EOF
require-mschap-v2
refuse-pap
refuse-chap
refuse-mschap
auth
noccp
nodefaultroute
name l2tpd
ms-dns $CLIENT_DNS
lcp-echo-interval 30
lcp-echo-failure 4
mtu 1400
mru 1400
EOF
[[ -f /etc/ppp/chap-secrets ]] || install -m 600 /dev/null /etc/ppp/chap-secrets
chmod 600 /etc/ppp/chap-secrets
systemctl enable strongswan-starter xl2tpd >/dev/null
systemctl restart strongswan-starter xl2tpd

# ---------- 6. tools -----------------------------------------
install -m 755 "$REPO_DIR/tools/l2tp-user"   /usr/local/sbin/l2tp-user
install -m 755 "$REPO_DIR/tools/wg-user"     /usr/local/sbin/wg-user
install -m 755 "$REPO_DIR/tools/alex-status" /usr/local/sbin/alex-status

# ---------- 7. 3x-ui panel (pinned) --------------------------
if [[ ! -x $XUI_DIR/x-ui ]]; then
  log "installing 3x-ui $XUI_VERSION (Let's Encrypt for $DOMAIN needs TCP 80 reachable now)"
  curl -fsSL -m 120 "https://raw.githubusercontent.com/MHSanaei/3x-ui/$XUI_VERSION/install.sh" -o /tmp/3x-ui-install.sh
  XUI_NONINTERACTIVE=1 XUI_SSL_MODE=domain XUI_DOMAIN="$DOMAIN" XUI_ACME_EMAIL="$ACME_EMAIL" \
  XUI_USERNAME="$PANEL_USER" XUI_PASSWORD="$PANEL_PASS" XUI_PANEL_PORT="$PANEL_PORT" \
  XUI_WEB_BASE_PATH="$PANEL_PATH" XUI_DB_TYPE=sqlite \
    bash /tmp/3x-ui-install.sh "$XUI_VERSION" </dev/null
else
  log "3x-ui already installed — keeping it"
fi

log "pinning Xray core $XRAY_VERSION (pre-releases broke older clients)"
tmp=$(mktemp -d)
curl -fsSL -m 180 "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/$XRAY_ZIP" -o "$tmp/x.zip"
unzip -qo "$tmp/x.zip" xray -d "$tmp"
[[ -f $XUI_DIR/bin/$XRAY_BIN ]] && cp -a "$XUI_DIR/bin/$XRAY_BIN" "$XUI_DIR/bin/$XRAY_BIN.bak"
install -d -m 755 /usr/local/lib/alex-server /etc/systemd/system/x-ui.service.d
install -m 755 "$tmp/xray" "/usr/local/lib/alex-server/xray-$XRAY_VERSION"
install -m 755 "$tmp/xray" "$XUI_DIR/bin/$XRAY_BIN"
cat > /etc/systemd/system/x-ui.service.d/10-pin-xray.conf <<EOF
[Service]
ExecStartPre=/usr/bin/install -m 755 /usr/local/lib/alex-server/xray-$XRAY_VERSION $XUI_DIR/bin/$XRAY_BIN
EOF
rm -rf "$tmp"
systemctl daemon-reload
systemctl restart x-ui
sleep 3

# ---------- 8. panel configuration through its API -----------
TOKEN=""
[[ -f /etc/x-ui/install-result.env ]] && TOKEN=$(. /etc/x-ui/install-result.env; echo "${XUI_API_TOKEN:-}")
[[ -n "$TOKEN" ]] || TOKEN=$("$XUI_DIR/x-ui" setting -getApiToken -tokenName alex-installer 2>/dev/null | grep -oE '[A-Za-z0-9_-]{32,}' | tail -1 || true)
[[ -n "$TOKEN" ]] || die "could not obtain a 3x-ui API token"
BASE_PATH=$(. /etc/x-ui/install-result.env 2>/dev/null; echo "${XUI_WEB_BASE_PATH:-$PANEL_PATH}")
BASE="https://$DOMAIN:$PANEL_PORT/${BASE_PATH#/}"; BASE=${BASE%/}
api() { curl -fsS -m 60 --resolve "$DOMAIN:$PANEL_PORT:127.0.0.1" -H "Authorization: Bearer $TOKEN" "$BASE/$1" "${@:2}"; }
api panel/api/inbounds/list >/dev/null || die "panel API not reachable at https://$DOMAIN:$PANEL_PORT/$BASE_PATH"

log "Xray template: egress via wg-out (mark 0x200), DoH DNS, block private/self"
PUBLIC_IP=$(ip -4 route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
api panel/api/xray/ -X POST | jq -r '.obj' | jq '.xraySetting' > "$STATE/xray-template.orig.json"
jq --arg ip "$PUBLIC_IP/32" '
  .dns = {servers:["https://1.1.1.1/dns-query","https://8.8.8.8/dns-query"], queryStrategy:"UseIPv4", tag:"dns-in"} |
  .outbounds = [
    {tag:"egress", protocol:"freedom", settings:{domainStrategy:"UseIPv4"}, streamSettings:{sockopt:{mark:512}}},
    {tag:"block", protocol:"blackhole", settings:{}} ] |
  .routing = {domainStrategy:"IPIfNonMatch", rules:[
    {type:"field", inboundTag:["api"], outboundTag:"api"},
    {type:"field", inboundTag:["dns-in"], outboundTag:"egress"},
    {type:"field", ip:["geoip:private","127.0.0.0/8",$ip], outboundTag:"block"},
    {type:"field", network:"tcp,udp", outboundTag:"egress"} ]}
' "$STATE/xray-template.orig.json" > "$STATE/xray-template.json"
api panel/api/xray/update -X POST --data-urlencode "xraySetting=$(jq -c . "$STATE/xray-template.json")" | jq -e '.success' >/dev/null

if api panel/api/inbounds/list | jq -e --argjson p "$VLESS_PORT" '.obj[] | select(.port==$p)' >/dev/null; then
  log "VLESS inbound on $VLESS_PORT already exists — keeping its keys and users"
else
  log "creating VLESS + Reality inbound on TCP $VLESS_PORT (target $REALITY_TARGET)"
  keys=$("$XUI_DIR/bin/$XRAY_BIN" x25519)
  PRIV=$(awk -F': *' 'tolower($1) ~ /private/ {print $2; exit}' <<<"$keys")
  PUB=$(awk -F': *' 'tolower($1) ~ /public|password/ {print $2; exit}' <<<"$keys")
  [[ -n "$PRIV" && -n "$PUB" ]] || die "could not generate Reality keys"
  SID=$(openssl rand -hex 8)
  DEMO_UUID=$(cat /proc/sys/kernel/random/uuid)
  DEMO_SUB=$(openssl rand -hex 8)
  body=$(jq -n --arg remark "$INBOUND_REMARK" --argjson port "$VLESS_PORT" --arg share "$SHARE_ADDR" \
    --arg target "$REALITY_TARGET" --arg priv "$PRIV" --arg pub "$PUB" --arg sid "$SID" \
    --arg uuid "$DEMO_UUID" --arg sub "$DEMO_SUB" '{
      up:0, down:0, total:0, remark:$remark, enable:true, expiryTime:0, listen:"", port:$port,
      protocol:"vless", tag:("inbound-"+($port|tostring)), excludeFromSub:false,
      shareAddrStrategy:"custom", shareAddr:$share,
      settings:{clients:[{id:$uuid, email:"demo", flow:"", enable:true, limitIp:0, totalGB:0,
                          expiryTime:0, subId:$sub, tgId:0, reset:0, security:"auto"}],
                decryption:"none", fallbacks:[]},
      streamSettings:{network:"tcp", security:"reality",
        realitySettings:{show:false, target:($target+":443"), xver:0, serverNames:[$target],
          privateKey:$priv, shortIds:[$sid],
          settings:{publicKey:$pub, fingerprint:"chrome", serverName:"", spiderX:"/"}},
        tcpSettings:{acceptProxyProtocol:false, header:{type:"none"}}},
      sniffing:{enabled:true, destOverride:["http","tls","quic"], metadataOnly:false, routeOnly:false}}')
  if ! api panel/api/inbounds/add -X POST -H 'Content-Type: application/json' -d "$body" | jq -e '.success' >/dev/null; then
    # older panel builds want the nested objects as JSON strings
    body=$(jq '.settings|=tojson | .streamSettings|=tojson | .sniffing|=tojson' <<<"$body")
    api panel/api/inbounds/add -X POST -H 'Content-Type: application/json' -d "$body" | jq -e '.success' >/dev/null \
      || die "could not create the VLESS inbound"
  fi
fi

# A loopback-only proxy lets the Telegram bot reach Telegram through the same
# fail-closed Finland egress.  It is never exposed on a public interface.
if ! api panel/api/inbounds/list | jq -e '.obj[] | select(.port==10808 and .listen=="127.0.0.1")' >/dev/null; then
  log "creating loopback proxy for the optional Telegram bot"
  bot_proxy=$(jq -n '{up:0,down:0,total:0,remark:"rdnet-bot-local-socks",enable:true,
    expiryTime:0,listen:"127.0.0.1",port:10808,protocol:"mixed",tag:"in-10808-tcpudp",
    excludeFromSub:true,settings:{auth:"noauth",udp:true},streamSettings:{},
    sniffing:{enabled:true,destOverride:["http","tls","quic"],metadataOnly:false,routeOnly:false}}')
  if ! api panel/api/inbounds/add -X POST -H 'Content-Type: application/json' -d "$bot_proxy" | jq -e '.success' >/dev/null; then
    bot_proxy=$(jq '.settings|=tojson | .streamSettings|=tojson | .sniffing|=tojson' <<<"$bot_proxy")
    api panel/api/inbounds/add -X POST -H 'Content-Type: application/json' -d "$bot_proxy" | jq -e '.success' >/dev/null \
      || die "could not create the local Telegram proxy"
  fi
fi

# Iranian DNS resolvers may return a filtering IP for WhatsApp/Meta.  When a
# client resolves locally, sniffing recovers the real HTTP/TLS/QUIC hostname
# without requiring a client-side config change.  Apply it to existing VLESS
# inbounds too, not only to a newly created one.
if [[ -f /etc/x-ui/x-ui.db ]]; then
  cp -a /etc/x-ui/x-ui.db "$STATE/x-ui.pre-sniffing.db"
  sqlite3 /etc/x-ui/x-ui.db <<'SQL'
UPDATE inbounds
SET sniffing = json_set(
  CASE WHEN json_valid(sniffing) THEN sniffing
       ELSE '{"enabled":true,"destOverride":["http","tls","quic"],"metadataOnly":false,"routeOnly":false}' END,
  '$.enabled', json('true'),
  '$.destOverride', json('["http","tls","quic"]'),
  '$.metadataOnly', json('false'),
  '$.routeOnly', json('false')
)
WHERE protocol = 'vless';
SQL
fi
systemctl restart x-ui
sleep 3

# ---------- 9. firewall (last, so SSH is checked first) ------
log "firewall (nftables) — fail-closed: user traffic can only leave through wg-out"
PING_RULE=""; [[ "$ALLOW_PING" == "yes" ]] && PING_RULE='icmp type echo-request limit rate 5/second accept'
cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
# Generated by Alex Server install.sh
flush ruleset

table inet alex_server {
  chain input {
    type filter hook input priority filter; policy drop;
    ct state invalid drop
    iifname "lo" accept
    ct state established,related accept
    $PING_RULE
    tcp dport $SSH_PORT accept comment "ssh"
    tcp dport 80 accept comment "acme http-01 (cert renewal)"
    tcp dport $VLESS_PORT accept comment "vless reality"
    tcp dport $PANEL_PORT accept comment "3x-ui panel"
    tcp dport $SUB_PORT accept comment "3x-ui subscription"
    udp dport { 500, 4500, $WG_PORT } accept comment "ipsec + wireguard"
    udp dport 1701 meta ipsec exists accept comment "l2tp only inside ipsec"
    ip protocol esp accept
  }

  chain forward {
    type filter hook forward priority filter; policy drop;
    ct state invalid drop
    iifname "wg-clients" meta nfproto ipv6 drop
    iifname "ppp*" meta nfproto ipv6 drop
    iifname "wg-clients" oifname "wg-out" tcp flags syn tcp option maxseg size set rt mtu
    iifname "ppp*" oifname "wg-out" tcp flags syn tcp option maxseg size set rt mtu
    iifname "wg-clients" oifname "wg-out" ip saddr $WG_NET.0/24 accept
    iifname "ppp*" oifname "wg-out" ip saddr $L2TP_NET.0/24 accept
    iifname "wg-out" oifname "wg-clients" ip daddr $WG_NET.0/24 ct state established,related accept
    iifname "wg-out" oifname "ppp*" ip daddr $L2TP_NET.0/24 ct state established,related accept
  }

  chain output {
    type filter hook output priority filter; policy accept;
    meta mark 0x200 meta nfproto ipv6 drop comment "kill switch v6"
    meta mark 0x200 oifname != "wg-out" drop comment "kill switch"
  }

  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    oifname "wg-out" ip saddr { $WG_NET.0/24, $L2TP_NET.0/24 } masquerade
  }
}
EOF
nft -c -f /etc/nftables.conf
systemctl enable nftables >/dev/null
nft -f /etc/nftables.conf

# ---------- 10. fail2ban for ssh ------------------------------
cat > /etc/fail2ban/jail.d/alex-server.conf <<EOF
[sshd]
enabled = true
port = $SSH_PORT
backend = systemd
EOF
systemctl enable fail2ban >/dev/null
systemctl restart fail2ban

# ---------- 11. optional RDnet Telegram bot ------------------
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  log "RDnet Telegram bot: V2Ray + L2TP + WireGuard management"
  id -u rdnet-bot >/dev/null 2>&1 || useradd --system --home /var/lib/rdnet-bot --shell /usr/sbin/nologin rdnet-bot
  install -d -o rdnet-bot -g rdnet-bot -m 700 /var/lib/rdnet-bot /var/lib/rdnet-bot/backups
  install -d -o root -g rdnet-bot -m 750 /var/lib/rdnet-vpn-stats
  install -d -m 755 /opt/rdnet-bot /etc/rdnet-bot
  install -m 644 "$REPO_DIR/bot/rdnet_bot.py" "$REPO_DIR/bot/backup.py" "$REPO_DIR/bot/README.md" /opt/rdnet-bot/
  install -m 644 "$REPO_DIR/bot/requirements.txt" /opt/rdnet-bot/requirements.txt
  [[ -x /opt/rdnet-bot/venv/bin/python ]] || python3 -m venv /opt/rdnet-bot/venv
  /opt/rdnet-bot/venv/bin/pip install -q --disable-pip-version-check -r /opt/rdnet-bot/requirements.txt

  install -m 755 "$REPO_DIR/bot/rdnet-vpn-helper.py" /usr/local/sbin/rdnet-vpn-helper
  install -m 755 "$REPO_DIR/bot/ppp-rdnet-up" /etc/ppp/ip-up.d/rdnet-status
  install -m 755 "$REPO_DIR/bot/ppp-rdnet-down" /etc/ppp/ip-down.d/rdnet-status
  install -m 440 "$REPO_DIR/bot/rdnet-bot.sudoers" /etc/sudoers.d/rdnet-bot-vpn
  visudo -cf /etc/sudoers.d/rdnet-bot-vpn >/dev/null
  for unit in rdnet-bot.service rdnet-bot-backup.service rdnet-bot-backup.timer rdnet-bot-health.service rdnet-bot-health.timer rdnet-vpn-sample.service rdnet-vpn-sample.timer; do
    install -m 644 "$REPO_DIR/bot/$unit" "/etc/systemd/system/$unit"
  done

  bot_token=$TOKEN
  [[ -n "$bot_token" ]] || die "could not create the panel API token for RDnet Bot"
  owner_code=$(openssl rand -hex 12)
  [[ -f /root/rdnet-owner-bootstrap-code ]] && owner_code=$(cat /root/rdnet-owner-bootstrap-code)
  sub_path=$(sqlite3 /etc/x-ui/x-ui.db "select value from settings where key='subPath';")
  [[ -n "$sub_path" ]] || sub_path=/sub/
  umask 077
  cat > /etc/rdnet-bot/env <<EOF
BOT_TOKEN=$TELEGRAM_BOT_TOKEN
PANEL_URL=$BASE
PANEL_TOKEN=$bot_token
OWNER_BOOTSTRAP_CODE=$owner_code
DB_PATH=/var/lib/rdnet-bot/bot.db
CHECK_INTERVAL=600
TELEGRAM_PROXY=http://127.0.0.1:10808
SUB_URL_BASE=https://$DOMAIN:$SUB_PORT$sub_path
EOF
  printf '%s\n' "$owner_code" > /root/rdnet-owner-bootstrap-code
  chmod 600 /etc/rdnet-bot/env /root/rdnet-owner-bootstrap-code
  systemctl daemon-reload
  systemctl enable --now rdnet-bot.service rdnet-bot-backup.timer rdnet-bot-health.timer rdnet-vpn-sample.timer >/dev/null
  systemctl restart rdnet-bot.service
  echo "  Telegram bot : enabled"
  echo "  Claim owner  : send /claimowner $owner_code to your bot (one time)"
else
  warn "TELEGRAM_BOT_TOKEN is empty: RDnet Bot was not installed"
fi

# ---------- done ---------------------------------------------
echo
log "DONE"
echo "  Panel        : https://$DOMAIN:$PANEL_PORT/$BASE_PATH/   (user: $PANEL_USER)"
echo "  Subscription : https://$DOMAIN:$SUB_PORT/...  (link per user inside the panel)"
echo "  Egress pubkey: $EGRESS_PUB   <- put this on the egress router as the peer key"
[[ $EGRESS_READY == 1 ]] || warn "egress not configured yet: set EGRESS_PEER_PUBKEY in .env and run ./install.sh again"
echo "  Next steps   : enable 2FA in the panel, add users, run: alex-status"
