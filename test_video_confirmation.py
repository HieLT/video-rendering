"""Offline tests for confirmation, completion, and idle-only retry decisions."""
import asyncio
import json
from unittest.mock import patch
from browser_queue import async_playwright
from video_worker import POLL_JS, GenerationRejectedError
import video_worker_ui as worker

EN = 'The video will be generated using **the Dreamina Seedance 2.5 model. It will use 2 credits. You still have 0 video credits remaining today.**'
JA = '\u52d5\u753b\u306f**Dreamina Seedance 2.5 \u30e2\u30c7\u30eb**\u3092\u4f7f\u7528\u3057\u3066\u751f\u6210\u3055\u308c\u307e\u3059\u3002'

def user(index=1):
    return {'user_type': 1, 'index_in_conv': str(index)}

def reply(text, index=2, done=True, **extra):
    result = {'user_type': 2, 'index_in_conv': str(index), 'content_status': 0 if done else 100,
              'ext': {'is_finish': '1'} if done else {},
              'content': [{'block_type': 10000, 'is_finish': True, 'content': {'text_block': {'text': text}}}]}
    result.update(extra)
    return result

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("https://fixture.test/**", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
            await page.goto("https://fixture.test/")
            async def classify(messages, after=0):
                return await page.evaluate("""async ({source, messages, after}) => {
                    const previous = globalThis.fetch;
                    globalThis.fetch = async () => ({ok:true, status:200, json:async () => ({downlink_body:{pull_singe_chain_downlink_body:{messages}}})});
                    try { return await (eval('(' + source + ')'))({conversationId:'fixture', afterIndex:after}); }
                    finally { globalThis.fetch = previous; }
                }""", {'source': POLL_JS, 'messages': messages, 'after': after})
            cases = [
                ('English confirmation', [user(), reply(EN)], True, False),
                ('Japanese confirmation', [user(), reply(JA)], True, False),
                ('unbolded model', [user(), reply('Dreamina Seedance 2.5 failed')], False, True),
                ('generic error', [user(), reply('An error occurred. Please try again.')], False, True),
                ('still streaming despite finished block', [user(), reply('Error', done=False)], False, False),
                ('no reply yet', [user()], False, False),
                ('unknown finish state', [user(), reply('Error', ext={})], False, False),
                ('another response still generating', [user(), reply('Error'), reply('Thinking', index=3, done=False)], False, False),
                ('older response still generating', [reply('Thinking', index=1, done=False), user(2), reply('Error', index=3)], False, False),
                ('old confirmation ignored', [user(), reply(EN), user(3), reply('Error', index=4)], False, True),
                ('old rejection ignored', [user(), reply('Error'), user(3)], False, False),
                ('accepted plus follow-up text', [user(), reply(EN), reply('Follow-up', index=3)], True, False),
            ]
            for name, messages, accepted, retry in cases:
                result = await classify(messages)
                assert result['accepted'] == accepted, (name, result)
                assert bool(result['rejection']) == retry, (name, result)
                print('PASS', name, flush=True)
            blocked = reply('Translated text is irrelevant', index=7,
                            ext={'is_finish':'1', 'ai_creation_res_code':'710082022'})
            result = await classify([user(), reply(EN), blocked])
            assert result['accepted'] and result['rejection']['terminal']
            assert result['rejection']['code'] == '710082022'
            assert not (await classify([user(), reply(EN), blocked, user(8)]))['rejection']
            assert not (await classify([user(), reply(EN), blocked], after=7))['rejection']
            assert not (await classify([user(), reply(EN), blocked, reply('Thinking', index=8, done=False)]))['rejection']
            tool_blocked = reply('', index=7, ext={'is_finish':'1', 'ai_creation_tool_list':[
                {'status':5, 'fail_code':710082022}]})
            assert (await classify([user(), reply(EN), tool_blocked]))['rejection']['terminal']
            print('PASS terminal API rejection after acceptance, translation independence, and retry boundaries')
            copyright_reply = reply('Copyright refusal', index=12,
                ext={'is_finish':'1', 'ai_creation_res_code':'710092007',
                     'ai_creation_tool_list':json.dumps([{'status':5, 'fail_code':710092007}])})
            copyright_reply['content'] = json.dumps(copyright_reply['content'])
            result = await classify([user(7), reply(EN, index=8), copyright_reply])
            assert result['accepted'] and result['rejection']['code'] == '710092007'
            assert result['rejection']['terminal'] and result['latestIndex'] == 12
            assert not (await classify([user(7), reply(EN, index=8), copyright_reply], after=12))['rejection']
            assert not (await classify([user(7), copyright_reply, user(13)]))['rejection']
            assert not (await classify([user(7), reply(EN, index=8), copyright_reply, reply('Thinking', index=14, done=False)]))['rejection']
            tool_only = reply('Tool failure', index=12, ext={'is_finish':'1',
                'ai_creation_tool_list':json.dumps([{'status':5, 'fail_code':710092007}])})
            assert (await classify([user(), reply(EN), tool_only]))['rejection']['code'] == '710092007'
            for malformed in ('bad json', '{}', 'null', '[null]'):
                other = reply('Follow-up', index=12, ext={'is_finish':'1', 'ai_creation_tool_list':malformed})
                assert not (await classify([user(), reply(EN), other]))['rejection']
            print('PASS delayed copyright rejection, JSON tool metadata, and retry boundaries')
            text_only = reply('\u8457\u4f5c\u6a29\u3092\u4fdd\u8b77\u3059\u308b\u305f\u3081\u3001\u751f\u6210\u3055\u308c\u305f\u52d5\u753b\u3092\u8868\u793a\u3067\u304d\u307e\u305b\u3093\u3002', index=12)
            result = await classify([user(), reply(EN), text_only])
            assert result['accepted'] and result['rejection']['code'] == 'copyright_text'
            assert not (await classify([user(), reply(EN), text_only], after=12))['rejection']
            assert not (await classify([user(), reply(EN), text_only, user(13)]))['rejection']
            assert not (await classify([user(), reply(EN), text_only, reply('Thinking', index=14, done=False)]))['rejection']
            for code in ('710082031', '710082041'):
                refusal = reply('Refused', index=12, ext={'is_finish':'1','ai_creation_res_code':code})
                assert (await classify([user(),reply(EN),refusal]))['rejection']['code'] == code
            from video_schedule import is_policy_rejection
            prompt_refusal = 'ご希望のコンテンツを生成できません。他の内容をお試しください。'
            for messages in ([user(),reply(prompt_refusal)], [user(),reply(EN),reply(prompt_refusal,index=12)]):
                result=await classify(messages)
                assert result['rejection']['code']=='prompt_refusal_text'
                assert result['rejection']['reason']==prompt_refusal
                assert is_policy_rejection(result['rejection'])
            assert not (await classify([user(),reply(prompt_refusal,done=False)]))['rejection']
            print('PASS prompt refusal stops with original text, including delayed refusal')
            confirmation_request = """Only two videos can be generated at a time. The current request is one video, so I'll generate it directly.
Video generation currently supports durations from 4 to 15 seconds. Your scene is dialogue-heavy and requires clear pacing; I recommend 12 seconds. If you want a different length, let me know.
I'll proceed with:
- Aspect ratio: 16:9
- Duration: 12 seconds
- Task type: r2v (using the five reference images for character, faction, and environment identity)
Confirm with "Generate" and I'll create it."""
            from video_schedule import is_policy_rejection
            for code in (None, '710082031', '710082041'):
                ext={'is_finish':'1'}
                if code: ext['ai_creation_res_code']=code
                result=await classify([user(),reply(confirmation_request,ext=ext)])
                assert result['rejection'] and not result['accepted']
                assert not is_policy_rejection(result['rejection']),result
            print('PASS confirmation request is retryable, not policy')
            print('PASS text-only copyright and explicit rejection after acceptance')

            video = reply('Video done')
            video['content'].append({'block_type':2074, 'is_finish':True, 'content':{'creation_block':{'creations':[{'type':2,'video':{'download_url':'https://example.test/video.mp4'}}]}}})
            result = await classify([user(),video])
            assert result['videos'] and not result['rejection']
            assert not (await classify([user(), reply(EN), blocked, video]))['rejection']
            assert not (await classify([user(), reply(EN), copyright_reply, video]))['rejection']
            assert not (await classify([user(), reply(EN), text_only, video]))['rejection']
            print('PASS completed video without confirmation and video precedence')
            raw = reply(JA); raw['content'] = json.dumps(raw['content'])
            assert (await classify([user(),raw]))['accepted']
            assert not (await classify([user(),reply('Error')], after=2))['rejection']
            print('PASS JSON string content and retry boundary')
        finally:
            await browser.close()

    # Exercise the actual Python monitor: two consecutive idle responses are required.
    idle = {'ok':True,'responseFinished':True,'responseGenerating':False,'latestIndex':2,
            'texts':[], 'rejection':{'code':'missing_confirmation','reason':'error'}}
    busy = {**idle,'responseFinished':False,'responseGenerating':True,'rejection':None}
    accepted = {**idle,'accepted':True,'rejection':None}
    done = {**idle,'rejection':None,'videos':['https://example.test/video.mp4']}
    class Context:
        async def cookies(self, *args): return []
    class Page:
        def __init__(self, sequence): self.sequence=iter(sequence); self.calls=0
        def is_closed(self): return False
        async def evaluate(self,*args):
            self.calls+=1
            return next(self.sequence)
    class Download:
        def stat(self): return type('Stat',(),{'st_size':100})()
        def __str__(self): return 'fixture.mp4'
    async def sleep(*args): pass
    async def download(*args, **kwargs): return Download()
    with patch.object(worker.asyncio,'sleep',sleep), patch.object(worker,'_download',download):
        refusals = [
            "\u3054\u5e0c\u671b\u306e\u30b3\u30f3\u30c6\u30f3\u30c4\u3092\u751f\u6210\u3067\u304d\u307e\u305b\u3093\u3002\u4ed6\u306e\u5185\u5bb9\u3092\u304a\u8a66\u3057\u304f\u3060\u3055\u3044\u3002",
            "Unable to generate this content. Please try something else.",
            "Failed to create the requested content.",
            "\u65e0\u6cd5\u751f\u6210\u8be5\u5185\u5bb9",
        ]
        for text in refusals:
            assert not worker.CREDIT_FAIL_PATTERN.search(text)
            refusal = {**idle, 'texts': [text]}
            page = Page([refusal, refusal])
            try:
                await worker.poll_conversation('test', page, Context(), 'fixture', 30)
            except GenerationRejectedError:
                assert page.calls == 2
            else:
                raise AssertionError('Content refusal must reach generation retry')
        for text in ('Insufficient credits', 'Not enough video credits',
                     '\u6b8b\u9ad8\u4e0d\u8db3', '\u4f59\u989d\u4e0d\u8db3'):
            page = Page([{**idle, 'texts': [text]}])
            try:
                await worker.poll_conversation('test', page, Context(), 'fixture', 30)
            except worker.CreditError:
                assert page.calls == 1
            else:
                raise AssertionError('Explicit insufficient credit must remain a quota error')
        print('PASS content refusal retries; explicit quota errors remain distinct')
        page=Page([idle,busy,idle,idle])
        try:
            await worker.poll_conversation('test',page,Context(),'fixture',30)
        except GenerationRejectedError:
            assert page.calls == 4
        else: raise AssertionError('Expected retry only after two consecutive idle responses')
        terminal = {**idle, 'latestIndex':7, 'accepted':True,
                    'rejection':{'code':'710082022','reason':'blocked','terminal':True}}
        page=Page([accepted,terminal,busy,terminal,terminal])
        try:
            await worker.poll_conversation('test',page,Context(),'fixture',30)
        except GenerationRejectedError as exc:
            assert page.calls == 5 and exc.code == '710082022' and exc.latest_index == 7
        else: raise AssertionError('Expected terminal rejection to enter existing retry loop')
        print('PASS terminal rejection after acceptance requires two consecutive idle polls')
        page=Page([accepted,idle,idle,done])
        result=await worker.poll_conversation('test',page,Context(),'fixture',30)
        assert result['video_url'] and page.calls==4
        print('PASS idle stability and accepted state retained without resubmission')

if __name__ == '__main__':
    asyncio.run(run())
