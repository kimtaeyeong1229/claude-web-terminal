#!/usr/bin/env python3
"""Claude CLI Web Terminal Server.

A web-based multi-session terminal UI for Claude CLI.
Each tab runs an independent Claude CLI instance in a pseudo-terminal.
"""

import asyncio
import fcntl
import glob as globmod
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import termios
import time
import uuid
from pathlib import Path

from aiohttp import web

STATIC_DIR = Path(__file__).parent / "static"
CLAUDE_CMD = os.environ.get("CLAUDE_CMD", shutil.which("claude") or "claude")
MY_PID = os.getpid()


class Session:
    def __init__(self, name: str, working_dir: str = None, extra_args: list = None):
        self.id = str(uuid.uuid4())[:8]
        self.name = name
        self.created_at = time.time()
        self.working_dir = os.path.abspath(os.path.expanduser(working_dir)) if working_dir else str(Path.home())
        self.extra_args = extra_args or []
        self.master_fd = None
        self.pid = None
        self.alive = False
        self.websockets: list[web.WebSocketResponse] = []
        self.scrollback = bytearray()
        self.max_scrollback = 200_000  # ~200KB

    def spawn(self):
        """Spawn a Claude CLI process in a pty."""
        pid, fd = pty.openpty()
        self.master_fd = fd
        child_pid = os.fork()
        if child_pid == 0:
            # Child process
            os.close(fd)
            os.setsid()
            slave_fd = pid
            # Set up slave as controlling terminal
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.chdir(self.working_dir)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            cmd_args = [CLAUDE_CMD] + self.extra_args
            os.execvpe(CLAUDE_CMD, cmd_args, env)
        else:
            os.close(pid)
            self.pid = child_pid
            self.alive = True
            # Set initial size (80x24)
            self.resize(80, 24)

    def resize(self, cols: int, rows: int):
        """Resize the pty."""
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass

    def write(self, data: bytes):
        """Write data to the pty."""
        if self.master_fd is not None and self.alive:
            try:
                os.write(self.master_fd, data)
            except OSError:
                self.alive = False

    def read(self) -> bytes | None:
        """Non-blocking read from the pty."""
        if self.master_fd is None:
            return None
        try:
            r, _, _ = select.select([self.master_fd], [], [], 0)
            if r:
                data = os.read(self.master_fd, 65536)
                if data:
                    # Append to scrollback
                    self.scrollback.extend(data)
                    if len(self.scrollback) > self.max_scrollback:
                        self.scrollback = self.scrollback[-self.max_scrollback:]
                    return data
                else:
                    self.alive = False
                    return None
            return b""
        except OSError:
            self.alive = False
            return None

    def kill(self):
        """Kill the session process."""
        if self.pid and self.alive:
            try:
                os.kill(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.alive = False
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


class SessionManager:
    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self._read_task = None

    def create_session(self, name: str, working_dir: str = None, extra_args: list = None) -> Session:
        session = Session(name, working_dir, extra_args)
        session.spawn()
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Session | None:
        return self.sessions.get(session_id)

    def delete_session(self, session_id: str):
        session = self.sessions.pop(session_id, None)
        if session:
            session.kill()

    def list_sessions(self) -> list[dict]:
        # Reap dead processes
        for sid in list(self.sessions):
            s = self.sessions[sid]
            if s.pid:
                try:
                    pid, status = os.waitpid(s.pid, os.WNOHANG)
                    if pid != 0:
                        s.alive = False
                except ChildProcessError:
                    s.alive = False
        return [
            {
                "id": s.id,
                "name": s.name,
                "alive": s.alive,
                "created_at": s.created_at,
                "working_dir": s.working_dir,
            }
            for s in self.sessions.values()
        ]

    async def start_read_loop(self):
        """Background task that reads from all ptys and broadcasts to websockets."""
        while True:
            for session in list(self.sessions.values()):
                if not session.alive:
                    # Notify connected websockets that session died
                    for ws in session.websockets[:]:
                        try:
                            await ws.send_json({"type": "exit"})
                        except Exception:
                            pass
                    continue
                data = session.read()
                if data:
                    for ws in session.websockets[:]:
                        try:
                            await ws.send_bytes(data)
                        except Exception:
                            session.websockets.remove(ws)
            await asyncio.sleep(0.01)  # 10ms poll


manager = SessionManager()


def detect_external_claude_processes() -> list[dict]:
    """Scan /proc for claude processes not managed by this server."""
    managed_pids = {s.pid for s in manager.sessions.values() if s.pid}
    results = []
    uid = os.getuid()

    for proc_dir in globmod.glob("/proc/[0-9]*"):
        try:
            pid = int(os.path.basename(proc_dir))

            # Skip our own managed sessions and our own server process
            if pid in managed_pids or pid == MY_PID:
                continue

            # Check owner
            stat = os.stat(proc_dir)
            if stat.st_uid != uid:
                continue

            # Read cmdline
            with open(f"{proc_dir}/cmdline", "rb") as f:
                cmdline_raw = f.read()
            if not cmdline_raw:
                continue
            cmdline_parts = cmdline_raw.rstrip(b"\x00").split(b"\x00")
            cmdline = [p.decode("utf-8", errors="replace") for p in cmdline_parts]

            # Check if this is a claude process (not defunct, not our bash wrapper)
            exe_name = os.path.basename(cmdline[0]) if cmdline else ""
            if exe_name != "claude":
                continue

            # Read status to check it's not a zombie
            with open(f"{proc_dir}/status") as f:
                status_text = f.read()
            state_match = re.search(r"^State:\s+(\S)", status_text, re.MULTILINE)
            if state_match and state_match.group(1) == "Z":
                continue  # zombie

            # Get working directory
            try:
                cwd = os.readlink(f"{proc_dir}/cwd")
            except OSError:
                cwd = "?"

            # Get TTY from /proc/PID/stat (field 7, 0-indexed 6)
            try:
                with open(f"{proc_dir}/stat") as f:
                    stat_line = f.read()
                # Parse past the comm field (which can contain spaces/parens)
                close_paren = stat_line.rfind(")")
                fields_after = stat_line[close_paren + 2:].split()
                tty_nr = int(fields_after[4])  # field index 6 overall, 4 after state
                if tty_nr > 0:
                    major = (tty_nr >> 8) & 0xFF
                    minor = tty_nr & 0xFF
                    if major == 136:  # pts
                        tty_name = f"pts/{minor}"
                    else:
                        tty_name = f"tty{minor}"
                else:
                    tty_name = "?"
            except Exception:
                tty_name = "?"

            # Get start time from /proc/PID directory creation time
            start_time = os.stat(proc_dir).st_mtime

            # Get args (skip the exe name)
            args = " ".join(cmdline[1:]) if len(cmdline) > 1 else ""

            results.append({
                "pid": pid,
                "cwd": cwd,
                "tty": tty_name,
                "args": args,
                "start_time": start_time,
            })

        except (OSError, ValueError, IndexError):
            continue

    results.sort(key=lambda x: x["start_time"])
    return results


# --- HTTP API ---

async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_sessions(request):
    return web.json_response(manager.list_sessions())


async def api_create_session(request):
    body = await request.json()
    name = body.get("name", f"Session {len(manager.sessions) + 1}")
    working_dir = body.get("working_dir", str(Path.home()))
    working_dir = os.path.expanduser(working_dir)
    working_dir = os.path.abspath(working_dir)
    if not os.path.isdir(working_dir):
        return web.json_response({"error": f"Directory not found: {working_dir}"}, status=400)
    extra_args = body.get("extra_args", [])
    session = manager.create_session(name, working_dir, extra_args)
    return web.json_response({"id": session.id, "name": session.name})


async def api_rename_session(request):
    session_id = request.match_info["id"]
    body = await request.json()
    session = manager.get_session(session_id)
    if not session:
        return web.json_response({"error": "not found"}, status=404)
    session.name = body.get("name", session.name)
    return web.json_response({"id": session.id, "name": session.name})


async def api_delete_session(request):
    session_id = request.match_info["id"]
    manager.delete_session(session_id)
    return web.json_response({"ok": True})


async def api_external_processes(request):
    """List claude processes running outside this server."""
    procs = detect_external_claude_processes()
    return web.json_response(procs)


async def api_kill_external(request):
    """Kill an external claude process by PID."""
    pid = int(request.match_info["pid"])
    uid = os.getuid()
    # Safety: only kill processes owned by us and that are actually claude
    try:
        stat = os.stat(f"/proc/{pid}")
        if stat.st_uid != uid:
            return web.json_response({"error": "not owned by you"}, status=403)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
        parts = cmdline.rstrip(b"\x00").split(b"\x00")
        exe_name = os.path.basename(parts[0].decode("utf-8", errors="replace")) if parts else ""
        if exe_name != "claude":
            return web.json_response({"error": "not a claude process"}, status=400)
        os.kill(pid, signal.SIGTERM)
        return web.json_response({"ok": True, "pid": pid})
    except ProcessLookupError:
        return web.json_response({"error": "process not found"}, status=404)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_list_dirs(request):
    """List directories under a given path for the folder browser."""
    raw = request.query.get("path", "~")
    target = os.path.expanduser(raw)
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        return web.json_response({"error": f"Not a directory: {target}"}, status=400)
    dirs = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith('.'):
                continue
            dirs.append(entry.name)
    except PermissionError:
        pass
    return web.json_response({"path": target, "dirs": dirs})


async def ws_terminal(request):
    """WebSocket endpoint for terminal I/O."""
    session_id = request.match_info["id"]
    session = manager.get_session(session_id)
    if not session:
        return web.json_response({"error": "not found"}, status=404)

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    session.websockets.append(ws)

    # Send scrollback buffer so reconnecting clients see history
    if session.scrollback:
        await ws.send_bytes(bytes(session.scrollback))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                # Control messages (JSON)
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "resize":
                        session.resize(data["cols"], data["rows"])
                except (json.JSONDecodeError, KeyError):
                    pass
            elif msg.type == web.WSMsgType.BINARY:
                # Terminal input
                session.write(msg.data)
            elif msg.type == web.WSMsgType.ERROR:
                break
    finally:
        if ws in session.websockets:
            session.websockets.remove(ws)

    return ws


async def on_startup(app):
    app["read_loop"] = asyncio.create_task(manager.start_read_loop())


async def on_cleanup(app):
    app["read_loop"].cancel()
    for session in list(manager.sessions.values()):
        session.kill()


def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/", index)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_post("/api/sessions", api_create_session)
    app.router.add_patch("/api/sessions/{id}", api_rename_session)
    app.router.add_delete("/api/sessions/{id}", api_delete_session)
    app.router.add_get("/api/external", api_external_processes)
    app.router.add_delete("/api/external/{pid}", api_kill_external)
    app.router.add_get("/api/dirs", api_list_dirs)
    app.router.add_get("/ws/{id}", ws_terminal)
    app.router.add_static("/static/", STATIC_DIR)

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Claude CLI Web Terminal")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()

    print(f"\n  Claude CLI Web Terminal")
    print(f"  Listening on http://{args.host}:{args.port}")
    print(f"  (Remote access: http://<this-pc-ip>:{args.port})\n")

    web.run_app(create_app(), host=args.host, port=args.port, print=None)
