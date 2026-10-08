@echo off
REM Run both services: Python core (bot + forwarder + gRPC) and Go API
REM Configure via config.yaml or ATF_* environment variables first.

echo Starting Python core (bot + gRPC on %ATF_GRPC_PORT%)...
start "ATF-Core" cmd /k ".venv\Scripts\python.exe -m core.main"

timeout /t 3 >nul

echo Starting Go API (HTTP on %ATF_API_ADDR%)...
cd api
start "ATF-API" cmd /k "atf-api.exe"
cd ..

echo Both services started.
