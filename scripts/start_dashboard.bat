@echo off
rem Opens the jobbot dashboard at http://127.0.0.1:8765
cd /d "%~dp0\.."
if not defined JOBBOT_PYTHON set JOBBOT_PYTHON=python
start "" http://127.0.0.1:8765
"%JOBBOT_PYTHON%" -m jobbot web
