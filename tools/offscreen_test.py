"""离屏验证 Duo 折叠着色器 -- 不需要真窗口, 出 PNG + 数值断言。

为什么要数值断言: 光保存图片无法自动判断"背景兜底到底生效了没有",
也发现不了着色器整体变黑之类的静默失败。所以这里除了出图, 还统计亮度:

  1) tilt=0 必须是恒等映射 -- 渲染结果要等于原图, 否则说明采样/翻转写错了
  2) tilt=60 时 backdrop 模式的顶部区域必须显著亮于 black 模式
     (顶部离铰链最远, 是视线逃逸最多的地方, 正是黑场与背景的区别所在)
  3) tilt=60 两种模式的中下部 (靠铰链) 都必须仍然清晰 -- 空间感的核心

用法:
    .venv\\Scripts\\python.exe tools\\offscreen_test.py
    .venv\\Scripts\\python.exe tools\\offscreen_test.py --tilt 75
"""
import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PyQt6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram
from PyQt6.QtWidgets import QApplication
from OpenGL import GL

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from render.shader import FS_DUO, VS  # noqa: E402

# 输出重定向(管道)时 Windows 会按 cp936 编码, 导致中文乱码; 强制 UTF-8。
# 直接挂在控制台上时 Python 本来就写 UTF-8, 这一步不影响。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

OUT_DIR = Path(__file__).resolve().parent.parent
TEX_W, TEX_H = 1920, 1200
W, H = 960, 600

# 合并前的原版着色器, 逐字取自 windowsduo/win/glass_overlay.py 的 FS_DUO。
# 保留它是为了做回归比对: outside=black 时合并版必须和它逐像素一致,
# 这样才能证明"加了背景兜底"没有顺手改动原本的动画。
ORIGINAL_FS = """#version 330 compatibility
uniform sampler2D uTex;
uniform vec2  uRes;
uniform float uTilt;
uniform float uEyeZ;
uniform float uSpread;
uniform float uDark;
uniform int   uMaxTaps;
varying vec2 vUV;

const float GOLDEN = 2.39996322972865332;
const float TWO_PI = 6.28318530717958648;

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}

void main() {
    vec2 p = vUV * uRes;
    float tilt = uTilt;
    vec2 uvFlat = vec2(vUV.x, 1.0 - vUV.y);

    if (tilt < 1e-5) {
        gl_FragColor = vec4(texture(uTex, uvFlat).rgb, 1.0);
        return;
    }

    float d = p.y;
    vec3 glass = vec3(p.x, d * cos(tilt), d * sin(tilt));
    vec3 eye   = vec3(uRes * 0.5, uEyeZ);

    float depth = eye.z - glass.z;
    if (depth <= 1e-3) { gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
    float t   = eye.z / depth;
    vec2 hit  = eye.xy + (glass.xy - eye.xy) * t;

    float gap    = glass.z;
    float radius = uSpread * gap;

    if (hit.x < -radius || hit.x > uRes.x + radius ||
        hit.y < -radius || hit.y > uRes.y + radius) {
        gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return;
    }

    float att = max(1.0 - uDark * radius, 0.0);

    vec2 uvHit = vec2(hit.x / uRes.x, 1.0 - hit.y / uRes.y);

    if (radius < 0.5) {
        gl_FragColor = vec4(textureLod(uTex, uvHit, 0.0).rgb * att, 1.0);
        return;
    }

    float lod  = clamp(log2(max(radius, 1.0) / 16.0), 0.0, 6.0);
    float effR = radius / exp2(lod);

    int taps = int(clamp(effR * 2.0, 6.0, float(uMaxTaps)));
    float rot = hash21(gl_FragCoord.xy) * TWO_PI;

    float footX = (radius + 1.0) / uRes.x;
    float footY = (radius + 1.0) / uRes.y;

    vec3 sum = vec3(0.0);
    for (int i = 0; i < taps; ++i) {
        float r = effR * sqrt((float(i) + 0.5) / float(taps));
        float a = float(i) * GOLDEN + rot;
        vec2 off = r * vec2(cos(a), sin(a));
        vec2 uv  = uvHit + vec2(off.x / uRes.x, -off.y / uRes.y);
        float cx = smoothstep(0.0, footX, uv.x) * (1.0 - smoothstep(1.0 - footX, 1.0, uv.x));
        float cy = smoothstep(0.0, footY, 1.0 - uv.y) * (1.0 - smoothstep(1.0 - footY, 1.0, 1.0 - uv.y));
        sum += textureLod(uTex, uv, lod).rgb * cx * cy;
    }
    vec3 c = sum / float(taps) * att;
    gl_FragColor = vec4(c, 1.0);
}
"""


