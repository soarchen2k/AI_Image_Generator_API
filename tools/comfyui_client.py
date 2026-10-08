#!/usr/bin/env python3
"""Standard-library ComfyUI Classic API and Comfy API v2 client."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLASSIC_URL = "http://192.168.3.75:8188"
# 生成结果默认写入运行命令时的当前工作目录；每个新主题用 COMFYUI_OUTPUT_DIR 指向该目录下的主题子目录。
DEFAULT_OUTPUT_DIR = Path.cwd()
FALLBACK_STATUSES = {404, 405}


class HttpStatusError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.url = url
        self.body = body
        detail = body.strip()
        if len(detail) > 2000:
            detail = detail[:2000] + "…"
        path = urllib.parse.urlsplit(url).path or url
        message = f"HTTP {status}: {path}"
        if detail:
            message += f"\n{detail}"
        super().__init__(message)


class V2SubmissionError(HttpStatusError):
    pass


class WorkflowFormatError(ValueError):
    pass


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without replacing existing process env vars."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return value


def get_config(args: argparse.Namespace) -> dict[str, Any]:
    classic_url = os.getenv("COMFYUI_URL", DEFAULT_CLASSIC_URL).strip().rstrip("/")
    if not classic_url:
        classic_url = DEFAULT_CLASSIC_URL
    v2_url = os.getenv("COMFYUI_V2_URL", "").strip().rstrip("/") or classic_url
    output_dir = Path(os.getenv("COMFYUI_OUTPUT_DIR", "").strip() or DEFAULT_OUTPUT_DIR).expanduser()
    api = (getattr(args, "api", None) or os.getenv("COMFYUI_API", "auto")).lower()
    if api not in {"auto", "v2", "classic"}:
        raise ValueError("API mode must be auto, v2, or classic")
    return {
        "classic_url": classic_url,
        "v2_url": v2_url,
        "token": os.getenv("COMFYUI_API_TOKEN", "").strip() or None,
        "output_dir": output_dir,
        "api": api,
        "poll_interval": getattr(args, "poll_interval", None) or _positive_float("COMFYUI_POLL_INTERVAL", 2.0),
        "wait_timeout": getattr(args, "wait_timeout", None) or _positive_float("COMFYUI_WAIT_TIMEOUT", 1800.0),
    }


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, host, port


def _auth_headers(url: str, auth_base: str | None, token: str | None) -> dict[str, str]:
    if token and auth_base and _origin(url) == _origin(auth_base):
        return {"Authorization": f"Bearer {token}"}
    return {}


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Remove authorization when a redirect leaves the original origin."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> urllib.request.Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            redirected.remove_header("Authorization")
            redirected.unredirected_hdrs.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("authorization", None)
        return redirected


def _open(request: urllib.request.Request, timeout: float) -> Any:
    opener = urllib.request.build_opener(SafeRedirectHandler())
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        authorization = request.get_header("Authorization")
        if authorization:
            for secret in (authorization, authorization.partition(" ")[2]):
                if secret:
                    body = body.replace(secret, "<redacted>")
        raise HttpStatusError(exc.code, request.full_url, body) from None
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        raise ConnectionError(f"Could not connect to {urllib.parse.urlsplit(request.full_url).netloc}: {reason}") from None


def request_json(url: str, method: str = "GET", payload: Any | None = None,
                 timeout: float = 30, token: str | None = None,
                 auth_base: str | None = None, extra_headers: dict[str, str] | None = None) -> Any:
    headers = {"Accept": "application/json", **_auth_headers(url, auth_base, token), **(extra_headers or {})}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _open(request, timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Expected JSON from {urllib.parse.urlsplit(url).path}") from exc


def download_url(url: str, destination: Path, timeout: float,
                 token: str | None = None, auth_base: str | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    headers = {"Accept": "*/*", **_auth_headers(url, auth_base, token)}
    request = urllib.request.Request(url, headers=headers, method="GET")
    with _open(request, timeout) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        workflow = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Workflow file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise WorkflowFormatError(f"Invalid JSON in {path}:{exc.lineno}:{exc.colno}: {exc.msg}") from None
    if not isinstance(workflow, dict):
        raise WorkflowFormatError("Workflow JSON must be an object")
    if "nodes" in workflow and "links" in workflow:
        raise WorkflowFormatError("This looks like the UI/save format. Export it with File → Export Workflow (API).")
    nodes = [value for value in workflow.values() if isinstance(value, dict) and "class_type" in value]
    if not nodes:
        raise WorkflowFormatError("No API-format nodes with class_type were found")
    return workflow


def _json_print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _get_mode_url(config: dict[str, Any], api: str) -> tuple[str, str]:
    if api == "classic":
        return config["classic_url"], "classic"
    return config["v2_url"], "v2"


def command_status(args: argparse.Namespace, config: dict[str, Any]) -> None:
    mode = config["api"]
    if mode == "classic":
        result = request_json(config["classic_url"] + "/system_stats", timeout=10,
                              token=config["token"], auth_base=config["classic_url"])
        _json_print({"api": "classic", "url": config["classic_url"], "status": "ok", "system": result})
        return
    if mode == "v2":
        result = request_json(config["v2_url"] + "/api/v2/health", timeout=10,
                              token=config["token"], auth_base=config["v2_url"])
        _json_print({"api": "v2", "url": config["v2_url"], "status": "ok", "health": result})
        return
    try:
        health = request_json(config["v2_url"] + "/api/v2/health", timeout=10,
                              token=config["token"], auth_base=config["v2_url"])
        _json_print({"api": "v2", "url": config["v2_url"], "status": "ok", "health": health})
    except HttpStatusError as exc:
        if exc.status not in FALLBACK_STATUSES:
            raise
        system = request_json(config["classic_url"] + "/system_stats", timeout=10,
                              token=config["token"], auth_base=config["classic_url"])
        _json_print({"api": "classic", "url": config["classic_url"],
                     "v2_probe": f"HTTP {exc.status}", "status": "ok", "system": system})


def command_inspect(args: argparse.Namespace, _config: dict[str, Any]) -> None:
    path = args.workflow.expanduser().resolve()
    workflow = load_workflow(path)
    result_nodes = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        meta = node.get("_meta", {})
        title = meta.get("title") if isinstance(meta, dict) else None
        result_nodes.append({
            "id": str(node_id),
            "class_type": node.get("class_type"),
            "title": title,
            "input_fields": list(inputs.keys()),
        })
    _json_print({"path": str(path), "format": "api", "node_count": len(result_nodes), "nodes": result_nodes})


def _safe_filename(value: Any, fallback: str, content_type: str | None = None) -> str:
    raw = str(value or "").replace("\\", "/").split("/")[-1].strip()
    raw = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw)
    raw = raw.strip(" .")
    if not raw or raw in {".", ".."}:
        raw = fallback
    if not Path(raw).suffix and content_type:
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if extension:
            raw += extension
    return raw


def _unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    stem, suffix = candidate.stem, candidate.suffix
    index = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _safe_error_detail(value: Any, token: str | None) -> str:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    return rendered.replace(token, "<redacted>") if token else rendered


def _v2_poll_job(job_id: str, config: dict[str, Any]) -> dict[str, Any]:
    url = config["v2_url"] + "/api/v2/jobs/" + urllib.parse.quote(job_id, safe="")
    deadline = time.monotonic() + config["wait_timeout"]
    while True:
        job = request_json(url, timeout=30, token=config["token"], auth_base=config["v2_url"])
        if not isinstance(job, dict):
            raise RuntimeError("Comfy API v2 returned an unexpected job response")
        status = str(job.get("status", "")).lower()
        if status in {"completed", "succeeded", "success"}:
            return job
        if status in {"failed", "error", "cancelled", "canceled"}:
            detail = _safe_error_detail(job.get("error") or job.get("message") or job, config["token"])
            raise RuntimeError(f"Comfy API v2 job {job_id} ended with status {status}: {detail}")
        if not status:
            raise RuntimeError("Comfy API v2 job response did not include status")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for Comfy API v2 job {job_id} after {config['wait_timeout']:g}s")
        time.sleep(config["poll_interval"])


def _v2_output_url(output: dict[str, Any], base_url: str) -> str | None:
    for key in ("download_url", "content_url", "url"):
        value = output.get(key)
        if isinstance(value, str) and value:
            return urllib.parse.urljoin(base_url.rstrip("/") + "/", value)
    for key in ("asset", "asset_info", "info"):
        nested = output.get(key)
        if isinstance(nested, dict):
            value = nested.get("url") or nested.get("download_url") or nested.get("content_url")
            if isinstance(value, str) and value:
                return urllib.parse.urljoin(base_url.rstrip("/") + "/", value)
    asset_id = output.get("asset_id")
    if not asset_id and isinstance(output.get("asset"), str):
        asset_id = output["asset"]
    if not asset_id and isinstance(output.get("asset"), dict):
        asset_id = output["asset"].get("id") or output["asset"].get("asset_id")
    if not asset_id and isinstance(output.get("asset_info"), dict):
        asset_id = output["asset_info"].get("id") or output["asset_info"].get("asset_id")
    if not asset_id:
        asset_id = output.get("id")
    if asset_id:
        quoted_id = urllib.parse.quote(str(asset_id), safe="")
        return base_url.rstrip("/") + f"/api/v2/assets/{quoted_id}/content"
    return None


def _download_v2_outputs(job: dict[str, Any], config: dict[str, Any]) -> list[str]:
    outputs = job.get("outputs", [])
    if not isinstance(outputs, list):
        raise RuntimeError("Comfy API v2 job outputs field is not a list")
    saved: list[str] = []
    for index, output in enumerate(outputs, start=1):
        if not isinstance(output, dict):
            continue
        url = _v2_output_url(output, config["v2_url"])
        if not url:
            raise RuntimeError(f"Could not find a content URL or asset ID for v2 output {index}")
        filename = output.get("name") or output.get("file_path") or output.get("filename")
        if not filename and isinstance(output.get("asset"), dict):
            filename = output["asset"].get("file_path") or output["asset"].get("name")
        content_type = output.get("content_type") or output.get("mime_type")
        path = _unique_path(config["output_dir"], _safe_filename(filename, f"output-{index:03d}", content_type))
        download_url(url, path, timeout=120, token=config["token"], auth_base=config["v2_url"])
        saved.append(str(path))
    return saved


def run_v2(workflow: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    endpoint = config["v2_url"] + "/api/v2/jobs"
    key = str(uuid.uuid4())
    try:
        job = request_json(endpoint, method="POST", payload={"workflow": workflow}, timeout=30,
                           token=config["token"], auth_base=config["v2_url"],
                           extra_headers={"Idempotency-Key": key})
    except HttpStatusError as exc:
        raise V2SubmissionError(exc.status, exc.url, exc.body) from None
    if not isinstance(job, dict):
        raise RuntimeError("Comfy API v2 returned an unexpected submission response")
    job_id = job.get("id") or job.get("job_id")
    if not job_id:
        raise RuntimeError("Comfy API v2 submission response did not include a job ID")
    completed = _v2_poll_job(str(job_id), config)
    files = _download_v2_outputs(completed, config)
    return {"api": "v2", "job_id": str(job_id), "status": completed.get("status"),
            "output_dir": str(config["output_dir"]), "outputs": files}


def _classic_history_item(prompt_id: str, config: dict[str, Any]) -> dict[str, Any] | None:
    url = config["classic_url"] + "/history/" + urllib.parse.quote(prompt_id, safe="")
    history = request_json(url, timeout=30, token=config["token"], auth_base=config["classic_url"])
    if not isinstance(history, dict):
        return None
    item = history.get(prompt_id)
    return item if isinstance(item, dict) else None


def _classic_is_failed(item: dict[str, Any]) -> bool:
    status = item.get("status", {})
    if not isinstance(status, dict):
        return False
    status_str = str(status.get("status_str", "")).lower()
    if status_str in {"error", "failed", "failure"}:
        return True
    messages = status.get("messages", [])
    if isinstance(messages, list):
        return any(isinstance(message, (list, tuple)) and message and str(message[0]).lower() == "execution_error"
                   for message in messages)
    return False


def _classic_is_complete(item: dict[str, Any]) -> bool:
    status = item.get("status", {})
    if not isinstance(status, dict):
        return False
    return bool(status.get("completed")) or str(status.get("status_str", "")).lower() == "success"


def _classic_poll(prompt_id: str, config: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + config["wait_timeout"]
    while True:
        item = _classic_history_item(prompt_id, config)
        if item is not None:
            if _classic_is_failed(item):
                detail = _safe_error_detail(item.get("status"), config["token"])
                raise RuntimeError(f"Classic API prompt {prompt_id} failed: {detail}")
            if _classic_is_complete(item):
                return item
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for Classic API prompt {prompt_id} after {config['wait_timeout']:g}s")
        time.sleep(config["poll_interval"])


def _download_classic_outputs(item: dict[str, Any], config: dict[str, Any]) -> list[str]:
    outputs = item.get("outputs", {})
    if not isinstance(outputs, dict):
        return []
    saved: list[str] = []
    index = 0
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for values in node_output.values():
            if not isinstance(values, list):
                continue
            for entry in values:
                if not isinstance(entry, dict) or not entry.get("filename"):
                    continue
                index += 1
                query = urllib.parse.urlencode({
                    "filename": entry["filename"],
                    "subfolder": entry.get("subfolder", ""),
                    "type": entry.get("type", "output"),
                })
                url = config["classic_url"] + "/view?" + query
                path = _unique_path(config["output_dir"], _safe_filename(entry["filename"], f"output-{index:03d}"))
                download_url(url, path, timeout=120, token=config["token"], auth_base=config["classic_url"])
                saved.append(str(path))
    return saved


def run_classic(workflow: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    response = request_json(config["classic_url"] + "/prompt", method="POST",
                            payload={"prompt": workflow, "client_id": str(uuid.uuid4())},
                            timeout=30, token=config["token"], auth_base=config["classic_url"])
    if not isinstance(response, dict) or not response.get("prompt_id"):
        detail = _safe_error_detail(response, config["token"])
        raise RuntimeError(f"Classic API did not return a prompt_id: {detail}")
    prompt_id = str(response["prompt_id"])
    completed = _classic_poll(prompt_id, config)
    files = _download_classic_outputs(completed, config)
    return {"api": "classic", "prompt_id": prompt_id,
            "output_dir": str(config["output_dir"]), "outputs": files}


def command_run(args: argparse.Namespace, config: dict[str, Any]) -> None:
    workflow_path = args.workflow.expanduser().resolve()
    workflow = load_workflow(workflow_path)
    mode = config["api"]
    if mode == "classic":
        _json_print(run_classic(workflow, config))
        return
    try:
        result = run_v2(workflow, config)
    except V2SubmissionError as exc:
        if mode != "auto" or exc.status not in FALLBACK_STATUSES:
            raise
        result = run_classic(workflow, config)
        result["v2_fallback"] = f"HTTP {exc.status} from the v2 job submission endpoint"
    _json_print(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call ComfyUI Classic API or Comfy API v2")
    parser.add_argument("--env-file", type=Path,
                        default=PROJECT_ROOT / "config" / "comfyui.env",
                        help="Optional KEY=VALUE configuration file (default: config/comfyui.env)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Check the configured ComfyUI API")
    status.add_argument("--api", choices=("auto", "v2", "classic"), help="API mode override")
    status.set_defaults(handler=command_status)

    inspect = subparsers.add_parser("inspect", help="Summarize an API Format workflow")
    inspect.add_argument("workflow", type=Path)
    inspect.set_defaults(handler=command_inspect)

    run = subparsers.add_parser("run", help="Submit a workflow and download its outputs")
    run.add_argument("workflow", type=Path)
    run.add_argument("--api", choices=("auto", "v2", "classic"), help="API mode override")
    run.add_argument("--poll-interval", type=float, help="Polling interval in seconds")
    run.add_argument("--wait-timeout", type=float, help="Maximum wait time in seconds")
    run.set_defaults(handler=command_run)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        load_env_file(args.env_file.expanduser())
        config = get_config(args)
        args.handler(args, config)
        return 0
    except (HttpStatusError, ConnectionError, FileNotFoundError, TimeoutError,
            WorkflowFormatError, ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
