const { formatLayerValue, resolveLayer } = require('../../utils/gis-transform');

const DETAIL_LAYER_KEYS = [
  'age65_share_pct',
  'q3_lst_c_mean',
  'q3_coverage_pct',
  'tree_cover_pct',
  'built_up_pct',
  'permanent_water_pct',
  'mean_elevation_m',
];

function gridFeatures(collection) {
  if (!collection || !Array.isArray(collection.features)) return [];
  return collection.features.filter((feature) => (
    feature
    && feature.properties
    && feature.properties.feature_type === 'modis_cell'
  ));
}

function gridId(item) {
  const properties = item && item.properties || {};
  return String(properties.cell_id || item && item.id || '未编号');
}

function detailSpecs(collection) {
  return DETAIL_LAYER_KEYS.reduce((result, key) => {
    result[key] = resolveLayer(collection, key);
    return result;
  }, {});
}

function buildGridDetails(collection, item, layerKey, activeSpec, specs) {
  const properties = item && item.properties || {};
  const resolvedActiveSpec = activeSpec || resolveLayer(collection, layerKey);
  const resolvedSpecs = specs || detailSpecs(collection);
  const candidateValue = item && Number.isFinite(item.value) ? item.value : properties[layerKey];
  const details = {
    id: gridId(item),
    activeLabel: resolvedActiveSpec.short_label || resolvedActiveSpec.label,
    activeValue: formatLayerValue(candidateValue, resolvedActiveSpec),
    age: formatLayerValue(properties.age65_share_pct, resolvedSpecs.age65_share_pct),
    temperature: formatLayerValue(properties.q3_lst_c_mean, resolvedSpecs.q3_lst_c_mean),
    coverage: formatLayerValue(properties.q3_coverage_pct, resolvedSpecs.q3_coverage_pct),
    tree: formatLayerValue(properties.tree_cover_pct, resolvedSpecs.tree_cover_pct),
    built: formatLayerValue(properties.built_up_pct, resolvedSpecs.built_up_pct),
    water: formatLayerValue(properties.permanent_water_pct, resolvedSpecs.permanent_water_pct),
    elevation: formatLayerValue(properties.mean_elevation_m, resolvedSpecs.mean_elevation_m),
  };
  details.ariaLabel = [
    `网格 ${details.id}`,
    `${details.activeLabel} ${details.activeValue}`,
    `65 岁及以上人口 ${details.age}`,
    `地表温度 ${details.temperature}`,
    `观测覆盖 ${details.coverage}`,
    `树木覆盖 ${details.tree}`,
    `建成区 ${details.built}`,
    `永久水域 ${details.water}`,
    `表面高程 ${details.elevation}`,
  ].join('，');
  return details;
}

function buildRepresentativeRows(collection, layerKey, limit) {
  const safeLimit = Math.max(1, Math.min(20, Number(limit) || 8));
  const activeSpec = resolveLayer(collection, layerKey);
  const specs = detailSpecs(collection);
  return gridFeatures(collection)
    .map((feature) => ({ feature, value: feature.properties[layerKey] }))
    .filter((entry) => Number.isFinite(entry.value))
    .sort((left, right) => {
      if (right.value !== left.value) return right.value - left.value;
      const leftId = gridId(left.feature);
      const rightId = gridId(right.feature);
      return leftId < rightId ? -1 : (leftId > rightId ? 1 : 0);
    })
    .slice(0, safeLimit)
    .map((entry, index) => Object.assign(
      { rank: index + 1 },
      buildGridDetails(collection, entry.feature, layerKey, activeSpec, specs)
    ));
}

function buildGridPage(collection, layerKey, options) {
  const settings = options || {};
  const query = String(settings.query || '').trim().toLowerCase();
  const pageSize = Math.max(5, Math.min(50, Number(settings.pageSize) || 20));
  const activeSpec = resolveLayer(collection, layerKey);
  const specs = detailSpecs(collection);
  const matches = gridFeatures(collection)
    .filter((feature) => !query || gridId(feature).toLowerCase().includes(query))
    .sort((left, right) => {
      const leftId = gridId(left);
      const rightId = gridId(right);
      return leftId < rightId ? -1 : (leftId > rightId ? 1 : 0);
    });
  const total = matches.length;
  const pageCount = total ? Math.ceil(total / pageSize) : 0;
  const requestedPage = Math.max(1, Number(settings.page) || 1);
  const page = pageCount ? Math.min(pageCount, requestedPage) : 0;
  const offset = page > 0 ? (page - 1) * pageSize : 0;
  const rows = matches.slice(offset, offset + pageSize).map((feature, index) => Object.assign(
    { position: offset + index + 1 },
    buildGridDetails(collection, feature, layerKey, activeSpec, specs)
  ));
  return {
    page,
    pageCount,
    pageSize,
    total,
    rows,
    hasPrevious: page > 1,
    hasNext: page > 0 && page < pageCount,
  };
}

function gridCellCount(collection) {
  return gridFeatures(collection).length;
}

module.exports = {
  buildGridDetails,
  buildGridPage,
  buildRepresentativeRows,
  gridCellCount,
};
