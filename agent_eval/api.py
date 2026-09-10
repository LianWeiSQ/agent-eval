from __future__ import annotations

import json
import mimetypes
import re
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .common import EvalError, new_id
from .service import Actor, EvaluationService


STATIC_ROOT = Path(__file__).resolve().parent / "static"


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AgentEval/1.0"
    service: EvaluationService

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[evaluation-api] {self.address_string()} {format % args}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _actor(self) -> Actor:
        return Actor(
            tenant_id=self.headers.get("X-Tenant-ID") or "dev-tenant",
            project_id=self.headers.get("X-Project-ID") or "dev-project",
            actor_id=self.headers.get("X-Actor-ID") or "local-user",
            role=self.headers.get("X-Role") or "project_admin",
        )

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 5 * 1024 * 1024:
            raise EvalError("payload_too_large", "请求体不能超过 5 MB", status=413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvalError("invalid_json", f"请求 JSON 无法解析：{exc}") from exc
        if not isinstance(value, dict):
            raise EvalError("invalid_json", "请求体顶层必须是 JSON 对象")
        return value

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        request_id = self.headers.get("X-Request-ID") or new_id("req")
        try:
            if path == "/health":
                return self._json(200, {"status": "ok", "service": "evaluation-service", "schema_version": "1.0.0"}, request_id)
            if not path.startswith("/api/"):
                return self._static(path)
            actor = self._actor()
            body = self._body() if method == "POST" else {}
            data, status = self._route(method, path, query, body, actor)
            if isinstance(data, tuple) and len(data) == 3 and data[0] == "binary":
                _, media_type, content = data
                return self._binary(status, media_type, content)
            return self._json(status, data, request_id)
        except EvalError as exc:
            self._json(exc.status, None, request_id, error={"code": exc.code, "message": exc.message})
        except Exception as exc:
            self._json(500, None, request_id, error={"code": "internal_error", "message": str(exc)})

    def _route(self, method: str, path: str, query: dict[str, list[str]], body: dict[str, Any], actor: Actor) -> tuple[Any, int]:
        service = self.service
        if method == "GET" and path == "/api/v1/dashboard":
            return service.dashboard(actor), 200
        if method == "POST" and path == "/api/v1/bootstrap":
            return service.ensure_demo_data(actor), 200

        if method == "GET" and path == "/api/v1/terminal-bench/status":
            return service.terminal_bench_status(actor), 200
        if method == "POST" and path == "/api/v1/terminal-bench/setup":
            return service.prepare_terminal_bench(
                actor,
                model_profile=body.get("model_profile"),
                reasoning_effort=body.get("reasoning_effort"),
            ), 200
        if method == "POST" and path == "/api/v1/terminal-bench/evaluations":
            return service.start_terminal_bench_evaluation(actor, body), 202

        if path == "/api/v1/benchmarks":
            if method == "GET":
                return service.list_benchmarks(actor, status=_first(query, "status")), 200
            return service.create_benchmark(actor, body.get("package") or body, publish=bool(body.get("publish"))), 201
        if method == "POST" and path == "/api/v1/benchmarks/validate":
            return service.validate_benchmark_path(Path(str(body.get("path", ""))), actor), 200
        if method == "POST" and path == "/api/v1/benchmarks/import":
            return service.import_benchmark_path(actor, Path(str(body.get("path", ""))), publish=bool(body.get("publish"))), 201
        match = re.fullmatch(r"/api/v1/benchmarks/([^/]+)", path)
        if match and method == "GET":
            return service.get_benchmark(actor, match.group(1)), 200
        match = re.fullmatch(r"/api/v1/benchmarks/([^/]+)/(publish|archive)", path)
        if match and method == "POST":
            return (service.publish_benchmark(actor, match.group(1)) if match.group(2) == "publish" else service.archive_benchmark(actor, match.group(1))), 200
        match = re.fullmatch(r"/api/v1/benchmarks/([^/]+)/copy", path)
        if match and method == "POST":
            return service.copy_benchmark(actor, match.group(1), body), 201
        match = re.fullmatch(r"/api/v1/benchmarks/([^/]+)/export/(json|jsonl)", path)
        if match and method == "GET":
            media_type, content = service.export_benchmark(actor, match.group(1), match.group(2))
            return ("binary", media_type, content), 200

        if path == "/api/v1/agent-snapshots":
            return (service.list_snapshots(actor), 200) if method == "GET" else (service.create_snapshot(actor, body), 201)
        match = re.fullmatch(r"/api/v1/agent-snapshots/([^/]+)/health", path)
        if match and method in {"GET", "POST"}:
            return service.adapter_health(actor, match.group(1)), 200
        match = re.fullmatch(r"/api/v1/agent-snapshots/([^/]+)/try", path)
        if match and method == "POST":
            return service.try_agent(actor, match.group(1), body), 200

        if path == "/api/v1/graders":
            return (service.list_graders(actor), 200) if method == "GET" else (service.create_grader(actor, body), 201)

        if path == "/api/v1/jobs/estimate" and method == "POST":
            return service.estimate_job(actor, body), 200
        if path == "/api/v1/jobs":
            return (service.list_jobs(actor, status=_first(query, "status")), 200) if method == "GET" else (service.create_job(actor, body), 201)
        match = re.fullmatch(r"/api/v1/jobs/([^/]+)", path)
        if match and method == "GET":
            return service.get_job(actor, match.group(1), summary=_first(query, "view") == "summary"), 200
        match = re.fullmatch(r"/api/v1/jobs/([^/]+)/(start|pause|resume|cancel)", path)
        if match and method == "POST":
            operation = {"start": service.start_job, "pause": service.pause_job, "resume": service.resume_job, "cancel": service.cancel_job}[match.group(2)]
            return operation(actor, match.group(1)), 202
        match = re.fullmatch(r"/api/v1/jobs/([^/]+)/report", path)
        if match and method == "GET":
            return service.get_report(actor, match.group(1)), 200
        match = re.fullmatch(r"/api/v1/jobs/([^/]+)/export/(json|jsonl|csv|markdown)", path)
        if match and method == "GET":
            media_type, content = service.export_report(actor, match.group(1), match.group(2))
            return ("binary", media_type, content), 200

        if path == "/api/v1/trials" and method == "GET":
            return service.list_trials(actor, job_id=_first(query, "job_id"), outcome=_first(query, "outcome")), 200
        match = re.fullmatch(r"/api/v1/trials/([^/]+)/trace", path)
        if match and method == "GET":
            return service.get_trial_trace(actor, match.group(1), view=_first(query, "view") or "summary"), 200
        match = re.fullmatch(r"/api/v1/trials/([^/]+)/supervise", path)
        if match and method == "POST":
            return service.supervise_trial(actor, match.group(1), body), 201
        match = re.fullmatch(r"/api/v1/trials/([^/]+)", path)
        if match and method == "GET":
            return service.get_trial(actor, match.group(1)), 200
        match = re.fullmatch(r"/api/v1/trials/([^/]+)/(retry|regrade)", path)
        if match and method == "POST":
            return (service.retry_trial(actor, match.group(1)) if match.group(2) == "retry" else service.regrade_trial(actor, match.group(1))), 202

        if path == "/api/v1/reviews" and method == "GET":
            return service.list_reviews(actor, status=_first(query, "status")), 200
        match = re.fullmatch(r"/api/v1/reviews/([^/]+)/submit", path)
        if match and method == "POST":
            return service.submit_review(actor, match.group(1), body), 200

        if path == "/api/v1/supervisions" and method == "GET":
            return service.list_supervisions(actor, status=_first(query, "status")), 200
        match = re.fullmatch(r"/api/v1/supervisions/([^/]+)/decide", path)
        if match and method == "POST":
            return service.decide_correction(actor, match.group(1), body), 201

        match = re.fullmatch(r"/api/v1/artifacts/([^/]+)", path)
        if match and method == "GET":
            media_type, content = service.get_artifact_content(actor, match.group(1))
            return ("binary", media_type, content), 200
        if path == "/api/v1/audit" and method == "GET":
            return service.list_audit(actor, limit=int(_first(query, "limit") or 100)), 200
        raise EvalError("not_found", f"API 不存在：{method} {path}", status=404)

    def _static(self, path: str) -> None:
        relative = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT not in target.parents and target != STATIC_ROOT:
            raise EvalError("not_found", "页面不存在", status=404)
        if not target.is_file():
            target = STATIC_ROOT / "index.html"
        content = target.read_bytes()
        self._binary(200, mimetypes.guess_type(target.name)[0] or "application/octet-stream", content)

    def _json(self, status: int, data: Any, request_id: str, *, error: dict[str, Any] | None = None) -> None:
        content = json.dumps({"data": data, "error": error, "request_id": request_id}, ensure_ascii=False).encode("utf-8")
        self._binary(status, "application/json; charset=utf-8", content)

    def _binary(self, status: int, media_type: str, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(content)

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Tenant-ID, X-Project-ID, X-Request-ID, X-Actor-ID, X-Role")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


class EvaluationHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can bind a second server to an occupied port.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(service: EvaluationService, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    server = EvaluationHTTPServer((host, port), handler)
    print(f"Agent Evaluation Portal: http://{host}:{port}")
    print(f"Data directory: {service.data_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
