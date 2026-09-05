const EARTH_RADIUS_M = 6371000;

function finiteCoordinate(value, minimum, maximum) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  if (!Number.isFinite(number) || number < minimum || number > maximum) return null;
  return number;
}

function normalizePoint(value) {
  const source = value && typeof value === 'object' ? value : {};
  const latitude = finiteCoordinate(
    source.latitude !== undefined ? source.latitude : source.lat,
    -90,
    90
  );
  const longitudeSource = source.longitude !== undefined
    ? source.longitude
    : (source.lng !== undefined ? source.lng : source.lon);
  const longitude = finiteCoordinate(longitudeSource, -180, 180);
  if (latitude === null || longitude === null) return null;
  return { latitude, longitude };
}

function haversineDistanceMeters(pointA, pointB) {
  const start = normalizePoint(pointA);
  const end = normalizePoint(pointB);
  if (!start || !end) return null;
  const toRadians = (degrees) => degrees * Math.PI / 180;
  const latitudeA = toRadians(start.latitude);
  const latitudeB = toRadians(end.latitude);
  const deltaLatitude = latitudeB - latitudeA;
  const deltaLongitude = toRadians(end.longitude - start.longitude);
  const haversine = (
    Math.sin(deltaLatitude / 2) ** 2
    + Math.cos(latitudeA) * Math.cos(latitudeB) * Math.sin(deltaLongitude / 2) ** 2
  );
  const centralAngle = 2 * Math.asin(Math.min(1, Math.sqrt(Math.max(0, haversine))));
  return EARTH_RADIUS_M * centralAngle;
}

function formatDistanceMeters(value) {
  if (value === null || value === undefined || value === '') return '';
  const distance = Number(value);
  if (!Number.isFinite(distance) || distance < 0) return '';
  if (distance < 1000) return `约 ${Math.round(distance / 10) * 10} 米`;
  const kilometres = distance / 1000;
  return `约 ${kilometres < 10 ? kilometres.toFixed(1) : Math.round(kilometres)} 公里`;
}

function sortResourcesByDistance(resources, origin) {
  const source = Array.isArray(resources) ? resources : [];
  const userPoint = normalizePoint(origin);
  return source
    .map((resource, originalIndex) => {
      const targetPoint = normalizePoint(resource);
      const distanceMeters = userPoint && targetPoint
        ? haversineDistanceMeters(userPoint, targetPoint)
        : null;
      return {
        originalIndex,
        resource: Object.assign({}, resource, {
          hasCoordinates: Boolean(targetPoint),
          distanceMeters,
          distanceText: formatDistanceMeters(distanceMeters),
        }),
      };
    })
    .sort((left, right) => {
      const leftHasDistance = Number.isFinite(left.resource.distanceMeters);
      const rightHasDistance = Number.isFinite(right.resource.distanceMeters);
      if (leftHasDistance && rightHasDistance) {
        const delta = left.resource.distanceMeters - right.resource.distanceMeters;
        if (Math.abs(delta) > 1e-9) return delta;
      } else if (leftHasDistance !== rightHasDistance) {
        return leftHasDistance ? -1 : 1;
      }
      // 距离相同或都不可计算时保持服务器原顺序，避免列表无故跳动。
      return left.originalIndex - right.originalIndex;
    })
    .map((entry) => entry.resource);
}

module.exports = {
  EARTH_RADIUS_M,
  formatDistanceMeters,
  haversineDistanceMeters,
  normalizePoint,
  sortResourcesByDistance,
};
