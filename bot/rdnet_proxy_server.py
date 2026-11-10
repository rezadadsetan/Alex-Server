#!/usr/bin/python3
"""TLS HTTP forward proxy with shared quotas and simultaneous source-IP limits.

All destinations go through the existing loopback Xray proxy. There is no direct
internet fallback. One process owns the in-memory IP admission registry.
"""
import asyncio
import base64
import ipaddress
import json
import logging
import os
import socket
import ssl
import time
from collections import Counter
from urllib.parse import urlsplit

from rdnet_proxy_store import Store

LOG = logging.getLogger('rdnet-proxy')
HEADER_LIMIT = 16384
CHUNK = 32768


class Denied(Exception):
    def __init__(self, status=403):
        self.status = status


class Admissions:
    def __init__(self):
        self.sessions = {}

    def acquire(self, account, source):
        address = ipaddress.ip_address(source)
        networks = json.loads(account['allowed_ips'])
        if networks and not any(address in ipaddress.ip_network(n) for n in networks):
            raise Denied()
        counts = self.sessions.setdefault(account['username'], Counter())
        if sum(counts.values()) >= 128 or (source not in counts and len(counts) >= account['max_ips']):
            if not counts:
                self.sessions.pop(account['username'], None)
            raise Denied(429)
        counts[source] += 1

    def release(self, username, source):
        counts = self.sessions.get(username)
        if not counts:
            return
        counts[source] -= 1
        if counts[source] <= 0:
            del counts[source]
        if not counts:
            del self.sessions[username]


def parse_request(raw):
    try:
        lines = raw[:-4].decode('iso-8859-1').split('\r\n')
        method, target, version = lines[0].split(' ')
        if version not in ('HTTP/1.0', 'HTTP/1.1') or not method.isalpha():
            raise ValueError()
        headers = []
        for line in lines[1:]:
            name, value = line.split(':', 1)
            if not name or any(ch not in "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ" for ch in name):
                raise ValueError()
            if any(ord(ch) < 32 and ch != '\t' for ch in value):
                raise ValueError()
            headers.append((name.lower(), value.strip()))
        auth = [v for k, v in headers if k == 'proxy-authorization']
        if len(auth) != 1 or not auth[0].startswith('Basic '):
            raise Denied(407)
        username, password = base64.b64decode(auth[0][6:], validate=True).decode('utf-8').split(':', 1)
        if len(username) > 64 or len(password) > 256:
            raise ValueError()
        if method == 'CONNECT':
            parsed = urlsplit('//' + target)
        else:
            parsed = urlsplit(target)
            if parsed.scheme != 'http' or parsed.fragment:
                raise ValueError()
        if parsed.username or parsed.password or not parsed.hostname:
            raise ValueError()
        port = parsed.port or (443 if method == 'CONNECT' else 80)
        if port not in (80, 443):
            raise Denied(403)
        return method, parsed, port, headers, username, password
    except Denied:
        raise
    except (ValueError, UnicodeError):
        raise Denied(400) from None


async def destination(host, port, blocked):
    results = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 8)
    addresses = {r[4][0] for r in results}
    # Resolve once and pass the numeric address upstream, avoiding DNS rebinding.
    if not addresses or any(not ipaddress.ip_address(a).is_global or ipaddress.ip_address(a).is_multicast or a in blocked for a in addresses):
        raise Denied(403)
    return sorted(addresses)[0]


def authority(address, port):
    return f'[{address}]:{port}' if ':' in address else f'{address}:{port}'


