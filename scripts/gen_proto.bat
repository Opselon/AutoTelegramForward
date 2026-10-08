@echo off
REM Regenerate protobuf bindings for Python (core) and Go (api)
.venv\Scripts\python.exe scripts\gen_proto.py
