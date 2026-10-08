#!/usr/bin/env bash
# Regenerate protobuf bindings for Python (core) and Go (api)
.venv/Scripts/python.exe scripts/gen_proto.py 2>/dev/null || python3 scripts/gen_proto.py
