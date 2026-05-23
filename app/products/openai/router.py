"""OpenAI-compatible API router (/v1/*)."""

import base64
import binascii
import mimetypes
import re
from typing import Annotated, AsyncGenerator, AsyncIterable, Literal

import orjson
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

from app.control.account.state_machine import is_manageable
from app.platform.auth.middleware import verify_api_key
from app.platform.errors import AppError, ValidationError
from app.platform.logging.logger import logger
from app.platform.storage import image_files_dir, video_files_dir
from app.control.model import registry as model_registry
from app.control.model.spec import ModelSpec
from app.control.account.quota_defaults import supports_mode
from .schemas import (
    ChatCompletionRequest,
    ImageGenerationRequest,
    VideoConfig,
    ImageConfig,
    ResponsesCreateRequest,
)
from .chat import completions as chat_completions

router = APIRouter(prefix="/v1")
_POOL_ID_TO_NAME = {0: "basic", 1: "super", 2: "heavy"}
_TAG_MODELS = "OpenAI - Models"
_TAG_CHAT = "OpenAI - Chat"
_TAG_RESPONSES = "OpenAI - Responses"
_TAG_IMAGES = "OpenAI - Images"
_TAG_VIDEOS = "OpenAI - Videos"
_TAG_FILES = "OpenAI - Files"
_GROK_IMAGINE_VIDEO_MODEL = "grok-imagine-video"


def _is_grok_imagine_video_alias(model: str | None) -> bool:
    normalized = (model or "").strip().lower()
    return normalized.startswith("grok-imagine-") and "-video-" in normalized


def _grok_imagine_video_alias_resolution(model: str | None) -> str | None:
    normalized = (model or "").strip().lower()
    if normalized.startswith("grok-imagine-0.8-video-"):
        return "480p"
    if normalized.startswith("grok-imagine-1.0-video-"):
        return "720p"
    if _is_grok_imagine_video_alias(normalized):
        return "720p"
    return None


def _grok_imagine_video_alias_seconds(model: str | None) -> int | None:
    normalized = (model or "").strip().lower()
    if not _is_grok_imagine_video_alias(normalized):
        return None
    match = re.search(r"-(\d+)s\b", normalized)
    if match:
        return int(match.group(1))
    return None


def _grok_imagine_video_alias_size(model: str | None) -> str | None:
    normalized = (model or "").strip().lower()
    if not _is_grok_imagine_video_alias(normalized):
        return None
    if "landscape" in normalized:
        return "1280x720"
    if "portrait" in normalized:
        return "720x1280"
    return None


def _normalize_video_prompt_ratio(prompt: str | None, size: str) -> str | None:
    if not prompt:
        return prompt

    ratio = "16:9 横屏" if size == "1280x720" else "9:16 竖屏"
    replacements = {
        "视频比例：9:16 竖屏": f"视频比例：{ratio}",
        "视频比例: 9:16 竖屏": f"视频比例: {ratio}",
        "视频比例：16:9 横屏": f"视频比例：{ratio}",
        "视频比例: 16:9 横屏": f"视频比例: {ratio}",
    }
    for old, new in replacements.items():
        prompt = prompt.replace(old, new)
    return prompt


def _normalize_grok_video_alias_request(
    model: str | None,
    prompt: str | None,
    size: str | None,
    resolution_name: str | None,
    seconds: str | int | None,
    aspect_ratio: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | int | None]:
    if not _is_grok_imagine_video_alias(model):
        return model, prompt, size, resolution_name, seconds

    alias_size = _grok_imagine_video_alias_size(model)
    if not alias_size:
        requested_aspect = (aspect_ratio or "").strip()
        if requested_aspect == "9:16":
            alias_size = "720x1280"
        elif requested_aspect == "16:9":
            alias_size = "1280x720"
        else:
            requested_size = (size or "").strip().lower()
            alias_size = "720x1280" if requested_size == "720x1280" else "1280x720"

    return (
        _GROK_IMAGINE_VIDEO_MODEL,
        _normalize_video_prompt_ratio(prompt, alias_size),
        alias_size,
        _grok_imagine_video_alias_resolution(model) or resolution_name,
        _grok_imagine_video_alias_seconds(model) or seconds,
    )


