        // ==================== #15 皮肤系统（深空玻璃默认 / 浅色）单点应用 ====================
        // 皮肤标识挂 <html> 的 data-skin（缺省 = 深色）；只换令牌取值，组件 DOM 与规则选择器不动。
        function applySkin(skin) {
            const light = skin === 'light';
            if (light) document.documentElement.dataset.skin = 'light';
            else delete document.documentElement.dataset.skin;
            const label = document.getElementById('skinToggleLabel');
            if (label) label.textContent = light ? '深色皮肤' : '浅色皮肤';
            const btn = document.getElementById('skinToggle');
            if (btn) { btn.title = light ? '切换深色皮肤' : '切换浅色皮肤'; btn.setAttribute('aria-label', light ? '切换深色皮肤' : '切换浅色皮肤'); }
            try { localStorage.setItem(SKIN_KEY, light ? 'light' : 'dark'); } catch (e) {}
            // #16 流体配色随皮肤即时刷新（未初始化时函数内早退，不抛）
            if (typeof applyFluidTokens === 'function') applyFluidTokens();
        }
        function toggleSkin() {
            applySkin(document.documentElement.dataset.skin === 'light' ? 'dark' : 'light');
        }
        function initSkin() {
            const btn = document.getElementById('skinToggle');
            if (btn) btn.addEventListener('click', toggleSkin);
            applySkin(localStorage.getItem(SKIN_KEY));
        }

        // ==================== #16 流体置换背景（原生 WebGL2 + GLSL，零外部依赖） ====================
        // 全屏 canvas #fluidBg（body 首子元素、fixed/inset:0/z-index:0/pointer-events:none）替代旧
        // 双层网格底纹：铺满视口、位于内容层 .page（z-index:1）之下、随窗口自适应。
        // 配色唯一落点 = board.py 令牌块（--fluid-*）；本模块经 getComputedStyle 读取，不硬编码色值
        // （applyFluidTokens 内的数值兜底仅「缺令牌时防抛」，不参与配色决策）。
        // 纯函数 parseCssColor / fluidRenderSize / fluidTokenNames / fluidReadTokens 由 node harness 断言。
        const FLUID_MAX_DPR = 1.5;    // 渲染分辨率钳制：DPR 上限
        const FLUID_MAX_EDGE = 1920;  // 渲染分辨率钳制：最长边上限
        const FLUID_VEIL_AMOUNT = 0.7;  // 流体与 --fluid-veil 的混底比例（保内容可读性）
        let fluidEl = null, fluidGl = null, fluidProg = null, fluidLoc = null;
        let fluidRaf = 0, fluidAnimated = false, fluidPaused = true, fluidLast = 0, fluidTime = 0;

        // 令牌名清单（自含字面量：供 node harness 独立提取；与 board.py 令牌块集合的一致性由
        // tests/test_fluid_background.py:H5 对源码文本比对，防改名漂移）
        function fluidTokenNames() {
            return ['--fluid-color-a', '--fluid-color-b', '--fluid-color-c',
                    '--fluid-veil', '--fluid-opacity'];
        }

        // CSS 颜色 → [r,g,b]（0..1）；支持 #rgb / #rrggbb / rgb() / rgba()，非法串返回 null（不抛）
        function parseCssColor(str) {
            if (typeof str !== 'string') return null;
            const s = str.trim();
            if (s.charAt(0) === '#') {
                const h = s.slice(1);
                if (!/^[0-9a-fA-F]+$/.test(h)) return null;
                if (h.length === 3) {
                    return [parseInt(h[0] + h[0], 16) / 255, parseInt(h[1] + h[1], 16) / 255,
                            parseInt(h[2] + h[2], 16) / 255];
                }
                if (h.length === 6) {
                    return [parseInt(h.slice(0, 2), 16) / 255, parseInt(h.slice(2, 4), 16) / 255,
                            parseInt(h.slice(4, 6), 16) / 255];
                }
                return null;
            }
            const lower = s.toLowerCase();
            if (lower.indexOf('rgb(') !== 0 && lower.indexOf('rgba(') !== 0) return null;
            const open = s.indexOf('('), close = s.lastIndexOf(')');
            if (open < 0 || close < open) return null;
            const parts = s.slice(open + 1, close).split(',');
            if (parts.length < 3) return null;
            const out = [];
            for (let i = 0; i < 3; i++) {
                const v = parseFloat(parts[i]);
                if (isNaN(v)) return null;
                out.push(Math.max(0, Math.min(255, v)) / 255);
            }
            return out;
        }

        // 渲染缓冲区尺寸：DPR 上限与最长边上限双钳制；输入非法/为 0 时也保证结果 ≥ 1
        function fluidRenderSize(cssW, cssH, dpr, maxDpr, maxEdge) {
            const w = Math.max(1, Math.round(cssW || 0));
            const h = Math.max(1, Math.round(cssH || 0));
            const d = Math.min(Math.max(dpr || 1, 1), maxDpr);
            let rw = Math.max(1, Math.round(w * d));
            let rh = Math.max(1, Math.round(h * d));
            const edge = Math.max(rw, rh);
            if (edge > maxEdge) {
                const k = maxEdge / edge;
                rw = Math.max(1, Math.round(rw * k));
                rh = Math.max(1, Math.round(rh * k));
            }
            return {w: rw, h: rh};
        }

        // 读取流体令牌：去空白后返回；任一缺失回退 null（不抛），由调用方决定兜底
        function fluidReadTokens(cs) {
            const read = (name) => {
                try {
                    const v = cs ? cs.getPropertyValue(name) : '';
                    return v && v.trim() ? v.trim() : null;
                } catch (e) { return null; }
            };
            return {a: read('--fluid-color-a'), b: read('--fluid-color-b'), c: read('--fluid-color-c'),
                    veil: read('--fluid-veil'), opacity: read('--fluid-opacity')};
        }

        // 顶点：无属性缓冲的全屏三角形（gl_VertexID 推导）
        const FLUID_VS = `#version 300 es
out vec2 vUv;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    vUv = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;
        // 片元：4 阶 value-noise FBM + 两次域扭曲产出流体置换场，三色插值 + veil 混底 + 暗角 + 抖动
        const FLUID_FS = `#version 300 es
