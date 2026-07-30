const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(
  path.join(__dirname, '..', 'static', 'js', 'cooling-map.js'),
  'utf8',
);

function makeElement() {
  return {
    children: [],
    classList: {
      add() {},
      toggle() {},
    },
    dataset: {},
    disabled: false,
    innerHTML: '',
    textContent: '',
    appendChild(child) {
      this.children.push(child);
    },
    addEventListener(type, handler) {
      this.listeners = this.listeners || {};
      this.listeners[type] = handler;
    },
    setAttribute() {},
    scrollIntoView() {},
  };
}

test('浏览器定位只参与本地距离计算，AMap 只接收公开资源点', () => {
  const publicPoints = [
    {
      id: 'resource-1',
      name: '公开资源一',
      lat: 29.2700,
      lng: 116.2000,
      coordinate_system: 'GCJ-02',
    },
    {
      id: 'resource-2',
      name: '公开资源二',
      lat: 29.3800,
      lng: 116.3200,
      coordinate_system: 'GCJ-02',
    },
  ];
  const mapPanel = makeElement();
  mapPanel.dataset.hasMapKey = '1';
  const mapContainer = makeElement();
  const mapData = makeElement();
  mapData.textContent = JSON.stringify(publicPoints);
  const locateButton = makeElement();
  const status = makeElement();
  const elements = {
    coolingMapPanel: mapPanel,
    coolingMap: mapContainer,
    coolingMapData: mapData,
    coolingLocateButton: locateButton,
    coolingMapStatus: status,
  };

  const markerOptions = [];
  const fitViewPositions = [];
  const zoomCenters = [];
  const directCenters = [];

  class FakeMarker {
    constructor(options) {
      this.options = options;
      markerOptions.push(options);
    }

    getPosition() {
      return this.options.position;
    }

    on() {}
  }

  class FakeMap {
    addControl() {}

    add() {}

    setFitView(markers) {
      fitViewPositions.push(markers.map((marker) => marker.getPosition()));
    }

    setZoomAndCenter(zoom, center) {
      zoomCenters.push({ zoom, center });
    }

    setCenter(center) {
      directCenters.push(center);
    }
  }

  class FakeInfoWindow {
    setContent() {}

    open() {}
  }

  const windowListeners = {};
  const fakeWindow = {
    AMap: {
      InfoWindow: FakeInfoWindow,
      Map: FakeMap,
      Marker: FakeMarker,
      Pixel: class FakePixel {},
      Scale: class FakeScale {},
      ToolBar: class FakeToolBar {},
    },
    addEventListener(type, handler) {
      windowListeners[type] = handler;
    },
  };
  const preciseBrowserLocation = {
    longitude: 116.201234567,
    latitude: 29.271234567,
  };
  const fakeNavigator = {
    geolocation: {
      getCurrentPosition(success) {
        success({
          coords: preciseBrowserLocation,
        });
      },
    },
  };
  const fakeDocument = {
    createElement() {
      return makeElement();
    },
    getElementById(id) {
      return elements[id] || null;
    },
    querySelectorAll() {
      return [];
    },
  };

  vm.runInNewContext(script, {
    document: fakeDocument,
    navigator: fakeNavigator,
    window: fakeWindow,
  });

  assert.deepEqual(
    markerOptions.map((options) => Array.from(options.position)),
    publicPoints.map((point) => [point.lng, point.lat]),
  );
  assert.deepEqual(
    fitViewPositions.map((group) => (
      Array.from(group, (position) => Array.from(position))
    )),
    [[
      [publicPoints[0].lng, publicPoints[0].lat],
      [publicPoints[1].lng, publicPoints[1].lat],
    ]],
  );

  locateButton.listeners.click();

  // 定位后 Marker 数量不变，最近点居中仍只使用公开资源坐标。
  assert.equal(markerOptions.length, publicPoints.length);
  assert.equal(fitViewPositions.length, 1);
  assert.equal(directCenters.length, 0);
  assert.deepEqual(
    Array.from(zoomCenters.at(-1).center),
    [publicPoints[0].lng, publicPoints[0].lat],
  );
  assert.match(status.textContent, /不上传至本项目服务器或保存/);
  assert.equal(typeof windowListeners.pagehide, 'function');
});
