"""Run locally: python -m unittest discover -s bot -p test_proxy.py -v"""
import asyncio
import base64
import concurrent.futures
import importlib.util
import json
import os
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from rdnet_proxy_store import Store, allowed_networks, quota_bytes
from rdnet_proxy_server import Admissions, Denied, Proxy, destination, parse_request
from rdnet_proxy_admin import execute


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, 'accounts.db'))
        self.store.initialize()
        self.account = self.store.create('دفتر تهران', '1.5', 2)

    def test_credentials_unique_hashed_and_persistent(self):
        a = self.account
        row = self.store.authenticate(a['username'], a['password'])
        self.assertIsNotNone(row)
        self.assertNotIn(a['password'], str(self.store.get(a['username'])))
        self.assertIsNone(self.store.authenticate(a['username'], 'wrong'))
        other = self.store.create('دفتر تهران', 2, 2)
        self.assertNotEqual(other['username'], a['username'])
        self.assertEqual(row['quota'], int(1.5 * 1024 ** 3))
        self.assertTrue(self.store.charge(a['username'], 100, row['password_hash']))
        self.assertEqual(Store(self.store.path).get(a['username'])['used'], 100)

    def test_atomic_shared_quota(self):
        a = self.store.get(self.account['username'])
        with self.store.db() as c:
            c.execute('UPDATE accounts SET quota=100 WHERE username=?', (a['username'],))
        def charge(_):
            return self.store.charge(a['username'], 10, a['password_hash'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(charge, range(20)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(self.store.get(a['username'])['used'], 100)
        self.assertIsNone(self.store.authenticate(a['username'], self.account['password']))

    def test_revoke_and_reset_invalidate_existing_sessions(self):
        a = self.store.get(self.account['username'])
        password = self.store.reset_password(a['username'])
        self.assertFalse(self.store.enabled(a['username'], a['password_hash']))
        self.assertFalse(self.store.charge(a['username'], 1, a['password_hash']))
        current = self.store.authenticate(a['username'], password)
        self.assertIsNotNone(current)
        self.store.disable(a['username'])
        self.assertFalse(self.store.enabled(a['username'], current['password_hash']))
        self.assertIsNone(self.store.authenticate(a['username'], password))

    def test_validation_and_helper_boundary(self):
        for n in ('0', '-1', 'NaN', 'Infinity', '1e30', None):
            with self.assertRaises(ValueError):
                quota_bytes(n)
        self.assertEqual(allowed_networks('203.0.113.7/24'), ['203.0.113.0/24'])
        with self.assertRaises(ValueError):
            allowed_networks('example.com')
        for request in ({'action': 'shell'}, {'action': 'list', 'path': '/etc/shadow'},
                        {'action': 'disable', 'username': ';id'},
                        {'action': 'create', 'name': 'x', 'gb': '1', 'max_ips': 0, 'acl': '*'}):
            with self.assertRaises(ValueError):
                execute(request, self.store)
        self.assertNotIn('password_hash', json.dumps(execute({'action': 'list'}, self.store)))


class AdmissionTests(unittest.TestCase):
    def test_multiple_connections_one_ip_and_limit(self):
        registry = Admissions()
        a = {'username': 'p_test', 'allowed_ips': '[]', 'max_ips': 2}
        registry.acquire(a, '1.1.1.1')
        registry.acquire(a, '1.1.1.1')
        registry.acquire(a, '8.8.8.8')
        with self.assertRaises(Denied):
            registry.acquire(a, '9.9.9.9')
        registry.release('p_test', '1.1.1.1')
        with self.assertRaises(Denied):
            registry.acquire(a, '9.9.9.9')
        registry.release('p_test', '1.1.1.1')
        registry.acquire(a, '9.9.9.9')
        self.assertEqual(len(registry.sessions['p_test']), 2)

    def test_acl_and_connection_ceiling(self):
        registry = Admissions()
        a = {'username': 'p_test', 'allowed_ips': '["203.0.113.0/24"]', 'max_ips': 2}
        with self.assertRaises(Denied):
            registry.acquire(a, '8.8.8.8')
        for _ in range(128):
            registry.acquire(a, '203.0.113.5')
        with self.assertRaises(Denied):
            registry.acquire(a, '203.0.113.5')


class ParsingTests(unittest.TestCase):
    def test_missing_duplicate_auth_and_private_protocols(self):
        with self.assertRaises(Denied) as caught:
            parse_request(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n')
        self.assertEqual(caught.exception.status, 407)
        auth = b'Proxy-Authorization: Basic ' + base64.b64encode(b'user:pass') + b'\r\n'
        for first, headers in [(b'CONNECT example.com:22', auth), (b'GET ftp://example.com/', auth),
                               (b'CONNECT example.com:443', auth * 2)]:
            with self.assertRaises(Denied):
                parse_request(first + b' HTTP/1.1\r\n' + headers + b'\r\n')


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, 'accounts.db'))
        self.store.initialize()
        self.account = self.store.create('test', 1, 2)
        self.upstreams = 0
        self.upstream_writers = []
        self.client_writers = []
        async def echo(reader, writer):
            self.upstreams += 1
            self.upstream_writers.append(writer)
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                self.assertNotIn(b'Proxy-Authorization', request)
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                while data := await reader.read(32768):
                    writer.write(data)
                    await writer.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
        self.upstream = await asyncio.start_server(echo, '127.0.0.1', 0)
        port = self.upstream.sockets[0].getsockname()[1]
        self.proxy = Proxy(self.store, upstream=('127.0.0.1', port))
        self.server = await asyncio.start_server(self.proxy.handle, '127.0.0.1', 0, limit=16384)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        for writer in self.client_writers + self.upstream_writers:
            writer.close()
        self.server.close()
        self.upstream.close()
        await self.server.wait_closed()
        await self.upstream.wait_closed()
        for _ in range(30):
            if self.proxy.clients == 0:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.proxy.clients, 0)
        self.tmp.cleanup()

    async def connect(self, password=None, host='1.1.1.1'):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.client_writers.append(writer)
        auth = base64.b64encode(f"{self.account['username']}:{password or self.account['password']}".encode()).decode()
        writer.write(f'CONNECT {host}:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n'.encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        return reader, writer, response

    async def test_authenticated_relay_and_exact_bidirectional_charge(self):
        reader, writer, response = await self.connect()
        self.assertIn(b'200', response)
        writer.write(b'hello')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(5), 3), b'hello')
        self.assertEqual(self.store.get(self.account['username'])['used'], 10)

    async def test_denied_auth_and_private_destination_never_reach_upstream(self):
        _, _, response = await self.connect(password='wrong')
        self.assertIn(b'407', response)
        _, _, response = await self.connect(host='127.0.0.1')
        self.assertIn(b'403', response)
        self.assertEqual(self.upstreams, 0)

    async def test_quota_blocks_before_excess_forwarding(self):
        with self.store.db() as c:
            c.execute('UPDATE accounts SET quota=20')
        reader, writer, response = await self.connect()
        self.assertIn(b'200', response)
        writer.write(b'12345678')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(8), 3), b'12345678')
        writer.write(b'12345678')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b'')
        self.assertEqual(self.store.get(self.account['username'])['used'], 16)

    async def test_revocation_closes_existing_session(self):
        reader, _, response = await self.connect()
        self.assertIn(b'200', response)
        self.store.disable(self.account['username'])
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')

    async def test_reset_closes_existing_session(self):
        reader, _, response = await self.connect()
        self.assertIn(b'200', response)
        self.store.reset_password(self.account['username'])
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')

    async def test_no_direct_fallback(self):
        self.upstream.close()
        await self.upstream.wait_closed()
        _, _, response = await self.connect()
        self.assertIn(b'502', response)

    async def test_http_request_keeps_host_and_strips_proxy_password(self):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.client_writers.append(writer)
        auth = base64.b64encode(f"{self.account['username']}:{self.account['password']}".encode()).decode()
        writer.write(f'GET http://1.1.1.1/test?q=yes HTTP/1.1\r\nHost: attacker.invalid\r\nProxy-Authorization: Basic {auth}\r\n\r\n'.encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
        self.assertIn(b'GET /test?q=yes HTTP/1.1', response)
        self.assertIn(b'Host: 1.1.1.1:80', response)
        self.assertNotIn(b'Proxy-Authorization', response)
        self.assertNotIn(self.account['password'].encode(), response)

    async def test_destination_checks(self):
        for host in ('127.0.0.1', '169.254.169.254', '10.0.0.1', '::1', '224.0.0.1', 'ff02::1'):
            with self.assertRaises(Denied):
                await destination(host, 443, set())
        with self.assertRaises(Denied):
            await destination('1.1.1.1', 443, {'1.1.1.1'})

    async def test_verified_tls_and_plaintext_rejection(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = os.path.join(self.tmp.name, 'cert.pem'), os.path.join(self.tmp.name, 'key.pem')
        with open(cert_path, 'wb') as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        with open(key_path, 'wb') as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert_path, key_path)
        tls_server = await asyncio.start_server(self.proxy.handle, '127.0.0.1', 0, ssl=server_context, ssl_handshake_timeout=2)
        self.addAsyncCleanup(tls_server.wait_closed)
        self.addCleanup(tls_server.close)
        port = tls_server.sockets[0].getsockname()[1]
        client_context = ssl.create_default_context(cafile=cert_path)
        reader, writer = await asyncio.open_connection('127.0.0.1', port, ssl=client_context, server_hostname='localhost')
        self.client_writers.append(writer)
        auth = base64.b64encode(f"{self.account['username']}:{self.account['password']}".encode()).decode()
        writer.write(f'CONNECT 1.1.1.1:443 HTTP/1.1\r\nProxy-Authorization: Basic {auth}\r\n\r\n'.encode())
        await writer.drain()
        self.assertIn(b'200', await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3))
        writer.write(b'tls-check')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(9), 3), b'tls-check')
        plain_reader, plain_writer = await asyncio.open_connection('127.0.0.1', port)
        self.client_writers.append(plain_writer)
        plain_writer.write(b'CONNECT example.com:443 HTTP/1.1\r\n\r\n')
        await plain_writer.drain()
        self.assertEqual(await asyncio.wait_for(plain_reader.read(), 3), b'')


if __name__ == '__main__':
    unittest.main()
