from __future__ import annotations

import ipaddress
import os
import queue
import re
import select
import shutil
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from fuckclassroom.core.config import AppConfig


ProgressCallback = Callable[[int, str], None]
_ENDPOINT_PATTERN = re.compile(
    r"^RUST_HY2_SOCKS\s+port=(\d+)\s+user=([^\s]+)\s+pass=([^\s]+)$"
)
_MAX_PROXY_HEADER_BYTES = 64 * 1024
_HY2_BUILD_TIMEOUT_SECONDS = 900.0


class Hy2ProxyError(RuntimeError):
    pass


class _Hy2ApplicationControlBlocked(Hy2ProxyError):
    pass


@dataclass(frozen=True)
class Hy2ProxyStatus:
    enabled: bool
    running: bool
    message: str
    proxy_url: str = ""
    runner_path: str = ""
    source_dir: str = ""
    building: bool = False
    build_detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _SocksEndpoint:
    port: int
    username: str
    password: str


class Hy2ProxyManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._build_lock = threading.Lock()
        self._build_thread: threading.Thread | None = None
        self._build_started_at: float | None = None
        self._build_detail = ""
        self._process: subprocess.Popen[str] | None = None
        self._bridge: _ConnectProxyServer | None = None
        self._bridge_thread: threading.Thread | None = None
        self._proxy_url = ""
        self._last_error = ""
        self._build_error = ""
        if self.config.hy2_enabled:
            self.start_auto_build()

    def start_auto_build(self) -> bool:
        """Start one background build when the bundled runner is missing or stale.

        A failed startup build is deliberately not retried by status polling. Otherwise
        every settings-page refresh can erase the real Cargo error and reset the UI to
        "00:00 / preparing" forever. A process restart or an explicit ensure/build call
        may retry after the environment has been fixed.
        """
        if (
            not self.config.hy2_enabled
            or self._build_error
            or not self._runner_needs_rebuild()
        ):
            return False
        current = self._build_thread
        if current is not None and current.is_alive():
            return False
        with self._build_lock:
            current = self._build_thread
            if current is not None and current.is_alive():
                return False
            if self._build_error or not self._runner_needs_rebuild():
                return False
            self._build_detail = "正在准备 Cargo 构建"
            self._build_started_at = time.monotonic()
            thread = threading.Thread(
                target=self._auto_build_worker,
                name="hy2-auto-build",
                daemon=True,
            )
            self._build_thread = thread
            thread.start()
            return True

    def wait_for_auto_build(self, timeout_seconds: float | None = None) -> bool:
        thread = self._build_thread
        if thread is None:
            return True
        thread.join(timeout=None if timeout_seconds is None else max(0.0, timeout_seconds))
        return not thread.is_alive()

    def ensure_built(
        self,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Ensure the bundled Rust runner exists and matches the checked-in source."""
        with self._build_lock:
            if not force and not self._runner_needs_rebuild():
                self._build_error = ""
                self._build_detail = ""
                self._build_started_at = None
                _report(progress, 100, "Hy2 代理组件已是最新版本")
                return False
            if self._build_started_at is None:
                self._build_started_at = time.monotonic()
            if not self._build_detail:
                self._build_detail = "正在准备 Cargo 构建"
            # An explicit attempt is a real retry, so clear the previous failure while it runs.
            self._build_error = ""
            try:
                self._compile_runner(progress)
            except Hy2ProxyError as exc:
                self._build_error = str(exc)
                self._build_detail = ""
                self._build_started_at = None
                raise
            except Exception as exc:
                wrapped = Hy2ProxyError(
                    f"Hy2 代理组件构建异常：{type(exc).__name__}: {exc}"
                )
                self._build_error = str(wrapped)
                self._build_detail = ""
                self._build_started_at = None
                raise wrapped from exc
            self._build_error = ""
            self._build_detail = ""
            self._build_started_at = None
            return True

    def ensure_started(self, timeout_seconds: float = 30.0) -> str:
        if not self.config.hy2_enabled:
            raise Hy2ProxyError("Hy2 代理尚未启用")

        # Normal path: use the verified CI-built runner. If Windows Smart App
        # Control rejects that unsigned executable (WinError/os error 4551), rebuild
        # the same Rust source locally and retry once. Rust is therefore only needed
        # as a Windows fallback, not for normal installations.
        self.ensure_built()
        try:
            return self._start_runner_once(timeout_seconds)
        except _Hy2ApplicationControlBlocked as blocked:
            self._build_detail = "预编译 Hy2 被 Windows 应用控制拦截，正在尝试本地 Rust 构建"
            try:
                self.ensure_built(force=True)
            except Hy2ProxyError as build_exc:
                self._last_error = (
                    f"{blocked}；自动切换到本地 Rust 构建也失败：{build_exc}。"
                    "正常安装不需要 Rust；仅此兜底路径需要 Cargo。"
                )
                raise Hy2ProxyError(self._last_error) from build_exc

            try:
                return self._start_runner_once(timeout_seconds)
            except _Hy2ApplicationControlBlocked as local_blocked:
                self._last_error = (
                    "Windows 应用控制同时阻止了预编译和本地构建的 Hy2 runner。"
                    "程序不会自动关闭 Windows 安全功能；请关闭 Hy2，改用公网教务或 WebVPN。"
                )
                raise Hy2ProxyError(self._last_error) from local_blocked

    def _start_runner_once(self, timeout_seconds: float) -> str:
        with self._lock:
            if self._is_running_locked():
                return self._proxy_url
            self._stop_locked()
            runner = self.config.hy2_runner_path
            if not runner.is_file():
                self._last_error = "Hy2 代理组件未生成可执行文件"
                raise Hy2ProxyError(self._last_error)

            output: queue.Queue[str | None] = queue.Queue()
            creation_flags = (
                subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            )
            try:
                process = subprocess.Popen(
                    [str(runner)],
                    cwd=str(runner.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creation_flags,
                )
            except OSError as exc:
                if _is_windows_application_control_error(exc):
                    self._last_error = (
                        "Windows 应用控制阻止了 Hy2 代理组件"
                        "（WinError/os error 4551，通常来自 Smart App Control）"
                    )
                    raise _Hy2ApplicationControlBlocked(self._last_error) from exc
                self._last_error = f"Hy2 代理组件启动失败：{exc}"
                raise Hy2ProxyError(self._last_error) from exc

            self._process = process
            reader = threading.Thread(
                target=_read_process_lines,
                args=(process, output),
                daemon=True,
            )
            reader.start()
            endpoint = self._wait_for_endpoint(process, output, timeout_seconds)
            try:
                bridge = _ConnectProxyServer(endpoint)
            except OSError as exc:
                self._stop_locked()
                self._last_error = f"本地 Hy2 代理端口创建失败：{exc}"
                raise Hy2ProxyError(self._last_error) from exc

            bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
            bridge_thread.start()
            self._bridge = bridge
            self._bridge_thread = bridge_thread
            self._proxy_url = f"http://127.0.0.1:{bridge.server_address[1]}"
            self._last_error = ""
            return self._proxy_url

    def build_runner(self, progress: ProgressCallback | None = None) -> str:
        """Force a rebuild. Kept for backward compatibility with the old endpoint."""
        self.ensure_built(progress, force=True)
        with self._lock:
            self._stop_locked()
        if self.config.hy2_enabled:
            _report(progress, 82, "Hy2 代理组件已构建，正在启动连接")
            self.ensure_started()
            _report(progress, 100, "Hy2 代理已启动")
        else:
            _report(progress, 100, "Hy2 代理组件已准备好")
        return "/settings?hy2_built=1"

    def get_status(self) -> Hy2ProxyStatus:
        # Status reads must not turn a completed failure into an endless retry loop.
        if (
            self.config.hy2_enabled
            and not self._build_error
            and self._runner_needs_rebuild()
        ):
            self.start_auto_build()
        build_thread = self._build_thread
        building = bool(build_thread and build_thread.is_alive())
        build_detail = self._build_detail
        with self._lock:
            running = self._is_running_locked()
            if running:
                message = "Hy2 代理正在运行"
            elif building:
                elapsed = 0
                if self._build_started_at is not None:
                    elapsed = max(0, int(time.monotonic() - self._build_started_at))
                message = f"Hy2 代理组件正在自动构建（已等待 {_format_elapsed(elapsed)}）"
                if build_detail:
                    message += f"：{build_detail}"
            elif self._build_error:
                message = f"Hy2 代理自动构建失败：{self._build_error}"
            elif not self.config.hy2_enabled:
                message = "Hy2 代理未启用（启用后将自动构建）"
            elif self._runner_needs_rebuild():
                message = "Hy2 代理组件等待自动构建"
            else:
                message = "代理组件已就绪，将在访问本科教务时启动"
            return Hy2ProxyStatus(
                enabled=self.config.hy2_enabled,
                running=running,
                message=message,
                proxy_url=self._proxy_url if running else "",
                runner_path=str(self.config.hy2_runner_path),
                source_dir=str(self.config.hy2_source_dir),
                building=building,
                build_detail=build_detail if building else "",
            )

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _auto_build_worker(self) -> None:
        try:
            self.ensure_built()
        except Hy2ProxyError:
            # The error is stored by ensure_built and exposed through get_status().
            return
        except Exception as exc:
            # Last-resort protection for failures outside the normal build wrapper.
            self._build_error = f"Hy2 自动构建线程异常：{type(exc).__name__}: {exc}"
            self._build_detail = ""
            self._build_started_at = None

    def _runner_needs_rebuild(self) -> bool:
        runner = self.config.hy2_runner_path
        if not runner.is_file():
            return True
        try:
            runner_mtime = runner.stat().st_mtime_ns
        except OSError:
            return True

        source_dir = self.config.hy2_source_dir
        tracked: list[Path] = [source_dir / "Cargo.toml"]
        for name in ("Cargo.lock", "build.rs", "rust-toolchain.toml", "rust-toolchain"):
            candidate = source_dir / name
            if candidate.is_file():
                tracked.append(candidate)
        tracked.extend(path for path in source_dir.rglob("*.rs") if path.is_file())
        try:
            return any(path.is_file() and path.stat().st_mtime_ns > runner_mtime for path in tracked)
        except OSError:
            return True

    def _compile_runner(self, progress: ProgressCallback | None = None) -> None:
        source_dir = self.config.hy2_source_dir
        manifest = source_dir / "Cargo.toml"
        if not manifest.is_file():
            raise Hy2ProxyError(f"没有找到内置 Hy2 Rust 工程：{manifest}")
        cargo = shutil.which("cargo")
        if not cargo:
            raise Hy2ProxyError(
                "没有找到 Cargo。正常使用预编译 Hy2 不需要 Rust；"
                "当前只有本地构建兜底需要 rustup/Cargo，项目会按 rust-toolchain.toml 使用 Rust 1.96.0"
            )

        target_dir = self.config.data_dir / "hy2" / "build"
        target_dir.mkdir(parents=True, exist_ok=True)
        command = [
            cargo,
            "build",
            "--release",
            "--bin",
            "hy2_serve",
            "--manifest-path",
            str(manifest),
            "--target-dir",
            str(target_dir),
        ]
        if (source_dir / "Cargo.lock").is_file():
            command.insert(3, "--locked")

        self._build_detail = "Cargo 已启动，正在检查/下载依赖"
        _report(progress, 8, "正在编译内置 Hy2 代理组件")
        creation_flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        env = os.environ.copy()
        env.setdefault("CARGO_TERM_COLOR", "never")
        output: queue.Queue[str | None] = queue.Queue()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(source_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
                env=env,
            )
        except OSError as exc:
            raise Hy2ProxyError(f"Hy2 代理组件构建失败：{exc}") from exc

        reader = threading.Thread(target=_read_process_lines, args=(process, output), daemon=True)
        reader.start()
        deadline = time.monotonic() + _HY2_BUILD_TIMEOUT_SECONDS
        last_line = ""
        timed_out = False
        while True:
            if time.monotonic() >= deadline:
                timed_out = True
                if process.poll() is None:
                    process.kill()
                break
            if process.poll() is not None and output.empty():
                break
            try:
                line = output.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                if process.poll() is not None:
                    break
                continue
            stripped = _compact_status_line(line)
            if not stripped:
                continue
            last_line = stripped
            self._build_detail = stripped
            _report(progress, 12, f"Hy2 构建：{stripped}")

        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait(timeout=5)

        if timed_out:
            raise Hy2ProxyError("Hy2 代理组件构建超过 15 分钟，已停止 Cargo；请检查 Rust/crates.io 网络")
        if return_code != 0:
            raise Hy2ProxyError(
                f"Hy2 代理组件构建失败：{last_line or f'Cargo 返回状态 {return_code}'}"
            )

        built_name = "hy2_serve.exe" if os.name == "nt" else "hy2_serve"
        built_runner = target_dir / "release" / built_name
        if not built_runner.is_file():
            raise Hy2ProxyError(f"构建完成但没有找到代理程序：{built_runner}")
        runner = self.config.hy2_runner_path
        runner.parent.mkdir(parents=True, exist_ok=True)
        self._build_detail = "Cargo 构建完成，正在安装本地代理组件"
        shutil.copy2(built_runner, runner)
        _report(progress, 78, "Hy2 代理组件已构建")

    def _wait_for_endpoint(
        self,
        process: subprocess.Popen[str],
        output: queue.Queue[str | None],
        timeout_seconds: float,
    ) -> _SocksEndpoint:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while time.monotonic() < deadline:
            if process.poll() is not None and output.empty():
                break
            try:
                line = output.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            stripped = line.strip()
            match = _ENDPOINT_PATTERN.fullmatch(stripped)
            if match:
                return _SocksEndpoint(
                    port=int(match.group(1)),
                    username=match.group(2),
                    password=match.group(3),
                )

        self._stop_locked()
        self._last_error = "Hy2 代理连接超时或提前退出"
        raise Hy2ProxyError(self._last_error)

    def _is_running_locked(self) -> bool:
        return bool(
            self._process
            and self._process.poll() is None
            and self._bridge
            and self._bridge_thread
            and self._bridge_thread.is_alive()
        )

    def _stop_locked(self) -> None:
        bridge = self._bridge
        process = self._process
        self._bridge = None
        self._bridge_thread = None
        self._process = None
        self._proxy_url = ""
        if bridge:
            bridge.shutdown()
            bridge.server_close()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


class _ConnectProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, endpoint: _SocksEndpoint) -> None:
        self.endpoint = endpoint
        super().__init__(("127.0.0.1", 0), _ConnectProxyHandler)


class _ConnectProxyHandler(socketserver.BaseRequestHandler):
    server: _ConnectProxyServer

    def handle(self) -> None:
        upstream: socket.socket | None = None
        try:
            self.request.settimeout(10)
            header = _read_proxy_header(self.request)
            method, authority, _ = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace").split(" ", 2)
            if method.upper() != "CONNECT":
                self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                return
            host, port = _parse_authority(authority)
            upstream = _socks5_connect(self.server.endpoint, host, port)
            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.settimeout(None)
            upstream.settimeout(None)
            _relay(self.request, upstream)
        except (Hy2ProxyError, OSError, ValueError):
            try:
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if upstream:
                upstream.close()


def _read_process_lines(process: subprocess.Popen[str], output: queue.Queue[str | None]) -> None:
    try:
        if process.stdout:
            for line in process.stdout:
                output.put(line)
    finally:
        output.put(None)


def _read_proxy_header(client: socket.socket) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            raise Hy2ProxyError("代理客户端提前关闭")
        data.extend(chunk)
        if len(data) > _MAX_PROXY_HEADER_BYTES:
            raise Hy2ProxyError("代理请求头过大")
    return bytes(data)


def _parse_authority(authority: str) -> tuple[str, int]:
    if authority.startswith("["):
        closing = authority.find("]")
        if closing < 0 or closing + 2 > len(authority) or authority[closing + 1] != ":":
            raise ValueError("无效的 IPv6 CONNECT 地址")
        host = authority[1:closing]
        raw_port = authority[closing + 2 :]
    else:
        host, separator, raw_port = authority.rpartition(":")
        if not separator:
            raise ValueError("CONNECT 地址缺少端口")
    port = int(raw_port)
    if not host or not 1 <= port <= 65535:
        raise ValueError("无效的 CONNECT 地址")
    return host, port


def _socks5_connect(endpoint: _SocksEndpoint, host: str, port: int) -> socket.socket:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return _socks5_connect_once(endpoint, host, port)
        except (Hy2ProxyError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.15 * (attempt + 1))
    raise Hy2ProxyError(f"Hy2 连接 {host}:{port} 失败：{last_error}") from last_error


def _socks5_connect_once(endpoint: _SocksEndpoint, host: str, port: int) -> socket.socket:
    upstream = socket.create_connection(("127.0.0.1", endpoint.port), timeout=10)
    try:
        upstream.sendall(b"\x05\x01\x02")
        if _recv_exact(upstream, 2) != b"\x05\x02":
            raise Hy2ProxyError("Hy2 SOCKS5 代理拒绝身份认证")
        username = endpoint.username.encode("utf-8")
        password = endpoint.password.encode("utf-8")
        if len(username) > 255 or len(password) > 255:
            raise Hy2ProxyError("Hy2 SOCKS5 临时凭据过长")
        upstream.sendall(
            b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password
        )
        auth_response = _recv_exact(upstream, 2)
        if auth_response != b"\x01\x00":
            raise Hy2ProxyError("Hy2 SOCKS5 临时凭据无效")

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            encoded_host = host.encode("idna")
            if len(encoded_host) > 255:
                raise Hy2ProxyError("代理目标域名过长")
            destination = b"\x03" + bytes([len(encoded_host)]) + encoded_host
        else:
            destination = (b"\x01" if address.version == 4 else b"\x04") + address.packed
        upstream.sendall(b"\x05\x01\x00" + destination + port.to_bytes(2, "big"))
        response = _recv_exact(upstream, 4)
        if response[0] != 5 or response[1] != 0:
            raise Hy2ProxyError(f"Hy2 SOCKS5 连接目标失败，状态码 {response[1]}")
        if response[3] == 1:
            _recv_exact(upstream, 4)
        elif response[3] == 4:
            _recv_exact(upstream, 16)
        elif response[3] == 3:
            _recv_exact(upstream, _recv_exact(upstream, 1)[0])
        else:
            raise Hy2ProxyError("Hy2 SOCKS5 返回了未知地址类型")
        _recv_exact(upstream, 2)
        return upstream
    except Exception:
        upstream.close()
        raise


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise Hy2ProxyError("Hy2 SOCKS5 连接提前关闭")
        data.extend(chunk)
    return bytes(data)


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    while True:
        readable, _, exceptional = select.select(sockets, [], sockets, 30)
        if exceptional or not readable:
            return
        for source in readable:
            target = right if source is left else left
            data = source.recv(64 * 1024)
            if not data:
                return
            target.sendall(data)


def _is_windows_application_control_error(exc: OSError) -> bool:
    code = getattr(exc, "winerror", None)
    if code is None:
        code = getattr(exc, "errno", None)
    if code == 4551:
        return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "application control policy",
            "smart app control",
            "应用程序控制策略",
        )
    )


def _last_nonempty_line(content: str) -> str:
    return next((line.strip() for line in reversed(content.splitlines()) if line.strip()), "")


def _compact_status_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()[-220:]


def _format_elapsed(seconds: int) -> str:
    minutes, seconds = divmod(max(0, seconds), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(percent, message)
