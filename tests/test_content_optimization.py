import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import threads_trainer as app
from trainer_content import (ContentStore, canonical_post, classify, keywords,
                             fingerprint, compare_reports, EXTRACT_POSTS_JS)


def report(phase='before', **changes):
    return dict(runId='session', phase=phase, time='2026-10-20T00:00:00+00:00',
                fingerprint='same', verifiedForYou=True, status='complete',
                sampled=5, classifiable=5, matched=2, unknown=0, ads=0,
                overlap=0, target=5, rate=40.0, delta=None,
                ids=['P'+str(i) for i in range(5)], **changes)


class RulesTests(unittest.TestCase):
    def test_nfkc_casefold_words_phrases_and_exclusion(self):
        self.assertEqual(classify('Ｔａｉｗａｎ', ['AI'], []), 'unmatched')
        self.assertEqual(classify('ＡＩ 機器學習心得', ['ai'], []), 'matched')
        self.assertEqual(classify('機器學習心得', ['學習'], []), 'matched')
        self.assertEqual(classify('machine learning', ['machine learning'], []), 'matched')
        self.assertEqual(classify('machine  learning', ['machine learning'], []), 'unmatched')
        self.assertEqual(classify('AI ads', ['AI'], ['ads'], 'positive'), 'excluded')
        self.assertEqual(classify('AI', ['AI'], [], 'negative'), 'excluded')
        self.assertEqual(classify('', ['AI'], [], 'positive'), 'unknown')
        self.assertEqual(keywords('AI\nａｉ\n'), ['ai'])

    def test_url_identity_is_cross_domain_and_rejects_bad_hosts(self):
        self.assertEqual(canonical_post('https://threads.net/@a/post/ABC?x=1')[0], canonical_post('https://www.threads.com/@b/post/ABC')[0])
        for url in ('https://user@threads.com/@a/post/ABC', 'https://threads.com.evil/@a/post/A', 'https://threads.com/search?q=/post/A', 'javascript:hi', 'https://threads.com:999/@a/post/A', 'https://threads.com/@a/post/A/replies'):
            self.assertIsNone(canonical_post(url))

    def test_comparison_requires_same_run_rules_and_minimum(self):
        before = report()
        after = report('after');after['rate'] = 60
        self.assertEqual(compare_reports(before, after), 20)
        for key, value in (('runId','other'), ('fingerprint','other'), ('verifiedForYou', False), ('classifiable',4), ('status','unavailable')):
            changed = dict(after);changed[key] = value
            self.assertIsNone(compare_reports(before, changed))
        self.assertEqual(fingerprint(['AI','中文'], []), fingerprint(['中文','ai'], []))

    def test_new_settings_defaults_validation_and_old_url_fallback(self):
        settings = app.parse_settings({'topics':'AI'})
        self.assertTrue(settings.feed_check)
        self.assertEqual(settings.feed_sample_size,20)
        for payload in ({'feedCheck':'false'}, {'feedSampleSize':True}, {'feedSampleSize':51}, {'includeKeywords':[]}, {'excludeKeywords':'x'*50001}):
            with self.assertRaises(ValueError):
                app.parse_settings({'topics':'AI', **payload})
        trainer = app.ThreadsTrainer(app.parse_settings({'topics':'AI','urlTemplate':'https://threads.com/'}),lambda _:None,app.StopSignal())
        self.assertIn('search?q=AI',trainer._topic_url('AI'))


