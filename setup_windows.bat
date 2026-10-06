@echo off
chcp 65001 >nul
title Setup Video AI Studio (Windows Installer)
echo ============================================================
echo   ⚡ ដំឡើងបរិស្ថាន Video AI Studio សម្រាប់ Windows
echo   ⚡ Setting up Video AI Studio on Windows
echo ============================================================
echo.

cd /d "%~dp0"

:: 1. Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] មិនទាន់មាន Python នៅក្នុងម៉ាស៊ីនទេ!
    echo សូមដំឡើង Python 3.10 ឬ 3.11 ពី https://www.python.org/downloads/
    echo សូមប្រាកដថាបានគូសធីក "Add python.exe to PATH"
    pause
    exit /b 1
)

:: 2. Create virtual environment if not exists
if not exist "venv" (
    echo [1/4] កំពុងបង្កើត Virtual Environment (venv)...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] មិនអាចបង្កើត venv បានទេ។ នឹងប្រើ Global Python ជំនួសវិញ។
    )
)

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo.
echo [2/4] កំពុង Upgrade Pip...
python -m pip install --upgrade pip

:: 3. Check for Nvidia GPU
echo.
echo [3/4] ត្រួតពិនិត្យក្រាហ្វិកកាត (GPU Check)...
where nvidia-smi >nul 2>nul
if %errorlevel% equ 0 (
    echo [INFO] រកឃើញ Nvidia GPU! កំពុងដំឡើង PyTorch CUDA សម្រាប់ល្បឿនលឿនបំផុត...
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
) else (
    echo [INFO] មិនមាន Nvidia GPU ឬប្រើ CPU Mode... កំពុងដំឡើង PyTorch CPU...
    pip install torch torchaudio
)

:: 4. Install requirements
echo.
echo [4/4] កំពុងដំឡើងបណ្តុំ Library ទាំងអស់ (requirements.txt)...
pip install -r requirements.txt

echo.
echo ============================================================
echo   ✅ ការដំឡើងបានបញ្ចប់ជោគជ័យ ១០០%!
echo   ✅ Setup completed successfully!
echo   👉 លោកអ្នកអាចចុចពីរដងលើ "Start_Video_AI.bat" ដើម្បីបើកកម្មវិធី!
echo ============================================================
echo.
pause
