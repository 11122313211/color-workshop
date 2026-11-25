/* three.js 真 3D 渲染器（V0.4）：注册到 gamut-view 渲染器注册表的 'three' 槽位。
 *
 * 本模块由 gamut-view.js 在首次切入 3D 模式时动态 import（three.js 约 2.1MB，
 * 静态引入会拖慢首屏）。模块顶层自注册：
 *   draw(canvas, data, state, spectrum) → 屏幕点集 {sx, sy, lab}
 *   hitTest(screenPts, px, py, radius)  → 最近点
 *   dispose()
 *
 * 视觉设计（对齐 V0.4 实施规划 §3.3）：
 * - 表面网格：Lab 空间凸包包络（后端 scipy Qhull 输出三角索引 data.surface.triangles），
 *   逐顶点 Lab 真彩（linear sRGB 顶点色）+ Lambert 光照，透明外壳。
 * - 点云：ShaderMaterial，L* 切片走 uniform（零几何重建），点尺寸随深度衰减。
 * - 底面 a*b* 网格线 + 主题令牌配色（resolveToken）。
 */

import * as THREE from "/static/vendor/three/three.module.js";
import { registerRenderer } from "./gamut-view.js";
import { labToLinearRgb, resolveToken } from "./components.js";

/* ---------- 与 gamut-view.js 一致的坐标归一化 ---------- */

// Lab → 世界坐标：x = a*（红绿）、y（上）= L*、z = b*（黄蓝），均归一到 [0,1]
function labPos(lab) {
  return [(lab[1] + 128) / 256, lab[0] / 100, (lab[2] + 128) / 256];
}

/* ---------- 渲染上下文（按 canvas 键控；应用内同时只有一个色域视图实例） ---------- */

let ctx = null; // { canvas, renderer, scene, camera, dir, grid, points, mesh, uniforms, dataRef }

function ensureCtx(canvas) {
  if (ctx && ctx.canvas === canvas) return ctx;
  if (ctx) disposeCtx();
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setClearColor(0x000000, 0); // 透明背景，主题底色透出

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(40, 1, 0.05, 10);
  const ambient = new THREE.AmbientLight(0xffffff, 1.6);
  const dir = new THREE.DirectionalLight(0xffffff, 2.2);
  scene.add(ambient, dir);

  // 底面 a*b* 网格（L=0 平面，y=0）
  const gridPts = [];
  for (let i = 0; i <= 4; i++) {
    const t = i / 4;
    gridPts.push(t, 0, 0, t, 0, 1);
    gridPts.push(0, 0, t, 1, 0, t);
  }
  const gridGeom = new THREE.BufferGeometry();
  gridGeom.setAttribute("position", new THREE.Float32BufferAttribute(gridPts, 3));
  const gridMat = new THREE.LineBasicMaterial({
    color: new THREE.Color(resolveToken("--label-3")),
    transparent: true,
    opacity: 0.4,
  });
  const grid = new THREE.LineSegments(gridGeom, gridMat);
  scene.add(grid);

  ctx = { canvas, renderer, scene, camera, dir, grid, points: null, mesh: null, uniforms: null, dataRef: null };
  return ctx;
}

function disposeCtx() {
  if (!ctx) return;
  for (const obj of [ctx.points, ctx.mesh]) {
    if (!obj) continue;
    ctx.scene.remove(obj);
    obj.geometry.dispose();
    obj.material.dispose();
  }
  ctx.grid.geometry.dispose();
  ctx.grid.material.dispose();
  ctx.renderer.dispose();
  ctx = null;
}

