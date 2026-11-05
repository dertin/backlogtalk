"""Single-instance command delivery using per-user local sockets."""

from __future__ import annotations

import hashlib
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtNetwork import QLocalServer, QLocalSocket


class Instance:
    def __init__(self, root: Path):
        self.lock = None
        self.socket = None
        self.server = None
        self.path = None
        self.root = root

    def acquire_or_send(self, command: str) -> bool:
        """Return True for the primary instance, False after delivering a command."""
        windows = sys.platform == "win32"
        key = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        if windows:
            # The per-user temporary directory inherits Windows user ACLs.
            directory = Path(tempfile.gettempdir()) / "backlogtalk"
        else:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
            directory = Path(runtime) / f"backlogtalk-{os.getuid()}"
            if len(os.fsencode(directory / f"{key}.sock")) >= 100:
                directory = Path("/tmp") / f"backlogtalk-{os.getuid()}"
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or (not windows and directory.stat().st_uid != os.getuid()):
            raise RuntimeError("Directorio de instancia no seguro")
        if not windows:
            directory.chmod(0o700)
        self.path = directory / f"{key}.sock"
        self.lock = open(directory / f"{key}.lock", "a+b")
        try:
            if windows:
                import msvcrt

                self.lock.seek(0, os.SEEK_END)
                if self.lock.tell() == 0:
                    self.lock.write(b"0")
                    self.lock.flush()
                self.lock.seek(0)
                try:
                    msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    if error.errno not in (11, 13, 36):
                        raise
                    raise BlockingIOError from error
            else:
                import fcntl

                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            for attempt in range(20):
                try:
                    if windows:
                        client = QLocalSocket()
                        client.connectToServer(f"backlogtalk-{key}")
                        if not client.waitForConnected(500):
                            raise ConnectionRefusedError(client.errorString())
                        if client.write(command.encode("ascii")) < 0:
                            raise RuntimeError("No se pudo enviar el atajo a la instancia abierta.")
                        if client.bytesToWrite():
                            client.waitForBytesWritten(500)
                        if client.bytesToWrite():
                            raise RuntimeError(
                                "No se pudo confirmar el envío del atajo; revisá la ventana abierta."
                            )
                        client.disconnectFromServer()
                        return False
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(0.5)
                        client.connect(str(self.path))
                        client.sendall(command.encode("ascii"))
                    return False
                except (FileNotFoundError, ConnectionRefusedError) as error:
                    if attempt == 19:
                        raise RuntimeError(
                            "La aplicación no respondió al atajo; revisá la ventana abierta."
                        ) from error
                    time.sleep(0.1)
        if windows:
            self.server = QLocalServer()
            self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if not self.server.listen(f"backlogtalk-{key}"):
                raise RuntimeError(f"No se pudo iniciar la instancia: {self.server.errorString()}")
            return True
        self.path.unlink(missing_ok=True)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(self.path))
        if not windows:
            self.path.chmod(0o600)
        self.socket.listen(8)
        self.socket.setblocking(False)
        return True

    def receive(self) -> list[str]:
        commands = []
        if self.server is not None:
            while self.server.hasPendingConnections() or self.server.waitForNewConnection(0)[0]:
                client = self.server.nextPendingConnection()
                if client is None:
                    break
                if client.bytesAvailable() or client.waitForReadyRead(100):
                    try:
                        message = bytes(client.read(32)).decode("ascii")
                    except UnicodeDecodeError:
                        message = ""
                    if message in ("capture", "capture-system", "open", "open-history"):
                        commands.append(message)
                client.close()
            return commands
        if self.socket is None:
            return commands
        while True:
            try:
                client, _ = self.socket.accept()
            except BlockingIOError:
                return commands
            with client:
                client.settimeout(0.1)
                try:
                    message = client.recv(32).decode("ascii")
                except (OSError, UnicodeDecodeError):
                    continue
                if message in ("capture", "capture-system", "open", "open-history"):
                    commands.append(message)

    def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        if self.socket is not None:
            self.socket.close()
            self.socket = None
            self.path.unlink(missing_ok=True)
        if self.lock is not None:
            self.lock.close()
            self.lock = None
