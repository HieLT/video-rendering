import unittest

from video_worker_ui import _model_menu_item_matches


class ModelMenuMatchingTests(unittest.TestCase):
    def test_localized_seedance_20_fast_option_matches(self):
        self.assertTrue(_model_menu_item_matches("Dreamina Seedance 2.0??", "seedance_v2.0"))
        self.assertTrue(_model_menu_item_matches("Dreamina Seedance 2.0 Fast", "seedance_v2.0"))
        self.assertTrue(_model_menu_item_matches("Seedance2.0Fast", "seedance_v2.0"))

    def test_does_not_confuse_versions(self):
        self.assertFalse(_model_menu_item_matches("Dreamina Seedance 2.5", "seedance_v2.0"))
        self.assertFalse(_model_menu_item_matches("Dreamina Seedance 2.0??", "seedance_v2.5"))
        self.assertTrue(_model_menu_item_matches("Dreamina Seedance 2.5", "seedance_v2.5"))


if __name__ == "__main__":
    unittest.main()
