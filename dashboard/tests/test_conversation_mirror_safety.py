"""Regression tests for customer-controlled conversation rendering."""

import html
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock


MODULE_PATH = (
    Path(__file__).parents[1]
    / "app"
    / "pages"
    / "hub"
    / "conversation_mirror.py"
)


class ConversationMirrorSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.streamlit = types.ModuleType("streamlit")
        cls.streamlit.markdown = Mock()

        auth = types.ModuleType("utils.auth")
        auth.api_get = Mock()
        auth.api_post = Mock()
        auth.api_put = Mock()

        sys.modules["streamlit"] = cls.streamlit
        sys.modules["pandas"] = types.ModuleType("pandas")
        sys.modules["utils"] = types.ModuleType("utils")
        sys.modules["utils.auth"] = auth

        spec = importlib.util.spec_from_file_location(
            "conversation_mirror_under_test",
            MODULE_PATH,
        )
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        self.streamlit.markdown.reset_mock()

    def test_customer_html_is_escaped_at_unsafe_rendering_boundary(self):
        payloads = [
            "<script>alert(1)</script>",
            "<style>body { display: none; }</style>",
            '<img src=x onerror="alert(1)">',
            "<b>ordinary HTML tags</b>",
        ]

        for content in payloads:
            with self.subTest(content=content):
                self.streamlit.markdown.reset_mock()
                self.module._render_message_bubble(
                    {
                        "direction": "inbound",
                        "content": content,
                        "language": "<style>bad</style>",
                        "timestamp": "2026-07-27T12:00:00Z",
                    }
                )

                rendered = self.streamlit.markdown.call_args.args[0]
                self.assertNotIn(content, rendered)
                self.assertIn(html.escape(content), rendered)
                self.assertNotIn("<style>bad</style>", rendered)
                self.assertIn("&lt;style&gt;bad&lt;/style&gt;", rendered)
                self.assertTrue(
                    self.streamlit.markdown.call_args.kwargs["unsafe_allow_html"]
                )

    def test_outbound_content_uses_the_same_safe_boundary(self):
        content = '<img src=x onerror="alert(1)">'

        self.module._render_message_bubble(
            {"direction": "outbound", "content": content}
        )

        rendered = self.streamlit.markdown.call_args.args[0]
        self.assertNotIn(content, rendered)
        self.assertIn(html.escape(content), rendered)


if __name__ == "__main__":
    unittest.main()
