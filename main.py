import sys
import os

# Automatically add application root directory and bundled tools to system PATH
app_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__))
if app_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = app_dir + os.pathsep + os.environ.get("PATH", "")

# Prevent OpenBLAS / PyTorch multi-threaded stack allocation crash on macOS ARM64
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"

from PIL import ImageFont
from qt_compat import QApplication, Qt, QFontDatabase, QFont
from gui.main_window import MainWindow
from utils.file_utils import ensure_directories
from utils.ffmpeg import is_ffmpeg_available
from utils.dns_resilience import setup_dns_resilience

# Activate resilient DNS fallback for Edge-TTS & Cloud APIs
setup_dns_resilience()

def check_khmer_fonts():
    font_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'fonts'))
    found = []
    if os.path.exists(font_dir):
        for f in sorted(os.listdir(font_dir)):
            if f.endswith(('.ttf', '.otf')):
                found.append(f"✅ {f}")
    if os.path.exists('/System/Library/Fonts/Supplemental/Khmer Sangam MN.ttf'):
        found.append("✅ Khmer Sangam MN (macOS System)")
    
    print(f"\n=== Khmer Font Library ({len(found)} fonts available) ===")
    for f in found:
        print(f)
    print("===================================================\n")

def main():
    check_khmer_fonts()

    # High DPI scaling support
    if hasattr(Qt, 'HighDpiScaleFactorRoundingPolicy'):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setApplicationName("Khmer Video Translator")
    app.setOrganizationName("KhmerAI")

    icon_path = os.path.abspath(os.path.join(os.path.dirname(__file__), 'assets', 'app_icon.png'))
    if os.path.exists(icon_path):
        from qt_compat import QIcon
        app.setWindowIcon(QIcon(icon_path))

    # Pre-load all Khmer fonts directly into Qt Font Database
    font_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'fonts'))
    if os.path.exists(font_dir):
        for f in sorted(os.listdir(font_dir)):
            if f.endswith(('.ttf', '.otf')):
                QFontDatabase.addApplicationFont(os.path.join(font_dir, f))
    
    # Set default native font to Kantumruy Pro with OpenType tables
    app.setFont(QFont("Kantumruy Pro", 11))

    # Ensure output and temp dirs exist
    ensure_directories()

    # Warn if FFmpeg is missing
    if not is_ffmpeg_available():
        print("WARNING: FFmpeg command not found in system PATH. Video combination features require FFmpeg.")

    window = MainWindow()
    window.show()
    window.raise_()
    window.activateWindow()

    sys.exit(app.exec() if hasattr(app, 'exec') else app.exec_())

if __name__ == "__main__":
    main()
