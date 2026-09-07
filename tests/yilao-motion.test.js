/* 使用隔离 DOM 与定时器验证动效行为，不依赖后端或剪贴板权限。 */
'use strict';

const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = readFileSync(path.resolve(__dirname, '../static/js/yilao-motion.js'), 'utf8');

function environment({ reduced = false, clipboard, supportsIO = true, legacyMedia = false } = {}) {
    const classEvents = [];

    class Element {
        constructor(tag, attributes = {}, text = '') {
            this.tagName = tag.toUpperCase();
            this.attributes = new Map();
            this.dataset = {};
            this.tokens = new Set();
            this.children = [];
            this.parentElement = null;
            this.listeners = new Map();
            this.style = {};
            this._text = text;
            this.rect = { top: 0, bottom: 100, left: 0, right: 300 };
            this.classList = {
                add: (...tokens) => tokens.forEach(token => {
                    this.tokens.add(token);
                    classEvents.push({ element: this, token });
                }),
                remove: (...tokens) => tokens.forEach(token => this.tokens.delete(token)),
                contains: token => this.tokens.has(token),
            };
            Object.entries(attributes).forEach(([key, value]) => this.setAttribute(key, value));
        }
        set className(value) { this.tokens = new Set(value.split(/\s+/).filter(Boolean)); }
        get className() { return [...this.tokens].join(' '); }
        get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
        set textContent(value) {
            this.children.forEach(child => { child.parentElement = null; });
            this.children = [];
            this._text = String(value);
        }
        get innerText() { return this.textContent; }
        get innerHTML() { return this.textContent; }
        set innerHTML(value) { this.textContent = value; }
        getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
        setAttribute(name, value) {
            this.attributes.set(name, String(value));
            if (name === 'class') this.className = value;
            if (name.startsWith('data-')) {
                const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
                this.dataset[key] = String(value);
            }
        }
        removeAttribute(name) { this.attributes.delete(name); }
        appendChild(child) {
            if (child.parentElement) child.remove();
            this.children.push(child);
            child.parentElement = this;
            return child;
        }
        remove() {
            if (this.parentElement) {
                this.parentElement.children = this.parentElement.children.filter(child => child !== this);
                this.parentElement = null;
            }
        }
        matches(selector) {
            return selector.split(',').some(part => {
                const value = part.trim();
                if (value.startsWith('#')) return this.getAttribute('id') === value.slice(1);
                const attribute = value.match(/^\[([\w-]+)\]$/);
                if (attribute) return this.getAttribute(attribute[1]) !== null;
                const className = value.match(/^\.([\w-]+)(?:\[([\w-]+)\])?$/);
                return Boolean(className && this.tokens.has(className[1]) &&
                    (!className[2] || this.getAttribute(className[2]) !== null));
            });
        }
        closest(selector) {
            for (let element = this; element; element = element.parentElement) {
                if (element.matches(selector)) return element;
            }
            return null;
        }
        getBoundingClientRect() { return this.rect; }
        addEventListener(name, callback) {
            if (!this.listeners.has(name)) this.listeners.set(name, []);
            this.listeners.get(name).push(callback);
        }
        dispatch(name, event) { (this.listeners.get(name) || []).forEach(callback => callback(event)); }
    }

    const body = new Element('body');
    const descendants = element => element.children.flatMap(child => [child, ...descendants(child)]);
    const document = {
        body,
        readyState: 'complete',
        activeElement: body,
        documentElement: { clientHeight: 800, clientWidth: 1200 },
        createElement: tag => new Element(tag),
        createTextNode: text => new Element('text', {}, text),
        querySelectorAll: selector => descendants(body).filter(element => element.matches(selector)),
        querySelector: selector => {
            if (!selector || selector === '[') throw new SyntaxError('无效选择器');
            return document.querySelectorAll(selector)[0] || null;
        },
    };
    const observers = [];
    class Observer {
        constructor(callback) {
            this.callback = callback;
            this.observed = new Set();
            this.disconnected = false;
            observers.push(this);
        }
        observe(element) { this.observed.add(element); }
        unobserve(element) { this.observed.delete(element); }
        disconnect() { this.observed.clear(); this.disconnected = true; }
        intersect(element) { this.callback([{ target: element, isIntersecting: true }]); }
    }
    const mediaListeners = [];
    const media = {
        matches: reduced,
        set(value) {
            this.matches = value;
            mediaListeners.forEach(callback => callback({ matches: value }));
        },
    };
    if (legacyMedia) media.addListener = callback => mediaListeners.push(callback);
    else media.addEventListener = (_name, callback) => mediaListeners.push(callback);

    let time = 0;
    let timerId = 0;
    const timers = new Map();
    const frames = [];
    const window = {
        location: { search: '' },
        localStorage: { getItem: () => null, setItem: () => {} },
        matchMedia: () => media,
        innerHeight: 800,
        innerWidth: 1200,
        setTimeout: (callback, delay) => {
            const id = ++timerId;
            timers.set(id, { callback, at: time + delay });
            return id;
        },
        clearTimeout: id => timers.delete(id),
        requestAnimationFrame: callback => frames.push(callback),
    };
    if (supportsIO) window.IntersectionObserver = Observer;
    const context = {
        document, window, navigator: { clipboard }, URLSearchParams, Promise,
        performance: { now: () => time },
    };
    if (supportsIO) context.IntersectionObserver = Observer;

    return {
        body, document, classEvents, observers, media, timers, frames,
        element: (tag, attributes, text) => new Element(tag, attributes, text),
        run: () => vm.runInNewContext(script, context, { filename: 'yilao-motion.js' }),
        click(target) {
            const event = { target, prevented: false, preventDefault() { this.prevented = true; } };
            body.dispatch('click', event);
            return event;
        },
        advance(milliseconds) {
            time += milliseconds;
            [...timers].sort((a, b) => a[1].at - b[1].at).forEach(([id, timer]) => {
                if (timer.at <= time && timers.delete(id)) timer.callback();
            });
        },
        status: () => body.children.find(child => child.getAttribute('role') === 'status'),
    };
}

