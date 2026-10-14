import * as THREE from '/static/vendor/three.module.js';
import { OrbitControls } from '/static/vendor/OrbitControls.js';

// Global application state
let currentData = null;
let previewData = null;
let activeIfcPath = null;
let activeScanPath = null;
let activeLayout = 'split'; // 'split' (Side-by-Side) or 'overlay' (Aligned Overlay)
let activeViewMode = 'status'; // 'status' or 'heatmap'
let selectedElementId = null;

// Landmark Point Selection State
let pickedBimPoints = [];     // [{ id, x, y, z, meshBim, meshOverlay }]
let pickedScanPoints = [];    // [{ id, x, y, z, rawX, rawY, rawZ, meshScan, meshOverlay }]
let pairConnectionLines = []; // [THREE.Line] for overlay view

// Scan Scaling State (APEC & Multiscale Scans)
let activeScanScale = { x: 1.0, y: 1.0, z: 1.0 };
let origScanCenter = new THREE.Vector3(0, 0, 0);
let bboxRatioUniform = 1.0;
let bboxRatioAxes = [1.0, 1.0, 1.0];

// Manual Coarse Alignment & 3D Gizmo State
let activeAlignMode = 'pairs'; // 'pairs' or 'manual'
let manualTransform = {
  tx: 0.0,
  ty: 0.0,
  tz: 0.0,
  rx: 0.0, // degrees in UI
  ry: 0.0,
  rz: 0.0,
};
let activeGizmoMode = 'translate'; // 'translate', 'rotate', or 'none'
let gizmoGroup = null;
let isDraggingGizmo = false;
let activeGizmoHandle = null;
let gizmoDragStart = { x: 0, y: 0 };
let gizmoInitialTransform = { ...manualTransform };

function getMarkerScaleFactor() {
  const extents = (previewData && previewData.bim_extents) ? previewData.bim_extents : [10, 10, 10];
  const maxDim = Math.max(...extents);
  return Math.min(18.0, Math.max(1.0, maxDim / 14.0));
}

// Robust JSON fetch wrapper with descriptive error reporting
async function safeFetchJson(url, options) {
  const res = await fetch(url, options);
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    if (!res.ok) {
      throw new Error(`Server returned HTTP ${res.status}: ${text.slice(0, 150)}`);
    }
    throw new Error(`Invalid JSON: ${text.slice(0, 150)}`);
  }
  if (!res.ok) {
    throw new Error(data && data.error ? data.error : `HTTP Error ${res.status}`);
  }
  if (data && data.error) {
    throw new Error(data.error);
  }
  return data;
}

// Color palettes
const COLOR_COMPLETE = 0x10b981;
const COLOR_PARTIAL = 0xf59e0b;
const COLOR_NOT_STARTED = 0xef4444;
const COLOR_RAW_SCAN = 0xfb923c;     // Vivid orange for raw unaligned scan
const COLOR_ALIGNED_SCAN = 0x38bdf8; // Bright cyan for registered scan
const COLOR_BIM_PIN = 0x38bdf8;      // Cyan pin on BIM
const COLOR_SCAN_PIN = 0xf59e0b;     // Orange pin on Scan

// DOM selector shortcut
const $ = (id) => document.getElementById(id);

// Three.js Viewport Definitions
const viewBim = {
  canvas: null,
  renderer: null,
  scene: null,
  camera: null,
  controls: null,
  center: new THREE.Vector3(4, 3, 1.5),
  group: null,
  markersGroup: null,
  meshes: [],
  pointerDownPos: { x: 0, y: 0 },
  pointerDownTime: 0,
};

const viewScan = {
  canvas: null,
  renderer: null,
  scene: null,
  camera: null,
  controls: null,
  center: new THREE.Vector3(4.7, 2.7, 1.4),
  group: null,
  scanMesh: null,
  markersGroup: null,
  pointerDownPos: { x: 0, y: 0 },
  pointerDownTime: 0,
};

const viewOverlay = {
  canvas: null,
  renderer: null,
  scene: null,
  camera: null,
  controls: null,
  center: new THREE.Vector3(4, 3, 1.5),
  bimGroup: null,
  scanMesh: null,
  modelPointsMesh: null,
  markersGroup: null,
  meshes: [],
  pointerDownPos: { x: 0, y: 0 },
  pointerDownTime: 0,
};

const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();

// Boot application
window.addEventListener('DOMContentLoaded', async () => {
  init3DViewports();
  bindUI();
  await loadInitialStatusAndPreview();
});

// Setup 3D Viewports (Left BIM, Right Scan, and Aligned Overlay)
function init3DViewports() {
  // 1. Left Viewport: BIM Design Model
  initSingleViewport(viewBim, 'cvBim', 'paneBim', 0x0a0e17);
  viewBim.group = new THREE.Group();
  viewBim.scene.add(viewBim.group);
  viewBim.markersGroup = new THREE.Group();
  viewBim.scene.add(viewBim.markersGroup);

  // 2. Right Viewport: Raw Point Cloud Scan
  initSingleViewport(viewScan, 'cvScan', 'paneScan', 0x0b101c);
  viewScan.group = new THREE.Group();
  viewScan.scene.add(viewScan.group);
  viewScan.markersGroup = new THREE.Group();
  viewScan.scene.add(viewScan.markersGroup);

  // 3. Superimposed Overlay Viewport
  initSingleViewport(viewOverlay, 'cvOverlay', 'overlayViewportContainer', 0x0a0e17);
  viewOverlay.bimGroup = new THREE.Group();
  viewOverlay.scene.add(viewOverlay.bimGroup);
  viewOverlay.markersGroup = new THREE.Group();
  viewOverlay.scene.add(viewOverlay.markersGroup);

  // Camera Synchronization Listeners
  setupCameraSync();

  // Pointer event listeners for picking & selection
  setupPointerEvents();

  window.addEventListener('resize', onResize);

  // Start unified animation loop
  animate();
}

function initSingleViewport(view, canvasId, containerId, bgColor) {
  const canvas = $(canvasId);
  const container = $(containerId);
  view.canvas = canvas;

  view.scene = new THREE.Scene();
  view.scene.background = new THREE.Color(bgColor);

  // Ground Grid
  const grid = new THREE.GridHelper(40, 40, 0x223045, 0x141d2c);
  grid.rotation.x = Math.PI / 2;
  grid.position.set(6, 4, -0.21);
  view.scene.add(grid);

  // Ambient & Directional Lights
  const ambient = new THREE.AmbientLight(0xffffff, 0.85);
  view.scene.add(ambient);

  const dir1 = new THREE.DirectionalLight(0xffffff, 0.95);
  dir1.position.set(25, -25, 30);
  view.scene.add(dir1);

  const dir2 = new THREE.DirectionalLight(0x90b0e0, 0.5);
  dir2.position.set(-25, 25, 20);
  view.scene.add(dir2);

  // Camera setup (Z-up coordinate frame)
  const w = container.clientWidth || 600;
  const h = container.clientHeight || 450;
  view.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 400);
  view.camera.up.set(0, 0, 1);
  view.camera.position.set(view.center.x + 15, view.center.y - 19, view.center.z + 13);

  view.renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  view.renderer.setSize(w, h);
  view.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  view.controls = new OrbitControls(view.camera, view.renderer.domElement);
  view.controls.target.copy(view.center);
  view.controls.enableDamping = true;
  view.controls.dampingFactor = 0.08;
  view.controls.update();
}

// Synchronize Orbit & Zoom between Left (BIM) and Right (Scan) viewports
function setupCameraSync() {
  let isSyncing = false;

  function syncCameras(sourceControls, sourceCam, targetControls, targetCam) {
    if (isSyncing) return;
    if (!$('chkSyncCameras') || !$('chkSyncCameras').checked) return;

    isSyncing = true;
    const offset = sourceCam.position.clone().sub(sourceControls.target);
    targetCam.position.copy(targetControls.target).add(offset);
    targetCam.quaternion.copy(sourceCam.quaternion);
    targetCam.zoom = sourceCam.zoom;
    targetControls.update();
    isSyncing = false;
  }

  viewBim.controls.addEventListener('change', () => {
    syncCameras(viewBim.controls, viewBim.camera, viewScan.controls, viewScan.camera);
  });

  viewScan.controls.addEventListener('change', () => {
    syncCameras(viewScan.controls, viewScan.camera, viewBim.controls, viewBim.camera);
  });
}

// Pointer event listeners for picking and inspection
function setupPointerEvents() {
  // 1. BIM Canvas (Left)
  viewBim.canvas.addEventListener('pointerdown', (e) => {
    viewBim.pointerDownPos = { x: e.clientX, y: e.clientY };
    viewBim.pointerDownTime = performance.now();
  });
  viewBim.canvas.addEventListener('pointerup', (e) => {
    const moved = Math.hypot(e.clientX - viewBim.pointerDownPos.x, e.clientY - viewBim.pointerDownPos.y);
    const elapsed = performance.now() - viewBim.pointerDownTime;
    if (moved <= 6 && elapsed <= 350) {
      handleBimCanvasClick(e);
    }
  });

  // 2. Scan Canvas (Right)
  viewScan.canvas.addEventListener('pointerdown', (e) => {
    viewScan.pointerDownPos = { x: e.clientX, y: e.clientY };
    viewScan.pointerDownTime = performance.now();
  });
  viewScan.canvas.addEventListener('pointerup', (e) => {
    const moved = Math.hypot(e.clientX - viewScan.pointerDownPos.x, e.clientY - viewScan.pointerDownPos.y);
    const elapsed = performance.now() - viewScan.pointerDownTime;
    if (moved <= 6 && elapsed <= 350) {
      handleScanCanvasClick(e);
    }
  });

  // 3. Overlay Canvas (with 3D Gizmo drag support)
  viewOverlay.canvas.addEventListener('pointerdown', (e) => {
    viewOverlay.pointerDownPos = { x: e.clientX, y: e.clientY };
    viewOverlay.pointerDownTime = performance.now();

    if (activeAlignMode === 'manual' && activeGizmoMode !== 'none' && gizmoGroup && gizmoGroup.visible) {
      const rect = viewOverlay.canvas.getBoundingClientRect();
      mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
      raycaster.setFromCamera(mouse, viewOverlay.camera);

      const visibleChildren = gizmoGroup.children.filter((c) => c.visible);
      const hits = raycaster.intersectObjects(visibleChildren, false);
      if (hits.length > 0) {
        isDraggingGizmo = true;
        activeGizmoHandle = hits[0].object.userData;
        gizmoDragStart = { x: e.clientX, y: e.clientY };
        gizmoInitialTransform = { ...manualTransform };
        viewOverlay.controls.enabled = false;
        viewOverlay.canvas.style.cursor = 'grabbing';
        e.preventDefault();
        return;
      }
    }
  });

  window.addEventListener('pointermove', (e) => {
    if (!isDraggingGizmo || !activeGizmoHandle) return;

    const dx = e.clientX - gizmoDragStart.x;
    const dy = e.clientY - gizmoDragStart.y;
    const tSens = 0.05 * getMarkerScaleFactor();
    const rSens = 0.4;

    const axis = activeGizmoHandle.axis;
    if (activeGizmoHandle.type === 'translate') {
      if (axis === 'tx') {
        manualTransform.tx = +(gizmoInitialTransform.tx + dx * tSens).toFixed(2);
      } else if (axis === 'ty') {
        manualTransform.ty = +(gizmoInitialTransform.ty - dy * tSens).toFixed(2);
      } else if (axis === 'tz') {
        manualTransform.tz = +(gizmoInitialTransform.tz - dy * tSens).toFixed(2);
      }
    } else if (activeGizmoHandle.type === 'rotate') {
      if (axis === 'rz') {
        let val = Math.round(gizmoInitialTransform.rz + dx * rSens);
        while (val > 180) val -= 360;
        while (val < -180) val += 360;
        manualTransform.rz = val;
      } else if (axis === 'ry') {
        let val = Math.round(gizmoInitialTransform.ry - dy * rSens);
        while (val > 180) val -= 360;
        while (val < -180) val += 360;
        manualTransform.ry = val;
      } else if (axis === 'rx') {
        let val = Math.round(gizmoInitialTransform.rx + dx * rSens);
        while (val > 180) val -= 360;
        while (val < -180) val += 360;
        manualTransform.rx = val;
      }
    }

    updateManualTransform(true);
  });

  window.addEventListener('pointerup', (e) => {
    if (isDraggingGizmo) {
      isDraggingGizmo = false;
      activeGizmoHandle = null;
      viewOverlay.controls.enabled = true;
      viewOverlay.canvas.style.cursor = 'default';
      return;
    }

    if (e.target === viewOverlay.canvas) {
      const moved = Math.hypot(e.clientX - viewOverlay.pointerDownPos.x, e.clientY - viewOverlay.pointerDownPos.y);
      const elapsed = performance.now() - viewOverlay.pointerDownTime;
      if (moved <= 6 && elapsed <= 350) {
        handleOverlayCanvasClick(e);
      }
    }
  });
}

