"""
Deploy a VeriStand project, run a real-time sequence, and download the TDMS
logs from an NI Linux Real-Time target.

Requirements (on the Windows host running VeriStand):
    python -m pip install niveristand paramiko

With UI_MODE = "editor" (default), close VeriStand first: the script launches
the VeriStand Editor itself, which deploys the project and opens your screen.
Uses the niveristand 4.x Execution API (niveristand.clientapi).
"""

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from time import sleep

import paramiko
from niveristand import VeriStandException
from niveristand.clientapi import Factory, SequenceCallInfo, SequenceState

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
PROJECT_DIR = Path(r"C:\Users\deaton\Documents\VeriStand Projects\Integrated Test System")
SEQUENCE_NAME = "TestPoints.nivsseq"

# UI_MODE:
#   "editor"    - open the project in the new VeriStand Editor, deploy, and show a screen
#   "workspace" - legacy Project Explorer + Workspace window (.nivsproj projects)
#   "none"      - headless deploy through the Gateway, no UI
UI_MODE = "editor"

# VeriStand Editor settings
VERISTAND_EXE = None              # e.g. r"C:\Program Files\NI\VeriStand 2026\VeriStand.exe"; None = auto-find
SCREEN_FILE = None                # .nivsscr path relative to the project folder; None = first one found
OPERATE_MODE = True               # Lock the screen in operate mode (no accidental edits)
EDITOR_STARTUP_TIMEOUT_S = 300    # Time allowed for the Editor to start and finish deploying

# Legacy Workspace settings
CLOSE_GUI_WHEN_DONE = False       # Leave the legacy project open after the test (True to close)
PROJECT_USERNAME = ""             # Only needed if the legacy project is password-protected
PROJECT_PASSWORD = ""

GATEWAY_IP = "localhost"          # VeriStand Gateway (this PC)
TARGET_NAME = "Controller"        # Target name as it appears in the system definition
DEPLOY_TIMEOUT_MS = 120_000
SEQUENCE_TIMEOUT_S = 60 * 60      # Abort the sequence if it runs longer than this
SEQUENCE_TIMEOUT_MS = 1_000.0     # Per-call timeout passed to SequenceCallInfo
POLL_INTERVAL_S = 1.0

RT_HOST = "192.168.68.50"
RT_USER = "admin"                 # NI Linux RT default user
RT_PASSWORD = ""                  # NI Linux RT default is a blank password
RT_LOG_DIR = "/home/lvuser/logs"
LOG_EXTENSIONS = (".tdms", ".tdms_index")
DOWNLOAD_ONLY_NEW = True          # Only fetch files created/changed during this run

LOCAL_DOWNLOAD_DIR = PROJECT_DIR / "Downloaded Logs" / time.strftime("%Y-%m-%d_%H%M%S")

# Post-test analysis: run check_dio_response.py on each downloaded SlowSpeed*.tdms file
CHECK_SCRIPT = Path(__file__).resolve().with_name("check_dio_response.py")
CHECK_FILE_PREFIX = "SlowSpeed"
CHECK_ARGS = []                   # extra options, e.g. ["--limit", "1.5", "--csv", "results.csv"]

# Simulated power supply: launched before VeriStand starts
START_POWER_SUPPLY = True
POWER_SUPPLY_SCRIPT = Path(__file__).resolve().with_name("run_power_supply_vi.py")
POWER_SUPPLY_TIMEOUT_S = 120      # Time allowed for LabVIEW to start and launch the VI
POWER_SUPPLY_SETTLE_S = 5         # Wait after launch so the VI is up before VeriStand connects


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def find_one(root: Path, pattern: str, preferred_stem: str = None) -> Path:
    """Find a file under root matching pattern (recursive)."""
    matches = sorted(root.rglob(pattern))
    if not matches:
        raise FileNotFoundError(f'No "{pattern}" found under "{root}"')
    if preferred_stem:
        for m in matches:
            if m.stem.lower() == preferred_stem.lower():
                return m
    if len(matches) > 1:
        print(f'Warning: multiple "{pattern}" files found, using "{matches[0]}"')
    return matches[0]


