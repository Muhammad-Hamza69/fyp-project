/**
 * NeuroFlow 3D viewer — cerebral vasculature with a per-case aneurysm bulge.
 *
 * WHAT IS REAL HERE, AND WHAT IS NOT
 * ----------------------------------
 * The brain/vessel network is a GENERIC anatomical asset. It is not any
 * patient's vasculature and nothing in it comes from a scan. It is context.
 *
 * The BULGE is the data. For each case it is:
 *   - positioned  at the anatomical site recorded for that case (MCA / ICA /
 *                 ACOM / PCOM / posterior), snapped onto real vessel geometry
 *   - sized       from morphology measured on that case's reconstructed
 *                 surface: max dome diameter, neck diameter, aspect ratio
 *   - coloured    from that case's computed hemodynamics (TAWSS or OSI),
 *                 through the same normalisation as the 2D heatmap and gauges
 *
 * The sac is drawn at TRUE ANATOMICAL SCALE. An 8 mm aneurysm on a 167 mm
 * brain is genuinely small, and inflating it for visual effect would make the
 * one quantitative thing in this view a lie. Visibility is solved by moving
 * the CAMERA to the site instead, which costs nothing in accuracy.
 *
 * SIZE
 * ----
 * The source asset is 35.7 MB / 902k faces and took ~70 s to download. This
 * loads models/brain.glb — the vessel network alone, decimated to 150k faces,
 * 3.6 MB. The other two sub-meshes in the source were always hidden by this
 * viewer, so their bytes were downloaded and discarded on every visit.
 * The aneurysm site is precomputed into models/brain.json rather than found by
 * scanning 453k vertices in the browser on every page load.
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const ASSET_GLB = 'models/brain.glb';
const ASSET_META = 'models/brain.json';

// Identical endpoints to packages/shared/src/risk.ts and the 2D heatmap, so
// the 3D view cannot disagree with the gauges beside it.
const STABLE_COLOR = '#1F5F99';
const CRITICAL_COLOR = '#B83232';
const TAWSS_MIN = 0.15, TAWSS_MAX = 1.5;
const OSI_MIN = 0.03, OSI_MAX = 0.35;

// Fixed viewing distance in model units, identical for every case so sac sizes
// are directly comparable between patients. Mirrored in render_brain.py.
const VIEW_DISTANCE = 0.52;

let scene, camera, renderer, controls;
let mountEl, loadingEl;
let initialized = false;
let webglBlocked = false;
let assetMeta = null;
let vesselMesh = null;
let sacMesh = null;         // the per-case aneurysm; rebuilt on every switch
let currentKey = null;
let loadPromise = null;

function setMessage(html, isError) {
    if (!loadingEl) return;
    loadingEl.innerHTML = html;
    loadingEl.classList.remove('hidden');
    loadingEl.style.color = isError ? 'var(--color-high-risk)' : '';
}

function hideMessage() {
    if (loadingEl) loadingEl.classList.add('hidden');
}

function clamp01(v) { return Math.max(0, Math.min(1, v)); }

/** 0..1 risk for a zone — mirrors riskFactor() in the shared risk library. */
function riskFactorForZone(zone, mode) {
    if (!zone) return 0;
    return mode === 'OSI'
        ? clamp01((zone.osi - OSI_MIN) / (OSI_MAX - OSI_MIN))
        : 1.0 - clamp01((zone.tawss - TAWSS_MIN) / (TAWSS_MAX - TAWSS_MIN));
}

function onResize() {
    if (!mountEl || !renderer || !camera) return;
    const w = mountEl.clientWidth, h = mountEl.clientHeight;
    if (!w || !h) return;
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
}

function animate() {
    requestAnimationFrame(animate);
    if (controls) controls.update();
    if (renderer && scene && camera) renderer.render(scene, camera);
}

/**
 * Is a WebGL context obtainable at all?
 *
 * Checked explicitly because THREE.WebGLRenderer throws a bare "Error creating
 * WebGL context" that says nothing about the cause. The usual reason is that
 * the browser's hardware acceleration is switched off or the GPU is
 * blocklisted — a machine setting, not a fault in the page — and the user
 * needs to be told that rather than shown a stack trace.
 */
function webglAvailable() {
    try {
        const c = document.createElement('canvas');
        return !!(window.WebGLRenderingContext &&
                  (c.getContext('webgl2') || c.getContext('webgl') ||
                   c.getContext('experimental-webgl')));
    } catch {
        return false;
    }
}

