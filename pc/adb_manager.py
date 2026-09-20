"""adb 命令行封装：设备列表与端口转发。"""
import shutil
import subprocess


class Adb:
    def __init__(self, path="adb"):
        self.path = shutil.which(path) or path

    def _run(self, *args, check=True):
        return subprocess.run([self.path, *args], capture_output=True, text=True, check=check)

    def devices(self):
        out = []
        for line in self._run("devices", "-l").stdout.splitlines()[1:]:
            if line.strip():
                p = line.split()
                out.append({"serial": p[0], "state": p[1], "desc": line})
        return out

    def forward(self, serial, local_port, remote_port):
        """把 PC 的 local_port 转发到手机上的 remote_port（先清理可能残留的 forward）。"""
        self._run("-s", serial, "forward", "--remove-all", check=False)
        self._run("-s", serial, "forward", f"tcp:{local_port}", f"tcp:{remote_port}")

    def remove_forward(self, serial, local_port):
        self._run("-s", serial, "forward", "--remove", f"tcp:{local_port}", check=False)
