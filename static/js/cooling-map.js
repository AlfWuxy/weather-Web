(function () {
    'use strict';

    const mapPanel = document.getElementById('coolingMapPanel');
    const mapContainer = document.getElementById('coolingMap');
    const mapDataElement = document.getElementById('coolingMapData');
    const locateButton = document.getElementById('coolingLocateButton');
    const statusElement = document.getElementById('coolingMapStatus');
    if (!mapPanel || !mapContainer || !mapDataElement || !statusElement) {
        return;
    }

    let rawPoints = [];
    try {
        rawPoints = JSON.parse(mapDataElement.textContent || '[]');
    } catch (_error) {
        rawPoints = [];
    }

    const points = rawPoints.filter(function (point) {
        return (
            point
            && point.coordinate_system === 'GCJ-02'
            && Number.isFinite(Number(point.lat))
            && Number.isFinite(Number(point.lng))
        );
    }).map(function (point) {
        return Object.assign({}, point, {
            id: String(point.id),
            lat: Number(point.lat),
            lng: Number(point.lng)
        });
    });

    let pageActive = true;
    let map = null;
    let infoWindow = null;
    let locating = false;
    const markerById = new Map();

    function setStatus(message, state) {
        statusElement.textContent = message;
        statusElement.classList.toggle('is-error', state === 'error');
        statusElement.classList.toggle('is-success', state === 'success');
    }

    function setLocating(nextValue) {
        locating = nextValue;
        if (!locateButton) {
            return;
        }
        locateButton.disabled = nextValue || !map || points.length === 0;
        locateButton.setAttribute('aria-busy', nextValue ? 'true' : 'false');
        locateButton.innerHTML = nextValue
            ? '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>正在读取一次位置'
            : '<i class="bi bi-crosshair me-1" aria-hidden="true"></i>按当前位置找附近';
    }

    function textRow(value) {
        const row = document.createElement('span');
        row.textContent = value;
        return row;
    }

    function buildInfoContent(point) {
        const content = document.createElement('div');
        content.className = 'cooling-map-info';
        const title = document.createElement('strong');
        title.textContent = point.name || '避暑资源';
        content.appendChild(title);
        content.appendChild(textRow(
            [point.type, point.community].filter(Boolean).join(' · ') || '避暑资源'
        ));
        content.appendChild(textRow(point.address || '地址未标注'));
        content.appendChild(textRow(point.open_hours || '开放时间未核验，请出发前确认'));
        return content;
    }

    function openPoint(point) {
        const marker = markerById.get(String(point.id));
        if (!map || !marker) {
            return;
        }
        if (!infoWindow) {
            infoWindow = new window.AMap.InfoWindow({
                offset: new window.AMap.Pixel(0, -28)
            });
        }
        infoWindow.setContent(buildInfoContent(point));
        infoWindow.open(map, marker.getPosition());
        map.setZoomAndCenter(15, marker.getPosition());
    }

    function distanceMeters(first, second) {
        const earthRadius = 6371008.8;
        const toRadians = function (value) {
            return value * Math.PI / 180;
        };
        const latitudeDelta = toRadians(second.lat - first.lat);
        const longitudeDelta = toRadians(second.lng - first.lng);
        const firstLatitude = toRadians(first.lat);
        const secondLatitude = toRadians(second.lat);
        const haversine = (
            Math.sin(latitudeDelta / 2) ** 2
            + Math.cos(firstLatitude)
            * Math.cos(secondLatitude)
            * Math.sin(longitudeDelta / 2) ** 2
        );
        return earthRadius * 2 * Math.atan2(
            Math.sqrt(haversine),
            Math.sqrt(Math.max(0, 1 - haversine))
        );
    }

    function nearestPoint(origin) {
        return points.reduce(function (nearest, point) {
            const distance = distanceMeters(origin, point);
            if (!nearest || distance < nearest.distance) {
                return { point: point, distance: distance };
            }
            return nearest;
        }, null);
    }

    function formatDistance(distance) {
        if (distance < 1000) {
            return Math.max(1, Math.round(distance)) + ' 米';
        }
        return (distance / 1000).toFixed(distance < 10000 ? 1 : 0) + ' 公里';
    }

    function transformLatitude(longitude, latitude) {
        let result = (
            -100
            + 2 * longitude
            + 3 * latitude
            + .2 * latitude * latitude
            + .1 * longitude * latitude
            + .2 * Math.sqrt(Math.abs(longitude))
        );
        result += (
            (20 * Math.sin(6 * longitude * Math.PI)
                + 20 * Math.sin(2 * longitude * Math.PI))
            * 2 / 3
        );
        result += (
            (20 * Math.sin(latitude * Math.PI)
                + 40 * Math.sin(latitude / 3 * Math.PI))
            * 2 / 3
        );
        result += (
            (160 * Math.sin(latitude / 12 * Math.PI)
                + 320 * Math.sin(latitude * Math.PI / 30))
            * 2 / 3
        );
        return result;
    }

    function transformLongitude(longitude, latitude) {
        let result = (
            300
            + longitude
            + 2 * latitude
            + .1 * longitude * longitude
            + .1 * longitude * latitude
            + .1 * Math.sqrt(Math.abs(longitude))
        );
        result += (
            (20 * Math.sin(6 * longitude * Math.PI)
                + 20 * Math.sin(2 * longitude * Math.PI))
            * 2 / 3
        );
        result += (
            (20 * Math.sin(longitude * Math.PI)
                + 40 * Math.sin(longitude / 3 * Math.PI))
            * 2 / 3
        );
        result += (
            (150 * Math.sin(longitude / 12 * Math.PI)
                + 300 * Math.sin(longitude / 30 * Math.PI))
            * 2 / 3
        );
        return result;
    }

    function wgs84ToGcj02(longitude, latitude) {
        // 中国境外不需要偏移；都昌定位在页面内完成换算，不调用第三方转换接口。
        if (
            longitude < 72.004
            || longitude > 137.8347
            || latitude < .8293
            || latitude > 55.8271
        ) {
            return { lng: longitude, lat: latitude };
        }
        const semiMajorAxis = 6378245;
        const eccentricitySquared = .006693421622965943;
        const longitudeDelta = transformLongitude(
            longitude - 105,
            latitude - 35
        );
        const latitudeDelta = transformLatitude(
            longitude - 105,
            latitude - 35
        );
        const latitudeRadians = latitude / 180 * Math.PI;
        const sineLatitude = Math.sin(latitudeRadians);
        const magic = 1 - eccentricitySquared * sineLatitude * sineLatitude;
        const squareRootMagic = Math.sqrt(magic);
        const adjustedLatitude = (
            latitudeDelta * 180
            / (
                (semiMajorAxis * (1 - eccentricitySquared))
                / (magic * squareRootMagic)
                * Math.PI
            )
        );
        const adjustedLongitude = (
            longitudeDelta * 180
            / (
                semiMajorAxis
                / squareRootMagic
                * Math.cos(latitudeRadians)
                * Math.PI
            )
        );
        return {
            lng: longitude + adjustedLongitude,
            lat: latitude + adjustedLatitude
        };
    }

    function applyConvertedLocation(location) {
        if (!pageActive || !map) {
            return;
        }
        // 精确坐标只作为本函数的临时值，用于与公开资源点计算直线距离。
        const userPoint = {
            lng: Number(location.lng),
            lat: Number(location.lat)
        };
        if (!Number.isFinite(userPoint.lng) || !Number.isFinite(userPoint.lat)) {
            setLocating(false);
            setStatus('位置坐标转换失败，请稍后重试。', 'error');
            return;
        }

        const nearest = nearestPoint(userPoint);
        if (nearest) {
            // 地图 SDK 只接收公开资源点，用户精确坐标不会进入地图实例。
            openPoint(nearest.point);
            setStatus(
                '本页直线距离最近的是“'
                + (nearest.point.name || '避暑资源')
                + '”，约 '
                + formatDistance(nearest.distance)
                + '。精确位置不上传至本项目服务器或保存，也不会用于地图打点。'
                + '出发前请确认道路与开放情况。',
                'success'
            );
        }
        setLocating(false);
    }

    function convertBrowserLocation(position) {
        if (!pageActive || !map) {
            setLocating(false);
            return;
        }
        const longitude = Number(position.coords.longitude);
        const latitude = Number(position.coords.latitude);
        if (!Number.isFinite(longitude) || !Number.isFinite(latitude)) {
            setLocating(false);
            setStatus('浏览器返回的位置无效，请改用文字地址查看。', 'error');
            return;
        }

        // 浏览器位置通常是 WGS84，页面内转换后再与 GCJ-02 资源计算距离。
        applyConvertedLocation(wgs84ToGcj02(longitude, latitude));
    }

    function handleLocationError(error) {
        setLocating(false);
        if (!pageActive) {
            return;
        }
        const denied = error && error.code === 1;
        setStatus(
            denied
                ? '你没有授权本次定位。地图和文字清单仍可正常使用。'
                : '这次没有取得位置，请稍后重试或使用文字清单。',
            denied ? null : 'error'
        );
    }

    function requestOneTimeLocation() {
        if (locating || !map || points.length === 0) {
            return;
        }
        if (!navigator.geolocation) {
            setStatus('当前浏览器不支持定位，请使用文字地址查看。', 'error');
            return;
        }
        setLocating(true);
        setStatus('浏览器将申请一次位置权限，拒绝后仍可继续查看地图。');
        navigator.geolocation.getCurrentPosition(
            convertBrowserLocation,
            handleLocationError,
            {
                enableHighAccuracy: false,
                timeout: 10000,
                maximumAge: 0
            }
        );
    }

    function clearEphemeralLocation() {
        pageActive = false;
        locating = false;
    }

    function initializeMap() {
        if (!window.AMap || mapPanel.dataset.hasMapKey !== '1') {
            if (locateButton) {
                locateButton.disabled = true;
            }
            if (points.length > 0) {
                setStatus('地图服务暂时无法加载，下面的资源地址和开放信息仍可使用。', 'error');
            }
            return;
        }

        map = new window.AMap.Map(mapContainer, {
            zoom: 11,
            center: [116.20, 29.27],
            mapStyle: 'amap://styles/normal',
            viewMode: '2D'
        });
        map.addControl(new window.AMap.Scale());
        map.addControl(new window.AMap.ToolBar());
        mapContainer.classList.add('is-ready');

        points.forEach(function (point) {
            const marker = new window.AMap.Marker({
                position: [point.lng, point.lat],
                title: point.name || '避暑资源',
                anchor: 'bottom-center'
            });
            marker.on('click', function () {
                openPoint(point);
            });
            markerById.set(String(point.id), marker);
            map.add(marker);
        });
        if (points.length > 0) {
            map.setFitView(Array.from(markerById.values()), false, [50, 50, 50, 50], 15);
        }
        setLocating(false);
    }

    document.querySelectorAll('[data-cooling-map-focus]').forEach(function (button) {
        button.addEventListener('click', function () {
            const point = points.find(function (item) {
                return item.id === String(button.dataset.coolingMapFocus);
            });
            if (!point || !map) {
                setStatus('这个资源暂无可用地图点位，请以文字地址为准。', 'error');
                return;
            }
            openPoint(point);
            mapPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    });

    if (locateButton) {
        locateButton.addEventListener('click', requestOneTimeLocation);
    }
    window.addEventListener('pagehide', clearEphemeralLocation);
    window.addEventListener('beforeunload', clearEphemeralLocation);
    window.addEventListener('pageshow', function () {
        pageActive = true;
        setLocating(false);
    });

    initializeMap();
}());
