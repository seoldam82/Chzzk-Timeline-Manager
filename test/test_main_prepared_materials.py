import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


def load_main_module():
    chzzk_api = types.ModuleType("Chzzk_api")
    chzzk_api.select_chzzk_vod = lambda *args, **kwargs: None
    chzzk_api.download_chzzk_vod_chats = lambda *args, **kwargs: ""
    chzzk_api.CONFIG = {}

    timeline = types.ModuleType("Timeline")
    for name in (
        "download_chzzk_vod_audio",
        "transcribe_chzzk_audio",
        "generate_chzzk_timeline",
        "merge_and_format_final_timeline",
        "timestamp_to_seconds",
        "correct_streamer_nicknames_with_codex",
        "ensure_codex_ready",
        "load_chzzk_streamers_raw_db",
        "prepare_streamer_profile",
    ):
        setattr(timeline, name, lambda *args, **kwargs: None)
    timeline.prepare_streamer_profile = lambda target_channel_id="", target_streamer="", **kwargs: (
        target_streamer, ""
    )

    sys.modules["Chzzk_api"] = chzzk_api
    sys.modules["Timeline"] = timeline
    main_path = Path(__file__).parents[1] / "src" / "code" / "Main.py"
    spec = importlib.util.spec_from_file_location("main_under_test", main_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PreparedTimelineMaterialsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_module()

    def test_loads_prepared_transcription_when_both_materials_exist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vod_id = "123"
            script_dir = Path(temp_dir) / "voicepalette" / f"VOD_{vod_id}"
            chat_dir = Path(temp_dir) / "chat_cache" / vod_id
            script_dir.mkdir(parents=True)
            chat_dir.mkdir(parents=True)
            (script_dir / "full_raw_script.txt").write_text(
                "[00:00:01] 충분히 긴 테스트 대본", encoding="utf-8"
            )
            (chat_dir / f"chat_{vod_id}_full.txt").write_text(
                "[00:00:01] 테스트 채팅", encoding="utf-8"
            )

            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                result = self.main.load_prepared_timeline_materials(vod_id)
            finally:
                os.chdir(previous_cwd)

            self.assertIn("충분히 긴 테스트 대본", result)

    def test_rejects_incomplete_materials_without_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = os.getcwd()
            output = io.StringIO()
            try:
                os.chdir(temp_dir)
                with contextlib.redirect_stdout(output):
                    result = self.main.load_prepared_timeline_materials("456")
            finally:
                os.chdir(previous_cwd)

            self.assertEqual("", result)
            self.assertIn("STT 대본", output.getvalue())
            self.assertIn("채팅 캐시", output.getvalue())

    def test_parses_supported_time_formats(self):
        self.assertEqual(754, self.main.parse_time_input("12:34"))
        self.assertEqual(3723, self.main.parse_time_input("01:02:03"))

    def test_rejects_invalid_time_components(self):
        for invalid_value in ("90", "1:60", "00:00:60", "aa:bb"):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaises(ValueError):
                    self.main.parse_time_input(invalid_value)

    def test_time_range_reprompts_until_valid(self):
        values = iter(("00:20", "00:10", "00:05", "00:30"))
        with mock.patch("builtins.input", side_effect=lambda *_: next(values)):
            with contextlib.redirect_stdout(io.StringIO()):
                result = self.main.ask_analysis_time_range(60)
        self.assertEqual((5, 30), result)

    def test_time_range_prompt_shows_full_video_range(self):
        values = iter(("00:00", "01:00"))
        output = io.StringIO()
        def fake_input(prompt):
            output.write(prompt)
            return next(values)

        with mock.patch("builtins.input", side_effect=fake_input):
            self.main.ask_analysis_time_range(35670)
        self.assertIn("영상 범위: 00:00:00 ~ 09:54:30", output.getvalue())

    def test_normal_mode_passes_range_to_audio_and_stt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audio_path = Path(temp_dir) / "range_audio.ts"
            audio_path.write_bytes(b"audio")
            download_audio = mock.Mock(return_value=str(audio_path))
            transcribe = mock.Mock(return_value="[00:10:00] 테스트 대본")

            patches = (
                mock.patch.object(self.main, "ensure_codex_ready"),
                mock.patch.object(
                    self.main, "select_chzzk_vod",
                    return_value=("123", "테스트 VOD", 3600, "스트리머"),
                ),
                mock.patch.object(self.main, "ask_use_collab_member_reference", return_value=False),
                mock.patch.object(self.main, "download_chzzk_vod_audio", download_audio),
                mock.patch.object(self.main, "transcribe_chzzk_audio", transcribe),
                mock.patch.object(self.main, "download_chzzk_vod_chats", return_value=""),
                mock.patch.object(self.main, "timestamp_to_seconds", return_value=600),
                mock.patch.object(self.main, "generate_chzzk_timeline", return_value=[]),
                mock.patch("builtins.input", side_effect=("", "00:10:00", "00:20:00")),
            )
            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                with contextlib.ExitStack() as stack:
                    for active_patch in patches:
                        stack.enter_context(active_patch)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.main.run_pure_test()
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(600, download_audio.call_args.kwargs["start_sec"])
            self.assertEqual(1200, download_audio.call_args.kwargs["end_sec"])
            self.assertEqual(600, transcribe.call_args.kwargs["timestamp_offset_sec"])
            self.assertTrue(
                transcribe.call_args.kwargs["target_path"].endswith(
                    os.path.join("VOD_123", "raw_script_600_1200.txt")
                )
            )

    def test_selected_channel_profile_is_prepared_once_and_reused_for_each_range_chunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = "[00:10:00] 첫 청크\n[01:10:00] 둘째 청크"
            selected_channel_id = "selected-channel"
            prepare_profile = mock.Mock(return_value=("채널명", "[방송인 기본 정보]\n채널 프로필"))
            generate = mock.Mock(return_value=[{"seconds": 1}])
            patches = (
                mock.patch.object(self.main, "ensure_codex_ready"),
                mock.patch.object(
                    self.main, "select_chzzk_vod",
                    return_value=("123", "테스트 VOD", 7200, "채널명", selected_channel_id),
                ),
                mock.patch.object(self.main, "prepare_streamer_profile", prepare_profile),
                mock.patch.object(self.main, "ask_analysis_time_range", return_value=(0, 7200)),
                mock.patch.object(self.main, "ask_use_collab_member_reference", return_value=False),
                mock.patch.object(self.main, "load_prepared_timeline_materials", return_value=script),
                mock.patch.object(self.main, "download_chzzk_vod_chats", return_value=""),
                mock.patch.object(
                    self.main, "timestamp_to_seconds",
                    side_effect=lambda value: {"00:10:00": 600, "01:10:00": 4200}[value],
                ),
                mock.patch.object(self.main, "generate_chzzk_timeline", generate),
                mock.patch.object(self.main, "merge_and_format_final_timeline", return_value="[00:00:01] 결과"),
                mock.patch.object(
                    self.main, "correct_streamer_nicknames_with_codex",
                    side_effect=lambda timeline_text, **kwargs: timeline_text,
                ),
                mock.patch.object(self.main.os, "startfile", create=True),
                mock.patch("builtins.input", return_value="1"),
            )
            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                with contextlib.ExitStack() as stack:
                    for active_patch in patches:
                        stack.enter_context(active_patch)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.main.run_pure_test(timeline_only=True)
            finally:
                os.chdir(previous_cwd)

            prepare_profile.assert_called_once_with(
                target_channel_id=selected_channel_id, target_streamer="채널명"
            )
            self.assertEqual(2, generate.call_count)
            for call in generate.call_args_list:
                self.assertEqual(selected_channel_id, call.kwargs["target_channel_id"])
                self.assertEqual("[방송인 기본 정보]\n채널 프로필", call.kwargs["streamer_profile_context"])
            self.assertEqual([0, 1], [call.kwargs["chunk_index"] for call in generate.call_args_list])

    def test_legacy_vod_result_does_not_use_requested_channel_as_profile_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = "[00:10:00] 첫 청크\n[01:10:00] 둘째 청크"
            prepare_profile = mock.Mock(return_value=("채널명", "임시 프로필"))
            patches = (
                mock.patch.object(self.main, "ensure_codex_ready"),
                mock.patch.object(self.main, "CONFIG", {"TARGET_CHANNEL_ID": "requested-channel"}),
                mock.patch.object(
                    self.main, "select_chzzk_vod",
                    return_value=("123", "테스트 VOD", 7200, "채널명"),
                ),
                mock.patch.object(self.main, "prepare_streamer_profile", prepare_profile),
                mock.patch.object(self.main, "ask_analysis_time_range", return_value=(0, 7200)),
                mock.patch.object(self.main, "ask_use_collab_member_reference", return_value=False),
                mock.patch.object(self.main, "load_prepared_timeline_materials", return_value=script),
                mock.patch.object(self.main, "download_chzzk_vod_chats", return_value=""),
                mock.patch.object(
                    self.main, "timestamp_to_seconds",
                    side_effect=lambda value: {"00:10:00": 600, "01:10:00": 4200}[value],
                ),
                mock.patch.object(self.main, "generate_chzzk_timeline", return_value=[{"seconds": 1}]),
                mock.patch.object(self.main, "merge_and_format_final_timeline", return_value="[00:00:01] 결과"),
                mock.patch.object(
                    self.main, "correct_streamer_nicknames_with_codex",
                    side_effect=lambda timeline_text, **kwargs: timeline_text,
                ),
                mock.patch.object(self.main.os, "startfile", create=True),
                mock.patch("builtins.input", return_value="1"),
            )
            previous_cwd = os.getcwd()
            try:
                os.chdir(temp_dir)
                with contextlib.ExitStack() as stack:
                    for active_patch in patches:
                        stack.enter_context(active_patch)
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.main.run_pure_test(timeline_only=True)
            finally:
                os.chdir(previous_cwd)

            prepare_profile.assert_called_once_with(
                target_channel_id=None, target_streamer="채널명"
            )


if __name__ == "__main__":
    unittest.main()