def find_veristand_exe() -> Path:
    """Locate VeriStand.exe (newest version wins) unless VERISTAND_EXE is set."""
    if VERISTAND_EXE:
        return Path(VERISTAND_EXE)
    candidates = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        for pattern in ("NI/VeriStand*/VeriStand.exe",
                        "National Instruments/VeriStand*/VeriStand.exe",
                        "National Instruments/NI VeriStand*/VeriStand.exe"):
            candidates.extend(Path(base).glob(pattern))
    if not candidates:
        raise FileNotFoundError("VeriStand.exe not found. Set VERISTAND_EXE to its full path.")
    return sorted(candidates)[-1]


def launch_editor(project_file: Path, screen_rel_path: str):
    """Open the project in the VeriStand Editor, deploy it, and open a screen."""
    exe = find_veristand_exe()
    args = [str(exe), "/nivsprj", str(project_file), "/gateway", GATEWAY_IP, "/deploy"]
    if screen_rel_path:
        args += ["/openFile", screen_rel_path]
        if OPERATE_MODE:
            args.append("/operateScreen")
    print("Launching VeriStand Editor:")
    print("  " + subprocess.list2cmdline(args))
    return subprocess.Popen(args, cwd=str(exe.parent))


def wait_for_deployment(workspace, timeout_s: float):
    """Wait until the target is running, by reading its System Time channel."""
    channel = f"Targets/{TARGET_NAME}/System Channels/System Time"
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            value = workspace.get_single_channel_value(channel)
            if last is not None and value != last:
                return  # system time is advancing -> deployed and running
            last = value
        except Exception:
            pass  # not deployed yet
        time.sleep(2)
    raise TimeoutError(f"System was not running after {timeout_s} s (watching {channel}).")


def open_sftp(host: str, user: str, password: str):
    """Open an SFTP session. Handles NI Linux RT's blank-password admin account."""
    transport = paramiko.Transport((host, 22))
    transport.connect()
    if password:
        transport.auth_password(user, password)
    else:
        try:
            transport.auth_none(user)
        except paramiko.BadAuthenticationType:
            transport.auth_password(user, "")
    return transport, paramiko.SFTPClient.from_transport(transport)


def list_log_files(sftp: paramiko.SFTPClient, remote_dir: str) -> dict:
    """Return {filename: (size, mtime)} for log files in remote_dir."""
    files = {}
    try:
        entries = sftp.listdir_attr(remote_dir)
    except FileNotFoundError:
        return files
    for entry in entries:
        if stat.S_ISREG(entry.st_mode) and entry.filename.lower().endswith(LOG_EXTENSIONS):
            files[entry.filename] = (entry.st_size, entry.st_mtime)
    return files


def snapshot_remote_logs() -> dict:
    transport, sftp = open_sftp(RT_HOST, RT_USER, RT_PASSWORD)
    try:
        return list_log_files(sftp, RT_LOG_DIR)
    finally:
        sftp.close()
        transport.close()


def download_logs(before: dict) -> list:
    """Download TDMS files from the target. Returns the list of local paths."""
    LOCAL_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    transport, sftp = open_sftp(RT_HOST, RT_USER, RT_PASSWORD)
    downloaded = []
    try:
        after = list_log_files(sftp, RT_LOG_DIR)
        for name, info in sorted(after.items()):
            if DOWNLOAD_ONLY_NEW and before.get(name) == info:
                continue  # unchanged since before the run
            remote = f"{RT_LOG_DIR.rstrip('/')}/{name}"
            local = LOCAL_DOWNLOAD_DIR / name
            print(f"  {remote} -> {local}")
            sftp.get(remote, str(local))
            downloaded.append(local)
    finally:
        sftp.close()
        transport.close()
    return downloaded


