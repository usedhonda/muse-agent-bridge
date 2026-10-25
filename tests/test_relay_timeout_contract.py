"""Offline socket contract: CONNECT deadline must not become a relay idle limit.

No SSH, external network, credential helpers, or live relay are exercised.
"""
import socket
import unittest


class RelayTimeoutContractTests(unittest.TestCase):
    def test_connect_timeout_remains_on_relay_reads_until_cleared(self):
        relay, peer = socket.socketpair()
        try:
            relay.settimeout(0.03)  # shortened stand-in for the connect timeout
            peer.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            response = b''
            while b'\r\n\r\n' not in response:
                response += relay.recv(4096)
            self.assertIn(b' 200 ', response.split(b'\r\n', 1)[0])
            self.assertEqual(relay.gettimeout(), 0.03)
            with self.assertRaises(socket.timeout):
                relay.recv(1)  # handshake did not remove the timeout
            relay.settimeout(None)  # proposed change, only after CONNECT success
            self.assertIsNone(relay.gettimeout())
            peer.sendall(b'synthetic SSH bytes')
            self.assertEqual(relay.recv(1024), b'synthetic SSH bytes')
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(relay.recv(1), b'')  # EOF still terminates a relay
        finally:
            relay.close()
            peer.close()
