@echo off
setlocal EnableExtensions
TITLE EasyRecruit ATS 3.0
color 0A

REM ---------------------------------------------------------------
REM  Always run from the folder this .bat file lives in.
REM ---------------------------------------------------------------
cd /d "%~dp0"

echo.
echo  ============================================================
echo    EasyRecruit ATS 3.0  ^|  Windows Quick Start
echo  ============================================================
echo.
echo  Working folder: %cd%
echo.

REM ---------------------------------------------------------------
REM  Python check. Prefers 3.11-3.13 if multiple Pythons are
REM  installed (via the py launcher), because brand-new Python
REM  releases often don't have ready-made installers ("wheels")
REM  yet for packages with compiled code (pydantic, numpy, spaCy).
REM  If no compatible Python is found at all - or none is
REM  installed - this script downloads a small, self-contained
REM  Python 3.12 into this project's own folder and uses that
REM  instead. It does not touch or modify anything on your system.
REM ---------------------------------------------------------------
set "PYEXE="
for %%V in (3.11 3.12 3.13) do (
    if not defined PYEXE (
        py -%%V --version >nul 2>&1
        if not errorlevel 1 set "PYEXE=py -%%V"
    )
)
set "PYFOUND=1"
if not defined PYEXE (
    python --version >nul 2>&1
    if not errorlevel 1 (
        set "PYEXE=python"
    ) else (
        py --version >nul 2>&1
        if not errorlevel 1 (
            set "PYEXE=py"
        ) else (
            set "PYFOUND=0"
        )
    )
)

set "USE_EMBED=0"
if "%PYFOUND%"=="0" goto :no_python_found

for /f "tokens=2" %%v in ('%PYEXE% --version 2^>^&1') do set PYVER=%%v
echo [OK] Python %PYVER% found using "%PYEXE%"
set "PYMINOR=0"
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do set PYMINOR=%%b
if %PYMINOR% GEQ 14 goto :need_bootstrap
goto :python_ready

:no_python_found
echo [INFO] No Python installation found on this computer.
echo        Setting up a small, self-contained Python 3.12 inside
echo        this project folder instead...
call :bootstrap_python
if errorlevel 1 (
    echo.
    echo [ERROR] Could not set up Python automatically ^(no internet,
    echo         or python.org is unreachable^), and no Python is
    echo         installed on this computer. Install Python from
    echo         https://python.org ^(check "Add Python to PATH"
    echo         during setup^), then run this file again.
    pause
    exit /b 1
)
set "USE_EMBED=1"
goto :python_ready

:need_bootstrap
echo [INFO] Python %PYVER% is very new - packages with compiled
echo        code may not have ready-made installers for it yet.
echo        Setting up a small, self-contained Python 3.12 inside
echo        this project folder instead ^(does not touch your
echo        existing Python installation^)...
call :bootstrap_python
if errorlevel 1 (
    echo        Could not set up an isolated Python - continuing
    echo        with Python %PYVER%. Some packages below may fail.
    goto :python_ready
)
set "USE_EMBED=1"

:python_ready

REM ---------------------------------------------------------------
REM  Virtual environment. Skipped when using the self-contained
REM  Python above, since it already lives only inside this project
REM  folder and needs no extra isolation layer.
REM ---------------------------------------------------------------
if "%USE_EMBED%"=="1" (
    set "RUNPY=%CD%\pyembed\python.exe"
    echo [OK] Using self-contained Python 3.12 for this project.
    echo [1/6] No separate virtual environment needed.
) else (
    if not exist "venv\Scripts\activate.bat" (
        echo [1/6] Creating virtual environment...
        %PYEXE% -m venv venv
        if errorlevel 1 (
            echo [ERROR] Failed to create venv.
            pause
            exit /b 1
        )
    ) else (
        echo [1/6] Virtual environment already exists.
    )

    echo [2/6] Activating virtual environment...
    call "venv\Scripts\activate.bat"
    if errorlevel 1 (
        echo [ERROR] Could not activate venv.
        pause
        exit /b 1
    )
    set "RUNPY=python"
)

REM ---------------------------------------------------------------
REM  Dependencies. Every step tries the exact tested versions
REM  first; if that Python has no ready-made installer for one of
REM  them, it automatically retries with a flexible version range
REM  so pip can pick whatever version does have one.
REM ---------------------------------------------------------------
echo [3/6] Installing dependencies. This can take several minutes...

