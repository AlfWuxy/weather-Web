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
        const animated = document.querySelectorAll('.yl-fade-up, .yl-section, .narrative-step, [data-animate-in]');

        if (prefersReducedMotion() || !('IntersectionObserver' in window)) {
            animated.forEach(el => el.classList.add('in-view'));
            body.classList.add('motion-ready');
            return;
        }

        body.classList.add('motion-ready');
        const observer = new IntersectionObserver((entries) => {
            entries.forEach((entry) => {
                if (!entry.isIntersecting) return;
                entry.target.classList.add('in-view');
                observer.unobserve(entry.target);
            });
        }, { rootMargin: '0px 0px -8% 0px', threshold: 0.05 });

        animated.forEach(el => observer.observe(el));
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
        if (prefersReducedMotion()) return;

        document.querySelectorAll('[data-words]').forEach((el) => {
            if (el.dataset.wordsReady === '1') return;
            const text = el.textContent || '';
            el.textContent = '';
            Array.from(text).forEach((char, index) => {
                if (/\s/.test(char)) {
                    el.appendChild(document.createTextNode(char));
                    return;
                }
                const span = document.createElement('span');
                span.className = 'word';
                span.style.animationDelay = (index * 0.035) + 's';
                span.textContent = char;
                el.appendChild(span);
            });
            el.dataset.wordsReady = '1';
        });
    }

    function initCopyFeedback() {
        if (!navigator.clipboard) return;

        document.body.addEventListener('click', function(event) {
            const button = event.target.closest('[data-copy-target]');
            if (!button) return;

            const target = document.querySelector(button.dataset.copyTarget);
            if (!target) return;

            const originalHtml = button.innerHTML;
            navigator.clipboard.writeText((target.innerText || '').trim()).then(function() {
                button.innerHTML = '<i class="bi bi-check2-circle copy-ok"></i> 已复制';
                button.classList.add('text-success');
                window.setTimeout(function() {
                    button.innerHTML = originalHtml;
                    button.classList.remove('text-success');
                }, 1400);
            }).catch(function() {
                button.classList.add('shake');
                window.setTimeout(function() {
                    button.classList.remove('shake');
                }, 500);
            });
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
