"""Turns assistant.py into a standalone app (no Python needed to run it).
Run:  python build_app.py
The finished app appears in the 'dist' folder. It only works on the type of computer
you build it on (build on Windows for Windows, on a Mac for Mac)."""
import importlib.util, subprocess, sys

subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole", "--name", "MyAssistant", "assistant.py"]
if importlib.util.find_spec("pypdf"):
    cmd += ["--hidden-import", "pypdf"]
subprocess.check_call(cmd)
print("\nDone. Your app is in the 'dist' folder. Send that file to your friends.")
