/* 宜老天气通 · 轻动效初始化 */
(function () {
    'use strict';

    function onReady(callback) {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', callback);
        } else {
            callback();
        }
    }

    function safeStorageGet(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (_err) {
            return null;
        }
    }

    function safeStorageSet(key, value) {
        try {
            window.localStorage.setItem(key, value);
        } catch (_err) {
            // 本地存储不可用时,只影响偏好持久化。
        }
    }

    function normalizeMotion(value) {
        const tokens = String(value || '')
            .split(/\s+/)
            .filter(token => /^m[1-5]$/.test(token));
        return Array.from(new Set(tokens)).join(' ');
    }

    function prefersReducedMotion() {
        return Boolean(
            window.matchMedia &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches
        );
    }

    function initMotionState(body) {
        const params = new URLSearchParams(window.location.search);
        const fromUrl = normalizeMotion(params.get('motion'));
        const fromStorage = normalizeMotion(safeStorageGet('motion'));
        const fromMarkup = normalizeMotion(body.getAttribute('data-motion'));
        const motion = fromUrl || fromStorage || fromMarkup || 'm1 m2 m4 m5';

        body.setAttribute('data-motion', motion);
        if (fromUrl) {
            safeStorageSet('motion', fromUrl);
        }

        window.setMotion = function (combo) {
            const next = normalizeMotion(combo) || 'm1 m2 m4 m5';
            body.setAttribute('data-motion', next);
            safeStorageSet('motion', next);
        };
    }

    function initEntranceMotion(body) {
        const selector = '.yl-fade-up, .yl-section, .narrative-step, [data-animate-in]';
        const animated = document.querySelectorAll(selector);
        const motionQuery = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)');
        let observer = null;

        function reveal(el) {
            el.classList.add('in-view');
            if (observer) observer.unobserve(el);
        }

        function revealAll() {
            animated.forEach(reveal);
            if (observer) observer.disconnect();
        }

        function revealFocusedAncestors(target) {
            for (let el = target; el && el !== body; el = el.parentElement) {
                if (el.matches && el.matches(selector)) reveal(el);
            }
        }

        // 键盘跳转时同时展开所有入场祖先，避免焦点落在透明内容中。
        body.addEventListener('focusin', function (event) {
            revealFocusedAncestors(event.target);
        });

        if ((motionQuery && motionQuery.matches) || !('IntersectionObserver' in window)) {
            revealAll();
        } else {
            observer = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    if (entry.isIntersecting) reveal(entry.target);
                });
            }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

            const height = window.innerHeight || document.documentElement.clientHeight;
            const width = window.innerWidth || document.documentElement.clientWidth;
            animated.forEach((el) => {
                const rect = el.getBoundingClientRect();
                // 首屏先显示再启用动效，避免页面加载后出现一次隐藏闪烁。
                if (rect.bottom > 0 && rect.top < height && rect.right > 0 && rect.left < width) {
                    reveal(el);
                } else {
                    observer.observe(el);
                }
            });
        }

        revealFocusedAncestors(document.activeElement);
        body.classList.add('motion-ready');

        if (motionQuery) {
            const onPreferenceChange = function (event) {
                if (event.matches) revealAll();
            };
            if (motionQuery.addEventListener) {
                motionQuery.addEventListener('change', onPreferenceChange);
            } else if (motionQuery.addListener) {
                // 兼容仍使用旧媒体查询监听接口的浏览器。
                motionQuery.addListener(onPreferenceChange);
            }
        }
    }

    function initCountUp() {
        document.querySelectorAll('.yl-count[data-target]').forEach((el) => {
            const target = Number.parseFloat(el.dataset.target || '0');
            if (!Number.isFinite(target)) return;

            const decimals = Number.parseInt(el.dataset.decimals || '0', 10);
            const duration = Math.max(120, Number.parseInt(el.dataset.duration || '900', 10));

            function render(value) {
                el.textContent = decimals > 0 ? value.toFixed(decimals) : String(Math.round(value));
            }

            if (prefersReducedMotion() || !('IntersectionObserver' in window)) {
                render(target);
                return;
            }

            const observer = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) return;
                    const start = performance.now();

                    function tick(now) {
                        if (prefersReducedMotion()) {
                            render(target);
                            return;
                        }
                        const progress = Math.min(1, (now - start) / duration);
                        const eased = 1 - Math.pow(1 - progress, 3);
                        render(target * eased);
                        if (progress < 1) {
                            window.requestAnimationFrame(tick);
                        }
                    }

                    window.requestAnimationFrame(tick);
                    observer.unobserve(entry.target);
                });
            }, { threshold: 0.1 });

            observer.observe(el);
        });
    }

    function initWordMotion() {
        document.querySelectorAll('[data-words]').forEach((el) => {
            // 保留文字、强调与链接原结构，标题整体入场，中文换行和朗读保持自然。
            el.classList.add('yl-fade-up');
            el.dataset.wordsReady = '1';
        });
    }

    function initCopyFeedback() {
        const states = new WeakMap();
        const liveStatus = document.createElement('span');
        liveStatus.className = 'visually-hidden';
        liveStatus.setAttribute('role', 'status');
        liveStatus.setAttribute('aria-live', 'polite');
        liveStatus.setAttribute('aria-atomic', 'true');
        document.body.appendChild(liveStatus);

        function restoreBusyState(button, state) {
            if (state.originalBusy === null) button.removeAttribute('aria-busy');
            else button.setAttribute('aria-busy', state.originalBusy);
        }

        document.body.addEventListener('click', function (event) {
            const source = event.target && (event.target.closest ? event.target : event.target.parentElement);
            const button = source && source.closest('[data-copy-target]');
            if (!button) return;
            event.preventDefault();

            let state = states.get(button);
            if (!state) {
                const feedback = document.createElement('span');
                feedback.setAttribute('aria-hidden', 'true');
                state = { feedback, attempt: 0, timer: null, originalBusy: button.getAttribute('aria-busy') };
                states.set(button, state);
            }
            if (state.timer !== null) window.clearTimeout(state.timer);
            state.timer = null;
            const attempt = ++state.attempt;
            // 保留原按钮节点与事件；只更新独立反馈，不把上次成功文案当作原文。
            if (!state.feedback.parentElement) button.appendChild(state.feedback);
            state.feedback.className = 'copy-feedback ms-2';
            state.feedback.textContent = '正在复制…';
            button.setAttribute('aria-busy', 'true');
            liveStatus.textContent = '';

            function finish(message, success) {
                // 较早的剪贴板请求完成时，不覆盖最近一次操作的状态。
                if (state.attempt !== attempt) return;
                restoreBusyState(button, state);
                state.feedback.className = 'copy-feedback ms-2 ' + (success ? 'text-success' : 'text-danger');
                state.feedback.textContent = message;
                liveStatus.textContent = message;
                state.timer = window.setTimeout(function () {
                    state.feedback.remove();
                    state.timer = null;
                }, success ? 1800 : 4000);
            }

            let target;
            try {
                target = document.querySelector(button.dataset.copyTarget || '');
            } catch (_err) {
                finish('未找到可复制的内容', false);
                return;
            }
            if (!target) {
                finish('未找到可复制的内容', false);
                return;
            }

            const text = (target.innerText || target.textContent || '').trim();
            if (!text) {
                finish('暂无可复制的内容', false);
                return;
            }
            if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
                finish('请长按或选中文本复制', false);
                return;
            }

            try {
                Promise.resolve(navigator.clipboard.writeText(text)).then(function () {
                    finish('已复制', true);
                }, function () {
                    finish('复制未成功，请长按或选中文本复制', false);
                });
            } catch (_err) {
                finish('复制未成功，请长按或选中文本复制', false);
            }
        });
    }

    onReady(function () {
        const body = document.body;
        if (!body) return;

        initMotionState(body);
        initWordMotion();
        initEntranceMotion(body);
        initCountUp();
        initCopyFeedback();
    });
})();
