"""Classify one upload in a disposable process with bounded memory and CPU."""
import json
import resource
import sys
from dataclasses import asdict
from pathlib import Path

from .formats import detect
from .sandbox import limit_self


if __name__ == "__main__":
    limit_self()
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
    print(json.dumps(asdict(detect(Path(sys.argv[1]), "input" + sys.argv[2]))))