"%RUNPY%" -m pip install --upgrade pip -q

echo       Step A: Core web framework...
"%RUNPY%" -m pip install "fastapi==0.109.0" "uvicorn[standard]==0.27.0" "python-multipart==0.0.6" -q >nul 2>&1
if errorlevel 1 (
    echo       Exact versions unavailable for this Python - trying newer compatible ones...
    "%RUNPY%" -m pip install "fastapi>=0.109,<1" "uvicorn[standard]>=0.27,<1" "python-multipart>=0.0.6,<1" -q
    if errorlevel 1 goto :pip_error
)

echo       Step B: Auth packages...
"%RUNPY%" -m pip install "bcrypt>=3.2.0" -q
if errorlevel 1 goto :pip_error
"%RUNPY%" -m pip install "PyJWT==2.8.0" -q >nul 2>&1
if errorlevel 1 (
    "%RUNPY%" -m pip install "PyJWT>=2.8.0,<3" -q
    if errorlevel 1 goto :pip_error
)

echo       Step C: Validation...
"%RUNPY%" -m pip install "python-dotenv==1.0.0" "pydantic[email]==2.5.3" -q >nul 2>&1
if errorlevel 1 (
    echo       Exact versions unavailable for this Python - trying newer compatible ones...
    "%RUNPY%" -m pip install "python-dotenv>=1.0.0,<2" "pydantic[email]>=2.5,<3" -q
    if errorlevel 1 goto :pip_error
)

echo       Step D: Document parsing...
"%RUNPY%" -m pip install "pdfplumber==0.10.3" "python-docx==1.1.0" -q >nul 2>&1
if errorlevel 1 (
    echo       Exact versions unavailable for this Python - trying newer compatible ones...
    "%RUNPY%" -m pip install "pdfplumber>=0.10.3,<1" "python-docx>=1.1.0,<2" -q
    if errorlevel 1 goto :pip_error
)

echo       Step E: NLP and ML packages. This is the largest download, please wait...
"%RUNPY%" -m pip install "numpy==1.26.3" "scikit-learn==1.4.0" -q >nul 2>&1
if errorlevel 1 (
    echo       Exact versions unavailable for this Python - trying newer compatible ones...
    "%RUNPY%" -m pip install "numpy>=1.26.3,<3" "scikit-learn>=1.4.0,<2" -q
    if errorlevel 1 goto :pip_error
)
"%RUNPY%" -m pip install "spacy==3.7.2" -q >nul 2>&1
if errorlevel 1 (
    echo       Exact spaCy version unavailable for this Python - trying a newer compatible one...
    "%RUNPY%" -m pip install "spacy>=3.7.2,<4" -q
    if errorlevel 1 goto :pip_error
)
"%RUNPY%" -m pip install "sentence-transformers==2.4.0" -q >nul 2>&1
if errorlevel 1 (
    echo       Exact version unavailable for this Python - trying a newer compatible one...
    "%RUNPY%" -m pip install "sentence-transformers>=2.4.0,<4" -q
    if errorlevel 1 goto :pip_error
)

echo       Step F: HTTP and test tools...
"%RUNPY%" -m pip install "httpx==0.26.0" "pytest==7.4.4" -q >nul 2>&1
if errorlevel 1 (
    "%RUNPY%" -m pip install "httpx>=0.26.0,<1" "pytest>=7.4.4,<9" -q
    if errorlevel 1 goto :pip_error
)

echo [OK] All packages installed.
goto :spacy_model

:pip_error
echo.
echo [ERROR] A package could not be installed. This is usually one of two things:
echo         1. Your internet connection dropped - just run this file again.
echo         2. This package has no ready-made installer for this Python
echo            version yet, nor for any compatible-enough version.
echo            Delete the "venv" and "pyembed" folders in this project
echo            ^(if present^) and run this file again to retry from scratch.
pause
exit /b 1

REM ---------------------------------------------------------------
REM  spaCy language model
REM ---------------------------------------------------------------
:spacy_model
echo [4/6] Checking spaCy language model...
"%RUNPY%" -c "import spacy; spacy.load('en_core_web_sm')" >nul 2>&1
if errorlevel 1 (
    echo       Downloading en_core_web_sm model, about 12 MB...
    "%RUNPY%" -m spacy download en_core_web_sm
    if errorlevel 1 (
        echo       Direct download failed, trying wheel fallback...
        "%RUNPY%" -m pip install "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.7.1/en_core_web_sm-3.7.1-py3-none-any.whl"
        if errorlevel 1 (
            echo [ERROR] Could not install the spaCy language model.
            echo         Check your internet connection and try again.
            pause
            exit /b 1
        )
    )
    echo [OK] spaCy model ready.
) else (
    echo [OK] spaCy model already installed.
)

