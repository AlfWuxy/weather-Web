/* 公开县级天气：地点只保存在本机；缺测不能显示安全结论。 */
document.addEventListener('DOMContentLoaded', () => {
    const form = document.querySelector('[data-public-location-form]');
    const field = document.querySelector('[data-public-location]');
    const storageKey = 'public_weather_county';
    if (form && field) {
        try {
            const url = new URL(window.location.href);
            const saved = localStorage.getItem(storageKey);
            if (!url.searchParams.has('location') && saved && saved.length <= 100 && saved !== field.value) {
                url.searchParams.set('location', saved);
                window.location.replace(url.toString());
                return;
            }
            // 以服务器校验后的县名覆盖不可识别的查询，避免反复重定向。
            localStorage.setItem(storageKey, field.value);
        } catch (_) { /* 禁用本地存储时仍可按查询参数浏览。 */ }
        form.addEventListener('submit', () => {
            try { localStorage.setItem(storageKey, field.value.trim()); } catch (_) { /* 无存储也能提交。 */ }
        });
    }
});
