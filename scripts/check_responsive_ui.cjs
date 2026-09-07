/* 在真实浏览器中检查跨页布局与导航；只对隔离测试服务执行合成账号登录。 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const origin = process.env.UI_QA_BASE_URL || 'http://127.0.0.1:8770';
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname), '合成账号测试仅允许本机服务');
const output = process.env.UI_QA_OUTPUT_DIR || path.join(os.tmpdir(), 'yilao-ui-browser-results');
const engines = (process.env.UI_QA_ENGINES || 'chromium,webkit,firefox').split(',');
const viewports = [
    { name: 'small-phone', width: 320, height: 568, touch: true },
    { name: 'phone', width: 390, height: 844, touch: true },
    { name: 'phone-landscape', width: 844, height: 390, touch: true },
    { name: 'tablet', width: 768, height: 1024, touch: true },
    { name: 'tablet-landscape', width: 1024, height: 768, touch: true },
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'short-desktop', width: 1024, height: 320 },
];
const publicRoutes = ['/', '/entry', '/login', '/register', '/risk', '/cooling', '/transparency', '/about/trust-network'];
const roleRoutes = {
    user: ['/dashboard', '/elder-mode', '/family-members', '/profile'],
    caregiver: ['/pairs'],
    community: ['/community', '/community-risk'],
    admin: ['/dashboard'],
};
const results = [];
let failureShots = 0;

async function settle(page) {
    await page.waitForFunction(() => document.readyState === 'complete');
    await page.evaluate(() => document.fonts.ready);
}

async function layout(page) {
    const observed = await page.evaluate(() => ({
        title: document.title,
        text: document.querySelector('main')?.innerText.trim().length || 0,
        width: window.innerWidth,
        scrollWidth: document.documentElement.scrollWidth,
        brokenImages: [...document.images].filter(img => img.complete && !img.naturalWidth).map(img => img.currentSrc),
        overlay: !!document.querySelector('vite-error-overlay, nextjs-portal'),
    }));
    assert.match(observed.title, /宜老|天气|登录|注册|社区|健康|家庭|配对|照护|风险|资料|家属|老人/);
    assert.ok(observed.text > 15, '正文为空');
    assert.equal(observed.overlay, false, '页面出现开发错误层');
    assert.ok(observed.scrollWidth <= observed.width + 1, `横向溢出 ${observed.scrollWidth} > ${observed.width}`);
    assert.deepEqual(observed.brokenImages, [], '图片加载失败');
    return observed;
}

async function navigation(page, viewport) {
    const drawer = page.locator('#appNavDrawer');
    if (viewport.width < 992) {
        const toggle = page.locator('[data-bs-target="#appNavDrawer"]');
        const button = await toggle.boundingBox();
        assert.ok(button && button.width >= 44 && button.height >= 44, '手机菜单触控区域不足');
        await toggle.click();
        await page.waitForFunction(() => document.querySelector('#appNavDrawer').classList.contains('show'));
        const links = drawer.locator('a[href]');
        await links.last().scrollIntoViewIfNeeded();
        const bounds = await links.last().boundingBox();
        assert.ok(bounds && bounds.y >= 0 && bounds.y + bounds.height <= viewport.height + 1, '手机菜单末项不可达');
        await drawer.getByRole('button', { name: '关闭', exact: true }).click();
        await page.waitForFunction(() => !document.querySelector('#appNavDrawer').classList.contains('show') && !document.querySelector('.offcanvas-backdrop'));
        assert.equal(await toggle.evaluate(el => document.activeElement === el), true, '抽屉关闭后焦点未回到按钮');
        assert.notEqual(await page.locator('body').evaluate(el => el.style.overflow), 'hidden', '抽屉关闭后正文仍被锁定');
    } else {
        const trigger = page.locator('[data-nav-more-trigger="desktop"]');
        if (!await trigger.count()) return;
        await trigger.focus();
        await trigger.press('Enter');
        await page.waitForFunction(() => document.querySelector('#appMoreMenu').classList.contains('is-open'));
        const panel = page.locator('#appMegaMenu');
        // 等待短过渡完成后测量，避免动画中的偏移干扰边界判断。
        await panel.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished.catch(() => {}))));
        const bounds = await panel.boundingBox();
        assert.ok(bounds && bounds.x >= -1 && bounds.x + bounds.width <= viewport.width + 1, '桌面菜单横向越界');
        assert.ok(bounds.y >= 0 && bounds.y + bounds.height <= viewport.height + 1, '短屏菜单底部越界');
        const last = panel.locator('a[href]').last();
        await last.scrollIntoViewIfNeeded();
        const lastBounds = await last.boundingBox();
        assert.ok(lastBounds && lastBounds.y + lastBounds.height <= viewport.height + 1, '桌面菜单末项不可达');
        await last.focus();
        await last.press('Tab');
        await page.waitForFunction(() => !document.querySelector('#appMoreMenu').classList.contains('is-open'));
        await trigger.focus();
        await trigger.press('Enter');
        await trigger.press('Escape');
        assert.equal(await trigger.getAttribute('aria-expanded'), 'false');
        assert.equal(await trigger.evaluate(el => document.activeElement === el), true, 'Esc 后焦点未恢复');
    }
}

async function check(page, name, action) {
    const errors = [];
    const requests = [];
    const onError = error => errors.push(error.message);
    const onConsole = message => {
        if (message.type() === 'error') errors.push(message.text());
    };
    const onResponse = response => {
        if (response.url().startsWith(origin) && response.status() >= 400) requests.push(`${response.status()} ${response.url()}`);
    };
    page.on('pageerror', onError);
    page.on('console', onConsole);
    page.on('response', onResponse);
    try {
        await action();
        assert.deepEqual(errors, [], '浏览器运行异常');
        assert.deepEqual(requests, [], '同源资源或接口异常');
        results.push({ name, pass: true, url: page.url() });
    } catch (error) {
        let screenshot;
        if (failureShots++ < 8) {
            screenshot = path.join(output, `failure-${failureShots}.png`);
            await page.screenshot({ path: screenshot }).catch(() => {});
        }
        results.push({ name, pass: false, url: page.url(), error: error.message, errors, requests, screenshot });
        console.error(`FAIL ${name}: ${error.message}`);
    } finally {
        page.off('pageerror', onError);
        page.off('console', onConsole);
        page.off('response', onResponse);
    }
}

async function run(engineName) {
    const browser = await playwright[engineName].launch({ headless: true });
    try {
        for (const viewport of viewports) {
            const context = await browser.newContext({
                viewport: { width: viewport.width, height: viewport.height },
                hasTouch: !!viewport.touch,
                ...(engineName !== 'firefox' ? { isMobile: !!viewport.touch } : {}),
                reducedMotion: 'reduce',
            });
            const page = await context.newPage();
            page.setDefaultTimeout(10000);
            for (const route of publicRoutes) {
                await check(page, `${engineName}/${viewport.name}${route}`, async () => {
                    const response = await page.goto(origin + route);
                    assert.equal(response.status(), 200);
                    assert.equal(new URL(page.url()).pathname, route);
                    await settle(page);
                    await layout(page);
                    await navigation(page, viewport);
                    if (route === '/' && ['phone', 'desktop'].includes(viewport.name)) {
                        await page.screenshot({ path: path.join(output, `${engineName}-${viewport.name}.png`) });
                    }
                });
            }
            await check(page, `${engineName}/${viewport.name}/menu-link`, async () => {
                await page.goto(`${origin}/__uiqa/login/user`);
                if (viewport.width < 992) {
                    await page.locator('[data-bs-target="#appNavDrawer"]').click();
                    await page.waitForFunction(() => document.querySelector('#appNavDrawer').classList.contains('show'));
                    await page.locator('#appNavDrawer a[href="/cooling"]').click();
                } else {
                    const trigger = page.locator('[data-nav-more-trigger="desktop"]');
                    await trigger.focus();
                    await trigger.press('Enter');
                    await page.locator('#appMegaMenu a[href="/cooling"]').click();
                }
                await page.waitForURL('**/cooling');
                await settle(page);
                await layout(page);
                await page.goto(`${origin}/__uiqa/login/anonymous`);
            });
            if (['phone', 'desktop'].includes(viewport.name)) {
                for (const [role, routes] of Object.entries(roleRoutes)) {
                    await page.goto(`${origin}/__uiqa/login/${role}`);
                    for (const route of routes) {
                        await check(page, `${engineName}/${viewport.name}/${role}${route}`, async () => {
                            const response = await page.goto(origin + route);
                            assert.equal(response.status(), 200);
                            assert.equal(new URL(page.url()).pathname, route);
                            await settle(page);
                            await layout(page);
                            await navigation(page, viewport);
                        });
                    }
                }
                await page.goto(`${origin}/__uiqa/login/anonymous`);
                await check(page, `${engineName}/${viewport.name}/login-flow`, async () => {
                    await page.goto(origin + '/login');
                    await page.locator('[name="username"]').fill('uiqa_user');
                    await page.locator('[name="password"]').fill('UiSmoke-LocalOnly!');
                    await page.locator('form button[type="submit"]').click();
                    await page.waitForURL('**/pairs');
                    await settle(page);
                    await layout(page);
                });
            }
            await context.close();
            console.log(`${engineName}/${viewport.name} complete`);
        }
        const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
        const page = await context.newPage();
        page.setDefaultTimeout(10000);
        await check(page, `${engineName}/text-size-and-motion`, async () => {
            await page.goto(origin + '/');
            await settle(page);
            await page.locator('html').evaluate(el => { el.style.fontSize = '200%'; });
            await layout(page);
            await page.emulateMedia({ reducedMotion: 'reduce' });
            await page.waitForFunction(() => [...document.querySelectorAll('.yl-fade-up, .yl-section, .narrative-step, [data-animate-in]')].every(el => el.classList.contains('in-view')));
            await page.waitForFunction(() => [...document.querySelectorAll('.yl-fade-up, .yl-section, .narrative-step, [data-animate-in]')].every(el => getComputedStyle(el).opacity === '1' && getComputedStyle(el).transform === 'none'));
            assert.equal(await page.locator('h1 .word').count(), 0, '标题被拆碎');
            await page.emulateMedia({ reducedMotion: 'no-preference' });
            assert.equal(await page.locator('h1').evaluate(el => getComputedStyle(el).opacity), '1', '恢复动效后首屏标题被隐藏');
        });
        await context.close();
    } finally {
        await browser.close();
    }
}

(async () => {
    await fs.mkdir(output, { recursive: true });
    // 三个浏览器顺序运行，减少 CI 和本机内存峰值。
    for (const engine of engines) await run(engine);
    const failures = results.filter(result => !result.pass);
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ origin, engines, total: results.length, passed: results.length - failures.length, failures, results }, null, 2));
    console.log(`UI checks: ${results.length - failures.length}/${results.length}; results: ${output}`);
    process.exitCode = failures.length ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1; });
