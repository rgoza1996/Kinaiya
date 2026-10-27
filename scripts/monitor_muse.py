"""Inspect actual Muse processes and JSONL events; no simulated worker activity."""

import argparse
import json
import time
from pathlib import Path


def process_for(prompt):
    for directory in Path("/proc").iterdir():
        if directory.name.isdecimal():
            try:
                argv = (directory / "cmdline").read_bytes().split(b"\0")
            except (OSError, PermissionError):
                continue
            if b"--prompt-file" in argv and str(prompt).encode() in argv:
                return int(directory.name)
    return None


def inspect(task):
    log = Path(task["log"])
    terminal = None
    updates = []
    if log.exists():
        for line in log.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind, payload = event.get("payload_type", ""), event.get("payload", {})
            if kind.startswith("run.terminal."):
                terminal = kind.rsplit(".", 1)[-1]
            if kind == "tool.result":
                facts = payload.get("correlation_facts", {})
                updates.append(
                    f"{facts.get('tool_name', 'tool')}: {facts.get('outcome', 'observed')}"
                )
    pid = process_for(task["prompt"])
    state = (
        "active"
        if pid
        else ("completed" if terminal == "completed" else "failed" if terminal else "not started")
    )
    return {**task, "observed_state": state, "pid": pid, "last_tools": updates[-4:]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("registry", type=Path)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    while True:
        tasks = [inspect(task) for task in json.loads(args.registry.read_text())]
        if args.watch:
            print("\033[2J\033[H", end="")
        print("KINAIYA — actual Muse CLI sessions (completion still requires lead verification)")
        for task in tasks:
            print(f"\n{task['name']}: {task['observed_state']}  PID {task['pid'] or '—'}")
            print(f"  {task['issue']}  branch {task['branch']}")
            print(f"  worktree {task['worktree']}")
            print(f"  genuine CLI events: {task['log']}")
            print("  " + " | ".join(task["last_tools"]))
        if not args.watch:
            break
        time.sleep(2)


if __name__ == "__main__":
    main()