async def _available_pools(request: Request) -> frozenset[str]:
    repo = getattr(request.app.state, "repository", None)
    if repo is None:
        return frozenset()

    snapshot = await repo.runtime_snapshot()
    pools = {record.pool for record in snapshot.items if is_manageable(record)}
    return frozenset(pools)


def _model_available_for_pools(spec: ModelSpec, pools: frozenset[str]) -> bool:
    if not spec.enabled:
        return False
    for pool_id in spec.pool_candidates():
        pool = _POOL_ID_TO_NAME[pool_id]
        if pool in pools and supports_mode(pool, int(spec.mode_id)):
            return True
    return False


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


@router.get("/models", tags=[_TAG_MODELS], dependencies=[Depends(verify_api_key)])
async def list_models(request: Request):
    import time

    pools = await _available_pools(request)
    models = [
        {
            "id": m.model_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "xai",
            "name": m.public_name,
        }
        for m in model_registry.list_enabled()
        if _model_available_for_pools(m, pools)
    ]
    return JSONResponse({"object": "list", "data": models})


@router.get(
    "/models/{model_id}", tags=[_TAG_MODELS], dependencies=[Depends(verify_api_key)]
)
async def get_model_endpoint(model_id: str, request: Request):
    import time

    spec = model_registry.get(model_id)
    pools = await _available_pools(request)
    if spec is None or not _model_available_for_pools(spec, pools):
        return JSONResponse(
            {
                "error": {
                    "message": f"Model {model_id!r} not found",
                    "type": "invalid_request_error",
                }
            },
            status_code=404,
        )
    return JSONResponse(
        {
            "id": spec.model_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "xai",
            "name": spec.public_name,
        }
    )


# ---------------------------------------------------------------------------
# SSE streaming helpers
# ---------------------------------------------------------------------------


