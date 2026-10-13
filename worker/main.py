"""Punto de entrada del contenedor: `python -m worker.main worker|scheduler`.

Delega en la CLI de Taskiq. Un despliegue = N procesos `worker` + exactamente UN `scheduler`
(dos scheduler encolarían cada job dos veces).
"""
import os
import sys

COMMANDS = {
    "worker": ["taskiq", "worker", "worker.broker:broker", "worker.jobs", "--workers", "1"],
    "scheduler": ["taskiq", "scheduler", "worker.broker:scheduler", "worker.jobs", "--skip-first-run"],
}


# Proceso propio, sin Taskiq: la cola de inventarios (worker/jobs/inventory.py). UNO por despliegue.
INVENTORY = "inventory"


def argv_for(mode: str) -> list[str]:
    if mode not in COMMANDS:
        raise SystemExit(f"uso: python -m worker.main {{{'|'.join([*COMMANDS, INVENTORY])}}}")
    return COMMANDS[mode]


def main(args: list[str] | None = None) -> None:
    args = sys.argv[1:] if args is None else args
    if args and args[0] == INVENTORY:
        import asyncio

        from worker.jobs import inventory
        asyncio.run(inventory.main())
        return
    argv = argv_for(args[0] if args else "")
    os.execvp(argv[0], argv)  # noqa: S606 — argv fijo de esta tabla, sin shell ni entrada externa


if __name__ == "__main__":
    main()