function belowViewport(element) {
    element.rect = { top: 1000, bottom: 1400, left: 0, right: 300 };
    return element;
}

function copyFixture(options) {
    const env = environment(options);
    const target = env.body.appendChild(env.element('p', { id: 'copy-text' }, '  今天多云  '));
    const button = env.body.appendChild(env.element('button', { 'data-copy-target': '#copy-text' }));
    const label = button.appendChild(env.element('span', {}, '复制天气'));
    env.run();
    return { ...env, target, button, label };
}

const flushPromises = async () => { await Promise.resolve(); await Promise.resolve(); };

test('首屏先显示，视口外内容才交给滚动观察器', () => {
    const env = environment();
    const visible = env.body.appendChild(env.element('section', { class: 'yl-section' }));
    const later = env.body.appendChild(belowViewport(env.element('section', { class: 'yl-section' })));
    env.run();
    assert.equal(visible.classList.contains('in-view'), true);
    assert.equal(later.classList.contains('in-view'), false);
    assert.equal(env.observers[0].observed.has(visible), false);
    assert.equal(env.observers[0].observed.has(later), true);
    const visibleIndex = env.classEvents.findIndex(event => event.element === visible && event.token === 'in-view');
    const readyIndex = env.classEvents.findIndex(event => event.element === env.body && event.token === 'motion-ready');
    assert.ok(visibleIndex < readyIndex);
    env.observers[0].intersect(later);
    assert.equal(later.classList.contains('in-view'), true);
});

test('键盘焦点显示全部入场祖先', () => {
    const env = environment();
    const outer = env.body.appendChild(belowViewport(env.element('section', { class: 'yl-section' })));
    const inner = outer.appendChild(belowViewport(env.element('div', { 'data-animate-in': '' })));
    const link = inner.appendChild(env.element('a', {}, '查看预报'));
    env.run();
    env.body.dispatch('focusin', { target: link });
    assert.equal(outer.classList.contains('in-view'), true);
    assert.equal(inner.classList.contains('in-view'), true);
    assert.equal(env.observers[0].observed.size, 0);
});

test('标题整体入场并保留强调、链接及原始节点', () => {
    const env = environment();
    const title = env.body.appendChild(env.element('h1', { 'data-words': '' }, '让天气提醒'));
    const emphasis = title.appendChild(env.element('strong', {}, '真正有用'));
    const link = title.appendChild(env.element('a', { href: '/forecast' }, '查看天气'));
    const originalText = title.textContent;
    env.run();
    assert.equal(title.textContent, originalText);
    assert.deepEqual(title.children, [emphasis, link]);
    assert.equal(title.classList.contains('yl-fade-up'), true);
    assert.equal(env.document.querySelectorAll('.word').length, 0);
});