class Proxy:
    def __init__(self, store, blocked=(), upstream=('127.0.0.1', 10808)):
        self.store, self.blocked = store, set(blocked)
        self.upstream = upstream
        self.admissions = Admissions()
        self.clients = 0
        self.auth_slots = asyncio.Semaphore(8)
        self.attempts = {}

    async def authenticate(self, username, password, source):
        now = time.monotonic()
        # Bound unauthenticated password-hash work per source IP.
        self.attempts = {k: v for k, v in self.attempts.items() if now - v[0] < 60}
        started, count = self.attempts.get(source, (now, 0))
        if count >= 120:
            raise Denied(429)
        async with self.auth_slots:
            account = await asyncio.to_thread(self.store.authenticate, username, password)
        if not account:
            self.attempts[source] = (started, count + 1)
        return account

    async def pipe(self, reader, writer, account):
        while True:
            data = await asyncio.wait_for(reader.read(CHUNK), 120)
            if not data:
                return
            if not self.store.charge(account['username'], len(data), account['password_hash']):
                return
            writer.write(data)
            await asyncio.wait_for(writer.drain(), 30)

    async def watch(self, account):
        while self.store.enabled(account['username'], account['password_hash']):
            await asyncio.sleep(1)

    async def handle(self, reader, writer):
        self.clients += 1
        acquired, committed, upstream = False, False, None
        tasks = []
        account = None
        source = writer.get_extra_info('peername')[0]
        try:
            if self.clients > 256:
                raise Denied(503)
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
            if len(raw) > HEADER_LIMIT:
                raise Denied(431)
            method, parsed, port, headers, username, password = parse_request(raw)
            account = await self.authenticate(username, password, source)
            if not account:
                raise Denied(407)
            self.admissions.acquire(account, source)
            acquired = True
            address = await destination(parsed.hostname, port, self.blocked)
            upstream_reader, upstream = await asyncio.wait_for(asyncio.open_connection(*self.upstream, limit=HEADER_LIMIT), 8)
            endpoint = authority(address, port)
            # CONNECT for both HTTP and HTTPS: upstream never sees user credentials.
            upstream.write(f'CONNECT {endpoint} HTTP/1.1\r\nHost: {endpoint}\r\n\r\n'.encode('ascii'))
            await asyncio.wait_for(upstream.drain(), 8)
            response = await asyncio.wait_for(upstream_reader.readuntil(b'\r\n\r\n'), 15)
            if len(response) > HEADER_LIMIT or response.split(b'\r\n', 1)[0].split()[1] != b'200':
                raise Denied(502)
            if method == 'CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
            else:
                # One request per proxy connection. Reject ambiguous framing.
                lengths = [v for k, v in headers if k == 'content-length']
                if len(lengths) > 1 or any(k == 'transfer-encoding' for k, _ in headers):
                    raise Denied(400)
                if lengths and (not lengths[0].isascii() or not lengths[0].isdigit()):
                    raise Denied(400)
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                host = authority(parsed.hostname, port)
                stripped = {'proxy-authorization', 'proxy-connection', 'connection', 'host', 'keep-alive', 'upgrade', 'te', 'trailer'}
                connection_tokens = {v.strip().lower() for k, value in headers if k == 'connection' for v in value.split(',')}
                forwarded = [(k, v) for k, v in headers if k not in stripped | connection_tokens]
                outgoing = (f'{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in forwarded) + '\r\n').encode('iso-8859-1')
                if not self.store.charge(username, len(outgoing), account['password_hash']):
                    raise Denied(403)
                upstream.write(outgoing)
                await upstream.drain()
            committed = True
            tasks = [asyncio.create_task(self.pipe(reader, upstream, account)),
                     asyncio.create_task(self.pipe(upstream_reader, writer, account)),
                     asyncio.create_task(self.watch(account))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Denied as exc:
            if not committed:
                challenge = 'Proxy-Authenticate: Basic realm="RDNET"\r\n' if exc.status == 407 else ''
                writer.write(f'HTTP/1.1 {exc.status} Proxy Error\r\n{challenge}Content-Length: 0\r\nConnection: close\r\n\r\n'.encode())
        except (OSError, ValueError, IndexError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            if not committed:
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        except Exception:
            LOG.error('Connection closed because a required operation failed')
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if acquired:
                self.admissions.release(account['username'], source)
            if upstream:
                upstream.close()
            writer.close()
            self.clients -= 1


async def main():
    os.umask(0o077)
    config = json.load(open('/etc/rdnet-proxy/config.json'))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['cert'], config['key'])
    store = Store()
    store.initialize()
    proxy = Proxy(store, config['blocked_ips'])
    server = await asyncio.start_server(proxy.handle, '0.0.0.0', config['port'], ssl=context, ssl_handshake_timeout=10, limit=HEADER_LIMIT)
    LOG.info('TLS proxy ready on port %s', config['port'])
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
