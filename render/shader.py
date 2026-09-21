"""GLSL 着色器 -- windowsduo 的「悬浮玻璃」逆投影模型 (动画内核)。

模型 (来自 windowsduo, 它自己又是融合了 DuoLikeAnimation / iphone-duo /
MacDuo / FrostFold 的复刻):
    界面固定在世界空间的平面上不动; 屏幕是一块玻璃, 绕"铰链"(屏幕底边)向观察者
    方向转开。每个像素: 眼睛 -> 玻璃像素 -> 延长交到界面平面 = 采样点。
    间隙越大 -> 模糊半径越大、越暗; 视线完全出界 -> 原本是纯黑。
    这是空间效果: 靠近铰链处始终清晰, 远离铰链处(屏幕上部)先模糊、先消失。

本次合并新增 ("无黑场"做法):
    outside_mode = "backdrop" 时, 视线出界处不再给纯黑, 而是补一张只模糊不变形、
    对齐屏幕的背景图。默认仍是 "black", 完整保留 windowsduo 原本的观感。

    实现上是在 Vogel 盘采样里额外累计覆盖率 cov: 没被覆盖的比例 miss 用背景色补,
        c = sum / taps * att + backdrop * miss
    uOutside=0 时 backdrop() 返回 0, 公式退化成原版 -- 所以这是严格超集, 不是改写。
"""
# 用 **3.3 core** 而不是 compatibility: 兼容 profile 里的 `varying` /
# `gl_FragColor` / 即时模式在核显 (Intel/AMD) 拿到的**前向兼容上下文**里会被
# 剥掉, 直接编译失败、玻璃层白/黑屏 (NVIDIA 驱动宽松, 一直掩盖了这个坑)。
# core 里: `varying` -> `in`/`out`, `gl_FragColor` -> 自声明 out, 顶点用
# gl_VertexID 生成全屏三角形 (不需要 VBO / 顶点属性)。数学与采样一字未改。
VS = """#version 330 core
out vec2 vUV;
void main() {
    // 用顶点号直接生成一个盖住全屏的三角形 (0,0)/(2,0)/(0,2):
    // 经裁剪后正好覆盖 [0,1]x[0,1], vUV 落在 0..1, 与原来的四边形语义一致
    // (vUV.y=0 在底部, 即铰链)。不需要 VBO / 顶点属性。
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    vUV = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

FS_DUO = """#version 330 core
uniform sampler2D uTex;
uniform sampler2D uBackdrop;
uniform vec2  uRes;        // 截图尺寸 px
uniform float uTilt;       // 玻璃转角 (弧度), 0 = 贴合界面
uniform float uEyeZ;       // 眼睛到界面平面距离 px
uniform float uSpread;     // 单位间隙 -> 模糊半径 (散射半角正切)
uniform float uDark;       // 单位模糊半径损失的光量
uniform int   uMaxTaps;
uniform int   uOutside;    // 0 = 出界纯黑(原版)  1 = 背景兜底(无黑场)
uniform float uBgBlur;     // 背景相对前景的模糊比例
in  vec2 vUV;
out vec4 fragColor;

const float GOLDEN = 2.39996322972865332;
const float TWO_PI = 6.28318530717958648;

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

// 背景: 只模糊不变形, 对齐屏幕 (背景壁纸铺满整屏)
vec3 backdrop(vec2 screenUV, float radius) {
    if (uOutside == 0) return vec3(0.0);
    float lodB = clamp(log2(max(radius * uBgBlur, 1.0) / 32.0), 0.0, 6.0);
    return textureLod(uBackdrop, screenUV, lodB).rgb;
}

