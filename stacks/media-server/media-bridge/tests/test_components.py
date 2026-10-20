import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import components as main  # noqa: E402


class EnabledComponents(unittest.TestCase):
    def test_default_is_everything(self):
        self.assertEqual(main.enabled_components(""), main.COMPONENTS)
        self.assertEqual(main.enabled_components("  "), main.COMPONENTS)

    def test_subset_keeps_canonical_order(self):
        self.assertEqual(main.enabled_components("youtube, movierulz"), ["movierulz", "youtube"])

    def test_dev_profile_has_no_bridge_or_reaper(self):
        got = main.enabled_components("movierulz,tamilmv,youtube")
        self.assertNotIn("bridge", got)
        self.assertNotIn("reaper", got)

    def test_unknown_names_are_ignored(self):
        self.assertEqual(main.enabled_components("bridge,qbittorrent"), ["bridge"])


if __name__ == "__main__":
    unittest.main()
