@echo off
chcp 65001 >nul
title Video AI - Khmer Dubbing Studio (Windows)
echo ============================================================
echo   🎬 Video AI - Khmer Dubbing Studio for Windows
echo ============================================================
echo.

:: Move to script directory
cd /d "%~dp0"

:: 1. Check Python installation
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python មិនទាន់ត្រូវបានដំឡើងនៅលើកុំព្យូទ័រនេះទេ!
    echo [ERROR] Python is not installed or not in PATH!
    echo.
    echo សូមទាញយក និងដំឡើង Python 3.10 ឬ 3.11 ពី https://www.python.org/downloads/
    echo * ចំណាំ៖ ពេលដំឡើង សូមប្រាកដថាបានគូសធីក "Add python.exe to PATH"
    echo.
    pause
    exit /b 1
)

:: 2. Check virtual environment or use global python
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Activating virtual environment (venv)...
    call venv\Scripts\activate.bat
)

:: 3. Check FFmpeg
where ffmpeg >nul 2>nul
if %errorlevel% neq 0 (
    if not exist "ffmpeg.exe" (
        echo [WARNING] មិនទាន់រកឃើញ FFmpeg ទេ!
        echo [WARNING] FFmpeg is not found in PATH or project folder.
        echo.
        echo អ្នកអាចដំឡើង FFmpeg យ៉ាងងាយស្រួលដោយ៖
        echo 1. បើក Windows PowerShell រួចវាយ: winget install Gyan.FFmpeg
        echo 2. ឬទាញយក ffmpeg.exe ពី https://www.gyan.dev/ffmpeg/builds/ រួចដាក់ក្នុង folder នេះផ្ទាល់។
        echo.
    )
)

:: 4. Launch Video AI Studio
echo [INFO] កំពុងចាប់ផ្តើមបើកដំណើរការកម្មវិធី Video AI...
echo [INFO] Starting Video AI Studio...
echo.

set PYTHONUNBUFFERED=1
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
set MKL_NUM_THREADS=1

python main.py

if %errorlevel% neq 0 (
    echo.
    echo ============================================================
    echo [NOTICE] កម្មវិធីត្រូវបានបិទ ឬជួបបញ្ហា (Exit Code: %errorlevel%)
    echo ============================================================
    pause
)
