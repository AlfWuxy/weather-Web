/* 机构工作台只展示聚合值和质检问题；所有外部文本使用安全 DOM API。 */
(() => {
    'use strict';
    const root = document.getElementById('workbench');
    if (!root) return;
    const $ = (id) => document.getElementById(id);
    const apiBase = root.dataset.apiBase || '/api/v1/workbench';
    const canManage = root.dataset.canManage === 'true';
    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const numberFormat = new Intl.NumberFormat('zh-CN');
    const fields = [
        ['encounter_date', '就诊日期列', true], ['age', '年龄列', true],
        ['source_id', '可靠就诊编号列', false], ['diagnosis', '诊断列', false],
        ['patient_key', '患者记录号（用于核对）', false], ['residence_region_code', '患者居住地区划代码列', false]
    ];
    const state = { institution: '', epoch: 0, controller: null, overview: {}, batches: [], datasets: [], models: [], forecasts: [], jobHistory: [], jobs: new Map(), poll: null, trend: null, prediction: null, comparison: null, dialog: null, loading: false, pending: new Set() };
    const coverageLabels = { complete: '完整报送', partial: '部分报送', unreported: '未报送', missing: '未报送', closed: '停诊', zero: '确认零就诊', confirmed_zero: '确认零就诊', reported: '已报送', ready: '完整报送', incomplete: '部分报送' };
    const statusLabels = { pending: '等待处理', queued: '等待处理', running: '处理中', processing: '处理中', parsing: '检查中', confirming: '正在导入', frozen: '已冻结，正在打包', needs_mapping: '需选择字段', invalid: '存在异常', parsed: '待确认', preview: '待确认', needs_review: '待确认', awaiting_confirmation: '待确认', awaiting_review: '待确认', ready: '已准备好', completed: '已完成', complete: '已完成', success: '已完成', succeeded: '已完成', failed: '处理失败', error: '处理失败', imported: '已导入', committed: '已导入', confirmed: '已导入', superseded: '已被修订', candidate: '候选版本', active: '使用中', inactive: '未启用', retired: '已停用', rejected: '检查未通过', validated: '检查通过', exploratory: '探索性版本', cancelled: '已取消' };
    const doneStatuses = new Set(['succeeded', 'success', 'completed', 'complete', 'failed', 'error', 'cancelled']);
    const importedStatuses = new Set(['imported', 'committed', 'confirmed', 'superseded']);
    const previewStatuses = new Set(['parsed', 'preview', 'ready', 'needs_review', 'needs_mapping', 'invalid', 'awaiting_confirmation', 'awaiting_review']);
    const jobStatuses = new Set(['pending', 'queued', 'running', 'processing', 'parsing', 'confirming', 'frozen']);

    function permission(name) {
        const option = $('wb-institution').selectedOptions[0];
        return option?.dataset[name] === 'true';
    }
    function canChangeModels() { return canManage && permission('canManage'); }
    function applyPermissions() {
        const upload = permission('canUpload') && permission('rawApproved');
        $('wb-upload-button').disabled = !upload || state.pending.has($('wb-upload-button'));
        $('wb-upload-permission-note').hidden = upload;
        $('wb-upload-permission-note').textContent = !permission('canUpload') ? '当前账户只有查看权限。报送由本机构获授权成员完成。' : '此机构尚未确认原件存储和保存安排，暂不能上传真实原表。';
        $('wb-freeze').disabled = !permission('canExport') || !dailyRows().length || state.pending.has($('wb-freeze'));
        if (!permission('canExport')) $('wb-freeze').title = '当前账户没有本机构的数据导出权限。';
        if ($('wb-model-upload-form')) $('wb-model-upload-form').hidden = !canChangeModels();
        if ($('wb-rollback')) $('wb-rollback').hidden = !canChangeModels();
        if ($('wb-capture-forecast')) $('wb-capture-forecast').hidden = !canChangeModels();
    }

    function el(tag, className, value) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (value !== undefined && value !== null) node.textContent = String(value);
        return node;
    }
    function clear(node) { node.replaceChildren(); return node; }
    function fmt(value, decimals) {
        if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
        return decimals === undefined ? numberFormat.format(Number(value)) : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
    }
    function numeric(value) { return value === undefined || value === null || value === '' || !Number.isFinite(Number(value)) ? null : Number(value); }
    function dateOnly(value) { return value ? String(value).slice(0, 10) : '—'; }
    function dateTime(value) {
        if (!value) return '尚无记录';
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', year: 'numeric', hour12: false });
    }
    function validDate(value) {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
        const parsed = new Date(`${value}T00:00:00Z`);
        return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
    }
    function institutionPath(suffix) { return `/institutions/${encodeURIComponent(state.institution)}${suffix}`; }
    function showNotice(message, kind = 'info') {
        const notice = $('wb-notice');
        notice.textContent = message;
        notice.dataset.kind = kind;
        notice.hidden = !message;
        notice.setAttribute('role', kind === 'error' ? 'alert' : 'status');
        if ($('wb-detail-dialog').open && kind === 'error') {
            let inline = $('wb-dialog-notice');
            if (!inline) { inline = el('p', 'wb-inline-note wb-error'); inline.id = 'wb-dialog-notice'; inline.setAttribute('role', 'alert'); $('wb-detail-body').prepend(inline); }
            inline.textContent = message; inline.scrollIntoView({ block: 'nearest', behavior: 'instant' });
        }
    }
    async function request(path, options = {}) {
        const headers = new Headers(options.headers || {});
        headers.set('Accept', 'application/json');
        if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrfToken);
        if (options.json !== undefined) headers.set('Content-Type', 'application/json');
        const response = await fetch(`${apiBase}${path}`, { credentials: 'same-origin', signal: state.controller?.signal, ...options, headers, body: options.json !== undefined ? JSON.stringify(options.json) : options.body });
        const type = response.headers.get('content-type') || '';
        if (!type.includes('application/json')) throw new Error(response.status === 401 || response.redirected ? '登录状态已过期，请重新登录后继续。' : '服务暂时没有返回可读取的结果，请稍后刷新。');
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(typeof data.error === 'string' ? data.error : '请求未完成，请稍后重试。');
        return data;
    }
    function button(text, action, className = 'wb-text-button') {
        const node = el('button', className, text);
        node.type = 'button';
        node.addEventListener('click', action);
        return node;
    }
    async function busy(node, work, label = '处理中…') {
        if (node.disabled) return;
        const original = node.textContent;
        const epoch = state.epoch;
        node.disabled = true;
        node.textContent = label;
        state.pending.add(node);
        $('wb-institution').disabled = true;
        try { await work(); } catch (error) { if (error.name !== 'AbortError' && epoch === state.epoch) showNotice(error.message, 'error'); }
        finally { node.textContent = original; node.disabled = false; state.pending.delete(node); $('wb-institution').disabled = state.pending.size > 0 || !$('wb-institution').value; applyPermissions(); }
    }
    function badge(status) {
        const node = el('span', 'wb-status', statusLabels[status] || '状态待核对');
        if (['active', 'imported', 'committed', 'confirmed', 'succeeded', 'completed', 'ready', 'validated'].includes(status)) node.dataset.tone = 'success';
        else if (['failed', 'error', 'rejected'].includes(status)) node.dataset.tone = 'danger';
        else if (['preview', 'needs_review', 'candidate', 'exploratory'].includes(status)) node.dataset.tone = 'warning';
        return node;
    }
    function empty(target, message) { clear(target).append(el('p', 'wb-empty', message)); }
    function row(title, description, status) {
        const node = el('div', 'wb-list-row');
        const main = el('div', 'wb-row-main');
        main.append(el('p', 'wb-row-title', title), el('p', 'wb-row-description', description));
        node.append(main);
        if (status) node.append(badge(status));
        const actions = el('div', 'wb-row-actions');
        node.append(actions);
        return { node, main, actions };
    }
    function openDialog(title, type, id) {
        const dialog = $('wb-detail-dialog');
        $('wb-detail-title').textContent = title;
        state.dialog = { type, id };
        clear($('wb-detail-body'));
        if (!dialog.open) dialog.showModal();
        return $('wb-detail-body');
    }
    function closeDialog() { $('wb-detail-dialog').close(); state.dialog = null; if (state.comparison) { state.comparison.destroy(); state.comparison = null; } }
    function detailGrid(values) {
        const grid = el('dl', 'wb-detail-grid');
        values.forEach(([label, value]) => {
            const item = el('div'); item.append(el('dt', null, label), el('dd', null, value)); grid.append(item);
        });
        return grid;
    }
    function table(headers, rows) {
        const wrapper = el('div', 'wb-table-scroll');
        const node = el('table', 'wb-table');
        const head = el('thead'); const tr = el('tr');
        headers.forEach((label) => { const th = el('th', null, label); th.scope = 'col'; tr.append(th); });
        head.append(tr); node.append(head);
        const body = el('tbody');
        rows.forEach((values) => { const item = el('tr'); values.forEach((value) => item.append(el('td', null, value))); body.append(item); });
        node.append(body); wrapper.append(node); return wrapper;
    }
    function selectPanel(name, moveFocus = false) {
        document.querySelectorAll('.wb-tab').forEach((tab) => {
            const selected = tab.dataset.panel === name;
            tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
            $(`wb-panel-${tab.dataset.panel}`).hidden = !selected;
            if (selected && moveFocus) tab.focus();
        });
        state.trend?.resize(); state.prediction?.resize();
    }
    function dailyRows() { return Array.isArray(state.overview.daily) ? state.overview.daily : []; }
    function coverage(row) {
        const status = row.coverage_status || row.status || 'unreported';
        if (['complete', 'reported', 'ready'].includes(status) && Number(row.cases_60plus) === 0 && row.cases_60plus !== null) return '确认零就诊';
        return coverageLabels[status] || '待核对';
    }
    function countForChart(row) {
        const status = row.coverage_status || row.status;
        if (['partial', 'unreported', 'missing', 'closed', 'incomplete'].includes(status)) return null;
        return numeric(row.cases_60plus);
    }
    function chartOptions() {
        return { responsive: true, maintainAspectRatio: false, animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches ? { duration: 250 } : false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { display: false }, tooltip: { backgroundColor: '#2e2b28', padding: 12, callbacks: { label: (context) => `${context.dataset.label}：${context.parsed.y === null ? '未提供' : fmt(context.parsed.y, 1)}` } } }, scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 7, maxRotation: 0, font: { size: 11 }, color: '#79736c' }, border: { display: false } }, y: { beginAtZero: true, grid: { color: '#efede9' }, border: { display: false }, ticks: { color: '#79736c', precision: 0, font: { size: 11 } } } } };
    }
    function renderOverview() {
        const overview = state.overview;
        const summary = overview.summary || {};
        $('wb-total-records').textContent = fmt(summary.records);
        $('wb-total-days').textContent = fmt(summary.days);
        $('wb-ready-days').textContent = fmt(summary.ready_days);
        $('wb-last-received').textContent = dateOnly(summary.last_received || overview.latest_reported_at);
        $('wb-last-received-note').textContent = summary.last_received || overview.latest_reported_at ? '按实际收到时间记录' : '等待机构报送';
        const daily = dailyRows();
        $('wb-freeze').disabled = !daily.length;
        $('wb-freeze').title = daily.length ? '' : '确认首批报送后即可冻结数据。';
        const dates = daily.map((item) => item.date).filter(Boolean);
        if (!$('wb-cutoff').value && dates.length) $('wb-cutoff').value = dates[dates.length - 1];
        if (!$('wb-range-start').value && dates.length) $('wb-range-start').value = dates[0];
        if (!$('wb-range-end').value && dates.length) $('wb-range-end').value = dates[dates.length - 1];
        const hasValues = daily.some((item) => countForChart(item) !== null || numeric(item.tmean) !== null);
        $('wb-trend-empty').hidden = hasValues && typeof Chart !== 'undefined';
        if (hasValues && typeof Chart === 'undefined') $('wb-trend-empty').replaceChildren(el('p', null, '图表组件暂未加载，下方每日数据表仍可查看。'));
        if (state.trend) { state.trend.destroy(); state.trend = null; }
        if (hasValues && typeof Chart !== 'undefined') {
            const options = chartOptions();
            options.scales.y.title = { display: true, text: '就诊人次', color: '#79736c', font: { size: 11 } };
            options.scales.temperature = { position: 'right', grid: { drawOnChartArea: false }, border: { display: false }, ticks: { color: '#377baa', font: { size: 11 } }, title: { display: true, text: '°C', color: '#377baa' } };
            state.trend = new Chart($('wb-trend-chart'), { type: 'line', data: { labels: daily.map((item) => item.date), datasets: [
                { label: '60+ 就诊人次', data: daily.map(countForChart), yAxisID: 'y', borderColor: '#bc5b16', backgroundColor: '#bc5b16', borderWidth: 2, pointRadius: daily.length > 45 ? 0 : 2, pointHitRadius: 8, spanGaps: false, tension: .15 },
                { label: '日均温度 °C', data: daily.map((item) => numeric(item.tmean)), yAxisID: 'temperature', borderColor: '#377baa', backgroundColor: '#377baa', borderWidth: 1.6, pointRadius: 0, pointHitRadius: 8, spanGaps: false, tension: .2 }
            ] }, options });
        }
        const tbody = clear($('wb-daily-table'));
        daily.forEach((item) => { const tr = el('tr'); [item.date, coverage(item), fmt(item.cases_60plus), fmt(item.tmean, 1)].forEach((value) => tr.append(el('td', null, value))); tbody.append(tr); });
        if (!daily.length) { const tr = el('tr'); const td = el('td', null, '还没有已确认的日聚合数据。'); td.colSpan = 4; tr.append(td); tbody.append(tr); }
        if (overview.weather_source && overview.weather_product) $('wb-weather-note').textContent = `天气来源：${overview.weather_source} · ${overview.weather_product} 再分析。未报送或部分报送不会补成零；天气关联不等于因果关系。`;
        renderMonths(); applyPermissions();
    }
    function renderMonths() {
        const target = clear($('wb-months'));
        const months = state.overview.months || [];
        if (!months.length) { empty(target, '首批报送确认后，这里会按月份显示完整性。'); return; }
        months.forEach((month) => {
            const status = month.status || (Number(month.reported_days) === 0 ? 'unreported' : Number(month.reported_days) === Number(month.expected_days) ? 'complete' : 'partial');
            const node = button('', () => showMonth(month), 'wb-month');
            node.dataset.state = status;
            node.append(el('strong', null, month.month), el('span', null, `${coverageLabels[status] || '待核对'} · ${fmt(month.reported_days)}/${fmt(month.expected_days)} 天`), el('small', null, `天气 ${fmt(month.weather_days)} 天 · 可分析 ${fmt(month.ready_days)} 天`));
            node.setAttribute('aria-label', `${month.month}，${coverageLabels[status] || '待核对'}，已报送 ${fmt(month.reported_days)} 天，查看详情`);
            target.append(node);
        });
    }
    function showMonth(month) {
        const body = openDialog(`${month.month} · 月份覆盖`, 'month', month.month);
        body.append(detailGrid([['已报送', `${fmt(month.reported_days)} 天`], ['应覆盖', `${fmt(month.expected_days)} 天`], ['同期天气', `${fmt(month.weather_days)} 天`], ['可分析', `${fmt(month.ready_days)} 天`], ['就诊记录', fmt(month.records)]]));
        const days = dailyRows().filter((item) => String(item.date).startsWith(month.month));
        body.append(table(['日期', '报送状态', '60+ 人次', '日均温度 °C'], days.map((item) => [item.date, coverage(item), fmt(item.cases_60plus), fmt(item.tmean, 1)])));
        const sources = state.batches.filter((item) => (item.coverage_start || item.date_start || '') <= `${month.month}-31` && (item.coverage_end || item.date_end || '') >= `${month.month}-01` && importedStatuses.has(item.status));
        body.append(el('h3', 'mt-4', '数据来源'));
        if (!sources.length) body.append(el('p', 'wb-muted', '本月的批次来源可在导入记录中核对。'));
        sources.forEach((item) => body.append(el('p', 'wb-row-description', `${item.filename || '报送文件'} · ${dateTime(item.confirmed_at || item.created_at)}`)));
        body.append(el('p', 'wb-footnote', '零就诊仅来自完整报送的确认。停诊与未报送分别保留，不能作为正常营业日的零值。'));
    }
    function rememberJob(job, related = {}) {
        if (!job?.id || doneStatuses.has(job.status)) return;
        state.jobs.set(String(job.id), { ...related, ...job });
    }
    function extractJobs(items, type) {
        items.forEach((item) => {
            if (item.job) rememberJob(item.job, { type, resourceId: item.id });
            else if (item.job_id && jobStatuses.has(item.status)) rememberJob({ id: item.job_id, status: item.status }, { type, resourceId: item.id });
        });
    }
    function renderJobs() {
        const target = clear($('wb-background-jobs'));
        const visible = state.jobHistory.filter((job) => !doneStatuses.has(job.status) || job.status === 'failed').slice(0, 8);
        target.hidden = !visible.length;
        const labels = { parse: '检查报送文件', confirm: '确认导入数据', weather: '匹配同期天气', dataset: '生成训练包', forecast: '保存天气预报与预测' };
        visible.forEach((job) => {
            const entry = row(labels[job.kind] || '后台处理', job.error || '离开页面后继续处理，结果保留在本机构工作台。', job.status);
            progress(entry.main, { job });
            if (job.status === 'failed' && ((['parse', 'confirm'].includes(job.kind) && permission('canUpload')) || (job.kind === 'dataset' && permission('canExport')) || (['weather', 'forecast'].includes(job.kind) && canChangeModels()))) {
                const retry = button('重新处理', () => busy(retry, async () => {
                    const data = await request(`/jobs/${encodeURIComponent(job.id)}/retry`, { method: 'POST', json: {} }); rememberJob(data.job); showNotice('已重新提交后台任务。', 'success'); await refresh();
                })); entry.actions.append(retry);
            }
            target.append(entry.node);
        });
    }
    function progress(node, item) {
        const job = item.job || (item.job_id ? state.jobs.get(String(item.job_id)) : null);
        if (!job || !jobStatuses.has(job.status)) return;
        const value = numeric(job.progress);
        if (value !== null) {
            const wrapper = el('div', 'wb-job-progress'); wrapper.setAttribute('role', 'progressbar'); wrapper.setAttribute('aria-label', '后台处理进度'); wrapper.setAttribute('aria-valuenow', String(Math.min(100, Math.max(0, value)))); wrapper.setAttribute('aria-valuemin', '0'); wrapper.setAttribute('aria-valuemax', '100');
            const fill = el('span'); fill.style.width = `${Math.min(100, Math.max(0, value))}%`; wrapper.append(fill); node.append(wrapper);
        }
    }
    function renderBatches() {
        const target = clear($('wb-batches'));
        if (!state.batches.length) { empty(target, '还没有报送记录。先上传一份 Excel，检查真实日期和数据完整性。'); return; }
        state.batches.forEach((item) => {
            const start = item.coverage_start || item.date_start || item.report?.date_start;
            const end = item.coverage_end || item.date_end || item.report?.date_end;
            const description = `${dateOnly(start)} 至 ${dateOnly(end)} · ${dateTime(item.created_at)}${item.revision_of ? ' · 修订报送' : ''}`;
            const entry = row(item.filename || '机构报送', description, item.status);
            if (item.error || item.job?.error) entry.main.append(el('p', 'wb-row-description wb-error', item.error || item.job.error));
            progress(entry.main, item);
            entry.actions.append(button(previewStatuses.has(item.status) ? '检查并确认' : '查看详情', () => loadBatch(item.id)));
            if (permission('canUpload') && ['imported', 'committed', 'confirmed'].includes(item.status)) entry.actions.append(button('用修订文件更新', () => beginRevision(item)));
            target.append(entry.node);
        });
    }
    function mappingValues(container, includeEmpty = false) {
        const mapping = {};
        container.querySelectorAll('[data-field]').forEach((input) => { if (input.value.trim() || includeEmpty) mapping[input.dataset.field] = input.value.trim() || null; });
        return mapping;
    }
    function mappingControls(container, headers, mapping = {}, prefix = 'wb-map') {
        clear(container);
        fields.forEach(([key, title, required]) => {
            const wrapper = el('div'); const label = el('label', null, `${title}${required ? ' *' : ''}`); const id = `${prefix}-${key}`; label.htmlFor = id;
            let input;
            if (Array.isArray(headers) && headers.length) {
                input = el('select', 'form-select'); const blank = el('option', null, required ? '请选择对应列' : '不使用此列'); blank.value = ''; input.append(blank);
                headers.forEach((header) => { const option = el('option', null, header); option.value = header; input.append(option); });
            } else { input = el('input', 'form-control'); input.type = 'text'; input.placeholder = key === 'encounter_date' ? '例如：挂号日期' : key === 'age' ? '例如：年龄' : '可留空'; }
            input.id = id; input.dataset.field = key; input.value = mapping[key] || ''; input.autocomplete = 'off'; wrapper.append(label, input); container.append(wrapper);
        });
    }
    function beginRevision(batch) {
        closeDialog(); selectPanel('imports');
        $('wb-revision-of').value = batch.id;
        $('wb-revision-notice').replaceChildren(el('span', null, `正在修订「${batch.filename || '原报送'}」。确认后生成新版本，已冻结的数据包保持不变。`), button('取消修订', resetRevision));
        $('wb-revision-notice').hidden = false;
        $('wb-coverage-start').value = batch.coverage_start || batch.date_start || batch.report?.date_start || '';
        $('wb-coverage-end').value = batch.coverage_end || batch.date_end || batch.report?.date_end || '';
        $('wb-upload-file').focus();
    }
    function resetRevision() { $('wb-revision-of').value = ''; $('wb-revision-notice').hidden = true; }
    async function loadBatch(id) {
        const body = openDialog('报送详情', 'batch', String(id));
        body.append(el('p', 'wb-muted', '正在读取质检结果…'));
        const epoch = state.epoch;
        try {
            const data = await request(`/batches/${encodeURIComponent(id)}`);
            if (epoch !== state.epoch || state.dialog?.id !== String(id)) return;
            const item = { ...data.batch, ...(data.job ? { job: data.job } : {}) };
            rememberJob(data.job, { type: 'batch', resourceId: id });
            showBatch(item); schedulePoll();
        } catch (error) { if (error.name !== 'AbortError' && epoch === state.epoch) { clear(body).append(el('p', 'wb-error', error.message)); } }
    }
    function showBatch(batch) {
        const body = clear($('wb-detail-body'));
        if (state.dialog?.type === 'batch') state.dialog.status = batch.status;
        $('wb-detail-title').textContent = batch.filename || '报送详情';
        body.append(badge(batch.status));
        body.append(el('p', 'wb-row-description', `报送范围：${dateOnly(batch.coverage_start || batch.date_start)} 至 ${dateOnly(batch.coverage_end || batch.date_end)} · ${coverageLabels[batch.coverage_mode] || '完整性待核对'}`));
        if (batch.original_available && permission('canExport')) {
            const original = el('a', 'wb-text-button', '下载原始报送文件'); original.href = `${apiBase}/batches/${encodeURIComponent(batch.id)}/original`; original.download = ''; body.append(original);
        }
        progress(body, batch);
        if (batch.error || batch.job?.error) body.append(el('p', 'wb-inline-note wb-error', batch.error || batch.job.error));
        const report = batch.report || {};
        const counts = report.counts || {};
        if (!report.counts && jobStatuses.has(batch.status)) { body.append(el('p', 'wb-muted mt-3', '文件正在后台检查。可以关闭详情，处理完成后报送记录会更新。')); return; }
        body.append(detailGrid([['总行数', fmt(counts.rows)], ['有效行', fmt(counts.valid)], ['异常行', fmt(counts.invalid)], ['重复行', fmt(counts.duplicates)], ['已有数据重叠', fmt(counts.overlap)], ['缺失诊断', fmt(counts.missing_diagnosis)]]));
        body.append(el('p', 'wb-muted', `实际就诊日期：${dateOnly(report.date_start)} 至 ${dateOnly(report.date_end)}`));
        if (report.issues?.length) {
            body.append(el('h3', 'mt-4', '需要核对的项目'));
            const list = el('ul', 'wb-issues');
            report.issues.forEach((issue) => list.append(el('li', null, typeof issue === 'string' ? issue : `${issue.row ? `第 ${issue.row} 行：` : ''}${issue.message || issue.reason || '需要核对'}`)));
            body.append(list);
            if (report.issues_truncated) body.append(el('p', 'wb-muted', '问题较多，仅展示前 200 项，请按规则检查完整原表。'));
        }
        if (importedStatuses.has(batch.status)) {
            body.append(el('p', 'wb-inline-note', batch.status === 'superseded' ? '这一批已被后续修订替代，原文件和版本仍保留。' : '这一批已确认入库。重复上传同一个文件不会重复增加记录。'));
            if (permission('canUpload') && batch.status !== 'superseded') body.append(button('用修订文件更新', () => beginRevision(batch), 'btn btn-outline-secondary'));
            return;
        }
        if (!permission('canUpload')) { body.append(el('p', 'wb-muted', '报送确认由本机构获授权成员完成。')); return; }
        if (!previewStatuses.has(batch.status) && !report.headers?.length) return;
        body.append(el('h3', 'mt-4', '确认字段对应'));
        body.append(el('p', 'wb-field-help', '就诊编号须对应一次就诊；患者记录号仅用于核对。居住地区划代码仅接受 6、9 或 12 位行政区代码，不接收详细地址。'));
        const mapping = el('div', 'wb-form-grid'); mappingControls(mapping, report.headers, report.mapping, 'wb-confirm-map'); body.append(mapping);
        const changed = () => JSON.stringify(mappingValues(mapping, true)) !== JSON.stringify(Object.fromEntries(fields.map(([key]) => [key, report.mapping?.[key] || null])));
        const reparse = button('保存字段并重新检查', () => busy(reparse, async () => {
            const values = mappingValues(mapping, true);
            if (!values.encounter_date || !values.age) throw new Error('请选择就诊日期列和年龄列。');
            const data = await request(`/batches/${encodeURIComponent(batch.id)}/confirm`, { method: 'POST', json: { mapping: values } });
            rememberJob(data.job, { type: 'batch', resourceId: batch.id });
            if (data.batch) showBatch({ ...data.batch, job: data.job });
            else { closeDialog(); }
            showNotice('字段已提交重新检查。检查完成后请再次核对并确认。', 'success');
            await refresh();
        }), 'btn btn-outline-secondary');
        reparse.hidden = true; body.append(reparse);
        const duplicates = report.duplicate_groups || [];
        const decisions = {};
        if (duplicates.length) {
            body.append(el('h3', 'mt-4', '逐组确认疑似重复'));
            body.append(el('p', 'wb-muted', '这里只显示行号。请对照原始文件核实，避免把真实的多次就诊误删。'));
            duplicates.forEach((group, index) => {
                const wrapper = el('div', 'wb-duplicate'); const label = el('label', null, `第 ${index + 1} 组 · 文件行 ${(group.rows || []).join('、')}${group.existing_count ? ` · 已有 ${group.existing_count} 条` : ''}`);
                const select = el('select', 'form-select'); select.id = `wb-duplicate-${index}`; label.htmlFor = select.id;
                [['', '核对后选择处理方式'], ['keep_first', '仅保留第一条'], ['keep_all', '确认是不同就诊，全部保留'], ['skip', '跳过本组新记录']].forEach(([value, title]) => { const option = el('option', null, title); option.value = value; select.append(option); });
                decisions[group.id] = select; wrapper.append(label, select); body.append(wrapper);
            });
        }
        let revisionAck = null;
        if (report.revision_rows?.length || batch.revision_of || report.revision_of) {
            const label = el('label', 'wb-ack'); revisionAck = el('input'); revisionAck.type = 'checkbox';
            const description = report.revision_rows?.length ? `已核对第 ${report.revision_rows.join('、')} 行，同就诊编号的记录需要更新，并保留历史版本。` : '已核对这份文件是原批次的完整修订；确认后替代原批次，保留历史版本。';
            label.append(revisionAck, el('span', null, description)); body.append(label);
        }
        let missingAck = null;
        if (Number(counts.missing_diagnosis) > 0) {
            const label = el('label', 'wb-ack'); missingAck = el('input'); missingAck.type = 'checkbox';
            label.append(missingAck, el('span', null, `已核对 ${fmt(counts.missing_diagnosis)} 条缺失诊断记录，同意将其按诊断未知保留。`)); body.append(label);
        }
        if (Number(counts.invalid) > 0) body.append(el('p', 'wb-inline-note wb-error', '存在异常行。请先修正字段对应，或修订原始文件后重新上传；当前批次不能整体确认。'));
        const confirmAck = el('label', 'wb-ack'); const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.id = 'wb-confirm-ack';
        confirmAck.append(checkbox, el('span', null, '已核对字段、日期范围、报送完整性及以上问题，确认将这一批数据计入本机构。')); body.append(confirmAck);
        const actions = el('div', 'wb-dialog-actions');
        const submit = button('确认导入这一批', () => busy(submit, async () => {
            if (!checkbox.checked) throw new Error('请先勾选已核对并确认这一批数据。');
            const duplicateDecisions = {};
            for (const [id, select] of Object.entries(decisions)) { if (!select.value) { select.focus(); throw new Error('请逐组确认疑似重复记录。'); } duplicateDecisions[id] = select.value; }
            if (revisionAck && !revisionAck.checked) throw new Error('请先确认修订范围。');
            if (missingAck && !missingAck.checked) throw new Error('请先确认缺失诊断记录的处理方式。');
            const payload = { duplicates: duplicateDecisions };
            if (missingAck) payload.accept_missing_diagnosis = true;
            if (revisionAck) { payload.revision_rows = report.revision_rows || []; payload.confirm_revision = true; }
            const data = await request(`/batches/${encodeURIComponent(batch.id)}/confirm`, { method: 'POST', json: { decisions: payload } });
            rememberJob(data.job, { type: 'batch', resourceId: batch.id }); closeDialog();
            showNotice('已提交导入。后台处理完成后，数据概览会自动更新。', 'success'); await refresh();
        }), 'btn btn-primary');
        submit.disabled = Number(counts.invalid) > 0 || batch.status === 'needs_mapping' || batch.status === 'confirming';
        mapping.addEventListener('change', () => { reparse.hidden = !changed(); submit.disabled = changed() || Number(counts.invalid) > 0 || batch.status === 'needs_mapping'; });
        actions.append(button('暂不确认', closeDialog, 'btn btn-outline-secondary'), submit); body.append(actions);
    }
    function renderDatasets() {
        const target = clear($('wb-datasets'));
        if (!state.datasets.length) { empty(target, '还没有冻结的数据版本。确认报送后，选择截止日期生成第一个训练包。'); return; }
        state.datasets.forEach((item) => {
            const entry = row(item.name || `截至 ${dateOnly(item.cutoff || item.cutoff_date || item.date_end)} 的训练包`, `生成于 ${dateTime(item.created_at)}${item.row_count !== undefined ? ` · ${fmt(item.row_count)} 个日期` : ''}`, item.status || 'ready');
            if (item.error || item.job?.error) entry.main.append(el('p', 'wb-row-description wb-error', item.error || item.job.error));
            progress(entry.main, item);
            if (['ready', 'completed', 'succeeded', 'complete'].includes(item.status || 'ready')) {
                const link = el('a', 'wb-text-button', '下载训练包'); link.href = `${apiBase}/datasets/${encodeURIComponent(item.id)}/download`; link.download = ''; entry.actions.append(link);
            } else if (jobStatuses.has(item.status)) entry.actions.append(el('span', 'wb-muted', '后台生成中，可稍后回来下载'));
            target.append(entry.node);
        });
    }
    function modelAllowed(model) {
        if (typeof model.eligible_to_activate === 'boolean') return model.eligible_to_activate;
        return model.status !== 'active' && model.status !== 'rejected' && model.status !== 'exploratory' && (model.validation?.passed === true || model.validation?.valid === true);
    }
    function modelMetrics(model) { return model.metrics?.holdout?.[model.family] || model.metrics || model.evaluation?.holdout || model.evaluation?.metrics || {}; }
    function metricDescription(model) {
        const metrics = modelMetrics(model); const labels = { mae: 'MAE', rmse: 'RMSE', mean_nll: '平均负对数似然' };
        return Object.entries(labels).filter(([key]) => numeric(metrics[key]) !== null).map(([key, title]) => `${title} ${fmt(metrics[key], 2)}`).join(' · ') || '请查看模型详情中的评估结果';
    }
    function renderModels() {
        const target = clear($('wb-models'));
        const active = state.models.find((item) => item.status === 'active' || item.is_active === true);
        $('wb-model-state').textContent = active ? `当前使用：${active.name || active.family || '就诊计数模型'}。新报送可继续积累，模型不会自动替换。` : '尚未启用模型。上传数据与查看图表不受影响。';
        if ($('wb-capture-forecast')) { $('wb-capture-forecast').disabled = !active; $('wb-capture-forecast').title = active ? '' : '先启用通过检查的模型，才能保存预测。'; }
        const canRollback = !!active && state.models.some((item) => item.id !== active.id && (item.activated_at || item.previously_active || item.status === 'inactive' || item.status === 'retired'));
        if ($('wb-rollback')) { $('wb-rollback').disabled = !canRollback; $('wb-rollback').title = canRollback ? '' : '至少保留一个曾启用的历史版本后，才能回退。'; }
        if (!state.models.length) { empty(target, '还没有模型版本。下载训练包，在本地训练后上传候选模型。'); return; }
        state.models.forEach((item) => {
            const entry = row(item.name || item.family || '本地训练模型', `${dateTime(item.created_at)} · ${metricDescription(item)}`, item.status);
            entry.actions.append(button('查看评估', () => showModel(item)));
            if (canChangeModels() && item.status !== 'active') {
                const activate = button('启用', () => confirmActivation(item)); activate.disabled = !modelAllowed(item);
                activate.title = activate.disabled ? '需通过一致性与对照评估，且不属于探索性版本。' : '查看启用条件并确认'; entry.actions.append(activate);
                if (activate.disabled) entry.main.append(el('p', 'wb-row-description', '暂不能启用：请查看评估中的检查结果。'));
            }
            target.append(entry.node);
        }); applyPermissions();
    }
    function showModel(model) {
        const body = openDialog(model.name || '模型评估', 'model', String(model.id));
        body.append(badge(model.status));
        const family = ['negative_binomial_calendar', 'nb_calendar', 'nb2_calendar'].includes(model.family) ? '负二项日历基线' : ['negative_binomial_weather', 'nb_weather', 'nb2_thermal'].includes(model.family) ? '负二项天气候选' : '就诊计数模型';
        body.append(el('p', 'wb-row-description', `${family} · ${dateTime(model.created_at)}`));
        const metrics = modelMetrics(model);
        const metricLabels = { n_days: '有效评估日', mae: '平均绝对误差 MAE', rmse: '均方根误差 RMSE', mean_nll: '平均负对数似然', daily_interval_coverage: '日预测区间覆盖率', seven_day_mae: '七天累计 MAE' };
        const metricRows = Object.entries(metricLabels).filter(([key]) => metrics[key] !== undefined).map(([key, label]) => [label, fmt(metrics[key], key === 'n_days' ? 0 : 3)]);
        if (metricRows.length) { body.append(el('h3', 'mt-4', '保留评估结果')); body.append(table(['指标', '此版本'], metricRows)); }
        else body.append(el('p', 'wb-inline-note mt-3', '没有可直接比较的汇总指标。请核对上传包的评估报告。'));
        const periods = model.periods || model.split || model.evaluation?.periods || model.training?.periods || {};
        const periodRows = [['train', '训练'], ['development', '开发比较'], ['holdout', '保留评估']].map(([key, label]) => {
            const value = periods[key] || (key === 'development' ? periods.validation : null) || {};
            return [label, dateOnly(value.start || value.date_start), dateOnly(value.end || value.date_end)];
        });
        body.append(el('h3', 'mt-4', '时间范围'), table(['用途', '开始', '结束'], periodRows));
        if (model.metrics?.development && model.metrics?.holdout) {
            const rows = [];
            [['development', '开发比较'], ['holdout', '保留评估']].forEach(([phase, phaseLabel]) => {
                [['nb2_calendar', '日历基线'], ['nb2_thermal', '天气候选']].forEach(([modelFamily, title]) => {
                    const values = model.metrics[phase][modelFamily];
                    if (values) rows.push([phaseLabel, title, fmt(values.mae, 3), fmt(values.mean_nll, 3), fmt(values.n_days)]);
                });
            });
            body.append(el('h3', 'mt-4', '日历基线与天气候选'), table(['用途', '模型', 'MAE', '平均负对数似然', '有效日'], rows), el('p', 'wb-footnote', '候选选择只使用开发比较结果；保留评估用于报告，不能继续用于选择或调参。'));
        }
        body.append(el('p', 'wb-footnote', '只有机构、结局口径、数据版本和评估时段一致时，模型指标才可直接比较。历史实况回测不等同于真实预报验证。'));
        const validation = model.validation || {};
        const errors = validation.errors || validation.reasons || [];
        body.append(el('h3', 'mt-4', '启用检查'));
        body.append(el('p', modelAllowed(model) || model.status === 'active' ? 'wb-inline-note' : 'wb-inline-note wb-error', modelAllowed(model) || model.status === 'active' ? '服务器检查已通过。' : '尚未满足启用条件。'));
        const warnings = [...(Array.isArray(errors) ? errors : [errors]), ...(Array.isArray(validation.warnings) ? validation.warnings : []), validation.activation_block_reason].filter(Boolean);
        if (warnings.length) { const list = el('ul', 'wb-issues'); warnings.forEach((item) => list.append(el('li', null, typeof item === 'string' ? item : item.message || '需要核对评估条件'))); body.append(list); }
        if (model.dataset_id) body.append(el('p', 'wb-footnote', `关联训练包：${model.dataset_id}`));
        const comparisonTarget = el('div'); body.append(comparisonTarget);
        const compare = button('与当前版本按相同日期比较', () => busy(compare, async () => {
            const data = await request(`/models/${encodeURIComponent(model.id)}/comparison`);
            if (state.dialog?.id !== String(model.id) || state.dialog?.type !== 'model') return;
            const comparison = data.comparison || {}; clear(comparisonTarget);
            comparisonTarget.append(el('h3', 'mt-4', '同一数据版本与日期的对照'), el('p', 'wb-muted', `${dateOnly(comparison.period?.start)} 至 ${dateOnly(comparison.period?.end)} · 历史实况天气回测`));
            comparisonTarget.append(table(['指标', '此版本', '当前使用版本'], Object.entries(metricLabels).map(([key, label]) => [label, fmt(comparison.candidate?.[key], key === 'n_days' ? 0 : 3), fmt(comparison.current?.[key], key === 'n_days' ? 0 : 3)])));
            if (!comparison.current) comparisonTarget.append(el('p', 'wb-muted', '尚无启用版本，当前仅显示候选评估。'));
            if (comparison.delay_replay_performed !== true) comparisonTarget.append(el('p', 'wb-footnote', '历史报送时点尚未核实，未完成报送延迟重放；该结果不作为当时即可取得数据的真实运行验证。'));
            if (comparison.series?.length && typeof Chart !== 'undefined') {
                const wrapper = el('div', 'wb-chart mt-3'); const canvas = el('canvas'); canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', '候选模型保留评估的预测区间与实际值'); wrapper.append(canvas); comparisonTarget.append(wrapper);
                if (state.comparison) state.comparison.destroy();
                const options = chartOptions(); options.plugins.legend = { display: true, position: 'bottom', labels: { filter: (item) => item.text !== '区间下限', boxWidth: 9, font: { size: 10 } } };
                state.comparison = new Chart(canvas, { type: 'line', data: { labels: comparison.series.map((item) => item.date), datasets: [
                    { label: '区间下限', data: comparison.series.map((item) => numeric(item.lower_95 ?? item.lower)), borderWidth: 0, pointRadius: 0, fill: false },
                    { label: '日预测区间', data: comparison.series.map((item) => numeric(item.upper_95 ?? item.upper)), borderWidth: 0, pointRadius: 0, fill: '-1', backgroundColor: 'rgba(188,91,22,.10)' },
                    { label: '候选预测', data: comparison.series.map((item) => numeric(item.mean)), borderColor: '#bc5b16', backgroundColor: '#bc5b16', pointRadius: 0, borderWidth: 1.7 },
                    { label: '实际人次', data: comparison.series.map((item) => numeric(item.actual)), borderColor: '#377baa', backgroundColor: '#377baa', pointRadius: 0, borderWidth: 1.7 }
                ] }, options });
            }
        }), 'btn btn-outline-secondary mt-3'); body.append(compare);
        if (canChangeModels() && modelAllowed(model)) body.append(button('查看并确认启用', () => confirmActivation(model), 'btn btn-primary mt-3 ms-2'));
    }
    function confirmActivation(model) {
        const body = openDialog('确认启用候选模型', 'activate', String(model.id));
        body.append(el('p', null, `启用「${model.name || '此候选模型'}」后，后续保存的预测将使用这个版本。`), el('p', 'wb-muted', '已保存的预测和历史评估保持原版本，可在需要时回退。'));
        const label = el('label', 'wb-ack'); const ack = el('input'); ack.type = 'checkbox'; label.append(ack, el('span', null, '我已核对机构、数据版本、评估时段与对照结果，决定启用此版本。')); body.append(label);
        const actions = el('div', 'wb-dialog-actions');
        const submit = button('确认启用', () => busy(submit, async () => {
            if (!ack.checked) throw new Error('请先确认已核对评估条件。');
            await request(`/models/${encodeURIComponent(model.id)}/activate`, { method: 'POST', json: {} }); closeDialog(); showNotice('已启用所选模型。之后保存的预测会记录此版本。', 'success'); await refresh();
        }), 'btn btn-primary'); actions.append(button('返回评估', () => showModel(model), 'btn btn-outline-secondary'), submit); body.append(actions);
    }
    function forecastRows(forecast) { return forecast.daily || forecast.predictions || forecast.payload?.daily || []; }
    function renderForecasts() {
        const target = clear($('wb-forecasts')); clear($('wb-forecast-summary'));
        const forecast = state.forecasts[0];
        if (state.prediction) { state.prediction.destroy(); state.prediction = null; }
        $('wb-prediction-empty').hidden = false;
        $('wb-prediction-empty').querySelector('p').textContent = '还没有保存的预测记录。';
        if (!forecast) { $('wb-prediction-note').textContent = '启用模型后，开始积累真实预报验证记录。'; return; }
        const daily = forecastRows(forecast);
        const issued = forecast.received_at || forecast.issued_at || forecast.created_at;
        $('wb-prediction-note').textContent = `最近保存于 ${dateTime(issued)}。图中使用当时收到的天气预报，不用之后的实况替换。`;
        if (forecast.interval_limitation) $('wb-prediction-note').textContent += ` ${forecast.interval_limitation}`;
        if (forecast.status === 'unavailable') $('wb-prediction-empty').querySelector('p').textContent = forecast.reason || '本次天气输入不完整，未生成预测。可核对后重新采集。';
        if (daily.length && typeof Chart !== 'undefined') {
            $('wb-prediction-empty').hidden = true;
            const options = chartOptions(); options.plugins.legend = { display: true, position: 'bottom', labels: { usePointStyle: true, boxWidth: 7, font: { size: 11 }, filter: (item) => item.text !== '区间下限' } };
            const means = daily.map((item) => numeric(item.mean ?? item.predicted_mean ?? item.prediction));
            const hasInterval = daily.some((item) => numeric(item.lower_95 ?? item.lower) !== null && numeric(item.upper_95 ?? item.upper) !== null);
            const datasets = [];
            if (hasInterval) datasets.push({ label: '区间下限', data: daily.map((item) => numeric(item.lower_95 ?? item.lower)), borderWidth: 0, pointRadius: 0, fill: false }, { label: '日预测区间', data: daily.map((item) => numeric(item.upper_95 ?? item.upper)), borderWidth: 0, pointRadius: 0, fill: '-1', backgroundColor: 'rgba(188,91,22,.10)' });
            datasets.push({ label: '预测就诊', data: means, borderColor: '#bc5b16', backgroundColor: '#bc5b16', borderWidth: 2, pointRadius: 2, spanGaps: false }, { label: '实际就诊', data: daily.map((item) => numeric(item.observed ?? item.actual ?? item.cases_60plus)), borderColor: '#377baa', backgroundColor: '#377baa', borderWidth: 2, pointRadius: 2, spanGaps: false });
            state.prediction = new Chart($('wb-prediction-chart'), { type: 'line', data: { labels: daily.map((item) => item.date), datasets }, options });
        }
        const total = forecast.seven_day_total ?? forecast.seven_day_cumulative ?? forecast.cumulative_mean;
        const day7 = forecast.day7_mean ?? forecast.day_seven_mean;
        [['未来七天累计', total], ['第七天', day7]].forEach(([title, value]) => {
            if (numeric(value) === null) return;
            const item = el('div'); item.append(el('span', null, title), el('strong', null, `${fmt(value, 1)} 人次`)); $('wb-forecast-summary').append(item);
        });
        state.forecasts.slice(0, 12).forEach((item) => {
            const entry = row(`保存于 ${dateTime(item.received_at || item.issued_at || item.created_at)}`, `${dateOnly(item.start_date || forecastRows(item)[0]?.date)} 起 · ${item.source_label || '当时收到的天气预报'}`);
            if (item.reason) entry.main.append(el('p', 'wb-row-description wb-error', item.reason));
            entry.actions.append(button('查看记录', () => {
                const body = openDialog('当时保存的预测', 'forecast', String(item.id));
                body.append(el('p', 'wb-muted', `保存时间：${dateTime(item.received_at || item.issued_at || item.created_at)}`));
                if (item.reason) body.append(el('p', 'wb-inline-note wb-error', item.reason));
                if (item.interval_limitation) body.append(el('p', 'wb-footnote', item.interval_limitation));
                body.append(table(['日期', '预测人次', '日区间下限', '日区间上限', '实际人次'], forecastRows(item).map((day) => [day.date, fmt(day.mean ?? day.predicted_mean ?? day.prediction, 2), fmt(day.lower_95 ?? day.lower, 2), fmt(day.upper_95 ?? day.upper, 2), fmt(day.observed ?? day.actual ?? day.cases_60plus)])));
                body.append(el('p', 'wb-footnote', '实际就诊尚未完整报送时保持空缺。未来七天累计不展示未经验证的累计置信区间。'));
            })); target.append(entry.node);
        });
    }
    async function refresh() {
        if (!state.institution) return;
        const epoch = state.epoch;
        state.loading = true;
        $('wb-refresh').disabled = true;
        const query = new URLSearchParams();
        if ($('wb-range-start').value) query.set('start', $('wb-range-start').value);
        if ($('wb-range-end').value) query.set('end', $('wb-range-end').value);
        const requests = [
            ['数据概览', () => request(institutionPath(`/overview?${query}`)), (data) => { state.overview = data.overview || data; renderOverview(); }],
            ['报送记录', () => request(institutionPath('/batches')), (data) => { state.batches = data.batches || []; extractJobs(state.batches, 'batch'); renderBatches(); }],
            ['训练包', () => permission('canExport') ? request(institutionPath('/datasets')) : Promise.resolve({ datasets: [] }), (data) => { state.datasets = data.datasets || []; extractJobs(state.datasets, 'dataset'); renderDatasets(); if (!permission('canExport')) empty($('wb-datasets'), '当前账户没有本机构的数据导出权限。'); }],
            ['模型版本', () => request(institutionPath('/models')), (data) => { state.models = data.models || []; renderModels(); }],
            ['预测记录', () => request(institutionPath('/forecasts')), (data) => { state.forecasts = data.forecasts || []; renderForecasts(); }],
            ['后台任务', () => request(institutionPath('/jobs')), (data) => { state.jobHistory = data.jobs || []; state.jobHistory.forEach((job) => rememberJob(job)); renderJobs(); }]
        ];
        const results = await Promise.allSettled(requests.map(([, run]) => run()));
        if (epoch !== state.epoch) return;
        const errors = [];
        results.forEach((result, index) => {
            if (result.status === 'fulfilled') requests[index][2](result.value);
            else if (result.reason.name !== 'AbortError') errors.push(`${requests[index][0]}：${result.reason.message}`);
        });
        if (errors.length) showNotice(errors.join(' '), 'error');
        state.loading = false; $('wb-refresh').disabled = false; schedulePoll();
    }
    function schedulePoll() {
        clearTimeout(state.poll);
        if (!state.jobs.size || !state.institution) return;
        state.poll = setTimeout(pollJobs, document.hidden ? 10000 : 3500);
    }
    async function pollJobs() {
        if (!state.jobs.size || state.loading) { schedulePoll(); return; }
        const epoch = state.epoch;
        const jobs = Array.from(state.jobs.entries());
        const results = await Promise.allSettled(jobs.map(([id]) => request(`/jobs/${encodeURIComponent(id)}`)));
        if (epoch !== state.epoch) return;
        let changed = false;
        results.forEach((result, index) => {
            const [id, old] = jobs[index];
            if (result.status === 'rejected') { state.jobs.delete(id); if (result.reason.name !== 'AbortError') showNotice(`进度暂未同步：${result.reason.message} 可点击刷新重试，后台任务会继续。`, 'error'); return; }
            const job = result.value.job || result.value;
            if (doneStatuses.has(job.status)) {
                state.jobs.delete(id); changed = true;
                if (job.status === 'failed' || job.status === 'error') showNotice(job.error || '后台处理失败，请在导入记录查看原因。', 'error');
            } else { state.jobs.set(id, { ...old, ...job }); if (job.status !== old.status || job.progress !== old.progress) changed = true; }
        });
        if (changed) {
            await refresh();
            if (state.dialog?.type === 'batch') {
                const batch = state.batches.find((item) => String(item.id) === state.dialog.id);
                if (batch && batch.status !== state.dialog.status) await loadBatch(batch.id);
            }
        }
        schedulePoll();
    }
    function resetInstitution() {
        state.epoch += 1; state.controller?.abort(); state.controller = new AbortController(); clearTimeout(state.poll); state.jobs.clear();
        state.institution = $('wb-institution').value; state.overview = {}; state.batches = []; state.datasets = []; state.models = []; state.forecasts = []; state.jobHistory = []; state.loading = false;
        closeDialog(); resetRevision(); $('wb-upload-form').reset(); $('wb-range-start').value = ''; $('wb-range-end').value = ''; $('wb-cutoff').value = ''; if ($('wb-model-upload-form')) $('wb-model-upload-form').reset();
        showNotice(''); renderOverview(); renderBatches(); renderDatasets(); renderModels(); renderForecasts(); renderJobs(); applyPermissions();
        if (state.institution) refresh();
    }
    document.querySelectorAll('.wb-tab').forEach((tab) => {
        tab.addEventListener('click', () => selectPanel(tab.dataset.panel));
        tab.addEventListener('keydown', (event) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault(); const tabs = Array.from(document.querySelectorAll('.wb-tab')); const index = tabs.indexOf(tab);
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
            selectPanel(tabs[next].dataset.panel, true);
        });
    });
    document.querySelectorAll('[data-open-import]').forEach((node) => node.addEventListener('click', () => { selectPanel('imports'); $('wb-upload-file').focus(); }));
    document.querySelectorAll('[data-close-dialog]').forEach((node) => node.addEventListener('click', closeDialog));
    $('wb-detail-dialog').addEventListener('close', () => { state.dialog = null; });
    $('wb-detail-dialog').addEventListener('click', (event) => { if (event.target === $('wb-detail-dialog')) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog(); } });
    $('wb-institution').addEventListener('change', resetInstitution);
    $('wb-refresh').addEventListener('click', () => { showNotice(''); refresh(); });
    $('wb-range-form').addEventListener('submit', (event) => {
        event.preventDefault();
        if ($('wb-range-start').value && $('wb-range-end').value && $('wb-range-start').value > $('wb-range-end').value) { showNotice('开始日期不能晚于结束日期。', 'error'); return; }
        refresh();
    });
    mappingControls($('wb-upload-mapping'), null);
    $('wb-upload-form').addEventListener('submit', (event) => {
        event.preventDefault(); const form = event.currentTarget; if (!form.reportValidity()) return;
        busy($('wb-upload-button'), async () => {
            const file = $('wb-upload-file').files[0]; if (!file || !file.name.toLowerCase().endsWith('.xlsx')) throw new Error('请选择 .xlsx 格式的 Excel 文件。');
            const start = $('wb-coverage-start').value; const end = $('wb-coverage-end').value;
            if (!validDate(start) || !validDate(end) || start > end) throw new Error('请填写有效的报送起止日期，开始日期不能晚于结束日期。');
            const closedDates = $('wb-closed-dates').value.split(/[,，;；\s]+/).map((value) => value.trim()).filter(Boolean);
            if (closedDates.some((value) => !validDate(value) || value < start || value > end)) throw new Error('停诊日期需要使用 YYYY-MM-DD 格式，并处于本次报送范围内。');
            const body = new FormData(); body.append('file', file); body.append('coverage_start', start); body.append('coverage_end', end); body.append('coverage_mode', form.querySelector('[name="coverage_mode"]:checked').value); body.append('closed_dates', JSON.stringify([...new Set(closedDates)])); body.append('mapping', JSON.stringify(mappingValues($('wb-upload-mapping')))); body.append('authorized', 'true');
            if ($('wb-revision-of').value) body.append('revision_of', $('wb-revision-of').value);
            const data = await request(institutionPath('/batches'), { method: 'POST', body });
            rememberJob(data.job, { type: 'batch', resourceId: data.batch?.id }); form.reset(); resetRevision();
            showNotice(data.duplicate || data.existing ? '这个文件已提交过，已打开原有报送记录。' : '文件已收到，正在后台检查。离开页面后仍会继续。', 'success');
            await refresh(); if (data.batch?.id) await loadBatch(data.batch.id);
        }, '上传中…');
    });
    $('wb-dataset-form').addEventListener('submit', (event) => {
        event.preventDefault(); if (!event.currentTarget.reportValidity()) return;
        busy($('wb-freeze'), async () => {
            const cutoff = $('wb-cutoff').value; if (!validDate(cutoff)) throw new Error('请选择有效的截止日期。');
            const data = await request(institutionPath('/datasets'), { method: 'POST', json: { cutoff } });
            rememberJob(data.job, { type: 'dataset', resourceId: data.dataset?.id }); showNotice('已请求冻结数据。训练包生成后可在下方下载。', 'success'); await refresh();
        }, '正在冻结…');
    });
    $('wb-model-upload-form')?.addEventListener('submit', (event) => {
        event.preventDefault(); if (!event.currentTarget.reportValidity()) return; const submit = event.currentTarget.querySelector('button[type="submit"]');
        busy(submit, async () => {
            const file = $('wb-model-file').files[0]; if (!file || !file.name.toLowerCase().endsWith('.json')) throw new Error('请选择本地训练生成的 JSON 模型包。');
            if (file.size > 2 * 1024 * 1024) throw new Error('模型包不能超过 2 MB。请仅上传参数与评估信息。');
            let payload; try { payload = JSON.parse(await file.text()); } catch { throw new Error('文件不是有效的 JSON，请重新选择训练入口生成的模型包。'); }
            const data = await request(institutionPath('/models'), { method: 'POST', json: payload }); $('wb-model-file').value = ''; showNotice('候选模型已收到，请查看评估和启用检查结果。', 'success'); await refresh(); if (data.model) showModel(data.model);
        }, '检查模型包…');
    });
    $('wb-rollback')?.addEventListener('click', () => {
        const body = openDialog('回退模型版本', 'rollback', state.institution);
        body.append(el('p', null, '将后续预测切回上一个启用版本。已经保存的预测仍保留当时的模型记录。'));
        const submit = button('确认回退', () => busy(submit, async () => { await request(institutionPath('/rollback'), { method: 'POST', json: {} }); closeDialog(); showNotice('已回退到上一启用版本。', 'success'); await refresh(); }), 'btn btn-primary');
        const actions = el('div', 'wb-dialog-actions'); actions.append(button('取消', closeDialog, 'btn btn-outline-secondary'), submit); body.append(actions);
    });
    $('wb-capture-forecast')?.addEventListener('click', (event) => busy(event.currentTarget, async () => {
        const data = await request(institutionPath('/forecasts'), { method: 'POST', json: {} }); rememberJob(data.job, { type: 'forecast' }); showNotice('已请求保存本次天气预报与预测，完成后自动更新。', 'success'); await refresh();
    }, '正在保存…'));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) schedulePoll(); });
    resetInstitution();
})();
