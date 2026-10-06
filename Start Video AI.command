#!/bin/bash
# ==============================================================================
# One-Click Native Launcher for Video AI Desktop Dubbing Studio
# Double-click this file from macOS Desktop or Finder to start the application.
# ==============================================================================

PROJECT_DIR="/Users/retnaphut/Documents/vide ai"
cd "$PROJECT_DIR" || exit 1

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export PYTHONUNBUFFERED=1
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1

echo "=========================================================="
echo "🎬 Starting Video AI Desktop Dubbing Studio..."
echo "📁 Project Directory: $PROJECT_DIR"
echo "=========================================================="

python3 main.py
