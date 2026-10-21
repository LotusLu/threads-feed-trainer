"""Observed Threads icon navigation verifies account evidence conservatively."""
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import async_playwright
import threads_trainer as app


class ProfileNavigationFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_svg_navigation_and_negative_evidence(self):
        async with async_playwright() as runtime:
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                await page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<html></html>'))
                await page.goto('https://www.threads.com/')
                managed=MagicMock();managed.page=page;managed.show_if_requested=AsyncMock()
                trainer=app.ThreadsTrainer(app.parse_settings({'topics':'AI'}),lambda _:None,app.StopSignal())
                selected='<a aria-current="page" role="link" tabindex="0" href="/">為你推薦</a>'
                icon='<svg aria-label="個人檔案" role="img" width="24" height="24" viewBox="0 0 24 24"><title>個人檔案</title><path d="M1 1L20 20"/></svg>'
                profile=f'<a role="link" tabindex="0" href="/@account">{icon}</a>'
                cases=[
                    (selected+profile,True),
                    (selected+'<a role="link" href="/@account">Account</a>',False),
                    (selected+profile.replace('<svg ','<svg style="display:none" '),False),
                    (selected+profile.replace('/@account','https://evil.example/@account'),False),
                    (selected+profile.replace('/@account','https://user:secret@threads.com/@account'),False),
                    (selected+profile.replace('/@account','javascript://threads.com/@account'),False),
                    (selected+'<article>'+profile+'</article>',False),
                    ('<a aria-current="page" role="link" href="/following">Following</a>'+profile,False),
                    (selected+profile+'<a href="/login">登入</a>',False),
                    (selected+profile+'<button>Log in</button>',False),
                ]
                for markup, expected in cases:
                    with self.subTest(markup=markup):
                        await page.set_content(markup)
                        self.assertEqual(await trainer._verify_for_you(managed,time.monotonic()+.15),expected)
            finally:
                await browser.close()