function buildGamutObjects(data) {
  const labs = data.lab_points || [];
  const n = labs.length;
  const positions = new Float32Array(n * 3);
  const colors = new Float32Array(n * 3);
  const aLabs = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const p = labPos(labs[i]);
    positions[i * 3] = p[0];
    positions[i * 3 + 1] = p[1];
    positions[i * 3 + 2] = p[2];
    const rgb = labToLinearRgb(labs[i]);
    colors[i * 3] = rgb[0];
    colors[i * 3 + 1] = rgb[1];
    colors[i * 3 + 2] = rgb[2];
    aLabs[i] = labs[i][0];
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
  geom.setAttribute("aColor", new THREE.BufferAttribute(colors, 3));
  geom.setAttribute("aLab", new THREE.BufferAttribute(aLabs, 1));

  const uniforms = {
    uSlice: { value: -1 },
    uSliceOn: { value: 0 },
    uSize: { value: 1.0 * (window.devicePixelRatio || 1) },
  };
  const points = new THREE.Points(
    geom,
    new THREE.ShaderMaterial({
      uniforms,
      transparent: true,
      depthWrite: false,
      vertexShader: `
        attribute vec3 aColor;
        attribute vec3 aLab;
        varying vec3 vColor;
        varying float vL;
        uniform float uSize;
        void main() {
          vColor = aColor;
          vL = aLab.x;
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = uSize * (6.0 / -mv.z);
          gl_Position = projectionMatrix * mv;
        }
      `,
      fragmentShader: `
        varying vec3 vColor;
        varying float vL;
        uniform float uSlice;
        uniform float uSliceOn;
        void main() {
          float a = uSliceOn > 0.5 ? (abs(vL - uSlice) <= 6.0 ? 0.95 : 0.06) : 0.9;
          if (a < 0.02) discard;
          gl_FragColor = vec4(vColor, a);
          #include <colorspace_fragment>
        }
      `,
    })
  );

  // —— 表面网格（Qhull 凸包包络，三角索引指向 lab_points）——
  let mesh = null;
  const tris = data.surface && data.surface.triangles;
  if (tris && tris.length) {
    const index = [];
    for (const t of tris) index.push(t[0], t[1], t[2]);
    const mgeom = new THREE.BufferGeometry();
    mgeom.setAttribute("position", new THREE.BufferAttribute(positions.slice(), 3));
    mgeom.setAttribute("color", new THREE.BufferAttribute(colors.slice(), 3));
    mgeom.setIndex(index);
    const mmat = new THREE.MeshLambertMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.42,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    mesh = new THREE.Mesh(mgeom, mmat);
  }
  return { points, mesh, uniforms };
}

function refreshObjects(data) {
  if (!ctx || ctx.dataRef === data) return;
  for (const key of ["points", "mesh"]) {
    if (ctx[key]) {
      ctx.scene.remove(ctx[key]);
      ctx[key].geometry.dispose();
      ctx[key].material.dispose();
      ctx[key] = null;
    }
  }
  const built = buildGamutObjects(data);
  ctx.points = built.points;
  ctx.mesh = built.mesh;
  ctx.uniforms = built.uniforms;
  ctx.dataRef = data;
  ctx.scene.add(ctx.points);
  if (ctx.mesh) ctx.scene.add(ctx.mesh);
}

/* ---------- 注册表契约实现 ---------- */

const gamutThree = {
  name: "three",
  draw(canvas, data, state) {
    let c;
    try {
      c = ensureCtx(canvas);
    } catch (e) {
      return null; // WebGL 不可用 → 调用方回退 canvas2d 伪 3D
    }
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(canvas.clientWidth || 420, 50);
    const h = Math.max(canvas.clientHeight || w, 50);
    c.renderer.setPixelRatio(dpr);
    c.renderer.setSize(w, h, false);

    // 相机：方向复用 gamut-view basis(θ,φ).eye，距离 = zoom（拖拽/双击手感与伪 3D 一致）
    const th = (state.theta * Math.PI) / 180;
    const ph = (state.phi * Math.PI) / 180;
    const dist = state.zoom || 2.2;
    c.camera.position
      .set(Math.cos(ph) * Math.sin(th), Math.sin(ph), Math.cos(ph) * Math.cos(th))
      .multiplyScalar(dist)
      .add(new THREE.Vector3(0.5, 0.5, 0.5));
    c.camera.lookAt(0.5, 0.5, 0.5);
    c.dir.position.copy(c.camera.position);
    c.camera.aspect = w / h;
    c.camera.updateProjectionMatrix();

    refreshObjects(data);

    // L* 切片 → uniforms（零几何重建）；切片开启时外壳淡化以聚焦切片层
    if (c.uniforms) {
      c.uniforms.uSliceOn.value = state.slice == null ? 0 : 1;
      c.uniforms.uSlice.value = state.slice == null ? -1 : state.slice;
    }
    if (c.points) c.points.visible = state.showPoints !== false;
    if (c.mesh) {
      c.mesh.visible = state.showHull !== false;
      c.mesh.material.opacity = state.slice == null ? 0.42 : 0.08;
    }
    c.grid.material.color.set(resolveToken("--label-3"));

    c.renderer.render(c.scene, c.camera);

    if (state.showPoints === false) return [];
    // 屏幕点集（tooltip 命中）：世界坐标 → NDC → 像素
    const v = new THREE.Vector3();
    return (data.lab_points || []).map((lab) => {
      const p = labPos(lab);
      v.set(p[0], p[1], p[2]).project(c.camera);
      return { sx: ((v.x + 1) / 2) * w, sy: ((1 - v.y) / 2) * h, lab };
    });
  },
  hitTest(screenPts, px, py, radius = 10) {
    let best = null;
    let bestD2 = radius * radius;
    for (const p of screenPts) {
      if (!p) continue;
      const d2 = (p.sx - px) ** 2 + (p.sy - py) ** 2;
      if (d2 <= bestD2) {
        bestD2 = d2;
        best = p;
      }
    }
    return best;
  },
  dispose() {
    disposeCtx();
  },
};

registerRenderer("three", gamutThree);
