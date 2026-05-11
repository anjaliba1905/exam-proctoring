@echo off
echo ====================================================
echo  AI Exam Proctoring System v2 — First-Time Setup
echo ====================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from python.org
    pause & exit /b 1
)

:: Create virtual environment
if not exist "venv" (
    echo [1/5] Creating virtual environment...
    python -m venv venv
)

:: Activate
call venv\Scripts\activate.bat

:: Upgrade pip
echo [2/5] Upgrading pip...
python -m pip install --upgrade pip --quiet

:: Install dependencies
echo [3/5] Installing requirements...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] Some packages failed. Check internet connection.
    pause & exit /b 1
)

:: Install websocket-client separately (for live streaming)
pip install websocket-client --quiet

:: Download YOLO model
echo [4/5] Checking AI models...
if not exist "models" mkdir models
if not exist "models\yolov8n.pt" (
    echo Downloading YOLOv8n model...
    python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')" 2>nul
    if exist "yolov8n.pt" move "yolov8n.pt" "models\" >nul
)

:: Init database
echo [5/5] Initialising local database...
python -c "from database import init_db, init_exam_config; init_db(); init_exam_config(); print('DB ready.')"

echo.
echo ====================================================
echo  Setup complete!
echo  Edit .env with your server URL, then run: start_exam.bat
echo ====================================================
pause