// Main Animation Loop
function animate() {
  requestAnimationFrame(animate);

  if (activeLayout === 'split') {
    viewBim.controls.update();
    viewBim.renderer.render(viewBim.scene, viewBim.camera);

    viewScan.controls.update();
    viewScan.renderer.render(viewScan.scene, viewScan.camera);
  } else {
    viewOverlay.controls.update();
    viewOverlay.renderer.render(viewOverlay.scene, viewOverlay.camera);
  }
}

// Handle window & viewport resize
function onResize() {
  if (activeLayout === 'split') {
    resizeView(viewBim, 'paneBim');
    resizeView(viewScan, 'paneScan');
  } else {
    resizeView(viewOverlay, 'overlayViewportContainer');
  }
}

function resizeView(view, containerId) {
  const container = $(containerId);
  if (!container || !view.renderer || !view.camera) return;
  const w = container.clientWidth;
  const h = container.clientHeight;
  if (w === 0 || h === 0) return;

  view.camera.aspect = w / h;
  view.camera.updateProjectionMatrix();
  view.renderer.setSize(w, h);
}

// Layout Switcher: 'split' or 'overlay'
function setLayout(mode) {
  activeLayout = mode;
  $('btnLayoutSplit').classList.toggle('active', mode === 'split');
  $('btnLayoutOverlay').classList.toggle('active', mode === 'overlay');

  if (mode === 'split') {
    $('splitViewportContainer').classList.remove('hidden');
    $('overlayViewportContainer').classList.add('hidden');
  } else {
    $('splitViewportContainer').classList.add('hidden');
    $('overlayViewportContainer').classList.remove('hidden');
  }

  // Allow browser CSS flex recalculation before updating renderer dimensions
  setTimeout(() => onResize(), 30);
}

// Left Canvas: BIM Point Picking with corner vertex snapping
function handleBimCanvasClick(e) {
  const rect = viewBim.canvas.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, viewBim.camera);

  const hits = raycaster.intersectObjects(viewBim.meshes);
  if (hits.length === 0) return;

  const hit = hits[0];
  let pt = hit.point.clone();

  // Vertex/Corner snap detection
  const el = hit.object.userData.element;
  if (el) {
    let bestCorner = null;
    let minDist = 0.50 * getMarkerScaleFactor();

    if (el.mesh && el.mesh.vertices && el.mesh.vertices.length > 0) {
      for (const v of el.mesh.vertices) {
        const d = Math.hypot(pt.x - v[0], pt.y - v[1], pt.z - v[2]);
        if (d < minDist) {
          minDist = d;
          bestCorner = new THREE.Vector3(v[0], v[1], v[2]);
        }
      }
    } else if (el.bounds) {
      const [lo, hi] = el.bounds;
      const corners = [
        new THREE.Vector3(lo[0], lo[1], lo[2]),
        new THREE.Vector3(hi[0], lo[1], lo[2]),
        new THREE.Vector3(hi[0], hi[1], lo[2]),
        new THREE.Vector3(lo[0], hi[1], lo[2]),
        new THREE.Vector3(lo[0], lo[1], hi[2]),
        new THREE.Vector3(hi[0], lo[1], hi[2]),
        new THREE.Vector3(hi[0], hi[1], hi[2]),
        new THREE.Vector3(lo[0], hi[1], hi[2]),
      ];
      for (const c of corners) {
        const d = pt.distanceTo(c);
        if (d < minDist) {
          minDist = d;
          bestCorner = c;
        }
      }
    }
    if (bestCorner) pt = bestCorner.clone();
  }

  addBimPoint(pt.x, pt.y, pt.z);
}

// Right Canvas: Point Cloud Picking
function handleScanCanvasClick(e) {
  let pickedPt = null;

  if (viewScan.scanMesh) {
    const rect = viewScan.canvas.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(mouse, viewScan.camera);
    raycaster.params.Points.threshold = 0.35 * getMarkerScaleFactor();

    const hits = raycaster.intersectObject(viewScan.scanMesh);
    if (hits.length > 0) {
      pickedPt = hits[0].point.clone();
    } else {
      // Screen-space proximity fallback
      pickedPt = findClosestScanPointOnScreen(e.clientX, e.clientY, 36);
    }
  }

  if (pickedPt) {
    addScanPoint(pickedPt.x, pickedPt.y, pickedPt.z);
  } else {
    toast('No point cloud vertex near cursor — try clicking directly on the orange points.');
  }
}

// Overlay Canvas Click: Select & inspect BIM element
function handleOverlayCanvasClick(e) {
  const rect = viewOverlay.canvas.getBoundingClientRect();
  mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, viewOverlay.camera);

  const hits = raycaster.intersectObjects(viewOverlay.meshes);
  if (hits.length > 0) {
    const hit = hits[0];
    const idx = hit.object.userData.index;
    if (idx !== undefined) {
      selectElementByIndex(idx);
    }
  }
}

// Find closest scan point to cursor in 2D screen coordinates
function findClosestScanPointOnScreen(cx, cy, maxDistPx = 36) {
  if (!previewData || !previewData.scan_points) return null;
  const canvas = viewScan.canvas;
  const rect = canvas.getBoundingClientRect();
  const screenX = cx - rect.left;
  const screenY = cy - rect.top;

  let bestPt = null;
  let bestDistSq = maxDistPx * maxDistPx;
  const v = new THREE.Vector3();
  const sx = activeScanScale.x;
  const sy = activeScanScale.y;
  const sz = activeScanScale.z;

  for (const pt of previewData.scan_points) {
    v.set(pt[0] * sx, pt[1] * sy, pt[2] * sz);
    v.project(viewScan.camera);
    if (v.z > 1.0) continue; // Behind camera
    const px = ((v.x + 1) / 2) * rect.width;
    const py = ((-v.y + 1) / 2) * rect.height;
    const dSq = (px - screenX) ** 2 + (py - screenY) ** 2;
    if (dSq < bestDistSq) {
      bestDistSq = dSq;
      bestPt = new THREE.Vector3(pt[0] * sx, pt[1] * sy, pt[2] * sz);
    }
  }
  return bestPt;
}

// Add a picked BIM point (Left Pane & Overlay)
function addBimPoint(x, y, z) {
  const id = pickedBimPoints.length + 1;
  const pinObjBim = createMarkerPin(id, x, y, z, false);
  const pinObjOverlay = createMarkerPin(id, x, y, z, false);

  viewBim.markersGroup.add(pinObjBim);
  viewOverlay.markersGroup.add(pinObjOverlay);

  pickedBimPoints.push({
    id,
    x: +x.toFixed(4),
    y: +y.toFixed(4),
    z: +z.toFixed(4),
    meshBim: pinObjBim,
    meshOverlay: pinObjOverlay,
  });

  updateMarkerDisplay();
  toast(`Placed BIM Landmark #${id} on left pane at (${x.toFixed(2)}, ${y.toFixed(2)}, ${z.toFixed(2)})`);
}

// Add a picked Scan point (Right Pane & Overlay)
function addScanPoint(x, y, z) {
  const id = pickedScanPoints.length + 1;
  const pinObjScan = createMarkerPin(id, x, y, z, true);
  const pinObjOverlay = createMarkerPin(id, x, y, z, true);

  viewScan.markersGroup.add(pinObjScan);
  viewOverlay.markersGroup.add(pinObjOverlay);

  pickedScanPoints.push({
    id,
    x: +x.toFixed(4),
    y: +y.toFixed(4),
    z: +z.toFixed(4),
    rawX: +(x / (activeScanScale.x || 1.0)).toFixed(4),
    rawY: +(y / (activeScanScale.y || 1.0)).toFixed(4),
    rawZ: +(z / (activeScanScale.z || 1.0)).toFixed(4),
    meshScan: pinObjScan,
    meshOverlay: pinObjOverlay,
  });

  updateMarkerDisplay();
  toast(`Placed Scan Landmark #${id} on right pane at (${x.toFixed(2)}, ${y.toFixed(2)}, ${z.toFixed(2)})`);
}

// Create 3D pin marker with sphere and billboard text badge
function createMarkerPin(id, x, y, z, isScan) {
  const group = new THREE.Group();
  const colorHex = isScan ? COLOR_SCAN_PIN : COLOR_BIM_PIN;
  const K = getMarkerScaleFactor();

  // 1. Central sphere
  const sphereGeom = new THREE.SphereGeometry(0.10 * K, 16, 16);
  const sphereMat = new THREE.MeshStandardMaterial({
    color: colorHex,
    emissive: colorHex,
    emissiveIntensity: 0.65,
    roughness: 0.2,
  });
  const sphere = new THREE.Mesh(sphereGeom, sphereMat);
  sphere.position.set(x, y, z);
  group.add(sphere);

  // 2. White outer halo ring
  const ringGeom = new THREE.RingGeometry(0.13 * K, 0.16 * K, 24);
  const ringMat = new THREE.MeshBasicMaterial({
    color: 0xffffff,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.9,
  });
  const ring = new THREE.Mesh(ringGeom, ringMat);
  ring.rotation.x = Math.PI / 2;
  ring.position.set(x, y, z + 0.01 * K);
  group.add(ring);

  // 3. Billboard Sprite Label (#1, #2, ...)
  const sprite = makeMarkerSprite(`#${id}`, isScan);
  sprite.position.set(x, y, z + 0.32 * K);
  sprite.scale.set(0.55 * K, 0.55 * K, 1.0);
  group.add(sprite);

  return group;
}

// Billboard canvas sprite for pin numbers
function makeMarkerSprite(text, isScan) {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext('2d');

  // Pill / Circle
  ctx.beginPath();
  ctx.arc(64, 64, 52, 0, 2 * Math.PI);
  ctx.fillStyle = isScan ? '#f59e0b' : '#38bdf8';
  ctx.fill();
  ctx.lineWidth = 8;
  ctx.strokeStyle = '#ffffff';
  ctx.stroke();

  // Text
  ctx.font = 'bold 50px -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.fillStyle = '#0a0e17';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 64, 64);

  const texture = new THREE.CanvasTexture(canvas);
  texture.minFilter = THREE.LinearFilter;
  const mat = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false });
  const sprite = new THREE.Sprite(mat);
  sprite.scale.set(0.55, 0.55, 1.0);
  return sprite;
}

