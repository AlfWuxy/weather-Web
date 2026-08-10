(function () {
  'use strict';

  document.addEventListener('DOMContentLoaded', function () {
    const copyButton = document.getElementById('copyFamilyReminder');
    if (!copyButton) return;

    const message = document.getElementById('familyReminderMessage');
    const question = document.getElementById('familyReminderQuestion');
    const status = document.getElementById('familyReminderCopyStatus');
    const label = copyButton.querySelector('[data-copy-label]');
    let resetTimer = null;

    copyButton.addEventListener('click', async function () {
      const text = [
        message ? message.textContent.trim() : '',
        question ? question.textContent.trim() : '',
      ].filter(Boolean).join('\n');
      window.clearTimeout(resetTimer);
      if (status) status.textContent = '';

      try {
        const copied = (
          window.CWClipboard
          && typeof window.CWClipboard.copyText === 'function'
          && await window.CWClipboard.copyText(text)
        );
        if (!copied) throw new Error('copy_failed');
        if (label) label.textContent = '已复制';
        if (status) status.textContent = '今日提醒已复制到剪贴板。';
      } catch (_error) {
        if (label) label.textContent = '复制失败，请手动选择文字';
        if (status) status.textContent = '复制失败，请手动选择提醒文字。';
      }

      resetTimer = window.setTimeout(function () {
        if (label) label.textContent = '复制今日提醒';
        if (status) status.textContent = '';
      }, 2500);
    });
  });
})();
