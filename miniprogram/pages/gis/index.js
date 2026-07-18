const { getCommunity } = require('../../utils/public-data');
const { backendJson } = require('../../utils/request');
const { freshnessView, normalizeCommunity } = require('../../utils/format');
const { allowsJsMotion, safeJsDuration } = require('../../utils/motion');
const {
  beginPublicPage,
  hidePublicPage,
  pageCanRender,
  schedulePublicRefresh,
  showPublicPage,
  unloadPublicPage,
} = require('../../utils/public-page-lifecycle');
const { createPageShare, createTimelineShare, showPublicShareMenu } = require('../../utils/share');
const {
  FALLBACK_LAYERS,
  LAYER_ORDER,
  hitTest,
  legendEntries,
  makeCanvasModel,
  resolveLayer,
} = require('../../utils/gis-transform');
const {
  buildGridDetails,
  buildGridPage,
  buildRepresentativeRows,
  gridCellCount,
} = require('./view-model');

const REPRESENTATIVE_LIMIT = 8;
const GRID_PAGE_SIZE = 20;

function defaultLayerOptions() {
  return LAYER_ORDER.map((key) => ({ key, label: FALLBACK_LAYERS[key].short_label || FALLBACK_LAYERS[key].label }));
}

function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '约 1.8 MB';
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function layerViewData(collection, layerKey) {
  const spec = resolveLayer(collection, layerKey);
  return {
    activeLayerLabel: spec.label,
    activeSource: spec.source || '',
    legend: legendEntries(spec),
    representativeRows: buildRepresentativeRows(collection, layerKey, REPRESENTATIVE_LIMIT),
    cellCount: gridCellCount(collection),
  };
}

function gisDatasetSignature(gisInfo) {
  const source = gisInfo && typeof gisInfo === 'object' ? gisInfo : {};
  return JSON.stringify([
    source.geojson_url || source.data_url || source.url || '',
    source.generated_at || '',
    source.checksum_sha256 || source.sha256 || '',
    source.version || '',
    source.schema_version || '',
    Number(source.size_bytes) || 0,
  ]);
}

function touchCoordinate(touch, keys) {
  const source = touch || {};
  for (let index = 0; index < keys.length; index += 1) {
    const value = Number(source[keys[index]]);
    if (Number.isFinite(value)) return value;
  }
  return null;
}

