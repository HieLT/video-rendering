"""Regression for downloading videos with a scene name."""
import unittest

from video_worker import sanitize_filename_prefix


class VideoFilenameTests(unittest.TestCase):
    def test_named_scene(self):
        self.assertEqual(sanitize_filename_prefix(' scene 2 '), 'scene 2_')

    def test_windows_reserved_characters(self):
        self.assertEqual(sanitize_filename_prefix('scene/2: take?'), 'scene_2 take_')
        self.assertEqual(sanitize_filename_prefix('scene\\3'), 'scene_3_')

    def test_unnamed_scene(self):
        self.assertEqual(sanitize_filename_prefix(''), '')
        self.assertEqual(sanitize_filename_prefix(None), '')


if __name__ == '__main__':
    unittest.main()
