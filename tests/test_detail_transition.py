"""SPA post transitions must settle before hydrated search cards count as detail."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from playwright.async_api import async_playwright
import threads_trainer as app
from trainer_content import ContentStore


class DetailTransitionFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_spa_url_and_body_then_no_transition_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            async with async_playwright() as runtime:
                browser=await runtime.chromium.launch(headless=True)
                try:
                    page=await browser.new_page()
                    await page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<html></html>'))
                    await page.goto('https://www.threads.com/search?q=AI')
                    managed=MagicMock();managed.page=page;managed.show_if_requested=AsyncMock()
                    logs=[]
                    trainer=app.ThreadsTrainer(app.parse_settings({'topics':'AI'}),logs.append,app.StopSignal())
                    trainer.content=ContentStore(Path(directory)/'content.json')
                    trainer.navigate=AsyncMock(return_value=False)
                    await page.set_content('''<article><a href="/@a/post/A" onclick="event.preventDefault();document.querySelector('[data-post-text]').textContent='';setTimeout(()=>history.pushState({},'', '/@a/post/A'),120);setTimeout(()=>{document.querySelector('[data-post-text]').textContent='AI detail ready';window.detailReady=true},350)"><time>1h</time></a><div data-post-text>AI search card already hydrated</div></article>''')
                    original_pause=trainer.pause
                    actual_reading=[]
                    async def pause(seconds,deadline):
                        if seconds >= 4:
                            actual_reading.append(await page.evaluate('() => Boolean(window.detailReady)'))
                            return True
                        return await original_pause(seconds,deadline)
                    trainer.pause=pause
                    started=time.monotonic()
                    card={'id':'A','url':'https://www.threads.com/@a/post/A','text':'AI search card already hydrated'}
                    self.assertTrue(await trainer._read_card(managed,card,['AI'],[],started+2))
                    self.assertGreaterEqual(time.monotonic()-started,.3)
                    self.assertEqual(actual_reading,[True])
                    self.assertTrue(any('完成閱讀' in log for log in logs))
                    self.assertTrue(trainer.content.entry('A'))
                    self.assertEqual(trainer.stats['errors'],0)
                    self.assertGreater(managed.show_if_requested.await_count,2)
                    # A hydrated same-ID search result is insufficient without the
                    # actual post URL transition, even after clicking its permalink.
                    await page.goto('https://www.threads.com/search?q=AI')
                    await page.set_content('''<article><a href="/@b/post/B" onclick="event.preventDefault()"><time>1h</time></a><div data-post-text>AI search result</div></article>''')
                    logs.clear()
                    original_wait=trainer._wait_for_post_detail
                    trainer._wait_for_post_detail=lambda browser,post_id,deadline:original_wait(browser,post_id,deadline,timeout=.25)
                    card={'id':'B','url':'https://www.threads.com/@b/post/B','text':'AI search result'}
                    self.assertFalse(await trainer._read_card(managed,card,['AI'],[],time.monotonic()+2))
                    self.assertTrue(any('網址不是這篇貼文' in log for log in logs))
                    self.assertFalse(any('完成閱讀' in log for log in logs))
                    self.assertFalse(trainer.content.entry('B'))
                    self.assertEqual(trainer.stats['errors'],1)
                    # Stop and topic/session deadlines end the transition wait too.
                    trainer.stop_signal.stop()
                    self.assertIsNone(await original_wait(managed,'B',time.monotonic()+2))
                    trainer.stop_signal=app.StopSignal()
                    trainer.deadline=time.monotonic()-1
                    self.assertIsNone(await original_wait(managed,'B',time.monotonic()+2))
                finally:
                    await browser.close()


class ScanningPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_representative_per_view_without_duplicate_preview_spam(self):
        with tempfile.TemporaryDirectory() as directory:
            logs=[]
            trainer=app.ThreadsTrainer(app.parse_settings({'topics':'AI','scrollsPerTopic':3}),logs.append,app.StopSignal())
            trainer.content=ContentStore(Path(directory)/'c.json')
            for post_id in ('A','B'):trainer.content.read(f'https://threads.com/@a/post/{post_id}')
            trainer.cards=AsyncMock(return_value=[{'id':post_id,'url':f'https://threads.com/@a/post/{post_id}','text':'Previously read AI '+('正文 '*200)} for post_id in ('A','B')])
            trainer.pause=AsyncMock(return_value=True)
            browser=MagicMock();browser.show_if_requested=AsyncMock();browser.page.mouse.wheel=AsyncMock()
            await trainer._browse_topic(browser,'AI',time.monotonic()+1)
            previews=[log for log in logs if '檢視搜尋結果' in log]
            self.assertEqual(len(previews),2)
            self.assertTrue(all('已讀略過' in log for log in previews))
            self.assertTrue(all(len(log)<350 for log in previews))
            self.assertEqual(trainer.stats['duplicates'],6)
            self.assertEqual(trainer.stats['read'],0)
            self.assertEqual(trainer.stats['opened'],0)
            self.assertEqual(trainer.stats['candidates'],0)

    async def test_unmatched_preview_and_no_extra_preview_when_opening(self):
        with tempfile.TemporaryDirectory() as directory:
            logs=[]
            trainer=app.ThreadsTrainer(app.parse_settings({'topics':'AI','scrollsPerTopic':1}),logs.append,app.StopSignal())
            trainer.content=ContentStore(Path(directory)/'c.json')
            trainer.cards=AsyncMock(return_value=[{'id':'A','url':'https://threads.com/@a/post/A','text':'Taiwan hiking'}])
            trainer.pause=AsyncMock(return_value=True)
            browser=MagicMock();browser.show_if_requested=AsyncMock();browser.page.mouse.wheel=AsyncMock()
            await trainer._browse_topic(browser,'AI',time.monotonic()+1)
            self.assertEqual(sum('檢視搜尋結果／未符合主題' in log for log in logs),1)
            self.assertEqual(trainer.stats['matched'],0)
            logs.clear()
            trainer.cards=AsyncMock(return_value=[{'id':'B','url':'https://threads.com/@a/post/B','text':'AI content'}])
            trainer._read_card=AsyncMock(return_value=False)
            await trainer._browse_topic(browser,'AI',time.monotonic()+1)
            self.assertFalse(any('檢視搜尋結果' in log for log in logs))
            self.assertEqual(trainer.stats['read'],0)