// Update 3D Connecting Lines and Table
function updateMarkerDisplay() {
  // Clear old connection lines in overlay
  pairConnectionLines.forEach((line) => {
    viewOverlay.markersGroup.remove(line);
    line.geometry.dispose();
    line.material.dispose();
  });
  pairConnectionLines = [];

  const pairCount = Math.min(pickedBimPoints.length, pickedScanPoints.length);

  // Draw dashed connection lines in overlay view
  for (let i = 0; i < pairCount; i++) {
    const b = pickedBimPoints[i];
    const s = pickedScanPoints[i];
    const pB = new THREE.Vector3(b.x, b.y, b.z);
    const pS = new THREE.Vector3(s.x, s.y, s.z);

    const geom = new THREE.BufferGeometry().setFromPoints([pB, pS]);
    const mat = new THREE.LineDashedMaterial({
      color: 0xffffff,
      dashSize: 0.15,
      gapSize: 0.1,
      transparent: true,
      opacity: 0.85,
    });
    const line = new THREE.Line(geom, mat);
    line.computeLineDistances();
    viewOverlay.markersGroup.add(line);
    pairConnectionLines.push(line);
  }

  // Update header and sidebar count badges
  $('badgeBimCount').textContent = pickedBimPoints.length;
  $('badgeScanCount').textContent = pickedScanPoints.length;
  $('tabPairCount').textContent = `${pairCount}/5`;
  $('pairTotalCount').textContent = pairCount;

  // Update Status Badges & Buttons
  const isReady = pairCount >= 5;
  const pill = $('pairPillStatus');
  const reqBadge = $('pairRequirementBadge');
  const btnHero = $('btnRunPairMatch');
  const btnHeader = $('btnEstimate');

  if (isReady) {
    pill.textContent = `${pairCount} pairs ready ✓`;
    pill.className = 'pill-pair-status ready';

    reqBadge.textContent = `${pairCount} pairs ready for alignment & ICP!`;
    reqBadge.className = 'pair-req-badge ready';

    btnHero.disabled = false;
    btnHero.innerHTML = `<span class="btn-icon">⚡</span> Align ${pairCount} Point Pairs ➔ ICP`;
    btnHeader.disabled = false;
    btnHeader.innerHTML = `<span class="btn-icon">▶</span> Align ${pairCount} Pairs ➔ ICP`;
  } else {
    pill.textContent = `${pairCount} / 5 min pairs`;
    pill.className = 'pill-pair-status warning';

    reqBadge.textContent = `Need at least 5 point pairs (currently ${pairCount}/5)`;
    reqBadge.className = 'pair-req-badge warning';

    btnHero.disabled = true;
    btnHero.innerHTML = `<span class="btn-icon">⚡</span> Align 5 Point Pairs ➔ ICP`;
    btnHeader.disabled = true;
    btnHeader.innerHTML = `<span class="btn-icon">▶</span> Align 5 Pairs ➔ ICP`;
  }

  renderPairsTable();
}

// Render the Point Pairs Table in the Sidebar
function renderPairsTable() {
  const container = $('pairsList');
  const maxRows = Math.max(pickedBimPoints.length, pickedScanPoints.length);

  if (maxRows === 0) {
    container.innerHTML = `
      <div class="empty-pairs-state">
        No points selected yet.<br>
        Click in the <b>Left Viewport</b> for BIM points and the <b>Right Viewport</b> for Point Cloud points, or click <b>Auto 5 Pairs</b> for an instant test.
      </div>
    `;
    return;
  }

  let html = '';
  for (let i = 0; i < maxRows; i++) {
    const b = pickedBimPoints[i];
    const s = pickedScanPoints[i];

    let distStr = '--';
    if (b && s) {
      const d = Math.hypot(b.x - s.x, b.y - s.y, b.z - s.z);
      distStr = `${d.toFixed(2)}m`;
    }

    const bimLabel = b ? `<span class="coord-tag bim" title="(${b.x}, ${b.y}, ${b.z})">(${b.x.toFixed(1)}, ${b.y.toFixed(1)}, ${b.z.toFixed(1)})</span>` : `<span class="coord-tag pending">Waiting for Left BIM #${i+1}...</span>`;
    const scanLabel = s ? `<span class="coord-tag scan" title="(${s.x}, ${s.y}, ${s.z})">(${s.x.toFixed(1)}, ${s.y.toFixed(1)}, ${s.z.toFixed(1)})</span>` : `<span class="coord-tag pending">Waiting for Right Scan #${i+1}...</span>`;

    html += `
      <div class="pair-row" data-idx="${i}">
        <span class="pair-idx col-num">#${i + 1}</span>
        <div class="col-bim">${bimLabel}</div>
        <div class="col-scan">${scanLabel}</div>
        <span class="pair-dist col-dist">${distStr}</span>
        <div class="col-act">
          <button class="btn-del-pair" data-del="${i}" title="Delete Pair #${i + 1}">✕</button>
        </div>
      </div>
    `;
  }
  container.innerHTML = html;

  container.querySelectorAll('.btn-del-pair').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = Number(btn.dataset.del);
      deletePairAtIndex(idx);
    });
  });
}

// Delete a specific pair
function deletePairAtIndex(idx) {
  if (pickedBimPoints[idx]) {
    viewBim.markersGroup.remove(pickedBimPoints[idx].meshBim);
    viewOverlay.markersGroup.remove(pickedBimPoints[idx].meshOverlay);
    pickedBimPoints.splice(idx, 1);
  }
  if (pickedScanPoints[idx]) {
    viewScan.markersGroup.remove(pickedScanPoints[idx].meshScan);
    viewOverlay.markersGroup.remove(pickedScanPoints[idx].meshOverlay);
    pickedScanPoints.splice(idx, 1);
  }

  rebuildMarkers();
  updateMarkerDisplay();
  toast(`Removed pair #${idx + 1}`);
}

// Rebuild 3D markers from current arrays
function rebuildMarkers() {
  clearGroupChildren(viewBim.markersGroup);
  clearGroupChildren(viewScan.markersGroup);
  clearGroupChildren(viewOverlay.markersGroup);

  pickedBimPoints.forEach((p, i) => {
    p.id = i + 1;
    p.meshBim = createMarkerPin(p.id, p.x, p.y, p.z, false);
    p.meshOverlay = createMarkerPin(p.id, p.x, p.y, p.z, false);
    viewBim.markersGroup.add(p.meshBim);
    viewOverlay.markersGroup.add(p.meshOverlay);
  });

  pickedScanPoints.forEach((p, i) => {
    p.id = i + 1;
    p.meshScan = createMarkerPin(p.id, p.x, p.y, p.z, true);
    p.meshOverlay = createMarkerPin(p.id, p.x, p.y, p.z, true);
    viewScan.markersGroup.add(p.meshScan);
    viewOverlay.markersGroup.add(p.meshOverlay);
  });
}

function clearGroupChildren(group) {
  while (group.children.length > 0) {
    const obj = group.children[0];
    group.remove(obj);
    if (obj.geometry) obj.geometry.dispose();
  }
}

// Clear all picked points
function clearAllPoints() {
  clearGroupChildren(viewBim.markersGroup);
  clearGroupChildren(viewScan.markersGroup);
  clearGroupChildren(viewOverlay.markersGroup);

  pickedBimPoints = [];
  pickedScanPoints = [];
  pairConnectionLines = [];
  updateMarkerDisplay();
  toast('All landmark points cleared');
}

// Apply 3D Scale to Point Cloud (Interactive & Instant)
function applyScanScale(sx, sy, sz, updateInputs = false) {
  sx = Math.max(0.0001, parseFloat(sx) || 1.0);
  sy = Math.max(0.0001, parseFloat(sy) || 1.0);
  sz = Math.max(0.0001, parseFloat(sz) || 1.0);

  activeScanScale.x = sx;
  activeScanScale.y = sy;
  activeScanScale.z = sz;

  if (updateInputs) {
    if ($('inputScaleX')) $('inputScaleX').value = (Math.round(sx * 1000) / 1000).toString();
    if ($('inputScaleY')) $('inputScaleY').value = (Math.round(sy * 1000) / 1000).toString();
    if ($('inputScaleZ')) $('inputScaleZ').value = (Math.round(sz * 1000) / 1000).toString();
  }

  // Update header scale badge
  const isUniform = Math.abs(sx - sy) < 1e-4 && Math.abs(sy - sz) < 1e-4;
  const badgeText = isUniform ? `${(Math.round(sx * 100) / 100)}×` : `${sx.toFixed(1)}, ${sy.toFixed(1)}, ${sz.toFixed(1)}×`;
  const badgeEl = $('badgeScanScale');
  if (badgeEl) {
    badgeEl.textContent = badgeText;
    badgeEl.title = `Active Scale: X=${sx.toFixed(3)}, Y=${sy.toFixed(3)}, Z=${sz.toFixed(3)}`;
  }

  // Update Three.js scan mesh scale instantly
  if (viewScan.scanMesh) {
    viewScan.scanMesh.scale.set(sx, sy, sz);
  }
  if (viewOverlay.scanMesh) {
    viewOverlay.scanMesh.scale.set(sx, sy, sz);
  }

  // Update camera target to scaled scan center
  viewScan.center.set(origScanCenter.x * sx, origScanCenter.y * sy, origScanCenter.z * sz);
  viewScan.controls.target.copy(viewScan.center);
  viewScan.controls.update();

  // If user already picked scan pins, keep them placed on the exact same scaled features
  if (pickedScanPoints.length > 0) {
    pickedScanPoints.forEach((p) => {
      if (p.rawX !== undefined) {
        p.x = +(p.rawX * sx).toFixed(4);
        p.y = +(p.rawY * sy).toFixed(4);
        p.z = +(p.rawZ * sz).toFixed(4);
        if (p.meshScan) p.meshScan.position.set(p.x, p.y, p.z);
        if (p.meshOverlay) p.meshOverlay.position.set(p.x, p.y, p.z);
      }
    });
    updateMarkerDisplay();
  }
}

// Load suggested 5+ pairs from scene preview
function loadAuto5Pairs() {
  if (!previewData || !previewData.suggested_pairs || previewData.suggested_pairs.length < 5) {
    return toast('Scene preview does not have suggested pairs yet.');
  }

  clearAllPoints();
  const pairs = previewData.suggested_pairs;
  const serverScale = previewData.scan_scale || [1.0, 1.0, 1.0];
  const relSx = activeScanScale.x / (serverScale[0] || 1.0);
  const relSy = activeScanScale.y / (serverScale[1] || 1.0);
  const relSz = activeScanScale.z / (serverScale[2] || 1.0);

  for (const pair of pairs) {
    addBimPoint(pair.bim[0], pair.bim[1], pair.bim[2]);
    addScanPoint(pair.scan[0] * relSx, pair.scan[1] * relSy, pair.scan[2] * relSz);
  }
  toast(`Loaded ${pairs.length} salient landmark pairs for 1-click matching!`);
}

// Load default files & initial Scene Preview
async function loadInitialStatusAndPreview() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    if (data.defaults) {
      activeIfcPath = data.defaults.ifc;
      activeScanPath = data.defaults.scan;
      $('labelIfc').textContent = data.defaults.ifc_rel || 'sample_house/full_house.ifc';
      $('labelScan').textContent = data.defaults.scan_rel || 'sample_house/unfinished_scan.ply';
    }
  } catch (err) {
    console.error('Failed to load status:', err);
  }
  await fetchAndRenderScenePreview();
}

