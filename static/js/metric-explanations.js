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

    let activeController = null;
    let popoverSerial = 0;

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

    function clearActiveTriggers(instance) {
        if (!instance || !instance._activeTrigger) {
            return;
        }
        Object.keys(instance._activeTrigger).forEach(function (triggerName) {
            instance._activeTrigger[triggerName] = false;
        });
    }

    function hasActiveTrigger(instance) {
        return Boolean(
            instance
            && instance._activeTrigger
            && Object.values(instance._activeTrigger).some(Boolean)
        );
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
        let tip = null;
        let openedFromKeyboard = false;
        let restoreFocusAfterHide = false;
        const instance = new bootstrap.Popover(button, {
            container: 'body',
            customClass: 'yl-metric-popover',
            delay: { show: 80, hide: 350 },
            html: true,
            placement: 'auto',
            sanitize: true,
            title: metric.title,
            trigger: 'hover click',
            content: buildContent(metric, context, button.dataset.detailsUrl),
        });

        const controller = {
            button,
            close: function (restoreFocus = false) {
                restoreFocusAfterHide = restoreFocus;
                clearActiveTriggers(instance);
                instance.hide();
            },
            contains: function (target) {
                return Boolean(
                    target
                    && (button.contains(target) || (tip && tip.contains(target)))
                );
            },
        };

        const configureTip = function () {
            tip = instance.tip;
            if (!(tip instanceof HTMLElement)) {
                return;
            }

            if (!tip.id) {
                popoverSerial += 1;
                tip.id = `yl-metric-popover-${popoverSerial}`;
            }
            tip.setAttribute('role', 'dialog');
            tip.setAttribute('aria-modal', 'false');
            button.setAttribute('aria-controls', tip.id);

            const heading = tip.querySelector('.popover-header');
            if (heading) {
                heading.id = `${tip.id}-title`;
                tip.setAttribute('aria-labelledby', heading.id);
            }

            if (tip.dataset.metricInteractiveReady === '1') {
                return;
            }
            tip.dataset.metricInteractiveReady = '1';

            tip.addEventListener('mouseenter', function () {
                if (instance._activeTrigger) {
                    instance._activeTrigger.hover = true;
                }
            });
            tip.addEventListener('mouseleave', function () {
                if (instance._activeTrigger) {
                    instance._activeTrigger.hover = false;
                }
                if (!hasActiveTrigger(instance)) {
                    instance.hide();
                }
            });
            tip.addEventListener('focusin', function () {
                if (instance._activeTrigger) {
                    instance._activeTrigger.focus = true;
                }
            });
            tip.addEventListener('focusout', function () {
                window.setTimeout(function () {
                    if (tip && !tip.contains(document.activeElement) && document.activeElement !== button) {
                        controller.close(false);
                    }
                }, 0);
            });
            tip.addEventListener('keydown', function (event) {
                if (event.key === 'Tab') {
                    const focusableItems = Array.from(tip.querySelectorAll(
                        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
                    ));
                    const firstItem = focusableItems[0];
                    const lastItem = focusableItems[focusableItems.length - 1];
                    const leavesPopover = (
                        (!event.shiftKey && document.activeElement === lastItem)
                        || (event.shiftKey && document.activeElement === firstItem)
                    );
                    if (leavesPopover) {
                        event.preventDefault();
                        event.stopPropagation();
                        controller.close(true);
                    }
                    return;
                }
                if (event.key !== 'Escape') {
                    return;
                }
                event.preventDefault();
                event.stopPropagation();
                controller.close(true);
            });
        };

        button.dataset.metricInfoReady = '1';
        button.addEventListener('show.bs.popover', function () {
            if (activeController && activeController !== controller) {
                activeController.close(false);
            }
            activeController = controller;
            button.setAttribute('aria-expanded', 'true');
        });
        button.addEventListener('inserted.bs.popover', configureTip);
        button.addEventListener('shown.bs.popover', function () {
            configureTip();
            button.setAttribute('aria-expanded', 'true');
            if (openedFromKeyboard && tip) {
                const focusTarget = tip.querySelector('.yl-popover-link') || tip;
                focusTarget.focus({ preventScroll: true });
            }
            openedFromKeyboard = false;
        });
        button.addEventListener('hidden.bs.popover', function () {
            button.setAttribute('aria-expanded', 'false');
            button.removeAttribute('aria-controls');
            if (activeController === controller) {
                activeController = null;
            }
            if (restoreFocusAfterHide) {
                button.focus({ preventScroll: true });
            }
            restoreFocusAfterHide = false;
            tip = null;
        });
        button.addEventListener('click', function (event) {
            openedFromKeyboard = event.detail === 0;
        });
        button.addEventListener('keydown', function (event) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openedFromKeyboard = true;
                if (button.getAttribute('aria-expanded') === 'true') {
                    controller.close(true);
                } else {
                    if (instance._activeTrigger) {
                        instance._activeTrigger.click = true;
                    }
                    instance.show();
                }
                return;
            }
            if (event.key === 'Escape') {
                event.preventDefault();
                controller.close(true);
            }
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

    document.addEventListener('pointerdown', function (event) {
        if (activeController && !activeController.contains(event.target)) {
            activeController.close(false);
        }
    }, true);

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
