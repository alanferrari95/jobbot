@echo off
rem Runs `jobbot daily` (Task Scheduler, twice a day: see docs/scheduling.md).
rem Uses the JOBBOT_PYTHON variable if set, else the python on PATH. Log: logs\daily.log
cd /d "%~dp0\.."
if not defined JOBBOT_PYTHON set JOBBOT_PYTHON=python
if not exist logs mkdir logs
"%JOBBOT_PYTHON%" -m jobbot daily >> "logs\daily.log" 2>&1