// Fetch & render unaligned scene preview (BIM on left, Raw scan on right)
async function fetchAndRenderScenePreview() {
  const overlay = $('loadingOverlay');
  overlay.classList.remove('hidden');
  $('loadingStatusText').textContent = 'Loading BIM geometry and raw point cloud preview...';

  try {
    const perturb = $('chkPerturb').checked;
    const data = await safeFetchJson(`/api/scene_preview?ifc_path=${encodeURIComponent(activeIfcPath || '')}&scan_path=${encodeURIComponent(activeScanPath || '')}&demo_perturb=${perturb}`);
    previewData = data;

    // Cache Bounding Box Ratios & Scan Center
    bboxRatioUniform = data.bbox_ratio_uniform || 1.0;
    bboxRatioAxes = data.bbox_ratio_axes || [1.0, 1.0, 1.0];

    const hintEl = $('labelScaleRatioHint');
    if (hintEl) {
      hintEl.textContent = `BIM:Scan ratio: ${bboxRatioUniform}×`;
      hintEl.title = `Diagonal Ratio: ${bboxRatioUniform}×\nAxes: X=${bboxRatioAxes[0]}×, Y=${bboxRatioAxes[1]}×, Z=${bboxRatioAxes[2]}×`;
    }

    if (data.scan_center) {
      origScanCenter.set(data.scan_center[0], data.scan_center[1], data.scan_center[2]);
    }

    // Auto-tune scale for datasets with high bounding box disparity (e.g. APEC)
    if (activeIfcPath && activeIfcPath.includes('apec') && bboxRatioUniform > 5.0) {
      applyScanScale(bboxRatioUniform, bboxRatioUniform, bboxRatioUniform, true);
    } else {
      applyScanScale(1.0, 1.0, 1.0, true);
    }

    // Center Left Camera on BIM Model
    if (data.bim_center) {
      viewBim.center.set(data.bim_center[0], data.bim_center[1], data.bim_center[2]);
      viewBim.controls.target.copy(viewBim.center);
      viewBim.controls.update();

      viewOverlay.center.copy(viewBim.center);
      viewOverlay.controls.target.copy(viewOverlay.center);
      viewOverlay.controls.update();
    }

    // Center Right Camera on Raw Scan Point Cloud
    if (data.scan_center) {
      viewScan.center.set(origScanCenter.x * activeScanScale.x, origScanCenter.y * activeScanScale.y, origScanCenter.z * activeScanScale.z);
      viewScan.controls.target.copy(viewScan.center);
      viewScan.controls.update();
    }

    // Align camera angles
    setCameraPreset('iso');

    // Build 3D geometries for Left (BIM), Right (Scan), and Overlay
    buildPreviewScene(data);

    // Auto-load suggested 5+ pairs for instant 1-click matching
    loadAuto5Pairs();

    toast(`Loaded scene: ${data.elements.length} BIM solids & ${data.scan_points.length} scan points.`);
  } catch (err) {
    console.error('Preview error:', err);
    toast(`Failed to load preview: ${err.message}`);
  } finally {
    overlay.classList.add('hidden');
  }
}

// Build Preview 3D Scenes
function buildPreviewScene(data) {
  // Clear old meshes
  clearGroupChildren(viewBim.group);
  clearGroupChildren(viewScan.group);
  clearGroupChildren(viewOverlay.bimGroup);
  viewBim.meshes = [];
  viewOverlay.meshes = [];

  // A. Create BIM Meshes on Left Pane AND Overlay Pane
  data.elements.forEach((el, index) => {
    let geomBim, geomOverlay;
    let isCustomMesh = false;

    if (el.mesh && el.mesh.vertices && el.mesh.vertices.length > 0 && el.mesh.faces && el.mesh.faces.length > 0) {
      isCustomMesh = true;
      const vCount = el.mesh.vertices.length;
      const posArray = new Float32Array(vCount * 3);
      for (let i = 0; i < vCount; i++) {
        posArray[i * 3] = el.mesh.vertices[i][0];
        posArray[i * 3 + 1] = el.mesh.vertices[i][1];
        posArray[i * 3 + 2] = el.mesh.vertices[i][2];
      }

      const indexArray = [];
      for (let i = 0; i < el.mesh.faces.length; i++) {
        const f = el.mesh.faces[i];
        indexArray.push(f[0], f[1], f[2]);
      }

      geomBim = new THREE.BufferGeometry();
      geomBim.setAttribute('position', new THREE.BufferAttribute(posArray, 3));
      geomBim.setIndex(indexArray);
      geomBim.computeVertexNormals();

      geomOverlay = geomBim.clone();
    } else {
      const lo = el.bounds[0];
      const hi = el.bounds[1];
      const dx = Math.max(0.05, hi[0] - lo[0]);
      const dy = Math.max(0.05, hi[1] - lo[1]);
      const dz = Math.max(0.05, hi[2] - lo[2]);
      geomBim = new THREE.BoxGeometry(dx, dy, dz);
      geomOverlay = new THREE.BoxGeometry(dx, dy, dz);
    }

    // Left View Mesh
    const matBim = new THREE.MeshStandardMaterial({
      color: 0x3b82f6,
      transparent: true,
      opacity: 0.45,
      roughness: 0.35,
      metalness: 0.1,
      side: THREE.DoubleSide,
    });
    const meshBim = new THREE.Mesh(geomBim, matBim);
    if (!isCustomMesh) {
      const lo = el.bounds[0];
      const hi = el.bounds[1];
      meshBim.position.set((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2);
    } else {
      meshBim.position.set(0, 0, 0);
    }
    meshBim.userData = { element: el, index: index, originalColor: 0x3b82f6 };

    const edgesGeomBim = new THREE.EdgesGeometry(geomBim, 25);
    const lineMatBim = new THREE.LineBasicMaterial({ color: 0x60a5fa, transparent: true, opacity: 0.85 });
    const wireBim = new THREE.LineSegments(edgesGeomBim, lineMatBim);
    wireBim.name = 'wireframe';
    meshBim.add(wireBim);

    viewBim.group.add(meshBim);
    viewBim.meshes.push(meshBim);

    // Overlay View Mesh
    const matOverlay = matBim.clone();
    const meshOverlay = new THREE.Mesh(geomOverlay, matOverlay);
    if (!isCustomMesh) {
      const lo = el.bounds[0];
      const hi = el.bounds[1];
      meshOverlay.position.set((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2);
    } else {
      meshOverlay.position.set(0, 0, 0);
    }
    meshOverlay.userData = { element: el, index: index, originalColor: 0x3b82f6 };

    const edgesGeomOverlay = new THREE.EdgesGeometry(geomOverlay, 25);
    const wireOverlay = new THREE.LineSegments(edgesGeomOverlay, lineMatBim.clone());
    wireOverlay.name = 'wireframe';
    meshOverlay.add(wireOverlay);

    viewOverlay.bimGroup.add(meshOverlay);
    viewOverlay.meshes.push(meshOverlay);
  });

  // B. Raw Point Cloud on Right Pane (Vivid Orange)
  if (data.scan_points && data.scan_points.length > 0) {
    const scanGeom = new THREE.BufferGeometry();
    const positions = new Float32Array(data.scan_points.length * 3);
    for (let i = 0; i < data.scan_points.length; i++) {
      positions[i * 3] = data.scan_points[i][0];
      positions[i * 3 + 1] = data.scan_points[i][1];
      positions[i * 3 + 2] = data.scan_points[i][2];
    }
    scanGeom.setAttribute('position', new THREE.BufferAttribute(positions, 3));

    const pointSize = Math.max(0.04, 0.055 * Math.min(6.0, getMarkerScaleFactor() * 0.4));
    const scanMat = new THREE.PointsMaterial({
      color: COLOR_RAW_SCAN,
      size: pointSize,
      transparent: true,
      opacity: 0.9,
    });
    viewScan.scanMesh = new THREE.Points(scanGeom, scanMat);
    viewScan.scanMesh.scale.set(activeScanScale.x, activeScanScale.y, activeScanScale.z);
    viewScan.group.add(viewScan.scanMesh);

    // Also add raw scan points to overlay view for coarse alignment
    if (viewOverlay.scanMesh) {
      viewOverlay.scene.remove(viewOverlay.scanMesh);
      if (viewOverlay.scanMesh.geometry) viewOverlay.scanMesh.geometry.dispose();
    }
    viewOverlay.scanMesh = new THREE.Points(scanGeom.clone(), scanMat.clone());
    viewOverlay.scanMesh.scale.set(activeScanScale.x, activeScanScale.y, activeScanScale.z);
    viewOverlay.scene.add(viewOverlay.scanMesh);
  }

  // C. Initialize/Update 3D Gizmo in Overlay Viewport
  initGizmo();

  // Reset manual transform & adapt slider bounds
  resetManualTransformSliders();
}

// Bind UI event listeners
function bindUI() {
  // Layout toggles
  $('btnLayoutSplit').addEventListener('click', () => setLayout('split'));
  $('btnLayoutOverlay').addEventListener('click', () => setLayout('overlay'));

  // Preset selector
  $('datasetPreset').addEventListener('change', async (e) => {
    const val = e.target.value;
    if (val === 'apec') {
      activeIfcPath = 'apec/apec_site_model.ifc';
      activeScanPath = 'apec/pointcloud2.ply';
      $('labelIfc').textContent = 'apec/apec_site_model.ifc';
      $('labelScan').textContent = 'apec/pointcloud2.ply';
      toast('Switched to APEC Site Model (Wave-roofed Hall, Towers, 91 elements)');
    } else if (val === 'complex') {
      activeIfcPath = 'sample_complex/complex_facility.ifc';
      activeScanPath = 'sample_complex/unfinished_scan.ply';
      $('labelIfc').textContent = 'sample_complex/complex_facility.ifc';
      $('labelScan').textContent = 'sample_complex/unfinished_scan.ply';
      toast('Switched to Complex Facility dataset (L-shape, Pipes, Columns)');
    } else {
      activeIfcPath = 'sample_house/full_house.ifc';
      activeScanPath = 'sample_house/unfinished_scan.ply';
      $('labelIfc').textContent = 'sample_house/full_house.ifc';
      $('labelScan').textContent = 'sample_house/unfinished_scan.ply';
      toast('Switched to Sample House dataset');
    }
    await fetchAndRenderScenePreview();
  });

  // Demo perturb toggle
  $('chkPerturb').addEventListener('change', async () => {
    await fetchAndRenderScenePreview();
  });

  // Quick Action buttons
  $('btnAuto5Pairs').addEventListener('click', loadAuto5Pairs);
  $('btnHelperAutoPairs').addEventListener('click', loadAuto5Pairs);
  $('btnClearPoints').addEventListener('click', clearAllPoints);
  $('btnHelperClearPairs').addEventListener('click', clearAllPoints);

  // Main Alignment Matching Buttons (both sidebar & header)
  $('btnRunPairMatch').addEventListener('click', runPointPairMatching);
  $('btnEstimate').addEventListener('click', () => {
    if (activeAlignMode === 'manual') {
      runManualPoseIcp();
    } else {
      runPointPairMatching();
    }
  });

  // Sidebar navigation tabs
  $('tabMatchingBtn').addEventListener('click', () => switchSidebarTab('matching'));
  $('tabProgressBtn').addEventListener('click', () => switchSidebarTab('progress'));

  // Tolerance slider live adjustment
  $('toleranceRange').addEventListener('input', (e) => {
    const mm = e.target.value;
    $('toleranceLabel').textContent = `${mm} mm`;
    if (currentData) {
      recomputeToleranceInClient(Number(mm) / 1000);
    }
  });

  // View Mode switches
  document.querySelectorAll('.view-modes .btn-tool').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.view-modes .btn-tool').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      activeViewMode = btn.dataset.mode;
      update3DDisplayMode();
    });
  });

  // Layer toggles
  $('layerSolids').addEventListener('change', updateLayerVisibility);
  $('layerScan').addEventListener('change', updateLayerVisibility);
  $('layerMarkers').addEventListener('change', updateLayerVisibility);

  // Camera presets
  $('camIso').addEventListener('click', () => setCameraPreset('iso'));
  $('camTop').addEventListener('click', () => setCameraPreset('top'));
  $('camFront').addEventListener('click', () => setCameraPreset('front'));
  $('camSide').addEventListener('click', () => setCameraPreset('side'));
  $('camReset').addEventListener('click', () => setCameraPreset('reset'));

  // Element Search filter
  $('elemSearch').addEventListener('input', (e) => {
    filterElementRows(e.target.value.toLowerCase());
  });

  // Filter tabs
  document.querySelectorAll('.filter-tabs .tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.filter-tabs .tab-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      applyStatusFilter(btn.dataset.filter);
    });
  });

  // Export buttons
  $('btnSaveDisk').addEventListener('click', saveToDisk);
  $('btnExportMenu').addEventListener('click', (e) => {
    e.stopPropagation();
    $('exportMenu').classList.toggle('hidden');
  });
  window.addEventListener('click', () => $('exportMenu').classList.add('hidden'));

  // File picker badges
  $('btnSelectIfc').addEventListener('click', () => openFileDialog('ifc'));
  $('btnSelectScan').addEventListener('click', () => openFileDialog('scan'));

  // Scale Tuning Action buttons
  if ($('btnAutoScaleUniform')) {
    $('btnAutoScaleUniform').addEventListener('click', () => {
      applyScanScale(bboxRatioUniform, bboxRatioUniform, bboxRatioUniform, true);
      toast(`Applied uniform bounding box scale ratio: ${bboxRatioUniform}×`);
    });
  }
  if ($('btnAutoScaleAxes')) {
    $('btnAutoScaleAxes').addEventListener('click', () => {
      applyScanScale(bboxRatioAxes[0], bboxRatioAxes[1], bboxRatioAxes[2], true);
      toast(`Applied per-axis bounding box scale ratio: [${bboxRatioAxes.join(', ')}]`);
    });
  }
  if ($('btnResetScale')) {
    $('btnResetScale').addEventListener('click', () => {
      applyScanScale(1.0, 1.0, 1.0, true);
      toast('Reset scan scale to 1.0 (unscaled)');
    });
  }

  // Scale Manual Inputs (Live interactive update)
  const onScaleInputChanged = (axis) => {
    const isUniform = $('chkUniformScale') ? $('chkUniformScale').checked : true;
    if (isUniform) {
      const activeEl = axis === 'x' ? $('inputScaleX') : axis === 'y' ? $('inputScaleY') : $('inputScaleZ');
      const val = parseFloat(activeEl ? activeEl.value : 1.0) || 1.0;
      if ($('inputScaleX')) $('inputScaleX').value = val;
      if ($('inputScaleY')) $('inputScaleY').value = val;
      if ($('inputScaleZ')) $('inputScaleZ').value = val;
      applyScanScale(val, val, val, false);
    } else {
      const sx = parseFloat($('inputScaleX') ? $('inputScaleX').value : 1.0) || 1.0;
      const sy = parseFloat($('inputScaleY') ? $('inputScaleY').value : 1.0) || 1.0;
      const sz = parseFloat($('inputScaleZ') ? $('inputScaleZ').value : 1.0) || 1.0;
      applyScanScale(sx, sy, sz, false);
    }
  };

  if ($('inputScaleX')) $('inputScaleX').addEventListener('input', () => onScaleInputChanged('x'));
  if ($('inputScaleY')) $('inputScaleY').addEventListener('input', () => onScaleInputChanged('y'));
  if ($('inputScaleZ')) $('inputScaleZ').addEventListener('input', () => onScaleInputChanged('z'));

  // Alignment Mode Switches
  if ($('btnModePairs')) $('btnModePairs').addEventListener('click', () => setAlignMode('pairs'));
  if ($('btnModeManual')) $('btnModeManual').addEventListener('click', () => setAlignMode('manual'));

  // 3D Gizmo Mode Switches
  if ($('btnGizmoTranslate')) $('btnGizmoTranslate').addEventListener('click', () => setGizmoMode('translate'));
  if ($('btnGizmoRotate')) $('btnGizmoRotate').addEventListener('click', () => setGizmoMode('rotate'));
  if ($('btnGizmoNone')) $('btnGizmoNone').addEventListener('click', () => setGizmoMode('none'));

  // Quick Snap & Alignment Helpers
  if ($('btnManualCenterOnScan')) $('btnManualCenterOnScan').addEventListener('click', centerBimOnScan);
  if ($('btnManualAlignFloorZ')) $('btnManualAlignFloorZ').addEventListener('click', alignBimFloorZ);
  if ($('btnManualResetPose')) $('btnManualResetPose').addEventListener('click', resetManualPose);
  if ($('btnResetTranslation')) {
    $('btnResetTranslation').addEventListener('click', () => {
      manualTransform.tx = 0; manualTransform.ty = 0; manualTransform.tz = 0;
      updateManualTransform(true);
      toast('Reset translation (X=0, Y=0, Z=0)');
    });
  }
  if ($('btnResetRotation')) {
    $('btnResetRotation').addEventListener('click', () => {
      manualTransform.rx = 0; manualTransform.ry = 0; manualTransform.rz = 0;
      updateManualTransform(true);
      toast('Reset rotation (Yaw=0, Pitch=0, Roll=0)');
    });
  }

  // CTA Button for Manual ICP
  if ($('btnRunManualIcp')) $('btnRunManualIcp').addEventListener('click', runManualPoseIcp);

  // Sliders and Number Inputs for Translation
  ['Tx', 'Ty', 'Tz'].forEach((axisKey) => {
    const key = 't' + axisKey.charAt(1).toLowerCase();
    const slider = $(`sliderManual${axisKey}`);
    const input = $(`inputManual${axisKey}`);
    if (slider) {
      slider.addEventListener('input', (e) => {
        manualTransform[key] = parseFloat(e.target.value) || 0.0;
        if (input) input.value = manualTransform[key].toFixed(2);
        updateManualTransform(false);
      });
    }
    if (input) {
      input.addEventListener('change', (e) => {
        manualTransform[key] = parseFloat(e.target.value) || 0.0;
        if (slider) slider.value = manualTransform[key].toFixed(1);
        updateManualTransform(false);
      });
    }
  });

  // Sliders and Number Inputs for Rotation
  ['Rx', 'Ry', 'Rz'].forEach((axisKey) => {
    const key = 'r' + axisKey.charAt(1).toLowerCase();
    const slider = $(`sliderManual${axisKey}`);
    const input = $(`inputManual${axisKey}`);
    if (slider) {
      slider.addEventListener('input', (e) => {
        manualTransform[key] = parseFloat(e.target.value) || 0.0;
        if (input) input.value = Math.round(manualTransform[key]).toString();
        updateManualTransform(false);
      });
    }
    if (input) {
      input.addEventListener('change', (e) => {
        manualTransform[key] = parseFloat(e.target.value) || 0.0;
        if (slider) slider.value = Math.round(manualTransform[key]).toString();
        updateManualTransform(false);
      });
    }
  });

  // Nudge Buttons (step buttons)
  document.querySelectorAll('.btn-nudge').forEach((btn) => {
    btn.addEventListener('click', () => {
      const axis = btn.dataset.axis;
      const delta = parseFloat(btn.dataset.delta) || 0.0;
      if (axis && manualTransform[axis] !== undefined) {
        manualTransform[axis] += delta;
        if (axis.startsWith('r')) {
          while (manualTransform[axis] > 180) manualTransform[axis] -= 360;
          while (manualTransform[axis] < -180) manualTransform[axis] += 360;
        }
        updateManualTransform(true);
      }
    });
  });
}

