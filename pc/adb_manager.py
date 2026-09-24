"""adb command-line wrapper: device listing and port forwarding."""
import shutil
import subprocess


class AdbNotFound(RuntimeError):
    """adb is not runnable, with a message that does not depend on the OS locale."""


class Adb:
    def __init__(self, path="adb"):
        self.path = shutil.which(path) or path

    def _run(self, *args, check=True):
        try:
            return subprocess.run(
                [self.path, *args], capture_output=True, text=True, check=check)
        except FileNotFoundError:
            # subprocess raises a locale-specific OSError; the UI wants English.
            raise AdbNotFound(
                f"adb not found ({self.path!r}). Install Android platform-tools "
                f"and put adb on PATH, or pass an explicit path: Adb(path=...)")
        except OSError as e:
            raise AdbNotFound(f"adb could not be started: {e}")

    def devices(self):
        out = []
        for line in self._run("devices", "-l").stdout.splitlines()[1:]:
            if line.strip():
                p = line.split()
                out.append({"serial": p[0], "state": p[1], "desc": line})
        return out

    def forward(self, serial, local_port, remote_port):
        """Forward the PC's local_port to remote_port on the phone (clears any leftover forwards first)."""
        self._run("-s", serial, "forward", "--remove-all", check=False)
        self._run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}")

    def remove_forward(self, serial, local_port):
        self._run("-s", serial, "forward", "--remove", f"tcp:{local_port}", check=False)