Page({
  data: {
    metaLoading: true,
    metaError: '',
    freshness: {},
    gisInfo: {},
    sizeText: '约 1.8 MB',
    mapState: 'idle',
    mapError: '',
    mapNotice: '',
    drawProgress: 0,
    cellCount: 0,
    layerOptions: defaultLayerOptions(),
    activeLayer: 'age65_share_pct',
    activeLayerLabel: FALLBACK_LAYERS.age65_share_pct.label,
    activeSource: FALLBACK_LAYERS.age65_share_pct.source,
    legend: [],
    viewMode: 'map',
    canvasAvailable: true,
    representativeRows: [],
    gridSearchInput: '',
    gridSearch: '',
    gridPageRows: [],
    gridPage: 0,
    gridPageCount: 0,
    gridPageTotal: 0,
    gridHasPrevious: false,
    gridHasNext: false,
    selected: null,
    announcement: '',
    sourceVersions: [],
    reduceMotion: !allowsJsMotion(),
  },

  onLoad() {
    this._renderToken = 0;
    this._mapLoadToken = 0;
    this._metadataShowToken = 0;
    beginPublicPage(this);
    showPublicShareMenu();
  },

  onShow() {
    const resumeMapLoad = Boolean(this._resumeMapLoad);
    const resumeMapRender = Boolean(this._resumeMapRender);
    this._resumeMapLoad = false;
    this._resumeMapRender = false;
    const showToken = ++this._metadataShowToken;
    showPublicPage(this);
    // 先重验元数据，避免页面恢复时继续下载旧版 GIS 文件。
    this.loadMetadata({ waitForRevalidation: resumeMapLoad || resumeMapRender }).finally(() => {
      if (!pageCanRender(this) || showToken !== this._metadataShowToken) return;
      if (resumeMapLoad) {
        this.setData({ mapState: 'idle', mapError: '', mapNotice: '已返回页面，正在重新加载网格数据。' });
        this.loadMap();
      } else if (resumeMapRender && this._collection) {
        this.renderLayer();
      }
    });
  },

  onHide() {
    hidePublicPage(this);
    this._metadataShowToken += 1;
    this._canvasTouchStart = null;
    if (this.data.mapState === 'loading') {
      this._mapLoadToken += 1;
      if (this._mapRequest && typeof this._mapRequest.abort === 'function') this._mapRequest.abort();
      this._mapRequest = null;
      this._resumeMapLoad = true;
    }
    if (this.data.mapState === 'drawing') {
      this._renderToken += 1;
      this._resumeMapRender = true;
    }
  },

  onUnload() {
    unloadPublicPage(this);
    this._metadataShowToken += 1;
    this._renderToken += 1;
    this._mapLoadToken += 1;
    if (this._mapRequest && typeof this._mapRequest.abort === 'function') {
      this._mapRequest.abort();
    }
    this._mapRequest = null;
    this._canvasTouchStart = null;
    this._collection = null;
    this._canvasModel = null;
    this._canvas = null;
    this._context = null;
  },

  async onPullDownRefresh() {
    await this.loadMetadata({ force: true });
    wx.stopPullDownRefresh();
  },

  async loadMetadata(options) {
    if (!this.data.gisInfo || !Object.keys(this.data.gisInfo).length) {
      this.setData({ metaLoading: true, metaError: '' });
    }
    try {
      const waitForRevalidation = Boolean(options && options.waitForRevalidation);
      const requestOptions = Object.assign({}, options);
      delete requestOptions.waitForRevalidation;
      if (!waitForRevalidation) {
        requestOptions.onRevalidated = (freshResult) => {
          if (pageCanRender(this)) this.renderMetadata(freshResult);
        };
      }
      const result = await getCommunity(requestOptions);
      if (pageCanRender(this)) this.renderMetadata(result);
      if (waitForRevalidation && result.revalidated) {
        // 恢复中断的地图前等待 stale-while-revalidate 完成，避免白下载旧 URL。
        const freshResult = await result.revalidated;
        if (pageCanRender(this)) this.renderMetadata(freshResult);
        return freshResult;
      }
      return result;
    } catch (error) {
      if (!pageCanRender(this)) return;
      this.setData({ metaLoading: false, metaError: 'GIS 元数据暂时无法获取。' });
      return null;
    }
  },

  renderMetadata(result) {
    const normalized = normalizeCommunity(result.data);
    const gisInfo = normalized.gis || {};
    const nextGeojsonUrl = gisInfo.geojson_url || gisInfo.data_url || gisInfo.url || '';
    const nextSignature = gisDatasetSignature(gisInfo);
    const hasActiveDataset = Boolean(
      this._mapRequest
      || this._collection
      || ['loading', 'drawing', 'ready'].includes(this.data.mapState)
    );
    const datasetChanged = Boolean(
      this._geojsonSignature
      && this._geojsonSignature !== nextSignature
      && hasActiveDataset
    );
    const nextData = {
      metaLoading: false,
      metaError: gisInfo.available === false ? 'GIS 公开数据当前未发布。' : '',
      gisInfo,
      sizeText: formatBytes(gisInfo.size_bytes),
      freshness: freshnessView(result.meta, { generatedAt: gisInfo.generated_at || '' }),
    };
    if (datasetChanged) {
      this._mapLoadToken += 1;
      this._renderToken += 1;
      if (this._mapRequest && typeof this._mapRequest.abort === 'function') this._mapRequest.abort();
      this._mapRequest = null;
      this._collection = null;
      this._canvasModel = null;
      this._canvas = null;
      this._context = null;
      this._resumeMapLoad = false;
      this._resumeMapRender = false;
      Object.assign(nextData, {
        mapState: 'idle',
        mapError: '',
        mapNotice: '网格数据版本已更新，请重新加载地图与列表。',
        drawProgress: 0,
        cellCount: 0,
        representativeRows: [],
        gridSearchInput: '',
        gridSearch: '',
        gridPageRows: [],
        gridPage: 0,
        gridPageCount: 0,
        gridPageTotal: 0,
        gridHasPrevious: false,
        gridHasNext: false,
        selected: null,
        announcement: '网格数据版本已更新，请重新加载。',
        sourceVersions: [],
      });
    }
    this._geojsonUrl = nextGeojsonUrl;
    this._geojsonSignature = nextSignature;
    this.setData(nextData);
    schedulePublicRefresh(this, result.meta, () => this.loadMetadata());
  },

  retryMetadata() {
    this.loadMetadata({ force: true });
  },

  async loadMap() {
    if (this._collection) {
      if (this._context && this._canvas) this.renderLayer();
      return;
    }
    if (!this._geojsonUrl) {
      this.setData({ mapState: 'error', mapError: '后端尚未提供 GeoJSON 下载地址。' });
      return;
    }
    this._collection = null;
    this._canvasModel = null;
    this._canvas = null;
    this._context = null;
    this.setData({
      mapState: 'loading',
      mapError: '',
      mapNotice: '',
      drawProgress: 0,
      cellCount: 0,
      representativeRows: [],
      gridSearchInput: '',
      gridSearch: '',
      gridPageRows: [],
      gridPage: 0,
      gridPageCount: 0,
      gridPageTotal: 0,
      gridHasPrevious: false,
      gridHasNext: false,
      selected: null,
      announcement: `开始下载 GIS 网格数据，文件大小${this.data.sizeText}。`,
      sourceVersions: [],
    });
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
      const presentation = layerViewData(collection, this.data.activeLayer);
      if (!presentation.cellCount) throw new Error('geojson_cells_missing');
      const metadataLayers = collection.metadata && collection.metadata.layers || {};
      const layerOptions = LAYER_ORDER.filter((key) => metadataLayers[key] || FALLBACK_LAYERS[key]).map((key) => ({
        key,
        label: (metadataLayers[key] && (metadataLayers[key].short_label || metadataLayers[key].label)) || FALLBACK_LAYERS[key].short_label,
      }));
      // 完整响应通过校验后再保留，取消或失败时不会展示半份数据。
      this._collection = collection;
      const gridPage = buildGridPage(collection, this.data.activeLayer, {
        page: 1,
        pageSize: GRID_PAGE_SIZE,
      });
      this.setData({
        mapState: 'drawing',
        mapError: '',
        mapNotice: '',
        viewMode: 'map',
        canvasAvailable: true,
        layerOptions,
        ...presentation,
        gridPageRows: gridPage.rows,
        gridPage: gridPage.page,
        gridPageCount: gridPage.pageCount,
        gridPageTotal: gridPage.total,
        gridHasPrevious: gridPage.hasPrevious,
        gridHasNext: gridPage.hasNext,
        announcement: `网格数据下载完成，共 ${presentation.cellCount} 个网格，正在绘制地图。`,
        sourceVersions: Array.isArray(collection.metadata && collection.metadata.source_versions)
          ? collection.metadata.source_versions
          : [],
      });
      wx.nextTick(() => this.initializeCanvas());
    } catch (error) {
      if (this._unloaded || loadToken !== this._mapLoadToken) return;
      this._collection = null;
      this._canvasModel = null;
      this._canvas = null;
      this._context = null;
      this.setData({
        mapState: 'error',
        mapError: '网格数据加载失败。请检查网络后重试，页面不会使用不完整地图。',
        representativeRows: [],
        gridPageRows: [],
        gridPage: 0,
        gridPageCount: 0,
        gridPageTotal: 0,
        gridHasPrevious: false,
        gridHasNext: false,
        selected: null,
        announcement: '网格数据加载失败，没有显示不完整数据。',
        sourceVersions: [],
      });
    } finally {
      if (loadToken === this._mapLoadToken) this._mapRequest = null;
    }
  },

  cancelMapLoad() {
    if (this.data.mapState !== 'loading') return;
    const request = this._mapRequest;
    this._mapLoadToken += 1;
    this._renderToken += 1;
    this._mapRequest = null;
    if (request && typeof request.abort === 'function') request.abort();
    this._collection = null;
    this._canvasModel = null;
    this._canvas = null;
    this._context = null;
    this.setData({
      mapState: 'idle',
      mapError: '',
      mapNotice: '下载已取消，没有显示任何网格数据。',
      drawProgress: 0,
      cellCount: 0,
      representativeRows: [],
      gridPageRows: [],
      gridPage: 0,
      gridPageCount: 0,
      gridPageTotal: 0,
      gridHasPrevious: false,
      gridHasNext: false,
      selected: null,
      announcement: 'GIS 网格下载已取消，没有显示任何网格数据。',
      sourceVersions: [],
    });
  },

  retryMap() {
    if (this._mapRequest && typeof this._mapRequest.abort === 'function') {
      this._mapRequest.abort();
    }
    this._mapLoadToken += 1;
    this._renderToken += 1;
    this._mapRequest = null;
    this._collection = null;
    this._canvasModel = null;
    this._canvas = null;
    this._context = null;
    this.loadMap();
  },

  initializeCanvas() {
    if (this._unloaded || this._publicPageVisible === false) return;
    wx.createSelectorQuery()
      .in(this)
      .select('#gisCanvas')
      .fields({ node: true, size: true, rect: true })
      .exec((result) => {
        if (!pageCanRender(this)) return;
        const target = result && result[0];
        if (!target || !target.node || !target.width || !target.height) {
          this.setData({
            mapState: 'ready',
            mapError: '地图画布初始化失败，已切换到可读网格列表。',
            viewMode: 'list',
            canvasAvailable: false,
            drawProgress: 100,
            announcement: '地图画布不可用，已切换到可读网格列表。',
          });
          return;
        }
        const canvas = target.node;
        let context;
        let ratio;
        try {
          const system = typeof wx.getWindowInfo === 'function' ? wx.getWindowInfo() : {};
          ratio = Math.min(Number(system.pixelRatio) || 1, 2);
          canvas.width = target.width * ratio;
          canvas.height = target.height * ratio;
          context = canvas.getContext('2d');
          if (!context) throw new Error('canvas_context_missing');
          context.scale(ratio, ratio);
        } catch (error) {
          this._canvas = null;
          this._context = null;
          this.setData({
            mapState: 'ready',
            mapError: '地图画布初始化失败，已切换到可读网格列表。',
            viewMode: 'list',
            canvasAvailable: false,
            drawProgress: 100,
            announcement: '地图画布不可用，已切换到可读网格列表。',
          });
          return;
        }
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
    if (!this._collection) return;
    let presentation;
    try {
      presentation = layerViewData(this._collection, key);
    } catch (error) {
      this.setData({
        mapError: '该图层暂时无法转换，请选择其他图层。',
        announcement: '该图层暂时无法转换。',
      });
      return;
    }
    this.setData({
      activeLayer: key,
      ...presentation,
      selected: null,
      mapError: this.data.canvasAvailable ? '' : this.data.mapError,
      announcement: `已选择${presentation.activeLayerLabel}图层。`,
    }, () => {
      if (!pageCanRender(this)) return;
      this.updateGridPage({ page: 1 });
      if (this._context && this._canvas && this.data.canvasAvailable) this.renderLayer();
    });
  },

  chooseMode(event) {
    const mode = event.currentTarget.dataset.mode;
    if (!['map', 'list'].includes(mode) || mode === this.data.viewMode) return;
    if (mode === 'map' && !this.data.canvasAvailable) {
      this.setData({ announcement: '地图画布当前不可用，请使用网格列表。' });
      return;
    }
    this.setData({
      viewMode: mode,
      announcement: mode === 'map' ? '已切换到地图模式。' : '已切换到可读网格列表模式。',
    });
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
      this.setData({
        mapState: 'ready',
        mapError: '地图转换失败，已切换到可读网格列表。',
        viewMode: 'list',
        canvasAvailable: false,
        drawProgress: 100,
        announcement: '地图转换失败，已切换到可读网格列表。',
      });
      return;
    }
    this._canvasModel = model;
    const context = this._context;
    context.clearRect(0, 0, model.width, model.height);
    context.fillStyle = '#f6f1eb';
    context.fillRect(0, 0, model.width, model.height);
    const cells = model.cells;
    // 减少动态效果时仍分批绘制，避免低端机一次计算 2593 个网格而卡顿。
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
        if (!this.data.reduceMotion && progress % 10 === 0) this.setData({ drawProgress: progress });
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
      this.setData({
        mapState: 'ready',
        drawProgress: 100,
        announcement: `地图绘制完成，共 ${cells.length} 个网格。`,
      });
    };
    drawChunk();
  },

  selectGrid(cell) {
    if (!cell || !this._collection) return;
    const selected = buildGridDetails(
      this._collection,
      cell,
      this.data.activeLayer,
      this._canvasModel && this._canvasModel.spec
    );
    this.setData({
      selected,
      announcement: `已选择网格 ${selected.id}，${selected.activeLabel} ${selected.activeValue}。详细信息已移到当前视野。`,
    }, () => {
      if (pageCanRender(this)) this.scrollToSelected();
    });
  },

  scrollToSelected() {
    if (!pageCanRender(this) || typeof wx.pageScrollTo !== 'function') return;
    wx.pageScrollTo({
      selector: '#selectedDetail',
      duration: safeJsDuration(240),
    });
  },

  onGridSearchInput(event) {
    this.setData({ gridSearchInput: String(event.detail.value || '') });
  },

  searchGrid() {
    const gridSearch = String(this.data.gridSearchInput || '').trim();
    this.setData({ gridSearch }, () => this.updateGridPage({ page: 1 }));
  },

  clearGridSearch() {
    this.setData({ gridSearchInput: '', gridSearch: '' }, () => this.updateGridPage({ page: 1 }));
  },

  previousGridPage() {
    if (!this.data.gridHasPrevious) return;
    this.updateGridPage({ page: this.data.gridPage - 1 });
  },

  nextGridPage() {
    if (!this.data.gridHasNext) return;
    this.updateGridPage({ page: this.data.gridPage + 1 });
  },

  updateGridPage(options) {
    if (!this._collection) return;
    const pageResult = buildGridPage(this._collection, this.data.activeLayer, {
      query: this.data.gridSearch,
      page: options && options.page || this.data.gridPage || 1,
      pageSize: GRID_PAGE_SIZE,
    });
    const queryLabel = this.data.gridSearch ? `，编号含“${this.data.gridSearch}”` : '';
    this.setData({
      gridPageRows: pageResult.rows,
      gridPage: pageResult.page,
      gridPageCount: pageResult.pageCount,
      gridPageTotal: pageResult.total,
      gridHasPrevious: pageResult.hasPrevious,
      gridHasNext: pageResult.hasNext,
      announcement: pageResult.total
        ? `网格列表${queryLabel}，第 ${pageResult.page} 页，共 ${pageResult.total} 个网格。`
        : `没有找到编号含“${this.data.gridSearch}”的网格。`,
    });
  },

  onCanvasTap(event) {
    const touch = event.changedTouches && event.changedTouches[0];
    const touchStart = this._canvasTouchStart;
    this._canvasTouchStart = null;
    if (!this._canvasModel || this.data.mapState !== 'ready') return;
    if (!touch) return;
    const endClientX = touchCoordinate(touch, ['clientX', 'pageX', 'x']);
    const endClientY = touchCoordinate(touch, ['clientY', 'pageY', 'y']);
    if (
      touchStart
      && endClientX !== null
      && endClientY !== null
      && Math.hypot(endClientX - touchStart.x, endClientY - touchStart.y) > 12
    ) return;
    const touchX = Number.isFinite(Number(touch.x)) ? Number(touch.x) : Number(touch.clientX) - this._canvasLeft;
    const touchY = Number.isFinite(Number(touch.y)) ? Number(touch.y) : Number(touch.clientY) - this._canvasTop;
    const cell = hitTest(this._canvasModel.cells, touchX, touchY);
    if (!cell) return;
    this.selectGrid(cell);
  },

  onCanvasTouchStart(event) {
    const touch = event.touches && event.touches[0];
    const x = touchCoordinate(touch, ['clientX', 'pageX', 'x']);
    const y = touchCoordinate(touch, ['clientY', 'pageY', 'y']);
    this._canvasTouchStart = x === null || y === null ? null : { x, y };
  },

  onShareAppMessage() {
    return createPageShare({ title: '都昌县 1 km 热暴露 GIS', route: '/pages/gis/index' });
  },

  onShareTimeline() {
    return createTimelineShare({ title: '都昌县 1 km 热暴露 GIS' });
  },
});
