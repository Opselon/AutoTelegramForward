"""Protobuf generation script for Python and Go."""
import os
import subprocess
import sys
from pathlib import Path

def main():
    repo_root = Path(__file__).resolve().parent.parent
    proto_file = repo_root / "proto" / "autoforward.proto"
    python_out = repo_root / "core" / "proto"
    go_out = repo_root / "api" / "proto"

    python_out.mkdir(parents=True, exist_ok=True)
    go_out.mkdir(parents=True, exist_ok=True)

    print(f"Generating Python protobuf from {proto_file}...")
    subprocess.run([
        sys.executable, "-m", "grpc_tools.protoc",
        f"--proto_path={repo_root / 'proto'}",
        f"--python_out={python_out}",
        f"--grpc_python_out={python_out}",
        str(proto_file)
    ], check=True)

    # Patch grpc import in autoforward_pb2_grpc.py if necessary
    grpc_py = python_out / "autoforward_pb2_grpc.py"
    if grpc_py.exists():
        content = grpc_py.read_text(encoding="utf-8")
        if "import autoforward_pb2 as autoforward__pb2" in content:
            new_content = content.replace(
                "import autoforward_pb2 as autoforward__pb2",
                "from . import autoforward_pb2 as autoforward__pb2"
            )
            grpc_py.write_text(new_content, encoding="utf-8")

    print(f"Generating Go protobuf from {proto_file}...")
    env = os.environ.copy()
    user_home = Path.home()
    go_bin = user_home / "go" / "bin"
    if go_bin.exists():
        env["PATH"] = str(go_bin) + os.pathsep + env.get("PATH", "")

    subprocess.run([
        "protoc",
        f"--proto_path={repo_root / 'proto'}",
        f"--go_out={go_out}",
        "--go_opt=paths=source_relative",
        f"--go-grpc_out={go_out}",
        "--go-grpc_opt=paths=source_relative",
        str(proto_file)
    ], env=env, check=True)

    print("Protobuf code generation completed successfully!")

if __name__ == "__main__":
    main()
