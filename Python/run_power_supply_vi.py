"""
Launch and run a LabVIEW VI from Python using LabVIEW's ActiveX server.

Requirements:
  - Windows with LabVIEW installed (ActiveX server is enabled by default:
    Tools > Options > VI Server > ActiveX)
  - pywin32:  pip install pywin32
"""

import sys
import win32com.client

VI_PATH = (
    r"C:\Users\deaton\Documents\VeriStand Projects\Integrated Test System\Simulation"
    r"\Power Supply.vi"
)

# LabVIEW VI execution states (VI.ExecState)
EXEC_BAD = 0
EXEC_IDLE = 1
EXEC_RUN_TOP_LEVEL = 2
EXEC_RUNNING = 3


def main():
    # Connect to LabVIEW (starts it if it isn't already running)
    labview = win32com.client.Dispatch("LabVIEW.Application")

    # Open a reference to the VI
    vi = labview.GetVIReference(VI_PATH)

    # ExecState values: 0 = Bad (broken), 1 = Idle,
    # 2 = Running as top-level VI, 3 = Running as a subVI
    exec_state = vi.ExecState
    if exec_state in (EXEC_RUN_TOP_LEVEL, EXEC_RUNNING):
        print(f"Already running, nothing to do: {VI_PATH}")
        return
    if exec_state == EXEC_BAD:
        sys.exit(f"VI is broken and cannot run: {VI_PATH}")

    # Show the front panel so the VI stays loaded and visible
    vi.FPWinOpen = True

    # With late binding, pywin32 would otherwise invoke "Run" as a property
    # with no arguments (Async defaults to False), which blocks until the
    # VI finishes. Flag it as a method so the Async argument is honored.
    vi._FlagAsMethod("Run")

    # Run the VI. True = asynchronous, so Python returns immediately
    # and the VI keeps running in LabVIEW.
    vi.Run(True)

    print(f"Running: {VI_PATH}")


if __name__ == "__main__":
    main()