def make_source_texture():
    """合成"桌面": 渐变底 + 网格 + 色块, 便于判断模糊/翻转/取样方向。"""
    img = Image.new("RGB", (TEX_W, TEX_H))
    dr = ImageDraw.Draw(img)
    for x in range(0, TEX_W, 4):
        dr.line([(x, 0), (x, TEX_H)],
                fill=(int(255 * x / TEX_W), 40, int(255 - 255 * x / TEX_W)))
    for gx in range(0, TEX_W, 120):
        dr.line([(gx, 0), (gx, TEX_H)], fill=(255, 255, 255), width=3)
    for gy in range(0, TEX_H, 120):
        dr.line([(0, gy), (TEX_W, gy)], fill=(255, 255, 255), width=3)
    dr.rectangle([100, 100, 300, 250], fill=(0, 200, 0))
    dr.rectangle([1600, 900, 1850, 1100], fill=(255, 255, 0))
    return img


def make_backdrop_texture():
    """合成"背景壁纸": 暖色底 + 大色块 + 高频细纹。

    刻意加入高频细纹, 这样"背景模糊强度"才有可观测的效果 --
    纯色/大色块的背景无论怎么模糊都几乎不变, 测不出东西来。
    """
    img = Image.new("RGB", (TEX_W, TEX_H), (230, 140, 40))
    dr = ImageDraw.Draw(img)
    for i in range(0, TEX_W + TEX_H, 200):
        dr.ellipse([i - 150, i - 150, i + 150, i + 150], fill=(250, 200, 90))
    for y in range(0, TEX_H, 6):
        dr.line([(0, y), (TEX_W, y)], fill=(120, 60, 20))
    for x in range(0, TEX_W, 6):
        dr.line([(x, 0), (x, TEX_H)], fill=(255, 245, 200))
    return img


class Offscreen:
    def __init__(self):
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        QSurfaceFormat.setDefaultFormat(fmt)
        self.app = QApplication([sys.argv[0]])

        self.surface = QOffscreenSurface()
        self.surface.create()
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(fmt)
        assert self.ctx.create(), "GL context 创建失败"
        assert self.ctx.makeCurrent(self.surface), "makeCurrent 失败"

        self.prog = self._compile(FS_DUO, "合并版")
        self.prog_orig = self._compile(ORIGINAL_FS, "原版")
        self.prog.bind()
        self.prog_orig.bind()

        self.tex = self._upload(make_source_texture())
        self.bd = self._upload(make_backdrop_texture())

        self.fbo = GL.glGenFramebuffers(1)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        self.out_tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.out_tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, W, H, 0, GL.GL_RGBA,
                        GL.GL_UNSIGNED_BYTE, None)
        GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0,
                                  GL.GL_TEXTURE_2D, self.out_tex, 0)
        status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
        assert status == GL.GL_FRAMEBUFFER_COMPLETE, "FBO 不完整: %s" % status

    @staticmethod
    def _compile(fs_source, label):
        prog = QOpenGLShaderProgram()
        ok_v = prog.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Vertex, VS)
        ok_f = prog.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, fs_source)
        assert ok_v and ok_f, "%s 着色器编译失败:\n%s" % (label, prog.log())
        assert prog.link(), "%s 着色器链接失败:\n%s" % (label, prog.log())
        return prog

    @staticmethod
    def _upload(img):
        tex = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, img.width, img.height, 0,
                        GL.GL_RGB, GL.GL_UNSIGNED_BYTE, img.tobytes())
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER,
                           GL.GL_LINEAR_MIPMAP_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        return tex

    def render(self, tilt_deg, outside=0, eye_h=2.0, spread=0.42, dark=0.001,
               taps=32, bg_blur=1.0, orig=False):
        prog = self.prog_orig if orig else self.prog
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glViewport(0, 0, W, H)
        prog.bind()

        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.tex)
        GL.glUniform1i(prog.uniformLocation("uTex"), 0)

        # 原版着色器没有 uBackdrop/uOutside/uBgBlur, 但 glUniform*
        # 对 location=-1 是合法的空操作, 所以这里可以无差别设置
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.bd)
        GL.glUniform1i(prog.uniformLocation("uBackdrop"), 1)

        u = prog.uniformLocation
        GL.glUniform2f(u("uRes"), float(TEX_W), float(TEX_H))
        GL.glUniform1f(u("uTilt"), tilt_deg * 3.14159265 / 180.0)
        GL.glUniform1f(u("uEyeZ"), eye_h * TEX_H)
        GL.glUniform1f(u("uSpread"), spread)
        GL.glUniform1f(u("uDark"), dark)
        GL.glUniform1i(u("uMaxTaps"), taps)
        GL.glUniform1i(u("uOutside"), outside)
        GL.glUniform1f(u("uBgBlur"), bg_blur)

        GL.glBegin(GL.GL_QUADS)
        GL.glTexCoord2f(0, 0); GL.glVertex2f(-1, -1)
        GL.glTexCoord2f(1, 0); GL.glVertex2f(1, -1)
        GL.glTexCoord2f(1, 1); GL.glVertex2f(1, 1)
        GL.glTexCoord2f(0, 1); GL.glVertex2f(-1, 1)
        GL.glEnd()
        GL.glFlush()

        buf = GL.glReadPixels(0, 0, W, H, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 3)
        return arr[::-1].copy()          # GL 原点在左下, 翻成 top-down


