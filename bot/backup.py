import datetime
import os
import sqlite3
from pathlib import Path

directory = Path("/var/lib/rdnet-bot/backups")
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
for label, source in (("bot", "/var/lib/rdnet-bot/bot.db"), ("vpn-stats", "/var/lib/rdnet-vpn-stats/stats.db")):
    if not Path(source).exists():
        continue
    target = directory / f"{label}-{datetime.date.today().isoformat()}.db"
    src = sqlite3.connect(source)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    os.chmod(target, 0o600)
cutoff = datetime.datetime.now().timestamp() - 14 * 86400
for item in directory.glob("*.db"):
    if item.stat().st_mtime < cutoff:
        item.unlink()





