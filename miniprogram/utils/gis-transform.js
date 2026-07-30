const LAYER_ORDER = [
  'age65_share_pct',
  'age65_percentile',
  'age65_population_support',
  'q3_lst_c_mean',
  'q3_lst_delta_median_c',
  'q3_lst_percentile',
  'q3_coverage_pct',
  'tree_cover_pct',
  'built_up_pct',
  'permanent_water_pct',
  'mean_elevation_m',
];

const FALLBACK_LAYERS = {
  age65_share_pct: { label: '65 岁及以上人口比例', short_label: '65+ 人口', unit: '%', digits: 1, palette: ['#fff1df', '#f7c997', '#ee9551', '#d85d19', '#9f3211'], breaks: [0, 8, 14, 22, 32, 100], source: 'ASPECT 2020' },
  age65_percentile: { label: '65+ 人口比例全县相对分位', short_label: '65+ 相对分位', unit: '%', digits: 0, palette: ['#f1eef6', '#d7b5d8', '#df65b0', '#ce1256', '#7a0177'], breaks: [0, 20, 40, 60, 80, 100], source: '由 ASPECT 2020 有效网格计算' },
  age65_population_support: { label: '65+ 比例人口支持状态', short_label: '人口支持状态', unit: '', digits: 0, palette: ['#d9dfe2', '#237a57'], breaks: [0, 0, 1], value_labels: { 0: '无正人口支持', 1: '有正人口支持' }, source: 'ASPECT 2020 支持状态' },
  q3_lst_c_mean: { label: '晴空地表温度均值', short_label: '地表温度', unit: '°C', digits: 1, palette: ['#fff4d9', '#f8cf7a', '#ec9748', '#d85d19', '#8f2717'], breaks: [20, 28, 32, 36, 40, 60], source: 'NASA MYD11A1.061' },
  q3_lst_delta_median_c: { label: '地表温度相对全县中位数偏差', short_label: '地表温度偏差', unit: '°C', digits: 1, palette: ['#2c7bb6', '#abd9e9', '#fdae61', '#d7191c'], breaks: [-10, -5, 0, 5, 10], source: '由 MYD11A1.061 有效网格计算' },
  q3_lst_percentile: { label: '地表温度全县相对分位', short_label: '地表温度分位', unit: '%', digits: 0, palette: ['#ffffcc', '#fed976', '#fd8d3c', '#e31a1c', '#800026'], breaks: [0, 20, 40, 60, 80, 100], source: '由 MYD11A1.061 有效网格计算' },
  q3_coverage_pct: { label: 'Q3 观测覆盖率', short_label: '观测覆盖', unit: '%', digits: 1, palette: ['#eef4f8', '#c9dfea', '#83bed4', '#438ead', '#205b7a'], breaks: [0, 20, 40, 60, 80, 100], source: '独立复核程序 v3' },
  tree_cover_pct: { label: '树木覆盖比例', short_label: '树木覆盖', unit: '%', digits: 1, palette: ['#f0f4df', '#d5e6b5', '#a5cb78', '#6fa347', '#3e6f2d'], breaks: [0, 10, 25, 45, 70, 100], source: 'ESA WorldCover 2020' },
  built_up_pct: { label: '建成区覆盖比例', short_label: '建成区', unit: '%', digits: 1, palette: ['#f4efeb', '#dfcec5', '#c5a394', '#9f7161', '#70463e'], breaks: [0, 5, 15, 30, 55, 100], source: 'ESA WorldCover 2020' },
  permanent_water_pct: { label: '近似永久水域比例', short_label: '永久水域', unit: '%', digits: 1, palette: ['#edf7f9', '#c4e5ed', '#83c8da', '#439eb8', '#246b8b'], breaks: [0, 5, 15, 35, 65, 100], source: 'ESA WorldCover 2020' },
  mean_elevation_m: { label: '平均表面高程', short_label: '表面高程', unit: 'm', digits: 0, palette: ['#f1eee2', '#d9cfac', '#b7a477', '#89764c', '#55472f'], breaks: [0, 20, 40, 80, 160, 500], source: 'Copernicus GLO-30' },
};

function isFiniteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function upperBound(sortedValues, target) {
  let low = 0;
  let high = sortedValues.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (sortedValues[middle] <= target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function percentileRank(sortedValues, value) {
  if (!isFiniteNumber(value) || !sortedValues.length) return null;
  return Math.max(
    1,
    Math.min(100, Math.round(upperBound(sortedValues, value) / sortedValues.length * 100))
  );
}

function median(sortedValues) {
  if (!sortedValues.length) return null;
  const middle = Math.floor(sortedValues.length / 2);
  if (sortedValues.length % 2) return sortedValues[middle];
  return (sortedValues[middle - 1] + sortedValues[middle]) / 2;
}

function summary(values) {
  const sorted = values.filter(isFiniteNumber).sort((left, right) => left - right);
  return {
    sorted,
    min: sorted.length ? sorted[0] : null,
    median: median(sorted),
    max: sorted.length ? sorted[sorted.length - 1] : null,
  };
}

function enrichDerivedLayers(collection) {
  const parts = collectionParts(collection);
  const ageSummary = summary(parts.cells.map((feature) => feature.properties.age65_share_pct));
  const lstSummary = summary(parts.cells.map((feature) => feature.properties.q3_lst_c_mean));
  const derivedCells = parts.cells.map((feature) => {
    const properties = feature.properties || {};
    const ageValue = properties.age65_share_pct;
    const lstValue = properties.q3_lst_c_mean;
    let supportValue = null;
    if (properties.positive_population_support === true) supportValue = 1;
    if (properties.positive_population_support === false) supportValue = 0;
    return {
      ...feature,
      properties: {
        ...properties,
        age65_percentile: percentileRank(ageSummary.sorted, ageValue),
        age65_population_support: supportValue,
        q3_lst_delta_median_c: isFiniteNumber(lstValue) && isFiniteNumber(lstSummary.median)
          ? Number((lstValue - lstSummary.median).toFixed(4))
          : null,
        q3_lst_percentile: percentileRank(lstSummary.sorted, lstValue),
      },
    };
  });
  const derivedById = new Map(
    derivedCells.map((feature) => [String(feature.properties.cell_id || feature.id || ''), feature])
  );
  const features = collection.features.map((feature) => {
    if (!feature || !feature.properties || feature.properties.feature_type !== 'modis_cell') return feature;
    return derivedById.get(String(feature.properties.cell_id || feature.id || '')) || feature;
  });
  const deltaSummary = summary(
    derivedCells.map((feature) => feature.properties.q3_lst_delta_median_c)
  );
  const maxDelta = Math.max(
    Math.abs(deltaSummary.min || 0),
    Math.abs(deltaSummary.max || 0),
    1
  );
  const deltaBreaks = [
    -maxDelta,
    -maxDelta / 2,
    0,
    maxDelta / 2,
    maxDelta,
  ].map((value) => Number(value.toFixed(4)));
  const metadata = collection.metadata || {};
  const layers = metadata.layers || {};
  const definitions = {
    age65_percentile: {
      ...FALLBACK_LAYERS.age65_percentile,
      min: ageSummary.sorted.length ? 1 : null,
      median: 50,
      max: ageSummary.sorted.length ? 100 : null,
      valid_cells: ageSummary.sorted.length,
      missing_cells: derivedCells.length - ageSummary.sorted.length,
      definition: '把有正人口支持网格的模型化 65+ 人口比例放入全县有效网格分布，使用并列值上界计算相对百分位。',
      metric_key: 'gis_age65_share',
      details_anchor: 'gis-age65-share',
    },
    age65_population_support: {
      ...FALLBACK_LAYERS.age65_population_support,
      min: 0,
      median: 1,
      max: 1,
      valid_cells: derivedCells.filter((feature) => isFiniteNumber(feature.properties.age65_population_support)).length,
      missing_cells: derivedCells.filter((feature) => !isFiniteNumber(feature.properties.age65_population_support)).length,
      definition: '仅区分该网格是否具有正的模型化人口支持。无正人口支持不等于 65+ 人口比例为 0。',
      metric_key: 'gis_age65_share',
      details_anchor: 'gis-age65-share',
    },
    q3_lst_delta_median_c: {
      ...FALLBACK_LAYERS.q3_lst_delta_median_c,
      breaks: deltaBreaks,
      min: deltaSummary.min,
      median: deltaSummary.median,
      max: deltaSummary.max,
      valid_cells: deltaSummary.sorted.length,
      missing_cells: derivedCells.length - deltaSummary.sorted.length,
      definition: '每个网格的 2020 至 2024 年夏季晴空地表温度均值减去全县有效网格中位数。',
      metric_key: 'gis_lst_mean',
      details_anchor: 'gis-lst-mean',
    },
    q3_lst_percentile: {
      ...FALLBACK_LAYERS.q3_lst_percentile,
      min: lstSummary.sorted.length ? 1 : null,
      median: 50,
      max: lstSummary.sorted.length ? 100 : null,
      valid_cells: lstSummary.sorted.length,
      missing_cells: derivedCells.length - lstSummary.sorted.length,
      definition: '把卫星晴空地表温度均值放入全县有效网格分布，使用并列值上界计算相对百分位。',
      metric_key: 'gis_lst_mean',
      details_anchor: 'gis-lst-mean',
    },
  };
  return {
    ...collection,
    metadata: {
      ...metadata,
      layers: {
        ...layers,
        ...definitions,
      },
    },
    features,
  };
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
  const valueLabels = spec && spec.value_labels || {};
  const labeledValues = Object.keys(valueLabels)
    .map(Number)
    .filter(Number.isFinite)
    .sort((left, right) => left - right);
  if (labeledValues.length === palette.length) {
    return labeledValues.map((value, index) => ({
      color: palette[index],
      label: valueLabels[String(value)],
    }));
  }
  return palette.map((color, index) => {
    const start = breaks[index];
    const end = breaks[index + 1];
    return {
      color,
      label: end === undefined ? `>${start}${unit}` : `${start}至${end}${unit}`,
    };
  });
}

function pointOnSegment(point, start, end) {
  const epsilon = 1e-7;
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared <= epsilon * epsilon) {
    return Math.abs(point.x - start.x) <= epsilon && Math.abs(point.y - start.y) <= epsilon;
  }
  const cross = (point.x - start.x) * dy - (point.y - start.y) * dx;
  if (Math.abs(cross) > epsilon * Math.max(1, Math.abs(dx) + Math.abs(dy))) return false;
  const dot = (point.x - start.x) * dx + (point.y - start.y) * dy;
  if (dot < -epsilon) return false;
  return dot <= lengthSquared + epsilon;
}

function pointInPath(path, x, y) {
  if (!Array.isArray(path) || path.length < 3) return false;
  const point = { x, y };
  let inside = false;
  for (let index = 0, previous = path.length - 1; index < path.length; previous = index, index += 1) {
    const start = path[previous];
    const end = path[index];
    if (!start || !end || !isFiniteNumber(start.x) || !isFiniteNumber(start.y)
      || !isFiniteNumber(end.x) || !isFiniteNumber(end.y)) continue;
    // 边界点视为命中；共享边界最终归属绘制顺序靠后的网格。
    if (pointOnSegment(point, start, end)) return true;
    const crossesScanline = (start.y > y) !== (end.y > y);
    if (!crossesScanline) continue;
    const intersectionX = start.x + ((y - start.y) * (end.x - start.x)) / (end.y - start.y);
    if (x < intersectionX) inside = !inside;
  }
  return inside;
}

function hitTest(cells, x, y) {
  if (!isFiniteNumber(x) || !isFiniteNumber(y)) return null;
  const list = cells || [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const cell = list[index];
    if (x < cell.minX || x > cell.maxX || y < cell.minY || y > cell.maxY) continue;
    if (pointInPath(cell.path, x, y)) return cell;
  }
  return null;
}

function formatLayerValue(value, spec) {
  if (!isFiniteNumber(value)) return '无数据';
  const valueLabels = spec && spec.value_labels || {};
  if (Object.prototype.hasOwnProperty.call(valueLabels, String(value))) {
    return valueLabels[String(value)];
  }
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
  enrichDerivedLayers,
  formatLayerValue,
  hitTest,
  legendEntries,
  makeCanvasModel,
  pointInPath,
  project,
  resolveLayer,
};
