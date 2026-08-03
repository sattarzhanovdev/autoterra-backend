"""Ссылки на видео и файлы в обучающих материалах.

Поле принимает адрес, а из YouTube по кнопке «Поделиться → Встроить» приходит
целый <iframe>. Отбивать форму ошибкой из-за этого не стоит — вытаскиваем src.
"""

from django.test import TestCase

from .admin import LearningMaterialForm, extract_url

EMBED = (
    '<iframe width="560" height="315" '
    'src="https://www.youtube.com/embed/dQw4w9WgXcQ?si=abc" '
    'title="YouTube video player" frameborder="0" allowfullscreen></iframe>'
)


class ExtractUrlTests(TestCase):
    def test_plain_url_is_left_alone(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        self.assertEqual(extract_url(url), url)

    def test_src_is_taken_from_iframe(self):
        self.assertEqual(
            extract_url(EMBED), "https://www.youtube.com/embed/dQw4w9WgXcQ?si=abc"
        )

    def test_single_quotes_in_iframe_work_too(self):
        self.assertEqual(
            extract_url("<iframe src='https://rutube.ru/video/abc/'></iframe>"),
            "https://rutube.ru/video/abc/",
        )

    def test_protocol_relative_link_gets_https(self):
        # В HTML //host/path валиден, а URLField такое не принимает.
        self.assertEqual(extract_url("//youtu.be/abc"), "https://youtu.be/abc")

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(extract_url("  https://a.co/v  "), "https://a.co/v")

    def test_empty_stays_empty(self):
        self.assertEqual(extract_url(""), "")
        self.assertEqual(extract_url(None), "")


class LearningMaterialFormTests(TestCase):
    def _form(self, **extra):
        data = {
            "title": "Подготовка поверхности",
            "kind": "video",
            "status": "published",
            "category": "Покраска",
            "summary": "",
            "body": "",
            "video_url": "",
            "file_url": "",
        }
        data.update(extra)
        return LearningMaterialForm(data=data)

    def test_pasted_embed_code_is_accepted(self):
        form = self._form(video_url=EMBED)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["video_url"],
            "https://www.youtube.com/embed/dQw4w9WgXcQ?si=abc",
        )

    def test_ordinary_link_still_works(self):
        form = self._form(video_url="https://youtu.be/dQw4w9WgXcQ")

        self.assertTrue(form.is_valid(), form.errors)

    def test_video_link_is_optional(self):
        # Чек-лист или инструкция обходятся без видео.
        self.assertTrue(self._form(kind="checklist").is_valid())

    def test_real_garbage_is_still_rejected(self):
        """Терпимость к <iframe> не должна пропускать что попало."""
        form = self._form(video_url="это не ссылка")

        self.assertFalse(form.is_valid())
        self.assertIn("video_url", form.errors)

    def test_file_link_is_normalized_as_well(self):
        form = self._form(file_url="  https://files.example.com/manual.pdf ")

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["file_url"], "https://files.example.com/manual.pdf"
        )
