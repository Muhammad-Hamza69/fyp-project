/**
 * NeuroFlow 3D vessel viewer.
 *
 * Renders the ACTUAL solved vessel surface for the selected case, coloured by
 * the computed wall shear stress field.
 *
 * This replaces the previous behaviour, which loaded a generic 35.7 MB nerve
 * model (`NervesOnly_v1.glb`, ~70 s to download) and approximated "the
 * aneurysm" as the topmost cluster of its vertices. That model had no
 * relationship to any patient's vasculature, so the colouring was an
 * illustration rather than a result.
 *
 * Now each computed case ships its own mesh at models/{id}.glb — the same
 * surface OpenFOAM solved on, decimated to 40k triangles with per-vertex
 * colour baked from TAWSS (or OSI). Typical size ~1 MB, a 34x reduction, and
 * the geometry is patient-specific.
 *
 * Colour is baked server-side rather than shaded in the browser so that the
 * 3D view uses byte-for-byte the same normalisation as the 2D heatmap and the
 * gauges — they cannot drift apart.
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

let scene, camera, renderer, controls;
let mountEl, loadingEl;
let initialized = false;
let currentModel = null;
let currentKey = null;      // `${patientId}:${mode}` currently displayed
let loadToken = 0;          // guards against out-of-order async loads

function setMessage(html, isError) {
    if (!loadingEl) return;
    loadingEl.innerHTML = html;
    loadingEl.classList.remove('hidden');
    loadingEl.style.color = isError ? 'var(--color-high-risk)' : '';
}

function hideMessage() {
    if (loadingEl) loadingEl.classList.add('hidden');
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

/** Release GPU resources before swapping models — otherwise every case switch
 *  leaks a mesh and the 4 GB GPU eventually loses its WebGL context. */
function disposeModel() {
    if (!currentModel) return;
    currentModel.traverse((child) => {
        if (child.isMesh) {
            child.geometry?.dispose();
            if (Array.isArray(child.material)) child.material.forEach(m => m.dispose());
            else child.material?.dispose();
        }
    });
    scene.remove(currentModel);
    currentModel = null;
}

function init(containerId) {
    mountEl = document.getElementById(containerId);
    loadingEl = document.getElementById('neuro-3d-loading');
    if (!mountEl) return;

    if (initialized) { onResize(); return; }

    const w = mountEl.clientWidth || 600, h = mountEl.clientHeight || 400;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0b1524);

    camera = new THREE.PerspectiveCamera(45, w / h, 0.01, 100);
    camera.position.set(0, 0.35, 2.4);

    renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    mountEl.appendChild(renderer.domElement);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.minDistance = 0.4;
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

/**
 * Load and display the mesh for a patient.
 *
 * Demo cases have no solved geometry — there is nothing real to render, so we
 * say so rather than showing an unrelated model and implying otherwise.
 */
function applyRiskColors(patient, mode) {
    if (!patient || !initialized) return;

    const isComputed = patient.provenance && patient.provenance.source === 'computed';
    if (!isComputed) {
        disposeModel();
        currentKey = null;
        setMessage(
            '<i class="fa-solid fa-circle-info"></i> No solved geometry for this case.<br>' +
            'The 3D view renders the vessel surface produced by the CFD pipeline; ' +
            'demonstration cases have no computed mesh.<br>' +
            'Select a case marked <strong>CFD</strong> to view its geometry.',
            false
        );
        return;
    }

    const suffix = (mode === 'OSI') ? '-osi' : '';
    const url = `models/${patient.id}${suffix}.glb`;
    const key = `${patient.id}:${mode}`;
    if (key === currentKey) return;

    const token = ++loadToken;
    setMessage('<i class="fa-solid fa-circle-notch fa-spin"></i> Loading solved vessel surface…');

    new GLTFLoader().load(
        url,
        (gltf) => {
            // A newer request started while this one was in flight — discard.
            if (token !== loadToken) return;
            disposeModel();

            const model = gltf.scene;
            model.traverse((child) => {
                if (child.isMesh) {
                    child.material = new THREE.MeshStandardMaterial({
                        vertexColors: true,   // colour baked server-side
                        metalness: 0.1,
                        roughness: 0.55,
                        side: THREE.DoubleSide,
                    });
                }
            });

            // Frame the model regardless of its physical dimensions.
            const box = new THREE.Box3().setFromObject(model);
            const size = new THREE.Vector3(), centre = new THREE.Vector3();
            box.getSize(size); box.getCenter(centre);
            const maxDim = Math.max(size.x, size.y, size.z) || 1;
            model.scale.setScalar(1.8 / maxDim);
            model.position.sub(centre.multiplyScalar(1.8 / maxDim));

            scene.add(model);
            currentModel = model;
            currentKey = key;
            hideMessage();
        },
        undefined,
        () => {
            if (token !== loadToken) return;
            setMessage(
                '<i class="fa-solid fa-triangle-exclamation"></i> Could not load the vessel mesh.<br>' +
                `Expected <code>${url}</code>.<br>` +
                'If this page was opened directly from disk (file://), serve it over HTTP instead ' +
                '(e.g. <code>python -m http.server</code>).',
                true
            );
        }
    );
}

window.NeuroViewer = { init, applyRiskColors };
