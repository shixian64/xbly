from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class MomentVideoCompatibilityContracts(unittest.TestCase):
    def test_async_transcode_pipeline_remuxes_safe_h264_and_transcodes_other_sources(self) -> None:
        source = (ROOT / "bbw_web" / "moment_video.py").read_text(encoding="utf-8")

        for marker in (
            'COMPAT_PROFILE = "h264-main-1280-v2"',
            'ALLOWED_SOURCE_HOSTS = ("oss.banghua.xin",)',
            'r"/video/\\d{6}/\\d{10,20}\\.(?:mp4|mov)|"',
            'r"/audios/99999/\\d{4}/(?:0[1-9]|1[0-2])/[A-Z0-9_-]{8,128}\\.mp4"',
            '"-c:v",\n        "libx264"',
            "def can_remux_h264(probe: VideoProbe)",
            "def can_copy_aac_audio(probe: VideoProbe)",
            "def remux_h264(source: Path, probe: VideoProbe)",
            '"-c:v",\n        "copy"',
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
            "moment_video_max_source_bytes",
            "max_bytes=int(settings.moment_video_max_source_bytes)",
            'code="TRANSCODE_FAILED"',
        ):
            self.assertIn(marker, source)
        self.assertNotIn("capture_output=True", source)
        self.assertIn('"-i",\n        str(source)', source)
        self.assertNotIn('"-i",\n        source_url', source)
        self.assertNotIn("redis.lock(", source)
        self.assertNotIn("CredentialCipher", source)
        self.assertNotIn('COMPAT_PROFILE = "h264-main-1280-v1"', source)
        remux = source.split("def remux_h264", 1)[1].split(
            "def transcode_h264", 1
        )[0]
        self.assertNotIn('"-fs"', remux)
        self.assertIn("duration_tolerance=1.0", remux)

    def test_remux_only_copies_explicitly_compatible_aac_audio(self) -> None:
        from bbw_web.moment_video import VideoProbe, can_copy_aac_audio

        base = {
            "codec": "h264",
            "audio_codec": "aac",
            "audio_profile": "lc",
            "audio_channels": 2,
            "audio_sample_rate": 48000,
            "pixel_format": "yuv420p",
            "profile": "main",
            "level": 40,
            "format_name": "mov,mp4",
            "width": 1280,
            "height": 720,
            "duration": 60.0,
            "fps": 30.0,
        }

        def audio_probe(**changes: object) -> VideoProbe:
            return VideoProbe(**(base | changes))

        self.assertTrue(can_copy_aac_audio(audio_probe()))
        self.assertFalse(can_copy_aac_audio(audio_probe(audio_profile="he-aac")))
        self.assertFalse(can_copy_aac_audio(audio_probe(audio_profile="xhe-aac")))
        self.assertFalse(can_copy_aac_audio(audio_probe(audio_channels=6)))
        self.assertFalse(can_copy_aac_audio(audio_probe(audio_profile="unknown")))

    def test_remux_requires_main_profile_with_a_known_supported_level(self) -> None:
        from bbw_web.moment_video import VideoProbe, can_remux_h264

        base = {
            "codec": "h264",
            "audio_codec": "aac",
            "audio_profile": "lc",
            "audio_channels": 2,
            "audio_sample_rate": 48000,
            "pixel_format": "yuv420p",
            "profile": "main",
            "level": 40,
            "format_name": "mov,mp4",
            "width": 1280,
            "height": 720,
            "duration": 60.0,
            "fps": 30.0,
        }

        def video_probe(**changes: object) -> VideoProbe:
            return VideoProbe(**(base | changes))

        self.assertTrue(can_remux_h264(video_probe()))
        self.assertFalse(can_remux_h264(video_probe(profile="high")))
        self.assertFalse(can_remux_h264(video_probe(profile="baseline")))
        self.assertFalse(can_remux_h264(video_probe(level=0)))
        self.assertFalse(can_remux_h264(video_probe(level=50)))

    def test_output_validation_rejects_non_main_or_unsupported_level(self) -> None:
        from bbw_web.moment_video import (
            MomentVideoError,
            VideoProbe,
            _prepared_compatible_output,
        )

        base = {
            "codec": "h264",
            "audio_codec": "aac",
            "audio_profile": "lc",
            "audio_channels": 2,
            "audio_sample_rate": 48000,
            "pixel_format": "yuv420p",
            "profile": "main",
            "level": 40,
            "format_name": "mov,mp4",
            "width": 1280,
            "height": 720,
            "duration": 60.0,
            "fps": 30.0,
        }
        source_probe = VideoProbe(**base)
        invalid_outputs = (
            {"profile": "high"},
            {"profile": "baseline"},
            {"level": 0},
            {"level": 50},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.mp4"
            output.write_bytes(b"test")
            for changes in invalid_outputs:
                with self.subTest(changes=changes), patch(
                    "bbw_web.moment_video.probe_video",
                    return_value=VideoProbe(**(base | changes)),
                ), self.assertRaises(MomentVideoError) as raised:
                    _prepared_compatible_output(
                        output,
                        source_probe,
                        max_output=1024,
                    )
                self.assertEqual(raised.exception.code, "OUTPUT_INVALID")

    def test_strict_remux_duration_validation_rejects_truncated_output(self) -> None:
        from bbw_web.moment_video import (
            MomentVideoError,
            VideoProbe,
            _prepared_compatible_output,
        )

        base = {
            "codec": "h264",
            "audio_codec": "aac",
            "audio_profile": "lc",
            "audio_channels": 2,
            "audio_sample_rate": 48000,
            "pixel_format": "yuv420p",
            "profile": "main",
            "level": 40,
            "format_name": "mov,mp4",
            "width": 1280,
            "height": 720,
            "fps": 30.0,
        }
        source_probe = VideoProbe(**(base | {"duration": 600.0}))
        truncated_probe = VideoProbe(**(base | {"duration": 598.9}))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "remux.mp4"
            output.write_bytes(b"test")
            with patch(
                "bbw_web.moment_video.probe_video",
                return_value=truncated_probe,
            ), self.assertRaises(MomentVideoError) as raised:
                _prepared_compatible_output(
                    output,
                    source_probe,
                    max_output=1024,
                    duration_tolerance=1.0,
                )
        self.assertEqual(raised.exception.code, "OUTPUT_INVALID")

    def test_real_apk_moment_video_paths_are_allowed_without_broadening_hosts(
        self,
    ) -> None:
        from bbw_web.moment_video import MomentVideoError, canonical_source_identity

        allowed = {
            "video/202607/1784275489034.MOV": (
                "https://oss.banghua.xin/video/202607/1784275489034.MOV"
            ),
            "audios/99999/2022/08/IJ6J6OOCV2ah2HDAHAdqJsS2.mp4": (
                "https://oss.banghua.xin/audios/99999/2022/08/"
                "IJ6J6OOCV2ah2HDAHAdqJsS2.mp4"
            ),
        }
        for value, expected in allowed.items():
            with self.subTest(value=value):
                self.assertEqual(canonical_source_identity(value), expected)

        rejected = (
            "https://example.invalid/video/202607/1784275489034.MOV",
            "audios/99999/2022/13/IJ6J6OOCV2ah2HDAHAdqJsS2.mp4",
            "audios/99999/2022/08/../../secret.mp4",
            "audios/10000/2022/08/IJ6J6OOCV2ah2HDAHAdqJsS2.mp4",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(MomentVideoError):
                canonical_source_identity(value)

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

    def test_legacy_compat_cache_namespace_remains_cleanup_reachable(self) -> None:
        from bbw_web.moment_video import (
            COMPAT_PROFILE,
            LEGACY_COMPAT_PROFILES,
            LEGACY_TRANSCODE_QUEUES,
            TRANSCODE_QUEUE,
            _cache_index_keys,
            asset_id_for_url,
            cached_assets_for_cleanup,
            clear_empty_cache_index,
            compatibility_profiles_for_cleanup,
            object_key_for_asset,
            verified_source_identity,
        )

        asset_id = "a" * 64
        settings = SimpleNamespace(redis_prefix="bbw-test")
        legacy_profile = "h264-main-1280-v1"
        self.assertEqual(COMPAT_PROFILE, "h264-main-1280-v2")
        self.assertEqual(TRANSCODE_QUEUE, "transcode-v2")
        self.assertNotIn(TRANSCODE_QUEUE, LEGACY_TRANSCODE_QUEUES)
        self.assertIn(legacy_profile, LEGACY_COMPAT_PROFILES)
        self.assertEqual(
            compatibility_profiles_for_cleanup(),
            (COMPAT_PROFILE, legacy_profile),
        )
        self.assertIn(
            f"compat/moments/{legacy_profile}/",
            object_key_for_asset(asset_id, profile=legacy_profile),
        )
        source_url = "https://oss.banghua.xin/video/202607/1784275489034.MOV"
        legacy_asset_id = asset_id_for_url(source_url, profile=legacy_profile)
        self.assertEqual(
            verified_source_identity(source_url, legacy_asset_id),
            (source_url, legacy_profile),
        )

        class FakeRedis:
            def __init__(self) -> None:
                self.zrange_key = ""
                self.deleted: tuple[str, ...] = ()

            def zrangebyscore(
                self,
                key: str,
                _minimum: str,
                _maximum: float,
                *,
                start: int,
                num: int,
            ) -> list[bytes]:
                self.zrange_key = key
                self.assert_range = (start, num)
                return [asset_id.encode("ascii")]

            @staticmethod
            def eval(*_args: object) -> list[int]:
                return [4, 1]

            @staticmethod
            def hget(_key: str, _asset_id: str) -> int:
                return 4

            @staticmethod
            def zcard(_key: str) -> int:
                return 0

            @staticmethod
            def hlen(_key: str) -> int:
                return 0

            def delete(self, *keys: str) -> int:
                self.deleted = keys
                return len(keys)

        connection = FakeRedis()
        selected = cached_assets_for_cleanup(
            connection,
            settings,
            limit=50,
            profile=legacy_profile,
        )
        self.assertEqual(selected, [asset_id])
        self.assertIn(legacy_profile, connection.zrange_key)
        age_key, size_key, total_key = _cache_index_keys(
            settings,
            profile=legacy_profile,
        )
        self.assertTrue(
            clear_empty_cache_index(
                connection,
                settings,
                profile=legacy_profile,
            )
        )
        self.assertEqual(
            connection.deleted,
            (age_key, size_key, total_key, f"{total_key}:version"),
        )

    def test_feed_grants_and_api_lookups_bridge_v1_and_v2(self) -> None:
        from bbw_web.moment_media_api import _job_for_asset, _ready_metadata
        from bbw_web.moment_video import (
            COMPAT_PROFILE,
            asset_id_for_url,
            job_id_for_asset,
            object_key_for_asset,
        )
        from bbw_web.persistence import RuntimePersistence

        source_url = "https://oss.banghua.xin/video/202607/1784275489034.MOV"
        legacy_profile = "h264-main-1280-v1"
        current_asset_id = asset_id_for_url(source_url)
        legacy_asset_id = asset_id_for_url(source_url, profile=legacy_profile)
        identity = SimpleNamespace(user_id="user-1")

        class FakeRedis:
            def __init__(self) -> None:
                self.values: dict[str, bytes] = {}

            def pipeline(self) -> "FakeRedis":
                return self

            def set(self, key: str, value: bytes, **_kwargs: object) -> bool:
                self.values[key] = value
                return True

            @staticmethod
            def execute() -> list[object]:
                return []

            def exists(self, key: str) -> bool:
                return key in self.values

        redis = FakeRedis()
        persistence = object.__new__(RuntimePersistence)
        persistence.redis = redis
        persistence.settings = SimpleNamespace(redis_prefix="bbw-test")
        persistence.remember_moment_video_grants(
            identity,
            {"items": [{"id": "post-1", "video": source_url}]},
        )
        self.assertIn(
            persistence._moment_video_feed_grant_key(
                identity,
                "post-1",
                legacy_asset_id,
            ),
            redis.values,
        )

        redis.values.clear()
        redis.set(
            persistence._moment_video_feed_grant_key(
                identity,
                "post-1",
                legacy_asset_id,
            ),
            b"1",
        )
        self.assertTrue(
            persistence.authorize_moment_video(
                identity,
                post_id="post-1",
                asset_id=current_asset_id,
                source_url=source_url,
            )
        )
        for asset_id in (current_asset_id, legacy_asset_id):
            self.assertIn(
                persistence._moment_video_access_key(identity, asset_id),
                redis.values,
            )

        legacy_job = object()

        class FakeQueue:
            def fetch_job(self, job_id: str) -> object | None:
                if job_id == job_id_for_asset(
                    legacy_asset_id,
                    profile=legacy_profile,
                ):
                    return legacy_job
                return None

        self.assertEqual(
            _job_for_asset(FakeQueue(), legacy_asset_id),
            (legacy_job, legacy_profile),
        )

        class FakeStorage:
            def head_object(self, key: str) -> dict[str, int] | None:
                if key == object_key_for_asset(
                    legacy_asset_id,
                    profile=legacy_profile,
                ):
                    return {"size": 123}
                return None

        api_persistence = SimpleNamespace(
            get_r2_storage=lambda: FakeStorage(),
            redis=object(),
            settings=SimpleNamespace(),
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(persistence=api_persistence),
            )
        )
        with patch("bbw_web.moment_media_api.remember_cached_asset") as remember:
            ready = _ready_metadata(request, legacy_asset_id)
        self.assertEqual(ready, ({"size": 123}, legacy_profile))
        remember.assert_called_once_with(
            api_persistence.redis,
            api_persistence.settings,
            legacy_asset_id,
            123,
            profile=legacy_profile,
        )

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
            'payload["retryable"] = bool(retryable)',
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
            "return Queue(TRANSCODE_QUEUE, connection=_persistence(request).redis)",
            'f"moment-video-resolve:{identity.user_id}"',
            'f"moment-video-prepare-total:{identity.user_id}"',
            'f"moment-video-create:{identity.user_id}"',
            "PUBLIC_FAILURE_MESSAGES",
            'rolling_deploy_mismatch = error_code == "SOURCE_IDENTITY_MISMATCH"',
            '"retrying moment video after rolling deploy asset_id=%s"',
        ):
            self.assertIn(marker, source)
        self.assertNotIn('return Queue("transcode",', source)
        status_handler = source.split("def moment_video_status", 1)[1].split(
            "@router.api_route", 1
        )[0]
        self.assertLess(
            status_handler.index('if state == "processing"'),
            status_handler.index("ready = _ready_metadata"),
        )
        public_payload = source.split("def _public_payload", 1)[1].split(
            "def _ready_metadata", 1
        )[0]
        self.assertNotIn("source_url", public_payload)
        self.assertNotIn("original", public_payload.lower())
        prepare_handler = source.split("def prepare_moment_video", 1)[1].split(
            '@router.get("/{asset_id}/status")', 1
        )[0]
        self.assertLess(
            prepare_handler.index("ready = _ready_metadata"),
            prepare_handler.index('f"moment-video-create:{identity.user_id}"'),
        )
        self.assertLess(
            prepare_handler.index('f"moment-video-prepare-total:{identity.user_id}"'),
            prepare_handler.index("canonical_source_identity(body.source_url)"),
        )
        self.assertLess(
            prepare_handler.index('f"moment-video-prepare-total:{identity.user_id}"'),
            prepare_handler.index("persistence.authorize_moment_video("),
        )
        self.assertLess(
            prepare_handler.index('if state == "processing"'),
            prepare_handler.index('f"moment-video-create:{identity.user_id}"'),
        )

    def test_rq_r2_and_runtime_wiring_is_present(self) -> None:
        api = (ROOT / "bbw_web" / "api.py").read_text(encoding="utf-8")
        bff = (ROOT / "bbw_web" / "bff_server.py").read_text(encoding="utf-8")
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
        self.assertIn("legacy.MOMENT_VIDEO_COMPAT_ENABLED = True", api)
        self.assertIn('"moment_video_compat": MOMENT_VIDEO_COMPAT_ENABLED', bff)
        self.assertIn("def transcode_moment_video_job(", moment_video)
        self.assertIn("except MomentVideoError as exc", moment_video)
        self.assertIn("except MediaArchiveError as exc", moment_video)
        self.assertIn('"SOURCE_UNAVAILABLE"', moment_video)
        self.assertIn("status not in {408, 425, 429}", moment_video)
        self.assertNotIn("def transcode_moment_video_job(", jobs)
        self.assertIn(
            '"bbw_web.moment_video.transcode_moment_video_job"',
            moment_media_api,
        )
        self.assertIn("compat_media_deleted", jobs)
        self.assertIn("cached_assets_for_cleanup", jobs)
        self.assertIn("compatibility_profiles_for_cleanup", jobs)
        self.assertNotIn("purge_all=", jobs)
        self.assertIn("profile=compat_profile", jobs)
        self.assertIn("clear_empty_cache_index", jobs)
        self.assertIn("TRANSCODE_QUEUE = \"transcode-v2\"", moment_video)
        self.assertIn("LEGACY_TRANSCODE_QUEUES = (\"transcode\",)", moment_video)
        self.assertIn("verified_source_identity(source_url, asset_id)", moment_video)
        self.assertIn("profile=compat_profile", moment_video)
        self.assertIn("Worker(queues, connection=connection)", worker)
        self.assertIn('TRANSCODE_ONLY_QUEUES = {"transcode", "transcode-v2"}', worker)
        self.assertIn("name not in TRANSCODE_ONLY_QUEUES", worker)
        self.assertNotIn('name="bbw-worker"', worker)
        self.assertIn('os.getenv("BBW_RQ_WITH_SCHEDULER")', worker)
        self.assertIn("worker.work(with_scheduler=with_scheduler)", worker)
        self.assertIn("def head_object(self, key: str)", r2)
        self.assertIn("ca-certificates ffmpeg tzdata", dockerfile)
        self.assertIn(
            "critical,im-ingest,default,media,sync,transcode-v2,transcode",
            config,
        )
        self.assertIn("transcode-worker:", compose)
        self.assertIn("BBW_RQ_QUEUES: transcode-v2,transcode", compose)
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
        self.assertIn('BBW_MOMENT_VIDEO_MAX_SOURCE_BYTES: "157286400"', compose)
        self.assertIn('"BBW_MOMENT_VIDEO_MAX_SOURCE_BYTES", 150 * MEBIBYTE', config)
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