class HistoryTests(unittest.TestCase):
    def test_roundtrip_labels_private_bounded_and_run_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'content.json'
            store=ContentStore(path)
            store.read('https://threads.net/@user/post/A')
            store.feedback('A','negative')
            store.report('before', report())
            loaded=ContentStore(path)
            self.assertEqual(loaded.entry('A')['feedback'],'negative')
            self.assertEqual(loaded.snapshot()['qualityLabels']['negative'],1)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            loaded.feedback('A','clear')
            self.assertTrue(loaded.entry('A'))
            loaded.start_run()
            self.assertEqual(loaded.snapshot()['reports'],{})
            self.assertTrue(loaded.entry('A'))
            # Exercise count eviction without 5,000 fsyncs.
            loaded.data['posts']={str(i):dict(id=str(i),url=f'https://threads.com/@a/post/{i}',readAt=f'{i:08}') for i in range(5000)}
            loaded.read('https://threads.com/@a/post/new')
            self.assertEqual(len(loaded.data['posts']),5000)
            self.assertNotIn('0',loaded.data['posts'])

    def test_corrupt_or_invalid_report_preserved_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'content.json';path.write_text('broken')
            store=ContentStore(path)
            self.assertTrue(store.error)
            with self.assertRaises(OSError):store.read('https://threads.com/@a/post/A')
            self.assertEqual(path.read_text(),'broken')
            path.unlink();store=ContentStore(path);store.read('https://threads.com/@a/post/A')
            original=path.read_bytes()
            with patch('trainer_content.os.replace',side_effect=OSError('disk')):
                with self.assertRaises(OSError):store.feedback('A','positive')
            self.assertEqual(path.read_bytes(),original)
            self.assertIsNone(store.entry('A').get('feedback'))
            data=json.loads(original);data['reports']={'before':{'sampled':'bad'}}
            path.write_text(json.dumps(data));before=path.read_bytes()
            broken=ContentStore(path)
            self.assertTrue(broken.error)
            with self.assertRaises(OSError):broken.start_run()
            self.assertEqual(path.read_bytes(),before)

    def test_invalid_feedback_http_rejected_without_arbitrary_url_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            store=ContentStore(Path(directory)/'c.json');store.read('https://threads.com/@a/post/A')
            with patch.object(app,'CONTENT',store):
                server=ThreadingHTTPServer(('127.0.0.1',0),app.RequestHandler)
                threading.Thread(target=server.serve_forever,daemon=True).start()
                try:
                    for payload, expected in (({'id':'A','value':'positive'},200),({'id':'A','value':'clear'},200),({'id':'A','value':'bad'},400),({'id':'missing','value':'negative'},400),({'id':[],'value':'clear'},400),({'id':'A','value':[]},400)):
                        req=Request(f'http://127.0.0.1:{server.server_port}/api/feedback',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
                        try:response=urlopen(req)
                        except HTTPError as exc:response=exc
                        with response:self.assertEqual(response.status,expected)
                finally:server.shutdown();server.server_close()


class BrowserBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def trainer(self, **settings):
        return app.ThreadsTrainer(app.parse_settings({'topics':'AI\n中文',**settings}),lambda _:None,app.StopSignal())

    async def test_stop_and_expired_deadline_skip_navigation_and_dwell(self):
        trainer=self.trainer();browser=MagicMock();browser.show_if_requested=AsyncMock();browser.page.goto=AsyncMock()
        trainer.deadline=time.monotonic()-1
        self.assertFalse(await trainer.navigate(browser,app.THREADS_HOME,trainer.deadline))
        browser.page.goto.assert_not_awaited()
        self.assertFalse(await trainer.pause(4,trainer.deadline))
        trainer.deadline=float('inf');trainer.stop_signal.stop()
        self.assertFalse(await trainer.pause(4,float('inf')))

    async def test_hydration_timeout_retains_isolated_unknown_cards(self):
        trainer=self.trainer()
        browser=MagicMock();browser.show_if_requested=AsyncMock()
        browser.page.evaluate=AsyncMock(return_value=[{'url':'https://threads.com/@a/post/A','text':'','sponsored':False}])
        cards=await trainer.cards(browser,time.monotonic()+.02,wait=True)
        self.assertEqual(cards[0]['id'],'A')
        self.assertEqual(cards[0]['text'],'')

    async def test_sampling_unverified_is_unavailable_and_final_overlap_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            trainer=self.trainer(feedSampleSize=1);trainer.content=ContentStore(Path(directory)/'c.json')
            trainer.navigate=AsyncMock(return_value=True);trainer._verify_for_you=AsyncMock(return_value=False)
            browser=MagicMock();browser.show_if_requested=AsyncMock();browser.page.mouse.wheel=AsyncMock()
            before=await trainer._sample_feed(browser,'before',time.monotonic()+1)
            self.assertFalse(before['verifiedForYou']);self.assertIsNone(before['rate'])
            trainer._verify_for_you=AsyncMock(return_value=True)
            trainer.final_ids={'old'}
            trainer.cards=AsyncMock(return_value=[{'id':'old','text':'AI','url':'https://threads.com/@a/post/old'}, {'id':'new','text':'','url':'https://threads.com/@a/post/new'}])
            after=await trainer._sample_feed(browser,'after',time.monotonic()+1)
            self.assertEqual(after['overlap'],1);self.assertEqual(after['sampled'],1)
            self.assertEqual(after['unknown'],1);self.assertIsNone(after['rate']);self.assertIsNone(after['delta'])

    async def test_unmatched_does_not_block_other_topic_and_unknown_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            trainer=self.trainer(scrollsPerTopic=1);trainer.content=ContentStore(Path(directory)/'c.json')
            browser=MagicMock();browser.show_if_requested=AsyncMock();browser.page.mouse.wheel=AsyncMock()
            trainer.cards=AsyncMock(return_value=[{'id':'A','url':'https://threads.com/@a/post/A','text':'中文'}])
            trainer.pause=AsyncMock(return_value=True);trainer._read_card=AsyncMock(return_value=True)
            await trainer._browse_topic(browser,'AI',time.monotonic()+1)
            trainer._read_card.assert_not_awaited()
            await trainer._browse_topic(browser,'中文',time.monotonic()+1)
            trainer._read_card.assert_awaited_once()
            trainer.cards=AsyncMock(return_value=[{'id':'B','url':'https://threads.com/@a/post/B','text':''}])
            await trainer._browse_topic(browser,'AI',time.monotonic()+1)
            self.assertNotIn(('AI','B'),trainer.considered)

    async def test_completed_read_dedupes_even_when_store_write_fails(self):
        trainer=self.trainer();browser=MagicMock();browser.show_if_requested=AsyncMock()
        browser.page.url='https://threads.com/search?q=AI'
        browser.page.evaluate=AsyncMock(return_value={'x':0,'y':0})
        anchor=MagicMock();anchor.get_attribute=AsyncMock(return_value='/@a/post/A');anchor.is_visible=AsyncMock(return_value=True)
        async def click(**_):browser.page.url='https://threads.com/@a/post/A'
        anchor.click=AsyncMock(side_effect=click)
        locator=MagicMock();locator.count=AsyncMock(return_value=1);locator.nth.return_value=anchor;browser.page.locator.return_value=locator
        trainer.cards=AsyncMock(return_value=[{'id':'A','text':'AI'}]);trainer.pause=AsyncMock(return_value=True);trainer.navigate=AsyncMock(return_value=False)
        trainer.content=MagicMock();trainer.content.entry.return_value={};trainer.content.read.side_effect=OSError('disk')
        self.assertTrue(await trainer._read_card(browser,{'id':'A','url':'https://threads.com/@a/post/A'},['AI'],[],time.monotonic()+1))
        self.assertIn('A',trainer.read_ids)
        trainer.pause=AsyncMock(return_value=False)
        self.assertFalse(await trainer._read_card(browser,{'id':'B','url':'https://threads.com/@a/post/B'},['AI'],[],time.monotonic()+1))
        self.assertNotIn('B',trainer.read_ids)

    async def test_run_gates_final_sample_on_stop_and_feed_disabled(self):
        trainer=self.trainer();browser=MagicMock();browser.start=AsyncMock();browser.close=AsyncMock()
        trainer._sample_feed=AsyncMock(return_value={})
        async def stop(*_):trainer.stop_signal.stop()
        trainer._cycle_topics=AsyncMock(side_effect=stop)
        runtime=MagicMock();runtime.__aenter__=AsyncMock(return_value=MagicMock());runtime.__aexit__=AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            trainer.content=ContentStore(Path(directory)/'c.json')
            with patch.object(app,'PROFILE_DIR',Path(directory)/'profile'),patch.object(app,'ManagedBrowser',return_value=browser),patch.object(app,'async_playwright',return_value=runtime):
                await trainer.run()
        self.assertEqual(trainer._sample_feed.await_count,1)
        self.assertEqual(trainer._sample_feed.await_args.args[1],'before')


class ExtractorFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_js_extracts_isolated_visible_own_text_and_ads(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as runtime:
            browser=await runtime.chromium.launch(headless=True)
            try:
                page=await browser.new_page(viewport={'width':1000,'height':800})
                await page.set_content('''<nav><a href="https://threads.com/@nav/post/N">Nav</a></nav>
                <article><a href="https://threads.com/@a/post/A"><time>1h</time></a><div data-post-text>閱讀 <a href="https://example.com">機器學習</a> 技術心得</div><button>menu</button></article>
                <article style="display:none"><a href="https://threads.com/@b/post/B">B</a><div dir="auto">hidden AI</div></article>
                <article><a href="https://threads.com/@c/post/C">C</a><div dir="auto">talking about ads <span>廣告</span></div></article>
                <article><a href="https://threads.com/@d/post/D">D</a><span aria-label="Sponsored">Sponsored</span><div dir="auto">AI</div></article>
                <article><a href="https://threads.com/@e/post/E">E</a><a href="https://threads.com/@f/post/F">F</a><div dir="auto">mixed AI</div></article>
                <article><a href="https://threads.com/@u/post/U">U</a></article>
                <article style="position:absolute;top:2000px"><a href="https://threads.com/@g/post/G">G</a><div dir="auto">offscreen AI</div></article>''')
                cards=await page.evaluate(EXTRACT_POSTS_JS)
                by_id={canonical_post(c['url'])[0]:c for c in cards}
                self.assertEqual(set(by_id),{'A','C','D','U'})
                self.assertIn('機器學習',by_id['A']['text']);self.assertNotIn('menu',by_id['A']['text'])
                self.assertFalse(by_id['C']['sponsored']);self.assertTrue(by_id['D']['sponsored'])
                self.assertEqual(by_id['U']['text'],'')
                trainer = app.ThreadsTrainer(app.parse_settings({'topics':'AI'}), lambda _:None, app.StopSignal())
                managed=MagicMock();managed.page=page;managed.show_if_requested=AsyncMock()
                await page.set_content('<button role="tab" aria-selected="true">For You</button>')
                self.assertFalse(await trainer._verify_for_you(managed,time.monotonic()+1))
                await page.set_content('<button role="tab" aria-selected="true">For You</button><a aria-label="Profile" href="https://threads.com/@me">Me</a>')
                self.assertTrue(await trainer._verify_for_you(managed,time.monotonic()+1))
                await page.set_content('<button role="tab" aria-selected="true">For You</button><a aria-label="Profile" href="https://threads.com/@me">Me</a><a href="/login">Log in</a>')
                self.assertFalse(await trainer._verify_for_you(managed,time.monotonic()+1))
                await page.set_content('<button role="tab" aria-selected="false" onclick="this.setAttribute(\'aria-selected\',\'true\')">For You</button><a aria-label="Profile" href="https://threads.com/@me">Me</a>')
                self.assertTrue(await trainer._verify_for_you(managed,time.monotonic()+2))
            finally:await browser.close()