async def _safe_sse(stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    """Wrap an SSE stream, converting exceptions to in-band error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        payload = orjson.dumps({"error": exc.to_dict()["error"]}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        payload = orjson.dumps(
            {"error": {"message": str(exc), "type": "server_error"}}
        ).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------

_VALID_ROLES = {"developer", "system", "user", "assistant", "tool"}
_USER_BLOCK_TYPES = {"text", "image_url", "input_audio", "file"}
_ALLOWED_SIZES = {"1280x720", "720x1280", "1792x1024", "1024x1792", "1024x1024"}
_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh"}
_LITE_IMAGE_MODELS = {"grok-imagine-image-lite"}


def _validate_chat(req: ChatCompletionRequest) -> None:
    from app.platform.errors import ValidationError

    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")
    for i, msg in enumerate(req.messages):
        if msg.role not in _VALID_ROLES:
            raise ValidationError(
                f"role must be one of {sorted(_VALID_ROLES)}",
                param=f"messages.{i}.role",
            )
    if req.temperature is not None and not (0 <= req.temperature <= 2):
        raise ValidationError(
            "temperature must be between 0 and 2", param="temperature"
        )
    if req.top_p is not None and not (0 <= req.top_p <= 1):
        raise ValidationError("top_p must be between 0 and 1", param="top_p")
    if req.reasoning_effort is not None and req.reasoning_effort not in _EFFORT_VALUES:
        raise ValidationError(
            f"reasoning_effort must be one of {sorted(_EFFORT_VALUES)}",
            param="reasoning_effort",
        )


def _validate_image_n(model_name: str, n: int, *, param: str) -> None:
    max_n = 4 if model_name in _LITE_IMAGE_MODELS else 10
    if not (1 <= n <= max_n):
        raise ValidationError(
            f"n must be between 1 and {max_n} for model {model_name!r}",
            param=param,
        )


def _validate_image_edit_n(n: int, *, param: str) -> None:
    if not (1 <= n <= 2):
        raise ValidationError("n must be between 1 and 2 for image edit", param=param)


async def _upload_to_data_uri(upload: UploadFile, *, param: str) -> str:
    raw = await upload.read()
    if not raw:
        raise ValidationError("Uploaded image cannot be empty", param=param)

    mime = (
        (upload.content_type or "").strip().lower()
        or mimetypes.guess_type(upload.filename or "")[0]
        or "application/octet-stream"
    )
    if not mime.startswith("image/"):
        raise ValidationError("Uploaded file must be an image", param=param)

    try:
        blob_b64 = base64.b64encode(raw).decode("ascii")
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationError("Failed to encode uploaded image", param=param) from exc
    return f"data:{mime};base64,{blob_b64}"


@router.post(
    "/chat/completions", tags=[_TAG_CHAT], dependencies=[Depends(verify_api_key)]
)
async def chat_completions_endpoint(req: ChatCompletionRequest):
    _validate_chat(req)
    from app.platform.config.snapshot import get_config

    cfg = get_config()
    is_stream = (
        req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    )

    spec = model_registry.get(req.model)
    if spec is None:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    messages = [m.model_dump(exclude_none=True) for m in req.messages]

    try:
        # Dispatch by model capability.
        if spec.is_image_edit():
            from .images import edit as img_edit

            cfg = req.image_config or ImageConfig()
            _validate_image_edit_n(cfg.n or 1, param="image_config.n")
            result = await img_edit(
                model=req.model,
                messages=messages,
                n=cfg.n or 1,
                size=cfg.size or "1024x1024",
                response_format=cfg.response_format or "url",
                stream=is_stream,
                chat_format=True,
            )

        elif spec.is_image():
            from .images import generate as img_gen

            cfg = req.image_config or ImageConfig()
            size = cfg.size or "1024x1024"
            fmt = cfg.response_format or "url"
            n = cfg.n or 1
            _validate_image_n(req.model, n, param="image_config.n")
            # Extract prompt from last user message.
            prompt = next(
                (
                    m.content
                    for m in reversed(req.messages)
                    if m.role == "user"
                    and isinstance(m.content, str)
                    and m.content.strip()
                ),
                "",
            )
            result = await img_gen(
                model=req.model,
                prompt=prompt or "",
                n=n,
                size=size,
                response_format=fmt,
                stream=is_stream,
                chat_format=True,
            )

        elif spec.is_video():
            from .video import completions as vid_comp

            vcfg = req.video_config or VideoConfig()
            from .video import validate_video_length as _validate_video_length

            _validate_video_length(vcfg.seconds or 6)
            result = await vid_comp(
                model=req.model,
                messages=messages,
                stream=is_stream,
                seconds=vcfg.seconds or 6,
                size=vcfg.size or "720x1280",
                resolution_name=vcfg.resolution_name,
                preset=vcfg.preset,
            )

        else:
            # reasoning_effort=None → config default; "none" → off; otherwise → on.
            if req.reasoning_effort is None:
                emit_think: bool | None = None
            else:
                emit_think = req.reasoning_effort != "none"
            result = await chat_completions(
                model=req.model,
                messages=messages,
                stream=is_stream,
                emit_think=emit_think,
                tools=req.tools,
                tool_choice=req.tool_choice,
                temperature=req.temperature or 0.8,
                top_p=req.top_p or 0.95,
            )

    except AppError:
        raise
    except Exception as exc:
        logger.exception(
            "chat completions endpoint failed: model={} stream={} error={}",
            req.model,
            is_stream,
            exc,
        )
        if is_stream:
            _err_msg = str(
                exc
            )  # capture before Python clears the except-scope variable

            async def _err_stream():
                payload = orjson.dumps(
                    {"error": {"message": _err_msg, "type": "server_error"}}
                ).decode()
                yield f"event: error\ndata: {payload}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _err_stream(), media_type="text/event-stream", headers=_SSE_HEADERS
            )
        raise

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse(result), media_type="text/event-stream", headers=_SSE_HEADERS
    )


# ---------------------------------------------------------------------------
# /v1/responses  (OpenAI Responses API)
# ---------------------------------------------------------------------------


async def _safe_sse_responses(stream) -> AsyncGenerator[str, None]:
    """SSE wrapper that converts errors to Responses API error events."""
    try:
        async for chunk in stream:
            yield chunk
    except Exception as exc:
        from app.platform.errors import AppError

        if isinstance(exc, AppError):
            err = exc.to_dict()["error"]
        else:
            err = {
                "message": str(exc),
                "type": "server_error",
                "code": None,
                "param": None,
            }
        payload = orjson.dumps({"type": "error", **err}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


@router.post(
    "/responses", tags=[_TAG_RESPONSES], dependencies=[Depends(verify_api_key)]
)
async def responses_endpoint(req: ResponsesCreateRequest):
    from app.platform.config.snapshot import get_config
    from app.platform.errors import ValidationError as _ValidationError

    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise _ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    if not req.input:
        raise _ValidationError("input cannot be empty", param="input")

    cfg = get_config()
    is_stream = (
        req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    )

    # Map reasoning param → emit_think flag.
    # reasoning=None → use config; reasoning.effort="none" → off; otherwise on.
    if req.reasoning is None:
        emit_think = cfg.get_bool("features.thinking", True)
    elif isinstance(req.reasoning, dict) and req.reasoning.get("effort") == "none":
        emit_think = False
    else:
        emit_think = True

    from .responses import create as responses_create

    result = await responses_create(
        model=req.model,
        input_val=req.input,
        instructions=req.instructions,
        stream=is_stream,
        emit_think=emit_think,
        temperature=req.temperature or 0.8,
        top_p=req.top_p or 0.95,
        tools=req.tools or None,
        tool_choice=req.tool_choice,
    )

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse_responses(result),
        media_type = "text/event-stream",
        headers    = _SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# /v1/images/generations (standalone image endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/images/generations", tags=[_TAG_IMAGES], dependencies=[Depends(verify_api_key)]
)
async def image_generations(req: ImageGenerationRequest):
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled or not spec.is_image():
        raise ValidationError(
            f"Model {req.model!r} is not an image model", param="model"
        )
    _validate_image_n(req.model, req.n or 1, param="n")

    from .images import generate as img_gen

    result = await img_gen(
        model=req.model,
        prompt=req.prompt,
        n=req.n or 1,
        size=req.size or "1024x1024",
        response_format=req.response_format or "url",
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# /v1/videos (OpenAI videos.create surface)
# ---------------------------------------------------------------------------


@router.post("/videos", tags=[_TAG_VIDEOS], dependencies=[Depends(verify_api_key)])
async def videos_create(request: Request):
    from .video import create_video

    content_type = (request.headers.get("content-type") or "").split(";", 1)[0].lower()
    model: str | None = None
    prompt: str | None = None
    seconds: str | int | None = None
    size: str | None = None
    aspect_ratio: str | None = None
    resolution_name: str | None = None
    preset: str | None = None
    references_payload: list[dict[str, str]] | None = None

    if content_type == "application/json":
        try:
            body = await request.json()
        except ValueError as exc:
            raise ValidationError("Request body must be valid JSON", param="body") from exc
        if not isinstance(body, dict):
            raise ValidationError("Request body must be a JSON object", param="body")

        model = str(body.get("model") or "").strip() or None
        prompt = str(body.get("prompt") or "").strip() or None
        seconds = body.get("seconds", body.get("duration", 6))
        size = str(body.get("size") or "720x1280").strip() or "720x1280"
        aspect_ratio = str(body.get("aspect_ratio") or body.get("aspectRatio") or "").strip() or None
        resolution_name = str(body.get("resolution_name") or body.get("resolution") or "").strip() or None
        preset = str(body.get("preset") or "").strip() or None

        images = (
            body.get("images")
            or body.get("image_urls")
            or body.get("input_references")
            or body.get("input_reference")
            or []
        )
        if isinstance(images, (str, dict)):
            images = [images]
        if images:
            if not isinstance(images, list):
                raise ValidationError(
                    "images must be a list of image URLs or data URLs",
                    param="images",
                )
            references_payload = []
            for item in images[:7]:
                image_url = ""
                if isinstance(item, dict):
                    image_url = str(
                        item.get("image_url")
                        or item.get("url")
                        or item.get("dataUrl")
                        or item.get("data_url")
                        or ""
                    ).strip()
                else:
                    image_url = str(item).strip()
                if image_url:
                    references_payload.append({"image_url": image_url})
            references_payload = references_payload or None
    else:
        form = await request.form()
        model = str(form.get("model") or "").strip() or None
        prompt = str(form.get("prompt") or "").strip() or None
        seconds = form.get("seconds") or form.get("duration") or 6
        size = str(form.get("size") or "720x1280").strip() or "720x1280"
        aspect_ratio = str(form.get("aspect_ratio") or form.get("aspectRatio") or "").strip() or None
        resolution_name = str(form.get("resolution_name") or form.get("resolution") or "").strip() or None
        preset = str(form.get("preset") or "").strip() or None

        uploads = []
        for key in ("input_reference[]", "input_reference"):
            uploads.extend(
                item
                for item in form.getlist(key)
                if hasattr(item, "read") and hasattr(item, "filename")
            )
        if uploads:
            references_payload = [
                {"image_url": await _upload_to_data_uri(f, param="input_reference")}
                for f in uploads[:7]
            ]
        else:
            images = (
                form.getlist("images")
                or form.getlist("image_urls")
                or form.getlist("image")
                or form.getlist("input_references")
            )
            image_values = [str(item).strip() for item in images if str(item).strip()]
            if image_values:
                references_payload = [
                    {"image_url": item}
                    for item in image_values[:7]
                ]

    if not model:
        raise ValidationError("model is required", param="model")
    if not prompt:
        raise ValidationError("prompt is required", param="prompt")
    model, prompt, size, resolution_name, seconds = _normalize_grok_video_alias_request(
        model,
        prompt,
        size,
        resolution_name,
        seconds,
        aspect_ratio,
    )

    result = await create_video(
        model=model or "grok-video",
        prompt=prompt,
        seconds=seconds,
        size=size or "720x1280",
        resolution_name=resolution_name,
        preset=preset,
        input_references=references_payload,
    )
    return JSONResponse(result)


@router.get(
    "/videos/{video_id}", tags=[_TAG_VIDEOS], dependencies=[Depends(verify_api_key)]
)
async def videos_retrieve(video_id: str):
    from .video import retrieve

    return JSONResponse(await retrieve(video_id))


@router.get(
    "/videos/{video_id}/content",
    tags=[_TAG_VIDEOS],
    dependencies=[Depends(verify_api_key)],
)
async def videos_content(video_id: str):
    from .video import content_path

    path = await content_path(video_id)
    return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4")


# ---------------------------------------------------------------------------
# /v1/images/edits (standalone image-edit endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/images/edits", tags=[_TAG_IMAGES], dependencies=[Depends(verify_api_key)]
)
async def image_edits(
    model: Annotated[str, Form(...)],
    prompt: Annotated[str, Form(...)],
    image: Annotated[list[UploadFile], File(..., alias="image[]")],
    mask: Annotated[UploadFile | None, File()] = None,
    n: Annotated[int, Form()] = 1,
    size: Annotated[str, Form()] = "1024x1024",
    response_format: Annotated[str, Form()] = "url",
):
    spec = model_registry.get(model)
    if spec is None or not spec.enabled or not spec.is_image_edit():
        raise ValidationError(
            f"Model {model!r} is not an image-edit model", param="model"
        )
    if mask is not None:
        raise ValidationError("mask is not supported yet", param="mask")
    _validate_image_edit_n(n, param="n")

    from .images import edit as img_edit

    image_inputs = [
        await _upload_to_data_uri(item, param=f"image.{index}")
        for index, item in enumerate(image)
    ]
    # Wrap input into a single-message conversation.
    content = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image_input}}
        for image_input in image_inputs
    )
    messages = [{"role": "user", "content": content}]
    result = await img_edit(
        model=model,
        messages=messages,
        n=n,
        size=size,
        response_format=response_format,
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# /v1/files/image — serve locally saved images
# ---------------------------------------------------------------------------


@router.get("/files/video", tags=[_TAG_FILES])
async def serve_video(id: str = Query(..., description="Video file ID")):
    """Serve a locally cached video by file ID."""
    import re

    if not re.fullmatch(r"[0-9a-f\-]{16,36}", id):
        raise ValidationError("Invalid file ID", param="id")

    video_dir = video_files_dir()
    for name in (f"{id}.mp4", f"video_{id}.mp4"):
        path = video_dir / name
        if path.exists():
            return FileResponse(path, media_type="video/mp4")

    raise ValidationError(f"Video {id!r} not found", param="id")


@router.get("/files/image", tags=[_TAG_FILES])
async def serve_image(id: str = Query(..., description="Image file ID")):
    """Serve a locally cached image by file ID."""
    import re

    if not re.fullmatch(r"[0-9a-f\-]{16,36}", id):
        raise ValidationError("Invalid file ID", param="id")

    img_dir = image_files_dir()
    for ext in (".jpg", ".png"):
        path = img_dir / f"{id}{ext}"
        if path.exists():
            mime = "image/png" if ext == ".png" else "image/jpeg"
            return FileResponse(path, media_type=mime)

    raise ValidationError(f"Image {id!r} not found", param="id")


__all__ = ["router"]