function showStaticFallback(patient, mode, reason) {
    const suffix = (mode === 'OSI') ? '-osi' : '';
    setMessage(
        `<div style="text-align:left;max-width:52ch">
           <i class="fa-solid fa-circle-info"></i> <strong>Interactive 3D unavailable</strong><br>
           <span style="font-size:.85em;opacity:.85">${reason}</span><br><br>
           <span style="font-size:.85em">Showing a rendered view instead.
           All hemodynamic metrics beside this panel are unaffected.</span>
         </div>`, false);
    const holder = document.getElementById('neuro-3d-mount');
    if (!holder) return;
    holder.innerHTML =
        `<img src="models/${patient.id}${suffix}.png"
              alt="Vessel surface for ${patient.id}, coloured by ${mode}"
              style="width:100%;height:100%;object-fit:contain;background:#0b1524"
              onerror="this.style.display='none'">`;
}

/** Which anatomical anchor to use for this case. */
function resolveSite(patient) {
    if (!assetMeta) return null;
    const raw = (patient?.demographics?.site || '').trim().toUpperCase();
    const key = assetMeta.site_aliases?.[raw] || raw;
    return assetMeta.sites[key] ? key : assetMeta.default_site;
}

/**
 * Build the aneurysm sac for this case and attach it to the vessel network.
 *
 * WHY THE SAC IS ADDED GEOMETRY RATHER THAN A DEFORMATION
 * -------------------------------------------------------
 * The first attempt displaced every network vertex within a radius of the
 * site. That inflates EVERY vessel passing through that sphere independently,
 * so instead of one aneurysm you get four or five separate swollen branches —
 * a lumpy mass that reads as a rendering artefact. A saccular aneurysm is not
 * a thickened vessel; it is a sac hanging off one. Modelling it as its own
 * body is both truer to the anatomy and unambiguous on screen.
 *
 * Every dimension is measured, not chosen:
 *   width  = maxDiameter          (dome's widest span)
 *   neck   = neckDiameterMm       (where it meets the parent vessel)
 *   height = aspectRatio x neck   (aspect ratio IS height/neck)
 */
function buildSac(patient, mode) {
    if (!assetMeta || !scene) return;

    const siteKey = resolveSite(patient);
    const site = assetMeta.sites[siteKey];
    const m = patient.morphology || {};
    const upm = assetMeta.units_per_mm;

    const dome = patient.zones?.find(z => z.isAneurysm && /dome/i.test(z.name))
              || patient.zones?.find(z => z.isAneurysm);
    const factor = riskFactorForZone(dome, mode);

    const widthMm = Math.max(0.1, m.maxDiameter || 0);
    const neckMm = Math.max(0.1, m.neckDiameterMm || widthMm * 0.7);
    const heightMm = Math.max(0.1, (m.aspectRatio || 1.0) * neckMm);

    const rx = (widthMm / 2) * upm;      // half-width, both lateral axes
    const ry = (heightMm / 2) * upm;     // half-height, along the outward axis

    const C = new THREE.Vector3().fromArray(site.centre);
    const OUT = new THREE.Vector3().fromArray(site.outward).normalize();

    disposeSac();

    // A sphere scaled per-axis: round in cross-section, elongated along OUT by
    // the aspect ratio. Segment counts are modest — the sac is small on screen
    // and this is rebuilt on every case switch.
    const geom = new THREE.SphereGeometry(1, 40, 28);
    geom.scale(rx, ry, rx);

    const colour = new THREE.Color(STABLE_COLOR).lerp(
        new THREE.Color(CRITICAL_COLOR), factor);

    sacMesh = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
        color: colour,
        metalness: 0.05,
        roughness: 0.4,
        // A faint self-glow keeps the sac readable when it is partly occluded
        // by the vessels in front of it, which at the Circle of Willis it
        // usually is. It does not alter the colour's meaning.
        emissive: colour.clone().multiplyScalar(0.22),
    }));

    // Seat the sac on the vessel: its local +Y becomes OUT, and it is pushed
    // out by slightly less than a half-height so the base stays embedded in
    // the parent vessel rather than floating beside it.
    sacMesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), OUT);
    sacMesh.position.copy(C).addScaledVector(OUT, ry * 0.7);
    scene.add(sacMesh);

    // Aim at the sac, not the anatomical site: the sac sits outboard of the
    // site by its own half-height, so targeting the site would frame each case
    // differently depending on how tall its dome is.
    focusOn(sacMesh.position.clone());

    // Publish the numbers the sac was built from. Stating them beside the
    // render is what lets a viewer check the picture against the data rather
    // than take the shape on trust.
    const dims = document.getElementById('neuro-3d-sac-dims');
    if (dims) {
        dims.textContent =
            `${widthMm.toFixed(1)} mm dome × ${neckMm.toFixed(1)} mm neck, ` +
            `AR ${(m.aspectRatio || 1).toFixed(2)}, ${siteKey}`;
    }
    const modeEl = document.getElementById('neuro-3d-sac-mode');
    if (modeEl) modeEl.textContent = mode;
}

function disposeSac() {
    if (!sacMesh) return;
    scene.remove(sacMesh);
    sacMesh.geometry?.dispose();
    sacMesh.material?.dispose();
    sacMesh = null;
}

