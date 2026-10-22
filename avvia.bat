@echo off
rem Doppio clic per avviare l'assistente.
cd /d "%~dp0"
".venv\Scripts\python.exe" assistente.py %*
echo.
pause
