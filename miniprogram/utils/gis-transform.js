const LAYER_ORDER = [
  'age65_share_pct',
  'q3_lst_c_mean',
  'q3_coverage_pct',
  'tree_cover_pct',
  'built_up_pct',
  'permanent_water_pct',
  'mean_elevation_m',
];

const FALLBACK_LAYERS = {
  age65_share_pct: { label: '65 岁及以上人口比例', short_label: '65+ 人口', unit: '%', digits: 1, palette: ['#fff1df', '#f7c997', '#ee9551', '#d85d19', '#9f3211'], breaks: [0, 8, 14, 22, 32, 100], source: 'ASPECT 2020' },
  q3_lst_c_mean: { label: '晴空地表温度均值', short_label: '地表温度', unit: '°C', digits: 1, palette: ['#fff4d9', '#f8cf7a', '#ec9748', '#d85d19', '#8f2717'], breaks: [20, 28, 32, 36, 40, 60], source: 'NASA MYD11A1.061' },
  q3_coverage_pct: { label: 'Q3 观测覆盖率', short_label: '观测覆盖', unit: '%', digits: 1, palette: ['#eef4f8', '#c9dfea', '#83bed4', '#438ead', '#205b7a'], breaks: [0, 20, 40, 60, 80, 100], source: '独立复核程序 v3' },
  tree_cover_pct: { label: '树木覆盖比例', short_label: '树木覆盖', unit: '%', digits: 1, palette: ['#f0f4df', '#d5e6b5', '#a5cb78', '#6fa347', '#3e6f2d'], breaks: [0, 10, 25, 45, 70, 100], source: 'ESA WorldCover 2020' },
  built_up_pct: { label: '建成区覆盖比例', short_label: '建成区', unit: '%', digits: 1, palette: ['#f4efeb', '#dfcec5', '#c5a394', '#9f7161', '#70463e'], breaks: [0, 5, 15, 30, 55, 100], source: 'ESA WorldCover 2020' },
  permanent_water_pct: { label: '近似永久水域比例', short_label: '永久水域', unit: '%', digits: 1, palette: ['#edf7f9', '#c4e5ed', '#83c8da', '#439eb8', '#246b8b'], breaks: [0, 5, 15, 35, 65, 100], source: 'ESA WorldCover 2020' },
  mean_elevation_m: { label: '平均表面高程', short_label: '表面高程', unit: 'm', digits: 0, palette: ['#f1eee2', '#d9cfac', '#b7a477', '#89764c', '#55472f'], breaks: [0, 20, 40, 80, 160, 500], source: 'Copernicus GLO-30' },
};

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function walkCoordinates(coordinates, callback) {
  if (!Array.isArray(coordinates)) return;
  if (coordinates.length >= 2 && isFiniteNumber(coordinates[0]) && isFiniteNumber(coordinates[1])) {
    callback(coordinates[0], coordinates[1]);
    return;
  }
  coordinates.forEach((item) => walkCoordinates(item, callback));
}

function collectionParts(collection) {
  if (!collection || collection.type !== 'FeatureCollection' || !Array.isArray(collection.features)) {
    throw new Error('invalid_geojson_collection');
  }
  const cells = collection.features.filter((feature) => feature && feature.properties && feature.properties.feature_type === 'modis_cell');
  const boundary = collection.features.find((feature) => feature && feature.properties && feature.properties.feature_type === 'study_boundary') || null;
  if (!cells.length) throw new Error('geojson_cells_missing');
  return { cells, boundary, metadata: collection.metadata || {} };
}

function boundsForFeatures(features) {
  const bounds = { minLon: Infinity, maxLon: -Infinity, minLat: Infinity, maxLat: -Infinity };
  (features || []).forEach((feature) => {
    walkCoordinates(feature && feature.geometry && feature.geometry.coordinates, (lon, lat) => {
      bounds.minLon = Math.min(bounds.minLon, lon);
      bounds.maxLon = Math.max(bounds.maxLon, lon);
      bounds.minLat = Math.min(bounds.minLat, lat);
      bounds.maxLat = Math.max(bounds.maxLat, lat);
    });
  });
  if (![bounds.minLon, bounds.maxLon, bounds.minLat, bounds.maxLat].every(Number.isFinite)) {
    throw new Error('geojson_bounds_missing');
  }
  if (bounds.maxLon === bounds.minLon) bounds.maxLon += 0.0001;
  if (bounds.maxLat === bounds.minLat) bounds.maxLat += 0.0001;
  return bounds;
}

function resolveLayer(collection, layerKey) {
  const metadataLayers = collection && collection.metadata && collection.metadata.layers || {};
  const candidate = metadataLayers[layerKey] || FALLBACK_LAYERS[layerKey];
  if (!candidate) throw new Error('gis_layer_unknown');
  const fallback = FALLBACK_LAYERS[layerKey] || {};
  const breaks = Array.isArray(candidate.breaks) && candidate.breaks.length >= 2 ? candidate.breaks : fallback.breaks;
  const palette = Array.isArray(candidate.palette) && candidate.palette.length ? candidate.palette : fallback.palette;
  return Object.assign({}, fallback, candidate, { breaks, palette });
}

