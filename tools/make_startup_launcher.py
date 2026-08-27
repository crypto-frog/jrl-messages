"""Create (or remove) the Startup entry that keeps the agent running.

Writes a tiny .vbs into the current user's Startup folder that launches
agent_supervisor.pyw silently at every logon. No administrator rights are
needed, and it works on every Windows edition. install.bat runs this.

    python tools/make_startup_launcher.py            install the entry
    python tools/make_startup_launcher.py --remove   remove it
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "JRL-Messages-Agent.vbs"


def startup_dir() -> Path:
    base = os.environ.get("APPDATA")
    if not base:
        raise SystemExit("APPDATA is not set; is this Windows?")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / \
        "Programs" / "Startup"


def main() -> int:
    target = startup_dir() / NAME
    if "--remove" in sys.argv:
        try:
            target.unlink()
            print(f"Removed {target}")
        except FileNotFoundError:
            print("No startup entry to remove.")
        return 0
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    supervisor = ROOT / "agent_supervisor.pyw"
    if not pythonw.exists():
        raise SystemExit("Run install.bat first (missing .venv).")
    if not supervisor.exists():
        raise SystemExit("agent_supervisor.pyw is missing.")
    # 0 = hidden window, False = do not wait. VBS doubles quotes to escape.
    line = ('CreateObject("Wscript.Shell").Run '
            f'"""{pythonw}"" ""{supervisor}""", 0, False\n')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(line, encoding="utf-8")
    print(f"Startup entry written: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
