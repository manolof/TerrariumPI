#!/usr/bin/env python3
"""Smoke test for the Ludwig image.

Usage: python3 tests/smoke/smoke.py <image> [--platform linux/arm/v7]

What it does:
  1. Creates a Docker volume for /TerrariumPI/data.
  2. Applies the real migrations (yoyo, as terrariumDatabase.init does) and then tests/smoke/seed.sql
     (four areas, two sensors, four relays and the enclosure, from the scrubbed terrarium-ui fixtures).
  3. Starts the engine from the image through tests/smoke/launch.py (answers the Pi-only CPU serial and
     device-tree model reads) with no GPIO, no camera and no I2C. Hardware errors are expected.
  4. Waits up to SMOKE_TIMEOUT seconds (default 600, QEMU is slow) for the HTTP checks to pass.
  5. Checks /live/ with a plain-socket websocket client.
  6. Fails on ImportError, ModuleNotFoundError, AttributeError, NameError or SyntaxError in the container
     output or in the engine log.

Only the standard library is used. The container listens on 127.0.0.1 only. It never touches a real device.
"""

import argparse
import base64
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PORT = 8090
HTTP_CHECKS = [
    "/api/sensors/",
    "/api/relays/",
    "/api/areas/",
    "/api/enclosures/",
    "/api/weather/",
    "/api/system_status/",
    "/api/webcams/",
    "/",
]
BAD_LOG = re.compile(r"\b(ImportError|ModuleNotFoundError|AttributeError|NameError|SyntaxError)\b")
# Exact text of log lines the check accepts: a line that matches BAD_LOG, or the final exception line of a
# traceback (the line after the frames, for example "FileNotFoundError: [Errno 2] No such file ..."). Only for
# hardware that is not there, each with its reason. Empty on purpose: add a line only after a CI log shows it.
ALLOWED_LOG_LINES = ()
LOG_TAIL_LINES = 80

# Programs and modules the Pi needs at run time. Run in the image itself, before the engine starts.
COMMANDS = ["rpicam-vid", "ffmpeg", "pigpiod", "pigs", "pgrep", "nc"]
# The Pi runs the legacy camera stack (start_x=1 in its boot config): rpicam-vid finds no camera there, and
# rpilive_webcam.sh prefers raspivid. /usr/bin/raspivid is a wrapper: the real binary and its libraries came from
# the old image (Dockerfile.ludwig, stage legacy_camera), and only that process gets them.
RASPIVID_CHECK = (
    "command -v raspivid || echo 'raspivid: not found'; "
    "LD_LIBRARY_PATH=/opt/legacy-camera/lib ldd /opt/legacy-camera/raspivid"
)
RASPIVID_HELP = "raspivid --help"
# What the loader, or the wrapper failing to start the binary, says. Anything else is the program itself running.
# Without a camera it may stop on that (no /dev/vchiq here): that is not what this step checks.
LOADER_ERROR = re.compile(
    r"error while loading shared libraries|cannot open shared object|symbol lookup error|undefined symbol"
    r"|version `[^']+' not found|Exec format error"
)
IMPORT_CHECK = """
import importlib, importlib.util, sys
bad = 0
for name in ("cv2", "smbus2", "gpiozero", "pigpio", "bluepy.btle", "bme280"):
    try:
        importlib.import_module(name)
        print("ok  import", name)
    except Exception as ex:
        bad += 1
        print("FAIL import", name, type(ex).__name__, ex)
# RPi.GPIO refuses to import on anything but a Pi, so only look for it
if importlib.util.find_spec("RPi.GPIO") is None:
    bad += 1
    print("FAIL RPi.GPIO is not installed")
else:
    print("ok  RPi.GPIO is installed")
sys.exit(1 if bad else 0)
"""
# Seeded rows (seed.sql) used for the per-id manage routes: the temperature sensor and the Heat area.
SEED_SENSOR_ID = "721ce353dd0e129fbd1e2e5130c3a0a3"
SEED_AREA_ID = "105f3e6e-7bcd-4242-921e-836980015108"
# Weather key, mail user and mail password are write-only: no response may carry a value under these names.
SECRET_NAMES = ("password", "username", "appid", "key")
EXPECTED_COUNTS = {"/api/areas/": 4, "/api/sensors/": 2, "/api/relays/": 4, "/api/enclosures/": 1}

MIGRATE_AND_SEED = """
import sqlite3
from yoyo import get_backend, read_migrations
backend = get_backend('sqlite:///data/terrariumpi.db')
migrations = read_migrations('migrations')
with backend.lock():
    backend.apply_migrations(backend.to_apply(migrations))
db = sqlite3.connect('data/terrariumpi.db')
db.executescript(open('/seed.sql', encoding='utf-8').read())
db.commit()
for table in ('Enclosure', 'Area', 'Sensor', 'Relay'):
    print(table, db.execute('select count(*) from ' + table).fetchone()[0])
"""


def run(cmd, check=True, timeout=None):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


