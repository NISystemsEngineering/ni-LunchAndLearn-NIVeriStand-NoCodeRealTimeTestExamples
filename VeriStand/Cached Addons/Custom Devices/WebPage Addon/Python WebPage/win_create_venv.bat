@echo off
IF "%venv_name%"=="" SET venv_name=.venv

echo Creating virtual environment: %venv_name%...
python -m venv %venv_name%

if %errorlevel% neq 0 (
    echo.
    echo Error: Failed to create the virtual environment. Make sure Python is installed and added to your PATH.
    pause
    exit /b %errorlevel%
)

echo Virtual environment created successfully!


echo "Activating virtual environment..."
cmd /k ".venv\Scripts\activate.bat && python -m pip install --upgrade pip && pip install grpcio-tools && deactivate && exit 0"
