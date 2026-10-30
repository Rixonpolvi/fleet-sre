"""Mock Chronicle API (D3 stub)."""

from fastapi import FastAPI

from fleetsre.mock.world import World


def create_app(
    world: World, *, chaos: bool = False, dev_auth: bool = False
) -> FastAPI:
    raise NotImplementedError
