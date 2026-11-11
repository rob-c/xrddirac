"""Written by `diset-capture --with-hook`: log the plaintext of a TLS client."""
import atexit
import os
import ssl

_out = os.environ.get("XRD_DISET_TRACE")
if _out:
    _files = {}

    def _close_all():
        for handle in _files.values():
            try:
                handle.close()
            except Exception:
                pass

    atexit.register(_close_all)

    def _write(name, data):
        try:
            handle = _files.get(name)
            if handle is None:
                handle = _files[name] = open(os.path.join(_out, name), "ab", buffering=0)
            handle.write(bytes(data))
        except Exception:
            pass  # an observer must never break the thing it observes

    _send, _recv, _recv_into = (
        ssl.SSLSocket.send,
        ssl.SSLSocket.recv,
        ssl.SSLSocket.recv_into,
    )

    def send(self, data, flags=0):
        sent = _send(self, data, flags)
        _write("client.bin", memoryview(data)[:sent])
        return sent

    def sendall(self, data, flags=0):
        # Loop the *original* send rather than the C sendall: the C one calls back
        # into the patched send, which would log every frame twice.
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            written = _send(self, view[sent:], flags)
            _write("client.bin", view[sent : sent + written])
            sent += written

    def recv(self, buflen, flags=0):
        data = _recv(self, buflen, flags)
        _write("server.bin", data)
        return data

    def recv_into(self, buffer, nbytes=0, flags=0):
        read = _recv_into(self, buffer, nbytes, flags)
        _write("server.bin", memoryview(buffer)[:read])
        return read

    ssl.SSLSocket.send, ssl.SSLSocket.sendall = send, sendall
    ssl.SSLSocket.recv, ssl.SSLSocket.recv_into = recv, recv_into