precision highp float;
in vec2 vUv;
out vec4 outColor;
uniform float uTime;
uniform vec2 uRes;
uniform vec3 uColorA;
uniform vec3 uColorB;
uniform vec3 uColorC;
uniform vec3 uVeil;
uniform float uVeilAmount;

float fhash(vec2 p) {
    p = fract(p * vec2(123.34, 456.21));
    p += dot(p, p + 45.32);
    return fract(p.x * p.y);
}
float vnoise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    float a = fhash(i), b = fhash(i + vec2(1.0, 0.0));
    float c = fhash(i + vec2(0.0, 1.0)), d = fhash(i + vec2(1.0, 1.0));
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}
float fbm(vec2 p) {
    float v = 0.0, amp = 0.5;
    mat2 rot = mat2(0.8, 0.6, -0.6, 0.8);
    for (int i = 0; i < 4; i++) {
        v += amp * vnoise(p);
        p = rot * p * 2.02 + 11.3;
        amp *= 0.5;
    }
    return v;
}
void main() {
    vec2 uv = vUv;
    float aspect = uRes.x / max(uRes.y, 1.0);
    vec2 p = vec2(uv.x * aspect, uv.y) * 3.0;
    float t = uTime * 0.06;
    vec2 q = vec2(fbm(p + vec2(0.0, t)), fbm(p + vec2(5.2, 1.3) - t));
    vec2 r = vec2(fbm(p + 2.0 * q + vec2(1.7, 9.2) + t * 1.3),
                  fbm(p + 2.0 * q + vec2(8.3, 2.8) - t * 1.1));
    float flow = clamp(fbm(p + 2.5 * r), 0.0, 1.0);
    vec3 col = mix(uColorA, uColorB, smoothstep(0.12, 0.78, flow));
    col = mix(col, uColorC, smoothstep(0.74, 1.0, flow) * 0.5);
    float vig = smoothstep(1.2, 0.25, length((uv - 0.5) * vec2(aspect, 1.0)));
    col *= mix(0.82, 1.06, vig);
    col = mix(uVeil, col, uVeilAmount);
    col += (fhash(uv * uRes + fract(uTime)) - 0.5) / 255.0;
    outColor = vec4(col, 1.0);
}`;

        function fluidCompile(gl, type, src) {
            const sh = gl.createShader(type);
            gl.shaderSource(sh, src);
            gl.compileShader(sh);
            if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) { gl.deleteShader(sh); return null; }
            return sh;
        }

        // 建 program + uniform location（初始化与上下文恢复共用；恢复时必须重跑，否则对象的
        // GL 句柄已随丢失的上下文失效，继续使用只产生 INVALID_OPERATION）
        function fluidBuildProgram(gl) {
            const vs = fluidCompile(gl, gl.VERTEX_SHADER, FLUID_VS);
            const fs = fluidCompile(gl, gl.FRAGMENT_SHADER, FLUID_FS);
            if (!vs || !fs) return null;
            const prog = gl.createProgram();
            gl.attachShader(prog, vs);
            gl.attachShader(prog, fs);
            gl.linkProgram(prog);
            if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return null;
            return {prog: prog, loc: {
                uTime: gl.getUniformLocation(prog, 'uTime'),
                uRes: gl.getUniformLocation(prog, 'uRes'),
                uColorA: gl.getUniformLocation(prog, 'uColorA'),
                uColorB: gl.getUniformLocation(prog, 'uColorB'),
                uColorC: gl.getUniformLocation(prog, 'uColorC'),
                uVeil: gl.getUniformLocation(prog, 'uVeil'),
                uVeilAmount: gl.getUniformLocation(prog, 'uVeilAmount'),
            }};
        }

        // 配色刷新：由 initFluidBg 与 applySkin 调用；未初始化（无 WebGL2 / 初始化失败）时早退，
        // 防止首屏 applySkin 先于 initFluidBg 触发时抛错冒泡（阻断引导区后续初始化）。
        function applyFluidTokens() {
            if (!fluidGl) return;
            const t = fluidReadTokens(getComputedStyle(document.documentElement));
            const opacity = parseFloat(t.opacity);
            if (fluidEl && opacity >= 0 && opacity <= 1) fluidEl.style.opacity = String(opacity);
            const gl = fluidGl, L = fluidLoc;
            gl.useProgram(fluidProg);   // uniform location 仅在当前 program 上有效（否则 INVALID_OPERATION 静默不生效）
            const setCol = (loc, raw, fallback) => {
                const c = parseCssColor(raw) || fallback;
                gl.uniform3f(loc, c[0], c[1], c[2]);
            };
            setCol(L.uColorA, t.a, [0.02, 0.13, 0.24]);
            setCol(L.uColorB, t.b, [0.04, 0.31, 0.48]);
            setCol(L.uColorC, t.c, [0.13, 0.83, 0.93]);
            setCol(L.uVeil, t.veil, [0.02, 0.05, 0.09]);
            gl.uniform1f(L.uVeilAmount, FLUID_VEIL_AMOUNT);
            // 静态单帧模式（reduced-motion、循环已停）下切皮肤须立即重绘，否则 canvas 仍显示
            // 上一档配色；动画模式由下一帧自然刷新，无需在此重绘。
            if (!fluidAnimated) fluidRenderFrame(fluidTime);
        }

        function fluidResize() {
            if (!fluidGl || !fluidEl) return;
            const size = fluidRenderSize(window.innerWidth, window.innerHeight,
                                         window.devicePixelRatio || 1, FLUID_MAX_DPR, FLUID_MAX_EDGE);
            if (fluidEl.width !== size.w || fluidEl.height !== size.h) {
                fluidEl.width = size.w;
                fluidEl.height = size.h;
            }
            fluidGl.viewport(0, 0, size.w, size.h);
        }

        function fluidRenderFrame(t) {
            if (!fluidGl) return;
            try {
                const gl = fluidGl;
                gl.useProgram(fluidProg);
                gl.uniform1f(fluidLoc.uTime, t);
                gl.uniform2f(fluidLoc.uRes, fluidEl.width || 1, fluidEl.height || 1);
                gl.drawArrays(gl.TRIANGLES, 0, 3);
            } catch (e) { /* 上下文丢失等：静默保留静态渐变底 */ }
        }

        function fluidLoop(ts) {
            fluidRaf = 0;
            if (!fluidGl || fluidPaused) return;
            const now = (typeof ts === 'number') ? ts : performance.now();
            if (fluidLast) fluidTime += Math.min(Math.max(now - fluidLast, 0), 100) / 1000;
            fluidLast = now;
            fluidRenderFrame(fluidTime);
            fluidRaf = requestAnimationFrame(fluidLoop);
        }

        // 页签隐藏/回前台暂停与恢复（仅动画模式生效；静态单帧模式保持静止）
        function fluidSetPaused(paused) {
            if (!fluidAnimated) return;
            if (paused) {
                fluidPaused = true;
                if (fluidRaf) { cancelAnimationFrame(fluidRaf); fluidRaf = 0; }
            } else if (fluidPaused) {
                fluidPaused = false;
                fluidLast = 0;
                fluidRaf = requestAnimationFrame(fluidLoop);
            }
        }

        // 初始化：reduced-motion 渲染单帧静态流体；WebGL2 不可用/编译失败/异常 → 令牌化静态渐变底，
        // 全程无控制台输出。上下文丢失 → preventDefault + 停循环（丢失态 canvas 无图像，露出其
        // background-image 的静态渐变底）；恢复 → 经 fluidBuildProgram 重建 program/location 后继续。
        function initFluidBg() {
            fluidEl = document.getElementById('fluidBg');
            if (!fluidEl) return;
            let reduced = false;
            try {
                reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
            } catch (e) { reduced = false; }
            // 默认静态：只渲染一帧流体当底纹（不占 GPU/CPU、不分散注意力）；
            // localStorage reqBoardFluid=on 时才启用动态流体（设置 → 全局信息里切换）
            try { if (localStorage.getItem('reqBoardFluid') !== 'on') reduced = true; } catch (e) { reduced = true; }
            try {
                // preserveDrawingBuffer 仅静态单帧模式（reduced-motion）需要：该模式不跑循环，
                // 单帧内容须在合成后仍可见；动画模式每帧重绘，保持默认走合成快路径。
                const gl = fluidEl.getContext('webgl2', {
                    antialias: false, alpha: false, depth: false, stencil: false,
                    preserveDrawingBuffer: reduced});
                if (!gl) return;   // WebGL2 不可用 → 静态渐变底
                const built = fluidBuildProgram(gl);
                if (!built) return;   // 着色器编译 / program 链接失败 → 静态渐变底
                fluidGl = gl;
                fluidProg = built.prog;
                fluidLoc = built.loc;
                fluidEl.addEventListener('webglcontextlost', (ev) => {
                    ev.preventDefault();
                    fluidPaused = true;                       // 停循环，露出令牌化渐变底
                    if (fluidRaf) { cancelAnimationFrame(fluidRaf); fluidRaf = 0; }
                });
                fluidEl.addEventListener('webglcontextrestored', () => {
                    try {
                        const again = fluidBuildProgram(fluidGl);   // 上下文丢失后旧 GL 对象已失效，必须重建
                        if (!again) return;                        // 重建失败：停在静态渐变底
                        fluidProg = again.prog;
                        fluidLoc = again.loc;
                        fluidResize();
                        applyFluidTokens();
                        if (fluidAnimated) { fluidPaused = true; fluidSetPaused(false); }
                        else fluidRenderFrame(fluidTime);
                    } catch (e) { /* 保持回退态，不产生控制台报错 */ }
                });
                fluidResize();
                applyFluidTokens();
                window.addEventListener('resize', () => {
                    fluidResize();
                    if (!fluidAnimated) fluidRenderFrame(fluidTime);   // 静态单帧随尺寸重绘
                });
                if (reduced) { fluidRenderFrame(fluidTime); return; }  // 停用动画：单帧静态流体
                fluidAnimated = true;
                fluidPaused = false;
                fluidLast = 0;
                fluidRaf = requestAnimationFrame(fluidLoop);
            } catch (e) {
                fluidGl = null;   // 初始化失败：保持静态渐变底，不产生控制台报错
            }
        }