def run_sequence(factory: Factory, sequence_path: Path):
    """Deploy the sequence in a stimulus profile session and run it to completion."""
    call_info = SequenceCallInfo(str(sequence_path), TARGET_NAME, [], False, SEQUENCE_TIMEOUT_MS)
    session = factory.get_istimulus_profile_session(
        GATEWAY_IP,
        "Integrated Test System - TestPoints",
        [call_info],
        "Automated run of TestPoints.nivsseq",
    )
    session.deploy(False)  # deploy, but don't auto-start
    try:
        control = session[session.sequence_names[0]]
        control.run()
        start = time.monotonic()
        while control.state != SequenceState.STOPPED:
            elapsed = time.monotonic() - start
            if elapsed > SEQUENCE_TIMEOUT_S:
                control.abort()
                raise TimeoutError(f"Sequence exceeded {SEQUENCE_TIMEOUT_S} s and was aborted.")
            print(f"  Sequence running... t = {control.get_sequence_time():.1f} s", end="\r")
            time.sleep(POLL_INTERVAL_S)
        print()
        try:
            print(f"  Sequence return value: {control.get_return_value()}")
        except Exception:
            pass  # sequence may not define a return value
    finally:
        session.undeploy()


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def start_power_supply() -> bool:
    """Run run_power_supply_vi.py to start the simulated power supply VI in LabVIEW.

    The script starts the VI asynchronously and returns, so the VI keeps running
    in LabVIEW. Returns True on success.
    """
    if not START_POWER_SUPPLY:
        return True
    if not POWER_SUPPLY_SCRIPT.is_file():
        print(f"ERROR: {POWER_SUPPLY_SCRIPT} not found.")
        return False
    print(f"Starting simulated power supply ({POWER_SUPPLY_SCRIPT.name}) ...", flush=True)
    try:
        rc = subprocess.run([sys.executable, str(POWER_SUPPLY_SCRIPT)],
                            timeout=POWER_SUPPLY_TIMEOUT_S).returncode
    except subprocess.TimeoutExpired:
        print(f"ERROR: power supply launch did not finish within {POWER_SUPPLY_TIMEOUT_S} s.")
        return False
    if rc != 0:
        print(f"ERROR: power supply launch failed (exit code {rc}).")
        return False
    time.sleep(POWER_SUPPLY_SETTLE_S)
    return True


def main() -> int:
    sdf_path = find_one(PROJECT_DIR, "*.nivssdf", preferred_stem=PROJECT_DIR.name)
    sequence_path = find_one(PROJECT_DIR, SEQUENCE_NAME)
    print(f"System definition: {sdf_path}")
    print(f"Sequence:          {sequence_path}")

    # The simulated power supply must be running before VeriStand is launched.
    if not start_power_supply():
        print("Aborting: VeriStand was not started because the power supply isn't running.")
        return 1

    factory = Factory()

    if UI_MODE == "editor":
        return main_editor(factory, sequence_path)

    if not Factory.can_connect_to_veristand_gateway(GATEWAY_IP):
        if UI_MODE == "workspace" and GATEWAY_IP == "localhost":
            # Launch VeriStand (Gateway + Project Explorer) if it isn't already open.
            print("Starting VeriStand ...")
            factory.start_gateway_and_project_server_async()
            deadline = time.monotonic() + 120
            while not Factory.can_connect_to_veristand_gateway(GATEWAY_IP):
                if time.monotonic() > deadline:
                    print("Timed out waiting for VeriStand to start.")
                    return 1
                time.sleep(2)
        else:
            print(f"Cannot reach the VeriStand Gateway at {GATEWAY_IP}. Is VeriStand open?")
            return 1

    # Record what's already in the log folder so we only pull this run's files.
    print(f"Checking existing logs on {RT_HOST}:{RT_LOG_DIR} ...")
    before = snapshot_remote_logs() if DOWNLOAD_ONLY_NEW else {}

    workspace = factory.get_iworkspace2(GATEWAY_IP)
    project = None
    deployed = False
    exit_code = 0
    try:
        if UI_MODE == "workspace":
            project_path = find_one(PROJECT_DIR, "*.nivsproj", preferred_stem=PROJECT_DIR.name)
            print(f"Opening project {project_path} ...")
            project = factory.get_iproject(
                GATEWAY_IP, str(project_path), PROJECT_USERNAME, PROJECT_PASSWORD
            )
            project.visible = True       # show the Project Explorer window
            print("Deploying project ...")
            project.deploy(True)         # True = show the deployment progress dialog
            deployed = True
            project.open_workspace()     # open the Workspace (UI Manager) window
        else:
            print("Deploying system definition ...")
            workspace.connect_to_system(str(sdf_path), True, DEPLOY_TIMEOUT_MS)
            deployed = True

        print(f"Running {SEQUENCE_NAME} ...")
        run_sequence(factory, sequence_path)
        print("Sequence complete.")
        sleep(10)
    except VeriStandException as error:
        print(f"VeriStand error: {error.resolved_error_message}")
        exit_code = 1
    except Exception as exc:
        print(f"Error: {exc}")
        exit_code = 1
    finally:
        if deployed:
            # Undeploying closes any open TDMS files on the target before download.
            print("Undeploying ...")
            if project is not None:
                project.undeploy()
            else:
                workspace.disconnect_from_system("", True)
        if project is not None and CLOSE_GUI_WHEN_DONE:
            project.close()

    sleep(30)
    files = report_downloads(before)
    if run_dio_checks(files):
        exit_code = 1
    return exit_code


