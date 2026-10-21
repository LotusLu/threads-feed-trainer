"""Regression coverage for independent privacy controls and bounded post activity."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

import threads_trainer as app
from trainer_content import ContentStore


class ResultsPrivacyTests(unittest.TestCase):
    def test_old_hidden_all_migrates_results_hidden_and_new_controls_roundtrip(self):
        for legacy, expected in (({},False),({'topicsCollapsed':True},False),({'logCollapsed':True},False),({'topicsCollapsed':True,'logCollapsed':True},True)):
            self.assertEqual(app.SettingsStore._sanitize_ui(legacy)['reportsCollapsed'],expected)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json';store=app.SettingsStore(path)
            for ui in ({'topicsCollapsed':False,'logCollapsed':True,'reportsCollapsed':False}, {'topicsCollapsed':False,'logCollapsed':False,'reportsCollapsed':True}, {'topicsCollapsed':True,'logCollapsed':True,'reportsCollapsed':False}):
                store.save(ui=ui,draft_present=False)
                self.assertEqual(app.SettingsStore(path).snapshot()['ui'],ui)
            self.assertEqual(store.snapshot()['version'],1)
            with self.assertRaises(ValueError):store.save(ui={'reportsCollapsed':'true'},draft_present=False)



class ActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.logs=[]
        self.trainer=app.ThreadsTrainer(app.parse_settings({'topics':'AI'}),self.logs.append,app.StopSignal())
        self.trainer.current_topic='AI'
        self.trainer.content=ContentStore(Path(self.directory.name)/'content.json')
        self.card={'id':'A','url':'https://threads.net/@a/post/A?tracking=private','text':'AI '+('正文 '*200)}
        self.browser=MagicMock();self.browser.show_if_requested=AsyncMock()
        self.browser.page.url='https://threads.com/search?q=AI'
        self.browser.page.evaluate=AsyncMock(return_value={'x':0,'y':100})
        locator=MagicMock();locator.count=AsyncMock(return_value=1)
        anchor=MagicMock();anchor.is_visible=AsyncMock(return_value=True);anchor.get_attribute=AsyncMock(return_value='/@a/post/A')
        async def click(**_):self.browser.page.url='https://threads.com/@a/post/A'
        anchor.click=AsyncMock(side_effect=click);locator.nth.return_value=anchor
        self.browser.page.locator.return_value=locator
        self.trainer.cards=AsyncMock(return_value=[{'id':'A','text':self.card['text']}])
        self.trainer.navigate=AsyncMock(return_value=False)
        self.trainer.pause=AsyncMock(return_value=True)

    async def test_completed_lifecycle_uses_actual_url_and_bounded_own_preview(self):
        self.assertTrue(await self.trainer._read_card(self.browser,self.card,['AI'],[],time.monotonic()+5))
        for phrase in ('準備開啟貼文','已開啟貼文','正文已就緒，開始閱讀','完成閱讀'):
            self.assertTrue(any(phrase in log for log in self.logs),phrase)
        self.assertTrue(self.trainer.content.entry('A'))
        self.assertTrue(all('tracking=private' not in log for log in self.logs))
        self.assertTrue(all(len(log)<350 for log in self.logs))
        self.assertTrue(all('\n' not in log for log in self.logs))
        self.assertTrue(all('https://threads.net/@a/post/A' in log for log in self.logs))

    async def test_stop_during_dwell_is_not_completed_or_persisted(self):
        async def stop(*_):self.trainer.stop_signal.stop();return False
        self.trainer.pause=AsyncMock(side_effect=stop)
        self.assertFalse(await self.trainer._read_card(self.browser,self.card,['AI'],[],time.monotonic()+5))
        self.assertTrue(any('已收到停止要求' in log for log in self.logs))
        self.assertFalse(any('完成閱讀' in log for log in self.logs))
        self.assertFalse(self.trainer.content.entry('A'))
        self.assertNotIn('A',self.trainer.read_ids)
        self.assertEqual(self.trainer.stats['errors'],0)

    async def test_missing_link_changed_body_and_missing_detail_have_readable_skip(self):
        self.browser.page.locator.return_value.count=AsyncMock(return_value=0)
        self.assertFalse(await self.trainer._read_card(self.browser,self.card,['AI'],[],time.monotonic()+5))
        self.assertTrue(any('無法點擊' in log for log in self.logs))
        self.browser.page.locator.return_value.count=AsyncMock(return_value=1)
        self.trainer._wait_for_post_detail=AsyncMock(return_value=None)
        self.assertFalse(await self.trainer._read_card(self.browser,self.card,['AI'],[],time.monotonic()+5))
        self.assertTrue(any('正文尚未載入' in log for log in self.logs))
        self.trainer._wait_for_post_detail=AsyncMock(return_value={'id':'A','text':'other topic'})
        self.assertFalse(await self.trainer._read_card(self.browser,self.card,['AI'],[],time.monotonic()+5))
        self.assertTrue(any('不再符合' in log for log in self.logs))
        self.assertEqual(self.trainer.stats['errors'],2)
        self.assertEqual(self.trainer.stats['excluded'],1)
        self.assertFalse(self.trainer.content.entry('A'))
        self.assertFalse(any('完成閱讀' in log for log in self.logs))

    async def test_expired_budget_skips_without_open_or_completion(self):
        self.assertFalse(await self.trainer._read_card(self.browser,self.card,['AI'],[],time.monotonic()-1))
        self.assertTrue(any('時間已用完' in log for log in self.logs))
        self.browser.show_if_requested.assert_not_awaited()
        self.assertFalse(self.trainer.content.entry('A'))
