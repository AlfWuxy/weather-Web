/* ========================================================================
   yilao-data-fx.js
   数据强调动效 —— 三种模式的运行时
   引入后自动初始化页面上所有 [data-fx] 元素;数据更新通过对应方法触发
   ======================================================================== */

(function (global) {
    'use strict';

    // ==================== 工具 ====================

    function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }

    function animateNumber(el, from, to, opts) {
        opts = opts || {};
        const duration = opts.duration || 900;
        const decimals = opts.decimals || 0;
        const start = performance.now();
        const factor = Math.pow(10, decimals);

        function step(now) {
            const t = Math.min((now - start) / duration, 1);
            const eased = easeOutCubic(t);
            const cur = from + (to - from) * eased;
            el.textContent = (Math.round(cur * factor) / factor).toFixed(decimals);
            if (t < 1) requestAnimationFrame(step);
            else if (opts.onDone) opts.onDone();
        }
        requestAnimationFrame(step);
    }

    // ==================== A · COUNTER ====================
    // <div class="fx-counter" data-fx="counter" data-value="1280" data-decimals="0" data-suffix="位">
    //   <span class="fx-num">0</span><span class="fx-unit">位</span>
    //   <span class="fx-delta"></span>
    // </div>

    function initCounter(el) {
        const num = el.querySelector('.fx-num');
        const delta = el.querySelector('.fx-delta');
        const target = parseFloat(el.dataset.value || '0');
        const decimals = parseInt(el.dataset.decimals || '0', 10);
        const startVal = parseFloat(el.dataset.start || '0');

        // 入场:从 start → target
        animateNumber(num, startVal, target, { duration: 1200, decimals });

        // API: 更新值
        el._fxUpdate = function (newValue, options) {
            options = options || {};
            const oldValue = parseFloat(el.dataset.value || '0');
            const newVal = parseFloat(newValue);
            const diff = newVal - oldValue;
            el.dataset.value = newVal;

            animateNumber(num, oldValue, newVal, { duration: 900, decimals });

            // 数字跳动 + 高亮
            num.classList.remove('fx-jump', 'fx-highlight');
            void num.offsetWidth;
            num.classList.add('fx-jump', 'fx-highlight');
            setTimeout(() => num.classList.remove('fx-jump'), 500);
            setTimeout(() => num.classList.remove('fx-highlight'), 1400);

            // 闪光
            el.classList.remove('fx-flashing');
            void el.offsetWidth;
            el.classList.add('fx-flashing');
            setTimeout(() => el.classList.remove('fx-flashing'), 700);

            // 差值徽章
            if (delta && Math.abs(diff) > 0.001) {
                const cls = options.invertColor
                    ? (diff > 0 ? 'down' : 'up')   // 用于"风险下降是好事"反转色
                    : (diff > 0 ? 'up' : 'down');
                const sign = diff > 0 ? '↑ +' : '↓ ';
                const unit = options.unit || el.dataset.deltaUnit || '';
                delta.textContent = sign + (diff > 0 ? diff : -diff).toFixed(decimals) + unit;
                delta.classList.remove('up', 'down', 'neutral', 'show');
                delta.classList.add(cls);
                void delta.offsetWidth;
                delta.classList.add('show');
                setTimeout(() => delta.classList.remove('show'), 2400);
            }
        };
    }

    // ==================== B · BAR COMPARE ====================
    // <div class="fx-bar" data-fx="bar-compare" data-value="42" data-level="mid">
    //   <div class="fx-bar-track">
    //     <div class="fx-bar-ghost"></div>
    //     <div class="fx-bar-fill" data-level="mid" style="width:42%"></div>
    //   </div>
    //   <div class="fx-bar-hint"></div>
    // </div>

    function initBar(el) {
        const ghost = el.querySelector('.fx-bar-ghost');
        const fill  = el.querySelector('.fx-bar-fill');
        const hint  = el.querySelector('.fx-bar-hint');
        const initVal = parseFloat(el.dataset.value || '0');

        // 入场动画:从 0 长到 initVal
        fill.style.width = '0%';
        requestAnimationFrame(() => {
            fill.style.width = initVal + '%';
        });

        el._fxUpdate = function (newValue, options) {
            options = options || {};
            const oldVal = parseFloat(el.dataset.value || '0');
            const newVal = parseFloat(newValue);
            const newLevel = options.level || el.dataset.level;

            // 残影
            if (ghost) {
                ghost.style.transition = 'none';
                ghost.style.width = oldVal + '%';
                ghost.classList.remove('fading');
                ghost.classList.add('show');
                requestAnimationFrame(() => {
                    ghost.style.transition = '';
                });
            }

            // 新条延伸
            requestAnimationFrame(() => {
                fill.style.width = newVal + '%';
                if (newLevel) fill.dataset.level = newLevel;
            });

            // 数字徽章
            if (hint) {
                const diff = newVal - oldVal;
                if (Math.abs(diff) > 0.01) {
                    const sign = diff > 0 ? '+' : '';
                    hint.textContent = sign + diff.toFixed(0);
                    hint.style.left = ((oldVal + newVal) / 2) + '%';
                    hint.classList.remove('down', 'show');
                    if (diff < 0) hint.classList.add('down');
                    void hint.offsetWidth;
                    setTimeout(() => hint.classList.add('show'), 250);
                    setTimeout(() => hint.classList.remove('show'), 2800);
                }
            }

            // 残影淡出
            if (ghost) {
                setTimeout(() => ghost.classList.add('fading'), 1800);
            }

            el.dataset.value = newVal;
            if (newLevel) el.dataset.level = newLevel;
        };
    }

    // ==================== C · GAUGE ====================
    // <div class="fx-gauge" data-fx="gauge" data-value="42">
    //   <svg viewBox="0 0 200 130">…three arcs + needle…</svg>
    //   <div class="fx-gauge-value">42</div>
    //   <div class="fx-gauge-label">综合风险</div>
    // </div>
    // <div class="fx-level-tag mid">中风险</div>
    // 通过 data-fx-bind="<gaugeId>" 绑定外部等级标签

    function levelOf(score) {
        if (score < 40) return { key: 'low',  name: '低风险' };
        if (score < 70) return { key: 'mid',  name: '中风险' };
        return { key: 'high', name: '高风险' };
    }

    function angleOf(score) {
        return -120 + (score / 100) * 240;
    }

    function initGauge(el) {
        const needle = el.querySelector('.fx-gauge-needle');
        const numEl  = el.querySelector('.fx-gauge-value');
        const initScore = parseFloat(el.dataset.value || '0');
        const arcs = {
            low:  el.querySelector('[data-arc="low"]'),
            mid:  el.querySelector('[data-arc="mid"]'),
            high: el.querySelector('[data-arc="high"]')
        };

        // 入场
        if (needle) {
            needle.style.transition = 'none';
            needle.style.transform = `rotate(${angleOf(0)}deg)`;
            requestAnimationFrame(() => {
                needle.style.transition = '';
                needle.style.transform = `rotate(${angleOf(initScore)}deg)`;
            });
        }
        if (numEl) animateNumber(numEl, 0, initScore, { duration: 1300 });

        const lv = levelOf(initScore);
        if (arcs[lv.key]) {
            setTimeout(() => arcs[lv.key].classList.add('fx-active'), 1300);
            setTimeout(() => arcs[lv.key].classList.remove('fx-active'), 4500);
        }

        // 找到联动的等级标签
        const id = el.id;
        const tagWrap = id ? document.querySelector('[data-fx-gauge-tag="' + id + '"]') : null;

        el._fxUpdate = function (newScore) {
            const old = parseFloat(el.dataset.value || '0');
            const nv = parseFloat(newScore);
            el.dataset.value = nv;

            if (needle) needle.style.transform = `rotate(${angleOf(nv)}deg)`;
            if (numEl)  animateNumber(numEl, old, nv, { duration: 1300 });

            // 弧线脉冲
            const lvNew = levelOf(nv);
            const lvOld = levelOf(old);
            Object.values(arcs).forEach(a => a && a.classList.remove('fx-active'));
            setTimeout(() => {
                if (arcs[lvNew.key]) arcs[lvNew.key].classList.add('fx-active');
            }, 600);
            setTimeout(() => {
                if (arcs[lvNew.key]) arcs[lvNew.key].classList.remove('fx-active');
            }, 4000);

            // 等级标签
            if (tagWrap) {
                if (lvNew.key !== lvOld.key) {
                    tagWrap.innerHTML =
                        '<span class="fx-level-jump">' +
                        '  <span class="fx-from-tag fx-level-tag ' + lvOld.key + '">' + lvOld.name + '</span>' +
                        '  <span class="fx-arrow">→</span>' +
                        '  <span class="fx-to-tag fx-level-tag ' + lvNew.key + '">' + lvNew.name + '</span>' +
                        '</span>';
                    setTimeout(() => {
                        tagWrap.innerHTML = '<span class="fx-level-tag ' + lvNew.key + ' fx-changing">' + lvNew.name + '</span>';
                        setTimeout(() => {
                            const t = tagWrap.querySelector('.fx-level-tag');
                            if (t) t.classList.remove('fx-changing');
                        }, 600);
                    }, 2400);
                } else {
                    const tag = tagWrap.querySelector('.fx-level-tag');
                    if (tag) {
                        tag.classList.add('fx-changing');
                        setTimeout(() => tag.classList.remove('fx-changing'), 600);
                    }
                }
            }
        };
    }

    // ==================== 自动初始化 ====================

    function initAll(root) {
        root = root || document;
        const supportsIO = 'IntersectionObserver' in window;

        function bootstrap(el) {
            if (el._fxInit) return;
            el._fxInit = true;
            const kind = el.dataset.fx;
            try {
                if (kind === 'counter')      initCounter(el);
                else if (kind === 'bar-compare') initBar(el);
                else if (kind === 'gauge')   initGauge(el);
            } catch (e) {
                console.warn('[yilao-data-fx] init failed for', el, e);
            }
        }

        const els = root.querySelectorAll('[data-fx]');
        if (supportsIO) {
            const io = new IntersectionObserver((entries) => {
                entries.forEach(en => {
                    if (en.isIntersecting) {
                        bootstrap(en.target);
                        io.unobserve(en.target);
                    }
                });
            }, { threshold: 0.15 });
            els.forEach(el => io.observe(el));
        } else {
            els.forEach(bootstrap);
        }
    }

    // ==================== 公开 API ====================

    const YL = {
        initAll: initAll,
        update: function (selectorOrEl, newValue, options) {
            const el = typeof selectorOrEl === 'string' ? document.querySelector(selectorOrEl) : selectorOrEl;
            if (el && el._fxUpdate) el._fxUpdate(newValue, options);
        },
        rebind: initAll,
    };

    global.YilaoFx = YL;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => initAll());
    } else {
        initAll();
    }
})(window);
