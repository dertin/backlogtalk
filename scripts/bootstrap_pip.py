"""Install a verified pip wheel before downloading project dependencies."""

import hashlib
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

WHEEL_NAME = "pip-26.3-py3-none-any.whl"
WHEEL_URL = (
    "https://files.pythonhosted.org/packages/07/25/"
    "ee8c38ae72d66b844ce8eb9711ba905dba21ca1a8a0c960057c41cd23337/" + WHEEL_NAME
)
WHEEL_SHA256 = "f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13"
MAX_WHEEL_BYTES = 5 * 1024 * 1024


def main():
    with urllib.request.urlopen(WHEEL_URL, timeout=30) as response:
        wheel_data = response.read(MAX_WHEEL_BYTES + 1)
    if len(wheel_data) > MAX_WHEEL_BYTES or hashlib.sha256(wheel_data).hexdigest() != WHEEL_SHA256:
        raise ValueError("No se pudo verificar la integridad del paquete pip.")
    with tempfile.TemporaryDirectory(prefix="backlogtalk-pip-") as directory:
        wheel = Path(directory) / WHEEL_NAME
        wheel.write_bytes(wheel_data)
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                'import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module("pip", run_name="__main__")',
                str(wheel),
                "--isolated",
                "install",
                "--force-reinstall",
                "--no-index",
                "--no-deps",
                "--disable-pip-version-check",
                str(wheel),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
