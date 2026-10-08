#!/usr/bin/env python3
"""Small local image-generation API backed by a ComfyUI API workflow.

Uses only the Python standard library and the existing comfyui_client helpers.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from comfyui_client import (
    FALLBACK_STATUSES,
    PROJECT_ROOT,
    HttpStatusError,
    V2SubmissionError,
    get_config,
    load_env_file,
    load_workflow,
    request_json,
    run_classic,
    run_v2,
)


WORKFLOW_PATH = PROJECT_ROOT / "workflows" / "txt2img" / "qwen-image-2.1.json"
MODEL_ID = "qwen-image-2.1"
MAX_REQUEST_BYTES = 1_048_576
SCHEMA_CACHE_SECONDS = 300.0

# Every public field maps to an existing literal input in the inspected API workflow.
# Inputs represented by links (model/conditioning/latent/image tensors) stay internal.
FIELD_MAP: dict[str, tuple[str, str]] = {
    "prompt": ("459:452", "prompt"),
    "negative_prompt": ("459:452", "negative_prompt"),
    "resolution": ("459:452", "resolution"),
    "aspect_ratio": ("13", "aspect_ratio"),
    "megapixels": ("13", "megapixels"),
    "multiple": ("13", "multiple"),
    "batch_size": ("459:456", "batch_size"),
    "seed": ("459:458", "seed"),
    "steps": ("459:458", "steps"),
    "cfg": ("459:458", "cfg"),
    "sampler_name": ("459:458", "sampler_name"),
    "scheduler": ("459:458", "scheduler"),
    "denoise": ("459:458", "denoise"),
    "unet_name": ("459:451", "unet_name"),
    "weight_dtype": ("459:451", "weight_dtype"),
    "clip_name": ("459:453", "clip_name"),
    "clip_type": ("459:453", "type"),
    "clip_device": ("459:453", "device"),
    "vae_name": ("459:454", "vae_name"),
    "filename_prefix": ("461", "filename_prefix"),
    "format": ("461", "format"),
    "bit_depth": ("461", "format.bit_depth"),
    "input_color_space": ("461", "format.input_color_space"),
}
REQUEST_FIELDS = set(FIELD_MAP) | {"model", "n", "response_format"}
SCHEMA_NODE_TYPES = (
    "ResolutionSelector",
    "TextEncodeQwenImage21",
    "UNETLoader",
    "CLIPLoader",
    "VAELoader",
    "EmptyLatentImage",
    "KSampler",
    "SaveImageAdvanced",
)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class SchemaCache:
    comfy_config: dict[str, Any]
    ttl: float = SCHEMA_CACHE_SECONDS
    expires_at: float = 0.0
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            if self.values and time.monotonic() < self.expires_at:
                return self.values
            refreshed: dict[str, dict[str, Any]] = {}
            base_url = self.comfy_config["classic_url"]
            for node_type in SCHEMA_NODE_TYPES:
                payload = request_json(
                    f"{base_url}/object_info/{urllib.parse.quote(node_type, safe='')}",
                    timeout=15,
                    token=self.comfy_config["token"],
                    auth_base=base_url,
                )
                spec = payload.get(node_type) if isinstance(payload, dict) else None
                if not isinstance(spec, dict):
                    raise RuntimeError("ComfyUI returned an invalid node definition")
                refreshed[node_type] = spec
            self.values = refreshed
            self.expires_at = time.monotonic() + self.ttl
            return self.values


@dataclass
class APIState:
    workflow: dict[str, Any]
    comfy_config: dict[str, Any]
    token: str | None
    public_url: str
    schemas: SchemaCache
    generation_lock: threading.Lock = field(default_factory=threading.Lock)


def _is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _workflow_node(workflow: dict[str, Any], node_id: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        raise ValueError(f"Workflow contract is missing node {node_id}")
    return node


def _validate_workflow_contract(workflow: dict[str, Any]) -> None:
    expected_classes = {
        "13": "ResolutionSelector",
        "461": "SaveImageAdvanced",
        "459:451": "UNETLoader",
        "459:452": "TextEncodeQwenImage21",
        "459:453": "CLIPLoader",
        "459:454": "VAELoader",
        "459:456": "EmptyLatentImage",
        "459:457": "VAEDecode",
        "459:458": "KSampler",
    }
    for node_id, expected_class in expected_classes.items():
        node = _workflow_node(workflow, node_id)
        if node.get("class_type") != expected_class:
            raise ValueError(f"Workflow contract mismatch at node {node_id}")
    for public_name, (node_id, input_name) in FIELD_MAP.items():
        if input_name not in _workflow_node(workflow, node_id)["inputs"]:
            raise ValueError(f"Workflow contract is missing input for {public_name}")


def _node_input_spec(
    schemas: dict[str, dict[str, Any]],
    workflow: dict[str, Any],
    public_name: str,
) -> Any:
    node_id, input_name = FIELD_MAP[public_name]
    node_type = workflow[node_id]["class_type"]
    node_schema = schemas[node_type]
    root_name, separator, nested_name = input_name.partition(".")
    input_groups = node_schema.get("input", {})
    root_spec = None
    for group in ("required", "optional"):
        group_values = input_groups.get(group, {})
        if root_name in group_values:
            root_spec = group_values[root_name]
            break
    if root_spec is None:
        raise APIError(502, "workflow_schema_mismatch", "A workflow input is not available on ComfyUI.")
    if not separator:
        return root_spec

    if not isinstance(root_spec, list) or len(root_spec) < 2 or not isinstance(root_spec[1], dict):
        raise APIError(502, "workflow_schema_mismatch", "A nested workflow input has no ComfyUI schema.")
    selected_key = workflow[node_id]["inputs"].get(root_name)
    option = next(
        (
            item
            for item in root_spec[1].get("options", [])
            if isinstance(item, dict) and item.get("key") == selected_key
        ),
        None,
    )
    if option is None:
        raise APIError(400, "invalid_parameter", f"Select a valid format before setting {public_name}.")
    child_spec = option.get("inputs", {}).get("required", {}).get(nested_name)
    if child_spec is None:
        raise APIError(400, "invalid_parameter", f"{public_name} is not available for the selected format.")
    return child_spec


def _schema_choices(spec: Any) -> list[Any] | None:
    if not isinstance(spec, list) or not spec:
        return None
    kind = spec[0]
    if isinstance(kind, list):
        return kind
    if len(spec) > 1 and isinstance(spec[1], dict):
        if kind == "COMBO":
            values = spec[1].get("options")
            return values if isinstance(values, list) else None
        if kind == "COMFY_DYNAMICCOMBO_V3":
            return [
                item.get("key")
                for item in spec[1].get("options", [])
                if isinstance(item, dict) and isinstance(item.get("key"), str)
            ]
    return None


def _schema_bounds(spec: Any) -> tuple[float | int | None, float | int | None]:
    if not isinstance(spec, list) or len(spec) < 2 or not isinstance(spec[1], dict):
        return None, None
    return spec[1].get("min"), spec[1].get("max")


def _coerce_parameter(
    public_name: str,
    value: Any,
    spec: Any,
    workflow: dict[str, Any],
) -> Any:
    if public_name in {"prompt", "negative_prompt"}:
        if not isinstance(value, str):
            raise APIError(400, "invalid_parameter", f"{public_name} must be a string.")
        if public_name == "prompt" and not value.strip():
            raise APIError(400, "invalid_parameter", "prompt must not be empty.")
        return value

    choices = _schema_choices(spec)
    if choices is not None:
        if value not in choices:
            raise APIError(400, "invalid_parameter", f"{public_name} is not an available ComfyUI option.")
        return value

    if not isinstance(spec, list) or not spec:
        raise APIError(502, "workflow_schema_mismatch", f"ComfyUI did not describe {public_name}.")
    kind = spec[0]
    if kind == "INT":
        if isinstance(value, bool) or not isinstance(value, int):
            raise APIError(400, "invalid_parameter", f"{public_name} must be an integer.")
        minimum, maximum = _schema_bounds(spec)
        if minimum is not None and value < minimum or maximum is not None and value > maximum:
            raise APIError(400, "invalid_parameter", f"{public_name} is outside ComfyUI's supported range.")
        return value
    if kind == "FLOAT":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise APIError(400, "invalid_parameter", f"{public_name} must be a finite number.")
        number = float(value)
        minimum, maximum = _schema_bounds(spec)
        if minimum is not None and number < minimum or maximum is not None and number > maximum:
            raise APIError(400, "invalid_parameter", f"{public_name} is outside ComfyUI's supported range.")
        return number
    if kind == "STRING":
        if not isinstance(value, str):
            raise APIError(400, "invalid_parameter", f"{public_name} must be a string.")
        return value
    raise APIError(502, "workflow_schema_mismatch", f"ComfyUI input type for {public_name} is unsupported.")


def _validate_filename_prefix(value: Any) -> str:
    if not isinstance(value, str):
        raise APIError(400, "invalid_parameter", "filename_prefix must be a string.")
    value = value.strip()
    if not value or len(value) > 128:
        raise APIError(400, "invalid_parameter", "filename_prefix must contain 1 to 128 characters.")
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(ch) < 32 for ch in value)
        or any(ch in value for ch in '<>:"|?*')
        or value.endswith((" ", "."))
    ):
        raise APIError(400, "invalid_parameter", "filename_prefix cannot contain a path or control characters.")
    device_name = value.split(".", 1)[0].rstrip(" .").upper()
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", device_name):
        raise APIError(400, "invalid_parameter", "filename_prefix is a reserved Windows device name.")
    return value


def _apply_request(
    workflow: dict[str, Any],
    payload: dict[str, Any],
    schemas: dict[str, dict[str, Any]],
) -> tuple[int, str]:
    if "prompt" not in payload:
        raise APIError(400, "missing_prompt", "prompt is required.")
    unknown = sorted(set(payload) - REQUEST_FIELDS)
    if unknown:
        raise APIError(
            400,
            "unknown_parameter",
            "Unsupported request field(s): " + ", ".join(unknown),
        )

    model = payload.get("model", MODEL_ID)
    if model != MODEL_ID:
        raise APIError(400, "unknown_model", f"This endpoint supports only {MODEL_ID}.")

    response_format = payload.get("response_format", "b64_json")
    if not isinstance(response_format, str) or response_format not in {"b64_json", "url"}:
        raise APIError(400, "invalid_parameter", "response_format must be b64_json or url.")

    if "n" in payload and "batch_size" in payload:
        raise APIError(400, "invalid_parameter", "Use either n or batch_size, not both.")

    values = {key: value for key, value in payload.items() if key in FIELD_MAP}
    if "n" in payload:
        values["batch_size"] = payload["n"]

    # Select the output format before validating its format-specific options.
    ordered_names = sorted(values, key=lambda key: (key != "format", key))
    for public_name in ordered_names:
        node_id, input_name = FIELD_MAP[public_name]
        if public_name == "seed" and values[public_name] == -1:
            value = secrets.randbits(64)
            spec = _node_input_spec(schemas, workflow, public_name)
        elif public_name == "filename_prefix":
            value = _validate_filename_prefix(values[public_name])
            spec = None
        else:
            value = values[public_name]
            spec = _node_input_spec(schemas, workflow, public_name)
        if spec is not None:
            value = _coerce_parameter(public_name, value, spec, workflow)
        workflow[node_id]["inputs"][input_name] = value

    seed = workflow["459:458"]["inputs"]["seed"]
    return seed, response_format


def _submit_and_wait(workflow: dict[str, Any], comfy_config: dict[str, Any]) -> dict[str, Any]:
    mode = comfy_config["api"]
    if mode == "classic":
        return run_classic(workflow, comfy_config)
    try:
        return run_v2(workflow, comfy_config)
    except V2SubmissionError as exc:
        if mode != "auto" or exc.status not in FALLBACK_STATUSES:
            raise
        result = run_classic(workflow, comfy_config)
        result["v2_fallback"] = f"HTTP {exc.status} from v2 submission"
        return result


def _safe_output_path(raw_path: str, output_dir: Path) -> Path:
    root = output_dir.resolve()
    candidate = Path(raw_path).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise APIError(502, "invalid_output", "ComfyUI returned an output outside the configured output folder.") from None
    if not candidate.is_file():
        raise APIError(502, "missing_output", "A generated image file could not be found.")
    return candidate


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length")
    try:
        length = int(raw_length or "")
    except ValueError:
        raise APIError(411, "content_length_required", "Send a JSON body with Content-Length.") from None
    if length < 0:
        raise APIError(400, "invalid_request", "Content-Length cannot be negative.")
    if length > MAX_REQUEST_BYTES:
        raise APIError(413, "request_too_large", "The request body exceeds the configured limit.")
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise APIError(415, "unsupported_media_type", "Content-Type must be application/json.")
    try:
        value = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise APIError(400, "invalid_json", "The request body must be valid UTF-8 JSON.") from None
    if not isinstance(value, dict):
        raise APIError(400, "invalid_request", "The JSON request body must be an object.")
    return value


class ImageAPIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: APIState) -> None:
        super().__init__(address, ImageAPIHandler)
        self.state = state


class ImageAPIHandler(BaseHTTPRequestHandler):
    server: ImageAPIServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Do not log URLs, prompt bodies, auth headers, or output filenames.
        route = urllib.parse.urlsplit(self.path).path
        if route.startswith("/v1/images/"):
            route = "/v1/images/{filename}"
        sys.stdout.write(f"{self.log_date_time_string()} {self.command} {route}\n")
        sys.stdout.flush()

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, error: APIError) -> None:
        self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

    def _authorized(self) -> bool:
        token = self.server.state.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        match = re.fullmatch(r"(?i)Bearer\s+(.+)", header.strip())
        return bool(match and hmac.compare_digest(match.group(1), token))

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self.close_connection = True
        self._send_json(401, {"error": {"code": "unauthorized", "message": "A valid Bearer token is required."}})
        return False

    def do_GET(self) -> None:
        route = urllib.parse.urlsplit(self.path).path
        if route == "/health":
            self._send_json(200, {"status": "ok", "service": "comfyui-image-api"})
            return
        if not self._require_auth():
            return
        if route == "/v1/models":
            self._send_json(
                200,
                {"object": "list", "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]},
            )
            return
        if route.startswith("/v1/images/"):
            self._serve_image(route.removeprefix("/v1/images/"))
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "Route not found."}})

    def _serve_image(self, encoded_name: str) -> None:
        name = urllib.parse.unquote(encoded_name)
        if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
            self._send_json(404, {"error": {"code": "not_found", "message": "Image not found."}})
            return
        try:
            path = _safe_output_path(
                str(self.server.state.comfy_config["output_dir"] / name),
                self.server.state.comfy_config["output_dir"],
            )
            content = path.read_bytes()
        except (OSError, APIError):
            self._send_json(404, {"error": {"code": "not_found", "message": "Image not found."}})
            return
        content_type = "application/octet-stream"
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}:
            content_type = mimetypes.guess_type(path.name)[0] or content_type
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        route = urllib.parse.urlsplit(self.path).path
        if route != "/v1/images/generations":
            self._send_json(404, {"error": {"code": "not_found", "message": "Route not found."}})
            return
        if not self._require_auth():
            return
        try:
            payload = _read_json_body(self)
            if not self.server.state.generation_lock.acquire(blocking=False):
                raise APIError(429, "generation_in_progress", "Another API generation request is still running.")
            try:
                self._generate(payload)
            finally:
                self.server.state.generation_lock.release()
        except APIError as exc:
            self._send_error(exc)
        except Exception as exc:
            # Keep unexpected implementation or dependency details out of HTTP responses.
            sys.stderr.write(f"Image API request failed: {type(exc).__name__}\n")
            self._send_json(
                500,
                {"error": {"code": "internal_error", "message": "The image API could not complete the request."}},
            )

    def _generate(self, payload: dict[str, Any]) -> None:
        workflow = copy.deepcopy(self.server.state.workflow)
        try:
            schemas = self.server.state.schemas.get()
            seed, response_format = _apply_request(workflow, payload, schemas)
            if response_format == "url" and not self.server.state.public_url:
                raise APIError(
                    400,
                    "public_url_required",
                    "Set COMFYUI_IMAGE_API_PUBLIC_URL before requesting URL responses on this bind address.",
                )
            result = _submit_and_wait(workflow, self.server.state.comfy_config)
            output_paths = result.get("outputs", [])
            if not isinstance(output_paths, list) or not output_paths:
                raise APIError(502, "empty_output", "ComfyUI completed without returning image files.")

            data = []
            output_dir = self.server.state.comfy_config["output_dir"]
            for raw_path in output_paths:
                path = _safe_output_path(str(raw_path), output_dir)
                item: dict[str, Any] = {"filename": path.name}
                if response_format == "b64_json":
                    item["b64_json"] = base64.b64encode(path.read_bytes()).decode("ascii")
                else:
                    item["url"] = (
                        self.server.state.public_url
                        + "/v1/images/"
                        + urllib.parse.quote(path.name, safe="")
                    )
                data.append(item)

            response: dict[str, Any] = {
                "created": int(time.time()),
                "model": MODEL_ID,
                "seed": seed,
                "data": data,
            }
            job_id = result.get("prompt_id") or result.get("job_id")
            if job_id:
                response["id"] = str(job_id)
            self._send_json(200, response)
        except APIError:
            raise
        except TimeoutError:
            self._send_json(
                504,
                {
                    "error": {
                        "code": "generation_timeout",
                        "message": "Waiting timed out. Check the ComfyUI queue before retrying to avoid duplicate work.",
                    }
                },
            )
        except HttpStatusError as exc:
            status = 422 if exc.status in {400, 422} else 502
            self._send_json(
                status,
                {
                    "error": {
                        "code": "comfyui_rejected_workflow" if status == 422 else "comfyui_http_error",
                        "message": f"ComfyUI returned HTTP {exc.status}.",
                    }
                },
            )
        except (ConnectionError, OSError):
            self._send_json(
                503,
                {"error": {"code": "comfyui_unavailable", "message": "Could not reach ComfyUI or retrieve its output."}},
            )
        except RuntimeError:
            self._send_json(
                502,
                {"error": {"code": "generation_failed", "message": "ComfyUI did not complete the generation successfully."}},
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local Qwen Image 2.1 ComfyUI generation API")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / "config" / "comfyui.env",
        help="Optional KEY=VALUE file (default: config/comfyui.env)",
    )
    parser.add_argument("--host", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Listen port (default: 8190)")
    parser.add_argument("--api", choices=("auto", "v2", "classic"), help="ComfyUI API mode override")
    parser.add_argument("--poll-interval", type=float, help="ComfyUI history polling interval in seconds")
    parser.add_argument("--wait-timeout", type=float, help="Maximum wait time for one generation in seconds")
    return parser.parse_args()


def _build_state(args: argparse.Namespace) -> tuple[APIState, str, int]:
    load_env_file(args.env_file.expanduser())
    comfy_config = get_config(args)
    workflow = load_workflow(WORKFLOW_PATH)
    _validate_workflow_contract(workflow)

    host = (args.host or os.getenv("COMFYUI_IMAGE_API_HOST", "127.0.0.1")).strip()
    if not host:
        host = "127.0.0.1"
    port = args.port if args.port is not None else int(os.getenv("COMFYUI_IMAGE_API_PORT", "8190"))
    if not 1 <= port <= 65535:
        raise ValueError("COMFYUI_IMAGE_API_PORT must be between 1 and 65535")
    try:
        if ":" in host and host != "localhost":
            raise ValueError("Use an IPv4 bind address such as 127.0.0.1 or 0.0.0.0")
        ipaddress.ip_address(host) if host != "localhost" else None
    except ValueError as exc:
        if "Use an IPv4" in str(exc):
            raise
        raise ValueError("COMFYUI_IMAGE_API_HOST must be localhost or an IPv4 address") from None

    token = os.getenv("COMFYUI_IMAGE_API_TOKEN", "").strip() or None
    if not _is_loopback(host) and not token:
        raise ValueError("Set COMFYUI_IMAGE_API_TOKEN before binding the image API to a non-loopback address")
    public_url = os.getenv("COMFYUI_IMAGE_API_PUBLIC_URL", "").strip().rstrip("/")
    if public_url:
        parsed_public_url = urllib.parse.urlsplit(public_url)
        if (
            parsed_public_url.scheme not in {"http", "https"}
            or not parsed_public_url.netloc
            or parsed_public_url.username is not None
            or parsed_public_url.password is not None
            or parsed_public_url.path not in {"", "/"}
            or parsed_public_url.query
            or parsed_public_url.fragment
        ):
            raise ValueError("COMFYUI_IMAGE_API_PUBLIC_URL must be an HTTP(S) origin without credentials, path, query, or fragment")
    if not public_url:
        if _is_loopback(host):
            public_url = f"http://127.0.0.1:{port}"
        elif host != "0.0.0.0":
            public_url = f"http://{host}:{port}"
    state = APIState(
        workflow=workflow,
        comfy_config=comfy_config,
        token=token,
        public_url=public_url,
        schemas=SchemaCache(comfy_config),
    )
    return state, host, port


def main() -> int:
    args = _parse_args()
    try:
        state, host, port = _build_state(args)
        with ImageAPIServer((host, port), state) as server:
            print(f"ComfyUI Image API listening on http://{host}:{port}")
            print(f"Workflow: {WORKFLOW_PATH}")
            print("POST /v1/images/generations | GET /v1/models | GET /health")
            server.serve_forever()
    except (HttpStatusError, ConnectionError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ComfyUI Image API stopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