REM ---------------------------------------------------------------
REM  Semantic similarity model (best-effort - the app still works
REM  without it, just with keyword/structure scoring only, so a
REM  failure here is a warning, not a stop-the-install error).
REM  Pre-downloading it now means the FIRST resume analysis won't
REM  be the one waiting on this download.
REM ---------------------------------------------------------------
echo [5/6] Pre-downloading semantic similarity model, about 90 MB...
"%RUNPY%" -c "import socket,sys; socket.setdefaulttimeout(5); s=socket.socket(); r=s.connect_ex(('huggingface.co',443)); s.close(); sys.exit(0 if r==0 else 1)" >nul 2>&1
if errorlevel 1 (
    echo       Skipped - huggingface.co is not reachable from this network right now.
    echo       This is OK: the app will still run using keyword and structure
    echo       scoring, and will retry this download automatically next time
    echo       you run the app with a working connection.
) else (
    "%RUNPY%" -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" >nul 2>&1
    if errorlevel 1 (
        echo       Could not download it right now. This is OK: the app will
        echo       still run using keyword and structure scoring.
    ) else (
        echo [OK] Semantic similarity model ready.
    )
)

REM ---------------------------------------------------------------
REM  Environment file
REM ---------------------------------------------------------------
echo [6/6] Checking configuration...
if not exist ".env" (
    if exist ".env.example" (
        copy /y ".env.example" ".env" >nul
        echo [OK] Created .env from template.
    )
) else (
    echo [OK] .env exists.
)
if not exist "app\data\" mkdir "app\data"

REM ---------------------------------------------------------------
REM  Sanity check: uvicorn must be importable before we try to launch
REM ---------------------------------------------------------------
"%RUNPY%" -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] uvicorn is still not installed correctly.
    echo         Delete the "venv" and "pyembed" folders in this project
    echo         ^(whichever is present^) and run this file again.
    pause
    exit /b 1
)

REM ---------------------------------------------------------------
REM  Launch
REM ---------------------------------------------------------------
echo.
echo  ============================================================
echo   Server ready at:  http://localhost:8001
echo   Open browser to:  http://localhost:8001
echo   API docs at:      http://localhost:8001/api/docs
echo   Press Ctrl+C to stop the server.
echo  ============================================================
echo.
echo  Starting server...
echo.

start "" "http://localhost:8001"
"%RUNPY%" -m uvicorn main:app --host 0.0.0.0 --port 8001

echo.
echo  Server stopped.
pause
exit /b 0

REM ---------------------------------------------------------------
REM  Subroutine: download and prepare a self-contained Python 3.12
REM  entirely inside this project's own folder (pyembed\). Uses
REM  Windows' built-in PowerShell to download and unzip - no extra
REM  software required. Never touches anything outside this folder.
REM ---------------------------------------------------------------
:bootstrap_python
if exist "pyembed\python.exe" (
    "pyembed\python.exe" -c "import pip" >nul 2>&1
    if not errorlevel 1 exit /b 0
)
if not exist "pyembed" mkdir "pyembed"

echo        Downloading Python 3.12 ^(about 10 MB^)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip' -OutFile 'pyembed\py312.zip' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
    echo        Could not download Python 3.12.
    exit /b 1
)

echo        Extracting...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -Path 'pyembed\py312.zip' -DestinationPath 'pyembed' -Force } catch { exit 1 }"
if errorlevel 1 (
    echo        Could not extract Python 3.12.
    exit /b 1
)
del "pyembed\py312.zip" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path 'pyembed\python312._pth') { (Get-Content 'pyembed\python312._pth') -replace '#import site','import site' | Set-Content 'pyembed\python312._pth' }"

echo        Installing pip...
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile 'pyembed\get-pip.py' -UseBasicParsing } catch { exit 1 }"
if errorlevel 1 (
    echo        Could not download the pip installer.
    exit /b 1
)
"pyembed\python.exe" "pyembed\get-pip.py" -q --no-warn-script-location
if errorlevel 1 (
    echo        Could not install pip.
    exit /b 1
)
del "pyembed\get-pip.py" >nul 2>&1
exit /b 0