function colorForValue(value, spec) {
  if (!isFiniteNumber(value)) return '#ddd8d3';
  const breaks = spec.breaks || [];
  const palette = spec.palette || [];
  for (let index = 1; index < breaks.length; index += 1) {
    if (value <= breaks[index]) return palette[Math.min(index - 1, palette.length - 1)];
  }
  return palette[palette.length - 1] || '#d85d19';
}

function project(lon, lat, bounds, width, height, padding) {
  const innerWidth = Math.max(1, width - padding * 2);
  const innerHeight = Math.max(1, height - padding * 2);
  const middleLatitude = (bounds.minLat + bounds.maxLat) / 2;
  const longitudeFactor = Math.max(0.1, Math.cos(middleLatitude * Math.PI / 180));
  const dataWidth = (bounds.maxLon - bounds.minLon) * longitudeFactor;
  const dataHeight = bounds.maxLat - bounds.minLat;
  const scale = Math.min(innerWidth / dataWidth, innerHeight / dataHeight);
  const renderedWidth = dataWidth * scale;
  const renderedHeight = dataHeight * scale;
  const offsetX = padding + (innerWidth - renderedWidth) / 2;
  const offsetY = padding + (innerHeight - renderedHeight) / 2;
  return {
    x: offsetX + (lon - bounds.minLon) * longitudeFactor * scale,
    y: offsetY + (bounds.maxLat - lat) * scale,
  };
}

function firstRing(feature) {
  const geometry = feature && feature.geometry;
  if (!geometry || !Array.isArray(geometry.coordinates)) return [];
  if (geometry.type === 'Polygon') return geometry.coordinates[0] || [];
  if (geometry.type === 'MultiPolygon') return geometry.coordinates[0] && geometry.coordinates[0][0] || [];
  return [];
}

function projectedPath(feature, bounds, width, height, padding) {
  return firstRing(feature)
    .filter((point) => Array.isArray(point) && isFiniteNumber(point[0]) && isFiniteNumber(point[1]))
    .map((point) => project(point[0], point[1], bounds, width, height, padding));
}

function makeCanvasModel(collection, layerKey, width, height, padding) {
  const canvasWidth = Number(width);
  const canvasHeight = Number(height);
  if (!(canvasWidth > 0) || !(canvasHeight > 0)) throw new Error('canvas_size_invalid');
  const safePadding = Number(padding) >= 0 ? Number(padding) : 10;
  const parts = collectionParts(collection);
  const bounds = boundsForFeatures(parts.cells);
  const spec = resolveLayer(collection, layerKey);
  const cells = parts.cells.slice(0, 6000).map((feature) => {
    const value = feature.properties && feature.properties[layerKey];
    const path = projectedPath(feature, bounds, canvasWidth, canvasHeight, safePadding);
    const xs = path.map((point) => point.x);
    const ys = path.map((point) => point.y);
    return {
      id: String(feature.properties && feature.properties.cell_id || feature.id || ''),
      value: isFiniteNumber(value) ? value : null,
      color: colorForValue(value, spec),
      path,
      minX: xs.length ? Math.min.apply(null, xs) : 0,
      maxX: xs.length ? Math.max.apply(null, xs) : 0,
      minY: ys.length ? Math.min.apply(null, ys) : 0,
      maxY: ys.length ? Math.max.apply(null, ys) : 0,
      properties: feature.properties || {},
    };
  });
  const boundaryPath = parts.boundary
    ? projectedPath(parts.boundary, bounds, canvasWidth, canvasHeight, safePadding)
    : [];
  return { width: canvasWidth, height: canvasHeight, bounds, cells, boundaryPath, layerKey, spec };
}

function legendEntries(spec) {
  const breaks = spec && spec.breaks || [];
  const palette = spec && spec.palette || [];
  const unit = spec && spec.unit || '';
  return palette.map((color, index) => {
    const start = breaks[index];
    const end = breaks[index + 1];
    return {
      color,
      label: end === undefined ? `>${start}${unit}` : `${start}至${end}${unit}`,
    };
  });
}

function hitTest(cells, x, y) {
  const list = cells || [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const cell = list[index];
    if (x >= cell.minX && x <= cell.maxX && y >= cell.minY && y <= cell.maxY) return cell;
  }
  return null;
}

function formatLayerValue(value, spec) {
  if (!isFiniteNumber(value)) return '无数据';
  const digits = Number.isInteger(spec && spec.digits) ? spec.digits : 1;
  const unit = spec && spec.unit || '';
  return `${value.toFixed(digits)}${unit}`;
}

module.exports = {
  FALLBACK_LAYERS,
  LAYER_ORDER,
  boundsForFeatures,
  collectionParts,
  colorForValue,
  formatLayerValue,
  hitTest,
  legendEntries,
  makeCanvasModel,
  resolveLayer,
};
