@echo off
cd /d "%~dp0"
call venv\Scripts\activate.bat 2>nul || (
    echo [ERROR] venv not found. Run setup.bat first.
    pause & exit /b 1
)
echo Starting AI Exam Proctoring System...
python main_app.py
pause
