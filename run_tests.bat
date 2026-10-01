@echo off
REM ==========================================================================
REM  run_tests.bat - Test suite for geeko-pool-mqtt
REM
REM  Usage:
REM    run_tests.bat                 Unit tests, execution mode automatic
REM    run_tests.bat local           Unit tests with local Python 3.13
REM    run_tests.bat docker          Unit tests in the container
REM    run_tests.bat integration     Unit and integration tests with test broker
REM    run_tests.bat all             same as integration
REM    run_tests.bat build           Install dependencies or build the image
REM    run_tests.bat check           Show only the resolved environment
REM    run_tests.bat help            This help
REM
REM  Without an argument, local Python 3.13 is searched for. If none is found,
REM  the suite runs automatically in Docker if Docker is available. The
REM  python:3.13-slim image from Dockerfile.test contains all dependencies.
REM
REM  Further arguments are passed unchanged to pytest, for
REM  example:  run_tests.bat local -k heat_pump -x
REM
REM  Environment variables:
REM    PYTHON          Interpreter path, overrides the search
REM    MQTT_TEST_HOST  Broker host, default 127.0.0.1
REM    MQTT_TEST_PORT  Broker port, default 1883
REM ==========================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

REM ---- Read mode and arguments --------------------------------------------
set "GAVEARG=0"
if not "%~1"=="" set "GAVEARG=1"
set "MODE=%~1"
if not defined MODE set "MODE=auto"
if "%GAVEARG%"=="1" shift

set "EXTRA="
:collect_args
if "%~1"=="" goto :collect_done
REM "run_tests.bat docker unit" is a natural input. The word unit would be a
REM pytest path here, so it is discarded.
if /i "%~1"=="unit" if not defined EXTRA (
    shift
    goto :collect_args
)
set "EXTRA=!EXTRA! %1"
shift
goto :collect_args
:collect_done

if not defined MQTT_TEST_PORT set "MQTT_TEST_PORT=1883"
if not defined MQTT_TEST_HOST set "MQTT_TEST_HOST=127.0.0.1"

REM ---- Check mode ----------------------------------------------------------
set "KNOWN="
for %%M in (auto local docker integration int all build check help h) do (
    if /i "%MODE%"=="%%M" set "KNOWN=1"
)
if not defined KNOWN goto :unknown_mode
if /i "%MODE%"=="help" goto :usage
if /i "%MODE%"=="h" goto :usage

echo.
echo [INFO] Working directory ... %CD%

REM ---- Determine execution mode -------------------------------------------
if /i "%MODE%"=="local"  goto :use_local
if /i "%MODE%"=="docker" goto :use_docker

call :find_python
if defined PY goto :use_local
call :find_docker
if defined DOCKER goto :use_docker
goto :nothing_available

