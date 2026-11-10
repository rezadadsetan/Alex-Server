#!/usr/bin/python3
"""Fixed-action sudo bridge. Never accepts file paths, commands or SQL."""
import json
import os
import re
import sys

sys.path.insert(0, '/opt/rdnet-proxy')
from rdnet_proxy_store import Store


def execute(request, store):
    action = request.get('action')
    if action == 'create' and set(request) == {'action', 'name', 'gb', 'max_ips', 'acl'}:
        return store.create(request['name'], request['gb'], request['max_ips'], request['acl'])
    if action == 'list' and set(request) == {'action'}:
        return {'accounts': store.list()}
    if action in ('disable', 'reset') and set(request) == {'action', 'username'}:
        username = request['username']
        if not isinstance(username, str) or not re.fullmatch(r'p_[a-f0-9]{12}', username):
            raise ValueError('شناسه حساب نامعتبر است.')
        if action == 'disable':
            store.disable(username)
            return {}
        return {'username': username, 'password': store.reset_password(username)}
    raise ValueError('عملیات نامعتبر است.')


def main():
    os.umask(0o077)
    try:
        if os.geteuid() != 0 or len(sys.argv) != 1:
            raise ValueError('این helper فقط از مسیر مدیریتی قابل اجراست.')
        data = sys.stdin.buffer.read(4097)
        if len(data) > 4096:
            raise ValueError('درخواست بیش از حد بزرگ است.')
        result = execute(json.loads(data), Store())
        with open('/etc/rdnet-proxy/config.json') as config_file:
            config = json.load(config_file)
        result['endpoint'] = {'host': config['public_host'], 'port': config['port'], 'scheme': 'https'}
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
    except Exception:
        # No tracebacks, request bodies, hashes, paths or credentials in logs.
        print(json.dumps({'ok': False, 'error': 'عملیات انجام نشد؛ ورودی و آماده‌بودن سرویس را بررسی کنید.'}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
