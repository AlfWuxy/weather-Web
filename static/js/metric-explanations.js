(function () {
    'use strict';

    const catalogNode = document.getElementById('metricExplanationCatalog');
    if (!catalogNode || typeof bootstrap === 'undefined' || !bootstrap.Popover) {
        return;
    }

    let catalog = {};
    try {
        catalog = JSON.parse(catalogNode.textContent || '{}');
    } catch (error) {
        console.warn('指标解释目录解析失败', error);
        return;
    }

    const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    function readContext(button) {
        if (!button.dataset.metricContext) {
            return null;
        }
        try {
            const context = JSON.parse(button.dataset.metricContext);
            return context && typeof context === 'object' ? context : null;
        } catch (error) {
            console.warn('本次指标输入解析失败', error);
            return null;
        }
    }

    function buildList(items, className) {
        if (!Array.isArray(items) || !items.length) {
            return '';
        }
        return `<ul class="${className}">${items.map(item => `<li>${escapeHtml(item)}</li>`).join('')}</ul>`;
    }

    function buildContext(context) {
        if (!context) {
            return '';
        }
        const rows = Object.entries(context)
            .filter(([, value]) => value !== null && value !== undefined && value !== '')
            .map(([label, value]) => (
                `<div class="yl-popover-context-row"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
            ));
        if (!rows.length) {
            return '';
        }
        return `<div class="yl-popover-context"><div class="yl-popover-eyebrow">本次结果</div>${rows.join('')}</div>`;
    }

    function buildContent(metric, context, detailsUrl) {
        const thresholds = buildList(metric.thresholds, 'yl-popover-list');
        const detailsLink = detailsUrl
            ? `<a class="yl-popover-link" href="${escapeHtml(detailsUrl)}">查看完整计算方法 <span aria-hidden="true">→</span></a>`
            : '';
        return [
            `<p class="yl-popover-summary">${escapeHtml(metric.summary)}</p>`,
            metric.formula
                ? `<div class="yl-popover-formula"><div class="yl-popover-eyebrow">公式</div><code>${escapeHtml(metric.formula)}</code></div>`
                : '',
            buildContext(context),
            thresholds ? `<div class="yl-popover-thresholds"><div class="yl-popover-eyebrow">分级</div>${thresholds}</div>` : '',
            detailsLink,
        ].join('');
    }

    function initButton(button) {
        if (!(button instanceof HTMLElement) || button.dataset.metricInfoReady === '1') {
            return;
        }
        const metric = catalog[button.dataset.metricInfo];
        if (!metric) {
            return;
        }

        const context = readContext(button);
        const instance = new bootstrap.Popover(button, {
            container: 'body',
            customClass: 'yl-metric-popover',
            delay: { show: 80, hide: 350 },
            html: true,
            placement: 'auto',
            sanitize: true,
            title: metric.title,
            trigger: 'hover focus click',
            content: buildContent(metric, context, button.dataset.detailsUrl),
        });

        button.dataset.metricInfoReady = '1';
        button.addEventListener('show.bs.popover', function () {
            button.setAttribute('aria-expanded', 'true');
        });
        button.addEventListener('shown.bs.popover', function () {
            button.setAttribute('aria-expanded', 'true');
        });
        button.addEventListener('hidden.bs.popover', function () {
            button.setAttribute('aria-expanded', 'false');
        });
        const forceHide = function () {
            if (instance._activeTrigger) {
                Object.keys(instance._activeTrigger).forEach(function (triggerName) {
                    instance._activeTrigger[triggerName] = false;
                });
            }
            button.blur();
            instance.hide();
        };
        button.addEventListener('keydown', function (event) {
            if (event.key !== 'Escape') {
                return;
            }
            forceHide();
        });
    }

    function initWithin(root) {
        if (!root) {
            return;
        }
        if (root.matches && root.matches('[data-metric-info]')) {
            initButton(root);
        }
        root.querySelectorAll?.('[data-metric-info]').forEach(initButton);
    }

    initWithin(document);
    window.initMetricInfo = initWithin;

    const observer = new MutationObserver(function (mutations) {
        mutations.forEach(function (mutation) {
            mutation.addedNodes.forEach(function (node) {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    initWithin(node);
                }
            });
        });
    });
    observer.observe(document.body, { childList: true, subtree: true });
})();