def luma(arr):
    return arr.astype(np.float32) @ np.array([0.299, 0.587, 0.114], np.float32)


def band(arr, frac):
    """取顶部 frac 比例的横带 (离铰链最远 -> 视线最容易逃逸)。"""
    return luma(arr[:max(1, int(H * frac))]).mean()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tilt", type=float, default=60.0)
    args = ap.parse_args()

    off = Offscreen()
    failures = []

    # ---------- 1) tilt=0 必须恒等 ----------
    # 基准必须用 BOX (2x2 面积平均): 输出是纹理的一半尺寸, GL 在 2 倍缩小下
    # 取到的正是 mip1, 即精确的 2x2 盒式平均。用 PIL 双线性比会在高对比网格线
    # 上因重采样核不同而虚报误差。
    flat = off.render(0.0)
    ref = np.asarray(make_source_texture().resize((W, H), Image.BOX), dtype=np.int16)
    diff = np.abs(flat.astype(np.int16) - ref).mean()
    print("[1] tilt=0 恒等性      : 平均像素差 %.2f / 255" % diff)
    if diff > 3.0:
        failures.append("tilt=0 不是恒等映射 (差 %.2f)" % diff)
    Image.fromarray(flat).save(str(OUT_DIR / "offscreen_tilt0.png"))

    # ---------- 2) 出界: black vs backdrop ----------
    black = off.render(args.tilt, outside=0)
    back = off.render(args.tilt, outside=1)
    Image.fromarray(black).save(str(OUT_DIR / ("offscreen_tilt%d_black.png"
                                               % int(args.tilt))))
    Image.fromarray(back).save(str(OUT_DIR / ("offscreen_tilt%d_backdrop.png"
                                              % int(args.tilt))))

    tb, tbd = band(black, 0.25), band(back, 0.25)
    print("[2] tilt=%g 顶部亮度   : black %.1f   backdrop %.1f   (+%.1f)"
          % (args.tilt, tb, tbd, tbd - tb))
    if tbd <= tb + 3.0:
        failures.append("backdrop 模式顶部没有变亮, 背景兜底未生效")

    hb, hbd = band(black, 1.0), band(back, 1.0)
    print("[3] tilt=%g 全图亮度   : black %.1f   backdrop %.1f" % (args.tilt, hb, hbd))

    # ---------- 3) 靠铰链处仍应清晰 ----------
    bottom = luma(black[int(H * 0.92):]).std()
    top = luma(black[:int(H * 0.08)]).std()
    print("[4] 空间感(black 模式) : 顶部细节 std %.2f   底部细节 std %.2f"
          % (top, bottom))
    if bottom <= top:
        failures.append("底部(靠铰链)没有比顶部(远端)更清晰, 逆投影模型可能错了")

    # ---------- 5) 背景模糊强度确实可调 ----------
    zero = off.render(args.tilt, outside=1, bg_blur=0.0)
    d2 = np.abs(zero.astype(np.int16) - back.astype(np.int16)).mean()
    print("[5] bg_blur=0 vs 1     : 平均像素差 %.2f" % d2)
    if d2 < 0.5:
        failures.append("bg_blur 不起作用 (差仅 %.2f), 背景模糊强度没接上" % d2)

    # ---------- 6) 合并不得改动原版动画 ----------
    # 这是本次合并最关键的一条: outside=black 时, 合并版必须和 windowsduo 的
    # 原版着色器逐像素一致。合并版多算了覆盖率 cov 并用背景补 miss,
    # 但 outside=0 时 backdrop() 恒返回 0, 公式退化成原版, 所以差异应为 0。
    for tilt in (25.0, args.tilt, 80.0):
        merged = off.render(tilt, outside=0)
        original = off.render(tilt, orig=True)
        d3 = np.abs(merged.astype(np.int16) - original.astype(np.int16)).mean()
        mx = np.abs(merged.astype(np.int16) - original.astype(np.int16)).max()
        print("[6] tilt=%4g 对原版差异 : 平均 %.3f  最大 %d" % (tilt, d3, mx))
        if mx > 1:
            failures.append("tilt=%g 处合并版与原版不一致 (最大差 %d), "
                            "合并改动了原本的动画" % (tilt, mx))

    print("-" * 60)
    if failures:
        for f in failures:
            print("[FAIL] " + f)
        return 1
    print("[PASS] 着色器 6 项检查全部通过; PNG 已写入 %s" % OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
