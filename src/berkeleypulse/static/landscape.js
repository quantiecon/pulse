(function () {
  var field = document.getElementById("field");
  var canvas = document.getElementById("field-canvas");
  if (!field || !canvas) return;
  var ctx = canvas.getContext("2d", { alpha: false });
  if (!ctx) return;

  var TAU = Math.PI * 2;
  var RIPPLE = 2.2;
  var background = "#000000";
  var ramp = colorRamp(["#CFD5DF", "#F4F5F6", "#CFD5DF"]);
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var fine = window.matchMedia("(pointer: fine)");
  var paused = reduce.matches;
  var manual = false;
  var w = 0;
  var h = 0;
  var frame = 0;
  var previous = 0;
  var elapsed = 0;
  var visible = true;
  var pointer = { x: 0.5, y: 0.62, strength: 0 };
  var target = { x: 0.5, y: 0.62, strength: 0 };
  var ripples = [];
  var landing = document.body && document.body.classList.contains("gate");
  var stars = buildStars();
  var galaxy = buildGalaxy();
  var toggle = document.getElementById("field-motion");

  function colorRamp(colors) {
    var stops = colors.map(function (hex) {
      return [1, 3, 5].map(function (index) { return parseInt(hex.slice(index, index + 2), 16); });
    });
    var values = [];
    for (var i = 0; i < 256; i++) {
      var position = (i / 255) * (stops.length - 1);
      var left = Math.min(stops.length - 2, Math.floor(position));
      var mix = position - left;
      values.push(stops[left].map(function (value, channel) {
        return Math.round(value + (stops[left + 1][channel] - value) * mix);
      }).join(","));
    }
    return values;
  }

  function unit(index) {
    var value = Math.sin(index * 127.1) * 43758.5453;
    return value - Math.floor(value);
  }

  function buildStars() {
    var points = [];
    var count = landing ? 1400 : 920;
    for (var index = 0; index < count; index++) {
      var faint = unit(index + 31) > 0.42;
      var roll = unit(index + 19);
      points.push({
        x: 0.008 + unit(index) * 0.984,
        y: landing ? 0.004 + Math.pow(roll, 1.75) * 0.64 : 0.012 + roll * 0.64,
        radius: unit(index + 8) > 0.97 ? 1.35 : faint ? 0.42 : 0.55 + unit(index + 4) * 0.4,
        alpha: faint ? 0.16 + unit(index + 2) * 0.28 : 0.48 + unit(index + 2) * 0.46,
        phase: unit(index + 6) * TAU,
        twinkle: !faint && unit(index + 11) > 0.62
      });
    }
    for (var hidden = 0; hidden < 42; hidden++) {
      for (var layer = 0; layer < 3; layer++) {
        points.push({
          x: (hidden + 0.5) / 42 + (unit(hidden * 3 + layer) - 0.5) * 0.01,
          y: 1,
          tuck: 0.007 + layer * 0.009,
          radius: 0.65 + unit(hidden + layer + 5) * 0.35,
          alpha: 0.72 + unit(hidden + layer + 9) * 0.28,
          phase: unit(hidden + layer + 2) * TAU,
          twinkle: true
        });
      }
    }
    return points;
  }

  function buildGalaxy() {
    var arms = [];
    var index;
    for (index = 0; index < 90; index++) {
      var angle = unit(index) * TAU;
      var radius = Math.pow(unit(index + 3), 0.62) * 0.28;
      arms.push({
        angle: angle,
        radius: radius,
        color: "255,226,196",
        alpha: 0.35 + unit(index + 5) * 0.55,
        size: 0.55 + unit(index + 7) * 0.9
      });
    }
    for (index = 0; index < 420; index++) {
      var arm = index % 2;
      var travel = (index % 210) / 210;
      var radius = 0.1 + travel * 0.9;
      var dust = unit(index + 13) > 0.84;
      var blue = unit(index + 17) > 0.4;
      arms.push({
        angle: arm * Math.PI + radius * 5.4 + (unit(index + 9) - 0.5) * 0.42,
        radius: radius,
        color: dust ? "186,124,72" : blue ? "156,196,255" : "255,244,228",
        alpha: 0.28 + unit(index + 2) * 0.62,
        size: unit(index + 4) > 0.93 ? 1.55 : 0.45 + unit(index + 6) * 0.55
      });
    }
    return arms;
  }

  function galaxyFrame() {
    return {
      cx: w * 0.8,
      cy: h * 0.14,
      scale: Math.min(w, h) * 0.28,
      tilt: -0.42
    };
  }

  function insideGalaxy(x, y) {
    var frame = galaxyFrame();
    var cos = Math.cos(frame.tilt);
    var sin = Math.sin(frame.tilt);
    var ox = x - frame.cx;
    var oy = y - frame.cy;
    var lx = (ox * cos + oy * sin) / frame.scale;
    var ly = (-ox * sin + oy * cos) / frame.scale;
    return lx * lx + (ly / 0.4) * (ly / 0.4) < 1.35;
  }

  function relief(u, z, t) {
    var flowU = u + Math.sin(z * 5.8 + u * 4.2 - t * 0.28) * 0.045;
    var flowZ = z + Math.sin(u * 5.3 - z * 2 + t * 0.21) * 0.035;
    var ridgeCenter = 0.03 + flowZ * 0.55 + Math.sin(flowZ * 5.4 - t * 0.34) * 0.045;
    var ridgeWidth = 0.21 + z * 0.1;
    var across = (flowU - ridgeCenter) / ridgeWidth;
    var ridge = Math.exp(-across * across);
    var elevation = (0.15 + Math.sin(flowZ * 4 + t * 0.3) * 0.012) * Math.exp(-Math.pow((flowZ - 0.25) / 0.65, 2));
    var shoulderCenter = 1.02 - flowZ * 0.12 + Math.sin(flowZ * 4 + t * 0.25) * 0.05;
    var shoulder = Math.exp(-Math.pow((flowU - shoulderCenter) / (0.35 + z * 0.1), 2));
    var shoulderElevation = 0.1 * Math.exp(-Math.pow((flowZ - 0.38) / 0.8, 2));
    var swellPhase = flowU * 6.2 - flowZ * 10 + t * 0.55;
    var crossPhase = flowU * 10.5 + flowZ * 6.5 - t * 0.38;
    var swell = (Math.sin(swellPhase) * 0.032 + Math.sin(crossPhase) * 0.018) * (0.45 + Math.min(z, 1) * 0.55);
    return {
      y: h * (0.7 + flowZ * flowZ * 0.32 - ridge * elevation - shoulder * shoulderElevation + swell),
      ridge: ridge,
      shoulder: shoulder,
      swellPhase: swellPhase
    };
  }

  function activeWaves(t) {
    var fieldScale = Math.max(w, h * 0.8);
    return ripples.map(function (ripple) {
      var age = t - ripple.startedAt;
      var progress = Math.min(1, age / RIPPLE);
      return {
        x: ripple.x * w,
        y: ripple.y * h,
        radius: age * fieldScale * 0.38,
        width: Math.max(18, fieldScale * 0.028) * (1 + progress * 0.7),
        strength: Math.sin(Math.min(1, age / 0.12) * Math.PI / 2) * Math.pow(1 - progress, 1.5)
      };
    });
  }

  function shiftY(x, y, z, waves, px, py, strength) {
    var displacement = 0;
    var rippleLight = 0;
    for (var waveIndex = 0; waveIndex < waves.length; waveIndex++) {
      var wave = waves[waveIndex];
      var waveDistance = Math.hypot(x - wave.x, (y - wave.y) * 1.65);
      var band = (waveDistance - wave.radius) / wave.width;
      if (Math.abs(band) > 3.5) continue;
      var envelope = Math.exp(-band * band * 0.75) * wave.strength;
      displacement += Math.cos(band * 1.6) * envelope;
      rippleLight += Math.exp(-band * band * 1.5) * wave.strength;
    }
    y -= Math.tanh(displacement) * h * 0.022 * (0.45 + Math.min(z, 1) * 0.55);
    var dx = x - px * w;
    var dy = y - py * h;
    var radius = h * 0.31;
    var influence = Math.exp(-(dx * dx + dy * dy) / (radius * radius)) * strength;
    y -= influence * h * 0.024 * (0.25 + z * 0.75);
    return { y: y, dx: dx, influence: influence, rippleLight: rippleLight };
  }

  function buildCrest(t, px, py, strength, waves) {
    var cols = 96;
    var crest = [];
    var column;
    var waveList = waves || activeWaves(t);
    for (column = 0; column < cols; column++) crest.push(h + 40);
    for (var row = 0; row < 40; row++) {
      var z = row / 52;
      for (column = 0; column < cols; column++) {
        var u = column / (cols - 1);
        var point = relief(u, z, t);
        var drift = Math.sin(z * 7.4 + u * 3.5 - t * 0.32);
        var x = u * w + drift * w * 0.045 * (0.25 + z * 0.65);
        var shifted = shiftY(x, point.y, z, waveList, px, py, strength);
        var bin = Math.max(0, Math.min(cols - 1, Math.round((x / w) * (cols - 1))));
        if (shifted.y < crest[bin]) crest[bin] = shifted.y;
      }
    }
    for (column = 0; column < cols; column++) {
      if (crest[column] <= h) continue;
      var left = column > 0 ? crest[column - 1] : h;
      var right = column < cols - 1 ? crest[column + 1] : h;
      crest[column] = Math.min(left, right);
    }
    return crest;
  }

  function drawGalaxy(t) {
    var frame = galaxyFrame();
    var cx = frame.cx;
    var cy = frame.cy;
    var scale = frame.scale;
    var spin = t * 0.02;
    var tilt = frame.tilt;
    var cos = Math.cos(tilt);
    var sin = Math.sin(tilt);
    var halo = ctx.createRadialGradient(cx, cy, 0, cx, cy, scale * 0.62);
    halo.addColorStop(0, "rgba(255,214,170,0.2)");
    halo.addColorStop(0.28, "rgba(140,170,230,0.07)");
    halo.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = halo;
    ctx.beginPath();
    ctx.arc(cx, cy, scale * 0.62, 0, TAU);
    ctx.fill();
    for (var index = 0; index < galaxy.length; index++) {
      var star = galaxy[index];
      var angle = star.angle + spin;
      var dx = Math.cos(angle) * star.radius;
      var dy = Math.sin(angle) * star.radius * 0.4;
      var x = cx + (dx * cos - dy * sin) * scale;
      var y = cy + (dx * sin + dy * cos) * scale;
      ctx.fillStyle = "rgba(" + star.color + "," + star.alpha + ")";
      ctx.beginPath();
      ctx.arc(x, y, star.size, 0, TAU);
      ctx.fill();
    }
    var core = ctx.createRadialGradient(cx, cy, 0, cx, cy, scale * 0.08);
    core.addColorStop(0, "rgba(255,248,236,0.95)");
    core.addColorStop(0.45, "rgba(255,196,140,0.45)");
    core.addColorStop(1, "rgba(255,196,140,0)");
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(cx, cy, scale * 0.08, 0, TAU);
    ctx.fill();
  }

  function planeMask(t, px, py, strength) {
    var cell = 5;
    var cols = Math.ceil(w / cell);
    var gridRows = Math.ceil(h / cell);
    var mask = new Uint8Array(cols * gridRows);
    var depthSteps = 52;
    var rows = 67;
    var overscan = Math.max(64, w * 0.15);
    var pointScale = Math.min(1.1, Math.max(0.78, w / 1200));
    var fieldScale = Math.max(w, h * 0.8);
    var waves = activeWaves(t);
    for (var row = 0; row < rows; row++) {
      var z = row / depthSteps;
      var depth = 0.13 + z * z * 1.35;
      var spacing = Math.max(1.8, fieldScale * 0.019 * depth);
      var halfColumns = Math.ceil((w * 0.5 + overscan) / spacing);
      var horizonFade = 1 - Math.exp(-z * 14);
      for (var horizontal = -halfColumns; horizontal <= halfColumns; horizontal++) {
        var grainPhase = horizontal * 2.399 + row * 3.17;
        var baseX = w * 0.5 + (horizontal + (row % 2) * 0.5 + Math.sin(grainPhase) * 0.16) * spacing;
        var u = baseX / w;
        var drift = Math.sin(z * 7.4 + u * 3.5 - t * 0.32);
        var x = baseX + drift * w * 0.045 * (0.25 + z * 0.65);
        var shape = relief(u, z, t);
        var shifted = shiftY(x, shape.y, z, waves, px, py, strength);
        var y = shifted.y;
        x += shifted.dx * shifted.influence * 0.026;
        if (x < -10 || x > w + 10 || y > h + 10) continue;
        var foldLight = Math.pow(0.5 + 0.5 * Math.cos(shape.swellPhase + 0.7), 3);
        var grain = 0.88 + Math.sin(grainPhase) * 0.12;
        var brightness = Math.min(0.95, 0.09 + z * 0.1 + shape.ridge * 0.25 + shape.shoulder * 0.18 + foldLight * 0.53 + shifted.influence * 0.15 + shifted.rippleLight * 0.5) * horizonFade * grain;
        if (brightness < 0.16) continue;
        var cx = Math.floor(x / cell);
        var cy = Math.floor(y / cell);
        if (cx >= 0 && cy >= 0 && cx < cols && cy < gridRows) mask[cy * cols + cx] = 1;
      }
    }
    return { mask: mask, cols: cols, cell: cell };
  }

  function drawStars(t) {
    for (var index = 0; index < stars.length; index++) {
      var star = stars[index];
      if (star.tuck) continue;
      var x = star.x * w;
      var y = star.y * h;
      if (insideGalaxy(x, y)) continue;
      var flicker = star.twinkle ? 0.72 + 0.28 * Math.sin(t * 0.7 + star.phase) : 1;
      ctx.fillStyle = "rgba(255,255,255," + (star.alpha * flicker) + ")";
      ctx.beginPath();
      ctx.arc(x, y, star.radius, 0, TAU);
      ctx.fill();
    }
  }

  function haze(x, y, radius, color) {
    var light = ctx.createRadialGradient(w * x, h * y, 0, w * x, h * y, Math.max(w, h) * radius);
    light.addColorStop(0, color);
    light.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = light;
    ctx.fillRect(0, 0, w, h);
  }

  function drawSignal(t, px, py, strength) {
    haze(0.17, 0.64, 0.38, "rgba(190,190,190,0.055)");
    haze(0.96, 0.77, 0.37, "rgba(190,190,190,0.025)");
    var depthSteps = 52;
    var rows = 67;
    var overscan = Math.max(64, w * 0.15);
    var pointScale = Math.min(1.1, Math.max(0.78, w / 1200));
    var fieldScale = Math.max(w, h * 0.8);
    var waves = activeWaves(t);

    for (var row = 0; row < rows; row++) {
      var z = row / depthSteps;
      var depth = 0.13 + z * z * 1.35;
      var spacing = Math.max(1.8, fieldScale * 0.019 * depth);
      var halfColumns = Math.ceil((w * 0.5 + overscan) / spacing);
      var horizonFade = 1 - Math.exp(-z * 14);
      for (var horizontal = -halfColumns; horizontal <= halfColumns; horizontal++) {
        var grainPhase = horizontal * 2.399 + row * 3.17;
        var baseX = w * 0.5 + (horizontal + (row % 2) * 0.5 + Math.sin(grainPhase) * 0.16) * spacing;
        var u = baseX / w;
        var drift = Math.sin(z * 7.4 + u * 3.5 - t * 0.32);
        var x = baseX + drift * w * 0.045 * (0.25 + z * 0.65);
        var shape = relief(u, z, t);
        var y = shape.y;
        var shifted = shiftY(x, y, z, waves, px, py, strength);
        y = shifted.y;
        var influence = shifted.influence;
        var rippleLight = shifted.rippleLight;
        x += shifted.dx * influence * 0.026;
        if (x < -10 || x > w + 10 || y > h + 10) continue;
        var foldLight = Math.pow(0.5 + 0.5 * Math.cos(shape.swellPhase + 0.7), 3);
        var grain = 0.88 + Math.sin(grainPhase) * 0.12;
        var light = Math.min(1, shape.ridge * 0.7 + shape.shoulder * 0.4 + foldLight * 0.6 + rippleLight * 0.7);
        var brightness = Math.min(0.95, 0.09 + z * 0.1 + shape.ridge * 0.25 + shape.shoulder * 0.18 + foldLight * 0.53 + influence * 0.15 + rippleLight * 0.5) * horizonFade * grain;
        var point = (0.38 + z * 0.83 + light * 0.15) * pointScale * (0.92 + grain * 0.08);
        ctx.fillStyle = "rgba(" + ramp[Math.max(0, Math.min(255, Math.round(u * 255)))] + "," + brightness + ")";
        ctx.beginPath();
        ctx.arc(x, y, point, 0, TAU);
        ctx.fill();
      }
    }
  }

  function draw() {
    if (!w || !h) return;
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, w, h);
    var seconds = elapsed / 1000;
    drawStars(seconds);
    drawGalaxy(seconds);
  }

  function animate(now) {
    frame = 0;
    if (paused || !visible || document.hidden) return;
    if (previous && now - previous < 15.25) {
      frame = window.requestAnimationFrame(animate);
      return;
    }
    var delta = previous ? Math.min(80, now - previous) : 16.7;
    previous = now;
    elapsed += delta;
    var skyTime = elapsed / 1000;
    while (ripples.length && skyTime - ripples[0].startedAt >= RIPPLE) ripples.shift();
    var easing = 1 - Math.exp(-delta / 160);
    pointer.x += (target.x - pointer.x) * easing;
    pointer.y += (target.y - pointer.y) * easing;
    pointer.strength += (target.strength - pointer.strength) * easing;
    draw();
    frame = window.requestAnimationFrame(animate);
  }

  function sync() {
    window.cancelAnimationFrame(frame);
    frame = 0;
    previous = 0;
    if (!paused && visible && !document.hidden) frame = window.requestAnimationFrame(animate);
    else draw();
  }

  function layoutHorizon() {
    if (!w || !h) return;
    var resting = buildCrest(0, 0.5, 0.62, 0, []);
    var cols = resting.length - 1;
    for (var index = 0; index < stars.length; index++) {
      var star = stars[index];
      if (!star.tuck) continue;
      var column = Math.max(0, Math.min(cols, Math.round(star.x * cols)));
      star.y = resting[column] / h + star.tuck;
    }
  }

  function resize() {
    var rect = field.getBoundingClientRect();
    w = rect.width;
    h = rect.height;
    var dpr = Math.min(window.devicePixelRatio || 1, 1.75);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  function paintMotion() {
    if (!toggle) return;
    var locked = reduce.matches && !manual;
    toggle.disabled = locked;
    toggle.setAttribute("aria-pressed", paused ? "true" : "false");
    toggle.setAttribute("aria-label", locked ? "Motion off" : paused ? "Play motion" : "Pause motion");
  }

  function setPaused(value) {
    paused = value;
    paintMotion();
    sync();
  }

  document.addEventListener("pointermove", function (event) {
    if (paused || !fine.matches || event.pointerType === "touch") return;
    target.x = event.clientX / Math.max(w, 1);
    target.y = event.clientY / Math.max(h, 1);
    if (pointer.strength < 0.001) {
      pointer.x = target.x;
      pointer.y = target.y;
    }
    target.strength = 1;
    if (!frame) sync();
  }, { passive: true });

  document.addEventListener("pointerleave", function () {
    target.strength = 0;
  });

  document.addEventListener("click", function (event) {
    if (paused || !w || !h || event.button !== 0) return;
    var node = event.target;
    if (node && node.closest && node.closest("a, button, input, textarea, select, label, summary, [data-landscape-control]")) return;
    ripples.push({ x: event.clientX / w, y: event.clientY / h, startedAt: elapsed / 1000 });
    if (ripples.length > 4) ripples.shift();
    if (!frame) sync();
  });

  if (toggle) {
    toggle.addEventListener("click", function () {
      if (reduce.matches) return;
      manual = !paused;
      setPaused(!paused);
    });
  }

  reduce.addEventListener("change", function () {
    if (!manual) setPaused(reduce.matches);
  });

  var resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(field);
  var intersection = new IntersectionObserver(function (entries) {
    visible = entries[0].isIntersecting;
    sync();
  });
  intersection.observe(field);
  document.addEventListener("visibilitychange", sync);
  paintMotion();
  resize();
  sync();
})();