/**
 * Point the camera at the sac.
 *
 * The sac is small — 8 mm against a 167 mm brain — and sits at the Circle of
 * Willis, buried inside a dense network. Framing the whole brain would leave
 * it invisible. Moving the camera is the honest way to make it legible: the
 * geometry stays at true scale and only the viewpoint changes.
 *
 * The distance is FIXED, deliberately. Zooming proportionally to the sac would
 * make every aneurysm fill the same fraction of the screen and silently cancel
 * the size difference between cases — the one thing this view exists to show.
 */
function focusOn(centre) {
    if (!controls || !camera) return;
    const dist = VIEW_DISTANCE;
    const dirOut = centre.clone().normalize();
    // Offset slightly above the outward axis so the sac is seen against the
    // network rather than end-on.
    const eye = centre.clone().add(
        dirOut.multiplyScalar(dist)).add(new THREE.Vector3(0, 0.12 * dist, 0));
    camera.position.copy(eye);
    controls.target.copy(centre);
    controls.update();
}

async function loadAsset() {
    if (loadPromise) return loadPromise;
    loadPromise = (async () => {
        const metaRes = await fetch(ASSET_META);
        if (!metaRes.ok) throw new Error(`${ASSET_META} -> HTTP ${metaRes.status}`);
        assetMeta = await metaRes.json();

        const gltf = await new GLTFLoader().loadAsync(ASSET_GLB);
        const model = gltf.scene;

        let biggest = null;
        model.traverse((child) => {
            if (child.isMesh &&
                (!biggest || child.geometry.attributes.position.count >
                             biggest.geometry.attributes.position.count)) {
                biggest = child;
            }
        });
        if (!biggest) throw new Error('no mesh in brain.glb');

        vesselMesh = biggest;
        // The network is anatomical context, not data, so it stays a single
        // neutral colour. Every per-case value is carried by the sac.
        vesselMesh.material = new THREE.MeshStandardMaterial({
            color: new THREE.Color(STABLE_COLOR),
            metalness: 0.15, roughness: 0.45, side: THREE.DoubleSide,
        });

        scene.add(model);
        return assetMeta;
    })();
    return loadPromise;
}

function init(containerId) {
    mountEl = document.getElementById(containerId);
    loadingEl = document.getElementById('neuro-3d-loading');
    if (!mountEl) return;
    if (initialized) { onResize(); return; }

    if (!webglAvailable()) {
        webglBlocked = true;
        return;   // applyRiskColors falls back to the static render
    }

    const w = mountEl.clientWidth || 600, h = mountEl.clientHeight || 400;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1524);
    camera = new THREE.PerspectiveCamera(45, w / h, 0.01, 100);
    camera.position.set(0, 0.2, 2.4);

    // Even with the capability probe above, context creation can still fail
    // (driver blocklists, exhausted contexts). Catch it rather than let the
    // module die mid-init and leave the panel in an indeterminate state.
    try {
        renderer = new THREE.WebGLRenderer({ antialias: true });
    } catch (err) {
        webglBlocked = true;
        console.error('[NeuroViewer] WebGL context creation failed:', err);
        return;
    }
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    mountEl.appendChild(renderer.domElement);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 0.08;
    controls.maxDistance = 8;

    scene.add(new THREE.AmbientLight(0xffffff, 0.85));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(4, 5, 6);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0x88aaff, 0.45);
    fill.position.set(-5, -3, -4);
    scene.add(fill);

    window.addEventListener('resize', onResize);
    initialized = true;
    animate();
}

function applyRiskColors(patient, mode) {
    if (!patient) return;

    if (webglBlocked) {
        showStaticFallback(patient, mode,
            'This browser could not create a WebGL context. Enable hardware '
            + 'acceleration (Chrome: Settings -> System -> "Use graphics '
            + 'acceleration when available", then restart) to rotate the model.');
        return;
    }
    if (!initialized) return;

    const key = `${patient.id}:${mode}`;
    if (key === currentKey) return;
    currentKey = key;

    setMessage('<i class="fa-solid fa-circle-notch fa-spin"></i> Loading cerebral vasculature…');

    loadAsset().then(() => {
        // A newer case was selected while the asset was downloading.
        if (currentKey !== key) return;
        buildSac(patient, mode);
        hideMessage();
    }).catch((err) => {
        console.error('[NeuroViewer]', err);
        setMessage(
            '<i class="fa-solid fa-triangle-exclamation"></i> Could not load the 3D model.<br>' +
            `Expected <code>${ASSET_GLB}</code> and <code>${ASSET_META}</code>.<br>` +
            'If this page was opened directly from disk (file://), serve it over HTTP instead ' +
            '(e.g. <code>python -m http.server</code>).',
            true
        );
    });
}

window.NeuroViewer = { init, applyRiskColors };
