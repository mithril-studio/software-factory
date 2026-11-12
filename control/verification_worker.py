"""Standalone stdlib worker shipped into a review VM; never executed on the server.

Reads the frozen plan from FACTORY_VERIFICATION_PLAN. Keeps app lifecycle, recordings,
and a structured result even on errors. The harness is installed outside the app checkout.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlparse


def stop(process):
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        finally:
            # The shell can exit before a grandchild that ignored TERM.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def command(args, log, timeout=600):
    process = subprocess.Popen(args, stdout=log, stderr=log, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    finally:
        stop(process)


def main():
    plan = json.loads(os.environ["FACTORY_VERIFICATION_PLAN"])
    root = Path(os.environ["FACTORY_VERIFICATION_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    config = plan["e2e"]
    harness = Path(os.environ.get("FACTORY_E2E_HARNESS", "/opt/factory/e2e-agent-harness/e2e.mjs"))
    report = {"flows": {}, "harness_sha256": None}
    statements = {c["id"]: c["statement"] for c in plan["criteria"]}
    try:
        report["harness_sha256"] = hashlib.sha256(harness.read_bytes()).hexdigest()
        report["harness_files"] = json.loads(harness.with_name("factory-version.json").read_text())
        for name, expected in report["harness_files"].items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("invalid harness version manifest")
            if hashlib.sha256((harness.parent / path).read_bytes()).hexdigest() != expected:
                raise RuntimeError("installed harness changed since it was pinned")
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        if actual != os.environ["FACTORY_VERIFY_SHA"]:
            raise RuntimeError("checkout does not match the verification commit")
        if subprocess.call(["git", "diff", "--quiet", "HEAD"]):
            raise RuntimeError("reviewer changed tracked files before browser verification")
        for flow in config["flows"]:
            app = None
            entry = report["flows"][flow["id"]] = {}
            with (root / (flow["id"] + ".log")).open("w") as log:
                try:
                    for phase in ("setup", "reset"):
                        if command(["bash", "-lc", config[phase]], log):
                            raise RuntimeError(phase + " failed")
                    url = urlparse(config["ready_url"])
                    try:
                        with socket.create_connection((url.hostname, url.port or 80), timeout=1):
                            raise RuntimeError("app port is already occupied before startup")
                    except (ConnectionRefusedError, TimeoutError):
                        pass
                    app = subprocess.Popen(["bash", "-lc", config["start"]], stdout=log,
                                           stderr=log, start_new_session=True)
                    deadline = time.monotonic() + 90
                    while True:
                        if app.poll() is not None:
                            raise RuntimeError("app exited before readiness")
                        try:
                            with urllib.request.urlopen(config["ready_url"], timeout=3) as response:
                                if response.status < 400:
                                    break
                        except Exception:
                            pass
                        if time.monotonic() >= deadline:
                            raise RuntimeError("app readiness timed out")
                        time.sleep(0.5)
                    text = f"# {flow['id']}\n\nStart: {flow['start']}\n\n## Task\n{flow['task']}\n\n## Checks\n"
                    text += "\n".join("- " + " ".join(statements[c].split()) for c in flow["criteria"])
                    flow_file = root / (flow["id"] + ".md")
                    flow_file.write_text(text)
                    entry["exit_code"] = command(
                        ["node", str(harness), "run", str(flow_file), "--url", config["ready_url"],
                         "--run", str(root / flow["id"]), "--minutes", "5", "--budget", "1"],
                        log, timeout=600)
                    if entry["exit_code"] not in (0, 1, 3):
                        raise RuntimeError("browser harness failed")
                except Exception as exc:
                    entry["error"] = str(exc)
                finally:
                    stop(app)
                    try:
                        if command(["bash", "-lc", config["cleanup"]], log, timeout=60):
                            entry["error"] = "cleanup failed"
                    except Exception as exc:
                        entry["error"] = "cleanup: " + str(exc)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        (root / "worker.json").write_text(json.dumps(report))


if __name__ == "__main__":
    main()
