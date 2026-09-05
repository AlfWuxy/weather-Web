(function () {
    const root = document.getElementById('helpInboxRoot');
    if (!root) return;
    const listUrl = root.getAttribute('data-list-url');
    const csrf = root.getAttribute('data-csrf') || '';
    const pendingList = document.getElementById('pendingAckList');
    const openList = document.getElementById('openList');
    const emptyEl = document.getElementById('inboxEmpty');
    const errorEl = document.getElementById('inboxError');
    const staleEl = document.getElementById('inboxStale');
    const countEl = document.getElementById('inboxOpenCount');
    const tpl = document.getElementById('helpCardTpl');
    let timer = null;
    let inflight = false;
    let lastItems = null;
    let lastFetchedAt = null;
    let editing = false;

    function headers() {
        return {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-CSRF-Token': csrf
        };
    }

    function showError(on) {
        errorEl.classList.toggle('d-none', !on);
        if (on) emptyEl.classList.add('d-none');
    }

    function render(data) {
        const items = (data && data.items) || [];
        lastItems = items;
        lastFetchedAt = new Date();
        pendingList.innerHTML = '';
        openList.innerHTML = '';
        const pending = items.filter(function (item) { return item.status === 'pending_ack'; });
        const rest = items.filter(function (item) { return item.status !== 'pending_ack'; });
        countEl.textContent = '未结 ' + (data.open_count || items.length);
        const badge = document.getElementById('navHelpBadge');
        if (badge) {
            const n = Number(data.open_count || items.length) || 0;
            badge.textContent = n > 99 ? '99+' : String(n);
            badge.classList.toggle('d-none', n < 1);
        }
        emptyEl.classList.toggle('d-none', items.length > 0);
        showError(false);
        staleEl.classList.add('d-none');
        function add(item, parent) {
            const node = tpl.content.cloneNode(true);
            node.querySelector('.help-status-label').textContent = item.status_label || item.status;
            const elderEl = node.querySelector('.help-elder-label');
            if (elderEl) elderEl.textContent = item.elder_label || '照护对象';
            node.querySelector('.help-id').textContent = item.id;
            node.querySelector('.help-meta').textContent = [
                '来源 ' + (item.origin_channel === 'miniprogram' ? '微信' : '网页'),
                item.created_at ? ('发起 ' + item.created_at) : '',
                item.acknowledged_at ? ('收到 ' + item.acknowledged_at) : '等待家属接收'
            ].filter(Boolean).join(' · ');
            const actions = node.querySelector('.help-actions');
            (item.allowed_actions || []).forEach(function (name) {
                const btn = document.createElement('button');
                btn.type = 'button';
                const labels = { ack: '已收到', start: '开始处理', resolve: '已解决', cancel: '取消求助' };
                const classes = { ack: 'btn-primary', start: 'btn-primary', resolve: 'btn-success', cancel: 'btn-outline-secondary' };
                btn.className = 'btn btn-sm ' + (classes[name] || 'btn-outline-secondary');
                btn.textContent = labels[name] || name;
                btn.addEventListener('click', function () { act(item, name, btn); });
                actions.appendChild(btn);
            });
            const link = document.createElement('a');
            link.className = 'btn btn-sm btn-outline-secondary';
            link.href = '/caregiver/help/' + encodeURIComponent(item.id);
            link.textContent = '详情';
            actions.appendChild(link);
            parent.appendChild(node);
        }
        pending.forEach(function (item) { add(item, pendingList); });
        rest.forEach(function (item) { add(item, openList); });
    }

    function load() {
        if (document.hidden || inflight || editing) return;
        inflight = true;
        fetch(listUrl + '?status=open&limit=50', { headers: headers(), credentials: 'same-origin' })
            .then(function (res) {
                return res.json().then(function (data) { return { ok: res.ok, data: data }; });
            })
            .then(function (result) {
                if (!result.ok || !result.data || !result.data.success) {
                    if (!lastItems) showError(true);
                    else {
                        staleEl.textContent = '当前显示的是 ' + (lastFetchedAt ? lastFetchedAt.toLocaleString() : '之前') + ' 的记录，刷新失败。';
                        staleEl.classList.remove('d-none');
                    }
                    return;
                }
                render(result.data.data || {});
            })
            .catch(function () {
                if (!lastItems) showError(true);
                else {
                    staleEl.textContent = '网络中断，仍显示上次成功读取的记录。';
                    staleEl.classList.remove('d-none');
                }
            })
            .finally(function () { inflight = false; });
    }

    function act(item, name, btn) {
        const pathMap = { ack: '/ack', start: '/start', resolve: '/resolve', cancel: '/cancel' };
        const path = pathMap[name];
        if (!path) return;
        const body = { expected_version: item.version, idempotency_key: item.id + ':' + name };
        if (name === 'resolve') body.resolution_code = 'reached_elder';
        if (name === 'cancel') body.cancel_reason = 'other';
        btn.disabled = true;
        fetch('/api/v1/help-requests/' + encodeURIComponent(item.id) + path, {
            method: 'POST',
            headers: headers(),
            credentials: 'same-origin',
            body: JSON.stringify(body)
        }).then(function (res) { return res.json().then(function (data) { return { ok: res.ok, status: res.status, data: data }; }); })
            .then(function (result) {
                if (result.status === 409) {
                    const card = btn.closest('.help-card');
                    if (card) card.querySelector('.help-conflict').classList.remove('d-none');
                    load();
                    return;
                }
                if (!result.ok || !result.data.success) throw new Error('fail');
                load();
            }).catch(function () {
                alert('操作未能完成，请重试。未成功不会被记成已解决。');
            }).finally(function () { btn.disabled = false; });
    }

    document.getElementById('inboxRetry').addEventListener('click', function () { load(); });
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            if (timer) { clearInterval(timer); timer = null; }
            return;
        }
        load();
        if (!timer) timer = setInterval(load, 5000 + Math.floor(Math.random() * 800));
    });
    try {
        const initialEl = document.getElementById('helpInboxInitial');
        render(JSON.parse((initialEl && initialEl.textContent) || '{}'));
    } catch (e) {
        showError(true);
    }
    timer = setInterval(load, 5000 + Math.floor(Math.random() * 800));
})();