void main() {
    // vUV: (0,0)=左下 (GL 约定)。铰链 = 屏幕底边。
    vec2 p = vUV * uRes;                 // y 自底向上, y=0 在铰链
    float tilt = uTilt;
    vec2 uvFlat = vec2(vUV.x, 1.0 - vUV.y);   // 平视采样 (截图行序 top-first)

    if (tilt < 1e-5) {
        fragColor = vec4(texture(uTex, uvFlat).rgb, 1.0);
        return;
    }

    // 玻璃像素放到 3D: 绕底边铰链旋转 tilt, 上部向观察者抬升
    float d = p.y;                                    // 该像素到铰链的距离
    vec3 glass = vec3(p.x, d * cos(tilt), d * sin(tilt));
    vec3 eye   = vec3(uRes * 0.5, uEyeZ);             // 眼睛: 屏幕中心正前上方

    // 光线 eye -> glass 像素, 延长交到界面平面 z=0
    float depth = eye.z - glass.z;
    if (depth <= 1e-3) { fragColor = vec4(backdrop(uvFlat, 0.0), 1.0); return; }
    float t   = eye.z / depth;
    vec2 hit  = eye.xy + (glass.xy - eye.xy) * t;     // 界面平面上的落点 (px, y 向上)

    // 玻璃与界面的间隙 -> 模糊半径
    float gap    = glass.z;
    float radius = uSpread * gap;

    // 整个模糊核都在界面之外
    if (hit.x < -radius || hit.x > uRes.x + radius ||
        hit.y < -radius || hit.y > uRes.y + radius) {
        fragColor = vec4(backdrop(uvFlat, radius), 1.0); return;
    }

    // 磨砂玻璃吸光: 与散射成正比地变暗
    float att = max(1.0 - uDark * radius, 0.0);

    vec2 uvHit = vec2(hit.x / uRes.x, 1.0 - hit.y / uRes.y);

    if (radius < 0.5) {
        fragColor = vec4(textureLod(uTex, uvHit, 0.0).rgb * att, 1.0);
        return;
    }

    // mip LOD: 大半径先降到低分辨率 mip 再盘式采样
    float lod  = clamp(log2(max(radius, 1.0) / 16.0), 0.0, 6.0);
    float effR = radius / exp2(lod);

    // Vogel 盘: sqrt 均匀面密度 + 黄金角 + 每像素随机旋转 -> 磨砂颗粒
    // taps 上界是 uMaxTaps (配置), 下界 6。**顺序很重要**: 原来的
    // `clamp(x, 6.0, float(uMaxTaps))` 在 uMaxTaps < 6 时 min > max, GLSL 规范里
    // 结果**未定义** —— 先 min(uMaxTaps) 再 max(6) 就永远合法。
    int taps = int(max(min(effR * 2.0, float(uMaxTaps)), 6.0));
    float rot = hash21(gl_FragCoord.xy) * TWO_PI;

    // 边缘覆盖率: 采样核出界部分按比例衰减, 不出现硬边
    float footX = (radius + 1.0) / uRes.x;
    float footY = (radius + 1.0) / uRes.y;

    vec3 sum = vec3(0.0);
    float cov = 0.0;
    for (int i = 0; i < taps; ++i) {
        float r = effR * sqrt((float(i) + 0.5) / float(taps));
        float a = float(i) * GOLDEN + rot;
        vec2 off = r * vec2(cos(a), sin(a));          // px, 界面平面坐标
        vec2 uv  = uvHit + vec2(off.x / uRes.x, -off.y / uRes.y);
        float cx = smoothstep(0.0, footX, uv.x) * (1.0 - smoothstep(1.0 - footX, 1.0, uv.x));
        float cy = smoothstep(0.0, footY, 1.0 - uv.y) * (1.0 - smoothstep(1.0 - footY, 1.0, 1.0 - uv.y));
        float w  = cx * cy;
        sum += textureLod(uTex, uv, lod).rgb * w;
        cov += w;
    }

    float n = float(taps);
    vec3 c = sum / n * att;
    // 未被覆盖的比例用背景补 (uOutside=0 时 backdrop 返回 0, 退化成原版)
    float miss = clamp(1.0 - cov / n, 0.0, 1.0);
    if (miss > 0.0) c += backdrop(uvFlat, radius) * miss;

    fragColor = vec4(c, 1.0);
}
"""
