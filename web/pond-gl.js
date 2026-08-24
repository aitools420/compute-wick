/* The pond, for real this time. WebGL1, two passes:
   1) scene FBO: frog (alpha cutout) + one glowing mote per live offer
   2) screen: gradient night, scene composited above the waterline, and below it
      the scene + headline reflected through rippling water that answers the cursor.
   Falls back to the 2D canvas path when WebGL is unavailable. */
(function () {
  "use strict";

  const STOCK_TOP = [0.047, 0.067, 0.055];
  const STOCK_MID = [0.031, 0.051, 0.043];
  const WATER_DEEP = [0.016, 0.031, 0.027];
  const AMBER = [0.941, 0.659, 0.408];
  const PHOS = [0.498, 0.851, 0.604];
  const HORIZON_FRAC = 0.62;
  const MAX_RIPPLES = 10;

  function compile(gl, type, src) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
      throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  function program(gl, vs, fs) {
    const p = gl.createProgram();
    gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs));
    gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS))
      throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  const SPRITE_VS = `
attribute vec4 a_data;   /* x, y, size, phase */
attribute float a_idle;
uniform vec2 u_res;
uniform float u_time;
varying float v_idle;
varying float v_glow;
void main(){
  v_idle = a_idle;
  v_glow = 0.55 + 0.45 * sin(u_time * 0.0011 + a_data.w);
  vec2 clip = (a_data.xy / u_res) * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  gl_PointSize = a_data.z * (0.75 + v_glow * 0.6);
}`;

  const SPRITE_FS = `
precision mediump float;
varying float v_idle;
varying float v_glow;
void main(){
  float d = length(gl_PointCoord - 0.5) * 2.0;
  float fall = pow(max(0.0, 1.0 - d), 2.4);
  vec3 c = mix(vec3(${AMBER.join(",")}), vec3(${PHOS.join(",")}), v_idle);
  gl_FragColor = vec4(c * fall * (0.30 + v_glow * 0.75), 0.0);
}`;

  const QUAD_VS = `
attribute vec2 a_pos;    /* pixel coords */
attribute vec2 a_uv;
uniform vec2 u_res;
varying vec2 v_uv;
void main(){
  vec2 clip = (a_pos / u_res) * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  v_uv = a_uv;
}`;

  const QUAD_FS = `
precision mediump float;
uniform sampler2D u_tex;
uniform float u_alpha;
varying vec2 v_uv;
void main(){
  vec4 t = texture2D(u_tex, v_uv);
  float a = t.a < 0.05 ? 0.0 : t.a;
  a *= smoothstep(0.0, 0.14, v_uv.x);      /* branch dissolves leftward */
  a *= 1.0 - smoothstep(0.92, 1.0, v_uv.y); /* soften the crop bottom */
  gl_FragColor = vec4(t.rgb * a, a) * u_alpha;
}`;

  const SCREEN_VS = `
attribute vec2 a_pos;
varying vec2 v_uv;      /* y = 0 at TOP */
void main(){
  gl_Position = vec4(a_pos, 0.0, 1.0);
  v_uv = vec2(a_pos.x * 0.5 + 0.5, 0.5 - a_pos.y * 0.5);
}`;

  const SCREEN_FS = `
precision highp float;
uniform sampler2D u_scene;
uniform sampler2D u_text;
uniform vec2 u_res;
uniform float u_time;
uniform float u_horizon;             /* fraction of height, from top */
uniform vec4 u_ripples[${MAX_RIPPLES}];  /* x(px), y(px), startMs, amp */
varying vec2 v_uv;

vec3 bgcol(float y){
  vec3 top = vec3(${STOCK_TOP.join(",")});
  vec3 mid = vec3(${STOCK_MID.join(",")});
  vec3 deep = vec3(${WATER_DEEP.join(",")});
  if (y < u_horizon) return mix(top, mid, y / u_horizon);
  return mix(mid, deep, (y - u_horizon) / (1.0 - u_horizon));
}

vec4 sceneAt(sampler2D t, vec2 p){ return texture2D(t, vec2(p.x, 1.0 - p.y)); }

void main(){
  vec2 uv = v_uv;
  vec3 color = bgcol(uv.y);

  if (uv.y < u_horizon){
    vec4 s = sceneAt(u_scene, uv);
    color = color * (1.0 - s.a) + s.rgb;
  } else {
    float depth = (uv.y - u_horizon) / max(0.001, 1.0 - u_horizon);
    vec2 px = uv * u_res;

    /* ripple field: rings from cursor + firefly touches */
    float dxr = 0.0;
    float sparkle = 0.0;
    for (int i = 0; i < ${MAX_RIPPLES}; i++){
      vec4 r = u_ripples[i];
      if (r.w <= 0.0) continue;
      float age = (u_time - r.z) / 1000.0;
      if (age < 0.0 || age > 3.0) continue;
      float rad = age * 150.0;
      float d = distance(px, r.xy);
      float ring = exp(-pow(d - rad, 2.0) / 260.0) * r.w * exp(-age * 1.6);
      dxr += ring * 9.0 * sign(px.x - r.x + 0.001);
      sparkle += ring * 0.06;
    }

    /* ambient waves grow with depth */
    float wob = ( sin(uv.y * 95.0 + u_time * 0.0012)
                + sin(uv.y * 41.0 - u_time * 0.0007)
                + sin((uv.y * 63.0 + uv.x * 18.0) + u_time * 0.0009) )
              * (0.0012 + depth * 0.0045);

    float ry = u_horizon - (uv.y - u_horizon) * 1.45;
    if (ry > 0.0){
      vec2 ruv = vec2(uv.x + wob + dxr / u_res.x, ry - wob * 0.4);
      float blur = (1.0 + depth * 3.0) / u_res.x;
      vec4 s = sceneAt(u_scene, ruv)
             + sceneAt(u_scene, ruv + vec2(blur, 0.0))
             + sceneAt(u_scene, ruv - vec2(blur, 0.0));
      vec4 tx = sceneAt(u_text, ruv)
              + sceneAt(u_text, ruv + vec2(blur * 0.6, 0.0))
              + sceneAt(u_text, ruv - vec2(blur * 0.6, 0.0));
      vec4 refl = (s + tx * 0.72) / 3.0;
      float strength = 0.42 * (1.0 - depth * 0.72);
      color += refl.rgb * strength;
      color += vec3(0.75, 0.85, 0.8) * sparkle;
    }

    /* the waterline: thin specular band with a slow shimmer */
    float band = exp(-abs(uv.y - u_horizon) * u_res.y * 0.55);
    float shimmer = 0.55 + 0.45 * sin(uv.x * 60.0 + u_time * 0.0016);
    color += vec3(0.62, 0.68, 0.64) * band * 0.22 * shimmer;
  }
  gl_FragColor = vec4(color, 1.0);
}`;

  function start(canvas, opts) {
    const gl = canvas.getContext("webgl", {
      alpha: false, antialias: false, powerPreference: "low-power", preserveDrawingBuffer: true,
    });
    if (!gl) return null;

    const hero = opts.heroEl;
    const dprCap = Math.min(devicePixelRatio || 1, 1.75);
    let W = 0, H = 0, HOR = 0;

    /* ---------- programs ---------- */
    const pSprite = program(gl, SPRITE_VS, SPRITE_FS);
    const pQuad = program(gl, QUAD_VS, QUAD_FS);
    const pScreen = program(gl, SCREEN_VS, SCREEN_FS);

    /* ---------- scene FBO + text texture ---------- */
    const sceneTex = gl.createTexture();
    const fbo = gl.createFramebuffer();
    const textTex = gl.createTexture();
    function setupTex(t, w, h) {
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }

    /* ---------- frog texture ---------- */
    const frogTex = gl.createTexture();
    let frogReady = false, frogAspect = 1.54;
    const frogImg = new Image();
    frogImg.onload = () => {
      frogAspect = frogImg.width / frogImg.height;
      gl.bindTexture(gl.TEXTURE_2D, frogTex);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, frogImg);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      frogReady = true;
    };
    frogImg.src = opts.frogURL;

    /* ---------- headline texture (drawn only in the reflection) ---------- */
    const textCanvas = document.createElement("canvas");
    function paintText() {
      const scale = canvas.width / Math.max(1, W);
      textCanvas.width = canvas.width;
      textCanvas.height = canvas.height;
      const c = textCanvas.getContext("2d");
      c.clearRect(0, 0, textCanvas.width, textCanvas.height);
      const heroBox = hero.getBoundingClientRect();
      for (const el of opts.textEls) {
        const nodes = [];
        (function walk(n) {
          for (const ch of n.childNodes) {
            if (ch.nodeType === 3 && ch.textContent.trim()) nodes.push(ch);
            else if (ch.nodeType === 1) walk(ch);
          }
        })(el);
        for (const node of nodes) {
          const parent = node.parentElement;
          const st = getComputedStyle(parent);
          const range = document.createRange();
          const words = node.textContent.split(/(\s+)/);
          let idx = 0;
          for (const w of words) {
            if (w.trim()) {
              range.setStart(node, idx);
              range.setEnd(node, idx + w.length);
              const r = range.getBoundingClientRect();
              c.font = `${st.fontWeight} ${parseFloat(st.fontSize) * scale}px ${st.fontFamily}`;
              c.fillStyle = st.color;
              c.textBaseline = "top";
              c.fillText(w, (r.left - heroBox.left) * scale, (r.top - heroBox.top) * scale);
            }
            idx += w.length;
          }
        }
      }
      gl.bindTexture(gl.TEXTURE_2D, textTex);
      gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, textCanvas);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    }

    /* ---------- motes ---------- */
    const per = Math.max(1, Math.ceil(opts.total / 600));
    const n = Math.max(40, Math.round(opts.total / per));
    const motes = [];
    const moteData = new Float32Array(n * 5);
    const spriteBuf = gl.createBuffer();

    function seedMotes() {
      motes.length = 0;
      const TOP = 110;
      for (let i = 0; i < n; i++) {
        motes.push({
          x: Math.random() * W,
          y: HOR - 6 - Math.pow(Math.random(), 2.3) * (HOR - TOP - 6),
          a: Math.random() * Math.PI * 2,
          v: 0.06 + Math.random() * 0.15,
          idle: Math.random() < opts.idleShare ? 1 : 0,
          ph: Math.random() * Math.PI * 2,
          r: (5 + Math.random() * 7),
        });
      }
    }

    /* ---------- ripples ---------- */
    const ripples = new Float32Array(MAX_RIPPLES * 4);
    let rippleIdx = 0;
    function addRipple(x, y, amp) {
      const o = rippleIdx * 4;
      ripples[o] = x * dprCap; ripples[o + 1] = y * dprCap;
      ripples[o + 2] = lastT; ripples[o + 3] = amp;
      rippleIdx = (rippleIdx + 1) % MAX_RIPPLES;
    }
    hero.addEventListener("pointermove", (e) => {
      const box = hero.getBoundingClientRect();
      const y = e.clientY - box.top;
      if (y > HOR && Math.random() < 0.35) addRipple(e.clientX - box.left, y, 0.7);
    }, { passive: true });
    hero.addEventListener("pointerdown", (e) => {
      const box = hero.getBoundingClientRect();
      const y = e.clientY - box.top;
      if (y > HOR) addRipple(e.clientX - box.left, y, 1.6);
    }, { passive: true });

    /* ---------- static geometry ---------- */
    const screenBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, screenBuf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const frogBuf = gl.createBuffer();

    function size() {
      W = hero.clientWidth; H = hero.clientHeight;
      /* waterline sits just under the hero text block, clamped to sane bounds */
      const wrap = hero.querySelector(".wrap");
      const wb = wrap.getBoundingClientRect(), hb = hero.getBoundingClientRect();
      HOR = Math.min(H * 0.80, Math.max(H * HORIZON_FRAC, wb.bottom - hb.top + 16));
      canvas.width = Math.round(W * dprCap);
      canvas.height = Math.round(H * dprCap);
      setupTex(sceneTex, canvas.width, canvas.height);
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, sceneTex, 0);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
      seedMotes();
      if (document.fonts && document.fonts.status === "loaded") paintText();
    }
    size();
    if (document.fonts) document.fonts.ready.then(paintText);
    addEventListener("resize", size);

    /* ---------- frame ---------- */
    let lastT = 0;
    let ambientNext = 1500;

    function step(t) {
      lastT = t;
      for (const m of motes) {
        m.a += (Math.random() - 0.5) * 0.12;
        m.x += Math.cos(m.a) * m.v;
        m.y += Math.sin(m.a) * m.v * 0.55;
        if (m.x < -10) m.x = W + 10;
        if (m.x > W + 10) m.x = -10;
        if (m.y < 110) { m.y = 110; m.a = -m.a; }
        if (m.y > HOR - 3) {
          m.y = HOR - 3; m.a = -m.a;
          if (Math.random() < 0.25) addRipple(m.x, HOR + 3, 0.5);
        }
      }
      if (t > ambientNext) {
        addRipple(Math.random() * W, HOR + 10 + Math.random() * (H - HOR) * 0.6, 0.35);
        ambientNext = t + 1800 + Math.random() * 2600;
      }
    }

    function drawScene(t) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND);

      if (frogReady) {
        const fh = Math.min(H * 0.42, (W * 0.62) / frogAspect);
        const fw = fh * frogAspect;
        const x1 = W - fw * 0.98, y2 = HOR + fh * 0.055;
        const y1 = y2 - fh, x2 = x1 + fw;
        gl.useProgram(pQuad);
        gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
        gl.bindBuffer(gl.ARRAY_BUFFER, frogBuf);
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
          x1, y1, 0, 0,  x2, y1, 1, 0,  x1, y2, 0, 1,
          x2, y1, 1, 0,  x2, y2, 1, 1,  x1, y2, 0, 1,
        ]), gl.DYNAMIC_DRAW);
        const aP = gl.getAttribLocation(pQuad, "a_pos");
        const aU = gl.getAttribLocation(pQuad, "a_uv");
        gl.enableVertexAttribArray(aP); gl.vertexAttribPointer(aP, 2, gl.FLOAT, false, 16, 0);
        gl.enableVertexAttribArray(aU); gl.vertexAttribPointer(aU, 2, gl.FLOAT, false, 16, 8);
        gl.uniform2f(gl.getUniformLocation(pQuad, "u_res"), W, H);
        gl.uniform1f(gl.getUniformLocation(pQuad, "u_alpha"), 1.0);
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, frogTex);
        gl.uniform1i(gl.getUniformLocation(pQuad, "u_tex"), 0);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
      }

      /* fireflies, additive */
      for (let i = 0; i < motes.length; i++) {
        const m = motes[i], o = i * 5;
        moteData[o] = m.x; moteData[o + 1] = m.y;
        moteData[o + 2] = m.r * dprCap; moteData[o + 3] = m.ph;
        moteData[o + 4] = m.idle;
      }
      gl.useProgram(pSprite);
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.bindBuffer(gl.ARRAY_BUFFER, spriteBuf);
      gl.bufferData(gl.ARRAY_BUFFER, moteData, gl.DYNAMIC_DRAW);
      const aD = gl.getAttribLocation(pSprite, "a_data");
      const aI = gl.getAttribLocation(pSprite, "a_idle");
      gl.enableVertexAttribArray(aD); gl.vertexAttribPointer(aD, 4, gl.FLOAT, false, 20, 0);
      gl.enableVertexAttribArray(aI); gl.vertexAttribPointer(aI, 1, gl.FLOAT, false, 20, 16);
      gl.uniform2f(gl.getUniformLocation(pSprite, "u_res"), W, H);
      gl.uniform1f(gl.getUniformLocation(pSprite, "u_time"), t);
      gl.drawArrays(gl.POINTS, 0, motes.length);
      gl.bindFramebuffer(gl.FRAMEBUFFER, null);
    }

    function drawScreen(t) {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.disable(gl.BLEND);
      gl.useProgram(pScreen);
      gl.bindBuffer(gl.ARRAY_BUFFER, screenBuf);
      const aP = gl.getAttribLocation(pScreen, "a_pos");
      gl.enableVertexAttribArray(aP);
      gl.vertexAttribPointer(aP, 2, gl.FLOAT, false, 0, 0);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, sceneTex);
      gl.uniform1i(gl.getUniformLocation(pScreen, "u_scene"), 0);
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, textTex);
      gl.uniform1i(gl.getUniformLocation(pScreen, "u_text"), 1);
      gl.uniform2f(gl.getUniformLocation(pScreen, "u_res"), canvas.width, canvas.height);
      gl.uniform1f(gl.getUniformLocation(pScreen, "u_time"), t);
      gl.uniform1f(gl.getUniformLocation(pScreen, "u_horizon"), HOR / H);
      gl.uniform4fv(gl.getUniformLocation(pScreen, "u_ripples"), ripples);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    }

    let running = true, raf = null;
    window.__pondFrames = 0;
    function loop(t) {
      if (!running) return;
      step(t);
      drawScene(t);
      drawScreen(t);
      window.__pondFrames++;
      raf = requestAnimationFrame(loop);
    }
    raf = requestAnimationFrame(loop);

    new IntersectionObserver((en) => {
      const vis = en[0].isIntersecting;
      if (vis && !running) { running = true; raf = requestAnimationFrame(loop); }
      if (!vis) { running = false; if (raf) cancelAnimationFrame(raf); }
    }).observe(canvas);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { running = false; if (raf) cancelAnimationFrame(raf); }
      else if (canvas.getBoundingClientRect().bottom > 0) { running = true; raf = requestAnimationFrame(loop); }
    });

    return { kind: "webgl", per: per };
  }

  window.PondGL = { start };
})();