// Alignment Mode Switcher ('pairs' vs 'manual')
function setAlignMode(mode) {
  activeAlignMode = mode;
  if ($('btnModePairs')) $('btnModePairs').classList.toggle('active', mode === 'pairs');
  if ($('btnModeManual')) $('btnModeManual').classList.toggle('active', mode === 'manual');

  if (mode === 'pairs') {
    if ($('sectionPointPairs')) $('sectionPointPairs').classList.remove('hidden');
    if ($('sectionManualTransform')) $('sectionManualTransform').classList.add('hidden');
    updateGizmoVisibility();
    updateMarkerDisplay();
    toast('Switched to 5+ Point Pairs Alignment Mode');
  } else {
    if ($('sectionPointPairs')) $('sectionPointPairs').classList.add('hidden');
    if ($('sectionManualTransform')) $('sectionManualTransform').classList.remove('hidden');
    setLayout('overlay'); // Immediately show superimposed BIM + scan in 3D
    updateGizmoVisibility();
    updateManualTransform(true);

    const btnHeader = $('btnEstimate');
    if (btnHeader) {
      btnHeader.disabled = false;
      btnHeader.innerHTML = `<span class="btn-icon">⚡</span> Run ICP ➔ Estimate`;
    }
    toast('Switched to Direct Move & Rotate BIM Mode (Overlay View)');
  }
}

// 3D Gizmo Mode Switcher ('translate', 'rotate', 'none')
function setGizmoMode(mode) {
  activeGizmoMode = mode;
  if ($('btnGizmoTranslate')) $('btnGizmoTranslate').classList.toggle('active', mode === 'translate');
  if ($('btnGizmoRotate')) $('btnGizmoRotate').classList.toggle('active', mode === 'rotate');
  if ($('btnGizmoNone')) $('btnGizmoNone').classList.toggle('active', mode === 'none');
  updateGizmoVisibility();
}

// Initialize Interactive 3D Transform Gizmo in Overlay Viewport
function initGizmo() {
  if (gizmoGroup) {
    viewOverlay.scene.remove(gizmoGroup);
    while (gizmoGroup.children.length > 0) {
      const c = gizmoGroup.children[0];
      gizmoGroup.remove(c);
      if (c.geometry) c.geometry.dispose();
      if (c.material) c.material.dispose();
    }
  }

  gizmoGroup = new THREE.Group();
  gizmoGroup.name = 'transformGizmo';

  const scale = getMarkerScaleFactor() * 3.5;
  const arrowLen = scale;
  const arrowRadius = scale * 0.035;
  const coneRadius = scale * 0.12;
  const coneLen = scale * 0.35;

  const matX = new THREE.MeshBasicMaterial({ color: 0xef4444, depthTest: false, transparent: true, opacity: 0.95 });
  const matY = new THREE.MeshBasicMaterial({ color: 0x22c55e, depthTest: false, transparent: true, opacity: 0.95 });
  const matZ = new THREE.MeshBasicMaterial({ color: 0x3b82f6, depthTest: false, transparent: true, opacity: 0.95 });

  // 1. Translation Handles (RGB Arrows)
  // X Arrow (along +X)
  const cylGeomX = new THREE.CylinderGeometry(arrowRadius, arrowRadius, arrowLen, 12);
  cylGeomX.rotateZ(-Math.PI / 2);
  cylGeomX.translate(arrowLen / 2, 0, 0);
  const meshCylX = new THREE.Mesh(cylGeomX, matX);
  meshCylX.renderOrder = 999;
  meshCylX.userData = { type: 'translate', axis: 'tx' };

  const coneGeomX = new THREE.ConeGeometry(coneRadius, coneLen, 16);
  coneGeomX.rotateZ(-Math.PI / 2);
  coneGeomX.translate(arrowLen + coneLen / 2, 0, 0);
  const meshConeX = new THREE.Mesh(coneGeomX, matX);
  meshConeX.renderOrder = 999;
  meshConeX.userData = { type: 'translate', axis: 'tx' };

  // Y Arrow (along +Y)
  const cylGeomY = new THREE.CylinderGeometry(arrowRadius, arrowRadius, arrowLen, 12);
  cylGeomY.translate(0, arrowLen / 2, 0);
  const meshCylY = new THREE.Mesh(cylGeomY, matY);
  meshCylY.renderOrder = 999;
  meshCylY.userData = { type: 'translate', axis: 'ty' };

  const coneGeomY = new THREE.ConeGeometry(coneRadius, coneLen, 16);
  coneGeomY.translate(0, arrowLen + coneLen / 2, 0);
  const meshConeY = new THREE.Mesh(coneGeomY, matY);
  meshConeY.renderOrder = 999;
  meshConeY.userData = { type: 'translate', axis: 'ty' };

  // Z Arrow (along +Z)
  const cylGeomZ = new THREE.CylinderGeometry(arrowRadius, arrowRadius, arrowLen, 12);
  cylGeomZ.rotateX(Math.PI / 2);
  cylGeomZ.translate(0, 0, arrowLen / 2);
  const meshCylZ = new THREE.Mesh(cylGeomZ, matZ);
  meshCylZ.renderOrder = 999;
  meshCylZ.userData = { type: 'translate', axis: 'tz' };

  const coneGeomZ = new THREE.ConeGeometry(coneRadius, coneLen, 16);
  coneGeomZ.rotateX(Math.PI / 2);
  coneGeomZ.translate(0, 0, arrowLen + coneLen / 2);
  const meshConeZ = new THREE.Mesh(coneGeomZ, matZ);
  meshConeZ.renderOrder = 999;
  meshConeZ.userData = { type: 'translate', axis: 'tz' };

  // 2. Rotation Handles (RGB Torus Rings)
  const ringRadius = scale * 0.9;
  const tubeRadius = scale * 0.035;

  // X Ring (YZ plane)
  const torusGeomX = new THREE.TorusGeometry(ringRadius, tubeRadius, 12, 48);
  torusGeomX.rotateY(Math.PI / 2);
  const meshTorusX = new THREE.Mesh(torusGeomX, matX);
  meshTorusX.renderOrder = 999;
  meshTorusX.userData = { type: 'rotate', axis: 'rx' };

  // Y Ring (XZ plane)
  const torusGeomY = new THREE.TorusGeometry(ringRadius, tubeRadius, 12, 48);
  torusGeomY.rotateX(Math.PI / 2);
  const meshTorusY = new THREE.Mesh(torusGeomY, matY);
  meshTorusY.renderOrder = 999;
  meshTorusY.userData = { type: 'rotate', axis: 'ry' };

  // Z Ring (XY plane)
  const torusGeomZ = new THREE.TorusGeometry(ringRadius, tubeRadius, 12, 48);
  const meshTorusZ = new THREE.Mesh(torusGeomZ, matZ);
  meshTorusZ.renderOrder = 999;
  meshTorusZ.userData = { type: 'rotate', axis: 'rz' };

  gizmoGroup.add(meshCylX, meshConeX, meshCylY, meshConeY, meshCylZ, meshConeZ);
  gizmoGroup.add(meshTorusX, meshTorusY, meshTorusZ);

  viewOverlay.scene.add(gizmoGroup);
  updateGizmoVisibility();
}

