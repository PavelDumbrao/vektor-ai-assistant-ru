import unittest

from ai_fixer_social.post_validation import validate_post, visible_text


class PostValidationTests(unittest.TestCase):
    def test_valid_html(self):
        self.assertEqual(validate_post("<b>Заголовок</b>\n\nТекст"), [])

    def test_visible_text(self):
        self.assertEqual(visible_text("<b>Привет</b> &amp; пока"), "Привет & пока")

    def test_long_dash_is_rejected(self):
        self.assertIn("long_dash_forbidden", validate_post("Текст — продолжение"))

    def test_unsafe_link_is_rejected(self):
        errors = validate_post('<a href="javascript:alert(1)">Клик</a>')
        self.assertIn("unsafe_html", errors)
        self.assertIn("unsafe_link", errors)

    def test_unbalanced_html_is_rejected(self):
        self.assertTrue(validate_post("<b>Текст"))

    def test_photo_caption_limit(self):
        errors = validate_post("я" * 1025, has_photo=True)
        self.assertIn("visible_text_too_long:1025>1024", errors)

    def test_text_limit(self):
        errors = validate_post("я" * 4097)
        self.assertIn("visible_text_too_long:4097>4096", errors)


if __name__ == "__main__":
    unittest.main()

