"""Opt-in loopback Laya worker; run in a separately provisioned model environment."""

import argparse
import hashlib
import hmac
import os
import re
from importlib.metadata import version
from pathlib import Path

from packages.providers.laya_artifact import (
    ARTIFACT_HEADER,
    WORKER_PID_HEADER,
    ArtifactError,
    verified_laya_checkpoint,
)


class ArtifactIdentityApp:
    """Bind authenticated responses to the verified worker snapshot."""

    def __init__(self, app, sha256, api_key):
        self.app = app
        self.identity = (ARTIFACT_HEADER.lower().encode(), sha256.encode())
        self.authorization = ("Bearer " + api_key).encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = [value for name, value in scope.get("headers", []) if name == b"authorization"]
        authorized = len(headers) == 1 and hmac.compare_digest(headers[0], self.authorization)

        async def identified(message):
            if message["type"] == "http.response.start" and authorized:
                message = dict(message)
                message["headers"] = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in {self.identity[0], WORKER_PID_HEADER.lower().encode()}
                ]
                if 200 <= message["status"] < 300:
                    message["headers"].append(self.identity)
                    message["headers"].append(
                        (WORKER_PID_HEADER.lower().encode(), str(os.getpid()).encode())
                    )
            await send(message)

        return await self.app(scope, receive, identified)


def build_app(checkpoint, *, device, threads, concurrency, api_key):
    """Load only owned verified files. Import heavy dependencies only in this worker."""
    if version("laya") != "0.4.16":
        raise ArtifactError("Provision the documented pinned Laya 0.4.16 environment")
    # Prevent inherited server settings, Hub credentials and auto-routing from widening
    # the explicitly configured local worker. This mutates only this separate process.
    for key in list(os.environ):
        if key.startswith("LAYA_") or key in {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"}:
            os.environ.pop(key)
    os.environ.update(
        LAYA_API_KEY=api_key,
        LAYA_DEVICE=device,
        LAYA_IDLE_UNLOAD_SECONDS="0",
        LAYA_MAX_CONCURRENT=str(concurrency),
        LAYA_MAX_TOKEN_BUDGET="1024",
        LAYA_JEV_STRICT="0",
        HF_HUB_OFFLINE="1",
        HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
        TRANSFORMERS_OFFLINE="1",
        OMP_NUM_THREADS=str(threads),
        MKL_NUM_THREADS=str(threads),
    )
    import torch
    from laya.router import Router
    from laya.serve import create_app

    torch.set_num_threads(threads)
    alias = checkpoint.model_alias
    source = str(checkpoint.directory)

    class PinnedRouter(Router):
        # The upstream HTTP layer treats an unknown short model name as automatic
        # routing. Require our alias after that resolution, before any inference.
        def predict(self, *args, model=None, **kwargs):
            if model != alias:
                raise ValueError("Explicit pinned worker model alias required")
            return super().predict(*args, model=model, **kwargs)

        def predict_batch(self, *args, model=None, **kwargs):
            if model != alias:
                raise ValueError("Explicit pinned worker model alias required")
            return super().predict_batch(*args, model=model, **kwargs)

    router = PinnedRouter(
        models={alias: source},
        default=alias,
        device=device,
        max_loaded=1,
        auto_task_detection=False,
        sha256_digests={alias: dict(checkpoint.digests)},
    )
    # The pinned Router always seeds built-ins, and unregister() refuses them.
    # Its public models registry is deliberately reduced before any load/request:
    # neither explicit model selection nor language/task auto-routing can load others.
    router.models = {alias: source}
    try:
        router.load(alias)
        # Upstream may normalize tokenizer metadata during load. Require a manifest
        # frozen after normalization; never serve modified bytes under the old pin.
        for name, expected in checkpoint.digests.items():
            with (checkpoint.directory / name).open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise ArtifactError("Checkpoint changed during load; normalize before freezing")
        return ArtifactIdentityApp(create_app(router), checkpoint.sha256, api_key), router
    except BaseException:
        router.unload()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-artifact-sha256", required=True)
    parser.add_argument("--api-key-env", default="KINAIYA_LAYA_API_KEY")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--port", type=int, default=8871)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.threads <= 32 or not 1 <= args.concurrency <= 8:
        parser.error("Invalid port, thread or concurrency bound")
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", args.api_key_env):
        parser.error("Use an environment variable name for the separate Laya secret")
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key or len(api_key.encode()) > 4096 or any(c in api_key for c in "\r\n"):
        parser.error("Set the separate Laya API secret in the configured environment variable")
    try:
        with verified_laya_checkpoint(
            args.model_dir, args.manifest, args.expected_artifact_sha256
        ) as checkpoint:
            app, router = build_app(
                checkpoint,
                device=args.device,
                threads=args.threads,
                concurrency=args.concurrency,
                api_key=api_key,
            )
            import uvicorn

            try:
                uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, log_level="warning")
            finally:
                router.unload()
    except ArtifactError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
