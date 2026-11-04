"""Regression checks for B preview boundaries and event-loop availability."""
import asyncio
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pmkb.desktop.markdown_preview import SharedMarkdownPreview


class PreviewRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fence_closer_requires_same_character_length_and_blank_tail(self):
        renderer = SharedMarkdownPreview()
        for body, kept in [
            ('````\nline\n```', '```'),
            ('```\nline\n```tail', '```tail'),
            ('```\nline\n~~~', '~~~'),
        ]:
            html = await renderer.render(SimpleNamespace(collection_id='a'*32),
                                         SimpleNamespace(body=body), Event())
            self.assertIn(kept, html)
        html = await renderer.render(SimpleNamespace(collection_id='a'*32),
            SimpleNamespace(body='```\nline\n```'), Event())
        self.assertEqual(html, '<pre><code>line</code></pre>')

    async def test_render_leaves_service_loop_free_and_observes_cancel(self):
        started, release, cancel = Event(), Event(), Event()
        renderer = SharedMarkdownPreview()
        from pmkb.application.parsing.markdown import parse_markdown
        def slow(*args):
            started.set()
            release.wait(3)
            return parse_markdown(*args)
        with patch('pmkb.desktop.markdown_preview.parse_markdown', slow):
            task = asyncio.create_task(renderer.render(
                SimpleNamespace(collection_id='a'*32),
                SimpleNamespace(body='text `code`'), cancel))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                await asyncio.wait_for(asyncio.sleep(.001), .5)
                cancel.set()
            finally:
                release.set()
            self.assertEqual(await task, '')
