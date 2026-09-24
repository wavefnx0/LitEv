@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo litev 1.0.1 - Windows build (low-memory)
echo ============================================================
echo.

echo Checking Python...
py --version
if errorlevel 1 goto :error

echo.
echo Installing required Python packages...
py -m pip install --upgrade -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Verifying all required imports...
py -c "import asyncio,httpx,httpcore,anyio,networkx,numpy,pandas,certifi,plotly,plotly.express,narwhals,openpyxl,et_xmlfile,tkinter; print('All required modules imported successfully.')"
if errorlevel 1 goto :error

echo.
echo Removing previous build output...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist litev_engine.spec del /q litev_engine.spec
if exist litev.spec del /q litev.spec

REM Build the analysis engine ONCE as a single executable.
REM This avoids copying a second huge PyInstaller dependency tree.
echo.
echo [1/2] Building analysis engine...
py -m PyInstaller --noconfirm --clean --onefile --console ^
  --version-file version_info_engine.txt ^
  --name litev_engine ^
  --hidden-import=_overlapped ^
  --hidden-import=asyncio.windows_events ^
  --hidden-import=httpx ^
  --hidden-import=httpcore ^
  --hidden-import=anyio ^
  --hidden-import=certifi ^
  --hidden-import=networkx ^
  --hidden-import=numpy ^
  --hidden-import=pandas ^
  --hidden-import=plotly ^
  --hidden-import=plotly.express ^
  --hidden-import=plotly.graph_objects ^
  --hidden-import=plotly.io ^
  --collect-data=plotly.validators ^
  --hidden-import=narwhals ^
  --hidden-import=openpyxl ^
  --hidden-import=et_xmlfile ^
  litev_engine.py
if errorlevel 1 goto :build_error

REM Build the GUI separately.
echo.
echo [2/2] Building Windows GUI...
py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --version-file version_info_litev.txt ^
  --name litev ^
  litev.py
if errorlevel 1 goto :build_error

REM Put only the two executables together. No xcopy of the
REM large PyInstaller dependency tree is performed.
echo.
echo Creating final application folder...
if exist litev rmdir /s /q litev
mkdir litev
copy /y "dist\litev.exe" "litev\litev.exe" >nul
copy /y "dist\litev_engine.exe" "litev\litev_engine.exe" >nul
if errorlevel 1 goto :build_error

echo.
echo ============================================================
echo BUILD COMPLETE
echo ============================================================
echo.
echo Application:
echo   %CD%\litev\litev.exe
echo.
echo The GUI launches the bundled engine automatically.
echo The dashboard opens in the default browser after analysis by default.
echo.
echo IMPORTANT: Ollama is only required for the optional local AI
echo features. If AI is enabled, the selected model must be available locally.
echo.
pause
exit /b 0

:build_error
echo.
echo ============================================================
echo BUILD FAILED

echo ============================================================
echo.
echo See the error immediately above this message.
echo.
pause
exit /b 1

:error
echo.
echo ============================================================
echo DEPENDENCY INSTALLATION FAILED

echo ============================================================
echo.
echo A Python package could not be installed/imported.
echo Check the error above and run build_windows.bat again.
echo.
pause
exit /b 1
