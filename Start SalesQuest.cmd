@echo off
REM Starts the SalesQuest automation backend and opens the ERP in your browser.
REM Keep this window open while you work: WhatsApp/email sending, sequences and the
REM daily reminder only run while it is open. Close it (or press Ctrl+C) to stop.
REM --lan also lets your phone open it on the same Wi-Fi (Settings > Open on your phone).
REM Remove --lan to keep it on this computer only.
cd /d "%~dp0"
set PY=python
where python >nul 2>nul || set PY=py
%PY% server.py --open --lan
pause
