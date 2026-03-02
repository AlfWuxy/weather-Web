/* Lightweight clipboard helper for HTTP + older browsers.
 *
 * navigator.clipboard requires a secure context (HTTPS / localhost) in most browsers.
 * Our pilot server may run on plain HTTP, so we provide a safe fallback.
 */
(function () {
  'use strict';

  function fallbackCopy(text) {
    try {
      const textarea = document.createElement('textarea');
      textarea.value = String(text ?? '');
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '-9999px';
      textarea.style.left = '-9999px';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(textarea);
      return !!ok;
    } catch (e) {
      return false;
    }
  }

  async function copyText(text) {
    const value = String(text ?? '');
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch (e) {
        // fall through
      }
    }
    return fallbackCopy(value);
  }

  window.CWClipboard = window.CWClipboard || {};
  window.CWClipboard.copyText = copyText;
})();

