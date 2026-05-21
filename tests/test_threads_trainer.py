import unittest

from threads_trainer import (
    DEFAULT_URL_TEMPLATE,
    collect_post_urls,
    parse_settings,
)


class SettingsTests(unittest.TestCase):
    def test_parse_settings_reads_posts_per_topic(self):
        settings = parse_settings(
            {
                "topics": "Python",
                "urlTemplate": DEFAULT_URL_TEMPLATE,
                "secondsPerTopic": 20,
                "scrollsPerTopic": 3,
                "cooldownSeconds": 1,
                "sessionMinutes": 5,
                "postsPerTopic": 4,
                "headless": False,
            }
        )

        self.assertEqual(settings.posts_per_topic, 4)

    def test_parse_settings_rejects_negative_posts_per_topic(self):
        with self.assertRaisesRegex(ValueError, "貼文"):
            parse_settings(
                {
                    "topics": "Python",
                    "postsPerTopic": -1,
                }
            )


class PostUrlTests(unittest.TestCase):
    def test_collect_post_urls_keeps_unique_threads_post_links(self):
        urls = collect_post_urls(
            [
                "https://www.threads.com/@alice/post/ABC123?x=1",
                "/@bob/post/DEF456",
                "https://www.threads.com/search?q=Python",
                "https://www.example.com/@mallory/post/NOPE",
                "/@alice/post/ABC123?duplicate=1",
            ],
            "https://www.threads.com/search?q=Python",
            limit=5,
        )

        self.assertEqual(
            urls,
            [
                "https://www.threads.com/@alice/post/ABC123",
                "https://www.threads.com/@bob/post/DEF456",
            ],
        )

    def test_collect_post_urls_respects_limit(self):
        urls = collect_post_urls(
            [
                "/@alice/post/ABC123",
                "/@bob/post/DEF456",
                "/@casey/post/GHI789",
            ],
            "https://www.threads.com/search?q=Python",
            limit=2,
        )

        self.assertEqual(len(urls), 2)


if __name__ == "__main__":
    unittest.main()
