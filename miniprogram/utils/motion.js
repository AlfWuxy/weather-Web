function readMotionSetting(wxApi) {
  const api = wxApi || (typeof wx !== 'undefined' ? wx : null);
  if (!api) return {};
  const settings = {};
  let modernReadSucceeded = false;
  ['getSystemSetting', 'getDeviceInfo'].forEach((method) => {
    try {
      if (typeof api[method] === 'function') {
        Object.assign(settings, api[method]() || {});
        modernReadSucceeded = true;
      }
    } catch (error) {
      // 单个系统信息接口不可用时继续读取其他兼容接口。
    }
  });
  // 基础库固定为 3.7.12；现代接口均不可用时采用默认动画设置。
  if (!modernReadSucceeded) return {};
  return settings;
}

function prefersReducedMotion(wxApi) {
  const settings = readMotionSetting(wxApi);
  return Boolean(
    settings.reduceMotion
    || settings.reducedMotion
    || settings.enableReduceMotion
    || settings.accessibilityReduceMotion
  );
}

function motionDuration(duration, wxApi) {
  return prefersReducedMotion(wxApi) ? 0 : Math.max(0, Number(duration) || 0);
}

module.exports = { motionDuration, prefersReducedMotion, readMotionSetting };