REM -------------------------------------------------------------------------
:use_local
call :find_python
if not defined PY (
    echo.
    echo [ERROR] No local Python 3.13 or newer found.
    echo.
    call :print_docker_hint
    exit /b 1
)
for /f "delims=" %%v in ('%PY% -c "import sys; print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo [INFO] Execution ...... local
echo [INFO] Interpreter ...... %PY%  !PYVER!
if /i "%MODE%"=="check" goto :ok_done
set "RUNNER=local"
goto :dispatch

:use_docker
call :find_docker
if not defined DOCKER (
    echo.
echo [ERROR] Docker was not found.
echo.
echo         Install Docker Desktop, or install Python 3.13 for
echo         a local run.
    exit /b 1
)
echo [INFO] Execution ...... Docker
if /i "%MODE%"=="check" goto :ok_done
set "RUNNER=docker"
goto :dispatch

REM -------------------------------------------------------------------------
:dispatch
if /i "!RUNNER!"=="docker" goto :dispatch_docker

REM Only local execution checks whether imports are available.
%PY% -c "import pytest, pytest_asyncio, paho.mqtt.client, gecko_iot_client, aiohttp, dotenv" >nul 2>&1
if errorlevel 1 goto :deps_missing
echo [INFO] Dependencies .. complete
echo.

if /i "%MODE%"=="build" goto :build_local
if /i "%MODE%"=="integration" goto :integration_local
if /i "%MODE%"=="int" goto :integration_local
if /i "%MODE%"=="all" goto :integration_local
goto :unit_local

:dispatch_docker
if /i "%MODE%"=="build" goto :build_docker
if /i "%MODE%"=="integration" goto :integration_docker
if /i "%MODE%"=="int" goto :integration_docker
if /i "%MODE%"=="all" goto :integration_docker
goto :unit_docker

REM -------------------------------------------------------------------------
:unit_local
echo === Unit tests (local) ===
echo.
%PY% -m pytest !EXTRA!
exit /b %ERRORLEVEL%

:integration_local
echo === Unit tests (local) ===
echo.
%PY% -m pytest !EXTRA!
set "UNIT_RC=!ERRORLEVEL!"
if not "!UNIT_RC!"=="0" exit /b !UNIT_RC!
goto :start_broker

:build_local
echo === Installing runtime and test dependencies ===
echo.
%PY% -m pip install --upgrade pip
if errorlevel 1 exit /b 1
%PY% -m pip install -r requirements.txt -r requirements-dev.txt
exit /b %ERRORLEVEL%

:unit_docker
echo === Unit tests (Docker) ===
echo The first run builds the image and can therefore take a while.
echo.
docker compose -f docker-compose.test.yml run --build --rm -T --no-deps tests python -m pytest !EXTRA!
exit /b %ERRORLEVEL%

:build_docker
echo === Building test image ===
echo.
docker compose -f docker-compose.test.yml build tests
exit /b %ERRORLEVEL%

:integration_docker
echo === Unit tests (Docker) ===
echo.
docker compose -f docker-compose.test.yml run --build --rm -T --no-deps tests python -m pytest !EXTRA!
set "UNIT_RC=!ERRORLEVEL!"
if not "!UNIT_RC!"=="0" exit /b !UNIT_RC!
goto :start_broker

REM -------------------------------------------------------------------------
:start_broker
echo.
echo === Starting broker ===
docker compose -f docker-compose.test.yml up -d mqtt-test
if errorlevel 1 (
    echo [ERROR] The broker could not be started.
    goto :cleanup
)
call :wait_for_broker
if errorlevel 1 (
    echo [ERROR] The broker does not answer on !MQTT_TEST_HOST!:!MQTT_TEST_PORT!
    goto :cleanup
)

echo.
echo === Integration tests ===
echo.
if /i "!RUNNER!"=="local" (
    %PY% -m pytest -m integration !EXTRA!
) else (
    docker compose -f docker-compose.test.yml run --build --rm -T --no-deps tests python -m pytest -m integration !EXTRA!
)
set "INT_RC=!ERRORLEVEL!"

echo.
echo === Stopping broker ===
docker compose -f docker-compose.test.yml down
if not "!INT_RC!"=="0" exit /b !INT_RC!
echo.
echo [OK] Integration tests succeeded.
exit /b 0

:cleanup
docker compose -f docker-compose.test.yml down
exit /b 1

REM -------------------------------------------------------------------------
:ok_done
echo [OK] Environment is ready.
exit /b 0

REM -------------------------------------------------------------------------
:deps_missing
echo.
echo [ERROR] Test dependencies are missing.
echo.
echo         Runtime:  gecko-iot-client, awscrt, awsiotsdk, aiohttp,
echo                  paho-mqtt, python-dotenv
echo         Tests:    pytest, pytest-asyncio
echo.
echo         Install once:     run_tests.bat build
echo         Or in container:  run_tests.bat docker
echo.
exit /b 1

REM -------------------------------------------------------------------------
REM  Find an interpreter with Python 3.13 or newer. Store the result in PY.
:find_python
set "PY="
if defined PYTHON (
    if exist "%PYTHON%" (
        "%PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%PYTHON%"
    )
)
if defined PY exit /b 0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=.venv\Scripts\python.exe"
)
if defined PY exit /b 0

py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.13"
if defined PY exit /b 0

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY exit /b 0

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
exit /b 0

REM -------------------------------------------------------------------------
:find_docker
set "DOCKER="
docker --version >nul 2>&1
if not errorlevel 1 set "DOCKER=1"
exit /b 0

REM -------------------------------------------------------------------------
REM  Wait for the broker. Deliberately without Python so the check also works
REM  in Docker mode, where local Python is unavailable.
:wait_for_broker
set /a TRIES=0
:wait_loop
powershell -NoProfile -Command "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect($env:MQTT_TEST_HOST, [int]$env:MQTT_TEST_PORT); exit 0 } catch { exit 1 } finally { $c.Close() }" >nul 2>&1
if not errorlevel 1 exit /b 0
set /a TRIES+=1
if !TRIES! GEQ 30 exit /b 1
ping -n 2 127.0.0.1 >nul
goto :wait_loop

REM -------------------------------------------------------------------------
:print_docker_hint
echo         No local Python 3.13 was found. Options:
echo.
echo           1) Install Python 3.13, then start again
echo           2) Use Docker:              run_tests.bat docker
echo           3) Own interpreter:          set PYTHON=C:\Python313\python.exe
echo.
exit /b 0

:nothing_available
echo.
echo [ERROR] Neither Python 3.13 nor Docker is available.
echo.
echo         gecko-iot-client 1.0.3 requires Python 3.13 or newer.
echo.
echo           1) Install Python 3.13
echo           2) Install Docker Desktop, then: run_tests.bat docker
echo           3) Own interpreter:          set PYTHON=C:\Python313\python.exe
echo.
exit /b 1

REM -------------------------------------------------------------------------
:unknown_mode
echo.
echo [ERROR] Unknown mode "!MODE!".
echo.
goto :usage

REM -------------------------------------------------------------------------
:usage
echo [INFO] Modes:
echo.
echo     auto          Default, picks local or Docker automatically
echo     local         Unit tests with local Python 3.13
echo     docker        Unit tests in the container
echo     integration   Unit and integration tests with test broker
echo     all           same as integration
echo     build         Installs dependencies or builds the test image
echo     check         Only checks the environment
echo     help          This help
echo.
echo [INFO] Examples:
echo.
echo     run_tests.bat
echo     run_tests.bat local -k heat_pump -x
echo     run_tests.bat docker
echo     run_tests.bat integration
echo     run_tests.bat build
echo.
echo [INFO] Environment variables:
echo.
echo     PYTHON          Path to the interpreter, overrides the search
echo     MQTT_TEST_HOST  Broker host, default 127.0.0.1
echo     MQTT_TEST_PORT  Broker port, default 1883
echo.
endlocal
exit /b 0
