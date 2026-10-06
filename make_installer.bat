@echo off
chcp 65001 >nul
title Video AI - Production Installer Builder (Windows)
echo ==============================================================================
echo   🏭 Video AI Studio - Commercial Installer Builder (CapCut Style)
echo ==============================================================================
echo.

cd /d "%~dp0"

:: 1. Verify Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python មិនទាន់ដំឡើងលើម៉ាស៊ីននេះទេ!
    pause
    exit /b 1
)

:: 1.5 Auto-bundle FFmpeg binaries into installer package
if not exist "ffmpeg.exe" (
    where ffmpeg >nul 2>nul
    if %errorlevel% equ 0 (
        for /f "tokens=*" %%i in ('where ffmpeg') do (
            if not exist "ffmpeg.exe" copy "%%i" "ffmpeg.exe" >nul 2>nul
        )
    )
)
if not exist "ffprobe.exe" (
    where ffprobe >nul 2>nul
    if %errorlevel% equ 0 (
        for /f "tokens=*" %%i in ('where ffprobe') do (
            if not exist "ffprobe.exe" copy "%%i" "ffprobe.exe" >nul 2>nul
        )
    )
)

:: 2. Compile Python to Closed-Source Binary Executable
echo [1/3] កំពុង Compile កូដ Python ទៅជា Binary Executable (គ្មាន .py សល់)...
python build_windows_exe.py

if %errorlevel% neq 0 (
    echo [ERROR] ការ Compile បានបរាជ័យ! សូមពិនិត្យ Error ខាងលើ។
    pause
    exit /b 1
)

:: 3. Locate Inno Setup Compiler (ISCC)
echo.
echo [2/3] ស្វែងរក Inno Setup Compiler (ISCC.exe)...
set ISCC_PATH=""

if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set ISCC_PATH="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set ISCC_PATH="C:\Program Files\Inno Setup 6\ISCC.exe"
) else (
    where ISCC.exe >nul 2>nul
    if %errorlevel% equ 0 (
        set ISCC_PATH="ISCC.exe"
    )
)

if %ISCC_PATH% == "" (
    echo.
    echo ==============================================================================
    echo ⚠️ មិនទាន់រកឃើញ Inno Setup លើកុំព្យូទ័រនេះទេ!
    echo ដើម្បីបង្កើត File Setup ដូច CapCut (Video_AI_Setup_v1.0.exe) សូមដំឡើង Inno Setup 6៖
    echo.
    echo 👉 ជម្រើសទី ១: បើក Terminal រួចវាយ: winget install JRSoftware.InnoSetup
    echo 👉 ជម្រើសទី ២: ទាញយកពី: https://jrsoftware.org/isdl.php
    echo.
    echo (ចំណាំ៖ ថត dist\Video_AI ត្រូវបាន Compile រួចរាល់ហើយ អាចដំណើរការ Video_AI.exe បាន!)
    echo ==============================================================================
    echo.
    pause
    exit /b 0
)

:: 4. Build Professional Installer
echo [3/3] កំពុងវេចខ្ចប់ចេញជា "Output\Video_AI_Setup_v1.0.exe"...
%ISCC_PATH% installer_config.iss

if %errorlevel% equ 0 (
    echo.
    echo ==============================================================================
    echo   🎉 ជោគជ័យ ១០០%!
    echo   📦 File ដំឡើងដូច CapCut ត្រូវបានបង្កើតនៅ: Output\Video_AI_Setup_v1.0.exe
    echo   🔒 កូដដើមត្រូវបានការពារ ១០០% គ្មាន User ណាអាចឃើញ ឬកែកូដបានឡើយ!
    echo ==============================================================================
    echo.
    explorer.exe Output
) else (
    echo [ERROR] ការវេចខ្ចប់ Inno Setup មានបញ្ហា។
)

pause
