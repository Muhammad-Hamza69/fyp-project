/**
 * NeuroFlow 3D Nerve/Vascular Model Viewer
 * Loads NervesOnly_v1.glb and colors it (blue -> red) using the same
 * TAWSS/OSI risk thresholds as the 2D heatmap, driven by the active patient.
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const MODEL_URL = 'NervesOnly_v1.glb';
const STABLE_COLOR = '#1F5F99';
const CRITICAL_COLOR = '#B83232';

let scene, camera, renderer, controls;
let modelMeshes = [];
let mountEl, loadingEl;
let initialized = false;
let loadStarted = false;

// Per-vertex data for localized (not whole-mesh) risk coloring on the nerve mesh.
// falloff[i] is 1.0 at the "hotspot" (aneurysm-adjacent) region and fades to 0.0
// away from it, so only that region turns red while the rest stays blue.
let nerveVertexData = null; // { falloff: Float32Array, colorAttr: THREE.BufferAttribute, vertexCount: number }

function setLoadingMessage(html, isError) {
    if (!loadingEl) return;
    loadingEl.innerHTML = html;
    loadingEl.classList.remove('hidden');
    loadingEl.style.color = isError ? 'var(--color-high-risk)' : '';
}

function hideLoading() {
    if (loadingEl) loadingEl.classList.add('hidden');
}

function riskFactorForZone(zone, mode) {
    let factor;
    if (mode === 'OSI') {
        factor = Math.max(0, Math.min(1, (zone.osi - 0.03) / (0.35 - 0.03)));
    } else {
        factor = 1.0 - Math.max(0, Math.min(1, (zone.tawss - 0.15) / (1.5 - 0.15)));
    }
    if (!zone.isAneurysm) factor *= 0.2;
    return factor;
}

function onResize() {
    if (!mountEl || !renderer || !camera) return;
    const width = mountEl.clientWidth;
    const height = mountEl.clientHeight;
    if (width === 0 || height === 0) return;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
}

function animate() {
    requestAnimationFrame(animate);
    if (controls) controls.update();
    if (renderer && scene && camera) renderer.render(scene, camera);
}

// The asset has no per-vertex clinical labels (no "this is the aneurysm"
// vertex group), so the critical region is approximated as the topmost
// cluster of vertices - matching where the aneurysm dome sits (above the
// neck, above the parent artery) in the 2D heatmap layout. Returns a
// per-vertex 0..1 weight: 1.0 at that cluster's centroid, fading to 0.0
// beyond a radius proportional to the mesh's own size.
function computeHotspotFalloff(geometry) {
    geometry.computeBoundingBox();
    const bbox = geometry.boundingBox;
    const posAttr = geometry.attributes.position;
    const vertexCount = posAttr.count;

    const yThreshold = bbox.min.y + (bbox.max.y - bbox.min.y) * 0.92;
    const hotspot = new THREE.Vector3();
    let hotspotCount = 0;
    for (let i = 0; i < vertexCount; i++) {
        const y = posAttr.getY(i);
        if (y >= yThreshold) {
            hotspot.x += posAttr.getX(i);
            hotspot.y += y;
            hotspot.z += posAttr.getZ(i);
            hotspotCount++;
        }
    }
    if (hotspotCount > 0) hotspot.divideScalar(hotspotCount);
    else hotspot.set(0, bbox.max.y, 0);

    const maxDim = Math.max(bbox.max.x - bbox.min.x, bbox.max.y - bbox.min.y, bbox.max.z - bbox.min.z);
    const hotRadius = maxDim * 0.42;

    const falloff = new Float32Array(vertexCount);
    const v = new THREE.Vector3();
    for (let i = 0; i < vertexCount; i++) {
        v.set(posAttr.getX(i), posAttr.getY(i), posAttr.getZ(i));
        const t = Math.max(0, 1 - v.distanceTo(hotspot) / hotRadius);
        falloff[i] = t * t * (3 - 2 * t); // smoothstep for a soft edge
    }
    return falloff;
}

function loadModel() {
    if (loadStarted) return;
    loadStarted = true;

    const loader = new GLTFLoader();
    loader.load(
        MODEL_URL,
        (gltf) => {
            const model = gltf.scene;

            // Normalize scale/position so the model fills the viewport nicely
            const box = new THREE.Box3().setFromObject(model);
            const size = new THREE.Vector3();
            const center = new THREE.Vector3();
            box.getSize(size);
            box.getCenter(center);
            const maxDim = Math.max(size.x, size.y, size.z) || 1;
            const scale = 1.6 / maxDim;
            model.scale.setScalar(scale);
            model.position.sub(center.multiplyScalar(scale));

            // This asset bundles 3 sub-meshes: the first one encountered is the
            // actual nerve/vessel branching network (~450k verts, the "NervesOnly"
            // part) - the other two are solid brain-shape geometry left over from
            // the source model. Those would visually bury the nerve mesh if shown,
            // so only the nerve mesh is kept visible.
            modelMeshes = [];
            let meshIndex = 0;
            model.traverse((child) => {
                if (child.isMesh) {
                    const isNerveMesh = meshIndex === 0;
                    meshIndex++;

                    if (!isNerveMesh) {
                        child.visible = false;
                        return;
                    }

                    const falloff = computeHotspotFalloff(child.geometry);
                    const colorAttr = new THREE.BufferAttribute(new Float32Array(falloff.length * 3), 3);
                    child.geometry.setAttribute('color', colorAttr);
                    nerveVertexData = { falloff, colorAttr, vertexCount: falloff.length };

                    child.material = new THREE.MeshStandardMaterial({
                        color: 0xffffff,
                        vertexColors: true,
                        metalness: 0.15,
                        roughness: 0.45
                    });
                    modelMeshes.push(child);
                }
            });

            scene.add(model);
            hideLoading();

            if (window.__neuroPendingRiskUpdate) {
                applyRiskColors(window.__neuroPendingRiskUpdate.patient, window.__neuroPendingRiskUpdate.mode);
            }
        },
        undefined,
        (err) => {
            console.error('[NeuroViewer] Failed to load NervesOnly_v1.glb', err);
            setLoadingMessage(
                '<i class="fa-solid fa-triangle-exclamation"></i> Could not load the 3D model.<br>' +
                'This usually means the page was opened directly as a file (file://) and the browser blocked the model fetch.<br>' +
                'Serve this folder over local HTTP (e.g. VS Code "Live Server", or <code>python -m http.server</code>) and reload.',
                true
            );
        }
    );
}

function init(containerId) {
    mountEl = document.getElementById(containerId);
    loadingEl = document.getElementById('neuro-3d-loading');
    if (!mountEl) return;

    if (initialized) {
        onResize();
        loadModel();
        return;
    }

    const width = mountEl.clientWidth || 600;
    const height = mountEl.clientHeight || 400;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1524);

    camera = new THREE.PerspectiveCamera(45, width / height, 0.1, 1000);
    camera.position.set(0, 0.1, 2.6);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    mountEl.appendChild(renderer.domElement);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 0.8;
    controls.maxDistance = 6;

    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
    const keyLight = new THREE.DirectionalLight(0xffffff, 1.2);
    keyLight.position.set(4, 5, 6);
    scene.add(keyLight);
    const fillLight = new THREE.DirectionalLight(0x88aaff, 0.5);
    fillLight.position.set(-5, -3, -4);
    scene.add(fillLight);

    window.addEventListener('resize', onResize);

    initialized = true;
    animate();
    loadModel();
}

function applyRiskColors(patient, mode) {
    if (!patient) return;

    if (modelMeshes.length === 0 || !nerveVertexData) {
        // Model not loaded yet - remember the latest request and apply once ready
        window.__neuroPendingRiskUpdate = { patient, mode };
        return;
    }

    // Drive redness by the aneurysm dome's live TAWSS/OSI (the same value the
    // 2D heatmap and gauges use), but apply it only near the hotspot region so
    // most of the nerve network stays blue and only the critical area shifts red.
    const domeZone = patient.zones.find(z => z.isAneurysm && /dome/i.test(z.name)) || patient.zones.find(z => z.isAneurysm);
    const factor = riskFactorForZone(domeZone, mode);

    const stable = new THREE.Color(STABLE_COLOR);
    const critical = new THREE.Color(CRITICAL_COLOR);
    const tmp = new THREE.Color();
    const { falloff, colorAttr, vertexCount } = nerveVertexData;
    const colors = colorAttr.array;

    for (let i = 0; i < vertexCount; i++) {
        tmp.copy(stable).lerp(critical, falloff[i] * factor);
        colors[i * 3] = tmp.r;
        colors[i * 3 + 1] = tmp.g;
        colors[i * 3 + 2] = tmp.b;
    }
    colorAttr.needsUpdate = true;
}

window.NeuroViewer = { init, applyRiskColors };
