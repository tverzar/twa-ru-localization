@echo off
chcp 65001 >nul
cd /d "%~dp0"
where pyw >nul 2>&1 && (start "" pyw -3 "tools\twa_ru_gui.py" & goto :eof)
where pythonw >nul 2>&1 && (start "" pythonw "tools\twa_ru_gui.py" & goto :eof)
where py >nul 2>&1 && (py -3 "tools\twa_ru_gui.py" & goto :eof)
where python >nul 2>&1 && (python "tools\twa_ru_gui.py" & goto :eof)
echo Python 3 не найден. Скачайте TWA-Russian.exe в разделе Releases или установите Python: https://www.python.org/downloads/
pause
