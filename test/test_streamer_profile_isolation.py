import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


CODE = Path(__file__).parents[1] / "src" / "code"


def load_timeline_module():
    requests = types.ModuleType("requests")
    requests.RequestException = Exception
    requests.get = mock.Mock()
    yt_dlp = types.ModuleType("yt_dlp")
    yt_dlp.YoutubeDL = object
    pydantic = types.ModuleType("pydantic")
    pydantic.BaseModel = object
    pydantic.ConfigDict = dict
    pydantic.Field = lambda *args, **kwargs: None
    sys.modules.setdefault("requests", requests)
    sys.modules.setdefault("yt_dlp", yt_dlp)
    sys.modules.setdefault("pydantic", pydantic)
    sys.path.insert(0, str(CODE))
    source = CODE / "Timeline.py"
    spec = importlib.util.spec_from_file_location("timeline_profile_isolation_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StreamerProfileIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.timeline = load_timeline_module()

    def test_profile_paths_are_channel_scoped_and_reject_invalid_ids(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(
                (str(Path(root) / "channel-1.txt"), str(Path(root) / "channel-1.meta.json")),
                self.timeline._streamer_profile_paths("channel-1", root),
            )
        for invalid_id in ("", "../escape", "channel/child", "a.b", "x" * 129):
            with self.subTest(channel_id=invalid_id):
                self.assertEqual(("", ""), self.timeline._streamer_profile_paths(invalid_id))

    def test_same_nickname_profiles_are_isolated_by_channel_id(self):
        with tempfile.TemporaryDirectory() as root:
            for channel_id in ("channel-a", "channel-b"):
                profile_path, _ = self.timeline._streamer_profile_paths(channel_id, root)
                Path(profile_path).write_text(
                    f"[방송인 기본 정보]\n- 스트리머 이름: 같은 닉네임\n- 표식: {channel_id}",
                    encoding="utf-8",
                )
            _, profile_a = self.timeline.load_streamer_profile("channel-a", "같은 닉네임", root)
            _, profile_b = self.timeline.load_streamer_profile("channel-b", "같은 닉네임", root)
            self.assertIn("표식: channel-a", profile_a)
            self.assertNotIn("channel-b", profile_a)
            self.assertIn("표식: channel-b", profile_b)
            self.assertNotIn("channel-a", profile_b)

    def test_root_legacy_profile_is_never_loaded(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "streamer_info.txt").write_text(
                "[방송 정보]\n- 스트리머: 같은 닉네임\n- 표식: legacy-only", encoding="utf-8"
            )
            _, profile = self.timeline.load_streamer_profile(
                "channel-a", "같은 닉네임", str(Path(root) / "streamer_profiles")
            )
            self.assertIn("치지직 채널 ID: channel-a", profile)
            self.assertNotIn("legacy-only", profile)

    def test_profile_with_different_nickname_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            profile_path, _ = self.timeline._streamer_profile_paths("channel-a", root)
            Path(profile_path).write_text(
                "[방송인 기본 정보]\n- 스트리머 이름: 다른 닉네임\n- 전용정보: secret", encoding="utf-8"
            )
            _, profile = self.timeline.load_streamer_profile("channel-a", "현재 닉네임", root)
            self.assertIn("스트리머 이름: 현재 닉네임", profile)
            self.assertNotIn("secret", profile)

    def test_missing_channel_id_skips_profile_io_research_and_prompt(self):
        timeline = self.timeline
        with mock.patch.object(timeline.os.path, "isfile", side_effect=AssertionError("profile read")), \
             mock.patch.object(timeline, "research_streamer_profile", side_effect=AssertionError("research")), \
             mock.patch("builtins.input", side_effect=AssertionError("prompt")):
            name, profile = timeline.prepare_streamer_profile("", "임시 닉네임")
        self.assertEqual("임시 닉네임", name)
        self.assertIn("스트리머 이름: 임시 닉네임", profile)
        self.assertIn("채널 ID: 확인 불가", profile)

    def test_missing_channel_id_load_does_not_open_global_profile(self):
        with mock.patch("builtins.open", side_effect=AssertionError("unexpected profile read")):
            name, profile = self.timeline.load_streamer_profile("../invalid", "임시 닉네임")
        self.assertEqual("임시 닉네임", name)
        self.assertIn("스트리머 이름: 임시 닉네임", profile)

    def test_official_update_writes_only_channel_profile_and_metadata(self):
        timeline = self.timeline
        with tempfile.TemporaryDirectory() as root:
            profile_root = Path(root) / "streamer_profiles"
            profile_root.mkdir()
            profile_path, metadata_path = timeline._streamer_profile_paths("channel-a", str(profile_root))
            Path(profile_path).write_text(
                "[방송인 기본 정보]\n- 스트리머 이름: 방송인\n\n[사용자 메모]\n- 유지할 내용",
                encoding="utf-8",
            )
            response = types.SimpleNamespace(
                status_code=200,
                content=b"{}",
                json=lambda: {"content": {
                    "channelId": "channel-a", "channelName": "방송인", "channelDescription": "공식 소개"
                }},
            )
            with mock.patch.object(timeline.requests, "get", return_value=response) as get, \
                 mock.patch("builtins.input", return_value="update"):
                name, profile = timeline.prepare_streamer_profile(
                    "channel-a", "방송인", profile_root=str(profile_root)
                )

            self.assertEqual("방송인", name)
            self.assertIn("공식 소개", profile)
            self.assertIn("유지할 내용", profile)
            self.assertEqual(profile, Path(profile_path).read_text(encoding="utf-8"))
            metadata = __import__("json").loads(Path(metadata_path).read_text(encoding="utf-8"))
            self.assertEqual("channel-a", metadata["channel_id"])
            self.assertEqual("방송인", metadata["channel_name"])
            self.assertIn("/channels/channel-a", get.call_args.args[0])
            self.assertFalse(Path(root, "streamer_info.txt").exists())

    def test_official_update_rejects_channel_or_name_mismatch(self):
        timeline = self.timeline
        cases = (
            {"channelId": "other-channel", "channelName": "방송인"},
            {"channelId": "channel-a", "channelName": "다른 이름"},
            {"channelName": "방송인"},
        )
        for content in cases:
            with self.subTest(content=content):
                response = types.SimpleNamespace(status_code=200, content=b"{}", json=lambda: {"content": content})
                with mock.patch.object(timeline.requests, "get", return_value=response):
                    profile, metadata = timeline.research_streamer_profile("channel-a", "방송인")
                self.assertEqual(("", {}), (profile, metadata))

    def test_selected_vod_returns_its_channel_id(self):
        source = CODE / "Chzzk_api.py"
        spec = importlib.util.spec_from_file_location("chzzk_api_profile_isolation_test", source)
        module = importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "config.json"
            config_path.write_text(json.dumps({}), encoding="utf-8")
            previous_cwd = __import__("os").getcwd()
            try:
                __import__("os").chdir(root)
                spec.loader.exec_module(module)
                with mock.patch.object(module, "get_chzzk_vod_list", return_value=[{
                    "videoNo": 123,
                    "videoTitle": "선택된 VOD",
                    "duration": 60,
                    "channel": {"channelId": "vod-channel-id", "channelName": "채널"},
                }]), mock.patch("builtins.input", return_value="1"), \
                     mock.patch("sys.stdout", new_callable=__import__("io").StringIO):
                    result = module.select_chzzk_vod("query-channel-id")
            finally:
                __import__("os").chdir(previous_cwd)
        self.assertEqual(("123", "선택된 VOD", 60, "채널", "vod-channel-id"), result)

    def test_timeline_generation_uses_prepared_profile_without_reloading(self):
        timeline = self.timeline
        with tempfile.TemporaryDirectory() as root:
            previous_cwd = __import__("os").getcwd()
            try:
                __import__("os").chdir(root)
                with mock.patch.object(timeline, "load_streamer_profile", side_effect=AssertionError("reloaded")), \
                     mock.patch.object(timeline, "run_codex", return_value='{"items": []}') as run_codex, \
                     mock.patch.object(timeline.time, "sleep"), \
                     mock.patch.object(timeline.TimelineResponse, "model_json_schema", return_value={}, create=True):
                    result = timeline.generate_chzzk_timeline(
                        input_script="[00:00:01] 대사",
                        use_collab_member_reference=False,
                        target_streamer="채널명",
                        target_channel_id="selected-channel",
                        streamer_profile_context="[방송인 기본 정보]\n준비된 프로필",
                    )
            finally:
                __import__("os").chdir(previous_cwd)
        self.assertEqual([], result)
        self.assertIn("준비된 프로필", run_codex.call_args.kwargs["prompt"])


if __name__ == "__main__":
    unittest.main()