function updateGizmoVisibility() {
  if (!gizmoGroup) return;
  const shouldShow = activeAlignMode === 'manual' && activeGizmoMode !== 'none';
  gizmoGroup.visible = shouldShow;
  if (!shouldShow) return;

  gizmoGroup.children.forEach((child) => {
    if (activeGizmoMode === 'translate') {
      child.visible = child.userData.type === 'translate';
    } else if (activeGizmoMode === 'rotate') {
      child.visible = child.userData.type === 'rotate';
    } else {
      child.visible = false;
    }
  });
}

// Reset manual transform & adapt slider bounds to model extents
function resetManualTransformSliders() {
  manualTransform = { tx: 0.0, ty: 0.0, tz: 0.0, rx: 0.0, ry: 0.0, rz: 0.0 };

  const extents = (previewData && previewData.bim_extents) ? previewData.bim_extents : [15, 15, 10];
  const maxDim = Math.max(...extents);
  const tRange = Math.max(25, Math.ceil(maxDim * 1.5));
  const tStep = tRange > 50 ? 0.5 : 0.1;

  ['Tx', 'Ty', 'Tz'].forEach((axis) => {
    const slider = $(`sliderManual${axis}`);
    if (slider) {
      slider.min = -tRange;
      slider.max = tRange;
      slider.step = tStep;
      slider.value = '0.0';
    }
    const input = $(`inputManual${axis}`);
    if (input) input.value = '0.0';
  });

  ['Rx', 'Ry', 'Rz'].forEach((axis) => {
    const slider = $(`sliderManual${axis}`);
    if (slider) {
      slider.min = -180;
      slider.max = 180;
      slider.step = 1;
      slider.value = '0';
    }
    const input = $(`inputManual${axis}`);
    if (input) input.value = '0';
  });

  updateManualTransform(true);
}

// Apply Manual Transform to BIM Model in Overlay Viewport
function updateManualTransform(syncUI = true) {
  const rxRad = THREE.MathUtils.degToRad(manualTransform.rx);
  const ryRad = THREE.MathUtils.degToRad(manualTransform.ry);
  const rzRad = THREE.MathUtils.degToRad(manualTransform.rz);

  const euler = new THREE.Euler(rxRad, ryRad, rzRad, 'XYZ');
  const rotMat = new THREE.Matrix4().makeRotationFromEuler(euler);
  const m = rotMat.elements;
  const R_user = [
    [m[0], m[4], m[8]],
    [m[1], m[5], m[9]],
    [m[2], m[6], m[10]]
  ];

  const cb = viewOverlay.center;
  const R_cb = [
    R_user[0][0]*cb.x + R_user[0][1]*cb.y + R_user[0][2]*cb.z,
    R_user[1][0]*cb.x + R_user[1][1]*cb.y + R_user[1][2]*cb.z,
    R_user[2][0]*cb.x + R_user[2][1]*cb.y + R_user[2][2]*cb.z
  ];

  const T_user = [
    manualTransform.tx + cb.x - R_cb[0],
    manualTransform.ty + cb.y - R_cb[1],
    manualTransform.tz + cb.z - R_cb[2]
  ];

  if (viewOverlay.bimGroup) {
    viewOverlay.bimGroup.position.set(T_user[0], T_user[1], T_user[2]);
    viewOverlay.bimGroup.rotation.copy(euler);
  }

  if (gizmoGroup) {
    gizmoGroup.position.set(
      manualTransform.tx + cb.x,
      manualTransform.ty + cb.y,
      manualTransform.tz + cb.z
    );
  }

  if (syncUI) {
    if ($('inputManualTx')) $('inputManualTx').value = manualTransform.tx.toFixed(2);
    if ($('sliderManualTx')) $('sliderManualTx').value = manualTransform.tx.toFixed(1);
    if ($('inputManualTy')) $('inputManualTy').value = manualTransform.ty.toFixed(2);
    if ($('sliderManualTy')) $('sliderManualTy').value = manualTransform.ty.toFixed(1);
    if ($('inputManualTz')) $('inputManualTz').value = manualTransform.tz.toFixed(2);
    if ($('sliderManualTz')) $('sliderManualTz').value = manualTransform.tz.toFixed(1);

    if ($('inputManualRz')) $('inputManualRz').value = Math.round(manualTransform.rz).toString();
    if ($('sliderManualRz')) $('sliderManualRz').value = Math.round(manualTransform.rz).toString();
    if ($('inputManualRy')) $('inputManualRy').value = Math.round(manualTransform.ry).toString();
    if ($('sliderManualRy')) $('sliderManualRy').value = Math.round(manualTransform.ry).toString();
    if ($('inputManualRx')) $('inputManualRx').value = Math.round(manualTransform.rx).toString();
    if ($('sliderManualRx')) $('sliderManualRx').value = Math.round(manualTransform.rx).toString();
  }
}

// Quick Helper: Center BIM onto Scan Point Cloud
function centerBimOnScan() {
  if (!previewData) return;
  const targetScanX = origScanCenter.x * activeScanScale.x;
  const targetScanY = origScanCenter.y * activeScanScale.y;
  const targetScanZ = origScanCenter.z * activeScanScale.z;

  manualTransform.tx = +(targetScanX - viewOverlay.center.x).toFixed(2);
  manualTransform.ty = +(targetScanY - viewOverlay.center.y).toFixed(2);
  manualTransform.tz = +(targetScanZ - viewOverlay.center.z).toFixed(2);

  updateManualTransform(true);
  toast(`BIM centered directly onto Scan at (${targetScanX.toFixed(1)}, ${targetScanY.toFixed(1)}, ${targetScanZ.toFixed(1)})`);
}

// Quick Helper: Align Ground / Base Floor Level (Z)
function alignBimFloorZ() {
  if (!previewData) return;
  let bimMinZ = 0;
  if (previewData.elements && previewData.elements.length > 0) {
    bimMinZ = Math.min(...previewData.elements.map((e) => e.bounds[0][2]));
  }
  let scanMinZ = 0;
  if (previewData.scan_points && previewData.scan_points.length > 0) {
    scanMinZ = Math.min(...previewData.scan_points.map((p) => p[2])) * activeScanScale.z;
  }
  manualTransform.tz = +(scanMinZ - bimMinZ).toFixed(2);
  updateManualTransform(true);
  toast(`Aligned BIM base floor (Z=${bimMinZ.toFixed(2)}m) to Scan ground (Z=${scanMinZ.toFixed(2)}m)`);
}

// Quick Helper: Reset Pose
function resetManualPose() {
  manualTransform = { tx: 0.0, ty: 0.0, tz: 0.0, rx: 0.0, ry: 0.0, rz: 0.0 };
  updateManualTransform(true);
  toast('Reset BIM transform back to original coordinates');
}