def report_downloads(before: dict):
    print(f"Downloading TDMS files from {RT_HOST}:{RT_LOG_DIR} ...")
    files = download_logs(before)
    if files:
        print(f"Downloaded {len(files)} file(s) to {LOCAL_DOWNLOAD_DIR}")
    else:
        print("No new TDMS files found on the target.")
    return files


def run_dio_checks(files: list) -> int:
    """Run check_dio_response.py on every SlowSpeed*.tdms file.

    Returns 0 if every check passed (or there was nothing to check), 1 otherwise.
    """
    targets = [f for f in files
               if f.suffix.lower() == ".tdms" and f.name.startswith(CHECK_FILE_PREFIX)]
    if not targets:
        print(f"No {CHECK_FILE_PREFIX}*.tdms files to check.")
        return 0
    if not CHECK_SCRIPT.is_file():
        print(f"ERROR: {CHECK_SCRIPT} not found; skipping DIO response checks.")
        return 1

    verdicts = {0: "PASS", 1: "FAIL"}
    results = []
    for tdms in targets:
        print(f"\n=== check_dio_response: {tdms.name} ===", flush=True)
        rc = subprocess.run([sys.executable, str(CHECK_SCRIPT), str(tdms), *CHECK_ARGS]).returncode
        results.append((tdms.name, verdicts.get(rc, "ERROR")))

    print("\nDIO response check summary:")
    for name, verdict in results:
        print(f"  {verdict:<6} {name}")
    return 0 if all(v == "PASS" for _, v in results) else 1


def main_editor(factory: Factory, sequence_path: Path) -> int:
    """New VeriStand Editor flow: Editor deploys and shows the screen, Python runs the test."""
    project_file = find_one(PROJECT_DIR, "*.nivsprj", preferred_stem=PROJECT_DIR.name)
    if SCREEN_FILE:
        screen_rel = SCREEN_FILE
    else:
        screens = sorted(PROJECT_DIR.rglob("*.nivsscr"))
        screen_rel = str(screens[0].relative_to(PROJECT_DIR)) if screens else None
        if not screens:
            print("Warning: no .nivsscr screen found; the Editor will open without a screen.")
    print(f"Project: {project_file}")
    print(f"Screen:  {screen_rel}")

    # Record what's already in the log folder so we only pull this run's files.
    print(f"Checking existing logs on {RT_HOST}:{RT_LOG_DIR} ...")
    before = snapshot_remote_logs() if DOWNLOAD_ONLY_NEW else {}

    launch_editor(project_file, screen_rel)

    print("Waiting for the Gateway ...")
    deadline = time.monotonic() + EDITOR_STARTUP_TIMEOUT_S
    while not Factory.can_connect_to_veristand_gateway(GATEWAY_IP):
        if time.monotonic() > deadline:
            print("Timed out waiting for the VeriStand Gateway.")
            return 1
        time.sleep(2)

    workspace = factory.get_iworkspace2(GATEWAY_IP)
    exit_code = 0
    deployed = False
    try:
        print("Waiting for the Editor to finish deploying ...")
        wait_for_deployment(workspace, max(30, deadline - time.monotonic()))
        deployed = True
        print(f"Running {SEQUENCE_NAME} (watch the Editor screen) ...")
        run_sequence(factory, sequence_path)
        print("Sequence complete.")
        sleep(5)
    except VeriStandException as error:
        print(f"VeriStand error: {error.resolved_error_message}")
        exit_code = 1
    except Exception as exc:
        print(f"Error: {exc}")
        exit_code = 1
    finally:
        if deployed:
            # Undeploy so the target closes its TDMS files. The Editor stays open
            # and simply shows that it's disconnected.
            print("Undeploying ...")
            workspace.disconnect_from_system("", True)
            sleep(10)

    files = report_downloads(before)
    sleep(10)
    if run_dio_checks(files):
        exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