def manage_paths(sensor_id, area_id):
    """Every GET route of terrariumManage, with ids for the per-id ones."""
    return [
        f"/api/manage/sensors/{sensor_id}/",
        f"/api/manage/areas/{area_id}/",
        "/api/manage/alerts/",
        "/api/manage/location/",
        "/api/manage/email/",
        "/api/manage/display/",
        "/api/manage/system/",
        "/api/manage/devices/types/",
    ]


def secret_field_problems(data, path="$"):
    """Paths of fields named like a secret that hold a value, anywhere in the JSON. Never returns the value."""
    problems = []
    if isinstance(data, dict):
        for name, value in data.items():
            here = f"{path}.{name}"
            if str(name).lower() in SECRET_NAMES and value not in (None, "", False):
                problems.append(here)
            problems.extend(secret_field_problems(value, here))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            problems.extend(secret_field_problems(value, f"{path}[{index}]"))
    return problems


def raspivid_problems(returncode, output):
    """What keeps raspivid from running, from the output of RASPIVID_CHECK. Empty when it can run."""
    problems = [line.strip() for line in output.splitlines() if "not found" in line]
    if returncode != 0 and not problems:
        problems.append(f"ldd exit code {returncode}")
    return problems


def raspivid_help_problems(returncode, output):
    """Loader errors in the output of RASPIVID_HELP. A camera error, or the exit code it gives, does not count."""
    problems = [line.strip() for line in output.splitlines() if LOADER_ERROR.search(line)]
    if returncode in (126, 127) and not problems:
        problems.append(f"raspivid could not start, exit code {returncode}")
    return problems


def tracebacks(lines):
    """Yield (header index, final exception line) for each traceback in the log."""
    for index, line in enumerate(lines):
        if not line.startswith("Traceback (most recent call last)"):
            continue
        end = index + 1
        while end < len(lines) and (lines[end].startswith((" ", chr(9))) or lines[end].startswith("Traceback")):
            end += 1
        yield index, (lines[end].strip() if end < len(lines) else "")


def http_get(path, timeout=10):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as ex:
        return ex.code, b""
    except Exception:
        return None, b""


def ws_frame_read(sock):
    def recv_exact(n):
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("socket closed")
            data += chunk
        return data

    head = recv_exact(2)
    opcode = head[0] & 0x0F
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(8))[0]
    return opcode, recv_exact(length)


