import sys
from pathlib import Path

# Ensure proto directory is in sys.path so generated code's flat imports work seamlessly
_proto_dir = str(Path(__file__).parent.resolve())
if _proto_dir not in sys.path:
    sys.path.insert(0, _proto_dir)

from . import autoforward_pb2 as pb
from . import autoforward_pb2_grpc as pb_grpc

__all__ = ["pb", "pb_grpc"]
