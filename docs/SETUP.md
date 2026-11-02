<div dir="rtl">

# راهنمای کامل راه‌اندازی Alex Server

در این راهنما همهٔ مقدارها **نمونه** هستند. `vpn.example.com`، `203.0.113.10` (سرور) و `198.51.100.20` (روتر خارجی) را با مقدارهای واقعی خودت عوض کن.

## فهرست
1. [پیش‌نیازها](#1)
2. [آماده‌سازی دامنه](#2)
3. [تنظیم فایل .env](#3)
4. [اجرای نصب](#4)
5. [تنظیم سمت خروجی (میکروتیک + Windscribe)](#5)
6. [تنظیمات پنل بعد از نصب](#6)
7. [ساخت کاربر](#7)
8. [تنظیم کلاینت‌ها](#8)
9. [جابه‌جایی به سرور جدید](#9)
10. [عیب‌یابی](#10)
11. [سؤال‌های پرتکرار](./FAQ.md)

---

<a id="1"></a>
## ۱. پیش‌نیازها

| مورد | توضیح |
|---|---|
| سرور | Ubuntu 24.04 خام، حداقل ۱ هسته و ۱ گیگ RAM، با IPv4 عمومی |
| دامنه | یک دامنه یا ساب‌دامین با رکورد A به IP سرور (برای گواهی HTTPS پنل) |
| خروجی | یک روتر یا سرور خارج از کشور که WireGuard دارد (میکروتیک RouterOS 7، یا هر لینوکسی) |
| دسترسی | SSH با root یا sudo |

پورت‌هایی که روی سرور باز می‌شوند:

| پورت | کاربرد |
|---|---|
| `SSH_PORT` (TCP) | مدیریت |
| `80` (TCP) | فقط برای گرفتن و تمدید گواهی Let's Encrypt |
| `443` (TCP) | VLESS + Reality |
| `PANEL_PORT` (TCP) | پنل 3x-ui |
| `SUB_PORT` (TCP، پیش‌فرض 2096) | لینک سابسکریپشن |
| `500`, `4500` (UDP) | IPsec |
| `1701` (UDP) | L2TP، فقط داخل IPsec |
| `51820` (UDP) | WireGuard کاربران |

<a id="2"></a>
## ۲. آماده‌سازی دامنه

- برای `vpn.example.com` یک رکورد **A** با مقدار `203.0.113.10` بساز.
- اگر از Cloudflare استفاده می‌کنی، پروکسی (ابر نارنجی) را **خاموش** کن (DNS only).
- **پنهان کردن دامنهٔ اصلی در کانفیگ کاربران:** در `.env` مقدار `SHARE_ADDR` را IP سرور یا یک دامنهٔ دوم و بی‌ربط بگذار. کاربران فقط همان را در کانفیگشان می‌بینند.

<a id="3"></a>
## ۳. تنظیم فایل `.env`

```bash
cp .env.example .env
nano .env
```

| متغیر | چه بگذاری |
|---|---|
| `DOMAIN` | دامنهٔ پنل، مثلاً `vpn.example.com` |
| `ACME_EMAIL` | ایمیل برای Let's Encrypt |
| `SHARE_ADDR` | آدرسی که در کانفیگ کاربران می‌آید. خالی = همان `DOMAIN` |
| `INBOUND_REMARK` | اسمی که کاربر در اپ می‌بیند، مثلاً `Server` |
| `SSH_PORT` | پورتی که SSH **همین الان** روی آن است. اگر اشتباه باشد، نصب متوقف می‌شود تا قفل نشوی |
| `PANEL_USER` / `PANEL_PASS` | ورود به پنل. رمز قوی بگذار |
| `PANEL_PATH` | مسیر مخفی پنل. خالی = تصادفی (پیشنهادی) |
| `REALITY_TARGET` | یک سایت **داخلی و سریع** با TLS 1.3، مثلاً `www.digikala.com` |
| `L2TP_PSK` | کلید مشترک L2TP. طولانی و تصادفی بگذار |
| `EGRESS_ENDPOINT` | `IP:port` روتر خارجی، مثلاً `198.51.100.20:51830` |
| `EGRESS_PEER_PUBKEY` | کلید عمومی WireGuard روتر خارجی. در اجرای اول خالی بماند |
| `EGRESS_ADDRESS` | IP این سرور داخل تونل، پیش‌فرض `10.203.70.2/30` |
| `TELEGRAM_BOT_TOKEN` | اختیاری؛ توکن دریافتی از BotFather. با پرکردن آن، RDnet Bot کاملاً خودکار نصب می‌شود |

برای ساختن رمز و PSK تصادفی:
```bash
openssl rand -base64 24
```

<a id="4"></a>
## ۴. اجرای نصب

روش تعاملی پیشنهادی:

```bash
sudo ./easy-install.sh
```

این روش دامنه، ایمیل، پورت فعلی SSH، ورود پنل، PSK، مشخصات تونل خروجی و در صورت تمایل توکن تلگرام را سؤال می‌کند؛ رمزها روی صفحه نمایش داده نمی‌شوند و فایل `.env` با مجوز `600` ساخته می‌شود.

اگر `.env` را دستی ساخته‌ای:

```bash
sudo ./install.sh
```

اسکریپت به ترتیب این کارها را انجام می‌دهد:
1. نصب پکیج‌ها: strongSwan، xl2tpd، WireGuard، nftables و fail2ban.
2. تنظیم sysctl: forwarding، policy routing و conntrack با ظرفیت ۶۵۵۳۶.
3. ساخت کلید تونل خروجی (`wg-out`) و routing جدول 200 با blackhole.
4. راه‌اندازی WireGuard کاربران و L2TP/IPsec.
5. نصب **3x-ui** با نسخهٔ ثابت و گواهی Let's Encrypt برای دامنه.
6. قفل دائمی **هستهٔ Xray** روی نسخهٔ سازگار؛ حتی Restart یا آپدیت پنل نمی‌تواند آن را جایگزین کند.
7. تنظیم Xray از طریق API پنل: خروجی فقط از تونل، DNS از نوع DoH، بستن آدرس‌های خصوصی و فعال‌کردن تشخیص HTTP/TLS/QUIC برای عبور از DNS جعلی.
8. ساخت inbound مربوط به **VLESS + Reality** با یک کاربر نمونه به اسم `demo`.
9. فایروال fail-closed و fail2ban.
10. در صورت وجود توکن، نصب RDnet Bot با مدیریت دکمه‌ای V2Ray، L2TP و WireGuard، QR، وضعیت و ثبت مصرف.

در پایان، **کلید عمومی تونل خروجی** این سرور چاپ می‌شود. آن را در قدم بعد لازم داری.

اگر توکن تلگرام وارد کرده باشی، در پایان یک دستور مانند `/claimowner CODE` نیز چاپ می‌شود. آن را فقط یک بار در چت خصوصی بات بفرست. پس از آن `/admin` یا `/vpnadmin` منوی دکمه‌ای مدیریت را باز می‌کند؛ برای ساخت V2Ray فقط نام را می‌نویسی و حجم، مدت و تعداد دستگاه را با دکمه انتخاب می‌کنی. WireGuard فایل و QR می‌دهد و L2TP نام و رمز را مرحله‌به‌مرحله می‌پرسد.

> اجرای اول بدون `EGRESS_PEER_PUBKEY` عمداً fail-closed است: همه‌چیز نصب می‌شود ولی کاربران اینترنت ندارند. بعد از تنظیم روتر خارجی، کلید را در `.env` بگذار و دوباره `sudo ./install.sh` بزن.

<a id="5"></a>
## ۵. تنظیم سمت خروجی

### گزینهٔ الف: میکروتیک (RouterOS 7)

`<IRAN_PUBKEY>` همان کلیدی است که `install.sh` چاپ کرد. `<WAN>` اسم اینترفیس اینترنت میکروتیک است.

```routeros
/interface wireguard add name=wg-iran listen-port=51830 mtu=1420
/interface wireguard peers add interface=wg-iran public-key="<IRAN_PUBKEY>" allowed-address=10.203.70.2/32 persistent-keepalive=25s
/ip address add address=10.203.70.1/30 interface=wg-iran
/ip firewall filter add chain=input protocol=udp dst-port=51830 action=accept place-before=0 comment="wg-iran"
/ip firewall nat add chain=srcnat src-address=10.203.70.2 out-interface=<WAN> action=masquerade comment="wg-iran"
/ip firewall mangle add chain=forward in-interface=wg-iran protocol=tcp tcp-flags=syn action=change-mss new-mss=clamp-to-pmtu
/interface wireguard print
```
کلید عمومی `wg-iran` را از خروجی دستور آخر بردار و در `.env` سرور به‌عنوان `EGRESS_PEER_PUBKEY` بگذار.

### گزینهٔ ب: خروج از Windscribe روی همان میکروتیک

اگر Windscribe روی میکروتیک یک اینترفیس WireGuard دارد (در اینجا `<WIND>`)، ترافیک ایران را فقط به آن بفرست. route نوع blackhole جلوی برگشت ترافیک به WAN را می‌گیرد:

```routeros
/routing table add name=via-wind fib
/ip route add dst-address=0.0.0.0/0 gateway=<WIND> routing-table=via-wind distance=1
/ip route add dst-address=0.0.0.0/0 blackhole routing-table=via-wind distance=250
/routing rule add src-address=10.203.70.2/32 action=lookup-only-in-table table=via-wind
/ip firewall nat add chain=srcnat src-address=10.203.70.2 out-interface=<WIND> action=masquerade comment="iran-via-wind"
/ip firewall nat disable [find comment="wg-iran"]
/ip firewall mangle add chain=forward out-interface=<WIND> protocol=tcp tcp-flags=syn action=change-mss new-mss=clamp-to-pmtu
```
**تست:** با یک کاربر وصل شو و `ifconfig.me` را باز کن. باید IP ویندسکرایب را ببینی.

### گزینهٔ ج: یک سرور لینوکسی خارجی
یک WireGuard ساده با `Address = 10.203.70.1/30`، peer با کلید این سرور و `AllowedIPs = 10.203.70.2/32`، به‌همراه masquerade روی اینترفیس اینترنت آن سرور.

<a id="6"></a>
## ۶. تنظیمات پنل بعد از نصب

آدرس پنل در پایان نصب چاپ می‌شود: `https://vpn.example.com:52667/<path>/`. آن را Bookmark کن.

1. **Settings ← Security ← Two-factor**: ورود دو مرحله‌ای را روشن کن و QR را با Google Authenticator اسکن کن.
2. **Settings ← Subscription**: مطمئن شو سابسکریپشن روشن است و پورتش همان `SUB_PORT` است.
3. کاربر `demo` را بعد از تست حذف کن.
4. هستهٔ Xray توسط installer روی نسخهٔ تست‌شده نگه داشته می‌شود. پنل را می‌توان جداگانه آپدیت کرد؛ برای تغییر هسته ابتدا سازگاری کلاینت‌ها را آزمایش کن.

<a id="7"></a>
## ۷. ساخت کاربر

### VLESS (از پنل)
**Inbounds ←** روی ردیف inbound گزینهٔ **Add Client** را بزن و این‌ها را پر کن:

| فیلد | نمونهٔ پروفایل ۳۰ گیگ | نمونهٔ پروفایل ۵۰ گیگ |
|---|---|---|
| Email (اسم) | `ali` | `sara` |
| Group | `30GB-1M` | `50GB-1M` |
| Total Flow | `30` GB | `50` GB |
| Expiry | Start after first use + `30` روز | همان |
| IP Limit | `1` | `1` |
| Flow | خالی | خالی |

در این نسخهٔ پنل، Group فقط یک برچسب است. حجم و مدت را باید دستی وارد کنی. بعد از Save، با آیکون **QR / Info** لینک سابسکریپشن را بردار و به کاربر بده.

### L2TP
```bash
sudo l2tp-user add ali 'StrongPass1'
sudo l2tp-user passwd ali 'NewPass2'
sudo l2tp-user del ali
sudo l2tp-user list
```
رمز را همیشه داخل `' '` بنویس. رمز نباید فاصله، `"` یا `\` داشته باشد.

### WireGuard
```bash
sudo wg-user add sara     # prints config + QR code
sudo wg-user show sara
sudo wg-user del sara
sudo wg-user list
```

<a id="8"></a>
## ۸. تنظیم کلاینت‌ها

### VLESS: اندروید و ویندوز
- **اندروید:** v2rayNG (آخرین نسخه) یا Hiddify. در اپ: **+ ← Import from clipboard** یا اسکن QR سابسکریپشن.
- **ویندوز:** v2rayN یا Hiddify.
- اگر کاربر کانفیگ قدیمی دارد، آن را پاک کند و سابسکریپشن را Update کند.

### L2TP: ویندوز
Settings ← Network & Internet ← VPN ← Add VPN:
- Provider: **Windows (built-in)**
- Server: آدرس `SHARE_ADDR` یا `DOMAIN`
- Type: **L2TP/IPsec with pre-shared key** و همان `L2TP_PSK`
- Username و Password همان چیزی است که با `l2tp-user` ساختی.

اگر خطای **809** گرفتی (کلاینت یا سرور پشت NAT است)، این را در PowerShell با دسترسی Admin بزن و ویندوز را ریستارت کن:
```powershell
New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\PolicyAgent" -Name AssumeUDPEncapsulationContextOnSendRule -PropertyType DWord -Value 2 -Force
```
> **اندروید ۱۲ به بعد L2TP ندارد.** برای اندروید از VLESS یا WireGuard استفاده کن.

### L2TP: iOS و مک
Settings ← VPN ← Add VPN Configuration ← **L2TP**. Server، Account، Password و Secret (همان PSK) را وارد کن.

### WireGuard
اپ رسمی WireGuard ← اسکن QR که `wg-user add` چاپ می‌کند.

<a id="9"></a>
## ۹. جابه‌جایی به سرور جدید

```bash
# old server
sudo ./backup.sh            # -> /root/alex-backup-<date>.tar.gz  (copy it off the server)

# new server (fresh Ubuntu 24.04)
git clone https://github.com/rezadadsetan/Alex-Server.git && cd Alex-Server
cp /path/to/old/.env .env   # same .env (change SSH_PORT if different)
sudo ./install.sh
sudo ./restore.sh /root/alex-backup-<date>.tar.gz
```
بعد **رکورد DNS را به IP جدید عوض کن.** کلیدها، UUIDها، کاربران و مصرف‌ها همان می‌مانند، پس **هیچ کاربری لازم نیست کانفیگش را عوض کند.** روتر خارجی هم دست نمی‌خورد، چون همان کلید تونل برمی‌گردد.

> اگر `SHARE_ADDR` را IP گذاشته بودی و نه دامنه، بعد از جابه‌جایی کاربران باید سابسکریپشن را Update کنند یا کانفیگ جدید بگیرند. برای همین دامنه بهتر است.

<a id="10"></a>
## ۱۰. عیب‌یابی

```bash
sudo alex-status
```

| علامت | علت محتمل | راه‌حل |
|---|---|---|
| همهٔ کاربران وصل می‌شوند ولی اینترنت ندارند | تونل خروجی برقرار نیست | در `alex-status` وضعیت handshake تونل `wg-out` را ببین. کلید و endpoint را در `.env` و روی روتر چک کن |
| پینگ VLESS هست ولی اتصال fail می‌شود | لینک کلید عمومی (`pbk`) ندارد، یا کانفیگ قدیمی است | کانفیگ را پاک کن و سابسکریپشن را Update کن |
| «TLS handshake timeout» یا کندی شدید | `REALITY_TARGET` خارجی یا کند است | یک سایت داخلی سریع بگذار. سرعت را با `curl -so /dev/null -w '%{time_appconnect}\n' https://SITE` بسنج |
| با v2rayN ویندوز کار می‌کند ولی با v2rayNG گوشی نه | هستهٔ Xray سرور pre-release است | `XRAY_VERSION` پایدار در `.env` بگذار و `install.sh` را دوباره اجرا کن. اپ گوشی را هم آپدیت کن |
| سابسکریپشن خالی است | inbound از سابسکریپشن کنار گذاشته شده، یا پورت بسته است | در تنظیمات inbound گزینهٔ Exclude from subscription خاموش باشد و `SUB_PORT` باز باشد |
| ping سرور جواب نمی‌دهد | ICMP بسته است | در `.env` مقدار `ALLOW_PING=yes` بگذار. برای تست اتصال، تست TCP بهتر است: `Test-NetConnection DOMAIN -Port 443` |
| دو کاربر L2TP پشت یک اینترنت هم‌دیگر را قطع می‌کنند | محدودیت ذاتی L2TP/IPsec | آن کاربران را به VLESS یا WireGuard منتقل کن |
| با زیاد شدن کاربران بعضی سایت‌ها باز نمی‌شوند | جدول conntrack پر شده | اسکریپت ظرفیت را ۶۵۵۳۶ گذاشته. با `alex-status` بررسی کن |
| واتس‌اپ دیر متصل می‌شود یا روی Connecting می‌ماند | DNS اپراتور مقصد Meta را به IP فیلترینگ می‌فرستد | نصب‌کننده sniffing نوع HTTP/TLS/QUIC را برای همهٔ ورودی‌های VLESS فعال می‌کند؛ installer را دوباره اجرا و سرویس را Restart کن |
| بعد از آپدیت پنل، VPN وصل می‌شود ولی اینترنت ندارد | آپدیت پنل هستهٔ Xray را با نسخه ناسازگار جایگزین کرده | installer را دوباره اجرا کن. قفل systemd نسخه پایدار را هنگام هر شروع سرویس برمی‌گرداند |

برای علت‌ها، تست‌های دقیق و راه‌حل‌های بیشتر، **[FAQ](./FAQ.md)** را ببین.

</div>