def ws_send_text(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytes([0x81])
    if len(payload) < 126:
        header += bytes([0x80 | len(payload)])
    else:
        header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def check_live(timeout=60):
    """Open /live/, send client_init and load_dashboard, expect at least one JSON frame with a type."""
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                "GET /live/ HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{PORT}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during handshake")
            response += chunk
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError(f"websocket handshake refused: {status_line!r}")
        ws_send_text(sock, json.dumps({"type": "client_init"}))
        ws_send_text(sock, json.dumps({"type": "load_dashboard"}))
        sock.settimeout(10)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                opcode, payload = ws_frame_read(sock)
            except socket.timeout:
                continue
            if opcode == 1:
                message = json.loads(payload.decode())
                if "type" in message:
                    return message["type"]
        raise RuntimeError("no frame from /live/ after client_init")
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--platform", default="linux/arm/v7")
    parser.add_argument("--seed", default=str(Path(__file__).with_name("seed.sql")))
    args = parser.parse_args()
    timeout = int(os.environ.get("SMOKE_TIMEOUT", "600"))
    seed = str(Path(args.seed).resolve())

    suffix = uuid.uuid4().hex[:8]
    volume = f"ludwig-smoke-data-{suffix}"
    name = f"ludwig-smoke-{suffix}"
    failures = []
    logs = ""

    def record(label, ok, detail=""):
        print(f"{'PASS' if ok else 'FAIL'}  {label} {detail}".rstrip(), flush=True)
        if not ok:
            failures.append(label)

    try:
        run(["docker", "volume", "create", volume])
        seeded = run(
            [
                "docker", "run", "--rm", "--platform", args.platform,
                "-v", f"{volume}:/TerrariumPI/data",
                "-v", f"{seed}:/seed.sql:ro",
                "--entrypoint", "/opt/venv/bin/python",
                "-w", "/TerrariumPI",
                args.image, "-c", MIGRATE_AND_SEED,
            ],
            timeout=timeout,
        )
        print(seeded.stdout.strip(), flush=True)

        for command in COMMANDS:
            found = run(
                ["docker", "run", "--rm", "--platform", args.platform, "--entrypoint", "/bin/sh", args.image,
                 "-c", f"command -v {command}"],
                check=False,
                timeout=timeout,
            )
            record(f"{command} is in the image", found.returncode == 0, found.stdout.strip())
        raspivid = run(
            ["docker", "run", "--rm", "--platform", args.platform, "--entrypoint", "/bin/sh", args.image,
             "-c", RASPIVID_CHECK],
            check=False,
            timeout=timeout,
        )
        print(raspivid.stdout.strip(), raspivid.stderr.strip()[-2000:], flush=True)
        problems = raspivid_problems(raspivid.returncode, raspivid.stdout + raspivid.stderr)
        record("raspivid is in the image with all its libraries", not problems, "; ".join(problems))
        raspivid_help = run(
            ["docker", "run", "--rm", "--platform", args.platform, "--entrypoint", "/bin/sh", args.image,
             "-c", RASPIVID_HELP],
            check=False,
            timeout=timeout,
        )
        print(raspivid_help.stdout.strip(), raspivid_help.stderr.strip()[-2000:], flush=True)
        print(f"raspivid --help exit code {raspivid_help.returncode}", flush=True)
        problems = raspivid_help_problems(raspivid_help.returncode, raspivid_help.stdout + raspivid_help.stderr)
        record("raspivid starts through its wrapper", not problems, "; ".join(problems))
        imports = run(
            ["docker", "run", "--rm", "--platform", args.platform, "--entrypoint", "/opt/venv/bin/python",
             args.image, "-c", IMPORT_CHECK],
            check=False,
            timeout=timeout,
        )
        print(imports.stdout.strip(), imports.stderr.strip()[-2000:], flush=True)
        record("python modules for the camera, I2C and GPIO import", imports.returncode == 0)

        run(
            [
                "docker", "run", "-d", "--name", name, "--platform", args.platform,
                "-p", f"127.0.0.1:{PORT}:{PORT}",
                "-v", f"{volume}:/TerrariumPI/data",
                "-e", "AUTO_REBOOT=false",
                # run.sh edits /etc/modules and the boot config, and asks for a reboot. Not here.
                # (In run.sh, ENABLE_CAMERA, ENABLE_SERIAL and ENABLE_CO2_SENSORS all read ENABLE_1_WIRE.)
                "-e", "ENABLE_I2C=false",
                "-e", "ENABLE_1_WIRE=false",
                "-v", f"{Path(__file__).resolve().parent}:/smoke:ro",
                args.image, "/opt/venv/bin/python", "/smoke/launch.py",
            ]
        )

        start = time.time()
        pending = list(HTTP_CHECKS)
        bodies = {}
        while pending and time.time() - start < timeout:
            running = run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False).stdout.strip()
            if running != "true":
                break
            for path in list(pending):
                status, body = http_get(path)
                if status == 200:
                    bodies[path] = body
                    pending.remove(path)
            if pending:
                time.sleep(5)
        elapsed = int(time.time() - start)

        for path in HTTP_CHECKS:
            record(f"GET {path}", path in bodies, f"({elapsed}s)" if path in bodies else "no 200 in time")

        for path, expected in EXPECTED_COUNTS.items():
            if path in bodies:
                try:
                    data = json.loads(bodies[path])
                    count = len(data.get("data", data)) if isinstance(data, dict) else len(data)
                    record(f"{path} holds {expected} seeded rows", count == expected, f"(found {count})")
                except Exception as ex:
                    record(f"{path} is JSON", False, str(ex))

        for path in manage_paths(SEED_SENSOR_ID, SEED_AREA_ID):
            status, body = http_get(path, timeout=60)
            try:
                found = secret_field_problems(json.loads(body)) if status == 200 else []
            except Exception as ex:
                record(f"GET {path} is JSON", False, str(ex))
                continue
            record(f"GET {path}", status == 200, f"(status {status})")
            record(f"GET {path} holds no secret value", not found, "; ".join(found))

        if "/api/sensors/" in bodies:
            try:
                record("/live/ answers client_init", True, f"(first frame: {check_live()})")
            except Exception as ex:
                record("/live/ answers client_init", False, str(ex))

        running = run(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False).stdout.strip()
        record("engine container still running", running == "true")

        logs = run(["docker", "logs", name], check=False)
        logs = logs.stdout + logs.stderr
        engine_log = run(["docker", "exec", name, "cat", "/TerrariumPI/log/terrariumpi.log"], check=False)
        logs += "\n" + engine_log.stdout
        lines = logs.splitlines()
        bad = [line for line in lines if BAD_LOG.search(line) and line.strip() not in ALLOWED_LOG_LINES]
        record("no ImportError/ModuleNotFoundError/AttributeError/NameError/SyntaxError in the logs", not bad)
        for line in bad[:20]:
            print("   ", line[:300])
        traces = [(i, last) for i, last in tracebacks(lines) if last not in ALLOWED_LOG_LINES]
        record("no Traceback in the logs, except allowed hardware cases", not traces)
        for i, last in traces[:20]:
            print(f"    line {i}: ends with {last[:300]}")
        print("---- engine log tail, for audit ----")
        print(os.linesep.join(line[:300] for line in engine_log.stdout.splitlines()[-LOG_TAIL_LINES:]))
    except Exception as ex:
        record("smoke run", False, str(ex))
    finally:
        if failures:
            tail = run(["docker", "logs", "--tail", "200", name], check=False)
            print("---- container log (last 200 lines) ----")
            print((tail.stdout + tail.stderr)[-12000:])
        run(["docker", "rm", "-f", name], check=False)
        run(["docker", "volume", "rm", "-f", volume], check=False)

    if failures:
        print(f"\nSMOKE FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("\nSMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