// Run ICP Registration from User-Defined Coarse BIM Pose
async function runManualPoseIcp() {
  const overlay = $('loadingOverlay');
  overlay.classList.remove('hidden');
  $('loadingStatusText').textContent = 'Refining initial coarse pose with Trimmed ICP & measuring progress...';

  try {
    const rxRad = THREE.MathUtils.degToRad(manualTransform.rx);
    const ryRad = THREE.MathUtils.degToRad(manualTransform.ry);
    const rzRad = THREE.MathUtils.degToRad(manualTransform.rz);

    const euler = new THREE.Euler(rxRad, ryRad, rzRad, 'XYZ');
    const rotMat = new THREE.Matrix4().makeRotationFromEuler(euler);
    const m = rotMat.elements;
    const R_user = [
      [m[0], m[4], m[8]],
      [m[1], m[5], m[9]],
      [m[2], m[6], m[10]]
    ];

    const cb = viewOverlay.center;
    const R_cb = [
      R_user[0][0]*cb.x + R_user[0][1]*cb.y + R_user[0][2]*cb.z,
      R_user[1][0]*cb.x + R_user[1][1]*cb.y + R_user[1][2]*cb.z,
      R_user[2][0]*cb.x + R_user[2][1]*cb.y + R_user[2][2]*cb.z
    ];

    const T_user = [
      manualTransform.tx + cb.x - R_cb[0],
      manualTransform.ty + cb.y - R_cb[1],
      manualTransform.tz + cb.z - R_cb[2]
    ];

    // Compute Scan -> BIM Initial Pose
    const initial_R = [
      [R_user[0][0], R_user[1][0], R_user[2][0]],
      [R_user[0][1], R_user[1][1], R_user[2][1]],
      [R_user[0][2], R_user[1][2], R_user[2][2]]
    ];
    const initial_t = [
      -(initial_R[0][0]*T_user[0] + initial_R[0][1]*T_user[1] + initial_R[0][2]*T_user[2]),
      -(initial_R[1][0]*T_user[0] + initial_R[1][1]*T_user[1] + initial_R[1][2]*T_user[2]),
      -(initial_R[2][0]*T_user[0] + initial_R[2][1]*T_user[1] + initial_R[2][2]*T_user[2])
    ];

    const payload = {
      ifc_path: activeIfcPath,
      scan_path: activeScanPath,
      initial_R: initial_R,
      initial_t: initial_t,
      tolerance_m: Number($('toleranceRange').value) / 1000,
      spacing: Number($('spacingSelect').value),
      demo_perturb: $('chkPerturb').checked,
      run_icp: true,
      scan_scale: [activeScanScale.x, activeScanScale.y, activeScanScale.z],
    };

    const data = await safeFetchJson('/api/match_pose', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    currentData = data;
    renderEstimationResults(data);
    setLayout('overlay');
    switchSidebarTab('progress');

    toast(`ICP Coarse Pose Refinement complete! Overall progress: ${data.summary.overall_progress_pct}%`);
  } catch (err) {
    console.error('Manual Pose ICP error:', err);
    toast(`Failed to align with ICP: ${err.message}`);
  } finally {
    overlay.classList.add('hidden');
  }
}

// Switch between Matching Tab and Progress Tab
function switchSidebarTab(tabName) {
  $('tabMatchingBtn').classList.toggle('active', tabName === 'matching');
  $('tabProgressBtn').classList.toggle('active', tabName === 'progress');
  $('panelMatching').classList.toggle('active', tabName === 'matching');
  $('panelProgress').classList.toggle('active', tabName === 'progress');
}

// Camera presets
function setCameraPreset(mode) {
  const views = [viewBim, viewScan, viewOverlay];
  const extents = (previewData && previewData.bim_extents) ? previewData.bim_extents : [10, 10, 10];
  const maxDim = Math.max(...extents);
  const dist = Math.max(26, maxDim * 1.4);

  views.forEach((v) => {
    const c = v.center;
    if (mode === 'top') {
      v.camera.position.set(c.x, c.y, c.z + dist);
      v.camera.up.set(0, 1, 0);
    } else if (mode === 'front') {
      v.camera.position.set(c.x, c.y - dist, c.z + dist * 0.1);
      v.camera.up.set(0, 0, 1);
    } else if (mode === 'side') {
      v.camera.position.set(c.x - dist, c.y, c.z + dist * 0.1);
      v.camera.up.set(0, 0, 1);
    } else {
      // Iso / Reset
      v.camera.position.set(c.x + dist * 0.6, c.y - dist * 0.75, c.z + dist * 0.5);
      v.camera.up.set(0, 0, 1);
    }
    v.camera.near = Math.max(0.01, dist * 0.001);
    v.camera.far = Math.max(2500, dist * 10);
    v.camera.updateProjectionMatrix();
    v.controls.target.copy(c);
    v.controls.update();
  });
}

// Core Matching Routine: 5 Point Pairs -> Horn Rigid Fit -> ICP -> Progress Estimation
async function runPointPairMatching() {
  const pairCount = Math.min(pickedBimPoints.length, pickedScanPoints.length);
  if (pairCount < 5) {
    toast(`Need at least 5 point pairs before matching (currently have ${pairCount}).`);
    return;
  }

  const btnHero = $('btnRunPairMatch');
  const btnHeader = $('btnEstimate');
  const overlay = $('loadingOverlay');
  const perturb = $('chkPerturb').checked;
  const tol_mm = Number($('toleranceRange').value);

  btnHero.disabled = true;
  btnHeader.disabled = true;
  overlay.classList.remove('hidden');

  const steps = [
    `Computing Horn quaternion rigid fit on ${pairCount} point pairs...`,
    'Refining registration via point-to-point trimmed ICP...',
    'Measuring surface support & tolerance matching...',
  ];
  let stepIdx = 0;
  const stepTimer = setInterval(() => {
    stepIdx = (stepIdx + 1) % steps.length;
    $('loadingStatusText').textContent = steps[stepIdx];
  }, 1100);

  try {
    const bim_points = pickedBimPoints.slice(0, pairCount).map((p) => [p.x, p.y, p.z]);
    const scan_points = pickedScanPoints.slice(0, pairCount).map((p) => [p.x, p.y, p.z]);

    const payload = {
      ifc_path: activeIfcPath,
      scan_path: activeScanPath,
      bim_points: bim_points,
      scan_points: scan_points,
      scan_scale: [activeScanScale.x, activeScanScale.y, activeScanScale.z],
      tolerance_m: tol_mm / 1000,
      demo_perturb: perturb,
      run_icp: true,
      spacing: 0.15,
    };

    const data = await safeFetchJson('/api/match_pairs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    currentData = data;
    renderEstimationResults(data);

    // Show Horn RMS card
    $('hornRmsCard').classList.remove('hidden');
    $('hornRmsVal').textContent = `${data.pair_alignment.residual_rms_mm} mm`;

    // Switch to Aligned Overlay layout to show registered scan over BIM
    setLayout('overlay');

    // Switch to Progress tab to display results
    switchSidebarTab('progress');

    toast(`Aligned ${pairCount} pairs! Horn RMS: ${data.pair_alignment.residual_rms_mm} mm · Final ICP: ${data.summary.icp_final_rms_mm} mm`);
  } catch (err) {
    console.error('Matching error:', err);
    toast(`Matching failed: ${err.message}`, 5000);
  } finally {
    clearInterval(stepTimer);
    btnHero.disabled = false;
    btnHeader.disabled = false;
    overlay.classList.add('hidden');
  }
}

// Render Results to UI and 3D Viewport
function renderEstimationResults(data) {
  const summary = data.summary;
  const elements = data.elements;

  // 1. Update KPI Cards
  const pct = summary.overall_progress_pct;
  $('heroProgressVal').textContent = `${pct.toFixed(1)}%`;
  $('overallBadge').textContent = `${summary.complete_elements} of ${summary.total_elements} Complete`;

  // Circular gauge fill
  const circumference = 2 * Math.PI * 50;
  const offset = circumference - (pct / 100) * circumference;
  $('ringProgress').style.strokeDashoffset = offset;
  $('ringProgress').style.stroke = pct >= 80 ? 'var(--complete)' : pct >= 40 ? 'var(--partial)' : 'var(--not-started)';

  $('countComplete').textContent = `${summary.complete_elements} Complete`;
  $('countPartial').textContent = `${summary.partial_elements} Partial`;
  $('countNotStarted').textContent = `${summary.not_started_elements} Not Started`;

  if (data.ground_truth) {
    $('gtCompareVal').textContent = `${data.ground_truth.overall_built_volume_pct}% GT Volume`;
  } else {
    $('gtCompareVal').textContent = `Elem avg: ${summary.element_completion_pct}%`;
  }

  // Diagnostics
  $('diagPairs').textContent = `${summary.point_pair_count} pairs`;
  $('diagHornRms').textContent = `${summary.pair_fit_rms_mm} mm`;
  $('diagRms').textContent = `${summary.icp_final_rms_mm} mm`;
  $('diagInliers').textContent = `${summary.scan_inlier_pct}%`;
  $('diagIter').textContent = `${summary.icp_iterations} iters`;
  $('diagTime').textContent = `${data.timing_s.total_execution}s`;

  // 2. Render Elements List
  renderElementsList(elements);

  // 3. Update 3D Meshes & Aligned Point Cloud in Overlay Viewport
  buildAligned3DScene(data);
}

// Build 3D Objects in Overlay Scene (Aligned scan + progress status materials)
function buildAligned3DScene(data) {
  const elements = data.elements;

  // Update BIM Box Meshes in both Left View and Overlay View to status colors
  [viewBim.meshes, viewOverlay.meshes].forEach((meshList) => {
    elements.forEach((el, index) => {
      if (index < meshList.length) {
        const mesh = meshList[index];
        const col = getStatusColor(el.estimated_status);
        mesh.material.color.setHex(col);
        mesh.material.opacity = el.estimated_status === 'complete' ? 0.65 : el.estimated_status === 'partial' ? 0.55 : 0.22;
        mesh.userData.element = el;
        mesh.userData.originalColor = col;
      }
    });
  });

  const viz = data.viz_data;

  // Add/Replace aligned scan points in viewOverlay
  if (viewOverlay.scanMesh) {
    viewOverlay.scene.remove(viewOverlay.scanMesh);
    if (viewOverlay.scanMesh.geometry) viewOverlay.scanMesh.geometry.dispose();
  }

  if (viz.scan_points && viz.scan_points.length > 0) {
    const scanGeom = new THREE.BufferGeometry();
    const positions = new Float32Array(viz.scan_points.length * 3);
    for (let i = 0; i < viz.scan_points.length; i++) {
      positions[i * 3] = viz.scan_points[i][0];
      positions[i * 3 + 1] = viz.scan_points[i][1];
      positions[i * 3 + 2] = viz.scan_points[i][2];
    }
    scanGeom.setAttribute('position', new THREE.BufferAttribute(positions, 3));

    const scanMat = new THREE.PointsMaterial({
      color: COLOR_ALIGNED_SCAN,
      size: 0.05,
      transparent: true,
      opacity: 0.85,
    });
    viewOverlay.scanMesh = new THREE.Points(scanGeom, scanMat);
    viewOverlay.scene.add(viewOverlay.scanMesh);
  }

  // BIM Surface Sample Points (for Heatmap) in viewOverlay
  if (viewOverlay.modelPointsMesh) {
    viewOverlay.scene.remove(viewOverlay.modelPointsMesh);
    if (viewOverlay.modelPointsMesh.geometry) viewOverlay.modelPointsMesh.geometry.dispose();
  }

  if (viz.model_samples && viz.model_samples.length > 0) {
    const modelGeom = new THREE.BufferGeometry();
    const count = viz.model_samples.length;
    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const tol = Number($('toleranceRange').value) / 1000;

    for (let i = 0; i < count; i++) {
      const p = viz.model_samples[i];
      positions[i * 3] = p[0];
      positions[i * 3 + 1] = p[1];
      positions[i * 3 + 2] = p[2];

      const c = getHeatmapColor(p[3], tol);
      colors[i * 3] = c.r;
      colors[i * 3 + 1] = c.g;
      colors[i * 3 + 2] = c.b;
    }

    modelGeom.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    modelGeom.setAttribute('color', new THREE.BufferAttribute(colors, 3));

    const modelMat = new THREE.PointsMaterial({
      vertexColors: true,
      size: 0.065,
      transparent: true,
      opacity: 0.9,
    });
    viewOverlay.modelPointsMesh = new THREE.Points(modelGeom, modelMat);
    viewOverlay.scene.add(viewOverlay.modelPointsMesh);
  }

  update3DDisplayMode();
}

// Status Color Helper
function getStatusColor(status) {
  if (status === 'complete') return COLOR_COMPLETE;
  if (status === 'partial') return COLOR_PARTIAL;
  return COLOR_NOT_STARTED;
}

// Tolerance Heatmap Color Helper
function getHeatmapColor(distance, tol) {
  const ratio = Math.min(2.0, distance / Math.max(0.01, tol));
  const c = new THREE.Color();
  if (ratio <= 1.0) {
    c.setRGB(7 / 255 + (236 / 255) * ratio, 141 / 255 + (60 / 255) * ratio, 109 / 255 - (32 / 255) * ratio);
  } else {
    const t = ratio - 1.0;
    c.setRGB(243 / 255 - (37 / 255) * t, 201 / 255 - (138 / 255) * t, 77 / 255 - (18 / 255) * t);
  }
  return c;
}

// Update 3D Display Mode (Status / Heatmap)
function update3DDisplayMode() {
  const isStatus = activeViewMode === 'status';
  const isHeatmap = activeViewMode === 'heatmap';

  $('legendStatus').classList.toggle('hidden', !isStatus);
  $('legendHeatmap').classList.toggle('hidden', !isHeatmap);
  $('legendMaxTol').textContent = `≥ ${(Number($('toleranceRange').value) * 2).toFixed(0)} mm`;

  const solidsVisible = $('layerSolids').checked;
  const scanVisible = $('layerScan').checked;
  const markersVisible = $('layerMarkers').checked;

  // Toggle solids visibility
  [viewBim.meshes, viewOverlay.meshes].forEach((meshList) => {
    meshList.forEach((mesh) => {
      mesh.visible = solidsVisible;
      if (isHeatmap) {
        mesh.material.opacity = 0.12;
      } else if (mesh.userData.element && mesh.userData.element.estimated_status) {
        mesh.material.opacity = mesh.userData.element.estimated_status === 'complete' ? 0.65 : mesh.userData.element.estimated_status === 'partial' ? 0.55 : 0.22;
      }
    });
  });

  if (viewScan.scanMesh) viewScan.scanMesh.visible = scanVisible;
  if (viewOverlay.scanMesh) viewOverlay.scanMesh.visible = scanVisible;

  if (viewOverlay.modelPointsMesh) {
    viewOverlay.modelPointsMesh.visible = isHeatmap;
  }

  viewBim.markersGroup.visible = markersVisible;
  viewScan.markersGroup.visible = markersVisible;
  viewOverlay.markersGroup.visible = markersVisible;
}

function updateLayerVisibility() {
  update3DDisplayMode();
}

// Client-side tolerance live adjustment
function recomputeToleranceInClient(newTol_m) {
  if (!currentData) return;
  const elements = currentData.elements;
  const viz = currentData.viz_data;

  if (viewOverlay.modelPointsMesh && viz.model_samples) {
    const colors = viewOverlay.modelPointsMesh.geometry.attributes.color.array;
    for (let i = 0; i < viz.model_samples.length; i++) {
      const d = viz.model_samples[i][3];
      const c = getHeatmapColor(d, newTol_m);
      colors[i * 3] = c.r;
      colors[i * 3 + 1] = c.g;
      colors[i * 3 + 2] = c.b;
    }
    viewOverlay.modelPointsMesh.geometry.attributes.color.needsUpdate = true;
  }

  const totals = new Array(elements.length).fill(0);
  const supported = new Array(elements.length).fill(0);

  if (viz.model_samples) {
    for (const item of viz.model_samples) {
      const owner = item[4];
      const d = item[3];
      totals[owner]++;
      if (d <= newTol_m) supported[owner]++;
    }
  }

  let completeCount = 0;
  let partialCount = 0;
  let notStartedCount = 0;
  let allSupported = 0;
  let allTotal = 0;

  elements.forEach((el, i) => {
    if (totals[i] > 0) {
      const frac = supported[i] / totals[i];
      el.supported_surface_fraction = frac;
      el.completion_pct = +(frac * 100).toFixed(1);
      el.supported_samples = supported[i];
      el.surface_samples = totals[i];
      el.estimated_status = frac >= 0.85 ? 'complete' : frac < 0.35 ? 'not_started' : 'partial';
    }

    if (el.estimated_status === 'complete') completeCount++;
    else if (el.estimated_status === 'partial') partialCount++;
    else notStartedCount++;

    allSupported += el.supported_samples || 0;
    allTotal += el.surface_samples || 0;

    const col = getStatusColor(el.estimated_status);
    if (viewBim.meshes[i]) {
      viewBim.meshes[i].material.color.setHex(col);
      viewBim.meshes[i].userData.originalColor = col;
    }
    if (viewOverlay.meshes[i]) {
      viewOverlay.meshes[i].material.color.setHex(col);
      viewOverlay.meshes[i].userData.originalColor = col;
    }
  });

  const overallPct = allTotal > 0 ? (allSupported / allTotal) * 100 : 0;
  $('heroProgressVal').textContent = `${overallPct.toFixed(1)}%`;
  const circumference = 2 * Math.PI * 50;
  $('ringProgress').style.strokeDashoffset = circumference - (overallPct / 100) * circumference;
  $('ringProgress').style.stroke = overallPct >= 80 ? 'var(--complete)' : overallPct >= 40 ? 'var(--partial)' : 'var(--not-started)';

  $('countComplete').textContent = `${completeCount} Complete`;
  $('countPartial').textContent = `${partialCount} Partial`;
  $('countNotStarted').textContent = `${notStartedCount} Not Started`;
  $('overallBadge').textContent = `${completeCount} of ${elements.length} Complete`;

  renderElementsList(elements);
  update3DDisplayMode();
}

// Render Element Breakdown Table
function renderElementsList(elements) {
  const container = $('elementsList');
  $('elemTotalCount').textContent = elements.length;

  if (!elements || elements.length === 0) {
    container.innerHTML = `<div class="empty-state">No elements found.</div>`;
    return;
  }

  container.innerHTML = elements
    .map((el, i) => {
      const statusClass = el.estimated_status;
      const statusTitle = el.estimated_status === 'complete' ? 'Complete' : el.estimated_status === 'partial' ? 'In Progress' : 'Not Started';
      const pct = (el.completion_pct !== undefined ? el.completion_pct : (el.supported_surface_fraction * 100)).toFixed(1);
      const gt = el.ground_truth;
      const gtBadge = gt ? `<span title="Ground Truth Built Volume: ${gt.built_volume_pct}%" style="font-size:10px;padding:1px 4px;border-radius:3px;background:rgba(255,255,255,0.08);color:var(--text-muted)">GT: ${gt.built_volume_pct}%</span>` : '';

      return `
      <div class="element-row ${selectedElementId === el.element_id ? 'selected' : ''}" data-id="${el.element_id}" data-index="${i}" data-status="${statusClass}">
        <div class="row-top">
          <div>
            <span class="elem-name">${escapeHtml(el.element_id)}</span>
            <span class="elem-type">${escapeHtml(el.ifc_class)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px">
            ${gtBadge}
            <span class="status-badge ${statusClass}">${statusTitle}</span>
            <span class="elem-pct">${pct}%</span>
          </div>
        </div>
        <div class="row-bar-wrap">
          <div class="row-bar-fill ${statusClass}" style="width: ${Math.min(100, Math.max(0, pct))}%"></div>
        </div>
        <div class="row-details">
          <span>Samples: ${el.supported_samples || 0} / ${el.surface_samples || 0}</span>
          <span>Vol: ${el.volume_m3 ? el.volume_m3.toFixed(2) : '-'} m³</span>
          <span>Dev: ${el.mean_deviation_mm ? el.mean_deviation_mm.toFixed(1) : '-'} mm</span>
        </div>
      </div>
    `;
    })
    .join('');

  container.querySelectorAll('.element-row').forEach((row) => {
    row.addEventListener('click', () => {
      const idx = Number(row.dataset.index);
      selectElementByIndex(idx);
    });
  });
}

// Select element in 3D and in Table
function selectElementByIndex(idx) {
  if (idx < 0 || idx >= viewOverlay.meshes.length) return;
  const mesh = viewOverlay.meshes[idx];
  const el = mesh.userData.element;
  selectedElementId = el.element_id;

  [viewBim.meshes, viewOverlay.meshes].forEach((meshList) => {
    meshList.forEach((m) => {
      m.material.emissive = new THREE.Color(0x000000);
    });
    if (meshList[idx]) {
      meshList[idx].material.emissive = new THREE.Color(0x2563eb);
      meshList[idx].material.emissiveIntensity = 0.5;
    }
  });

  document.querySelectorAll('.element-row').forEach((r) => {
    r.classList.toggle('selected', r.dataset.id === selectedElementId);
  });
  const activeRow = document.querySelector(`.element-row[data-id="${selectedElementId}"]`);
  if (activeRow) activeRow.scrollIntoView({ block: 'nearest', behavior: 'smooth' });

  showTooltipForElement(el);
}

// 3D Tooltip Inspector Display
function showTooltipForElement(el) {
  const tip = $('tooltip3d');
  $('tipName').textContent = el.element_id;
  const badge = $('tipStatusBadge');
  badge.textContent = el.estimated_status === 'complete' ? 'Complete' : el.estimated_status === 'partial' ? 'In Progress' : 'Not Started';
  badge.className = `status-badge ${el.estimated_status}`;

  $('tipClass').textContent = el.ifc_class;
  $('tipPct').textContent = `${(el.completion_pct !== undefined ? el.completion_pct : el.supported_surface_fraction * 100).toFixed(1)}%`;
  $('tipSamples').textContent = `${el.supported_samples || 0} / ${el.surface_samples || 0}`;
  $('tipDev').textContent = `${el.mean_deviation_mm ? el.mean_deviation_mm.toFixed(1) : '-'} mm`;

  const d = el.dimensions;
  $('tipDim').innerHTML = d ? `${d[0]} &times; ${d[1]} &times; ${d[2]} m` : '-';
  $('tipVol').innerHTML = el.volume_m3 ? `${el.volume_m3.toFixed(2)} m&sup3;` : '-';

  if (el.ground_truth) {
    $('tipGtRow').style.display = 'flex';
    $('tipGt').textContent = `${el.ground_truth.built_volume_pct}% Built (${el.ground_truth.status})`;
  } else {
    $('tipGtRow').style.display = 'none';
  }

  tip.classList.remove('hidden');
}

// Filter elements by text
function filterElementRows(query) {
  document.querySelectorAll('.element-row').forEach((row) => {
    const id = row.dataset.id.toLowerCase();
    row.style.display = id.includes(query) ? 'block' : 'none';
  });
}

// Filter elements by status tabs
function applyStatusFilter(filter) {
  document.querySelectorAll('.element-row').forEach((row) => {
    if (filter === 'all') {
      row.style.display = 'block';
    } else {
      row.style.display = row.dataset.status === filter ? 'block' : 'none';
    }
  });
}

// Save outputs to disk
async function saveToDisk() {
  if (!currentData) {
    toast('No alignment result to save yet. Run matching first.');
    return;
  }
  try {
    const res = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await res.json();
    if (d.status === 'success') {
      toast('Successfully saved aligned_scan.ply and icp_alignment.json to dataset folder!');
    } else {
      toast(`Export failed: ${d.error}`);
    }
  } catch (err) {
    toast(`Export error: ${err.message}`);
  }
}

// File picker modal dialog
let fileDialogMode = null;
let currentBrowsePath = null;
let selectedDialogFile = null;

function openFileDialog(mode) {
  fileDialogMode = mode;
  $('dialogTitle').textContent = mode === 'ifc' ? 'Select IFC Model (.ifc)' : 'Select Scan Cloud (.ply)';
  $('fileDialog').classList.remove('hidden');
  browseFolder(null, mode === 'ifc' ? '.ifc' : '.ply');
}

async function browseFolder(dirPath, ext) {
  try {
    const url = `/api/browse?${dirPath ? 'path=' + encodeURIComponent(dirPath) + '&' : ''}${ext ? 'ext=' + encodeURIComponent(ext) : ''}`;
    const res = await fetch(url);
    const data = await res.json();

    currentBrowsePath = data.path;
    $('dialogCurrentPath').value = data.rel || data.path;
    renderDialogList(data);
  } catch (err) {
    console.error('Browse error:', err);
  }
}

function renderDialogList(data) {
  const container = $('dialogList');
  container.innerHTML = '';

  if (data.parent) {
    const upItem = document.createElement('div');
    upItem.className = 'dialog-item';
    upItem.innerHTML = '📁 <b>.. (Parent Directory)</b>';
    upItem.onclick = () => browseFolder(data.parent, fileDialogMode === 'ifc' ? '.ifc' : '.ply');
    container.appendChild(upItem);
  }

  data.dirs.forEach((d) => {
    const item = document.createElement('div');
    item.className = 'dialog-item';
    item.innerHTML = `📁 ${escapeHtml(d.name)}`;
    item.onclick = () => browseFolder(d.path, fileDialogMode === 'ifc' ? '.ifc' : '.ply');
    container.appendChild(item);
  });

  data.files.forEach((f) => {
    const item = document.createElement('div');
    item.className = 'dialog-item';
    item.innerHTML = `📄 ${escapeHtml(f.name)} <span style="float:right;color:var(--text-dim);font-size:11px">${(f.size / 1024).toFixed(0)} KB</span>`;
    item.onclick = () => {
      document.querySelectorAll('.dialog-item').forEach((i) => i.classList.remove('selected'));
      item.classList.add('selected');
      selectedDialogFile = f.path;
      $('dialogConfirm').disabled = false;
    };
    container.appendChild(item);
  });
}

// Dialog close & confirm handlers
$('dialogClose').onclick = closeFileDialog;
$('dialogCancel').onclick = closeFileDialog;
$('dialogConfirm').onclick = async () => {
  if (selectedDialogFile) {
    if (fileDialogMode === 'ifc') {
      activeIfcPath = selectedDialogFile;
      $('labelIfc').textContent = selectedDialogFile.split('/').slice(-2).join('/');
    } else {
      activeScanPath = selectedDialogFile;
      $('labelScan').textContent = selectedDialogFile.split('/').slice(-2).join('/');
    }
    closeFileDialog();
    await fetchAndRenderScenePreview();
  }
};

function closeFileDialog() {
  $('fileDialog').classList.add('hidden');
  selectedDialogFile = null;
  $('dialogConfirm').disabled = true;
}

// Toast helper
let toastTimer = null;
function toast(msg, ms = 3500) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

function escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
