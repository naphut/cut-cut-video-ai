"""
Build Script for Video AI Studio (Windows Standalone Executable)
Compiles the application into a secure, closed-source Windows binary distribution
where no raw Python source code is exposed or editable by end users.
"""
import os
import sys
import shutil
import subprocess

# Ensure UTF-8 output even on Windows CP1252 consoles
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def check_pyinstaller():
    try:
        import PyInstaller
        return True
    except ImportError:
        print("[!] PyInstaller not installed. Installing PyInstaller...")
        res = subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=False)
        return res.returncode == 0

def build():
    print("==============================================================")
    print("[*] Starting Production Build for Video AI Studio (Windows)")
    print("==============================================================")

    if not check_pyinstaller():
        print("[X] Failed to find or install PyInstaller.")
        sys.exit(1)

    icon_path = os.path.join(BASE_DIR, "assets", "app_icon.ico")
    styles_path = os.path.join(BASE_DIR, "gui", "styles.qss")
    fonts_dir = os.path.join(BASE_DIR, "fonts")
    assets_dir = os.path.join(BASE_DIR, "assets")

    # Path separator for PyInstaller --add-data: ';' on Windows, ':' on POSIX
    sep = ";" if sys.platform == "win32" else ":"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "Video_AI",
        "--noconsole",            # Pure native desktop app (no console window)
        "--onedir",               # Standard high-performance directory distribution
        "--clean",
        "--noconfirm",
    ]

    if os.path.exists(icon_path):
        cmd.extend(["--icon", icon_path])

    # Include vital data directories & assets
    if os.path.exists(fonts_dir):
        cmd.extend(["--add-data", f"{fonts_dir}{sep}fonts"])

    if os.path.exists(assets_dir):
        cmd.extend(["--add-data", f"{assets_dir}{sep}assets"])

    if os.path.exists(styles_path):
        cmd.extend(["--add-data", f"{styles_path}{sep}gui"])

    # If local ffmpeg.exe exists, bundle it
    ffmpeg_exe = os.path.join(BASE_DIR, "ffmpeg.exe")
    ffprobe_exe = os.path.join(BASE_DIR, "ffprobe.exe")
    if os.path.exists(ffmpeg_exe):
        cmd.extend(["--add-data", f"{ffmpeg_exe}{sep}."])
    if os.path.exists(ffprobe_exe):
        cmd.extend(["--add-data", f"{ffprobe_exe}{sep}."])

    # Hidden imports to ensure dynamic modules are resolved
    hidden_imports = [
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "PySide6.QtMultimedia",
        "pydub",
        "requests",
        "scipy",
        "scipy.signal",
        "numpy",
        "cv2",
        "PIL",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageFont",
        "edge_tts",
        "core",
        "gui",
        "services",
        "utils"
    ]
    for hi in hidden_imports:
        cmd.extend(["--hidden-import", hi])

    # Exclude unnecessary bloated packages
    excludes = [
        "tkinter",
        "matplotlib",
        "IPython",
        "notebook",
        "jupyter",
        "unittest",
        "tests",
        "pydoc"
    ]
    for ex in excludes:
        cmd.extend(["--exclude-module", ex])

    # Entry point
    cmd.append(os.path.join(BASE_DIR, "main.py"))

    print("\n[+] Running compiler command:")
    print(" ".join(cmd))
    print("\n[*] Compiling... Please wait...\n")

    res = subprocess.run(cmd, cwd=BASE_DIR)
    if res.returncode == 0:
        dist_dir = os.path.join(BASE_DIR, "dist", "Video_AI")
        internal_dir = os.path.join(dist_dir, "_internal")
        for exe_name in ["ffmpeg.exe", "ffprobe.exe"]:
            src = os.path.join(BASE_DIR, exe_name)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, os.path.join(dist_dir, exe_name))
                    if os.path.exists(internal_dir):
                        shutil.copy2(src, os.path.join(internal_dir, exe_name))
                    print(f"[INFO] Copied {exe_name} to dist and _internal directories.")
                except Exception as e:
                    print(f"[!] Note: Could not copy {exe_name}: {e}")

        print("\n==============================================================")
        print("[SUCCESS] Compilation Successful!")
        print(f"[INFO] Standalone Windows App built at: {dist_dir}")
        print("[INFO] All Python code is compiled into binaries (Zero .py source code exposed).")
        print("[INFO] Run `make_installer.bat` to package into Video_AI_Setup.exe!")
        print("==============================================================\n")
    else:
        print(f"\n[ERROR] Compilation failed with exit code {res.returncode}")
        sys.exit(res.returncode)

if __name__ == "__main__":
    build()
