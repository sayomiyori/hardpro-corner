"""HTTP-морда переключателя моделей. Один роутер на доску и на агента.

    GET  /api/models                  — раскладка: профили, роли, карты
    POST /api/models/switch           — {role, model, start} переключить роль
    POST /api/models/role             — {role, model} только привязка, без запуска
    POST /api/models/launch           — {model} поднять профиль как есть
    POST /api/models/stop             — {model} погасить
    GET  /api/models/gpu              — карты
    POST /api/models/profile          — {model, spec} зарегистрировать/обновить
    DELETE /api/models/profile/{model} — убрать профиль
    GET  /api/models/check            — конфликты каталога (порты, карты, файлы)
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import registry

router = APIRouter()


class SwitchIn(BaseModel):
    role: str
    model: str
    start: bool = True


class RoleIn(BaseModel):
    role: str
    model: str


class ModelIn(BaseModel):
    model: str


class ProfileIn(BaseModel):
    model: str
    spec: dict


async def _run(fn, *args, **kwargs):
    """llama-server грузится минутами — не держим event loop."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except registry.RegistryError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/api/models")
async def api_models() -> dict:
    return await _run(registry.status)


@router.get("/api/models/gpu")
async def api_gpu() -> dict:
    return {"gpu": await _run(registry.gpus)}


@router.get("/api/models/check")
async def api_check() -> dict:
    return await _run(registry.switch_check)


@router.post("/api/models/switch")
async def api_switch(body: SwitchIn) -> dict:
    return await _run(registry.switch, body.role, body.model, body.start)


@router.post("/api/models/role")
async def api_role(body: RoleIn) -> dict:
    return await _run(registry.set_role, body.role, body.model)


@router.post("/api/models/launch")
async def api_launch(body: ModelIn) -> dict:
    return await _run(registry.launch, body.model)


@router.post("/api/models/stop")
async def api_stop(body: ModelIn) -> dict:
    return await _run(registry.stop, body.model)


@router.post("/api/models/restart")
async def api_restart(body: ModelIn) -> dict:
    return await _run(registry.restart, body.model)


@router.post("/api/models/profile")
async def api_profile(body: ProfileIn) -> dict:
    return await _run(registry.upsert_profile, body.model, body.spec)


@router.delete("/api/models/profile/{model}")
async def api_drop(model: str) -> dict:
    return await _run(registry.drop_profile, model)