test('启用减少动态效果后立即显示所有区块，兼容旧监听接口', () => {
    for (const legacyMedia of [false, true]) {
        const env = environment({ legacyMedia });
        const section = env.body.appendChild(belowViewport(env.element('section', { class: 'yl-section' })));
        env.run();
        assert.equal(section.classList.contains('in-view'), false);
        env.media.set(true);
        assert.equal(section.classList.contains('in-view'), true);
        assert.equal(env.observers[0].disconnected, true);
        env.media.set(false);
        assert.equal(section.classList.contains('in-view'), true);
    }
});

test('初始减少动态效果或没有观察器时保持内容可读', () => {
    for (const options of [{ reduced: true }, { supportsIO: false }]) {
        const env = environment(options);
        const section = env.body.appendChild(belowViewport(env.element('section', { class: 'yl-section' })));
        env.run();
        assert.equal(section.classList.contains('in-view'), true);
        assert.equal(env.observers.length, 0);
    }
});

test('数字滚动中启用减少动态效果会直接显示最终值', () => {
    const env = environment();
    const counter = env.body.appendChild(env.element('span', {
        class: 'yl-count', 'data-target': '27.5', 'data-decimals': '1',
    }, '27.5'));
    env.run();
    env.observers[1].intersect(counter);
    env.media.set(true);
    env.frames.shift()(100);
    assert.equal(counter.textContent, '27.5');
    assert.equal(env.frames.length, 0);
});

test('连续复制忽略旧请求，并在最新计时结束后还原原按钮节点', async () => {
    const pending = [];
    const env = copyFixture({ clipboard: { writeText: text => {
        assert.equal(text, '今天多云');
        return new Promise(resolve => pending.push(resolve));
    } } });
    assert.equal(env.click(env.label).prevented, true);
    env.click(env.label);
    pending[0]();
    await flushPromises();
    assert.equal(env.button.textContent, '复制天气正在复制…');
    assert.equal(env.button.getAttribute('aria-busy'), 'true');
    pending[1]();
    await flushPromises();
    assert.equal(env.button.textContent, '复制天气已复制');
    assert.equal(env.status().textContent, '已复制');
    assert.equal(env.status().getAttribute('aria-live'), 'polite');
    assert.equal(env.timers.size, 1);
    env.advance(900);
    env.click(env.label);
    pending[2]();
    await flushPromises();
    assert.equal(env.timers.size, 1);
    env.advance(900);
    assert.equal(env.button.textContent, '复制天气已复制');
    env.advance(900);
    assert.equal(env.button.textContent, '复制天气');
    assert.deepEqual(env.button.children, [env.label]);
    assert.equal(env.button.getAttribute('aria-busy'), null);
});

test('剪贴板拒绝或同步异常时给出可读反馈并保留原按钮', async () => {
    for (const writeText of [
        () => Promise.reject(new Error('权限拒绝')),
        () => { throw new Error('剪贴板不可用'); },
    ]) {
        const env = copyFixture({ clipboard: { writeText } });
        env.click(env.button);
        await flushPromises();
        assert.match(env.button.textContent, /复制未成功，请长按或选中文本复制/);
        assert.equal(env.status().textContent, '复制未成功，请长按或选中文本复制');
        assert.equal(env.button.getAttribute('aria-busy'), null);
        env.advance(4000);
        assert.deepEqual(env.button.children, [env.label]);
    }
});

test('无剪贴板 API、无效选择器、目标缺失与空文本均能安全反馈', () => {
    const noClipboard = copyFixture();
    noClipboard.click(noClipboard.button);
    assert.match(noClipboard.button.textContent, /请长按或选中文本复制/);
    assert.equal(noClipboard.button.getAttribute('aria-busy'), null);

    let calls = 0;
    for (const selector of ['[', '#missing', '']) {
        const env = copyFixture({ clipboard: { writeText: () => { calls++; } } });
        env.button.dataset.copyTarget = selector;
        assert.doesNotThrow(() => env.click(env.button));
        assert.equal(env.status().textContent, '未找到可复制的内容');
    }
    const empty = copyFixture({ clipboard: { writeText: () => { calls++; } } });
    empty.target.textContent = '  ';
    empty.click(empty.button);
    assert.equal(empty.status().textContent, '暂无可复制的内容');
    assert.equal(calls, 0);
});
