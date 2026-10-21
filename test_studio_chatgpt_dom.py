"""Real browser, fully intercepted fixture; no ChatGPT network traffic or login."""
import unittest
import tempfile
from pathlib import Path
from playwright.async_api import async_playwright
from studio_chatgpt import ChatGPTDOM, BrowserUnavailable


HTML = '''<html><body><main id="turns"></main>
<div id="prompt-textarea" contenteditable="true"></div>
<button data-testid="send-button" onclick="send()">Send</button>
<script>function send(){
const input=document.getElementById('prompt-textarea');
const turn=document.createElement('div');turn.setAttribute('data-message-author-role','user');
turn.textContent=input.innerText;document.getElementById('turns').append(turn);input.innerText='';
}</script></body></html>'''


class DOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(channel='chrome', headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
        self.page = await self.context.new_page()
        await self.page.goto('https://chatgpt.com/c/fixture')
        self.driver = ChatGPTDOM()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_draft_preserved_and_correlated_complete_reply(self):
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:one\ninput', [])
        with self.assertRaises(BrowserUnavailable):
            await self.driver.prepare(self.page, 'replacement', [])
        await self.driver.send(self.page)
        observed = await self.driver.inspect(self.page, 'FILM_STUDIO_TASK:one')
        self.assertTrue(observed['sent'])
        self.assertIsNone(observed['reply'])
        await self.page.evaluate('''() => {
            const t=document.createElement('div');t.dataset.messageAuthorRole='assistant';
            t.textContent='{"task_id":"one","output":{}}';document.getElementById('turns').append(t);
            const b=document.createElement('button');b.dataset.testid='stop-button';document.body.append(b);
        }''')
        self.assertIsNone((await self.driver.inspect(self.page, 'FILM_STUDIO_TASK:one'))['reply'])
        await self.page.locator('[data-testid="stop-button"]').evaluate('(e)=>e.remove()')
        self.assertIn('"one"',(await self.driver.inspect(self.page,'FILM_STUDIO_TASK:one'))['reply'])
        self.assertFalse((await self.driver.inspect(self.page,'FILM_STUDIO_TASK:other'))['sent'])

    async def test_later_user_turn_prevents_wrong_response_attribution(self):
        await self.driver.prepare(self.page,'FILM_STUDIO_TASK:one',[])
        await self.driver.send(self.page)
        await self.driver.prepare(self.page,'manual later question',[])
        await self.driver.send(self.page)
        with self.assertRaises(BrowserUnavailable):
            await self.driver.inspect(self.page,'FILM_STUDIO_TASK:one')

    async def test_stale_orchestrator_draft_is_replaced_but_manual_draft_is_preserved(self):
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:old\nold payload', [])
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:new\nnew payload', [])
        self.assertEqual((await self.page.locator('#prompt-textarea').inner_text()).strip(),
                         'FILM_STUDIO_TASK:new\nnew payload')
        await self.page.locator('#prompt-textarea').fill('manual unfinished note')
        with self.assertRaises(BrowserUnavailable):
            await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:next\npayload', [])

    async def test_marker_in_composer_is_not_a_delivery_receipt(self):
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:draft\npayload', [])
        observed = await self.driver.inspect(self.page, 'FILM_STUDIO_TASK:draft')
        self.assertFalse(observed['sent'])
        self.assertTrue(observed['draft'])

    async def test_japanese_send_button_without_testid_is_supported(self):
        await self.page.locator('[data-testid="send-button"]').evaluate('''button => {
            button.removeAttribute('data-testid');
            button.removeAttribute('type');
            button.setAttribute('aria-label', 'メッセージを送信');
        }''')
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:japanese-send\npayload', [])
        await self.driver.send(self.page)
        self.assertTrue((await self.driver.inspect(
            self.page, 'FILM_STUDIO_TASK:japanese-send'))['sent'])

    async def test_retry_replaces_attachments_instead_of_accumulating(self):
        await self.page.evaluate('''() => {
            const editor = document.querySelector('#prompt-textarea');
            const send = document.querySelector('[data-testid="send-button"]');
            const form = document.createElement('form');
            document.body.append(form); form.append(editor, send);
            const input = document.createElement('input');
            input.type = 'file'; input.multiple = true; form.append(input);
            input.addEventListener('change', () => {
                for (const file of input.files) {
                    const button = document.createElement('button');
                    button.type = 'button'; button.setAttribute('aria-label', file.name + 'を削除する');
                    button.onclick = () => button.remove(); form.append(button);
                }
            });
        }''')
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'reference.txt'
            file.write_text('reference')
            message = 'FILM_STUDIO_TASK:retry\npayload'
            await self.driver.prepare(self.page, message, [file])
            await self.page.locator('button[aria-label$="を削除する"]').evaluate(
                "e => e.setAttribute('aria-label', 'reference.txt を削除')")
            await self.driver.prepare(self.page, message, [file])
            self.assertEqual(await self.page.locator('button[aria-label$="を削除する"]').count(), 1)
            self.assertEqual(await self.page.locator('button[aria-label$="を削除"]').count(), 0)

    async def test_upload_uses_active_composer_when_hidden_input_precedes_it(self):
        await self.page.evaluate('''() => {
            const hidden = document.createElement('input');
            hidden.type = 'file'; hidden.multiple = true;
            document.body.prepend(hidden);
            const form = document.createElement('form');
            document.body.append(form);
            form.append(document.querySelector('#prompt-textarea'), document.querySelector('[data-testid="send-button"]'));
            const active = document.createElement('input'); active.type = 'file'; active.multiple = true;
            form.append(active);
            active.addEventListener('change', () => {
                for (const file of active.files) {
                    const button = document.createElement('button');
                    button.type = 'button'; button.setAttribute('aria-label', 'Remove file ' + file.name);
                    button.onclick = () => button.remove(); form.append(button);
                }
            });
        }''')
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'reference.txt'
            file.write_text('reference')
            await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:active\npayload', [file])
            counts = await self.page.locator('input[type=file]').evaluate_all('(inputs) => inputs.map(i => i.files.length)')
            self.assertEqual(counts, [0, 1])

    async def test_generic_picker_used_when_composer_has_specialized_pickers(self):
        await self.page.evaluate('''() => {
            const form = document.createElement('form'); document.body.append(form);
            form.append(document.querySelector('#prompt-textarea'), document.querySelector('[data-testid="send-button"]'));
            for (const accept of ['image/*,video/*', 'image/*', '']) {
                const input = document.createElement('input'); input.type = 'file'; input.multiple = true;
                if (accept) input.setAttribute('accept', accept);
                form.append(input);
                if (!accept) input.addEventListener('change', () => {
                    for (const file of input.files) {
                        const b = document.createElement('button'); b.type = 'button';
                        b.setAttribute('aria-label', 'Remove file ' + file.name); form.append(b);
                    }
                });
            }
        }''')
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / 'reference.png'; file.write_text('reference')
            await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:picker\npayload', [file])
            counts = await self.page.locator('input[type=file]').evaluate_all('(inputs) => inputs.map(i => i.files.length)')
            self.assertEqual(counts, [0, 0, 1])

    async def test_plain_text_image_progress_is_busy(self):
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:image\npayload', [])
        await self.driver.send(self.page)
        await self.page.evaluate('''() => {
            const t=document.createElement('div');t.dataset.messageAuthorRole='assistant';
            t.textContent='画像を作成中 32%';document.getElementById('turns').append(t);
        }''')
        observed = await self.driver.inspect(self.page, 'FILM_STUDIO_TASK:image')
        self.assertTrue(observed['busy'])
        self.assertIsNone(observed['reply'])

    async def test_localized_stop_button_and_draft_progress_are_busy(self):
        await self.page.evaluate('''() => {
            const b = document.createElement('button'); b.setAttribute('aria-label', '停止');
            document.body.append(b);
        }''')
        self.assertTrue(await self.driver.response_busy(self.page))
        await self.page.locator('button[aria-label="停止"]').evaluate('(el) => el.remove()')
        self.assertTrue(await self.driver.response_busy(self.page, '最初の下書きを作成中 54%'))

    async def test_other_role_keeps_its_own_tab(self):
        self.driver.context = self.context
        other = await self.driver.page('https://chatgpt.com/c/other-role')
        self.assertIsNot(other, self.page)
        self.assertEqual(self.page.url, 'https://chatgpt.com/c/fixture')
        self.assertEqual(await self.driver.page('https://chatgpt.com/c/other-role'), other)

    async def test_generated_image_controls_are_counted_for_delivery_reconciliation(self):
        self.assertEqual(await self.driver.image_download_count(self.page), 0)
        self.assertEqual(await self.driver.image_result_count(self.page), 0)
        await self.page.evaluate('''() => {
            const b=document.createElement('button');
            b.setAttribute('aria-label', '画像をダウンロード');document.body.append(b);
            const image=document.createElement('button');
            image.setAttribute('aria-label', '生成画像 1 を編集');document.body.append(image);
        }''')
        self.assertEqual(await self.driver.image_download_count(self.page), 1)
        self.assertEqual(await self.driver.image_result_count(self.page), 1)

    async def test_virtualized_images_are_matched_to_exact_task_without_role_attributes(self):
        await self.page.locator('#turns').evaluate('''el => {
            el.innerHTML = '<div>FILM_STUDIO_TASK:old</div>' +
                '<button data-testid="generated-image-preview"></button>' +
                '<div>FILM_STUDIO_TASK:new</div>';
        }''')
        self.assertIsNone(await self.driver.image_preview_for_marker(self.page, 'FILM_STUDIO_TASK:new'))
        await self.page.locator('[data-testid="generated-image-preview"]').evaluate('(el) => el.remove()')
        await self.page.locator('#turns').evaluate('''el => {
            el.insertAdjacentHTML('beforeend', '<button data-testid="generated-image-preview"></button>');
        }''')
        self.assertIsNotNone(await self.driver.image_preview_for_marker(self.page, 'FILM_STUDIO_TASK:new'))
        self.assertIsNone(await self.driver.image_preview_for_marker(self.page, 'FILM_STUDIO_TASK:old'))
        observed = await self.driver.inspect(self.page, 'FILM_STUDIO_TASK:new')
        self.assertEqual(observed['reply'], '[generated image available]')
        # ChatGPT can repeat the latest marker in a hidden accessibility node.
        await self.page.locator('#turns').evaluate('''el => {
            el.insertAdjacentHTML('beforeend', '<div hidden>FILM_STUDIO_TASK:new</div>');
        }''')
        self.assertIsNotNone(await self.driver.image_preview_for_marker(self.page, 'FILM_STUDIO_TASK:new'))
        await self.page.locator('#turns').evaluate('''el => {
            el.insertAdjacentHTML('beforeend', '<div>FILM_STUDIO_TASK:later</div>');
        }''')
        self.assertIsNone(await self.driver.image_preview_for_marker(self.page, 'FILM_STUDIO_TASK:later'))

    async def test_transient_image_overlay_is_dismissed_before_send(self):
        await self.page.evaluate('''() => {
            const overlay=document.createElement('div');
            overlay.className='codex-dialog-overlay';overlay.dataset.state='open';
            document.body.append(overlay);
            document.addEventListener('keydown', event => {
                if (event.key === 'Escape') overlay.remove();
            });
        }''')
        await self.driver.prepare(self.page, 'FILM_STUDIO_TASK:overlay\npayload', [])
        await self.driver.send(self.page)
        self.assertTrue((await self.driver.inspect(
            self.page, 'FILM_STUDIO_TASK:overlay'))['sent'])


if __name__ == '__main__': unittest.main()
