// 微信公开接口没有可靠的系统减少动态效果字段。JS 驱动动画采用即时完成策略，
// CSS 入场动画继续由 prefers-reduced-motion 媒体查询控制。
function allowsJsMotion() {
  return false;
}

function safeJsDuration(duration) {
  return allowsJsMotion() ? Math.max(0, Number(duration) || 0) : 0;
}

module.exports = { allowsJsMotion, safeJsDuration };
