#!/usr/bin/env python3
"""Command-line media client for the AuriX AI gateway.

This client uses AuriX's documented OpenAI-compatible HTTP surface. It does not
read browser sessions or call private ChatGPT/Flow frontend endpoints.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


DEFAULT_BASE_URL = "https://ai.aurix-mart.tech"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class AuriXMediaCLIError(RuntimeError):
    """Raised for expected command-line client failures."""


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _request(
    *,
    base_url: str,
    token: str,
    path: str,
    payload: dict[str, Any] | None = None,
    accept: str = "application/json",
    timeout: float = 180,
) -> tuple[bytes, str]:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
    }
    data = None
    method = "GET"
    if payload is not None:
        data = _json_bytes(payload)
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read(16 * 1024).decode("utf-8", errors="replace")
        raise AuriXMediaCLIError(f"HTTP {exc.code} from {url}: {body.strip()}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuriXMediaCLIError(f"Could not reach {url}: {exc}") from exc


def _request_absolute(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    data: bytes | None = None,
    method: str | None = None,
    timeout: float = 180,
) -> tuple[bytes, str]:
    body = data
    request_headers = dict(headers)
    if payload is not None:
        body = _json_bytes(payload)
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method or ("POST" if body is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        body = exc.read(16 * 1024).decode("utf-8", errors="replace")
        raise AuriXMediaCLIError(f"HTTP {exc.code} from {url}: {body.strip()}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuriXMediaCLIError(f"Could not reach {url}: {exc}") from exc


def _read_json(raw: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuriXMediaCLIError("Server returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AuriXMediaCLIError("Server returned a non-object JSON payload")
    return payload


def _write_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def _extension_for_content_type(content_type: str, fallback: str = ".bin") -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return mimetypes.guess_extension(media_type) or fallback


def _first_image_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise AuriXMediaCLIError("Image response did not include data[0]")
    return data[0]


def _save_json_image(payload: dict[str, Any], output: Path) -> Path:
    item = _first_image_data(payload)
    if isinstance(item.get("b64_json"), str):
        output.write_bytes(base64.b64decode(item["b64_json"]))
        return output
    if isinstance(item.get("url"), str):
        output.write_text(item["url"] + "\n", encoding="utf-8")
        return output
    raise AuriXMediaCLIError("Image response did not include b64_json or url")


def _multipart_form(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = "----aurix-media-cli-" + base64.urlsafe_b64encode(os.urandom(12)).decode("ascii")
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, path in files.items():
        filename = path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _poll_openai_video(
    *,
    base_url: str,
    token: str,
    video_id: str,
    timeout: float,
    poll_interval: float,
    max_polls: int,
) -> dict[str, Any]:
    status_url = urljoin(base_url.rstrip("/") + "/", f"v1/videos/{video_id}")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    status = "queued"
    payload: dict[str, Any] = {"id": video_id, "status": status}
    for _attempt in range(max(1, max_polls)):
        raw, _content_type = _request_absolute(
            url=status_url,
            headers=headers,
            method="GET",
            timeout=timeout,
        )
        payload = _read_json(raw)
        status = str(payload.get("status") or "")
        if status in {"completed", "failed", "cancelled", "expired"}:
            return payload
        if poll_interval > 0:
            import time

            time.sleep(poll_interval)
    raise AuriXMediaCLIError(f"Video {video_id} did not finish after {max_polls} polls")


def _poll_aurix_video(
    *,
    base_url: str,
    token: str,
    video_id: str,
    timeout: float,
    poll_interval: float,
    max_polls: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": video_id, "status": "queued"}
    for _attempt in range(max(1, max_polls)):
        raw, _content_type = _request(
            base_url=base_url,
            token=token,
            path=f"/v1/videos/{video_id}",
            timeout=timeout,
        )
        payload = _read_json(raw)
        status = str(payload.get("status") or "")
        if status in {"completed", "failed", "cancelled", "expired"}:
            return payload
        if poll_interval > 0:
            import time

            time.sleep(poll_interval)
    raise AuriXMediaCLIError(f"Video {video_id} did not finish after {max_polls} polls")


def _download_openai_video(
    *,
    base_url: str,
    token: str,
    video_id: str,
    output: Path,
    timeout: float,
) -> Path:
    content_url = urljoin(base_url.rstrip("/") + "/", f"v1/videos/{video_id}/content")
    raw, _content_type = _request_absolute(
        url=content_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "video/mp4, application/octet-stream"},
        method="GET",
        timeout=timeout,
    )
    output.write_bytes(raw)
    return output


def _download_aurix_video(
    *,
    base_url: str,
    token: str,
    video_id: str,
    output: Path,
    timeout: float,
) -> Path:
    raw, _content_type = _request(
        base_url=base_url,
        token=token,
        path=f"/v1/videos/{video_id}/content",
        accept="video/mp4, application/octet-stream",
        timeout=timeout,
    )
    output.write_bytes(raw)
    return output


def _first_video_uri(payload: dict[str, Any]) -> str:
    response = payload.get("response")
    if not isinstance(response, dict):
        raise AuriXMediaCLIError("Gemini operation did not include a response")
    candidates = [
        ("generateVideoResponse", "generatedSamples"),
        ("generateVideoResponse", "generatedVideos"),
    ]
    for container_name, list_name in candidates:
        container = response.get(container_name)
        if not isinstance(container, dict):
            continue
        items = container.get(list_name)
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            continue
        video = items[0].get("video")
        if isinstance(video, dict) and isinstance(video.get("uri"), str):
            return video["uri"]
    generated_videos = response.get("generatedVideos")
    if isinstance(generated_videos, list) and generated_videos and isinstance(generated_videos[0], dict):
        video = generated_videos[0].get("video")
        if isinstance(video, dict) and isinstance(video.get("uri"), str):
            return video["uri"]
    raise AuriXMediaCLIError("Gemini operation did not include a downloadable video URI")


def _poll_gemini_operation(
    *,
    base_url: str,
    api_key: str,
    operation_name: str,
    timeout: float,
    poll_interval: float,
    max_polls: int,
) -> dict[str, Any]:
    operation_url = urljoin(base_url.rstrip("/") + "/", operation_name.lstrip("/"))
    headers = {"x-goog-api-key": api_key, "Accept": "application/json"}
    payload: dict[str, Any] = {"name": operation_name, "done": False}
    for _attempt in range(max(1, max_polls)):
        raw, _content_type = _request_absolute(
            url=operation_url,
            headers=headers,
            method="GET",
            timeout=timeout,
        )
        payload = _read_json(raw)
        if payload.get("done") is True:
            if isinstance(payload.get("error"), dict):
                message = payload["error"].get("message") or payload["error"]
                raise AuriXMediaCLIError(f"Gemini video generation failed: {message}")
            return payload
        if poll_interval > 0:
            import time

            time.sleep(poll_interval)
    raise AuriXMediaCLIError(f"Gemini operation {operation_name} did not finish after {max_polls} polls")


def _models(args: argparse.Namespace) -> int:
    raw, _content_type = _request(
        base_url=args.base_url,
        token=args.api_key,
        path="/v1/models",
        timeout=args.timeout,
    )
    payload = _read_json(raw)
    if args.capability:
        data = payload.get("data")
        if isinstance(data, list):
            payload["data"] = [
                item
                for item in data
                if isinstance(item, dict) and args.capability in item.get("capabilities", [])
            ]
    _write_json(payload)
    return 0


def _image(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "model": args.model,
        "prompt": args.prompt,
    }
    for name in (
        "n",
        "size",
        "quality",
        "style",
        "response_format",
        "output_format",
        "background",
        "aspect_ratio",
        "image_detail",
        "user_id",
        "conversation_id",
    ):
        value = getattr(args, name)
        if value is not None:
            body[name] = value
    if args.image:
        body["image"] = args.image
    if args.reference_image:
        body["images"] = args.reference_image

    path = "/v1/images/generations"
    if args.binary:
        path += "?response_format=binary"
    raw, content_type = _request(
        base_url=args.base_url,
        token=args.api_key,
        path=path,
        payload=body,
        accept="image/*, application/octet-stream, application/json" if args.binary else "application/json",
        timeout=args.timeout,
    )
    if args.binary:
        output = args.output
        if output is None:
            output = Path("aurix-image" + _extension_for_content_type(content_type))
        output.write_bytes(raw)
        print(str(output))
        return 0

    payload = _read_json(raw)
    if args.output is None:
        _write_json(payload)
        return 0
    saved = _save_json_image(payload, args.output)
    print(str(saved))
    return 0


def _video(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "model": args.model,
        "prompt": args.prompt,
    }
    for name in ("seconds", "size", "input_reference", "user_id", "conversation_id"):
        value = getattr(args, name)
        if value is not None:
            body[name] = value
    raw, _content_type = _request(
        base_url=args.base_url,
        token=args.api_key,
        path="/v1/videos",
        payload=body,
        timeout=args.timeout,
    )
    created = _read_json(raw)
    video_id = created.get("id")
    if not isinstance(video_id, str) or not video_id:
        raise AuriXMediaCLIError("AuriX video response did not include an id")
    if args.no_wait:
        _write_json(created)
        return 0
    final = _poll_aurix_video(
        base_url=args.base_url,
        token=args.api_key,
        video_id=video_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )
    if final.get("status") != "completed":
        _write_json(final)
        return 1
    output = args.output or Path(f"{video_id}.mp4")
    print(str(_download_aurix_video(
        base_url=args.base_url,
        token=args.api_key,
        video_id=video_id,
        output=output,
        timeout=args.timeout,
    )))
    return 0


def _openai_video(args: argparse.Namespace) -> int:
    token = args.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
    if not token:
        raise AuriXMediaCLIError("--openai-api-key or OPENAI_API_KEY is required")
    fields = {
        "model": args.model,
        "prompt": args.prompt,
    }
    for name in ("seconds", "size"):
        value = getattr(args, name)
        if value is not None:
            fields[name] = str(value)
    files = {}
    if args.input_reference is not None:
        files["input_reference"] = args.input_reference
    body, content_type = _multipart_form(fields, files)
    raw, _response_content_type = _request_absolute(
        url=urljoin(args.openai_base_url.rstrip("/") + "/", "v1/videos"),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": content_type,
        },
        data=body,
        timeout=args.timeout,
    )
    created = _read_json(raw)
    video_id = created.get("id")
    if not isinstance(video_id, str) or not video_id:
        raise AuriXMediaCLIError("OpenAI video response did not include an id")
    if args.no_wait:
        _write_json(created)
        return 0
    final = _poll_openai_video(
        base_url=args.openai_base_url,
        token=token,
        video_id=video_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )
    if final.get("status") != "completed":
        _write_json(final)
        return 1
    output = args.output or Path(f"{video_id}.mp4")
    print(str(_download_openai_video(
        base_url=args.openai_base_url,
        token=token,
        video_id=video_id,
        output=output,
        timeout=args.timeout,
    )))
    return 0


def _gemini_veo(args: argparse.Namespace) -> int:
    api_key = args.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise AuriXMediaCLIError("--gemini-api-key or GEMINI_API_KEY is required")
    parameters: dict[str, Any] = {}
    for cli_name, api_name in (
        ("aspect_ratio", "aspectRatio"),
        ("resolution", "resolution"),
        ("duration_seconds", "durationSeconds"),
    ):
        value = getattr(args, cli_name)
        if value is not None:
            parameters[api_name] = value
    request_payload: dict[str, Any] = {
        "instances": [{"prompt": args.prompt}],
    }
    if parameters:
        request_payload["parameters"] = parameters
    url = urljoin(
        args.gemini_base_url.rstrip("/") + "/",
        f"models/{args.model}:predictLongRunning",
    )
    raw, _content_type = _request_absolute(
        url=url,
        headers={"x-goog-api-key": api_key, "Accept": "application/json"},
        payload=request_payload,
        timeout=args.timeout,
    )
    operation = _read_json(raw)
    operation_name = operation.get("name")
    if not isinstance(operation_name, str) or not operation_name:
        raise AuriXMediaCLIError("Gemini response did not include an operation name")
    if args.no_wait:
        _write_json(operation)
        return 0
    final = _poll_gemini_operation(
        base_url=args.gemini_base_url,
        api_key=api_key,
        operation_name=operation_name,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )
    video_uri = _first_video_uri(final)
    raw_video, _video_content_type = _request_absolute(
        url=video_uri,
        headers={"x-goog-api-key": api_key, "Accept": "video/mp4, application/octet-stream"},
        method="GET",
        timeout=args.timeout,
    )
    output = args.output or Path("gemini-veo.mp4")
    output.write_bytes(raw_video)
    print(str(output))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("AURIX_AI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key", default=os.environ.get("AURIX_AI_API_KEY", ""))
    parser.add_argument("--timeout", type=float, default=180)
    commands = parser.add_subparsers(dest="command", required=True)

    models = commands.add_parser("models", help="List account-visible models")
    models.add_argument("--capability", help="Filter by capability, such as image_generation")
    models.set_defaults(func=_models)

    image = commands.add_parser("image", help="Generate an image")
    image.add_argument("--model", required=True)
    image.add_argument("--prompt", required=True)
    image.add_argument("--output", type=Path)
    image.add_argument("--binary", action="store_true", help="Request raw image bytes")
    image.add_argument("--n", type=int)
    image.add_argument("--size")
    image.add_argument("--quality")
    image.add_argument("--style")
    image.add_argument("--response-format", choices=("url", "b64_json"), default="b64_json")
    image.add_argument("--output-format")
    image.add_argument("--background")
    image.add_argument("--aspect-ratio")
    image.add_argument("--image-detail")
    image.add_argument("--image", help="Primary HTTPS or base64 data URL image input")
    image.add_argument(
        "--reference-image",
        action="append",
        help="Additional HTTPS or base64 data URL reference image; may be passed up to four times",
    )
    image.add_argument("--user-id")
    image.add_argument("--conversation-id")
    image.set_defaults(func=_image)

    video = commands.add_parser("video", help="Generate video through AuriX /v1/videos")
    video.add_argument("--model", required=True)
    video.add_argument("--prompt", required=True)
    video.add_argument("--seconds", choices=("4", "8", "12"))
    video.add_argument("--size", choices=("720x1280", "1280x720", "1024x1792", "1792x1024"))
    video.add_argument("--input-reference", help="HTTPS or base64 data URL image reference")
    video.add_argument("--output", type=Path)
    video.add_argument("--user-id")
    video.add_argument("--conversation-id")
    video.add_argument("--poll-interval", type=float, default=10)
    video.add_argument("--max-polls", type=int, default=120)
    video.add_argument("--no-wait", action="store_true")
    video.set_defaults(func=_video)

    openai_video = commands.add_parser("openai-video", help="Generate video through OpenAI /v1/videos")
    openai_video.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL))
    openai_video.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY", ""))
    openai_video.add_argument("--model", default="sora-2")
    openai_video.add_argument("--prompt", required=True)
    openai_video.add_argument("--seconds", choices=("4", "8", "12"))
    openai_video.add_argument("--size", choices=("720x1280", "1280x720", "1024x1792", "1792x1024"))
    openai_video.add_argument("--input-reference", type=Path)
    openai_video.add_argument("--output", type=Path)
    openai_video.add_argument("--poll-interval", type=float, default=10)
    openai_video.add_argument("--max-polls", type=int, default=120)
    openai_video.add_argument("--no-wait", action="store_true")
    openai_video.set_defaults(func=_openai_video)

    gemini_veo = commands.add_parser("gemini-veo", help="Generate video through Gemini API Veo")
    gemini_veo.add_argument("--gemini-base-url", default=os.environ.get("GEMINI_BASE_URL", DEFAULT_GEMINI_BASE_URL))
    gemini_veo.add_argument("--gemini-api-key", default=os.environ.get("GEMINI_API_KEY", ""))
    gemini_veo.add_argument("--model", default="veo-3.1-generate-preview")
    gemini_veo.add_argument("--prompt", required=True)
    gemini_veo.add_argument("--aspect-ratio", choices=("16:9", "9:16"))
    gemini_veo.add_argument("--resolution", choices=("720p", "1080p", "4k"))
    gemini_veo.add_argument("--duration-seconds")
    gemini_veo.add_argument("--output", type=Path)
    gemini_veo.add_argument("--poll-interval", type=float, default=10)
    gemini_veo.add_argument("--max-polls", type=int, default=120)
    gemini_veo.add_argument("--no-wait", action="store_true")
    gemini_veo.set_defaults(func=_gemini_veo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command in {"models", "image", "video"} and not args.api_key:
        parser.error("--api-key or AURIX_AI_API_KEY is required")
    try:
        return args.func(args)
    except AuriXMediaCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
