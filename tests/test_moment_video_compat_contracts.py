from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MomentVideoCompatibilityContracts(unittest.TestCase):
    def test_async_transcode_pipeline_is_h264_aac_faststart_and_bounded(self) -> None:
        source = (ROOT / "bbw_web" / "moment_video.py").read_text(encoding="utf-8")

        for marker in (
            'COMPAT_PROFILE = "h264-main-1280-v1"',
            'ALLOWED_SOURCE_HOSTS = ("oss.banghua.xin",)',
            'SOURCE_PATH_RE = re.compile(r"^/video/\\d{6}/\\d{10,20}\\.(?:mp4|mov)$", re.I)',
            '"-c:v",\n        "libx264"',
            '"-profile:v",\n        "main"',
            '"-c:a",\n        "aac"',
            '"-movflags",\n        "+faststart"',
            '"-xerror"',
            "fps=30",
            "download_and_prepare(",
            "storage.upload_stream(",
            "storage.head_object(object_key)",
            "canonical_path = _canonical_url_path(parsed.path)",
            "if not SOURCE_PATH_RE.fullmatch(canonical_path)",
            "stderr=subprocess.DEVNULL",
            "duration = max(durations, default=0.0)",
            '"-t",\n        f"{probe.duration:.3f}"',
            '"-fs",\n        str(max_output)',
            '"-maxrate",\n        str(video_maxrate)',
            "width * height > max_pixels",
            "fps > max_fps",
            "compatible video was truncated before completion",
            "def verified_source_url(source_url: Any, asset_id: str)",
            "def remember_cached_asset(",
            "def cached_assets_for_cleanup(",
            "BBW_MOMENT_VIDEO_CACHE_RETENTION_DAYS",
            "BBW_MOMENT_VIDEO_CACHE_MAX_BYTES",
        ):
            self.assertIn(marker, source)
        self.assertNotIn("capture_output=True", source)
        self.assertIn('"-i",\n        str(source)', source)
        self.assertNotIn('"-i",\n        source_url', source)
        self.assertNotIn("redis.lock(", source)
        self.assertNotIn("CredentialCipher", source)

    def test_cache_accounting_is_atomic_and_quota_is_trimmed_on_writes(self) -> None:
        source = (ROOT / "bbw_web" / "moment_video.py").read_text(encoding="utf-8")

        for marker in (
            "_REMEMBER_CACHE_LUA",
            "_FORGET_CACHE_LUA",
            "_CACHE_USAGE_LUA",
            "redis.call('HGET', KEYS[2], asset_id)",
            "redis.call('HSET', KEYS[2], asset_id, size)",
            "redis.call('HDEL', KEYS[2], asset_id)",
            "for _, value in ipairs(redis.call('HVALS', KEYS[2]))",
            "def trim_cached_assets_after_write(",
            "total_bytes <= max_bytes and object_count <= max_objects",
            "protected_asset_id=asset_id",
        ):
            self.assertIn(marker, source)

        remember = source.split("def remember_cached_asset", 1)[1].split(
            "def _cache_usage", 1
        )[0]
        forget = source.split("def forget_cached_asset", 1)[1].split(
            "def trim_cached_assets_after_write", 1
        )[0]
        self.assertIn("connection.eval(", remember)
        self.assertNotIn("connection.hget(", remember)
        self.assertNotIn("connection.pipeline(", remember)
        self.assertIn("connection.eval(", forget)
        self.assertNotIn("connection.hget(", forget)
        self.assertNotIn("connection.pipeline(", forget)

        transcode = source.split("def transcode_job", 1)[1]
        self.assertGreaterEqual(transcode.count("trim_cached_assets_after_write("), 2)

    def test_api_never_returns_the_original_url_and_supports_retry(self) -> None:
        source = (ROOT / "bbw_web" / "moment_media_api.py").read_text(encoding="utf-8")

        for marker in (
            'router = APIRouter(prefix="/api/media/compat-video"',
            '@router.post("/prepare")',
            '@router.get("/{asset_id}/status")',
            '@router.api_route("/{asset_id}/play", methods=["GET", "HEAD"])',
            'retry: bool = False',
            'post_id: str = Field(min_length=1, max_length=128)',
            'persistence.authorize_moment_video(',
            'persistence.can_access_moment_video(identity, asset_id)',
            'payload["playback_url"]',
            'payload["retryable"] = True',
            'failure_ttl=3600',
            'Retry(max=2, interval=[30, 120])',
            'status_code=307',
            'DEFAULT_ORIGIN_PORTS = {"http": 80, "https": 443}',
            'normalized != _request_origin(request)',
            'f"moment-video-status-total:{identity.user_id}"',
            'f"moment-video-status:{identity.user_id}:{asset_id}"',
            'f"moment-video-play:{identity.user_id}", limit=60',
            'ttl = 900',
            'payload["retry_after"] = 3',
            'if request.method == "HEAD"',
            'return Response(status_code=200, media_type="video/mp4", headers=headers)',
            'result = job.return_value(refresh=True)',
            'if state == "finished"',
            'detail="兼容视频暂时无法重新生成"',
            "def _queue_depth(queue: Queue)",
            "StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry",
        ):
            self.assertIn(marker, source)
        status_handler = source.split("def moment_video_status", 1)[1].split(
            "@router.api_route", 1
        )[0]
        self.assertLess(
            status_handler.index('if state == "processing"'),
            status_handler.index("metadata = _ready_metadata"),
        )
        public_payload = source.split("def _public_payload", 1)[1].split(
            "def _ready_metadata", 1
        )[0]
        self.assertNotIn("source_url", public_payload)
        self.assertNotIn("original", public_payload.lower())

    def test_rq_r2_and_runtime_wiring_is_present(self) -> None:
        api = (ROOT / "bbw_web" / "api.py").read_text(encoding="utf-8")
        jobs = (ROOT / "bbw_web" / "jobs.py").read_text(encoding="utf-8")
        moment_video = (ROOT / "bbw_web" / "moment_video.py").read_text(encoding="utf-8")
        moment_media_api = (ROOT / "bbw_web" / "moment_media_api.py").read_text(
            encoding="utf-8"
        )
        worker = (ROOT / "bbw_web" / "worker.py").read_text(encoding="utf-8")
        r2 = (ROOT / "bbw_web" / "r2.py").read_text(encoding="utf-8")
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        config = (ROOT / "bbw_prod" / "config.py").read_text(encoding="utf-8")
        prepare_host = (ROOT / "docker" / "prepare-host.sh").read_text(encoding="utf-8")
        persistence = (ROOT / "bbw_web" / "persistence.py").read_text(encoding="utf-8")

        self.assertIn("app.include_router(moment_media_router)", api)
        self.assertIn("def transcode_moment_video_job(", moment_video)
        self.assertIn("except MomentVideoError as exc", moment_video)
        self.assertIn("except MediaArchiveError as exc", moment_video)
        self.assertIn("UpstreamMediaUnavailable", moment_video)
        self.assertIn("status not in {408, 425, 429}", moment_video)
        self.assertNotIn("def transcode_moment_video_job(", jobs)
        self.assertIn(
            '"bbw_web.moment_video.transcode_moment_video_job"',
            moment_media_api,
        )
        self.assertIn("compat_media_deleted", jobs)
        self.assertIn("cached_assets_for_cleanup", jobs)
        self.assertIn("Worker(queues, connection=connection)", worker)
        self.assertIn('if any(name != "transcode" for name in settings.rq_queues)', worker)
        self.assertNotIn('name="bbw-worker"', worker)
        self.assertIn('os.getenv("BBW_RQ_WITH_SCHEDULER")', worker)
        self.assertIn("worker.work(with_scheduler=with_scheduler)", worker)
        self.assertIn("def head_object(self, key: str)", r2)
        self.assertIn("ca-certificates ffmpeg tzdata", dockerfile)
        self.assertIn("critical,default,media,sync,transcode", config)
        self.assertIn("transcode-worker:", compose)
        self.assertIn("BBW_RQ_QUEUES: transcode", compose)
        self.assertIn('BBW_RQ_WITH_SCHEDULER: "true"', compose)
        self.assertIn("x-transcode-secrets: &transcode-secrets", compose)
        self.assertIn("secrets: *transcode-secrets", compose)
        self.assertIn('BBW_SKIP_DATABASE_CONFIG: "true"', compose)
        transcode_service = compose.split("  transcode-worker:", 1)[1].split(
            "\n  scheduler:", 1
        )[0]
        self.assertNotIn("postgres_password", transcode_service)
        self.assertNotIn("app_master_key", transcode_service)
        self.assertNotIn("credential_keyring", transcode_service)
        self.assertNotIn("txim_secret_key", transcode_service)
        self.assertIn("TMPDIR: /var/tmp/bbw-transcode", compose)
        self.assertIn('BBW_MOMENT_VIDEO_JOB_TIMEOUT_SECONDS: "1500"', compose)
        self.assertIn('BBW_MOMENT_VIDEO_MAX_SOURCE_PIXELS: "9000000"', compose)
        self.assertIn('BBW_MOMENT_VIDEO_MAX_SOURCE_FPS: "120"', compose)
        self.assertIn('BBW_MOMENT_VIDEO_CACHE_RETENTION_DAYS: "30"', compose)
        self.assertIn('BBW_MOMENT_VIDEO_CACHE_MAX_BYTES: "2147483648"', compose)
        self.assertIn("stop_grace_period: 32m", transcode_service)
        self.assertIn(
            "${BBW_DATA_ROOT:-/var/lib/bbw}/transcode:/var/tmp/bbw-transcode",
            compose,
        )
        self.assertIn('$DATA_ROOT/transcode', prepare_host)
        self.assertIn("def remember_moment_video_grants(", persistence)
        self.assertIn("def authorize_moment_video(", persistence)
        self.assertIn("def can_access_moment_video(", persistence)
        self.assertIn('path == "/api/moments/posts"', persistence)

    def test_transcode_entrypoint_imports_without_protocol_secrets(self) -> None:
        env = os.environ.copy()
        env["BBW_ENV"] = "production"
        env["BBW_TXIM_SECRET_KEY_FILE"] = ""
        env["BBW_ROOMKIT_BUSINESS_TOKEN_FILE"] = ""
        env.pop("BBW_TXIM_SECRET_KEY", None)
        env.pop("BBW_ROOMKIT_BUSINESS_TOKEN", None)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from bbw_web.moment_video import transcode_moment_video_job; "
                    "assert callable(transcode_moment_video_job); "
                    "assert 'bbw_protocol.sign' not in sys.modules"
                ),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
