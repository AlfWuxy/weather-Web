const { getCommunity } = require('../../utils/public-data');
const { backendJson } = require('../../utils/request');
const { freshnessView, normalizeCommunity } = require('../../utils/format');
const {
  FALLBACK_LAYERS,
  LAYER_ORDER,
  formatLayerValue,
  hitTest,
  legendEntries,
  makeCanvasModel,
  resolveLayer,
} = require('../../utils/gis-transform');

function defaultLayerOptions() {
  return LAYER_ORDER.map((key) => ({ key, label: FALLBACK_LAYERS[key].short_label || FALLBACK_LAYERS[key].label }));
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '约 1.9 MB';
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

Page({
  data: {
    metaLoading: true,
    metaError: '',
    freshness: {},
    gisInfo: {},
    sizeText: '约 1.9 MB',
    mapState: 'idle',
    mapError: '',
    drawProgress: 0,
    cellCount: 0,
    layerOptions: defaultLayerOptions(),
    activeLayer: 'age65_share_pct',
    activeLayerLabel: FALLBACK_LAYERS.age65_share_pct.label,
    activeSource: FALLBACK_LAYERS.age65_share_pct.source,
    legend: [],
    selected: null,
    sourceVersions: [],
  },

  onLoad() {
    this._renderToken = 0;
    this._mapLoadToken = 0;
    this._unloaded = false;
    this.loadMetadata();
  },

  onUnload() {
    this._unloaded = true;
    this._renderToken += 1;
    this._mapLoadToken += 1;
    if (this._mapRequest && typeof this._mapRequest.abort === 'function') {
      this._mapRequest.abort();
    }
    this._mapRequest = null;
    this._collection = null;
    this._canvasModel = null;
  },

  async onPullDownRefresh() {
    await this.loadMetadata({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadMetadata(options) {
    this.setData({ metaLoading: true, metaError: '' });
    try {
      const result = await getCommunity(options);
      if (this._unloaded) return;
      const normalized = normalizeCommunity(result.data);
      const gisInfo = normalized.gis || {};
      this._geojsonUrl = gisInfo.geojson_url || gisInfo.data_url || gisInfo.url || '';
      const unavailable = gisInfo.available === false;
      this.setData({
        metaLoading: false,
        metaError: unavailable ? 'GIS 公开数据当前未发布。' : '',
        gisInfo,
        sizeText: formatBytes(gisInfo.size_bytes),
        freshness: freshnessView(result.meta, { generatedAt: gisInfo.generated_at || '' }),
      });
    } catch (error) {
      if (this._unloaded) return;
      this.setData({ metaLoading: false, metaError: 'GIS 元数据暂时无法获取。' });
    }
  },

  retryMetadata() {
    this.loadMetadata({ force: true });
  },

  async loadMap() {
    if (this._collection) {
      this.renderLayer();
      return;
    }
    if (!this._geojsonUrl) {
      this.setData({ mapState: 'error', mapError: '后端尚未提供 GeoJSON 下载地址。' });
      return;
    }
    this.setData({ mapState: 'loading', mapError: '', drawProgress: 0, selected: null });
    const loadToken = ++this._mapLoadToken;
    try {
      const mapRequest = backendJson(this._geojsonUrl);
      this._mapRequest = mapRequest;
      const response = await mapRequest;
      if (this._unloaded || loadToken !== this._mapLoadToken) return;
      const collection = response && response.type === 'FeatureCollection'
        ? response
        : (response && response.data && response.data.type === 'FeatureCollection' ? response.data : null);
      if (!collection || !Array.isArray(collection.features)) {
        throw new Error('invalid_geojson_collection');
      }
      // 大对象只保存在页面实例中，正式绘制时仅构建一次 Canvas 模型。
      this._collection = collection;
      const metadataLayers = collection.metadata && collection.metadata.layers || {};
      const layerOptions = LAYER_ORDER.filter((key) => metadataLayers[key] || FALLBACK_LAYERS[key]).map((key) => ({
        key,
        label: (metadataLayers[key] && (metadataLayers[key].short_label || metadataLayers[key].label)) || FALLBACK_LAYERS[key].short_label,
      }));
      this.setData({
        mapState: 'drawing',
        layerOptions,
        sourceVersions: Array.isArray(collection.metadata && collection.metadata.source_versions)
          ? collection.metadata.source_versions
          : [],
      });
      wx.nextTick(() => this.initializeCanvas());
    } catch (error) {
      if (this._unloaded || loadToken !== this._mapLoadToken) return;
      this.setData({
        mapState: 'error',
        mapError: '网格数据加载失败。请检查网络后重试，页面不会使用不完整地图。',
      });
    } finally {
      if (loadToken === this._mapLoadToken) this._mapRequest = null;
    }
  },

  retryMap() {
    if (this._mapRequest && typeof this._mapRequest.abort === 'function') {
      this._mapRequest.abort();
    }
    this._mapLoadToken += 1;
    this._collection = null;
    this.loadMap();
  },

  initializeCanvas() {
    wx.createSelectorQuery()
      .in(this)
      .select('#gisCanvas')
      .fields({ node: true, size: true, rect: true })
      .exec((result) => {
        if (this._unloaded) return;
        const target = result && result[0];
        if (!target || !target.node || !target.width || !target.height) {
          this.setData({ mapState: 'error', mapError: 'Canvas 初始化失败，请重新进入页面。' });
          return;
        }
        const canvas = target.node;
        const system = typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : wx.getSystemInfoSync();
        const ratio = Number(system.pixelRatio) || 1;
        canvas.width = target.width * ratio;
        canvas.height = target.height * ratio;
        const context = canvas.getContext('2d');
        context.scale(ratio, ratio);
        this._canvas = canvas;
        this._context = context;
        this._canvasWidth = target.width;
        this._canvasHeight = target.height;
        this._canvasLeft = Number(target.left) || 0;
        this._canvasTop = Number(target.top) || 0;
        this.renderLayer();
      });
  },

  chooseLayer(event) {
    const key = event.currentTarget.dataset.key;
    if (!LAYER_ORDER.includes(key) || key === this.data.activeLayer) return;
    this.setData({ activeLayer: key, selected: null });
    if (this._collection && this._context) this.renderLayer();
  },

  renderLayer() {
    if (this._unloaded || !this._collection || !this._context || !this._canvas) return;
    const token = ++this._renderToken;
    let model;
    try {
      model = makeCanvasModel(
        this._collection,
        this.data.activeLayer,
        this._canvasWidth,
        this._canvasHeight,
        10
      );
    } catch (error) {
      this.setData({ mapState: 'error', mapError: '地图转换失败，已停止绘制。' });
      return;
    }
    this._canvasModel = model;
    const context = this._context;
    context.clearRect(0, 0, model.width, model.height);
    context.fillStyle = '#f6f1eb';
    context.fillRect(0, 0, model.width, model.height);
    const cells = model.cells;
    const chunkSize = 260;
    let cursor = 0;
    this.setData({
      mapState: 'drawing',
      drawProgress: 0,
      activeLayerLabel: model.spec.label,
      activeSource: model.spec.source || '',
      legend: legendEntries(model.spec),
      cellCount: cells.length,
    });

    const drawChunk = () => {
      if (this._unloaded || token !== this._renderToken) return;
      const end = Math.min(cells.length, cursor + chunkSize);
      for (; cursor < end; cursor += 1) {
        const cell = cells[cursor];
        if (!cell.path.length) continue;
        context.beginPath();
        context.moveTo(cell.path[0].x, cell.path[0].y);
        for (let pointIndex = 1; pointIndex < cell.path.length; pointIndex += 1) {
          context.lineTo(cell.path[pointIndex].x, cell.path[pointIndex].y);
        }
        context.closePath();
        context.fillStyle = cell.color;
        context.fill();
      }
      const progress = cells.length ? Math.round(cursor / cells.length * 100) : 100;
      if (cursor < cells.length) {
        if (progress % 10 === 0) this.setData({ drawProgress: progress });
        this._canvas.requestAnimationFrame(drawChunk);
        return;
      }
      if (model.boundaryPath.length) {
        context.beginPath();
        context.moveTo(model.boundaryPath[0].x, model.boundaryPath[0].y);
        for (let index = 1; index < model.boundaryPath.length; index += 1) {
          context.lineTo(model.boundaryPath[index].x, model.boundaryPath[index].y);
        }
        context.closePath();
        context.strokeStyle = '#5b3525';
        context.lineWidth = 1.5;
        context.stroke();
      }
      this.setData({ mapState: 'ready', drawProgress: 100 });
    };
    drawChunk();
  },

  onCanvasTap(event) {
    if (!this._canvasModel || this.data.mapState !== 'ready') return;
    const touch = event.changedTouches && event.changedTouches[0];
    if (!touch) return;
    const touchX = Number.isFinite(Number(touch.x)) ? Number(touch.x) : Number(touch.clientX) - this._canvasLeft;
    const touchY = Number.isFinite(Number(touch.y)) ? Number(touch.y) : Number(touch.clientY) - this._canvasTop;
    const cell = hitTest(this._canvasModel.cells, touchX, touchY);
    if (!cell) return;
    const properties = cell.properties || {};
    this.setData({
      selected: {
        id: cell.id,
        activeLabel: this._canvasModel.spec.short_label || this._canvasModel.spec.label,
        activeValue: formatLayerValue(cell.value, this._canvasModel.spec),
        age: formatLayerValue(properties.age65_share_pct, resolveLayer(this._collection, 'age65_share_pct')),
        temperature: formatLayerValue(properties.q3_lst_c_mean, resolveLayer(this._collection, 'q3_lst_c_mean')),
        coverage: formatLayerValue(properties.q3_coverage_pct, resolveLayer(this._collection, 'q3_coverage_pct')),
        tree: formatLayerValue(properties.tree_cover_pct, resolveLayer(this._collection, 'tree_cover_pct')),
        built: formatLayerValue(properties.built_up_pct, resolveLayer(this._collection, 'built_up_pct')),
        water: formatLayerValue(properties.permanent_water_pct, resolveLayer(this._collection, 'permanent_water_pct')),
        elevation: formatLayerValue(properties.mean_elevation_m, resolveLayer(this._collection, 'mean_elevation_m')),
      },
    });
  },

  blockCanvasMove() {},

  onShareAppMessage() {
    return { title: '都昌县 1 km 热暴露 GIS', path: '/pages/gis/index' };
  },
});
