@echo off
where pyw >nul 2>nul && (start "" pyw "%~dp0assistant.py") || (start "" pythonw "%~dp0assistant.py")
