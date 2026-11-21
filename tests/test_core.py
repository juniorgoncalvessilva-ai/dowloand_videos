import tempfile
import unittest
from pathlib import Path

from core import (
    Job,
    UrlError,
    _base_opts,
    copy_even_if_locked,
    ffmpeg_path,
    format_selector,
    is_cookie_lock_error,
    is_soft_download_error,
    parse_youtube_url,
    safe_name,
    vosk_lang_key,
    vtt_to_text,
)


class UrlTests(unittest.TestCase):
    def test_playlist_from_watch_link(self):
        parsed = parse_youtube_url(
            "https://www.youtube.com/watch?v=6D9TZyQxrC0&list=PLDLUTtB_bQle_E4j08aTGlbMW1EKMKAbE&index=1"
        )
        self.assertEqual(parsed["kind"], "playlist")
        self.assertEqual(parsed["playlist_id"], "PLDLUTtB_bQle_E4j08aTGlbMW1EKMKAbE")
        self.assertEqual(parsed["video_id"], "6D9TZyQxrC0")

    def test_second_playlist(self):
        parsed = parse_youtube_url(
            "https://www.youtube.com/watch?v=FnUFFwAlFHI&list=PLU_SHzaZfpz_9bRJ7G6bRjRho_MFzTJnX"
        )
        self.assertEqual(parsed["playlist_id"], "PLU_SHzaZfpz_9bRJ7G6bRjRho_MFzTJnX")

    def test_rejects_non_youtube(self):
        with self.assertRaises(UrlError):
            parse_youtube_url("https://vimeo.com/123")

    def test_safe_name_strips_windows_chars(self):
        self.assertEqual(safe_name('Aula 01: "solda"?'), "Aula 01 solda")

    def test_vtt_dedupes_auto_captions(self):
        vtt = """WEBVTT

00:00:00.000 --> 00:00:01.000
Olá

00:00:01.000 --> 00:00:02.000
Olá
mundo
"""
        self.assertEqual(vtt_to_text(vtt), "Olá\nmundo")

    def test_format_without_ffmpeg_avoids_merge(self):
        self.assertNotIn("+", format_selector("1080", False))

    def test_format_default_is_best(self):
        self.assertEqual(format_selector("nope", True), format_selector("best", True))
        self.assertIn("bv*", format_selector("best", True))

    def test_subtitle_429_is_soft_error(self):
        self.assertTrue(is_soft_download_error(RuntimeError(
            "ERROR: Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests"
        )))

    def test_subtitles_opt_toggled(self):
        job = Job(id="t", title="t")
        on = _base_opts(Path("."), "best", job, subtitles=True, languages=["pt"])
        off = _base_opts(Path("."), "best", job, subtitles=False)
        self.assertTrue(on.get("writesubtitles"))
        self.assertTrue(on.get("writeautomaticsub"))
        self.assertNotIn("writesubtitles", off)

    def test_ffmpeg_path_finds_local_install(self):
        self.assertTrue(ffmpeg_path())

    def test_vosk_lang_defaults_to_portuguese(self):
        self.assertEqual(vosk_lang_key("pt-BR"), "pt")
        self.assertEqual(vosk_lang_key("en"), "en")
        self.assertEqual(vosk_lang_key(None), "pt")
        self.assertTrue(is_cookie_lock_error(PermissionError(
            "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"
        )))
        self.assertFalse(is_cookie_lock_error(RuntimeError("HTTP 403")))

    def test_copy_even_if_locked_plain_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Cookies"
            dst = Path(tmp) / "copy" / "Cookies"
            src.write_bytes(b"sqlite-fake")
            copy_even_if_locked(src, dst)
            self.assertEqual(dst.read_bytes(), b"sqlite-fake")


if __name__ == "__main__":
    unittest.main()
