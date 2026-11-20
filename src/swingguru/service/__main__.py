"""Run the service: ``python -m swingguru.service`` (reads configuration from the environment)."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return "n/a"


def log_cpu() -> None:
    """What the container really gets: host cores vs the cgroup quota (ORT sizes its pool from the former)."""

    affinity = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    cpuinfo = _read("/proc/cpuinfo")
    model = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith("model name")), "n/a")
    logging.getLogger("swingguru").info(
        "cpu: os.cpu_count=%s affinity=%s cgroup cpu.max=%s memory.max=%s model=%s SG_ORT_THREADS=%s OMP_NUM_THREADS=%s",
        os.cpu_count(), affinity, _read("/sys/fs/cgroup/cpu.max"), _read("/sys/fs/cgroup/memory.max"), model,
        os.environ.get("SG_ORT_THREADS", "unset"), os.environ.get("OMP_NUM_THREADS", "unset"))


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
    log_cpu()
    from .api import create_app
    from .config import ConfigError

    try:
        app = create_app()
    except ConfigError as error:
        logging.getLogger("swingguru").error("invalid configuration: %s", error)
        return 2
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8790")),
                proxy_headers=True, forwarded_allow_ips="*", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
