# Keep connect timeouts separate from relay idle behavior

This is a generic corrective proposal for a Python HTTP CONNECT relay, not an
installed bridge component or a claim that a running tunnel has been changed.
Keep target paths, destinations, credentials and live operational evidence in
protected local records.

## Mechanism

A socket created with `socket.create_connection(address, timeout=20)` retains
that per-operation timeout. Successfully reading an HTTP CONNECT response does
not reset it. A later relay `recv()` can therefore raise after twenty seconds
without incoming bytes; `sendall()` can also time out. If a broad `OSError`
handler silently exits, the parent SSH client may report a broken pipe rather
than the original timeout. The socket behavior follows Python's
[create_connection and settimeout contract](https://docs.python.org/3/library/socket.html#socket.socket.settimeout).

Keepalives more frequent than the retained timeout may hide the defect by
causing peer responses. That is different from removing the unintended idle
cutoff. An observed broken pipe alone does not identify this cause; verify the
actual relay source, operation order and timestamped lifecycle separately.

## Minimal proposal

After successful CONNECT status validation, before starting either relay pump:

```diff
 if " 200 " not in status:
     sys.exit(f"CONNECT failed: {status}")
+# Connection/CONNECT I/O is bounded; SSH owns established-session liveness.
+s.settimeout(None)
 rest = buf.split(b"\r\n\r\n", 1)[1]
```

The timeout still applies while connecting and exchanging the CONNECT headers.
Only the established relay socket returns to blocking mode. This leaves the
SSH client's keepalive/failure-detection settings, trust checks, routing and
supervisor configuration unchanged. It does not establish that every possible
network interruption or child-process cleanup case is handled correctly.

## Evidence and remaining checks

`tests/test_relay_timeout_contract.py` uses a local socket pair and synthetic
CONNECT bytes. It shows the timeout surviving the handshake, the idle receive
failure, explicit clearing, subsequent byte delivery and EOF. It opens no SSH
connection, invokes no credentials, changes no live settings and injects no
live network failure. The shortened timeout is a test stand-in, not a timing
measurement of a deployed tunnel.

Before an authorized rollout, confirm the source revision and insertion point,
retain the existing SSH liveness configuration, and separately verify proxy
child exit/cleanup and reconnect behavior in the supported execution context.
A later controlled live comparison must distinguish initial connection failure,
established-session idle, genuine peer loss and supervisor restart delay.
Do not shorten or disable SSH failure detection to conceal a relay defect.
