"""``/v1/models``：给 Open WebUI 一个稳定的模型 id。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from .deps import CurrentUser, get_services, require_user

router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models")
def list_models(
    request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    services = get_services(request)
    model = services.settings.upstream.model
    return {
        "object": "list",
        "data": [
            {
                "id": model,
                "object": "model",
                "created": int(services.started_at) or int(time.time()),
                "owned_by": "llm-serving",
                "permission": [],
                "root": model,
            }
        ],
    }
